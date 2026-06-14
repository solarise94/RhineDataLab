from __future__ import annotations

import fcntl
import json
import math
import re
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib import error, request as url_request

from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.models.library import LibraryEntry, LibraryKind, LibraryRegistry
from app.services.app_config_service import AppConfigService
from app.services.project_service import ProjectService
from app.services.utils import atomic_write_json, read_json, sha256_file, utc_now


WORD_RE = re.compile(r"[a-z0-9_+\-/.]{2,}", re.I)


class LibrarySummaryPayload(BaseModel):
    summary_short: str
    summary_long: str = ""
    tags: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)


class LibraryRegistryService:
    def __init__(
        self,
        project_service: ProjectService,
        app_config_service: AppConfigService,
        settings: Settings | None = None,
        *,
        skill_roots: list[Path] | None = None,
        mcp_roots: list[Path] | None = None,
    ) -> None:
        self.project_service = project_service
        self.app_config_service = app_config_service
        self.settings = settings or get_settings()
        self.skill_roots = skill_roots
        self.mcp_roots = mcp_roots
        self.registry_root = Path(self.settings.data_root) / "_system" / "library"

    def list_entries(self, kind: LibraryKind, *, minimal: bool = False) -> dict[str, Any]:
        registry = self._ensure_registry(kind)
        items = [self._serialize_minimal_entry(item) if minimal else self._serialize_entry(item) for item in registry.items]
        return {
            "kind": kind,
            "items": items,
            "summary": f"{len(items)} {kind} entries available.",
            "updated_at": registry.updated_at,
        }

    def search_entries(
        self,
        kind: LibraryKind,
        *,
        query: str,
        runtime: str | None = None,
        tags: list[str] | None = None,
        top_k: int = 8,
        minimal: bool = False,
    ) -> dict[str, Any]:
        registry = self._ensure_registry(kind)
        compact_query = self._normalize_text(query)
        query_terms = compact_query.split() if compact_query else []
        tag_filters = {self._normalize_text(item) for item in (tags or []) if self._normalize_text(item)}
        runtime_filter = self._normalize_text(runtime or "")
        scored: list[tuple[float, LibraryEntry]] = []
        match_reasons: dict[str, str] = {}
        for entry in registry.items:
            score = self._score_entry(entry, compact_query, runtime_filter, tag_filters)
            if score <= 0:
                continue
            scored.append((score, entry))
            match_reasons[entry.id] = self._build_match_reason(
                entry, query_terms, runtime_filter, tag_filters
            )
        scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
        items = [
            self._serialize_minimal_entry(entry)
            if minimal
            else self._serialize_search_entry(
                entry, match_reason=match_reasons.get(entry.id, "broad match")
            )
            for _score, entry in scored[: max(1, min(top_k, 20))]
        ]
        return {
            "kind": kind,
            "query": query,
            "runtime": runtime,
            "tags": tags or [],
            "items": items,
            "summary": f"{len(items)} {kind} matches.",
            "updated_at": registry.updated_at,
        }

    def get_entry(self, kind: LibraryKind, entry_id: str) -> dict[str, Any]:
        registry = self._ensure_registry(kind)
        item = next((entry for entry in registry.items if entry.id == entry_id), None)
        if item is None:
            raise ValueError(f"{kind} library item not found: {entry_id}")
        return {
            "kind": kind,
            "item": self._serialize_detail_entry(item),
            "updated_at": registry.updated_at,
        }

    def refresh_entries(self, kind: LibraryKind, *, force: bool = False) -> dict[str, Any]:
        # Hold the lock for the whole scan+write so a concurrent install/register
        # cannot write a new entry between our scan and our write.
        with self._registry_lock(kind):
            if kind == "skill":
                items = self._build_skill_entries(force=force)
            else:
                items = self._build_mcp_entries(force=force)
            registry = LibraryRegistry(kind=kind, items=items, updated_at=utc_now())
            self._write_registry(registry)
        return {
            "kind": kind,
            "refreshed": len(items),
            "updated_at": registry.updated_at,
            "items": [self._serialize_entry(item) for item in items],
        }

    def resummarize_entry(self, kind: LibraryKind, entry_id: str) -> dict[str, Any]:
        # Ensure the registry exists before taking the lock, preserving the
        # rebuild-on-missing/corrupt behavior of the original _ensure_registry path.
        self._ensure_registry(kind)

        # Hold the lock for the full read-summarize-write cycle so concurrent
        # installs/registers cannot write new entries between our read and write.
        with self._registry_lock(kind):
            items = self._load_registry_items(kind)
            updated: list[LibraryEntry] = []
            target: LibraryEntry | None = None
            for item in items:
                if item.id != entry_id:
                    updated.append(item)
                    continue
                summary = self._summarize_entry_text(
                    kind,
                    name=item.name,
                    source_text=self._entry_source_text(item),
                    fallback_summary=item.summary_short,
                )
                refreshed = item.model_copy(
                    update={
                        "summary_short": summary.summary_short,
                        "summary_long": summary.summary_long,
                        "tags": summary.tags,
                        "use_cases": summary.use_cases,
                        "generated_by": self.settings.library_summarizer_model,
                        "generated_at": utc_now(),
                    }
                )
                updated.append(refreshed)
                target = refreshed
            if target is None:
                raise ValueError(f"{kind} library item not found: {entry_id}")
            new_registry = LibraryRegistry(kind=kind, items=updated, updated_at=utc_now())
            self._write_registry(new_registry)
        return {
            "kind": kind,
            "item": self._serialize_entry(target),
            "updated_at": new_registry.updated_at,
        }

    def resolve_skill_bindings(self, skill_ids: list[str]) -> list[dict[str, Any]]:
        registry = self._ensure_registry("skill")
        items_by_id = {item.id: item for item in registry.items}
        bindings: list[dict[str, Any]] = []
        for skill_id in skill_ids:
            item = items_by_id.get(skill_id)
            if item is None or not item.enabled:
                continue
            bindings.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "summary_short": item.summary_short,
                    "source_path": item.source_path,
                    "tags": list(item.tags),
                }
            )
        return bindings

    def resolve_mcp_bindings(self, project_id: str, mcp_ids: list[str], runtime: str | None = None) -> list[dict[str, Any]]:
        registry = self._ensure_registry("mcp")
        snapshot = self.project_service.get_project_snapshot(project_id)
        python_runtimes = snapshot.get("python_runtimes") or []
        runtimes_by_name = {
            str(item.get("name")): item
            for item in python_runtimes
            if isinstance(item, dict) and item.get("name")
        }
        bindings: list[dict[str, Any]] = []
        for mcp_id in mcp_ids:
            item = next((entry for entry in registry.items if entry.id == mcp_id), None)
            if item is None or not item.enabled:
                continue
            selected_runtime = self._select_supported_runtime(item, runtime, runtimes_by_name)
            config = self._build_mcp_config(item, selected_runtime, runtimes_by_name)
            bindings.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "summary_short": item.summary_short,
                    "supported_runtimes": list(item.supported_runtimes),
                    "compatibility_notes": list(item.compatibility_notes),
                    "launch_hint": item.launch_hint,
                    "source_path": item.source_path,
                    "runtime": selected_runtime,
                    "config": config,
                }
            )
        return bindings

    def _ensure_registry(self, kind: LibraryKind) -> LibraryRegistry:
        path = self._registry_path(kind)
        payload = read_json(path, {})
        try:
            registry = LibraryRegistry.model_validate(payload)
        except Exception:
            registry = LibraryRegistry(kind=kind, items=[], updated_at=None)
        if registry.kind != kind or not registry.items:
            refresh = self.refresh_entries(kind, force=False)
            registry = LibraryRegistry.model_validate(
                {
                    "kind": kind,
                    "items": refresh["items"],
                    "updated_at": refresh["updated_at"],
                }
            )
        return registry

    def _write_registry(self, registry: LibraryRegistry) -> None:
        atomic_write_json(self._registry_path(registry.kind), registry.model_dump())

    def _registry_path(self, kind: LibraryKind) -> Path:
        return self.registry_root / f"{kind}s.json"

    @contextmanager
    def _registry_lock(self, kind: LibraryKind):
        """Exclusive file lock for the given registry kind (context manager)."""
        lock_path = self.registry_root / f"{kind}s.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _install_lock(self, kind: LibraryKind, entry_id: str):
        """Exclusive file lock for installing/registering a specific capability id."""
        lock_path = self.registry_root / f"{kind}-{entry_id}.install.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _prepare_temp_dest(dest: Path) -> Path:
        """Return a temporary path next to dest for atomic install/replace."""
        import uuid
        # Clean up stale temp/backup siblings left by interrupted installs.
        if dest.parent.exists():
            for sibling in dest.parent.iterdir():
                if sibling.name.startswith(f".{dest.name}.") and (
                    sibling.name.startswith(f".{dest.name}.tmp-") or
                    sibling.name.startswith(f".{dest.name}.bak-")
                ):
                    try:
                        shutil.rmtree(sibling)
                    except OSError:
                        pass
        tmp = dest.parent / f".{dest.name}.tmp-{uuid.uuid4().hex}"
        if tmp.exists():
            shutil.rmtree(tmp)
        return tmp

    @staticmethod
    def _commit_temp_dest(tmp_dest: Path, dest: Path) -> None:
        """Atomically replace dest with tmp_dest.

        Uses a backup directory so that if the final rename fails, the previous
        installation can be restored. Any leftover backup/temp directories are
        cleaned up on the next install.
        """
        import uuid

        if not dest.exists():
            tmp_dest.rename(dest)
            return

        backup = dest.parent / f".{dest.name}.bak-{uuid.uuid4().hex}"
        dest.rename(backup)
        try:
            tmp_dest.rename(dest)
        except Exception:
            try:
                if not dest.exists() and backup.exists():
                    backup.rename(dest)
            except OSError:
                pass
            raise
        finally:
            if backup.exists():
                shutil.rmtree(backup)

    @staticmethod
    def _validate_capability_id(value: str) -> str:
        """Return a trimmed id or raise ValueError if it is unsafe."""
        entry_id = value.strip()
        if not entry_id or entry_id in {".", ".."}:
            raise ValueError("Invalid capability id")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", entry_id):
            raise ValueError("Capability id contains illegal characters")
        if entry_id.startswith(".") or entry_id.endswith("."):
            raise ValueError("Capability id cannot start or end with a dot")
        return entry_id

    @staticmethod
    def _assert_within_root(dest: Path, root: Path) -> None:
        """Raise ValueError if dest resolves outside root."""
        root = root.resolve()
        dest = dest.resolve()
        try:
            dest.relative_to(root)
        except ValueError as exc:
            raise ValueError("Target path escapes capabilities root") from exc

    def _add_or_replace_entry(self, kind: LibraryKind, entry: LibraryEntry) -> None:
        """Insert or update a single entry in the registry without re-scanning disk."""
        with self._registry_lock(kind):
            items = self._load_registry_items(kind)
            by_id = {item.id: item for item in items}
            by_id[entry.id] = entry
            registry = LibraryRegistry(kind=kind, items=list(by_id.values()), updated_at=utc_now())
            self._write_registry(registry)

    def _build_single_skill_entry(self, skill_dir: Path, installed_id: str) -> LibraryEntry:
        """Build a LibraryEntry for a single installed skill directory."""
        skill_md = skill_dir / "SKILL.md"
        source_text = self._read_source_text(skill_md)
        source_hash = sha256_file(skill_md)
        frontmatter = self._parse_frontmatter(source_text)
        display_name = str(frontmatter.get("name") or installed_id)
        fallback_summary = self._heuristic_summary("skill", display_name, source_text)
        tags = self._heuristic_tags(display_name, source_text)
        return LibraryEntry(
            id=installed_id,
            kind="skill",
            name=display_name,
            summary_short=fallback_summary,
            summary_long=fallback_summary,
            tags=self._merged_tags(["skill", skill_dir.parent.name.lstrip(".")], tags),
            use_cases=tags[:4],
            source_path=str(skill_md),
            source_hash=source_hash,
            enabled=True,
            runtime_requirements=[],
            compatibility_notes=[],
            supported_runtimes=[],
            launch_hint=None,
            generated_by=self.settings.library_summarizer_model,
            generated_at=utc_now(),
            metadata={
                "root": str(skill_dir.parent),
                "source": str(skill_md.relative_to(skill_dir.parent)),
            },
        )

    def _build_single_mcp_entry(self, manifest_path: Path) -> LibraryEntry:
        """Build a LibraryEntry from a single MCP manifest/server.json path."""
        entry_id = manifest_path.parent.name
        text = self._read_source_text(manifest_path)
        source_hash = sha256_file(manifest_path)
        display_name = entry_id
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("name"):
                display_name = str(manifest["name"])
        except Exception:
            pass
        fallback_summary = self._heuristic_summary("mcp", display_name, text)
        tags = self._heuristic_tags(display_name, text)
        return LibraryEntry(
            id=entry_id,
            kind="mcp",
            name=display_name,
            summary_short=fallback_summary,
            summary_long=fallback_summary,
            tags=self._merged_tags(["mcp"], tags),
            use_cases=tags[:4],
            source_path=str(manifest_path),
            source_hash=source_hash,
            enabled=True,
            runtime_requirements=[],
            compatibility_notes=[],
            supported_runtimes=[],
            launch_hint="see source manifest",
            generated_by=self.settings.library_summarizer_model,
            generated_at=utc_now(),
            metadata={"root": str(manifest_path.parent)},
        )

    def _build_skill_entries(self, *, force: bool) -> list[LibraryEntry]:
        previous = self._load_registry_items("skill")
        previous_by_id = {item.id: item for item in previous}
        entries: list[LibraryEntry] = []
        seen: set[str] = set()
        for root in self._resolved_skill_roots():
            for skill_md in sorted(root.glob("*/SKILL.md")):
                skill_id = skill_md.parent.name
                if skill_id in seen:
                    continue
                seen.add(skill_id)
                source_hash = sha256_file(skill_md)
                cached = previous_by_id.get(skill_id)
                source_text = self._read_source_text(skill_md)
                summary = self._select_or_generate_summary(
                    "skill",
                    name=skill_id,
                    source_hash=source_hash,
                    source_text=source_text,
                    cached=cached,
                    force=force,
                    fallback_summary=self._heuristic_summary("skill", skill_id, source_text),
                )
                frontmatter = self._parse_frontmatter(source_text)
                entries.append(
                    LibraryEntry(
                        id=skill_id,
                        kind="skill",
                        name=str(frontmatter.get("name") or skill_id),
                        summary_short=summary.summary_short,
                        summary_long=summary.summary_long,
                        tags=self._merged_tags(["skill", root.parent.name.lstrip(".")], summary.tags),
                        use_cases=summary.use_cases,
                        source_path=str(skill_md),
                        source_hash=source_hash,
                        enabled=True,
                        runtime_requirements=[],
                        compatibility_notes=[],
                        supported_runtimes=[],
                        launch_hint=None,
                        generated_by=self.settings.library_summarizer_model,
                        generated_at=utc_now(),
                        metadata={
                            "root": str(root),
                            "source": str(skill_md.relative_to(skill_md.parent.parent)),
                        },
                    )
                )
        entries.sort(key=lambda item: item.name.lower())
        return entries

    def _build_mcp_entries(self, *, force: bool) -> list[LibraryEntry]:
        previous = self._load_registry_items("mcp")
        previous_by_id = {item.id: item for item in previous}
        entries: list[LibraryEntry] = []
        seen: set[str] = set()
        candidates = self._scan_mcp_sources()
        if not candidates:
            candidates = [self._default_omicverse_candidate()]
        for candidate in candidates:
            entry_id = candidate["id"]
            if entry_id in seen:
                continue
            seen.add(entry_id)
            cached = previous_by_id.get(entry_id)
            source_hash = str(candidate["source_hash"])
            source_text = str(candidate["source_text"])
            summary = self._select_or_generate_summary(
                "mcp",
                name=str(candidate["name"]),
                source_hash=source_hash,
                source_text=source_text,
                cached=cached,
                force=force,
                fallback_summary=self._heuristic_summary("mcp", str(candidate["name"]), source_text),
            )
            entries.append(
                LibraryEntry(
                    id=entry_id,
                    kind="mcp",
                    name=str(candidate["name"]),
                    summary_short=summary.summary_short,
                    summary_long=summary.summary_long,
                    tags=self._merged_tags(candidate.get("tags") or ["mcp"], summary.tags),
                    use_cases=summary.use_cases,
                    source_path=candidate.get("source_path"),
                    source_hash=source_hash,
                    enabled=bool(candidate.get("enabled", True)),
                    runtime_requirements=list(candidate.get("runtime_requirements") or []),
                    compatibility_notes=list(candidate.get("compatibility_notes") or []),
                    supported_runtimes=list(candidate.get("supported_runtimes") or []),
                    launch_hint=candidate.get("launch_hint"),
                    generated_by=self.settings.library_summarizer_model,
                    generated_at=utc_now(),
                    metadata=dict(candidate.get("metadata") or {}),
                )
            )
        entries.sort(key=lambda item: item.name.lower())
        return entries

    def _scan_mcp_sources(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        manifest_names = ("server.json", "manifest.json", "mcp.json")
        for root in self._resolved_mcp_roots():
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                if path.name.lower() not in {*manifest_names, "readme.md"}:
                    continue
                entry_id = path.parent.name
                if entry_id in seen:
                    continue
                seen.add(entry_id)
                text = self._read_source_text(path)

                # Prefer a friendly name persisted in the canonical manifest
                display_name = entry_id
                manifest_path: Path | None = None
                for name in manifest_names:
                    candidate = path.parent / name
                    if candidate.exists():
                        manifest_path = candidate
                        break
                if manifest_path is not None:
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        if manifest.get("name"):
                            display_name = str(manifest["name"])
                    except Exception:
                        pass

                candidates.append(
                    {
                        "id": entry_id,
                        "name": display_name,
                        "source_path": str(path),
                        "source_hash": sha256_file(path),
                        "source_text": text,
                        "tags": ["mcp"],
                        "runtime_requirements": [],
                        "supported_runtimes": [],
                        "launch_hint": "see source manifest",
                        "metadata": {"root": str(root)},
                        "enabled": True,
                        "compatibility_notes": [],
                    }
                )
        return candidates

    def _default_omicverse_candidate(self) -> dict[str, Any]:
        source_text = (
            "OmicVerse MCP server for omics-oriented tools and runtime helpers. "
            "Use when a card needs single-cell or omics helper tools through the omicverse runtime."
        )
        source_hash = f"inline:{hash(source_text)}"
        return {
            "id": "omicverse",
            "name": "omicverse",
            "source_path": None,
            "source_hash": source_hash,
            "source_text": source_text,
            "tags": ["mcp", "omics", "runtime"],
            "runtime_requirements": ["omicverse"],
            "supported_runtimes": ["omicverse"],
            "launch_hint": "requires omicverse runtime",
            "metadata": {"source": "runtime_profile"},
            "enabled": True,
            "compatibility_notes": [],
        }

    def _load_registry_items(self, kind: LibraryKind) -> list[LibraryEntry]:
        payload = read_json(self._registry_path(kind), {})
        try:
            registry = LibraryRegistry.model_validate(payload)
        except Exception:
            return []
        return registry.items if registry.kind == kind else []

    def _app_installed_capabilities_root(self, kind: LibraryKind) -> Path:
        return Path(self.settings.data_root) / "_system" / "capabilities" / ("skills" if kind == "skill" else "mcp")

    def _resolved_skill_roots(self) -> list[Path]:
        if self.skill_roots is not None:
            roots = [path for path in self.skill_roots if path.exists()]
        else:
            roots = [path for path in [Path.home() / ".codex" / "skills", Path.home() / ".agents" / "skills"] if path.exists()]
        app_installed = self._app_installed_capabilities_root("skill")
        if app_installed.exists() and app_installed not in roots:
            roots.append(app_installed)
        return roots

    def _resolved_mcp_roots(self) -> list[Path]:
        if self.mcp_roots is not None:
            roots = [path for path in self.mcp_roots if path.exists()]
        else:
            roots = [path for path in [Path.home() / ".codex" / "mcp", Path.home() / ".agents" / "mcp"] if path.exists()]
        app_installed = self._app_installed_capabilities_root("mcp")
        if app_installed.exists() and app_installed not in roots:
            roots.append(app_installed)
        return roots

    @staticmethod
    def _read_source_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _entry_source_text(self, item: LibraryEntry) -> str:
        source_path = Path(item.source_path) if item.source_path else None
        if source_path and source_path.exists():
            return self._read_source_text(source_path)
        return item.summary_long or item.summary_short

    @staticmethod
    def _parse_frontmatter(text: str) -> dict[str, Any]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        metadata: dict[str, Any] = {}
        index = 1
        while index < len(lines) and lines[index].strip() != "---":
            line = lines[index].strip()
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip().strip('"').strip("'")
            index += 1
        return metadata

    def _select_or_generate_summary(
        self,
        kind: LibraryKind,
        *,
        name: str,
        source_hash: str,
        source_text: str,
        cached: LibraryEntry | None,
        force: bool,
        fallback_summary: str,
    ) -> LibrarySummaryPayload:
        if (
            not force
            and cached is not None
            and cached.source_hash == source_hash
            and cached.summary_short
        ):
            return LibrarySummaryPayload(
                summary_short=cached.summary_short,
                summary_long=cached.summary_long,
                tags=list(cached.tags),
                use_cases=list(cached.use_cases),
            )
        return self._summarize_entry_text(kind, name=name, source_text=source_text, fallback_summary=fallback_summary)

    def _summarize_entry_text(
        self,
        kind: LibraryKind,
        *,
        name: str,
        source_text: str,
        fallback_summary: str,
    ) -> LibrarySummaryPayload:
        prompt = (
            "You summarize engineering skills and MCP capabilities for a searchable registry.\n"
            "Return strict JSON with keys: summary_short, summary_long, tags, use_cases.\n"
            "summary_short must be one short Chinese sentence focused on purpose, like "
            "\"用于改善单细胞绘图\" or \"用于单细胞数据分析\".\n"
            "summary_long should be a short Chinese sentence about when to use it.\n"
            "tags and use_cases should be short arrays of lowercase keywords or short Chinese phrases.\n"
            "Do not describe installation steps or internal prompt rules.\n\n"
            f"Kind: {kind}\n"
            f"Name: {name}\n"
            "Source:\n"
            f"{source_text[:12000]}"
        )
        payload = self._call_summarizer(prompt)
        if payload is None:
            tags = self._heuristic_tags(name, source_text)
            return LibrarySummaryPayload(
                summary_short=fallback_summary,
                summary_long=fallback_summary,
                tags=tags[:6],
                use_cases=tags[:4],
            )
        try:
            return LibrarySummaryPayload.model_validate(payload)
        except Exception:
            tags = self._heuristic_tags(name, source_text)
            return LibrarySummaryPayload(
                summary_short=fallback_summary,
                summary_long=fallback_summary,
                tags=tags[:6],
                use_cases=tags[:4],
            )

    def _call_summarizer(self, prompt: str) -> dict[str, Any] | None:
        config = self.app_config_service.get_secret_settings()
        api_key = str(config.get("deepseek_api_key") or "").strip()
        if not api_key:
            return None
        base_url = str(config.get("deepseek_api_base_url") or self.settings.deepseek_api_base_url).rstrip("/")
        model = str(config.get("library_summarizer_model") or self.settings.library_summarizer_model)
        payload = {
            "model": model,
            "max_tokens": 600,
            "temperature": 0.1,
            "system": "Return one JSON object only.",
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        }
        http_request = url_request.Request(
            f"{base_url}/v1/messages",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with url_request.urlopen(http_request, timeout=60) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, json.JSONDecodeError, error.URLError, error.HTTPError):
            return None
        text = "\n".join(
            block.get("text", "")
            for block in response_payload.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if not text:
            return None
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S)
        candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _heuristic_summary(kind: LibraryKind, name: str, text: str) -> str:
        lowered = f"{name} {text}".lower()
        if "single cell" in lowered or "single-cell" in lowered or "单细胞" in lowered:
            return "用于单细胞数据分析"
        if "plot" in lowered or "绘图" in lowered or "visual" in lowered:
            return "用于改善科研绘图与结果展示"
        if "search" in lowered or "web" in lowered or "检索" in lowered:
            return "用于网页检索与信息提取"
        if "omics" in lowered or "rna" in lowered or "转录组" in lowered:
            return "用于组学分析与运行时辅助"
        if kind == "mcp":
            return "用于运行时工具接入与辅助分析"
        return "用于补充执行器的专项能力"

    @staticmethod
    def _heuristic_tags(name: str, text: str) -> list[str]:
        tokens = WORD_RE.findall(f"{name} {text}".lower())
        preferred = [
            token
            for token in tokens
            if token
            and token not in {"the", "and", "for", "with", "from", "this", "that", "used", "using", "skill", "skills"}
        ]
        ordered: list[str] = []
        for token in preferred:
            if token not in ordered:
                ordered.append(token)
        return ordered[:10]

    @staticmethod
    def _merged_tags(base: list[str], generated: list[str]) -> list[str]:
        ordered: list[str] = []
        for item in [*base, *generated]:
            compact = item.strip().lower()
            if compact and compact not in ordered:
                ordered.append(compact)
        return ordered

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(WORD_RE.findall(value.lower()))

    @staticmethod
    def _build_match_reason(
        entry: LibraryEntry,
        query_terms: list[str],
        runtime_filter: str,
        tag_filters: set[str],
    ) -> str:
        """Build a human-readable match reason from query/filter hits."""
        norm = LibraryRegistryService._normalize_text
        parts: list[str] = []
        norm_name = norm(entry.name)
        name_hits = [t for t in query_terms if t in norm_name]
        if name_hits:
            parts.append(f"name: {', '.join(name_hits)}")
        norm_aliases = " ".join(entry.aliases).lower()
        alias_hits = [t for t in query_terms if t in norm_aliases]
        if alias_hits:
            parts.append(f"aliases: {', '.join(alias_hits)}")
        norm_summary = norm(entry.summary_short or "")
        summary_hits = [t for t in query_terms if t in norm_summary]
        if summary_hits:
            parts.append(f"summary: {', '.join(summary_hits)}")
        entry_tag_norms = {norm(t) for t in entry.tags}
        tag_hits = tag_filters & entry_tag_norms
        if tag_hits:
            parts.append(f"tags: {', '.join(tag_hits)}")
        if runtime_filter:
            runtime_norms = {
                norm(r)
                for r in [*entry.supported_runtimes, *entry.runtime_requirements]
                if norm(r)
            }
            if runtime_filter in runtime_norms:
                parts.append(f"runtime: {runtime_filter}")
        return "; ".join(parts) if parts else "broad match"

    @staticmethod
    def _serialize_search_entry(item: LibraryEntry, *, match_reason: str = "broad match") -> dict[str, Any]:
        return {
            "id": item.id,
            "kind": item.kind,
            "name": item.name,
            "summary_short": item.summary_short,
            "match_reason": match_reason,
            "supported_runtimes": list(item.supported_runtimes),
            "enabled": item.enabled,
        }

    @staticmethod
    def _serialize_detail_entry(item: LibraryEntry) -> dict[str, Any]:
        return {
            "id": item.id,
            "kind": item.kind,
            "name": item.name,
            "summary_short": item.summary_short,
            "summary_long": item.summary_long,
            "use_cases": list(item.use_cases),
            "compatibility_notes": list(item.compatibility_notes),
            "supported_runtimes": list(item.supported_runtimes),
            "runtime_requirements": list(item.runtime_requirements),
            "launch_hint": item.launch_hint,
            "enabled": item.enabled,
            "source_kind": LibraryRegistryService._compute_source_kind(item),
        }

    @staticmethod
    def _compute_source_kind(item: LibraryEntry) -> str:
        sp = item.source_path or ""
        if "/_system/capabilities/" in sp:
            return "app-installed"
        if "/.codex/" in sp or "/.agents/" in sp:
            return "system"
        if sp:
            return "system"
        return "system"

    def _score_entry(
        self,
        entry: LibraryEntry,
        compact_query: str,
        runtime_filter: str,
        tag_filters: set[str],
    ) -> float:
        score = 0.1 if entry.enabled else 0.0
        haystack = " ".join(
            [
                entry.name.lower(),
                entry.summary_short.lower(),
                entry.summary_long.lower(),
                " ".join(entry.tags).lower(),
                " ".join(entry.use_cases).lower(),
                " ".join(entry.aliases).lower(),
            ]
        )
        if compact_query:
            query_terms = compact_query.split()
            matched = 0
            for term in query_terms:
                if term in haystack:
                    matched += 1
            # Also check aliases (not in haystack to avoid inflating phrase-match)
            alias_haystack = " ".join(entry.aliases).lower()
            alias_matched = 0
            for term in query_terms:
                if term in alias_haystack:
                    alias_matched += 1
            score += matched * 1.4 + alias_matched * 1.2
            if compact_query in haystack:
                score += 1.8
            if matched == 0 and alias_matched == 0:
                return 0.0
        if runtime_filter:
            runtime_terms = {
                self._normalize_text(item)
                for item in [*entry.runtime_requirements, *entry.supported_runtimes]
                if self._normalize_text(item)
            }
            if runtime_filter in runtime_terms:
                score += 1.0
            elif entry.kind == "mcp" and runtime_terms:
                return 0.0
        if tag_filters:
            entry_tags = {self._normalize_text(item) for item in entry.tags if self._normalize_text(item)}
            overlap = len(tag_filters & entry_tags)
            score += overlap * 0.8
            if overlap == 0:
                return 0.0
        return score

    @staticmethod
    def _serialize_entry(item: LibraryEntry) -> dict[str, Any]:
        payload = item.model_dump()
        payload["summary"] = item.summary_short
        payload["source_kind"] = LibraryRegistryService._compute_source_kind(item)
        # Do not expose raw host path via source field
        payload.pop("source_path", None)
        payload.pop("source_hash", None)
        payload.pop("generated_by", None)
        payload.pop("generated_at", None)
        payload.pop("metadata", None)
        return payload

    @staticmethod
    def _serialize_minimal_entry(item: LibraryEntry) -> dict[str, Any]:
        return {
            "id": item.id,
            "kind": item.kind,
            "name": item.name,
            "enabled": item.enabled,
        }

    @staticmethod
    def _select_supported_runtime(
        item: LibraryEntry,
        requested_runtime: str | None,
        runtimes_by_name: dict[str, dict[str, Any]],
    ) -> str | None:
        if requested_runtime and requested_runtime in item.supported_runtimes:
            return requested_runtime
        for runtime in item.supported_runtimes:
            candidate = runtimes_by_name.get(runtime)
            if candidate and candidate.get("exists"):
                return runtime
        return item.supported_runtimes[0] if item.supported_runtimes else requested_runtime

    def install_skill_from_directory(
        self,
        source_dir: Path,
        *,
        target_id: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Install a skill from a local directory into the app-managed capabilities root."""
        source_dir = source_dir.resolve()
        if not source_dir.exists():
            raise ValueError(f"Source path does not exist: {source_dir}")
        if not source_dir.is_dir():
            raise ValueError("Source must be a directory")
        if not (source_dir / "SKILL.md").exists():
            raise ValueError("Skill source must contain a SKILL.md file")

        installed_id = self._validate_capability_id(target_id or source_dir.name)
        with self._install_lock("skill", installed_id):
            cap_root = self._app_installed_capabilities_root("skill")
            dest = cap_root / installed_id
            self._assert_within_root(dest, cap_root)

            if dest.exists() and not overwrite:
                raise FileExistsError(f"Target already exists: {dest}")

            tmp_dest = self._prepare_temp_dest(dest)
            try:
                shutil.copytree(source_dir, tmp_dest)
                self._commit_temp_dest(tmp_dest, dest)
            except Exception:
                if tmp_dest.exists():
                    shutil.rmtree(tmp_dest)
                raise

            entry = self._build_single_skill_entry(dest, installed_id)
            self._add_or_replace_entry("skill", entry)

        return {
            "ok": True,
            "kind": "skill",
            "installed_id": installed_id,
            "installed_name": entry.name,
            "summary": f"Skill '{entry.name}' installed and available.",
            "warnings": [],
        }

    def register_mcp_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        *,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Register an MCP server by writing a server.json config under the app-managed MCP root."""
        if transport not in {"stdio", "http", "sse"}:
            raise ValueError("transport must be 'stdio', 'http', or 'sse'")
        if transport == "stdio":
            if not command:
                raise ValueError("command is required for stdio transport")
        else:
            if not url:
                raise ValueError("url is required for http/sse transport")

        server_id = self._validate_capability_id(server_id)
        with self._install_lock("mcp", server_id):
            cap_root = self._app_installed_capabilities_root("mcp")
            dest = cap_root / server_id
            self._assert_within_root(dest, cap_root)

            if dest.exists() and not overwrite:
                raise FileExistsError(f"Target already exists: {dest}")

            server_config: dict[str, Any]
            if transport == "stdio":
                server_config = {"command": command}
                if args:
                    server_config["args"] = args
                if env:
                    server_config["env"] = env
            else:
                server_config = {"type": transport, "url": url}
                if headers:
                    server_config["headers"] = headers
            server_config["name"] = name

            tmp_dest = self._prepare_temp_dest(dest)
            try:
                tmp_dest.mkdir(parents=True)
                (tmp_dest / "server.json").write_text(
                    json.dumps(server_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                self._commit_temp_dest(tmp_dest, dest)
            except Exception:
                if tmp_dest.exists():
                    shutil.rmtree(tmp_dest)
                raise

            entry = self._build_single_mcp_entry(dest / "server.json")
            self._add_or_replace_entry("mcp", entry)

        return {
            "ok": True,
            "kind": "mcp",
            "installed_id": server_id,
            "installed_name": entry.name,
            "summary": f"MCP server '{entry.name}' registered and available.",
            "warnings": [],
        }

    def install_mcp_from_directory(
        self,
        source_dir: Path,
        *,
        target_id: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Install an MCP server from a local directory into the app-managed capabilities root."""
        source_dir = source_dir.resolve()
        if not source_dir.exists():
            raise ValueError(f"Source path does not exist: {source_dir}")
        if not source_dir.is_dir():
            raise ValueError("Source must be a directory")

        manifest_order = ["server.json", "manifest.json", "mcp.json"]
        manifest_path: Path | None = None
        for name in manifest_order:
            candidate = source_dir / name
            if candidate.exists():
                manifest_path = candidate
                break
        if manifest_path is None:
            raise ValueError(
                "MCP source must contain at least one of: server.json, manifest.json, mcp.json"
            )

        installed_id = self._validate_capability_id(target_id or source_dir.name)
        with self._install_lock("mcp", installed_id):
            cap_root = self._app_installed_capabilities_root("mcp")
            dest = cap_root / installed_id
            self._assert_within_root(dest, cap_root)

            if dest.exists() and not overwrite:
                raise FileExistsError(f"Target already exists: {dest}")

            tmp_dest = self._prepare_temp_dest(dest)
            try:
                shutil.copytree(source_dir, tmp_dest)
                self._commit_temp_dest(tmp_dest, dest)
            except Exception:
                if tmp_dest.exists():
                    shutil.rmtree(tmp_dest)
                raise

            installed_manifest = next(
                (dest / name for name in manifest_order if (dest / name).exists()),
                dest / manifest_order[0],
            )
            entry = self._build_single_mcp_entry(installed_manifest)
            entry.id = installed_id
            if entry.name == manifest_path.parent.name:
                entry.name = installed_id
            self._add_or_replace_entry("mcp", entry)

        return {
            "ok": True,
            "kind": "mcp",
            "installed_id": installed_id,
            "installed_name": entry.name,
            "summary": f"MCP '{entry.name}' installed and available.",
            "warnings": [],
        }

    @staticmethod
    def _build_mcp_config(
        item: LibraryEntry,
        selected_runtime: str | None,
        runtimes_by_name: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Build MCP config from manifest.json/server.json or fallback to runtime profile."""
        # 1. Try to read server.json / manifest.json / mcp.json from source directory
        manifest_data: dict[str, Any] | None = None
        if item.source_path:
            source_path = Path(item.source_path)
            for manifest_name in ("server.json", "manifest.json", "mcp.json"):
                candidate = source_path.parent / manifest_name
                if candidate.exists():
                    try:
                        manifest_data = json.loads(candidate.read_text(encoding="utf-8"))
                        break
                    except (OSError, json.JSONDecodeError):
                        continue

        # 2. If manifest found, parse generic fields
        if manifest_data:
            # Some manifests nest under "mcpServers"; unwrap if present
            if "mcpServers" in manifest_data and isinstance(manifest_data["mcpServers"], dict):
                # If manifest has nested mcpServers, extract the first server config
                servers = manifest_data["mcpServers"]
                first_key = next(iter(servers), None)
                if first_key:
                    manifest_data = servers[first_key]

            # 2a. Remote transport: url-based config
            server_type = str(manifest_data.get("type") or "").strip().lower()
            url = str(manifest_data.get("url") or "").strip()
            headers = dict(manifest_data.get("headers") or {})
            if server_type in {"http", "sse"} and url:
                server_config: dict[str, Any] = {"url": url}
                if headers:
                    server_config["headers"] = headers
                return {"mcpServers": {item.id: server_config}}

            # 2b. Stdio transport: command-based config
            command = str(manifest_data.get("command") or "").strip()
            args = list(manifest_data.get("args") or [])
            env = dict(manifest_data.get("env") or {})

            if not command:
                # Manifest exists but lacks a valid command; continue to fallback
                manifest_data = None
            else:
                # Resolve Python runtime path when command is a generic Python interpreter
                if command in {"python", "python3", "py"}:
                    runtime = runtimes_by_name.get(selected_runtime or "", {})
                    python_path = runtime.get("path")
                    if python_path:
                        command = python_path

                server_config = {"command": command, "args": args}
                if env:
                    server_config["env"] = env
                return {"mcpServers": {item.id: server_config}}

        # 3. Fallback to omicverse runtime profile special case
        if item.id == "omicverse":
            runtime = runtimes_by_name.get(selected_runtime or "omicverse", {})
            python_path = runtime.get("path")
            if not python_path:
                return None
            return {
                "mcpServers": {
                    item.id: {
                        "command": python_path,
                        "args": ["-m", "omicverse.mcp", "--phase", "P0"],
                    }
                }
            }

        return None
