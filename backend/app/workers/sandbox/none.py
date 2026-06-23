from __future__ import annotations

from pathlib import Path
from typing import Any

from app.workers.sandbox.base import SandboxRenderer


class NoneRenderer(SandboxRenderer):
    """Unsandboxed renderer.

    This renderer performs no isolation. It exists so that ``resolve_renderer``
    can handle all legal sandbox modes uniformly and callers do not need a
    special-case ``mode == "none"`` guard before resolving.
    """

    @property
    def mode(self) -> str:
        return "none"

    def should_sandbox(self) -> bool:
        return False

    def render(
        self,
        *,
        command: list[str],
        packet: Any,
        project_root: Path,
        run_dir: Path,
        environment: dict[str, str],
        adapter_extra_env_keys: set[str],
        settings: object,
    ) -> tuple[list[str], dict[str, Any]]:
        return command, {"mode": "none"}
