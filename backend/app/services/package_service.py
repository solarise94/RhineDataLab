from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import Settings, get_settings
from app.models.cards import Card, CardAssetRef, CardStatus, CardType
from app.models.executor import ExecutorContext, RuntimeBindings
from app.models.packages import (
    PackageCompatibility,
    PackageImportResult,
    PackageImportStatus,
    PackageIndexEntry,
    PackageInstantiationResult,
    PackageManifest,
    PortableCardPackage,
)
from app.services.library_registry_service import LibraryRegistryService
from app.services.project_service import ProjectService
from app.services.utils import atomic_write_json, read_json, utc_now


# Security scan patterns for bundle files
_RISKY_SCRIPT_PATTERNS = [
    re.compile(r"\beval\s*\(", re.I),
    re.compile(r"\bexec\s*\(", re.I),
    re.compile(r"\bos\.system\s*\(", re.I),
    re.compile(r"\bsubprocess\b", re.I),
    re.compile(r"\bsystem\s*\(", re.I),
    re.compile(r"\bshell\s*=\s*True", re.I),
    re.compile(r"\burlopen\s*\(", re.I),
    re.compile(r"\burllib\.request\b", re.I),
    re.compile(r"\brequests\.get\s*\(", re.I),
]

_RISKY_URL_PATTERNS = [
    re.compile(r"https?://[^\s\"'`]+\.(exe|dll|so|dylib|bin)", re.I),
    re.compile(r"\bcurl\s+.*\|\s*(sh|bash|python)", re.I),
]

_RISKY_PROMPT_PATTERNS = [
    re.compile(r"\bignore\s+(previous\s+)?instructions?\b", re.I),
    re.compile(r"\bignore\s+(your\s+)?(safety\s+)?(guidelines?|rules?)\b", re.I),
    re.compile(r"\bdisregard\s+system\s+(prompt|instruction)\b", re.I),
    re.compile(r"\b(exfiltrate|leak|send)\s+(secret|key|token|password)\b", re.I),
]

_MAX_BUNDLE_FILES = 32
_MAX_BUNDLE_FILE_SIZE_BYTES = 256 * 1024  # 256 KB
_ALLOWED_BUNDLE_EXTENSIONS = {
    ".py",
    ".r",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".sh",
    ".js",
    ".css",
    ".html",
    ".csv",
    ".tsv",
}


class PackageService:
    def __init__(
        self,
        library_registry_service: LibraryRegistryService,
        project_service: ProjectService,
        settings: Settings | None = None,
        runtime_dependency_resolver: Any | None = None,
    ) -> None:
        self.library_registry_service = library_registry_service
        self.project_service = project_service
        self.settings = settings or get_settings()
        self.packages_root = Path(self.settings.data_root) / "_system" / "packages"
        self.runtime_dependency_resolver = runtime_dependency_resolver

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def import_package(
        self,
        source_path: str,
        *,
        overwrite: bool = False,
    ) -> PackageImportResult:
        """Import a portable card package from a local directory or archive."""
        source = Path(source_path)
        if not source.exists():
            return PackageImportResult(
                status="blocked",
                package_id="",
                version="",
                blockers=[f"Source path does not exist: {source_path}"],
            )

        # Load manifest
        manifest_dict = self._load_manifest_dict(source)
        if manifest_dict is None:
            return PackageImportResult(
                status="blocked",
                package_id="",
                version="",
                blockers=[f"manifest.json not found in {source_path}"],
            )

        # Validate manifest
        try:
            manifest = PackageManifest.model_validate(manifest_dict)
        except Exception as exc:
            return PackageImportResult(
                status="blocked",
                package_id=manifest_dict.get("package_id", ""),
                version=manifest_dict.get("version", ""),
                blockers=[f"Invalid manifest: {exc}"],
            )

        # Schema version check (v1 only)
        if manifest.schema_version != "portable_card_package.v1":
            return PackageImportResult(
                status="blocked",
                package_id=manifest.package_id,
                version=manifest.version,
                blockers=[f"Unsupported schema version: {manifest.schema_version}"],
            )

        # Load bundle files
        bundle_files = self._load_bundle_files(source, manifest)

        # Security scan
        scan_warnings = self._security_scan_bundle(bundle_files)

        # Compute content hash
        content_hash = self._compute_content_hash(manifest, bundle_files)

        # Verify declared hash if present
        if manifest.provenance.content_hash and manifest.provenance.content_hash != content_hash:
            return PackageImportResult(
                status="blocked",
                package_id=manifest.package_id,
                version=manifest.version,
                blockers=["Content hash mismatch. Package may be corrupted or tampered with."],
            )

        # Resolve capabilities (skills / MCPs)
        cap_warnings, cap_blockers = self._resolve_capabilities(manifest)

        warnings = scan_warnings + cap_warnings
        blockers = cap_blockers

        if blockers:
            return PackageImportResult(
                status="blocked",
                package_id=manifest.package_id,
                version=manifest.version,
                warnings=warnings,
                blockers=blockers,
            )

        # Check existing
        package_dir = self._package_dir(manifest.package_id, manifest.version)
        if package_dir.exists() and not overwrite:
            return PackageImportResult(
                status="blocked",
                package_id=manifest.package_id,
                version=manifest.version,
                blockers=[f"Package {manifest.package_id}@{manifest.version} already imported. Use overwrite=True to replace."],
            )

        # Store
        package = PortableCardPackage(
            manifest=manifest,
            bundle_files=bundle_files,
        )
        self._store_package(package, content_hash)

        status: PackageImportStatus = "ready_with_warnings" if warnings else "ready"
        return PackageImportResult(
            status=status,
            package_id=manifest.package_id,
            version=manifest.version,
            warnings=warnings,
        )

    def instantiate_package(
        self,
        package_id: str,
        project_id: str,
        *,
        input_bindings: dict[str, str] | None = None,
        parameter_bindings: dict[str, Any] | None = None,
        runtime_override: dict[str, str] | None = None,
        version: str | None = None,
    ) -> PackageInstantiationResult:
        """Instantiate a package into a project card."""
        package = self.get_package(package_id, version)
        if package is None:
            return PackageInstantiationResult(
                card_id="",
                project_id=project_id,
                package_id=package_id,
                version=version or "",
                blockers=[f"Package {package_id} not found."],
            )

        manifest = package.manifest

        # Get project runtime preferences
        try:
            proj_prefs = self.project_service.get_project_runtime_preferences(project_id)
        except Exception as exc:
            return PackageInstantiationResult(
                card_id="",
                project_id=project_id,
                package_id=package_id,
                version=manifest.version,
                blockers=[f"Failed to read project runtime preferences: {exc}"],
            )

        # Resolve runtime
        (
            eff_python,
            eff_r,
            python_source,
            r_source,
        ) = self._resolve_runtime_for_package(manifest, proj_prefs, runtime_override)

        # Resolve declared package dependencies against target runtimes
        dep_result = self._resolve_dependencies(
            manifest, project_id, eff_python, eff_r
        )
        dep_warnings = list(dep_result.get("warnings") or [])

        # If runtime dependencies cannot be satisfied, block instantiation
        if not dep_result.get("ok"):
            return PackageInstantiationResult(
                card_id="",
                project_id=project_id,
                package_id=package_id,
                version=manifest.version,
                effective_python_runtime=eff_python,
                effective_r_runtime=eff_r,
                runtime_source=python_source if eff_python else (r_source if eff_r else "project_default"),
                python_runtime_source=python_source,
                r_runtime_source=r_source,
                warnings=dep_warnings,
                blockers=list(dep_result.get("blockers") or []),
            )

        # Build inputs
        inputs: list[CardAssetRef] = []
        input_bindings = input_bindings or {}
        for inp in manifest.inputs_schema:
            asset_id = input_bindings.get(inp.slot)
            inputs.append(
                CardAssetRef(
                    label=inp.label,
                    asset_id=asset_id,
                    status="bound" if asset_id else "pending",
                )
            )

        # Build executor context
        runtime_bindings = RuntimeBindings(
            conda_env=eff_python,
            r_env=eff_r,
            runtime_source=python_source if eff_python else r_source,  # backward compat
            python_runtime_source=python_source,
            r_runtime_source=r_source,
        )
        executor_context = ExecutorContext(
            skills=list(manifest.executor.skills),
            mcp_servers=list(manifest.executor.mcp_servers),
            script_preference=manifest.executor.script_preference,
            instruction_blocks=list(manifest.executor.instruction_blocks),
            runtime_bindings=runtime_bindings,
        )

        # Create card
        card_id = str(uuid4())
        card = Card(
            card_id=card_id,
            card_type="module",
            title=manifest.title,
            status="proposed",
            summary=manifest.summary,
            why=manifest.description,
            inputs=inputs,
            executor_context=executor_context,
        )

        # Save card to project graph
        try:
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            cards.append(card)
            store.save_cards(cards)
        except Exception as exc:
            return PackageInstantiationResult(
                card_id=card_id,
                project_id=project_id,
                package_id=package_id,
                version=manifest.version,
                blockers=[f"Failed to save card to project: {exc}"],
            )

        # Copy bundle files into project (optional, for reference)
        warnings: list[str] = list(dep_warnings)
        if package.bundle_files:
            try:
                self._copy_bundle_into_project(project_id, card_id, package.bundle_files)
            except Exception as exc:
                warnings.append(f"Bundle files not copied into project: {exc}")

        # Determine unified runtime source for backward compat reporting
        runtime_source = python_source if eff_python else (r_source if eff_r else "project_default")

        return PackageInstantiationResult(
            card_id=card_id,
            project_id=project_id,
            package_id=package_id,
            version=manifest.version,
            effective_python_runtime=eff_python,
            effective_r_runtime=eff_r,
            runtime_source=runtime_source,
            python_runtime_source=python_source,
            r_runtime_source=r_source,
            warnings=warnings,
        )

    def list_packages(self) -> list[PackageIndexEntry]:
        """List all imported packages from the lightweight index."""
        index = self._read_index()
        return [PackageIndexEntry.model_validate(item) for item in index.get("entries", [])]

    def search_packages(
        self,
        query: str = "",
        tags: list[str] | None = None,
        runtime: str | None = None,
        top_k: int = 8,
    ) -> list[dict[str, Any]]:
        """Search packages by query, tags, or runtime."""
        entries = self.list_packages()
        compact_query = self._normalize_text(query)
        query_terms = compact_query.split() if compact_query else []
        tag_filters = {self._normalize_text(t) for t in (tags or []) if self._normalize_text(t)}
        runtime_filter = self._normalize_text(runtime or "")

        scored: list[tuple[float, PackageIndexEntry]] = []
        for entry in entries:
            score = self._score_index_entry(entry, query_terms, tag_filters, runtime_filter)
            if score <= 0:
                continue
            scored.append((score, entry))

        scored.sort(key=lambda item: (-item[0], item[1].title.lower()))

        results: list[dict[str, Any]] = []
        for score, entry in scored[:max(1, min(top_k, 20))]:
            results.append(
                {
                    "package_id": entry.package_id,
                    "version": entry.version,
                    "title": entry.title,
                    "summary": entry.summary,
                    "compatibility": entry.compatibility.model_dump(),
                    "match_reason": self._build_package_match_reason(
                        entry, query_terms, tag_filters, runtime_filter
                    ),
                }
            )
        return results

    def get_package(self, package_id: str, version: str | None = None) -> PortableCardPackage | None:
        """Retrieve a full package by id and optional version (latest if omitted)."""
        if version:
            manifest_path = self._package_dir(package_id, version) / "manifest.json"
            if not manifest_path.exists():
                return None
            manifest_dict = read_json(manifest_path, {})
            manifest = PackageManifest.model_validate(manifest_dict)
            bundle_files = self._load_stored_bundle_files(package_id, version)
            return PortableCardPackage(manifest=manifest, bundle_files=bundle_files)

        # Find latest version
        package_root = self.packages_root / package_id
        if not package_root.exists():
            return None
        versions = sorted(
            [d.name for d in package_root.iterdir() if d.is_dir()],
            key=lambda v: v.split(".")[:3],
            reverse=True,
        )
        if not versions:
            return None
        return self.get_package(package_id, versions[0])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _package_dir(self, package_id: str, version: str) -> Path:
        return self.packages_root / package_id / version

    def _load_manifest_dict(self, source: Path) -> dict[str, Any] | None:
        """Load manifest.json from source directory or archive."""
        if source.is_dir():
            manifest_path = source / "manifest.json"
            if manifest_path.exists():
                return read_json(manifest_path, None)
            return None

        if zipfile.is_zipfile(source):
            with zipfile.ZipFile(source, "r") as zf:
                for name in ("manifest.json",):
                    try:
                        return json.loads(zf.read(name).decode("utf-8"))
                    except (KeyError, json.JSONDecodeError):
                        continue
        return None

    def _load_bundle_files(self, source: Path, manifest: PackageManifest) -> dict[str, str]:
        """Load text bundle files from source directory or zip archive."""
        if source.is_dir():
            return self._load_bundle_files_from_dir(source, manifest)
        if zipfile.is_zipfile(source):
            return self._load_bundle_files_from_zip(source, manifest)
        return {}

    def _load_bundle_files_from_dir(self, source: Path, manifest: PackageManifest) -> dict[str, str]:
        """Load text bundle files from a source directory with path safety checks."""
        files: dict[str, str] = {}
        bundle_dir = source / "bundle"
        if not bundle_dir.exists():
            return files

        for bf in manifest.bundle.files:
            file_path = self._safe_bundle_path(bundle_dir, bf.path)
            if file_path is None:
                continue
            if not file_path.is_file():
                continue
            ext = file_path.suffix.lower()
            if ext not in _ALLOWED_BUNDLE_EXTENSIONS:
                continue
            size = file_path.stat().st_size
            if size > _MAX_BUNDLE_FILE_SIZE_BYTES:
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
                files[bf.path] = content
            except (OSError, UnicodeDecodeError):
                continue
        return files

    def _load_bundle_files_from_zip(self, source: Path, manifest: PackageManifest) -> dict[str, str]:
        """Load text bundle files from a zip archive with path safety checks."""
        files: dict[str, str] = {}
        with zipfile.ZipFile(source, "r") as zf:
            for bf in manifest.bundle.files:
                zip_path = f"bundle/{bf.path}"
                safe = self._safe_bundle_path(Path("/"), bf.path)
                if safe is None:
                    continue
                try:
                    info = zf.getinfo(zip_path)
                except KeyError:
                    continue
                ext = Path(bf.path).suffix.lower()
                if ext not in _ALLOWED_BUNDLE_EXTENSIONS:
                    continue
                if info.file_size > _MAX_BUNDLE_FILE_SIZE_BYTES:
                    continue
                try:
                    content = zf.read(zip_path).decode("utf-8")
                    files[bf.path] = content
                except (UnicodeDecodeError, OSError):
                    continue
        return files

    def _load_stored_bundle_files(self, package_id: str, version: str) -> dict[str, str]:
        """Load bundle files from stored location."""
        files: dict[str, str] = {}
        bundle_dir = self._package_dir(package_id, version) / "bundle"
        if not bundle_dir.exists():
            return files
        for file_path in bundle_dir.rglob("*"):
            if not file_path.is_file():
                continue
            # Defense-in-depth: verify resolved path stays within bundle_dir
            try:
                file_path.resolve().relative_to(bundle_dir.resolve())
            except ValueError:
                continue
            ext = file_path.suffix.lower()
            if ext not in _ALLOWED_BUNDLE_EXTENSIONS:
                continue
            try:
                rel_path = str(file_path.relative_to(bundle_dir))
                files[rel_path] = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        return files

    def _security_scan_bundle(self, bundle_files: dict[str, str]) -> list[str]:
        """Lightweight heuristic scan of bundle file contents."""
        warnings: list[str] = []
        for path, content in bundle_files.items():
            # Script risk
            for pattern in _RISKY_SCRIPT_PATTERNS:
                if pattern.search(content):
                    warnings.append(f"[{path}] Potential script risk detected.")
                    break
            # URL risk
            for pattern in _RISKY_URL_PATTERNS:
                if pattern.search(content):
                    warnings.append(f"[{path}] Suspicious URL or remote execution pattern detected.")
                    break
            # Prompt injection risk
            for pattern in _RISKY_PROMPT_PATTERNS:
                if pattern.search(content):
                    warnings.append(f"[{path}] Potential prompt injection pattern detected.")
                    break
        return warnings

    def _resolve_capabilities(self, manifest: PackageManifest) -> tuple[list[str], list[str]]:
        """Check whether required skills and MCPs exist in the local registry."""
        warnings: list[str] = []
        blockers: list[str] = []

        try:
            skill_registry = self.library_registry_service._ensure_registry("skill")
            skill_ids = {item.id for item in skill_registry.items}
        except Exception:
            skill_ids = set()

        try:
            mcp_registry = self.library_registry_service._ensure_registry("mcp")
            mcp_ids = {item.id for item in mcp_registry.items}
        except Exception:
            mcp_ids = set()

        for skill_id in manifest.compatibility.required_skills:
            if skill_id not in skill_ids:
                blockers.append(f"Required skill not found: {skill_id}")

        for skill_id in manifest.compatibility.optional_skills:
            if skill_id not in skill_ids:
                warnings.append(f"Optional skill not found: {skill_id}")

        for mcp_id in manifest.compatibility.required_mcps:
            if mcp_id not in mcp_ids:
                blockers.append(f"Required MCP not found: {mcp_id}")

        for mcp_id in manifest.compatibility.optional_mcps:
            if mcp_id not in mcp_ids:
                warnings.append(f"Optional MCP not found: {mcp_id}")

        return warnings, blockers

    def _compute_content_hash(self, manifest: PackageManifest, bundle_files: dict[str, str]) -> str:
        """Compute a SHA-256 hash over manifest + sorted bundle files."""
        hasher = hashlib.sha256()
        hasher.update(manifest.model_dump_json().encode("utf-8"))
        for path in sorted(bundle_files):
            hasher.update(path.encode("utf-8"))
            hasher.update(bundle_files[path].encode("utf-8"))
        return f"sha256:{hasher.hexdigest()}"

    def _store_package(self, package: PortableCardPackage, content_hash: str) -> None:
        """Persist package manifest and bundle files to disk."""
        manifest = package.manifest
        package_dir = self._package_dir(manifest.package_id, manifest.version)
        package_dir.mkdir(parents=True, exist_ok=True)

        # Write manifest (update content_hash)
        manifest_dict = manifest.model_dump()
        manifest_dict["provenance"]["content_hash"] = content_hash
        atomic_write_json(package_dir / "manifest.json", manifest_dict)

        # Write bundle files
        bundle_dir = package_dir / "bundle"
        if package.bundle_files:
            bundle_dir.mkdir(parents=True, exist_ok=True)
            for rel_path, content in package.bundle_files.items():
                file_path = self._safe_bundle_path(bundle_dir, rel_path)
                if file_path is None:
                    continue
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")

        # Update index
        self._update_index(
            PackageIndexEntry(
                package_id=manifest.package_id,
                version=manifest.version,
                title=manifest.title,
                summary=manifest.summary,
                tags=list(manifest.tags),
                compatibility=manifest.compatibility,
            )
        )

    def _read_index(self) -> dict[str, Any]:
        index_path = self.packages_root / "index.json"
        return read_json(index_path, {"entries": []})

    def _write_index(self, index: dict[str, Any]) -> None:
        index_path = self.packages_root / "index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(index_path, index)

    def _update_index(self, entry: PackageIndexEntry) -> None:
        index = self._read_index()
        entries: list[dict[str, Any]] = index.get("entries", [])
        # Remove existing entry for same package_id + version
        entries = [
            item
            for item in entries
            if not (item.get("package_id") == entry.package_id and item.get("version") == entry.version)
        ]
        entries.append(entry.model_dump())
        index["entries"] = entries
        index["updated_at"] = utc_now()
        self._write_index(index)

    def _resolve_runtime_for_package(
        self,
        manifest: PackageManifest,
        proj_prefs: Any,
        runtime_override: dict[str, str] | None,
    ) -> tuple[str | None, str | None, str, str]:
        """Resolve effective runtime for a package instance.

        Returns (eff_python, eff_r, python_source, r_source).
        Priority: runtime_override > package_requirement > project_default.
        """
        pkg_python = manifest.executor.runtime_requirements.python_runtime
        pkg_r = manifest.executor.runtime_requirements.r_runtime
        proj_python = getattr(proj_prefs, "python_runtime", None)
        proj_r = getattr(proj_prefs, "r_runtime", None)

        eff_python = proj_python
        eff_r = proj_r
        python_source = "project_default"
        r_source = "project_default"

        # Python runtime resolution from package requirements
        if pkg_python == "__system__":
            eff_python = None
            python_source = "__system__"
        elif pkg_python:
            if proj_python != pkg_python:
                # Project default doesn't match → card override from package requirement
                eff_python = pkg_python
                python_source = "package_requirement"

        # R runtime resolution from package requirements
        if pkg_r == "__system__":
            eff_r = None
            r_source = "__system__"
        elif pkg_r:
            if proj_r != pkg_r:
                eff_r = pkg_r
                r_source = "package_requirement"

        # Apply explicit runtime override (highest priority)
        if runtime_override:
            override_python = runtime_override.get("python_runtime")
            if override_python is not None:
                if override_python == "__system__" or override_python == "":
                    eff_python = None
                    python_source = "__system__"
                else:
                    eff_python = override_python
                    python_source = "card_override"
            override_r = runtime_override.get("r_runtime")
            if override_r is not None:
                if override_r == "__system__" or override_r == "":
                    eff_r = None
                    r_source = "__system__"
                else:
                    eff_r = override_r
                    r_source = "card_override"

        return eff_python, eff_r, python_source, r_source

    def _resolve_dependencies(
        self,
        manifest: PackageManifest,
        project_id: str,
        eff_python: str | None,
        eff_r: str | None,
    ) -> dict:
        """Check whether resolved runtimes satisfy package dependencies.

        Uses RuntimeDependencyResolverService if available.
        Returns {"ok": True} or {"ok": False, "blockers": [...], "warnings": [...]}.
        """
        deps = getattr(manifest, "dependencies", None) or []
        if not deps or self.runtime_dependency_resolver is None:
            return {"ok": True}

        blockers: list[str] = []
        warnings: list[str] = []

        for dep in deps:
            family = str(getattr(dep, "family", "python") or "python").strip()
            packages = list(getattr(dep, "packages", None) or [])
            if not packages:
                continue
            target_runtime = eff_python if family == "python" else eff_r
            try:
                plan = self.runtime_dependency_resolver.resolve(
                    project_id=project_id,
                    payload={
                        "ecosystem": family,
                        "runtime": target_runtime or "__system__",
                        "packages": packages,
                    },
                )
                if not plan.ok:
                    blockers.append(
                        f"{family}: {plan.message or 'dependency resolution failed'} "
                        f"(packages: {', '.join(packages)})"
                    )
                elif plan.status in ("fully_installable", "resolution_unknown"):
                    # resolution_unknown: resolver couldn't determine — warn but don't block
                    if plan.status == "resolution_unknown":
                        warnings.append(
                            f"{family}: dependency status uncertain for {', '.join(packages)}"
                        )
                elif plan.blocked:
                    for blk in plan.blocked:
                        blockers.append(
                            f"{family}: {getattr(blk, 'package', '?')} — "
                            f"{getattr(blk, 'reason', 'blocked')}"
                        )
                else:
                    # Other non-terminal statuses → warn, don't block
                    warnings.append(
                        f"{family}: {plan.status} for packages {', '.join(packages)}"
                    )
            except Exception as exc:
                # Resolver errors should not block instantiation — warn and continue
                warnings.append(f"{family}: dependency check failed ({exc})")

        return {
            "ok": len(blockers) == 0,
            "blockers": blockers,
            "warnings": warnings,
        }

    def _copy_bundle_into_project(
        self,
        project_id: str,
        card_id: str,
        bundle_files: dict[str, str],
    ) -> None:
        """Copy package bundle files into the project for reference."""
        project_root = self.project_service.project_path(project_id)
        dest_dir = project_root / "scripts" / "curated" / "packages" / card_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in bundle_files.items():
            file_path = self._safe_bundle_path(dest_dir, rel_path)
            if file_path is None:
                continue
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9_+\-/.]{2,}", value.lower(), re.I))

    @staticmethod
    def _safe_bundle_path(base_dir: Path, relative: str, *, must_exist: bool = False) -> Path | None:
        """Resolve a bundle-relative path and verify it stays within base_dir.

        Returns the resolved Path, or None if the path is unsafe.
        """
        if not relative:
            return None
        # Reject absolute paths and parent traversal in raw form
        if relative.startswith("/") or relative.startswith("\\"):
            return None
        if ".." in Path(relative).parts:
            return None
        resolved = (base_dir / relative).resolve()
        base_resolved = base_dir.resolve()
        try:
            resolved.relative_to(base_resolved)
        except ValueError:
            return None
        if must_exist and not resolved.exists():
            return None
        return resolved

    def _score_index_entry(
        self,
        entry: PackageIndexEntry,
        query_terms: list[str],
        tag_filters: set[str],
        runtime_filter: str,
    ) -> float:
        score = 0.0
        haystack = " ".join(
            [
                entry.title.lower(),
                entry.summary.lower(),
                " ".join(entry.tags).lower(),
            ]
        )
        if query_terms:
            matched = sum(1 for term in query_terms if term in haystack)
            score += matched * 1.4
            if matched == 0:
                return 0.0
        if runtime_filter:
            runtimes = {
                self._normalize_text(r)
                for r in entry.compatibility.supported_runtimes
                if self._normalize_text(r)
            }
            if runtime_filter in runtimes:
                score += 1.0
            elif runtimes:
                return 0.0
        if tag_filters:
            entry_tags = {self._normalize_text(t) for t in entry.tags if self._normalize_text(t)}
            overlap = len(tag_filters & entry_tags)
            score += overlap * 0.8
            if overlap == 0:
                return 0.0
        return max(score, 0.1)

    @staticmethod
    def _build_package_match_reason(
        entry: PackageIndexEntry,
        query_terms: list[str],
        tag_filters: set[str],
        runtime_filter: str,
    ) -> str:
        norm = PackageService._normalize_text
        parts: list[str] = []
        norm_title = norm(entry.title)
        name_hits = [t for t in query_terms if t in norm_title]
        if name_hits:
            parts.append(f"title: {', '.join(name_hits)}")
        norm_summary = norm(entry.summary)
        summary_hits = [t for t in query_terms if t in norm_summary]
        if summary_hits:
            parts.append(f"summary: {', '.join(summary_hits)}")
        entry_tag_norms = {norm(t) for t in entry.tags}
        tag_hits = tag_filters & entry_tag_norms
        if tag_hits:
            parts.append(f"tags: {', '.join(tag_hits)}")
        if runtime_filter:
            runtime_norms = {norm(r) for r in entry.compatibility.supported_runtimes if norm(r)}
            if runtime_filter in runtime_norms:
                parts.append(f"runtime: {runtime_filter}")
        return "; ".join(parts) if parts else "broad match"
