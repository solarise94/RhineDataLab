"""Tests for executor profile validation and provider renderers.

Covers the acceptance criteria from docs/19_noninteractive_cli_executor_compatibility_plan.md:

- Profile validation accepts supported combinations and rejects unsupported ones.
- cli_native renderers do not include provider API env keys.
- project_api renderers include only their selected protocol keys.
- claude_code project_api is intentionally unsupported.
- codex project_api is rejected.
- Secret redaction covers command, trace, and config plan.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from app.models.executor_profiles import (
    AUTH_MODE_CLI_NATIVE,
    AUTH_MODE_PROJECT_API,
    ExecutorProfileSpec,
    SUPPORTED_AUTH_MODES,
    SUPPORTED_API_PROTOCOLS,
    default_profiles,
    validate_profile,
)
from app.services.app_config_service import AppConfigService
from app.workers.provider_renderers.base import ProviderRenderer, ProviderRenderResult
from app.workers.provider_renderers.claude_code import ClaudeCodeRenderer
from app.workers.provider_renderers.codex import CodexRenderer
from app.workers.provider_renderers.opencode import OpenCodeRenderer
from app.workers.provider_renderers.pi import PiRenderer
from app.workers.provider_renderers import get_renderer_registry


# ---------------------------------------------------------------------------
# Profile validation tests
# ---------------------------------------------------------------------------


class TestProfileValidation(unittest.TestCase):
    def _base_profile(self, **overrides) -> ExecutorProfileSpec:
        base = {
            "profile_id": "test-profile",
            "display_name": "Test Profile",
            "worker_type": "opencode",
            "auth_mode": AUTH_MODE_CLI_NATIVE,
            "enabled": True,
        }
        base.update(overrides)
        return ExecutorProfileSpec(**base)

    def test_cli_native_opencode_is_valid(self):
        spec = self._base_profile(worker_type="opencode", auth_mode=AUTH_MODE_CLI_NATIVE)
        result = validate_profile(spec)
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])

    def test_project_api_opencode_is_valid(self):
        spec = self._base_profile(
            worker_type="opencode",
            auth_mode=AUTH_MODE_PROJECT_API,
            api_protocol="openai_compatible",
            credential_ref="project:openai_api_key",
        )
        result = validate_profile(spec)
        self.assertTrue(result.valid)

    def test_cli_native_claude_code_is_valid(self):
        spec = self._base_profile(worker_type="claude_code", auth_mode=AUTH_MODE_CLI_NATIVE)
        result = validate_profile(spec)
        self.assertTrue(result.valid)

    def test_project_api_claude_code_is_rejected(self):
        with self.assertRaises(Exception) as ctx:
            ExecutorProfileSpec(
                profile_id="test-cc-openai",
                display_name="CC OpenAI",
                worker_type="claude_code",
                auth_mode=AUTH_MODE_PROJECT_API,
                api_protocol="openai_compatible",
                credential_ref="project:openai_api_key",
            )
        self.assertIn("not supported", str(ctx.exception))

    def test_codex_cli_native_is_valid(self):
        spec = self._base_profile(worker_type="codex", auth_mode=AUTH_MODE_CLI_NATIVE)
        result = validate_profile(spec)
        self.assertTrue(result.valid)

    def test_codex_project_api_is_rejected(self):
        with self.assertRaises(Exception):
            ExecutorProfileSpec(
                profile_id="codex-project",
                display_name="Codex project API",
                worker_type="codex",
                auth_mode=AUTH_MODE_PROJECT_API,
            )

    def test_codex_project_api_rejected_in_validation(self):
        spec = ExecutorProfileSpec(
            profile_id="codex-native",
            display_name="Codex native",
            worker_type="codex",
            auth_mode=AUTH_MODE_CLI_NATIVE,
        )
        result = validate_profile(spec)
        self.assertTrue(result.valid)

    def test_pi_project_api_is_valid(self):
        spec = self._base_profile(
            worker_type="pi",
            auth_mode=AUTH_MODE_PROJECT_API,
            api_protocol="deepseek_compatible",
        )
        result = validate_profile(spec)
        self.assertTrue(result.valid)

    def test_pi_cli_native_is_valid(self):
        spec = ExecutorProfileSpec(
            profile_id="pi-native",
            display_name="Pi native",
            worker_type="pi",
            auth_mode=AUTH_MODE_CLI_NATIVE,
        )
        result = validate_profile(spec)
        self.assertTrue(result.valid)

    def test_disabled_profile_is_always_valid(self):
        spec = self._base_profile(enabled=False)
        result = validate_profile(spec)
        self.assertTrue(result.valid)
        self.assertTrue(any("disabled" in w.lower() for w in result.warnings))

    def test_default_profiles_all_validate(self):
        for profile in default_profiles():
            result = validate_profile(profile)
            self.assertTrue(result.valid, f"Profile {profile.profile_id} failed: {result.errors}")

    def test_support_matrix_has_all_workers(self):
        self.assertEqual(set(SUPPORTED_AUTH_MODES.keys()), {"pi", "opencode", "claude_code", "codex"})
        self.assertEqual(set(SUPPORTED_API_PROTOCOLS.keys()), {"pi", "opencode", "claude_code", "codex"})


# ---------------------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------------------


def _make_settings(**overrides) -> SimpleNamespace:
    defaults = {
        "deepseek_api_key": SimpleNamespace(get_secret_value=lambda: "sk-test-deepseek-key-123"),
        "deepseek_api_base_url": "https://api.deepseek.com/anthropic",
        "pi_deepseek_base_url": "https://api.deepseek.com",
        "manager_model": "deepseek-v4-pro",
        "executor_model": "deepseek-v4-flash",
        "reviewer_model": "deepseek-v4-flash",
        "library_summarizer_model": "deepseek-v4-flash",
        "manager_temperature": 0.2,
        "manager_max_tokens": 2400,
        "manager_timeout_seconds": 600,
        "worker_timeout_seconds": 900,
        "manifest_repair_timeout_seconds": 180,
        "anthropic_api_key": SimpleNamespace(get_secret_value=lambda: "sk-ant-test-key-456"),
        "anthropic_api_base_url": "https://api.anthropic.com",
        "openai_api_key": None,
        "openai_api_base_url": "https://api.openai.com/v1",
        "default_worker_type": "pi",
        "pi_command": None,
        "pi_command_json": None,
        "opencode_command": None,
        "opencode_command_json": None,
        "codex_command": None,
        "codex_command_json": None,
        "claude_code_command": None,
        "claude_code_command_json": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_prompt(tmp_path: Path) -> Path:
    prompt_path = tmp_path / "executor_prompt.md"
    prompt_path.write_text("# Test prompt\n\nDo something useful.\n", encoding="utf-8")
    return prompt_path


def _make_capability_packet() -> dict:
    return {
        "executor_context": {
            "skills": ["spatial-10x-converter"],
            "mcp_servers": ["omicverse"],
            "tool_policy": {
                "network": "allow",
                "python": True,
                "rscript": False,
                "shell": True,
                "git_write": False,
            },
        }
    }


def _write_capability_files(run_dir: Path) -> None:
    library_dir = run_dir / "library"
    skill_dir = library_dir / "skills" / "spatial-10x-converter"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# spatial converter\n", encoding="utf-8")
    (library_dir / "skill_bindings.json").write_text(
        json.dumps(
            [
                {
                    "id": "spatial-10x-converter",
                    "source_path": str(skill_dir / "SKILL.md"),
                    "run_path": str(skill_dir),
                }
            ]
        ),
        encoding="utf-8",
    )
    (library_dir / "mcp_bindings.json").write_text(
        json.dumps(
            [
                {
                    "id": "omicverse",
                    "config": {
                        "mcpServers": {
                            "omicverse": {
                                "command": "/envs/omicverse/bin/python",
                                "args": ["-m", "omicverse.mcp"],
                            }
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    (library_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "omicverse": {
                        "command": "/envs/omicverse/bin/python",
                        "args": ["-m", "omicverse.mcp"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )


class TestPiRenderer(unittest.TestCase):
    def test_project_api_renders_env_overlay(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = PiRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            prompt_path = _make_prompt(tmp_path)
            settings = _make_settings()
            profile = ExecutorProfileSpec(
                profile_id="pi-project",
                display_name="Pi",
                worker_type="pi",
                auth_mode=AUTH_MODE_PROJECT_API,
                api_protocol="deepseek_compatible",
            )
            result = renderer.render(
                auth_mode=AUTH_MODE_PROJECT_API,
                profile=profile,
                prompt_path=prompt_path,
                run_dir=run_dir,
                project_root=tmp_path,
                settings=settings,
            )
            self.assertTrue(result.is_supported)
            self.assertEqual(result.worker_type, "pi")
            self.assertEqual(result.auth_mode, AUTH_MODE_PROJECT_API)
            self.assertIn("BLUEPRINT_DEEPSEEK_API_KEY", result.environment_overlay)
            self.assertEqual(result.environment_overlay["BLUEPRINT_DEEPSEEK_API_KEY"], "sk-test-deepseek-key-123")
            self.assertEqual(result.provider_config_plan["provider_id"], "deepseek")

    def test_cli_native_uses_host_auth_without_project_key(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = PiRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            result = renderer.render(
                auth_mode=AUTH_MODE_CLI_NATIVE,
                profile=None,
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=_make_settings(),
            )
            self.assertTrue(result.is_supported)
            self.assertEqual(result.auth_mode, AUTH_MODE_CLI_NATIVE)
            self.assertNotIn("BLUEPRINT_DEEPSEEK_API_KEY", result.environment_overlay)
            self.assertFalse(result.provider_config_plan["credential_injected"])


class TestOpenCodeRenderer(unittest.TestCase):
    def test_cli_native_no_api_keys(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = OpenCodeRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            result = renderer.render(
                auth_mode=AUTH_MODE_CLI_NATIVE,
                profile=None,
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=_make_settings(),
            )
            self.assertTrue(result.is_supported)
            # cli_native may set OPENCODE_CONFIG_DIR to point to host auth path, but should not set API keys
            self.assertNotIn("OPENAI_API_KEY", result.environment_overlay)
            self.assertIn("opencode", result.command_argv)
            self.assertFalse(result.provider_config_plan["credential_injected"])

    def test_project_api_generates_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = OpenCodeRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            profile = ExecutorProfileSpec(
                profile_id="opencode-project",
                display_name="OpenCode project API",
                worker_type="opencode",
                auth_mode=AUTH_MODE_PROJECT_API,
                api_protocol="openai_compatible",
                provider_id="openai",
                model="gpt-4o",
                base_url="https://api.openai.com/v1",
                credential_ref="project:openai_api_key",
            )
            settings = _make_settings(
                openai_api_key=SimpleNamespace(get_secret_value=lambda: "sk-openai-test-789"),
            )
            result = renderer.render(
                auth_mode=AUTH_MODE_PROJECT_API,
                profile=profile,
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=settings,
            )
            self.assertTrue(result.is_supported)
            self.assertIn("OPENAI_API_KEY", result.environment_overlay)
            self.assertEqual(result.environment_overlay["OPENAI_API_KEY"], "sk-openai-test-789")
            config_path = run_dir / "opencode-config" / "opencode.json"
            self.assertTrue(config_path.exists())
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIn("openai", config["provider"])
            self.assertEqual(config["model"], "openai/gpt-4o")
            self.assertEqual(config["provider"]["openai"]["options"]["baseURL"], "https://api.openai.com/v1")
            self.assertEqual(result.config_file_paths, [str(config_path)])
            self.assertEqual(result.environment_overlay["OPENCODE_CONFIG_DIR"], str(config_path.parent))

    def test_project_api_generates_anthropic_compatible_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = OpenCodeRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            profile = ExecutorProfileSpec(
                profile_id="opencode-project",
                display_name="OpenCode project API",
                worker_type="opencode",
                auth_mode=AUTH_MODE_PROJECT_API,
                api_protocol="anthropic_compatible",
                provider_id="anthropic",
                model="deepseek-v4-flash",
                base_url="https://api.deepseek.com/anthropic",
                credential_ref="project:opencode_api_key",
            )
            settings = _make_settings(
                opencode_api_key=SimpleNamespace(get_secret_value=lambda: "sk-ant-opencode-123"),
                opencode_api_base_url="https://api.deepseek.com/anthropic",
                opencode_api_protocol="anthropic_compatible",
            )
            result = renderer.render(
                auth_mode=AUTH_MODE_PROJECT_API,
                profile=profile,
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=settings,
            )
            self.assertTrue(result.is_supported)
            self.assertIn("ANTHROPIC_API_KEY", result.environment_overlay)
            self.assertNotIn("OPENAI_API_KEY", result.environment_overlay)
            config = json.loads((run_dir / "opencode-config" / "opencode.json").read_text(encoding="utf-8"))
            self.assertIn("anthropic", config["provider"])
            self.assertEqual(config["model"], "anthropic/deepseek-v4-flash")
            self.assertEqual(config["provider"]["anthropic"]["options"]["baseURL"], "https://api.deepseek.com/anthropic/v1")

    def test_project_api_uses_settings_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = OpenCodeRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            profile = ExecutorProfileSpec(
                profile_id="opencode-project",
                display_name="OpenCode project API",
                worker_type="opencode",
                auth_mode=AUTH_MODE_PROJECT_API,
                api_protocol="openai_compatible",
                provider_id="openai",
                model="gpt-4o-mini",
            )
            settings = _make_settings(
                openai_api_key=SimpleNamespace(get_secret_value=lambda: "sk-openai-fallback-123"),
                openai_api_base_url="https://gateway.example.com/v1",
            )
            result = renderer.render(
                auth_mode=AUTH_MODE_PROJECT_API,
                profile=profile,
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=settings,
            )
            self.assertTrue(result.is_supported)
            self.assertEqual(result.environment_overlay["OPENAI_API_KEY"], "sk-openai-fallback-123")
            self.assertEqual(result.provider_config_plan["credential_ref"], "project:openai_api_key")
            self.assertEqual(result.provider_config_plan["base_url"], "https://gateway.example.com/v1")
            config = json.loads((run_dir / "opencode-config" / "opencode.json").read_text(encoding="utf-8"))
            self.assertEqual(config["provider"]["openai"]["options"]["baseURL"], "https://gateway.example.com/v1")

    def test_project_api_writes_capability_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = OpenCodeRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            _write_capability_files(run_dir)
            profile = ExecutorProfileSpec(
                profile_id="opencode-project",
                display_name="OpenCode project API",
                worker_type="opencode",
                auth_mode=AUTH_MODE_PROJECT_API,
                api_protocol="openai_compatible",
                provider_id="openai",
                credential_ref="project:openai_api_key",
            )
            settings = _make_settings(openai_api_key=SimpleNamespace(get_secret_value=lambda: "sk-openai-test-789"))
            result = renderer.render(
                auth_mode=AUTH_MODE_PROJECT_API,
                profile=profile,
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=settings,
                packet=_make_capability_packet(),
            )
            self.assertTrue(result.is_supported)
            self.assertIn("OPENCODE_MCP_CONFIG", result.environment_overlay)
            self.assertIn("BLUEPRINT_EXECUTOR_SKILL_PATHS", result.environment_overlay)
            config_path = run_dir / "opencode-config" / "opencode.json"
            self.assertTrue(config_path.exists())
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["skills"]["ids"], ["spatial-10x-converter"])
            self.assertEqual(config["mcp"]["ids"], ["omicverse"])
            self.assertEqual(config["tool_policy"]["git_write"], False)


class TestClaudeCodeRenderer(unittest.TestCase):
    def test_cli_native_no_api_keys(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = ClaudeCodeRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            result = renderer.render(
                auth_mode=AUTH_MODE_CLI_NATIVE,
                profile=None,
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=_make_settings(),
            )
            self.assertTrue(result.is_supported)
            self.assertNotIn("ANTHROPIC_API_KEY", result.environment_overlay)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", result.environment_overlay)
            self.assertNotIn("ANTHROPIC_BASE_URL", result.environment_overlay)
            self.assertFalse(result.provider_config_plan["credential_injected"])
            self.assertIn("claude", result.command_argv)

    def test_project_api_not_supported(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = ClaudeCodeRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            result = renderer.render(
                auth_mode=AUTH_MODE_PROJECT_API,
                profile=SimpleNamespace(),
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=_make_settings(),
            )
            self.assertFalse(result.is_supported)
            self.assertIn("not supported", result.unsupported_error)

    def test_cli_native_maps_capabilities_to_argv_and_env(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = ClaudeCodeRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            _write_capability_files(run_dir)
            profile = ExecutorProfileSpec(
                profile_id="cc-native",
                display_name="Claude Code native",
                worker_type="claude_code",
                auth_mode=AUTH_MODE_CLI_NATIVE,
            )
            result = renderer.render(
                auth_mode=AUTH_MODE_CLI_NATIVE,
                profile=profile,
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=_make_settings(),
                packet=_make_capability_packet(),
            )
            self.assertTrue(result.is_supported)
            self.assertIn("--mcp-config", result.command_argv)
            self.assertIn(str(run_dir / "library" / "mcp.json"), result.command_argv)
            self.assertIn("--disallowedTools", result.command_argv)
            self.assertIn("BLUEPRINT_EXECUTOR_SKILL_PATHS", result.environment_overlay)
            self.assertEqual(result.provider_config_plan["capabilities"]["mcp_servers"], ["omicverse"])


class TestCodexRenderer(unittest.TestCase):
    def test_cli_native_no_api_keys(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = CodexRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            result = renderer.render(
                auth_mode=AUTH_MODE_CLI_NATIVE,
                profile=None,
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=_make_settings(),
            )
            self.assertTrue(result.is_supported)
            # cli_native may set CODEX_CONFIG_DIR to point to host auth path, but should not set API keys
            self.assertNotIn("OPENAI_API_KEY", result.environment_overlay)
            self.assertIn("codex", result.command_argv)
            self.assertFalse(result.provider_config_plan["credential_injected"])

    def test_project_api_blocked(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = CodexRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            result = renderer.render(
                auth_mode=AUTH_MODE_PROJECT_API,
                profile=None,
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=_make_settings(),
            )
            self.assertFalse(result.is_supported)
            self.assertIn("not implemented", result.unsupported_error.lower())


# ---------------------------------------------------------------------------
# Secret redaction tests
# ---------------------------------------------------------------------------


class TestSecretRedaction(unittest.TestCase):
    def test_redact_command_api_key(self):
        command = ["claude", "--api-key", "sk-ant-secret-12345", "--model", "claude-sonnet-4-5"]
        redacted = ProviderRenderer.redact_command(command)
        self.assertNotIn("sk-ant-secret-12345", " ".join(redacted))
        self.assertIn("[REDACTED]", redacted)
        self.assertIn("claude-sonnet-4-5", redacted)

    def test_redact_command_inline_key(self):
        command = ["some-tool", "--api-key=sk-secret-value-xyz", "--other", "safe-value"]
        redacted = ProviderRenderer.redact_command(command)
        self.assertNotIn("sk-secret-value-xyz", " ".join(redacted))
        self.assertIn("safe-value", redacted)

    def test_redact_environment(self):
        env = {
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "HOME": "/home/user",
            "PATH": "/usr/bin",
        }
        redacted = ProviderRenderer.redact_environment(env)
        self.assertEqual(redacted["ANTHROPIC_API_KEY"], "[REDACTED]")
        self.assertEqual(redacted["HOME"], "/home/user")
        self.assertEqual(redacted["PATH"], "/usr/bin")

    def test_redact_text_sk_tokens(self):
        text = "Using key sk-abc123def456 for authentication"
        redacted = ProviderRenderer.redact_text(text)
        self.assertNotIn("sk-abc123def456", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_provider_config_plan_written(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            renderer = CodexRenderer()
            run_dir = tmp_path / "run"
            run_dir.mkdir()
            result = renderer.render(
                auth_mode=AUTH_MODE_CLI_NATIVE,
                profile=None,
                prompt_path=_make_prompt(tmp_path),
                run_dir=run_dir,
                project_root=tmp_path,
                settings=_make_settings(),
            )
            plan_path = result.write_provider_config_plan(run_dir)
            self.assertTrue(plan_path.exists())
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["schema_version"], "provider_config_plan.v1")
            self.assertEqual(plan["worker_type"], "codex")
            self.assertEqual(plan["auth_mode"], AUTH_MODE_CLI_NATIVE)
            self.assertTrue(plan["redacted"])
            # cli_native may include host_auth_path in the plan
            self.assertTrue("host_auth_path" in plan or plan.get("credential_injected") is False)


# ---------------------------------------------------------------------------
# Renderer registry tests
# ---------------------------------------------------------------------------


class TestRendererRegistry(unittest.TestCase):
    def test_registry_has_all_workers(self):
        registry = get_renderer_registry()
        workers = registry.list_worker_types()
        self.assertIn("pi", workers)
        self.assertIn("opencode", workers)
        self.assertIn("claude_code", workers)
        self.assertIn("codex", workers)

    def test_registry_get_returns_renderer(self):
        registry = get_renderer_registry()
        for worker in ["pi", "opencode", "claude_code", "codex"]:
            renderer = registry.get(worker)
            self.assertIsNotNone(renderer)
            self.assertEqual(renderer.worker_type, worker)

    def test_registry_get_unknown_returns_none(self):
        registry = get_renderer_registry()
        self.assertIsNone(registry.get("unknown_worker"))


# ---------------------------------------------------------------------------
# App config profile resolution tests
# ---------------------------------------------------------------------------


class TestExecutorProfileResolution(unittest.TestCase):
    def test_app_config_cache_reloads_when_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir, deepseek_api_key=None)
            service = AppConfigService(settings)
            config_path = Path(tmp_dir) / "_app_settings.json"
            config_path.write_text(
                json.dumps({"openai_api_base_url": "https://example.test/v1"}),
                encoding="utf-8",
            )

            secret_settings = service.get_secret_settings()

            self.assertEqual(secret_settings["openai_api_base_url"], "https://example.test/v1")

    def test_stored_profiles_do_not_hide_missing_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir)
            service = AppConfigService(settings)
            service.save_executor_profile(
                {
                    "profile_id": "custom-opencode",
                    "display_name": "Custom OpenCode",
                    "worker_type": "opencode",
                    "auth_mode": AUTH_MODE_CLI_NATIVE,
                    "enabled": True,
                }
            )

            resolved = service.resolve_executor_profile("pi", profile_id="pi-project-api")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved["profile_id"], "pi-project-api")

    def test_profile_id_must_match_worker_type(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir)
            service = AppConfigService(settings)

            self.assertIsNone(service.resolve_executor_profile("pi", profile_id="opencode-cli-native"))

    def test_api_provider_bindings_override_role_settings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir)
            service = AppConfigService(settings)

            public = service.update_settings(
                {
                    "api_provider_profiles": [
                        {
                            "provider_id": "anthropic-gateway",
                            "display_name": "Anthropic Gateway",
                            "protocol": "anthropic_compatible",
                            "model": "manager-model",
                            "base_url": "https://gateway.example.com/anthropic",
                            "native_base_url": "https://gateway.example.com",
                        },
                        {
                            "provider_id": "opencode-gateway",
                            "display_name": "OpenCode Gateway",
                            "protocol": "anthropic_compatible",
                            "model": "opencode-model",
                            "base_url": "https://opencode.example.com/anthropic",
                        },
                    ],
                    "api_provider_keys": {
                        "anthropic-gateway": "sk-ant-gateway",
                        "opencode-gateway": "sk-opencode-gateway",
                    },
                    "provider_bindings": {
                        "manager": {"provider_id": "anthropic-gateway"},
                        "reviewer": {"provider_id": "anthropic-gateway"},
                        "pi_executor": {"provider_id": "anthropic-gateway"},
                        "opencode_executor": {"provider_id": "opencode-gateway"},
                        "library_summarizer": {"provider_id": "anthropic-gateway"},
                    },
                }
            )

            self.assertTrue(
                next(item for item in public["api_provider_profiles"] if item["provider_id"] == "anthropic-gateway")[
                    "api_key_configured"
                ]
            )
            self.assertEqual(settings.manager_api_base_url, "https://gateway.example.com/anthropic")
            self.assertEqual(settings.manager_api_key.get_secret_value(), "sk-ant-gateway")
            self.assertEqual(settings.manager_model, "manager-model")
            self.assertEqual(settings.reviewer_api_base_url, "https://gateway.example.com/anthropic")
            self.assertEqual(settings.reviewer_model, "manager-model")
            self.assertEqual(settings.pi_anthropic_base_url, "https://gateway.example.com/anthropic")
            self.assertEqual(settings.pi_deepseek_base_url, "https://gateway.example.com")
            self.assertEqual(settings.pi_executor_model, "manager-model")
            self.assertEqual(settings.opencode_api_base_url, "https://opencode.example.com/anthropic")
            self.assertEqual(settings.opencode_api_key.get_secret_value(), "sk-opencode-gateway")
            self.assertEqual(settings.opencode_api_protocol, "anthropic_compatible")
            self.assertEqual(settings.opencode_executor_model, "opencode-model")

    def test_empty_api_provider_profiles_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir)
            service = AppConfigService(settings)

            with self.assertRaises(HTTPException) as ctx:
                service.update_settings({"api_provider_profiles": []})

            self.assertEqual(400, ctx.exception.status_code)
            self.assertIn("At least one API provider profile", str(ctx.exception.detail))

    def test_unknown_provider_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir)
            service = AppConfigService(settings)

            with self.assertRaises(HTTPException) as ctx:
                service.update_settings(
                    {
                        "api_provider_profiles": [
                            {
                                "provider_id": "anthropic-gateway",
                                "display_name": "Anthropic Gateway",
                                "protocol": "anthropic_compatible",
                                "model": "manager-model",
                                "base_url": "https://gateway.example.com/anthropic",
                            }
                        ],
                        "provider_bindings": {
                            "manager": {"provider_id": "missing-provider"},
                        },
                    }
                )

            self.assertEqual(400, ctx.exception.status_code)
            self.assertIn("Unknown provider bindings", str(ctx.exception.detail))

    def test_incompatible_provider_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir)
            service = AppConfigService(settings)

            with self.assertRaises(HTTPException) as ctx:
                service.update_settings(
                    {
                        "api_provider_profiles": [
                            {
                                "provider_id": "openai-gateway",
                                "display_name": "OpenAI Gateway",
                                "protocol": "openai_compatible",
                                "model": "gpt-compatible",
                                "base_url": "https://gateway.example.com/v1",
                            },
                            {
                                "provider_id": "anthropic-gateway",
                                "display_name": "Anthropic Gateway",
                                "protocol": "anthropic_compatible",
                                "model": "manager-model",
                                "base_url": "https://gateway.example.com/anthropic",
                            },
                        ],
                        "provider_bindings": {
                            "manager": {"provider_id": "anthropic-gateway"},
                            "reviewer": {"provider_id": "openai-gateway"},
                            "pi_executor": {"provider_id": "anthropic-gateway"},
                            "opencode_executor": {"provider_id": "anthropic-gateway"},
                            "library_summarizer": {"provider_id": "anthropic-gateway"},
                        },
                    }
                )

            self.assertEqual(400, ctx.exception.status_code)
            self.assertIn("Incompatible provider bindings", str(ctx.exception.detail))
            self.assertIn("reviewer=openai-gateway(openai_compatible)", str(ctx.exception.detail))

    def test_clearing_default_provider_key_clears_legacy_secret(self):
        # Env isolation (audit KI-1): _effective_*_api_key falls back to the
        # BLUEPRINT_DEEPSEEK_API_KEY env var as a legitimate config layer. That is
        # intended behavior, but it means this "after clear, no key remains" assertion
        # is only valid when the ambient env does not itself supply a key. Scrub the
        # provider env vars for the duration so the test reflects config state alone,
        # not whatever the developer happens to have exported. patch.dict restores
        # the original environment (including the popped vars) on exit.
        with patch.dict(os.environ, {}, clear=False):
            for var in ("BLUEPRINT_DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                os.environ.pop(var, None)
            with tempfile.TemporaryDirectory() as tmp_dir:
                settings = _make_settings(data_root=tmp_dir)
                service = AppConfigService(settings)
                service.update_settings({"deepseek_api_key": "sk-legacy"})

                public = service.update_settings({"clear_api_provider_keys": ["deepseek"]})

                deepseek = next(item for item in public["api_provider_profiles"] if item["provider_id"] == "deepseek")
                self.assertFalse(deepseek["api_key_configured"])
                self.assertIsNone(settings.deepseek_api_key)

    def test_manager_agent_config_selected_provider_id_reflects_binding(self):
        # Regression for the removed `or "deepseek"` mask (audit §3.1): selected_provider_id
        # must echo the actually-bound provider, never silently relabel to the default.
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir)
            service = AppConfigService(settings)
            service.update_settings(
                {
                    "api_provider_profiles": [
                        {
                            "provider_id": "deepseek",
                            "display_name": "DeepSeek",
                            "protocol": "anthropic_compatible",
                            "model": "deepseek-v4-flash",
                            "base_url": "https://api.deepseek.com/anthropic",
                        },
                        {
                            "provider_id": "anthropic-gateway",
                            "display_name": "Anthropic Gateway",
                            "protocol": "anthropic_compatible",
                            "model": "manager-model",
                            "base_url": "https://gateway.example.com/anthropic",
                            "native_base_url": "https://gateway.example.com",
                        },
                    ],
                    "api_provider_keys": {"anthropic-gateway": "sk-ant-gateway"},
                    "provider_bindings": {"manager": {"provider_id": "anthropic-gateway"}},
                }
            )

            cfg = service.manager_agent_config()

            self.assertEqual(cfg["selected_provider_id"], "anthropic-gateway")
            self.assertNotEqual(cfg["selected_provider_id"], "deepseek")

    def test_saved_provider_list_does_not_append_default_openai(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir)
            service = AppConfigService(settings)

            public = service.update_settings(
                {
                    "api_provider_profiles": [
                        {
                            "provider_id": "deepseek",
                            "display_name": "DeepSeek",
                            "protocol": "anthropic_compatible",
                            "model": "deepseek-v4-flash",
                            "base_url": "https://api.deepseek.com/anthropic",
                        }
                    ]
                }
            )

            provider_ids = [item["provider_id"] for item in public["api_provider_profiles"]]
            self.assertEqual(provider_ids, ["deepseek"])

    def test_provider_test_result_persists_until_profile_changes(self):
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, _size=None):
                return b"{}"

        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir)
            service = AppConfigService(settings)
            profile = {
                "provider_id": "deepseek",
                "display_name": "DeepSeek",
                "protocol": "anthropic_compatible",
                "model": "deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/anthropic",
                "native_base_url": "https://api.deepseek.com",
            }
            service.update_settings({"api_provider_profiles": [profile], "api_provider_keys": {"deepseek": "sk-test"}})

            with patch("app.services.app_config_service.request.urlopen", return_value=_Response()):
                result = service.test_api_provider(profile)

            self.assertTrue(result["ok"])
            public = service.get_public_settings()
            saved = public["api_provider_profiles"][0]["test_result"]
            self.assertTrue(saved["ok"])

            public = service.update_settings({"api_provider_profiles": [{**profile, "model": "deepseek-v4-pro"}]})
            self.assertIsNone(public["api_provider_profiles"][0]["test_result"])

    def test_save_executor_profile_preserves_default_profiles_on_first_write(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir)
            service = AppConfigService(settings)

            service.save_executor_profile(
                {
                    "profile_id": "pi-cli-native",
                    "display_name": "Pi Agent (local CLI login)",
                    "worker_type": "pi",
                    "auth_mode": AUTH_MODE_CLI_NATIVE,
                    "enabled": False,
                    "command": None,
                    "api_protocol": None,
                    "provider_id": None,
                    "model": None,
                    "base_url": None,
                    "credential_ref": None,
                    "permission_preset": "workspace_write",
                    "native_auth_readonly": True,
                }
            )

            profile_ids = [profile["profile_id"] for profile in service.list_executor_profiles()]
            self.assertIn("pi-project-api", profile_ids)
            self.assertIn("pi-cli-native", profile_ids)
            self.assertFalse(next(profile for profile in service.list_executor_profiles() if profile["profile_id"] == "pi-cli-native")["enabled"])

    def test_default_executor_profile_prefers_wrapper_for_pi_and_opencode(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir)
            service = AppConfigService(settings)

            pi_profile = service.resolve_executor_profile("pi")
            opencode_profile = service.resolve_executor_profile("opencode")
            codex_profile = service.resolve_executor_profile("codex")
            claude_profile = service.resolve_executor_profile("claude_code")

            self.assertEqual("project_api", pi_profile["auth_mode"])
            self.assertEqual("project_api", opencode_profile["auth_mode"])
            self.assertEqual("cli_native", codex_profile["auth_mode"])
            self.assertEqual("cli_native", claude_profile["auth_mode"])

    def test_update_settings_persists_worker_timeout_seconds(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = _make_settings(data_root=tmp_dir, worker_timeout_seconds=900, manifest_repair_timeout_seconds=180)
            service = AppConfigService(settings)

            public = service.update_settings({"worker_timeout_seconds": 1800, "manifest_repair_timeout_seconds": 360})

            self.assertEqual(1800, public["worker_timeout_seconds"])
            self.assertEqual(1800, settings.worker_timeout_seconds)
            self.assertEqual(360, public["manifest_repair_timeout_seconds"])
            self.assertEqual(360, settings.manifest_repair_timeout_seconds)
            saved = json.loads((Path(tmp_dir) / "_app_settings.json").read_text(encoding="utf-8"))
            self.assertEqual(1800, saved["worker_timeout_seconds"])
            self.assertEqual(360, saved["manifest_repair_timeout_seconds"])


if __name__ == "__main__":
    unittest.main()
