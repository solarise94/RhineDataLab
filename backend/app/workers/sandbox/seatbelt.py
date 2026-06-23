from __future__ import annotations

from pathlib import Path
from typing import Any

from app.workers.sandbox.base import SandboxRenderer


class SeatbeltRenderer(SandboxRenderer):
    """macOS ``seatbelt`` renderer placeholder.

    Seatbelt sandbox profiles are not yet implemented. Selecting this mode
    intentionally raises so that callers surface a clear unsupported-mode
    message instead of silently falling back to unsandboxed execution.
    """

    @property
    def mode(self) -> str:
        return "seatbelt"

    def should_sandbox(self) -> bool:
        return True

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
        raise NotImplementedError(
            "BLUEPRINT_EXECUTOR_SANDBOX_MODE=seatbelt is not implemented. "
            "Use bwrap on Linux or none for unsandboxed development."
        )
