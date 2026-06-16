from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import json
import os
import tempfile


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            # ``default=str`` is a defensive safety net: callers should keep
            # payloads JSON-native, but a stray pathlib.Path or datetime must
            # never prevent durable state (e.g. dependency job records) from
            # being persisted. See the resolved_runtime serialization bug that
            # left successful dependency installs stuck in an un-persistable
            # terminal state.
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_within(root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"Path escapes project root: {relative_path}")
    return candidate


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_slash_command(message: str) -> tuple[bool, str | None, str | None]:
    import re
    normalized = message.strip()
    match = re.match(r"^/auto(?:[ \t]+([^\r\n]*))?$", normalized)
    if not match:
        return False, None, None
    arg = match.group(1)
    if arg:
        arg = arg.strip()
    if not arg:
        return True, "bare", None
    if arg in {"off", "stop"}:
        return True, "stop", arg
    elif arg == "status":
        return True, "status", arg
    elif arg == "once":
        return True, "once", arg
    else:
        return True, "enable", arg
