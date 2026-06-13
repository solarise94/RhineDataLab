"""Shared reference-data registry.

Stores bundled reference-data files (GTF annotations, FASTA references, index
directories, ...) that reusable analysis cards declare as environment-level
dependencies. The registry is content-addressed (``ref_id`` derived from the
file sha256) so identical content is registered once and referenced by many
cards without duplication.

Storage layout mirrors the card library convention::

    {data_root}/_system/reference-data/
        index.json
        {ref_id}/
            meta.json
            data/<stored_filename>

Security contract (the router is mounted globally, so these matter):
- ``register_local`` only accepts source paths that ``resolve(strict=True)``
  inside an approved root (``data_root`` / ``data_directory_roots`` /
  ``project_roots``); symlinks escaping those roots are rejected.
- ``register_upload`` always streams through a registry-controlled temp file,
  so arbitrary host paths never reach the filesystem layer.
- Registration always *copies* into the registry (never moves or symlinks), so
  ``delete`` only ever removes the registry's own copy.
- ``resolve`` / ``download`` re-validate that the resolved path stays under the
  registry root, blocking path traversal.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Literal

from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.services.utils import atomic_write_json, read_json, sha256_file, utc_now

ReferenceDataKind = Literal["gtf", "fasta", "index", "annotation", "table", "other"]

# ref_id is generated (content-addressed) but defend the public methods against
# traversal regardless: only allow this character set in any ref_id we accept.
_REF_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ReferenceDataMeta(BaseModel):
    ref_id: str
    name: str
    kind: ReferenceDataKind = "other"
    sha256: str
    size: int
    original_filename: str
    stored_filename: str
    description: str | None = None
    added_at: str


class ReferenceDataIndexEntry(BaseModel):
    ref_id: str
    name: str
    kind: ReferenceDataKind = "other"
    sha256: str
    size: int
    original_filename: str
    description: str | None = None
    added_at: str


class ReferenceDataIndex(BaseModel):
    schema_version: str = "reference_data_index.v1"
    entries: list[ReferenceDataIndexEntry] = Field(default_factory=list)


class ReferenceDataError(ValueError):
    """Raised for user-facing reference-data failures (bad path, missing ref)."""


def _sanitize_filename(name: str) -> str:
    """Reduce an arbitrary filename to a safe stored filename."""
    base = Path(name).name  # strip any directory component
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return cleaned or "reference_data"


class ReferenceDataService:
    """Manages the system-level reference-data registry."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._root = Path(self.settings.data_root) / "_system" / "reference-data"
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _index_path(self) -> Path:
        return self._root / "index.json"

    def _ref_dir(self, ref_id: str) -> Path:
        return self._root / ref_id

    def _meta_path(self, ref_id: str) -> Path:
        return self._ref_dir(ref_id) / "meta.json"

    def _data_dir(self, ref_id: str) -> Path:
        return self._ref_dir(ref_id) / "data"

    def _ensure_dirs(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    def _read_index(self) -> dict[str, Any]:
        return read_json(
            self._index_path(),
            {"schema_version": "reference_data_index.v1", "entries": []},
        )

    def _write_index(self, index_data: dict[str, Any]) -> None:
        self._ensure_dirs()
        atomic_write_json(self._index_path(), index_data)

    # ------------------------------------------------------------------
    # Approved-root enforcement (local-path registration)
    # ------------------------------------------------------------------

    def _approved_roots(self) -> list[Path]:
        raw: list[str] = []
        raw.append(str(self.settings.data_root))
        if getattr(self.settings, "data_directory_roots", ""):
            raw.extend(str(self.settings.data_directory_roots).split(","))
        if getattr(self.settings, "project_roots", ""):
            raw.extend(str(self.settings.project_roots).split(","))
        roots: list[Path] = []
        for item in raw:
            item = item.strip()
            if not item:
                continue
            try:
                roots.append(Path(item).resolve())
            except OSError:
                continue
        return roots

    def _assert_within_approved(self, source: Path) -> Path:
        """Resolve ``source`` strictly (following symlinks) and require it to
        live inside an approved root. Returns the resolved file path."""
        if not source.exists():
            raise ReferenceDataError(f"Source path does not exist: {source}")
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise ReferenceDataError(f"Source path could not be resolved: {source}") from exc
        if not resolved.is_file():
            raise ReferenceDataError(f"Source path is not a regular file: {source}")
        for root in self._approved_roots():
            if resolved == root or root in resolved.parents:
                return resolved
        raise ReferenceDataError(
            "Source path is outside the approved data roots; copy the file into a "
            "data/project directory first, or upload it directly."
        )

    def _assert_ref_id(self, ref_id: str) -> str:
        if not _REF_ID_RE.match(ref_id or ""):
            raise ReferenceDataError("Invalid reference id.")
        return ref_id

    def _resolve_safe(self, ref_id: str) -> Path:
        """Return the stored file path for ``ref_id``, re-validating it stays
        under the registry root (path-traversal guard)."""
        self._assert_ref_id(ref_id)
        meta = self._load_meta(ref_id)
        candidate = (self._data_dir(ref_id) / meta.stored_filename).resolve()
        root = self._root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ReferenceDataError("Resolved reference path escapes the registry.")
        return candidate

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_local(
        self,
        source_path: Path,
        *,
        name: str,
        kind: ReferenceDataKind = "other",
        description: str | None = None,
    ) -> ReferenceDataMeta:
        """Register a file already on disk, constrained to approved roots."""
        resolved = self._assert_within_approved(Path(source_path))
        return self._ingest(resolved, name=name, kind=kind, description=description)

    def register_upload(
        self,
        file_obj: BinaryIO,
        *,
        filename: str,
        name: str,
        kind: ReferenceDataKind = "other",
        description: str | None = None,
    ) -> ReferenceDataMeta:
        """Register an uploaded file stream. Streams through a registry-owned
        temp file so no arbitrary host path is trusted."""
        self._ensure_dirs()
        tmp_dir = self._root / "_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, prefix="upload_", suffix=".bin")
        stored_filename_hint = _sanitize_filename(filename)
        try:
            with open(fd, "wb") as handle:
                shutil.copyfileobj(file_obj, handle, length=1024 * 1024)
            meta = self._ingest(
                Path(tmp_name),
                name=name,
                kind=kind,
                description=description,
                stored_filename_hint=stored_filename_hint,
            )
        finally:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        return meta

    def _ingest(
        self,
        source: Path,
        *,
        name: str,
        kind: ReferenceDataKind,
        description: str | None,
        stored_filename_hint: str | None = None,
    ) -> ReferenceDataMeta:
        """Hash the source, dedup by sha256, copy into the registry."""
        digest = sha256_file(source)
        size = source.stat().st_size
        ref_id = f"ref_{digest[:16]}"
        original_filename = source.name
        stored_filename = _sanitize_filename(stored_filename_hint or original_filename)

        clean_name = (name or "").strip() or original_filename

        with self._lock:
            self._ensure_dirs()
            index_data = self._read_index()
            entries: list[dict[str, Any]] = list(index_data.get("entries", []))

            # Content-addressed dedup: identical content is registered once.
            for entry in entries:
                if entry.get("sha256") == digest:
                    return ReferenceDataMeta(
                        ref_id=entry["ref_id"],
                        name=entry["name"],
                        kind=entry.get("kind", "other"),
                        sha256=entry["sha256"],
                        size=entry.get("size", size),
                        original_filename=entry.get("original_filename", original_filename),
                        stored_filename=entry.get("stored_filename", stored_filename),
                        description=entry.get("description"),
                        added_at=entry.get("added_at", utc_now()),
                    )

            ref_dir = self._ref_dir(ref_id)
            data_dir = self._data_dir(ref_id)
            data_dir.mkdir(parents=True, exist_ok=True)
            destination = data_dir / stored_filename
            shutil.copy2(source, destination)

            now = utc_now()
            meta = ReferenceDataMeta(
                ref_id=ref_id,
                name=clean_name,
                kind=kind,
                sha256=digest,
                size=size,
                original_filename=original_filename,
                stored_filename=stored_filename,
                description=description,
                added_at=now,
            )
            atomic_write_json(self._meta_path(ref_id), meta.model_dump())

            entries.append(
                ReferenceDataIndexEntry(
                    ref_id=ref_id,
                    name=clean_name,
                    kind=kind,
                    sha256=digest,
                    size=size,
                    original_filename=original_filename,
                    description=description,
                    added_at=now,
                ).model_dump()
            )
            index_data["entries"] = entries
            self._write_index(index_data)
            return meta

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _load_meta(self, ref_id: str) -> ReferenceDataMeta:
        self._assert_ref_id(ref_id)
        meta_path = self._meta_path(ref_id)
        if not meta_path.exists():
            raise ReferenceDataError(f"Reference not found: {ref_id}")
        return ReferenceDataMeta.model_validate(read_json(meta_path, {}))

    def list(self) -> list[dict[str, Any]]:
        index_data = self._read_index()
        entries = list(index_data.get("entries", []))
        entries.sort(key=lambda e: e.get("added_at") or "", reverse=True)
        return entries

    def get(self, ref_id: str) -> dict[str, Any]:
        return self._load_meta(ref_id).model_dump()

    def resolve(self, ref_id: str) -> Path:
        """Return the host path of the stored reference file (path-safe)."""
        return self._resolve_safe(ref_id)

    def download(self, ref_id: str) -> tuple[Path, ReferenceDataMeta]:
        path = self._resolve_safe(ref_id)
        return path, self._load_meta(ref_id)

    def delete(self, ref_id: str) -> dict[str, Any]:
        """Remove a reference from the registry (registry copy only)."""
        self._assert_ref_id(ref_id)
        with self._lock:
            ref_dir = self._ref_dir(ref_id)
            if not ref_dir.exists():
                raise ReferenceDataError(f"Reference not found: {ref_id}")
            resolved_ref_dir = ref_dir.resolve()
            resolved_root = self._root.resolve()
            if resolved_root not in resolved_ref_dir.parents and resolved_ref_dir != resolved_root:
                raise ReferenceDataError(f"Reference directory is outside registry: {ref_id}")
            shutil.rmtree(ref_dir, ignore_errors=True)

            index_data = self._read_index()
            entries = [e for e in index_data.get("entries", []) if e.get("ref_id") != ref_id]
            index_data["entries"] = entries
            self._write_index(index_data)
            return {"ok": True, "ref_id": ref_id}


def iter_reference_paths(service: ReferenceDataService, ref_ids: Iterable[str]) -> list[tuple[str, Path]]:
    """Resolve a set of ref_ids to (ref_id, host_path); skips missing refs.

    Used by ``instantiate`` to turn a card's ``reference_assets`` into concrete
    ``ExecutorReference`` host paths. Missing refs are silently skipped here;
    the caller decides whether a *required* missing ref is a hard error.
    """
    out: list[tuple[str, Path]] = []
    for ref_id in ref_ids:
        try:
            out.append((ref_id, service.resolve(ref_id)))
        except ReferenceDataError:
            continue
    return out
