from __future__ import annotations

from datetime import datetime
import logging
import re
import shutil
from pathlib import Path
from threading import RLock

from fastapi import HTTPException

from app.core.config import _parse_project_roots, default_conda_base_candidates, get_settings
from app.core.paths import (
    ARTIFACT_POINTERS_DIR,
    ARTIFACT_STORE_DIR,
    CHAT_DIR,
    CONFIGS_DIR,
    DATA_DIR,
    GRAPH_DIR,
    REPORTS_DIR,
    RESULTS_DIR,
    RUNS_DIR,
    SCRIPTS_DIR,
    project_root,
)
from app.models.cards import Card, CardAssetRef
from app.models.executor import ExecutorContext, ExecutorReference
from app.models.graph import ACTIVE_RUN_STATUSES, Asset, Claim, GraphState, Module, ModuleRef, ReportItem
from app.models.output_contracts import CardOutputSpec
from app.models.project import (
    DataDirectoryMount,
    ProjectRegistry,
    ProjectRegistryEntry,
    ProjectRuntimePreferences,
    ProjectState,
    ProjectSummary,
)
from app.services.asset_materialization_service import AssetMaterializationService
from app.services.git_service import GitService
from app.services.graph_store import GraphStore
from app.services.utils import atomic_write_json, read_json, resolve_within, sha256_file, utc_now
from app.workers.registry import build_worker_registry


PROJECT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
logger = logging.getLogger(__name__)


class ProjectService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.settings.data_root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, RLock] = {}

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------
    def _registry_path(self) -> Path:
        return self.settings.data_root / "_system" / "project_registry.json"

    def _load_registry(self) -> ProjectRegistry:
        path = self._registry_path()
        try:
            raw = read_json(path, {"items": []})
            if not isinstance(raw, dict):
                raise ValueError("Registry file is not a JSON object")
            return ProjectRegistry.model_validate(raw)
        except Exception as exc:
            logger.exception("Failed to load project registry from %s", path)
            raise RuntimeError(f"Project registry corrupted: {exc}") from exc

    def _save_registry(self, registry: ProjectRegistry) -> None:
        atomic_write_json(self._registry_path(), registry.model_dump())

    def _resolve_project_root(self, project_id: str) -> Path:
        """Resolve the project root directory, preferring registry entries."""
        self._validate_project_id(project_id)
        try:
            registry = self._load_registry()
            for entry in registry.items:
                if entry.project_id == project_id:
                    return Path(entry.project_root)
        except RuntimeError:
            # Registry corrupted — fall through to legacy
            pass
        # Legacy fallback
        return project_root(self.settings.data_root, project_id)

    def _get_registry_entry(self, project_id: str) -> ProjectRegistryEntry | None:
        try:
            registry = self._load_registry()
            for entry in registry.items:
                if entry.project_id == project_id:
                    return entry
        except RuntimeError:
            pass
        return None

    def _add_registry_entry(
        self,
        project_id: str,
        name: str,
        project_root_path: Path,
        root_kind: str = "managed_project_directory",
    ) -> None:
        registry = self._load_registry()
        now = utc_now()
        # Remove existing entry for same project_id if present
        registry.items = [e for e in registry.items if e.project_id != project_id]
        registry.items.append(
            ProjectRegistryEntry(
                project_id=project_id,
                name=name,
                project_root=str(project_root_path.resolve()),
                root_kind=root_kind,  # type: ignore[arg-type]
                created_at=now,
                updated_at=now,
            )
        )
        self._save_registry(registry)

    def _remove_registry_entry(self, project_id: str) -> None:
        try:
            registry = self._load_registry()
            before = len(registry.items)
            registry.items = [e for e in registry.items if e.project_id != project_id]
            if len(registry.items) < before:
                self._save_registry(registry)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def project_path(self, project_id: str) -> Path:
        return self._resolve_project_root(project_id)

    def lock_for(self, project_id: str) -> RLock:
        if project_id not in self._locks:
            self._locks[project_id] = RLock()
        return self._locks[project_id]

    def graph_store(self, project_id: str) -> GraphStore:
        return GraphStore(self.project_path(project_id))

    def git_service(self, project_id: str) -> GitService:
        return GitService(self.project_path(project_id))

    def ensure_seed_project(self) -> None:
        if self.project_path(self.settings.default_project_id).exists():
            return
        self.create_project(
            project_id=self.settings.default_project_id,
            name=self.settings.default_project_name,
            current_goal="完成 RNA-seq 差异表达与下游解释分析",
            seed_demo=True,
        )

    def list_projects(self) -> list[ProjectSummary]:
        projects: list[ProjectSummary] = []
        seen_ids: set[str] = set()

        # 1) Registry entries (authoritative)
        try:
            registry = self._load_registry()
            for entry in registry.items:
                root = Path(entry.project_root)
                if not root.exists():
                    now = utc_now()
                    projects.append(
                        ProjectSummary(
                            project_id=entry.project_id,
                            name=entry.name,
                            status="error",
                            schema_version=self.settings.schema_version,
                            current_goal="Project directory is missing or inaccessible",
                            created_at=entry.created_at,
                            updated_at=entry.updated_at,
                            runtime_preferences=ProjectRuntimePreferences(),
                            project_root=entry.project_root,
                            root_kind=entry.root_kind,
                            card_counts={},
                            result_counts={},
                        )
                    )
                    seen_ids.add(entry.project_id)
                    continue
                try:
                    summary = self._project_summary(entry.project_id)
                    summary = summary.model_copy(
                        update={
                            "project_root": entry.project_root,
                            "root_kind": entry.root_kind,
                        }
                    )
                    projects.append(summary)
                    seen_ids.add(entry.project_id)
                except Exception as exc:
                    logger.exception("Failed to load project summary for %s", entry.project_id)
                    now = utc_now()
                    projects.append(
                        ProjectSummary(
                            project_id=entry.project_id,
                            name=entry.name,
                            status="error",
                            schema_version=self.settings.schema_version,
                            current_goal=f"Project failed to load: {exc}",
                            created_at=entry.created_at,
                            updated_at=now,
                            runtime_preferences=ProjectRuntimePreferences(),
                            project_root=entry.project_root,
                            root_kind=entry.root_kind,
                            card_counts={"corrupted": 1},
                            result_counts={},
                        )
                    )
                    seen_ids.add(entry.project_id)
        except RuntimeError as exc:
            logger.error("Registry error during list_projects: %s", exc)

        # 2) Legacy fallback scan
        if self.settings.data_root.exists():
            for child in sorted(self.settings.data_root.iterdir()):
                if not child.is_dir():
                    continue
                if child.name.startswith("_"):
                    continue
                if child.name in seen_ids:
                    continue
                # Skip directories that do not look like Blueprint projects
                if not (child / "project.json").exists():
                    continue
                try:
                    projects.append(self._project_summary(child.name))
                    seen_ids.add(child.name)
                except Exception as exc:
                    logger.exception("Failed to load project summary for %s", child.name)
                    self._write_project_recovery_marker(child, f"Project failed to load during list_projects: {exc}")
                    now = utc_now()
                    projects.append(
                        ProjectSummary(
                            project_id=child.name,
                            name=f"{child.name} (corrupted)",
                            status="error",
                            schema_version=self.settings.schema_version,
                            current_goal=f"Project failed to load: {exc}",
                            created_at=now,
                            updated_at=now,
                            runtime_preferences=ProjectRuntimePreferences(),
                            card_counts={"corrupted": 1},
                            result_counts={},
                        )
                    )
                    seen_ids.add(child.name)

        return projects

    def _project_summary(self, project_id: str) -> ProjectSummary:
        store = self.graph_store(project_id)
        project = self._project_state_with_runtime_preferences(store)
        cards = store.load_cards()
        assets = store.load_assets()
        return ProjectSummary(
            **project.model_dump(),
            card_counts=self._count_by(cards, "status"),
            result_counts=self._count_by(assets, "status"),
        )

    @staticmethod
    def _write_project_recovery_marker(root: Path, reason: str) -> None:
        try:
            atomic_write_json(
                root / "project_recovery_required.json",
                {
                    "reason": reason,
                    "created_at": utc_now(),
                },
            )
        except Exception:
            logger.exception("Failed to write project recovery marker for %s", root)

    def create_project(
        self,
        project_id: str,
        name: str,
        current_goal: str,
        seed_demo: bool = False,
        data_directory: DataDirectoryMount | None = None,
        root_kind: str = "legacy_data_root",
    ) -> ProjectState:
        root = self.project_path(project_id)
        if root.exists():
            raise HTTPException(status_code=409, detail=f"Project already exists: {project_id}")
        root.mkdir(parents=True, exist_ok=True)
        self._scaffold_project_directories(root)

        now = utc_now()
        runtime_preferences = ProjectRuntimePreferences(
            python_runtime=self.settings.default_python_runtime,
            r_runtime=self.settings.default_r_runtime,
        )
        state = ProjectState(
            project_id=project_id,
            name=name,
            status="active",
            schema_version=self.settings.schema_version,
            current_goal=current_goal,
            created_at=now,
            updated_at=now,
            runtime_preferences=runtime_preferences,
            data_directory=data_directory,
            root_kind=root_kind,  # type: ignore[arg-type]
        )
        store = GraphStore(root)
        store.save_project_state(state)
        store.save_cards([])
        store.save_graph(
            GraphState(
                metadata={
                    "schema_version": self.settings.schema_version,
                    "runtime_preferences": state.runtime_preferences.model_dump(),
                    "default_conda_env": state.runtime_preferences.python_runtime,
                    "default_r_env": state.runtime_preferences.r_runtime,
                }
            )
        )
        store.save_proposals([])
        store.save_chat_sessions([])
        store.save_project_memory([])
        atomic_write_json(root / "graph" / "cleanup.json", [])
        (root / "configs" / "params.yaml").write_text(
            f"project_id: {project_id}\nname: {name}\n",
            encoding="utf-8",
        )
        (root / ".gitignore").write_text(
            "\n".join(
                [
                    "data/**",
                    "results/**/*.h5ad",
                    "results/**/*.bam",
                    "results/**/*.fastq",
                    "results/**/*.fq",
                    "results/**/*.cram",
                    "artifact_store/**",
                    "!artifacts/pointers/*.json",
                    "__pycache__/",
                    ".pytest_cache/",
                    "chat/**",
                    "work/**",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        if seed_demo:
            self._seed_demo(store, state)
        git = GitService(root)
        git.init_repo()
        git.commit("Initialize project scaffold")
        return state

    def _scaffold_project_directories(self, root: Path) -> None:
        """Create the standard Blueprint project directory structure under root."""
        for relative in [
            GRAPH_DIR,
            f"{GRAPH_DIR}/patches",
            CHAT_DIR,
            RUNS_DIR,
            RESULTS_DIR,
            REPORTS_DIR,
            ARTIFACT_POINTERS_DIR,
            ARTIFACT_STORE_DIR,
            f"{SCRIPTS_DIR}/generated",
            f"{SCRIPTS_DIR}/curated",
            CONFIGS_DIR,
            DATA_DIR,
            "work",
            "memory",
        ]:
            (root / relative).mkdir(parents=True, exist_ok=True)

    def create_project_from_directory(
        self,
        root_id: str,
        parent_path: str,
        directory_name: str,
        project_id: str,
        name: str,
        current_goal: str,
    ) -> ProjectState:
        """Create a managed Blueprint project under data_root and mount a user-selected data directory."""
        # ---- 1. Validation that does not touch disk ----
        self._validate_project_id(project_id)
        if not directory_name or directory_name.strip() == "." or directory_name.strip() == "..":
            raise HTTPException(status_code=422, detail="Directory name must not be empty or a relative path token.")
        if "/" in directory_name or "\\" in directory_name:
            raise HTTPException(status_code=422, detail="Directory name must not contain path separators.")

        # Check project_id conflicts before any disk mutation
        existing_entry = self._get_registry_entry(project_id)
        if existing_entry is not None:
            raise HTTPException(status_code=409, detail=f"Project ID already registered: {project_id}")
        if (self.settings.data_root / project_id).exists():
            raise HTTPException(status_code=409, detail=f"Project ID already exists in data root: {project_id}")

        # Validate the selected data directory
        relative_path = f"{parent_path.strip('/').rstrip('/')}/{directory_name.strip('/')}".strip("/")
        resolved_data_dir = self._validate_data_directory(root_id, relative_path, project_id)

        # ---- 2. Create managed project under data_root and mount data directory ----
        now = utc_now()
        mount = DataDirectoryMount(
            root_id=root_id,
            path=relative_path,
            resolved_path=str(resolved_data_dir),
            mounted_at=now,
        )

        state = self.create_project(
            project_id=project_id,
            name=name,
            current_goal=current_goal,
            data_directory=mount,
            root_kind="managed_project_directory",
        )

        # Register in project registry
        self._add_registry_entry(project_id, name, self.project_path(project_id), "managed_project_directory")
        return state

    def delete_project(self, project_id: str, delete_directory: bool = False) -> None:
        root = self.project_path(project_id)
        registry_entry = self._get_registry_entry(project_id)

        if not root.exists() and registry_entry is None:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        if len(self.list_projects()) <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the only project.")

        lock = self.lock_for(project_id)
        with lock:
            store = self.graph_store(project_id)
            graph = store.load_graph()
            active_runs = [run for run in graph.runs if run.status in ACTIVE_RUN_STATUSES]
            if active_runs:
                raise HTTPException(
                    status_code=409,
                    detail=f"Project {project_id} has active runs ({', '.join(r.run_id for r in active_runs)}) and cannot be deleted.",
                )

            # Remove registry entry first (if any), then optionally delete directory.
            self._remove_registry_entry(project_id)

            # For managed projects: default is remove-from-registry-only.
            # For legacy projects (no registry entry): default is delete-directory
            # to preserve backwards compatibility with the pre-registry behavior.
            should_delete_dir = delete_directory or (registry_entry is None)
            if should_delete_dir and root.exists():
                shutil.rmtree(root)

        self._locks.pop(project_id, None)

    # ------------------------------------------------------------------
    # Workspace roots helper (shared with workspace_roots API)
    # ------------------------------------------------------------------
    def workspace_roots(self) -> list[dict]:
        return self._workspace_roots()

    def _workspace_roots(self) -> list[dict]:
        roots: list[dict] = [{"root_id": "home", "label": "Home", "path": str(Path.home().resolve())}]
        extra = _parse_project_roots(self.settings.project_roots)
        for idx, p in enumerate(extra, start=1):
            roots.append({
                "root_id": f"extra_{idx}",
                "label": str(p.name) or str(p),
                "path": str(p),
            })
        return roots

    def data_directory_roots(self) -> list[dict]:
        roots: list[dict] = [{"root_id": "home", "label": "Home", "path": str(Path.home().resolve())}]
        extra = _parse_project_roots(self.settings.data_directory_roots)
        for idx, p in enumerate(extra, start=1):
            roots.append({
                "root_id": f"extra_{idx}",
                "label": str(p.name) or str(p),
                "path": str(p),
            })
        return roots

    def _validate_data_directory(self, root_id: str, relative_path: str, project_id: str) -> Path:
        """Validate a selected data directory and return its resolved path."""
        roots = self.data_directory_roots()
        root_info = next((r for r in roots if r["root_id"] == root_id), None)
        if root_info is None:
            raise HTTPException(status_code=404, detail=f"Data directory root not found: {root_id}")

        root_path = Path(root_info["path"]).resolve()
        target = (root_path / relative_path.strip("/")).resolve()

        # Boundary check
        if target != root_path and root_path not in target.parents:
            raise HTTPException(status_code=403, detail="Selected data directory is outside the allowed root.")

        # Must exist and be readable
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Data directory does not exist: {relative_path}")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail=f"Path is not a directory: {relative_path}")

        # Symlink escape check
        if target.is_symlink():
            real_target = target.resolve()
            if real_target != root_path and root_path not in real_target.parents:
                raise HTTPException(status_code=403, detail="Symlink points outside the allowed root.")

        # Reject Blueprint project state inside the data directory
        if (target / "project.json").exists() or (target / "graph" / "cards.json").exists():
            raise HTTPException(
                status_code=409,
                detail="Selected directory already contains a Blueprint project. Choose a different directory.",
            )

        # Overlap check: mounted dir must not equal or contain the managed project dir,
        # and must not be inside it.
        managed_project = project_root(self.settings.data_root, project_id).resolve()
        try:
            target.relative_to(managed_project)
            raise HTTPException(status_code=409, detail="Data directory cannot be inside the managed project directory.")
        except ValueError:
            pass
        try:
            managed_project.relative_to(target)
            raise HTTPException(status_code=409, detail="Data directory cannot contain the managed project directory.")
        except ValueError:
            pass

        return target

    def set_project_data_directory(self, project_id: str, root_id: str, path: str) -> DataDirectoryMount:
        """Mount a data directory to an existing project."""
        if not (self.project_path(project_id) / "project.json").exists():
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        resolved_path = self._validate_data_directory(root_id, path, project_id)

        mount = DataDirectoryMount(
            root_id=root_id,
            path=path.strip("/"),
            resolved_path=str(resolved_path),
            mounted_at=utc_now(),
        )

        with self.lock_for(project_id):
            store = self.graph_store(project_id)
            project = self._project_state_with_runtime_preferences(store)
            project = project.model_copy(update={"data_directory": mount, "updated_at": utc_now()})
            store.save_project_state(project)

        return mount

    def get_project_data_directory(self, project_id: str) -> DataDirectoryMount | None:
        if not (self.project_path(project_id) / "project.json").exists():
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
        store = self.graph_store(project_id)
        project = self._project_state_with_runtime_preferences(store)
        return project.data_directory

    def detach_project_data_directory(self, project_id: str) -> DataDirectoryMount | None:
        """Detach the mounted data directory from a project.

        Removes only the mount record; the user data directory is left untouched.
        Registered data_mount/... assets are marked as unavailable because their
        source paths can no longer be resolved.
        """
        if not (self.project_path(project_id) / "project.json").exists():
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        with self.lock_for(project_id):
            store = self.graph_store(project_id)
            project = self._project_state_with_runtime_preferences(store)
            mount = project.data_directory
            if mount is None:
                return None

            # Mark data_mount/... assets as unavailable
            graph = store.load_graph()
            changed = False
            for asset in graph.assets:
                if asset.path.startswith("data_mount/") and asset.status not in {"missing", "archived"}:
                    asset.status = "missing"
                    changed = True
            if changed:
                store.save_assets(graph.assets)

            project = project.model_copy(update={"data_directory": None, "updated_at": utc_now()})
            store.save_project_state(project)

        return mount

    def check_data_mount_assets_freshness(self, project_id: str) -> list[dict]:
        """Check freshness of all data_mount/... assets and mark stale/missing ones.

        Returns a list of issues found (asset_id, path, reason).
        """
        if not (self.project_path(project_id) / "project.json").exists():
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        mount = self.get_project_data_directory(project_id)
        data_root = Path(mount.resolved_path) if mount else None

        with self.lock_for(project_id):
            store = self.graph_store(project_id)
            graph = store.load_graph()
            issues: list[dict] = []
            changed = False

            for asset in graph.assets:
                if not asset.path.startswith("data_mount/"):
                    continue
                if asset.status == "archived":
                    continue

                # If no mount record, mark missing immediately
                if data_root is None or not data_root.exists():
                    asset.status = "missing"
                    issues.append({"asset_id": asset.asset_id, "path": asset.path, "reason": "mounted data directory is not available"})
                    changed = True
                    continue

                relative = asset.metadata.get("mount_relative_path", asset.path.replace("data_mount/", "", 1))
                try:
                    target = resolve_within(data_root, relative) if relative else data_root
                except ValueError:
                    asset.status = "missing"
                    issues.append({"asset_id": asset.asset_id, "path": asset.path, "reason": "path is outside mounted data directory"})
                    changed = True
                    continue

                if not target.exists():
                    asset.status = "missing"
                    issues.append({"asset_id": asset.asset_id, "path": asset.path, "reason": "source file no longer exists"})
                    changed = True
                    continue

                # Compare registered metadata with current file stat
                stat = target.stat()
                registered_size = asset.metadata.get("registered_size_bytes")
                registered_mtime = asset.metadata.get("registered_mtime")
                integrity_kind = asset.metadata.get("integrity_kind", "size_mtime")

                size_changed = registered_size is not None and stat.st_size != registered_size
                mtime_changed = False
                if registered_mtime:
                    try:
                        registered_mtime_dt = datetime.fromisoformat(registered_mtime.replace("Z", "+00:00"))
                        mtime_changed = abs(stat.st_mtime - registered_mtime_dt.timestamp()) > 1
                    except (ValueError, OSError):
                        mtime_changed = True

                digest_mismatch = False
                if integrity_kind == "sha256" and not size_changed and not mtime_changed:
                    registered_sha256 = asset.metadata.get("sha256")
                    if registered_sha256:
                        try:
                            current_digest = sha256_file(target)
                            digest_mismatch = current_digest != registered_sha256
                        except OSError:
                            digest_mismatch = True

                if size_changed or mtime_changed or digest_mismatch:
                    asset.status = "stale"
                    reason_parts: list[str] = []
                    if size_changed:
                        reason_parts.append("size changed")
                    if mtime_changed:
                        reason_parts.append("mtime changed")
                    if digest_mismatch:
                        reason_parts.append("sha256 mismatch")
                    issues.append({
                        "asset_id": asset.asset_id,
                        "path": asset.path,
                        "reason": "; ".join(reason_parts) if reason_parts else "integrity check failed",
                    })
                    changed = True

                # Recovery: restore missing/stale assets when mount/file are back and integrity passes
                if asset.status in {"missing", "stale"} and not (size_changed or mtime_changed or digest_mismatch):
                    asset.status = "valid"
                    changed = True

            if changed:
                store.save_assets(graph.assets)

        return issues

    def _seed_demo(self, store: GraphStore, state: ProjectState) -> None:
        now = utc_now()
        modules = [
            Module(
                module_id="module_de_analysis",
                title="差异表达分析",
                type="analysis_module",
                status="accepted",
                summary="比较 Treatment 和 Control 的表达差异。",
                depends_on_assets=["count_matrix_v1", "sample_metadata_v1"],
                expected_outputs=["deg_table", "volcano_plot", "ma_plot"],
                linked_cards=["card_de_analysis"],
                linked_runs=["run_004"],
                created_by="manager_ai",
                created_at=now,
            ),
            Module(
                module_id="module_group_enrichment",
                title="功能富集分析",
                type="module_group",
                status="planned",
                summary="基于 DEG 结果进行 GSEA 和 KEGG 分析。",
                depends_on_assets=["deg_table_v1", "ranked_gene_list_v1"],
                expected_outputs=["gsea_result", "kegg_result", "enrichment_plots"],
                linked_cards=["card_enrichment_group"],
                linked_runs=[],
                submodules=[
                    ModuleRef(module_id="module_gsea", title="GSEA 分析", status="planned"),
                    ModuleRef(module_id="module_kegg", title="KEGG 富集", status="planned"),
                ],
                created_by="manager_ai",
                created_at=now,
            ),
            Module(
                module_id="module_immune_infiltration",
                title="免疫浸润分析",
                type="analysis_module",
                status="proposed",
                summary="等待确认是否增加免疫浸润分析模块。",
                depends_on_assets=["deg_table_v1"],
                expected_outputs=["immune_score_table", "immune_heatmap"],
                linked_cards=["card_immune_module"],
                linked_runs=[],
                created_by="manager_ai",
                created_at=now,
            ),
        ]
        cards = [
            Card(
                card_id="card_de_analysis",
                card_type="module",
                title="差异表达分析",
                status="accepted",
                step=1,
                summary="已完成 Treatment vs Control 的差异表达分析。",
                why="用于识别主要差异基因并作为下游分析输入。",
                inputs=[
                    CardAssetRef(label="计数矩阵", asset_id="count_matrix_v1"),
                    CardAssetRef(label="样本信息", asset_id="sample_metadata_v1"),
                ],
                outputs=[
                    CardOutputSpec(
                        role="deg_table",
                        label="DEG 表",
                        artifact_class="table",
                        accepted_formats=["tsv", "csv"],
                        preferred_format="tsv",
                        asset_id="deg_table_v1",
                    ),
                    CardOutputSpec(
                        role="volcano_plot",
                        label="Volcano Plot",
                        artifact_class="figure",
                        accepted_formats=["svg", "png", "pdf"],
                        preferred_format="svg",
                        asset_id="volcano_plot_v1",
                    ),
                ],
                key_findings=["FDR < 0.05 的显著基因 1324 个。"],
                manager_review="结果已接受，可作为后续功能富集分析输入。",
                next_actions=["开始下游分析", "加入报告"],
                linked_modules=["module_de_analysis"],
                linked_runs=["run_004"],
                linked_assets=["deg_table_v1", "volcano_plot_v1"],
                # Showcase the reference-data pipeline (docs/63): a pre-resolved
                # reference (here, a GENCODE annotation used for gene-ID
                # mapping) is exposed to the executor via BLUEPRINT_REFERENCE_PATHS.
                # In a live project this path would be filled by a background
                # download job once the card's source-only reference resolves.
                executor_context=ExecutorContext(
                    references=[
                        ExecutorReference(
                            type="file",
                            path="reference/gencode.v44.annotation.gtf.gz",
                            description="gtf_annotation",
                        ),
                    ],
                    template_metadata={
                        "reference_paths": {
                            "gtf_annotation": "reference/gencode.v44.annotation.gtf.gz",
                        },
                    },
                ),
            ),
            Card(
                card_id="card_enrichment_group",
                card_type="module_group",
                title="功能富集分析",
                status="planned",
                step=2,
                aggregate_status="partially_planned",
                summary="基于差异表达结果进行 GSEA 和 KEGG 分析。",
                why="用于解释差异基因涉及的生物学通路。",
                inputs=[CardAssetRef(label="差异表达结果", asset_id="deg_table_v1")],
                outputs=[
                    CardOutputSpec(
                        role="gsea_result",
                        label="GSEA 结果",
                        artifact_class="table",
                        accepted_formats=["tsv", "csv"],
                        preferred_format="tsv",
                        status="planned",
                    ),
                    CardOutputSpec(
                        role="kegg_result",
                        label="KEGG 结果",
                        artifact_class="table",
                        accepted_formats=["tsv", "csv"],
                        preferred_format="tsv",
                        status="planned",
                    ),
                ],
                manager_review="待执行。",
                next_actions=["开始执行", "修改方案", "取消模块"],
                linked_modules=["module_group_enrichment"],
                linked_assets=["deg_table_v1"],
            ),
            Card(
                card_id="card_immune_module",
                card_type="module",
                title="免疫浸润分析",
                status="proposed",
                step=2,
                summary="客户提出增加免疫浸润分析模块。",
                why="用于解释免疫相关微环境变化。",
                inputs=[CardAssetRef(label="差异表达结果", asset_id="deg_table_v1")],
                outputs=[
                    CardOutputSpec(
                        role="immune_score_table",
                        label="免疫浸润评分",
                        artifact_class="table",
                        accepted_formats=["tsv", "csv"],
                        preferred_format="tsv",
                        asset_id="immune_score_table_v1",
                        status="planned",
                    )
                ],
                manager_review="等待用户确认是否加入蓝图。",
                next_actions=["接受提案", "修改提案", "查看影响"],
                linked_modules=["module_immune_infiltration"],
                linked_assets=["deg_table_v1"],
            ),
        ]
        assets = [
            Asset(
                asset_id="count_matrix_v1",
                asset_type="count_matrix",
                title="RNA-seq count matrix",
                status="valid",
                path="data/counts/count_matrix.tsv",
                summary="原始表达矩阵。",
            ),
            Asset(
                asset_id="sample_metadata_v1",
                asset_type="metadata",
                title="Sample metadata",
                status="valid",
                path="data/metadata/sample_metadata.tsv",
                summary="分组和批次信息。",
            ),
            Asset(
                asset_id="deg_table_v1",
                asset_type="deg_table",
                title="差异表达结果表",
                status="valid",
                created_by_run="run_004",
                path="results/de/run_004/deg_table.tsv",
                summary="Treatment vs Control 差异表达结果。",
                metadata={"num_significant_fdr_0_05": 1324},
                report_selected=True,
            ),
            Asset(
                asset_id="volcano_plot_v1",
                asset_type="figure",
                title="Volcano plot",
                status="valid",
                created_by_run="run_004",
                path="results/de/run_004/volcano_plot.png",
                summary="DEG 火山图。",
                report_selected=True,
            ),
            Asset(
                asset_id="ranked_gene_list_v1",
                asset_type="ranked_gene_list",
                title="Ranked gene list",
                status="valid",
                created_by_run="run_004",
                path="results/de/run_004/ranked_gene_list.tsv",
                summary="GSEA 排序基因列表。",
            ),
        ]
        claims = [
            Claim(
                claim_id="claim_ifn_activation",
                text="Treatment 组显示 interferon signaling 激活趋势。",
                status="valid",
                depends_on_assets=["deg_table_v1"],
                created_by_run="run_004",
                report_selected=True,
            )
        ]
        report_items = [
            ReportItem(
                item_id="report_01",
                section="差异表达分析",
                title="核心差异表达结果",
                summary="差异表达分析已完成，筛选到 1324 个显著基因。",
                linked_asset_ids=["deg_table_v1", "volcano_plot_v1"],
                linked_claim_ids=["claim_ifn_activation"],
            )
        ]
        (store.root / "results" / "de" / "run_004").mkdir(parents=True, exist_ok=True)
        (store.root / "data" / "counts").mkdir(parents=True, exist_ok=True)
        (store.root / "data" / "metadata").mkdir(parents=True, exist_ok=True)
        for path, content in {
            store.root / "results" / "de" / "run_004" / "deg_table.tsv": "gene\tlog2fc\tpadj\nIFIT1\t2.41\t0.001\nCXCL10\t1.95\t0.003\n",
            store.root / "results" / "de" / "run_004" / "ranked_gene_list.tsv": "gene\tscore\nIFIT1\t3.8\nCXCL10\t3.5\n",
            store.root / "results" / "de" / "run_004" / "volcano_plot.png": "placeholder image asset\n",
            store.root / "data" / "counts" / "count_matrix.tsv": "gene\ts1\ts2\nIFIT1\t10\t40\n",
            store.root / "data" / "metadata" / "sample_metadata.tsv": "sample\tgroup\ns1\tControl\ns2\tTreatment\n",
        }.items():
            path.write_text(content, encoding="utf-8")

        store.save_graph(GraphState(modules=modules, assets=assets, claims=claims, runs=[], report_items=report_items, metadata={"schema_version": self.settings.schema_version}))
        store.save_cards(cards)

    def get_project_snapshot(self, project_id: str) -> dict:
        if not (self.project_path(project_id) / "project.json").exists():
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
        store = self.graph_store(project_id)
        project = self._project_state_with_runtime_preferences(store)
        cards = store.load_cards()
        graph = store.load_graph()
        # Lazy bootstrap materialization bindings for legacy projects
        metadata = graph.metadata if isinstance(graph.metadata, dict) else {}
        # No persisted binding map -> derive it in-memory below. There is no
        # "bootstrapped" marker: reads never persist, so legacy bindings are
        # re-derived on every read (cheap, deterministic) until run-accept writes
        # the authoritative map.
        needs_bootstrap = not metadata.get("asset_materializations")
        if needs_bootstrap:
            # Derive legacy bindings in-memory for this response only. A read
            # must not mutate persisted state (idempotency); the binding map is
            # authoritatively persisted at run-accept. Consumers re-derive from
            # Asset.metadata["planned_asset_id"], so an unpersisted read is safe.
            AssetMaterializationService.bootstrap_from_aliases(graph, cards)
        summary = ProjectSummary(
            **project.model_dump(),
            card_counts=self._count_by(cards, "status"),
            result_counts=self._count_by(graph.assets, "status"),
        )
        return {
            "summary": summary,
            "project": project,
            "cards": cards,
            "graph": graph,
            "proposals": store.load_proposals(),
            "git_log": self.git_service(project_id).log(),
            "worker_capabilities": self._worker_capabilities(),
            "python_runtimes": self._python_runtimes(),
            "r_runtimes": self._r_runtimes(),
        }

    def get_project_snapshot_core(self, project_id: str) -> dict:
        """Lightweight snapshot without expensive runtime/worker enumeration."""
        if not (self.project_path(project_id) / "project.json").exists():
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
        store = self.graph_store(project_id)
        project = self._project_state_with_runtime_preferences(store)
        cards = store.load_cards()
        graph = store.load_graph()
        metadata = graph.metadata if isinstance(graph.metadata, dict) else {}
        # No persisted binding map -> derive it in-memory below. There is no
        # "bootstrapped" marker: reads never persist, so legacy bindings are
        # re-derived on every read (cheap, deterministic) until run-accept writes
        # the authoritative map.
        needs_bootstrap = not metadata.get("asset_materializations")
        if needs_bootstrap:
            # Derive legacy bindings in-memory for this response only. A read
            # must not mutate persisted state (idempotency); the binding map is
            # authoritatively persisted at run-accept. Consumers re-derive from
            # Asset.metadata["planned_asset_id"], so an unpersisted read is safe.
            AssetMaterializationService.bootstrap_from_aliases(graph, cards)
        summary = ProjectSummary(
            **project.model_dump(),
            card_counts=self._count_by(cards, "status"),
            result_counts=self._count_by(graph.assets, "status"),
        )
        return {
            "summary": summary,
            "project": project,
            "cards": cards,
            "graph": graph,
            "proposals": store.load_proposals(),
        }

    def get_project_environment(self, project_id: str) -> dict:
        """Runtime/worker environment data — expensive fs enumeration, fetched lazily."""
        if not (self.project_path(project_id) / "project.json").exists():
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
        return {
            "worker_capabilities": self._worker_capabilities(),
            "python_runtimes": self._python_runtimes(),
            "r_runtimes": self._r_runtimes(),
        }

    def get_project_runtime_preferences(self, project_id: str) -> ProjectRuntimePreferences:
        if not (self.project_path(project_id) / "project.json").exists():
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
        store = self.graph_store(project_id)
        project = self._project_state_with_runtime_preferences(store)
        return project.runtime_preferences

    def update_project_runtime_preferences(self, project_id: str, payload: dict) -> ProjectRuntimePreferences:
        if not (self.project_path(project_id) / "project.json").exists():
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
        store = self.graph_store(project_id)
        with self.lock_for(project_id):
            project = self._project_state_with_runtime_preferences(store)
            graph = store.load_graph()
            runtime_preferences = project.runtime_preferences.model_copy(deep=True)
            if "script_preference" in payload and payload["script_preference"] is not None:
                script_preference = str(payload["script_preference"]).strip()
                if script_preference in {"auto", "prefer_python", "prefer_r", "prefer_mixed"}:
                    runtime_preferences.script_preference = script_preference
            if "python_runtime" in payload:
                value = str(payload["python_runtime"]).strip() if payload["python_runtime"] is not None else ""
                runtime_preferences.python_runtime = value or None
            if "r_runtime" in payload:
                value = str(payload["r_runtime"]).strip() if payload["r_runtime"] is not None else ""
                runtime_preferences.r_runtime = value or None
            if "execution_mode" in payload and payload["execution_mode"] is not None:
                mode = str(payload["execution_mode"]).strip()
                if mode in {"guarded", "workspace_write"}:
                    runtime_preferences.execution_mode = mode
            project = project.model_copy(update={"runtime_preferences": runtime_preferences, "updated_at": utc_now()})
            graph.metadata["runtime_preferences"] = runtime_preferences.model_dump()
            graph.metadata["default_conda_env"] = runtime_preferences.python_runtime
            graph.metadata["default_r_env"] = runtime_preferences.r_runtime
            store.save_project_state(project)
            store.save_graph(graph)
        return runtime_preferences

    @staticmethod
    def _count_by(items: list[object], attr: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            key = getattr(item, attr)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _project_state_with_runtime_preferences(self, store: GraphStore) -> ProjectState:
        project = store.load_project_state()
        metadata = store.load_metadata()
        runtime_preferences = metadata.get("runtime_preferences")
        if isinstance(runtime_preferences, dict):
            try:
                project = project.model_copy(
                    update={
                        "runtime_preferences": ProjectRuntimePreferences.model_validate(runtime_preferences),
                    }
                )
            except Exception:
                pass
        return project

    def _worker_capabilities(self) -> list[dict]:
        registry = build_worker_registry()
        result = []
        for name, adapter in registry.items():
            checker = getattr(adapter, "capability_metadata", None)
            if callable(checker):
                result.append(checker(self.settings))
        return result

    @staticmethod
    def _runtime_source(path: Path | str | None) -> str:
        """Classify a runtime path by its provisioning source."""
        if path is None:
            return "system"
        path_obj = Path(path)
        if path_obj.name == "__system__":
            return "system"
        bundled_root = Path.home() / ".local/share/blueprint-re/mamba"
        try:
            path_obj.relative_to(bundled_root)
            return "bundled"
        except ValueError:
            pass
        for base in default_conda_base_candidates():
            try:
                path_obj.relative_to(base)
                return "conda"
            except ValueError:
                continue
        return "system"

    def _python_runtimes(self) -> list[dict]:
        runtimes: list[dict] = [
            {
                "name": "__system__",
                "label": "System Python",
                "path": None,
                "manager": "system",
                "source": "system",
                "exists": True,
            }
        ]
        seen = {"__system__"}
        manager_labels = {
            "miniforge3": "miniforge",
            "miniconda3": "miniconda",
            "anaconda3": "anaconda",
            "conda": "conda",
        }
        for base in default_conda_base_candidates(self.settings.executor_conda_base):
            if not base.exists():
                continue
            manager = manager_labels.get(base.name, base.name)
            base_python = base / "bin" / "python"
            if base_python.exists() and f"{manager}:base" not in seen:
                runtimes.append(
                    {
                        "name": "base",
                        "label": f"{manager}: base",
                        "path": str(base),
                        "manager": manager,
                        "source": self._runtime_source(base),
                        "exists": True,
                    }
                )
                seen.add(f"{manager}:base")
            envs_root = base / "envs"
            if not envs_root.exists():
                continue
            for env_dir in sorted(path for path in envs_root.iterdir() if path.is_dir()):
                python_bin = env_dir / "bin" / "python"
                if not python_bin.exists() or env_dir.name in seen:
                    continue
                runtimes.append(
                    {
                        "name": env_dir.name,
                        "label": f"{env_dir.name} ({manager})",
                        "path": str(env_dir),
                        "manager": manager,
                        "source": self._runtime_source(env_dir),
                        "exists": True,
                    }
                )
                seen.add(env_dir.name)
        return runtimes

    def _r_runtimes(self) -> list[dict]:
        system_rscript = shutil.which("Rscript")
        runtimes: list[dict] = [
            {
                "name": "__system__",
                "label": "System R",
                "path": system_rscript,
                "manager": "system",
                "source": "system",
                "exists": bool(system_rscript),
            }
        ]
        seen = {"__system__"}
        manager_labels = {
            "miniforge3": "miniforge",
            "miniconda3": "miniconda",
            "anaconda3": "anaconda",
            "conda": "conda",
        }
        for base in default_conda_base_candidates(self.settings.executor_conda_base):
            if not base.exists():
                continue
            manager = manager_labels.get(base.name, base.name)
            base_rscript = base / "bin" / "Rscript"
            if base_rscript.exists() and f"{manager}:base" not in seen:
                runtimes.append(
                    {
                        "name": "base",
                        "label": f"{manager}: base R",
                        "path": str(base_rscript),
                        "manager": manager,
                        "source": self._runtime_source(base),
                        "exists": True,
                    }
                )
                seen.add(f"{manager}:base")
            envs_root = base / "envs"
            if not envs_root.exists():
                continue
            for env_dir in sorted(path for path in envs_root.iterdir() if path.is_dir()):
                rscript_bin = env_dir / "bin" / "Rscript"
                if not rscript_bin.exists() or env_dir.name in seen:
                    continue
                runtimes.append(
                    {
                        "name": env_dir.name,
                        "label": f"{env_dir.name} R ({manager})",
                        "path": str(rscript_bin),
                        "manager": manager,
                        "source": self._runtime_source(env_dir),
                        "exists": True,
                    }
                )
                seen.add(env_dir.name)
        return runtimes

    def get_python_runtimes(self) -> list[dict]:
        """Public accessor for diagnostic/export consumers."""
        return self._python_runtimes()

    def get_r_runtimes(self) -> list[dict]:
        """Public accessor for diagnostic/export consumers."""
        return self._r_runtimes()

    @staticmethod
    def _validate_project_id(project_id: str) -> None:
        if not PROJECT_ID_RE.fullmatch(project_id):
            raise HTTPException(
                status_code=422,
                detail="project_id must use lowercase letters, numbers, and hyphens only.",
            )
