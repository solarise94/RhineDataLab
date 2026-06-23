from __future__ import annotations

import os

from app.workers.sandbox.base import SandboxRenderer
from app.workers.sandbox.bwrap import BwrapRenderer, ensure_bwrap_runtime
from app.workers.sandbox.container import ContainerRenderer
from app.workers.sandbox.none import NoneRenderer
from app.workers.sandbox.seatbelt import SeatbeltRenderer


__all__ = [
    "SandboxRenderer",
    "BwrapRenderer",
    "ContainerRenderer",
    "NoneRenderer",
    "SeatbeltRenderer",
    "resolve_renderer",
    "ensure_bwrap_runtime",
]


_VALID_SANDBOX_MODES = {"bwrap", "container", "seatbelt", "none"}
_RESOLVER_CACHE: dict[str, SandboxRenderer] = {}


def resolve_renderer(mode: str) -> SandboxRenderer:
    """Return the renderer for the given sandbox mode.

    All legal modes (including ``"none"``) can be resolved directly; callers
    should use ``renderer.should_sandbox()`` to decide whether isolation is
    active. Unknown modes raise ``RuntimeError``.
    """
    resolved_mode = (mode or "bwrap").lower().strip()
    if resolved_mode in ("", "auto"):
        resolved_mode = "bwrap"
    if resolved_mode not in _VALID_SANDBOX_MODES:
        raise RuntimeError(
            f"Unknown sandbox mode: {resolved_mode!r}. "
            "Supported values are: bwrap, container, seatbelt, none."
        )
    if resolved_mode not in _RESOLVER_CACHE:
        renderer: SandboxRenderer
        if resolved_mode == "bwrap":
            renderer = BwrapRenderer()
        elif resolved_mode == "container":
            renderer = ContainerRenderer()
        elif resolved_mode == "seatbelt":
            renderer = SeatbeltRenderer()
        else:
            renderer = NoneRenderer()
        _RESOLVER_CACHE[resolved_mode] = renderer
    return _RESOLVER_CACHE[resolved_mode]


def settings_sandbox_mode(settings: object) -> str:
    raw = str(getattr(settings, "executor_sandbox_mode", os.environ.get("BLUEPRINT_EXECUTOR_SANDBOX_MODE", "bwrap")))
    mode = (raw or "bwrap").strip().lower()
    if mode not in _VALID_SANDBOX_MODES:
        raise RuntimeError(
            f"Unknown executor sandbox mode: {mode!r}. "
            "Supported values are: bwrap, container, seatbelt, none."
        )
    return mode
