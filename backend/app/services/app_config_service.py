from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from threading import RLock
from typing import Any
from urllib import error, request

from fastapi import HTTPException
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.models.executor_profiles import default_profiles

API_PROVIDER_PROTOCOLS = {"anthropic_compatible", "openai_compatible"}
# Single source of truth for the system's default API provider. Keep this in sync
# with the Node sidecar's DEFAULT_PROVIDER_ID (manager-agent/src/server.js): the two
# runtimes each hold their own literal, so a default-provider change must touch both.
DEFAULT_PROVIDER_ID = "deepseek"
DEFAULT_PROVIDER_BINDINGS = {
    "manager": {"provider_id": DEFAULT_PROVIDER_ID},
    "reviewer": {"provider_id": DEFAULT_PROVIDER_ID},
    "pi_executor": {"provider_id": DEFAULT_PROVIDER_ID},
    "opencode_executor": {"provider_id": DEFAULT_PROVIDER_ID},
    "library_summarizer": {"provider_id": DEFAULT_PROVIDER_ID},
}
ROLE_PROVIDER_PROTOCOLS = {
    # These roles use Anthropic Messages-compatible requests in the backend today.
    "manager": {"anthropic_compatible"},
    "reviewer": {"anthropic_compatible"},
    "pi_executor": {"anthropic_compatible"},
    "opencode_executor": {"anthropic_compatible"},
    "library_summarizer": {"anthropic_compatible"},
}


class AppConfigService:
    """Stores UI-managed app configuration without exposing secrets back to clients."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = Path(self.settings.data_root) / "_app_settings.json"
        self._cache_lock = RLock()
        self._cache_mtime_ns: int | None = None
        self._cache_payload: dict[str, Any] | None = None
        self._apply_runtime_overrides(self._load(), strict=False)

    def get_public_settings(self) -> dict[str, Any]:
        config = self._load()
        deepseek_key = self._effective_deepseek_api_key(config)
        tavily_key = self._effective_tavily_api_key(config)
        anthropic_key = self._effective_anthropic_api_key(config)
        openai_key = self._effective_openai_api_key(config)
        provider_profiles = self._public_api_provider_profiles(config)
        websearch_enabled = self._effective_bool(
            config.get("manager_websearch_enabled"),
            os.environ.get("MANAGER_WEBSEARCH_ENABLED"),
            default=False,
        )
        return {
            "deepseek": {
                "api_key_configured": bool(deepseek_key),
                "api_base_url": str(config.get("deepseek_api_base_url") or self.settings.deepseek_api_base_url),
                "pi_base_url": str(config.get("pi_deepseek_base_url") or self.settings.pi_deepseek_base_url),
                "manager_model": str(config.get("manager_model") or self.settings.manager_model),
                "executor_model": str(config.get("executor_model") or self.settings.executor_model),
                "reviewer_model": str(config.get("reviewer_model") or self.settings.reviewer_model),
                "library_summarizer_model": str(
                    config.get("library_summarizer_model") or self.settings.library_summarizer_model
                ),
            },
            "web_search": {
                "enabled": websearch_enabled,
                "api_key_configured": bool(tavily_key),
                "base_url": str(config.get("tavily_base_url") or os.environ.get("TAVILY_BASE_URL") or "https://api.tavily.com"),
            },
            "anthropic": {
                "api_key_configured": bool(anthropic_key),
                "api_base_url": str(config.get("anthropic_api_base_url") or self.settings.anthropic_api_base_url),
            },
            "openai": {
                "api_key_configured": bool(openai_key),
                "api_base_url": str(config.get("openai_api_base_url") or self.settings.openai_api_base_url),
            },
            "api_provider_profiles": provider_profiles,
            "provider_bindings": self._public_provider_bindings(config, provider_profiles=provider_profiles),
            "default_worker_type": str(
                config.get("default_worker_type") or self.settings.default_worker_type
            ),
            "worker_timeout_seconds": self._effective_worker_timeout_seconds(config),
            "manifest_repair_timeout_seconds": self._effective_manifest_repair_timeout_seconds(config),
            "available_executors": self._available_executors(),
        }

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._load()
        string_fields = {
            "deepseek_api_base_url",
            "pi_deepseek_base_url",
            "manager_model",
            "executor_model",
            "reviewer_model",
            "library_summarizer_model",
            "tavily_base_url",
            "anthropic_api_base_url",
            "openai_api_base_url",
        }
        for field in string_fields:
            if field in payload and payload[field] is not None:
                value = str(payload[field]).strip()
                if value:
                    config[field] = value

        if "manager_websearch_enabled" in payload:
            config["manager_websearch_enabled"] = bool(payload["manager_websearch_enabled"])

        if "default_worker_type" in payload and payload["default_worker_type"] is not None:
            value = str(payload["default_worker_type"]).strip()
            if value in {"pi", "opencode", "codex", "claude_code"}:
                config["default_worker_type"] = value

        if "worker_timeout_seconds" in payload and payload["worker_timeout_seconds"] is not None:
            try:
                value = int(payload["worker_timeout_seconds"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="worker_timeout_seconds must be an integer.") from exc
            if value < 1:
                raise HTTPException(status_code=400, detail="worker_timeout_seconds must be at least 1 second.")
            config["worker_timeout_seconds"] = value

        if "manifest_repair_timeout_seconds" in payload and payload["manifest_repair_timeout_seconds"] is not None:
            try:
                value = int(payload["manifest_repair_timeout_seconds"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="manifest_repair_timeout_seconds must be an integer.") from exc
            if value < 1:
                raise HTTPException(status_code=400, detail="manifest_repair_timeout_seconds must be at least 1 second.")
            config["manifest_repair_timeout_seconds"] = value

        if payload.get("clear_deepseek_api_key"):
            config.pop("deepseek_api_key", None)
            self.settings.deepseek_api_key = None
        elif "deepseek_api_key" in payload and payload["deepseek_api_key"] is not None:
            value = str(payload["deepseek_api_key"]).strip()
            if value:
                config["deepseek_api_key"] = value

        if payload.get("clear_tavily_api_key"):
            config.pop("tavily_api_key", None)
        elif "tavily_api_key" in payload and payload["tavily_api_key"] is not None:
            value = str(payload["tavily_api_key"]).strip()
            if value:
                config["tavily_api_key"] = value

        if payload.get("clear_anthropic_api_key"):
            config.pop("anthropic_api_key", None)
            self.settings.anthropic_api_key = None
        elif "anthropic_api_key" in payload and payload["anthropic_api_key"] is not None:
            value = str(payload["anthropic_api_key"]).strip()
            if value:
                config["anthropic_api_key"] = value

        if payload.get("clear_openai_api_key"):
            config.pop("openai_api_key", None)
            self.settings.openai_api_key = None
        elif "openai_api_key" in payload and payload["openai_api_key"] is not None:
            value = str(payload["openai_api_key"]).strip()
            if value:
                config["openai_api_key"] = value

        if "api_provider_profiles" in payload and payload["api_provider_profiles"] is not None:
            provider_profiles = self._sanitize_api_provider_profiles(payload["api_provider_profiles"])
            if not provider_profiles:
                raise HTTPException(status_code=400, detail="At least one API provider profile must remain configured.")
            config["api_provider_profiles"] = provider_profiles
            self._prune_provider_test_results(config, provider_profiles)

        provider_profiles = self._api_provider_profiles(config)
        merged_provider_bindings = {
            role: dict(self._resolve_role_binding(config, role))
            for role in DEFAULT_PROVIDER_BINDINGS
        }
        if "provider_bindings" in payload and payload["provider_bindings"] is not None:
            merged_provider_bindings.update(self._sanitize_provider_bindings(payload["provider_bindings"]))
        self._validate_provider_bindings(merged_provider_bindings, provider_profiles)
        if merged_provider_bindings:
            config["provider_bindings"] = merged_provider_bindings
        else:
            config.pop("provider_bindings", None)

        api_provider_keys = config.get("api_provider_keys")
        if not isinstance(api_provider_keys, dict):
            api_provider_keys = {}
        for provider_id, value in (payload.get("api_provider_keys") or {}).items():
            clean_id = self._clean_provider_id(provider_id)
            clean_value = str(value or "").strip()
            if clean_id and clean_value:
                api_provider_keys[clean_id] = clean_value
                self._clear_provider_test_result(config, clean_id)
        for provider_id in payload.get("clear_api_provider_keys") or []:
            clean_id = self._clean_provider_id(provider_id)
            if clean_id:
                api_provider_keys.pop(clean_id, None)
                self._clear_provider_test_result(config, clean_id)
                if clean_id == "deepseek":
                    config.pop("deepseek_api_key", None)
                    self.settings.deepseek_api_key = None
                elif clean_id == "openai":
                    config.pop("openai_api_key", None)
                    self.settings.openai_api_key = None
                elif clean_id == "anthropic":
                    config.pop("anthropic_api_key", None)
                    self.settings.anthropic_api_key = None
        if api_provider_keys:
            config["api_provider_keys"] = api_provider_keys
        else:
            config.pop("api_provider_keys", None)

        self._save(config)
        self._apply_runtime_overrides(config, strict=True)
        return self.get_public_settings()

    def manager_agent_config(self, *, include_secrets: bool = False) -> dict[str, Any]:
        config = self._load()
        manager_binding = self._resolve_role_binding(config, "manager")
        manager_provider = self._require_api_provider(config, manager_binding.get("provider_id"), role="manager")
        base_url = str(manager_provider.get("base_url") or config.get("deepseek_api_base_url") or self.settings.deepseek_api_base_url)
        payload = {
            # The Node sidecar resolves providers through pi-ai's provider registry; keep the
            # runtime provider stable and pass custom endpoints through base URLs.
            "provider": os.environ.get("MANAGER_AGENT_PROVIDER") or DEFAULT_PROVIDER_ID,
            # manager_provider came from _require_api_provider, which only returns a
            # sanitized profile (always carrying a non-empty provider_id) or raises.
            # So no "or DEFAULT_PROVIDER_ID" fallback here: a missing id would be an
            # invariant violation we want to surface, not silently relabel as deepseek.
            "selected_provider_id": manager_provider["provider_id"],
            "provider_protocol": manager_provider.get("protocol"),
            "model": str(manager_provider.get("model") or config.get("manager_model") or self.settings.manager_model),
            "deepseek_api_base_url": base_url,
            "pi_deepseek_base_url": self._provider_native_base_url(manager_provider)
            or str(config.get("pi_deepseek_base_url") or self.settings.pi_deepseek_base_url),
            "websearch_enabled": self._effective_bool(
                config.get("manager_websearch_enabled"),
                os.environ.get("MANAGER_WEBSEARCH_ENABLED"),
                default=False,
            ),
            "tavily_base_url": str(config.get("tavily_base_url") or os.environ.get("TAVILY_BASE_URL") or "https://api.tavily.com"),
        }
        if include_secrets:
            payload["api_key"] = self._api_provider_key(config, manager_provider) or self._effective_deepseek_api_key(config)
            payload["tavily_api_key"] = self._effective_tavily_api_key(config)
        return payload

    def get_secret_settings(self) -> dict[str, Any]:
        config = self._load()
        providers = self._secret_api_provider_profiles(config)
        return {
            "deepseek_api_key": self._effective_deepseek_api_key(config),
            "deepseek_api_base_url": str(config.get("deepseek_api_base_url") or self.settings.deepseek_api_base_url),
            "pi_deepseek_base_url": str(config.get("pi_deepseek_base_url") or self.settings.pi_deepseek_base_url),
            "manager_model": str(config.get("manager_model") or self.settings.manager_model),
            "executor_model": str(config.get("executor_model") or self.settings.executor_model),
            "reviewer_model": str(config.get("reviewer_model") or self.settings.reviewer_model),
            "library_summarizer_model": str(
                config.get("library_summarizer_model") or self.settings.library_summarizer_model
            ),
            "manager_websearch_enabled": self._effective_bool(
                config.get("manager_websearch_enabled"),
                os.environ.get("MANAGER_WEBSEARCH_ENABLED"),
                default=False,
            ),
            "tavily_api_key": self._effective_tavily_api_key(config),
            "tavily_base_url": str(config.get("tavily_base_url") or os.environ.get("TAVILY_BASE_URL") or "https://api.tavily.com"),
            "anthropic_api_key": self._effective_anthropic_api_key(config),
            "anthropic_api_base_url": str(config.get("anthropic_api_base_url") or self.settings.anthropic_api_base_url),
            "openai_api_key": self._effective_openai_api_key(config),
            "openai_api_base_url": str(config.get("openai_api_base_url") or self.settings.openai_api_base_url),
            "api_provider_profiles": providers,
            "provider_bindings": self._public_provider_bindings(config, provider_profiles=self._public_api_provider_profiles(config)),
            "worker_timeout_seconds": self._effective_worker_timeout_seconds(config),
            "manifest_repair_timeout_seconds": self._effective_manifest_repair_timeout_seconds(config),
        }

    def _load(self) -> dict[str, Any]:
        with self._cache_lock:
            try:
                mtime_ns = self.path.stat().st_mtime_ns
            except OSError:
                self._cache_mtime_ns = None
                self._cache_payload = {}
                return {}
            if self._cache_payload is not None and self._cache_mtime_ns == mtime_ns:
                return dict(self._cache_payload)
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            self._cache_mtime_ns = mtime_ns
            self._cache_payload = dict(payload)
            return dict(payload)

    def _save(self, payload: dict[str, Any]) -> None:
        with self._cache_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            try:
                self._cache_mtime_ns = self.path.stat().st_mtime_ns
            except OSError:
                self._cache_mtime_ns = None
            self._cache_payload = dict(payload)

    def _apply_runtime_overrides(self, config: dict[str, Any], *, strict: bool) -> None:
        if config.get("default_worker_type"):
            self.settings.default_worker_type = str(config["default_worker_type"])
        if config.get("worker_timeout_seconds") is not None:
            self.settings.worker_timeout_seconds = self._effective_worker_timeout_seconds(config)
        if config.get("manifest_repair_timeout_seconds") is not None:
            self.settings.manifest_repair_timeout_seconds = self._effective_manifest_repair_timeout_seconds(config)
        if config.get("deepseek_api_base_url"):
            self.settings.deepseek_api_base_url = str(config["deepseek_api_base_url"])
        if config.get("pi_deepseek_base_url"):
            self.settings.pi_deepseek_base_url = str(config["pi_deepseek_base_url"])
        if config.get("manager_model"):
            self.settings.manager_model = str(config["manager_model"])
        if config.get("executor_model"):
            self.settings.executor_model = str(config["executor_model"])
        if config.get("reviewer_model"):
            self.settings.reviewer_model = str(config["reviewer_model"])
        if config.get("library_summarizer_model"):
            self.settings.library_summarizer_model = str(config["library_summarizer_model"])
        if config.get("anthropic_api_base_url"):
            self.settings.anthropic_api_base_url = str(config["anthropic_api_base_url"])
        if config.get("openai_api_base_url"):
            self.settings.openai_api_base_url = str(config["openai_api_base_url"])
        deepseek_key = self._effective_deepseek_api_key(config)
        self.settings.deepseek_api_key = SecretStr(deepseek_key) if deepseek_key else None
        anthropic_key = self._effective_anthropic_api_key(config)
        self.settings.anthropic_api_key = SecretStr(anthropic_key) if anthropic_key else None
        openai_key = self._effective_openai_api_key(config)
        self.settings.openai_api_key = SecretStr(openai_key) if openai_key else None
        try:
            self._apply_provider_binding_overrides(config)
        except HTTPException:
            if strict:
                raise

    def _effective_worker_timeout_seconds(self, config: dict[str, Any]) -> int:
        value = config.get("worker_timeout_seconds")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return int(self.settings.worker_timeout_seconds)
        return max(1, parsed)

    def _effective_manifest_repair_timeout_seconds(self, config: dict[str, Any]) -> int:
        value = config.get("manifest_repair_timeout_seconds")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return int(getattr(self.settings, "manifest_repair_timeout_seconds", 180))
        return max(1, parsed)

    def _apply_provider_binding_overrides(self, config: dict[str, Any]) -> None:
        self.settings.manager_api_key = None
        self.settings.manager_api_base_url = None
        self.settings.reviewer_api_key = None
        self.settings.reviewer_api_base_url = None
        self.settings.pi_api_key = None
        self.settings.pi_anthropic_base_url = None
        self.settings.opencode_api_key = None
        self.settings.opencode_api_base_url = None
        self.settings.opencode_api_protocol = None

        manager_binding = self._resolve_role_binding(config, "manager")
        manager_provider = self._require_api_provider(config, manager_binding.get("provider_id"), role="manager")
        if manager_provider.get("protocol") == "anthropic_compatible":
            manager_key = self._api_provider_key(config, manager_provider)
            self.settings.manager_api_base_url = str(manager_provider.get("base_url") or self.settings.deepseek_api_base_url)
            self.settings.manager_api_key = SecretStr(manager_key) if manager_key else None
            if manager_provider.get("provider_id") == "deepseek":
                self.settings.deepseek_api_base_url = self.settings.manager_api_base_url
                self.settings.deepseek_api_key = SecretStr(manager_key) if manager_key else self.settings.deepseek_api_key
        if manager_provider.get("model"):
            self.settings.manager_model = str(manager_provider["model"])

        reviewer_binding = self._resolve_role_binding(config, "reviewer")
        reviewer_provider = self._require_api_provider(config, reviewer_binding.get("provider_id"), role="reviewer")
        if reviewer_provider.get("protocol") == "anthropic_compatible":
            reviewer_key = self._api_provider_key(config, reviewer_provider)
            self.settings.reviewer_api_base_url = str(reviewer_provider.get("base_url") or self.settings.deepseek_api_base_url)
            self.settings.reviewer_api_key = SecretStr(reviewer_key) if reviewer_key else None
        if reviewer_provider.get("model"):
            self.settings.reviewer_model = str(reviewer_provider["model"])

        pi_binding = self._resolve_role_binding(config, "pi_executor")
        pi_provider = self._require_api_provider(config, pi_binding.get("provider_id"), role="pi_executor")
        if pi_provider.get("protocol") == "anthropic_compatible":
            pi_key = self._api_provider_key(config, pi_provider)
            self.settings.pi_anthropic_base_url = str(pi_provider.get("base_url") or self.settings.deepseek_api_base_url)
            self.settings.pi_api_key = SecretStr(pi_key) if pi_key else None
            native_base_url = self._provider_native_base_url(pi_provider) or str(self.settings.pi_deepseek_base_url)
            if native_base_url:
                self.settings.pi_deepseek_base_url = native_base_url
        if pi_provider.get("model"):
            self.settings.pi_executor_model = str(pi_provider["model"])

        opencode_binding = self._resolve_role_binding(config, "opencode_executor")
        opencode_provider = self._require_api_provider(config, opencode_binding.get("provider_id"), role="opencode_executor")
        if opencode_provider.get("protocol") == "openai_compatible":
            openai_key = self._api_provider_key(config, opencode_provider)
            self.settings.openai_api_base_url = str(opencode_provider.get("base_url") or self.settings.openai_api_base_url)
            self.settings.openai_api_key = SecretStr(openai_key) if openai_key else None
            self.settings.opencode_api_base_url = self.settings.openai_api_base_url
            self.settings.opencode_api_key = SecretStr(openai_key) if openai_key else None
            self.settings.opencode_api_protocol = "openai_compatible"
        elif opencode_provider.get("protocol") == "anthropic_compatible":
            opencode_key = self._api_provider_key(config, opencode_provider)
            self.settings.opencode_api_base_url = str(opencode_provider.get("base_url") or self.settings.deepseek_api_base_url)
            self.settings.opencode_api_key = SecretStr(opencode_key) if opencode_key else None
            self.settings.opencode_api_protocol = "anthropic_compatible"
        if opencode_provider.get("model"):
            self.settings.opencode_executor_model = str(opencode_provider["model"])

    def _effective_deepseek_api_key(self, config: dict[str, Any]) -> str:
        configured = str(config.get("deepseek_api_key") or "").strip()
        if configured:
            return configured
        if self.settings.deepseek_api_key:
            return self.settings.deepseek_api_key.get_secret_value()
        return os.environ.get("BLUEPRINT_DEEPSEEK_API_KEY", "").strip()

    @staticmethod
    def _effective_tavily_api_key(config: dict[str, Any]) -> str:
        configured = str(config.get("tavily_api_key") or "").strip()
        if configured:
            return configured
        return os.environ.get("TAVILY_API_KEY", "").strip()

    def _effective_anthropic_api_key(self, config: dict[str, Any]) -> str:
        configured = str(config.get("anthropic_api_key") or "").strip()
        if configured:
            return configured
        if self.settings.anthropic_api_key:
            return self.settings.anthropic_api_key.get_secret_value()
        return os.environ.get("ANTHROPIC_API_KEY", "").strip()

    def _effective_openai_api_key(self, config: dict[str, Any]) -> str:
        configured = str(config.get("openai_api_key") or "").strip()
        if configured:
            return configured
        if self.settings.openai_api_key:
            return self.settings.openai_api_key.get_secret_value()
        return os.environ.get("OPENAI_API_KEY", "").strip()

    def _default_api_provider_profiles(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "provider_id": "deepseek",
                "display_name": "DeepSeek",
                "protocol": "anthropic_compatible",
                "model": str(config.get("manager_model") or self.settings.manager_model),
                "base_url": str(config.get("deepseek_api_base_url") or self.settings.deepseek_api_base_url),
                "native_base_url": str(config.get("pi_deepseek_base_url") or self.settings.pi_deepseek_base_url),
            },
            {
                "provider_id": "openai",
                "display_name": "OpenAI Compatible",
                "protocol": "openai_compatible",
                "model": "gpt-4o-mini",
                "base_url": str(config.get("openai_api_base_url") or self.settings.openai_api_base_url),
                "native_base_url": "",
            },
            {
                "provider_id": "anthropic",
                "display_name": "Anthropic Compatible",
                "protocol": "anthropic_compatible",
                "model": str(config.get("manager_model") or self.settings.manager_model),
                "base_url": str(config.get("anthropic_api_base_url") or self.settings.anthropic_api_base_url),
                "native_base_url": "",
            },
        ]

    def _api_provider_profiles(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        if "api_provider_profiles" in config:
            sanitized = self._sanitize_api_provider_profiles(config.get("api_provider_profiles") or [])
            return sanitized or self._default_api_provider_profiles(config)
        stored = []
        defaults = self._default_api_provider_profiles(config)
        merged = [*stored]
        stored_ids = {item["provider_id"] for item in stored}
        merged.extend(item for item in defaults if item["provider_id"] not in stored_ids)
        return merged

    def _public_api_provider_profiles(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                **profile,
                "api_key_configured": bool(self._api_provider_key(config, profile)),
                "test_result": self._provider_test_result(config, profile),
            }
            for profile in self._api_provider_profiles(config)
        ]

    def _secret_api_provider_profiles(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                **profile,
                "api_key": self._api_provider_key(config, profile),
                "api_key_configured": bool(self._api_provider_key(config, profile)),
            }
            for profile in self._api_provider_profiles(config)
        ]

    def _resolve_api_provider(self, config: dict[str, Any], provider_id: Any) -> dict[str, Any] | None:
        clean_id = self._clean_provider_id(provider_id)
        profiles = self._api_provider_profiles(config)
        if clean_id:
            match = next((profile for profile in profiles if profile.get("provider_id") == clean_id), None)
            if match:
                return match
            return None
        return profiles[0] if profiles else None

    def _require_api_provider(self, config: dict[str, Any], provider_id: Any, *, role: str) -> dict[str, Any]:
        provider = self._resolve_api_provider(config, provider_id)
        if provider is None:
            clean_id = self._clean_provider_id(provider_id) or "<missing>"
            raise HTTPException(
                status_code=409,
                detail=f"Provider binding for {role} references unknown provider_id={clean_id}. Update API settings and try again.",
            )
        return provider

    def _api_provider_key(self, config: dict[str, Any], profile: dict[str, Any]) -> str:
        provider_id = str(profile.get("provider_id") or "").strip()
        keys = config.get("api_provider_keys")
        if isinstance(keys, dict):
            configured = str(keys.get(provider_id) or "").strip()
            if configured:
                return configured
        if provider_id == "deepseek":
            return self._effective_deepseek_api_key(config)
        if provider_id == "openai":
            return self._effective_openai_api_key(config)
        if provider_id == "anthropic":
            return self._effective_anthropic_api_key(config)
        return ""

    def _provider_test_result(self, config: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any] | None:
        provider_id = str(profile.get("provider_id") or "").strip()
        results = config.get("api_provider_test_results")
        if not provider_id or not isinstance(results, dict):
            return None
        stored = results.get(provider_id)
        if not isinstance(stored, dict):
            return None
        if stored.get("fingerprint") != self._provider_test_fingerprint(profile):
            return None
        result = stored.get("result")
        return dict(result) if isinstance(result, dict) else None

    def _save_provider_test_result(self, config: dict[str, Any], profile: dict[str, Any], result: dict[str, Any]) -> None:
        provider_id = str(profile.get("provider_id") or "").strip()
        if not provider_id:
            return
        results = config.get("api_provider_test_results")
        if not isinstance(results, dict):
            results = {}
        results[provider_id] = {
            "fingerprint": self._provider_test_fingerprint(profile),
            "result": dict(result),
        }
        config["api_provider_test_results"] = results
        self._save(config)

    def _clear_provider_test_result(self, config: dict[str, Any], provider_id: str) -> None:
        results = config.get("api_provider_test_results")
        if isinstance(results, dict):
            results.pop(provider_id, None)
            if not results:
                config.pop("api_provider_test_results", None)

    def _prune_provider_test_results(self, config: dict[str, Any], profiles: list[dict[str, Any]]) -> None:
        results = config.get("api_provider_test_results")
        if not isinstance(results, dict):
            return
        profiles_by_id = {str(profile.get("provider_id") or "").strip(): profile for profile in profiles}
        for provider_id in list(results):
            profile = profiles_by_id.get(provider_id)
            stored = results.get(provider_id)
            if not profile or not isinstance(stored, dict) or stored.get("fingerprint") != self._provider_test_fingerprint(profile):
                results.pop(provider_id, None)
        if not results:
            config.pop("api_provider_test_results", None)

    @staticmethod
    def _provider_test_fingerprint(profile: dict[str, Any]) -> str:
        payload = {
            "provider_id": str(profile.get("provider_id") or "").strip(),
            "protocol": str(profile.get("protocol") or "").strip(),
            "model": str(profile.get("model") or "").strip(),
            "base_url": str(profile.get("base_url") or "").strip().rstrip("/"),
            "native_base_url": str(profile.get("native_base_url") or "").strip().rstrip("/"),
        }
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _provider_native_base_url(profile: dict[str, Any]) -> str:
        configured = str(profile.get("native_base_url") or "").strip().rstrip("/")
        if configured:
            return configured
        base_url = str(profile.get("base_url") or "").strip().rstrip("/")
        if base_url.endswith("/anthropic"):
            return base_url.removesuffix("/anthropic")
        return base_url

    def _resolve_role_binding(self, config: dict[str, Any], role: str) -> dict[str, str]:
        bindings = self._sanitize_provider_bindings(config.get("provider_bindings") or {})
        default = DEFAULT_PROVIDER_BINDINGS[role]
        resolved = dict(bindings.get(role) or {})
        resolved.setdefault("provider_id", default["provider_id"])
        return resolved

    def _public_provider_bindings(
        self,
        config: dict[str, Any],
        *,
        provider_profiles: list[dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for role in DEFAULT_PROVIDER_BINDINGS:
            binding = self._resolve_role_binding(config, role)
            result[role] = dict(binding)
        return result

    def _validate_provider_bindings(
        self,
        bindings: dict[str, dict[str, str]],
        provider_profiles: list[dict[str, Any]],
    ) -> None:
        provider_ids = {str(profile.get("provider_id") or "").strip() for profile in provider_profiles}
        providers_by_id = {str(profile.get("provider_id") or "").strip(): profile for profile in provider_profiles}
        missing_roles: list[str] = []
        unknown_bindings: list[str] = []
        incompatible_bindings: list[str] = []
        for role in DEFAULT_PROVIDER_BINDINGS:
            provider_id = self._clean_provider_id((bindings.get(role) or {}).get("provider_id"))
            if not provider_id:
                missing_roles.append(role)
                continue
            if provider_id not in provider_ids:
                unknown_bindings.append(f"{role}={provider_id}")
                continue
            protocol = str(providers_by_id[provider_id].get("protocol") or "")
            allowed_protocols = ROLE_PROVIDER_PROTOCOLS.get(role, API_PROVIDER_PROTOCOLS)
            if protocol not in allowed_protocols:
                incompatible_bindings.append(f"{role}={provider_id}({protocol})")
        if missing_roles or unknown_bindings or incompatible_bindings:
            details: list[str] = []
            if missing_roles:
                details.append(f"Missing provider bindings for: {', '.join(missing_roles)}")
            if unknown_bindings:
                details.append(f"Unknown provider bindings: {', '.join(unknown_bindings)}")
            if incompatible_bindings:
                details.append(f"Incompatible provider bindings: {', '.join(incompatible_bindings)}")
            raise HTTPException(status_code=400, detail="; ".join(details))

    def _sanitize_api_provider_profiles(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        profiles: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            provider_id = self._clean_provider_id(item.get("provider_id"))
            if not provider_id or provider_id in seen:
                continue
            protocol = str(item.get("protocol") or "").strip()
            if protocol not in API_PROVIDER_PROTOCOLS:
                continue
            base_url = str(item.get("base_url") or "").strip()
            if not base_url:
                continue
            profiles.append(
                {
                    "provider_id": provider_id,
                    "display_name": str(item.get("display_name") or provider_id).strip() or provider_id,
                    "protocol": protocol,
                    "model": str(item.get("model") or "").strip(),
                    "base_url": base_url,
                    "native_base_url": str(item.get("native_base_url") or "").strip(),
                }
            )
            seen.add(provider_id)
        return profiles

    def _sanitize_provider_bindings(self, value: Any) -> dict[str, dict[str, str]]:
        if not isinstance(value, dict):
            return {}
        bindings: dict[str, dict[str, str]] = {}
        for role in DEFAULT_PROVIDER_BINDINGS:
            item = value.get(role)
            if not isinstance(item, dict):
                continue
            provider_id = self._clean_provider_id(item.get("provider_id"))
            binding: dict[str, str] = {}
            if provider_id:
                binding["provider_id"] = provider_id
            if binding:
                bindings[role] = binding
        return bindings

    @staticmethod
    def _clean_provider_id(value: Any) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9_.-]+", "-", text)
        return text.strip("-")

    def _available_executors(self) -> list[str]:
        available = []
        for name in ("pi", "opencode", "codex", "claude_code"):
            str_setting = getattr(self.settings, f"{name}_command", None)
            json_setting = getattr(self.settings, f"{name}_command_json", None)
            if str_setting or json_setting:
                available.append(name)
        return available

    @staticmethod
    def _effective_bool(config_value: Any, env_value: str | None, *, default: bool) -> bool:
        if config_value is not None:
            return bool(config_value)
        if env_value is None:
            return default
        return env_value.lower() in {"1", "true", "yes", "on"}

    def list_executor_profiles(self) -> list[dict[str, Any]]:
        config = self._load()
        profiles = config.get("executor_profiles")
        if not isinstance(profiles, list):
            return []
        return [item for item in profiles if isinstance(item, dict)]

    def save_executor_profile(self, profile: dict[str, Any]) -> None:
        config = self._load()
        profiles = config.get("executor_profiles")
        if not isinstance(profiles, list):
            profiles = [item.model_dump() for item in default_profiles()]
        profile_id = profile.get("profile_id")
        existing_index = next(
            (i for i, item in enumerate(profiles) if isinstance(item, dict) and item.get("profile_id") == profile_id),
            None,
        )
        if existing_index is not None:
            profiles[existing_index] = profile
        else:
            profiles.append(profile)
        config["executor_profiles"] = profiles
        self._save(config)

    def delete_executor_profile(self, profile_id: str) -> None:
        config = self._load()
        profiles = config.get("executor_profiles")
        if not isinstance(profiles, list):
            return
        config["executor_profiles"] = [
            item for item in profiles if not (isinstance(item, dict) and item.get("profile_id") == profile_id)
        ]
        self._save(config)

    def test_api_provider(self, profile: dict[str, Any], *, api_key: str | None = None, timeout_seconds: int = 30) -> dict[str, Any]:
        profiles = self._sanitize_api_provider_profiles([profile])
        if not profiles:
            return {"ok": False, "message": "Provider profile is incomplete. Check protocol, model, and base URL."}
        provider = profiles[0]
        config = self._load()
        resolved_key = str(api_key or "").strip() or self._api_provider_key(config, provider)
        if not resolved_key:
            return {"ok": False, "message": "API key is missing for this provider."}
        model = str(provider.get("model") or "").strip()
        if not model:
            return {"ok": False, "message": "Model name is missing."}

        started = time.monotonic()
        try:
            if provider["protocol"] == "anthropic_compatible":
                endpoint = self._anthropic_messages_url(str(provider["base_url"]))
                payload = {
                    "model": model,
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "Reply with OK."}]}],
                }
                headers = {
                    "content-type": "application/json",
                    "x-api-key": resolved_key,
                    "anthropic-version": "2023-06-01",
                }
            else:
                endpoint = self._openai_chat_completions_url(str(provider["base_url"]))
                payload = {
                    "model": model,
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                }
                headers = {
                    "content-type": "application/json",
                    "authorization": f"Bearer {resolved_key}",
                }
            http_request = request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers=headers,
            )
            with request.urlopen(http_request, timeout=timeout_seconds) as response:
                response.read(4096)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1200]
            result = {
                "ok": False,
                "message": f"Model test failed with HTTP {exc.code}.",
                "status_code": exc.code,
                "detail": detail,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            result = {
                "ok": False,
                "message": f"Model test failed: {exc}",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        else:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            result = {"ok": True, "message": "Model test succeeded.", "latency_ms": elapsed_ms}

        self._save_provider_test_result(config, provider, result)
        return result

    @staticmethod
    def _anthropic_messages_url(base_url: str) -> str:
        value = str(base_url or "").rstrip("/")
        if value.endswith("/v1/messages"):
            return value
        if value.endswith("/v1"):
            return f"{value}/messages"
        return f"{value}/v1/messages"

    @staticmethod
    def _openai_chat_completions_url(base_url: str) -> str:
        value = str(base_url or "").rstrip("/")
        if value.endswith("/chat/completions"):
            return value
        return f"{value}/chat/completions"

    def resolve_executor_command(self, worker_type: str) -> str | None:
        setting_name = f"{worker_type}_command"
        return getattr(self.settings, setting_name, None)

    def resolve_executor_profile(self, worker_type: str, profile_id: str | None = None) -> dict[str, Any] | None:
        stored_profiles = self.list_executor_profiles()
        default_profile_items = [profile.model_dump() for profile in default_profiles()]
        profiles = stored_profiles + [
            item for item in default_profile_items
            if not any(stored.get("profile_id") == item.get("profile_id") for stored in stored_profiles)
        ]
        if profile_id:
            return next(
                (item for item in profiles if item.get("profile_id") == profile_id and item.get("worker_type") == worker_type),
                None,
            )
        preferred_auth_mode = "project_api" if worker_type in {"pi", "opencode"} else "cli_native"
        preferred = next(
            (
                item for item in profiles
                if item.get("worker_type") == worker_type
                and item.get("enabled", True)
                and item.get("auth_mode") == preferred_auth_mode
            ),
            None,
        )
        if preferred:
            return preferred
        return next(
            (item for item in profiles if item.get("worker_type") == worker_type and item.get("enabled", True)),
            None,
        )
