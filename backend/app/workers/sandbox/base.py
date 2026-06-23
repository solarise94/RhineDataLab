from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class SandboxRenderer(ABC):
    """Abstract sandbox renderer.

    A renderer is responsible for:
    - Deciding whether its mode counts as sandboxed.
    - Mutating ``environment`` to add sandbox-specific runtime variables.
    - Returning the final argv and a JSON-serializable plan dict.

    Implementations must not raise during construction; all validation and
    runtime probing happens in :meth:`render`.
    """

    @property
    @abstractmethod
    def mode(self) -> str:
        """Sandbox mode identifier (e.g. ``bwrap``, ``container``, ``seatbelt``)."""

    @abstractmethod
    def should_sandbox(self) -> bool:
        """Return ``True`` if this mode provides execution isolation."""

    @abstractmethod
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
        """Render the command for this sandbox mode.

        Returns ``(final_command, sandbox_plan)``. The renderer may mutate
        ``environment`` in-place to add sandbox-specific variables.
        """
