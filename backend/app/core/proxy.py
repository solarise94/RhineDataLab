"""Egress proxy bootstrap for the Blueprint backend.

Pydantic-Settings reads BLUEPRINT_HTTP_PROXY / BLUEPRINT_HTTPS_PROXY /
BLUEPRINT_NO_PROXY into Settings but does not push them back to os.environ.
Subprocess installers (conda, pip, mamba, R) and stdlib urllib only look at
os.environ, so we re-export non-empty values at startup.
"""

import logging
import os

from app.core.config import Settings

logger = logging.getLogger(__name__)

_PROXY_MAP: dict[str, str] = {
    "http_proxy": "HTTP_PROXY",
    "https_proxy": "HTTPS_PROXY",
    "no_proxy": "NO_PROXY",
}


def configure_os_proxy(settings: Settings) -> None:
    """Copy non-empty proxy fields from Settings into os.environ."""
    for field, env_var in _PROXY_MAP.items():
        value = getattr(settings, field, "")
        if not value:
            continue
        os.environ[env_var] = value
        # Also set the lowercase form many tools accept.
        os.environ[field] = value
        logger.info("Proxy setting exported: %s", env_var)
