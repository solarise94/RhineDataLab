from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.workers.sandbox.base import SandboxRenderer


class ContainerRenderer(SandboxRenderer):
    """Container-aware sandbox renderer for the future Docker/OCI runtime.

    In the current shared-refactor phase this renderer does **not** actually
    spawn a container; it preserves the existing command and writes a
    ``sandbox_plan.json`` that records the intended container parameters.
    This keeps the manager-agent contract intact while the rest of the
    plumbing is being prepared.
    """

    @property
    def mode(self) -> str:
        return "container"

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
        sandbox_plan_path = run_dir / "sandbox_plan.json"
        sandbox_plan = {
            "mode": "container",
            "network": "host",
            "network_isolation": False,
            "project_root": str(project_root),
            "run_dir": str(run_dir),
            "command": command,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        sandbox_plan_path.write_text(json.dumps(sandbox_plan, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        return command, sandbox_plan
