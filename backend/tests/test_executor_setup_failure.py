"""Tests for agent_cli_executor._try_render_provider setup-failure handling (#5 / audit §4.2).

Pre-launch setup errors must NOT be swallowed into a fabricated profile or a
stdout-only print that silently degrades to the legacy template launch. Instead
they must write a structured terminal failure (synthetic_failure) so the backend
surfaces the *specific* cause instead of a generic "executor exit code N":

- An explicitly-requested profile that fails to load -> _SetupFailure (no fabrication).
- A failed settings load -> _SetupFailure.
- A renderer that raises -> _SetupFailure (no degrade-to-template).

A genuinely-empty profile lookup (no stored profile, no matching builtin default,
and no error) still falls back to a minimal fabricated spec -- the legitimate
cli_native path -- and writes NO terminal failure.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.models.runs import ExecutorFailureReport, TerminalReport
from app.workers import agent_cli_executor as ace


def _renderer_registry(renderer):
    registry = MagicMock()
    registry.get.return_value = renderer
    return MagicMock(return_value=registry)


class TryRenderProviderSetupFailureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        self.project_root = self.run_dir / "project"
        self.project_root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _call(self, *, provider="codex", auth_mode="cli_native", profile_id=None, packet=None):
        return ace._try_render_provider(
            provider=provider,
            auth_mode=auth_mode,
            profile_id=profile_id,
            packet=packet if packet is not None else {"task_id": "run-xyz"},
            run_dir=self.run_dir,
            project_root=self.project_root,
        )

    def _read_failure(self):
        terminal = TerminalReport.model_validate_json(
            (self.run_dir / "terminal_report.json").read_text(encoding="utf-8")
        )
        failure = ExecutorFailureReport.model_validate_json(
            (self.run_dir / "executor_failure.json").read_text(encoding="utf-8")
        )
        return terminal, failure

    def _assert_no_terminal_report(self):
        self.assertFalse((self.run_dir / "terminal_report.json").exists())
        self.assertFalse((self.run_dir / "executor_failure.json").exists())

    # --- Q2: explicitly-requested profile that fails to load is fatal, no fabrication ---
    def test_explicit_profile_load_error_writes_structured_failure_no_fabrication(self):
        renderer = MagicMock()
        config_service = MagicMock()
        config_service.resolve_executor_profile.side_effect = OSError("corrupt profile store")

        with patch.object(ace, "_write_setup_failure", wraps=ace._write_setup_failure) as spy, \
             patch("app.workers.provider_renderers.get_renderer_registry", _renderer_registry(renderer)), \
             patch("app.services.app_config_service.AppConfigService", return_value=config_service), \
             patch("app.core.config.get_settings", MagicMock(return_value=MagicMock())):
            with self.assertRaises(ace._SetupFailure):
                self._call(profile_id="my-profile")

        # Renderer was never invoked: we refused to render over a broken profile.
        renderer.render.assert_not_called()
        spy.assert_called_once()
        terminal, failure = self._read_failure()
        self.assertEqual(terminal.terminal_kind, "synthetic_failure")
        self.assertEqual(terminal.reason_code, "execution_error")
        self.assertEqual(failure.reason_code, "execution_error")
        self.assertIn("my-profile", failure.summary)
        self.assertIn("corrupt profile store", failure.summary)
        self.assertEqual(failure.details.get("phase"), "profile_resolution")
        self.assertEqual(terminal.run_id, "run-xyz")

    # --- Q2 boundary: no explicit profile_id -> load error is NOT fatal, falls back ---
    def test_no_profile_id_load_error_falls_back_to_fabrication(self):
        renderer = MagicMock()
        render_result = MagicMock()
        renderer.render.return_value = render_result
        config_service = MagicMock()
        config_service.resolve_executor_profile.side_effect = OSError("transient read")

        with patch("app.workers.provider_renderers.get_renderer_registry", _renderer_registry(renderer)), \
             patch("app.services.app_config_service.AppConfigService", return_value=config_service), \
             patch("app.core.config.get_settings", MagicMock(return_value=MagicMock())), \
             patch("app.models.executor_profiles.default_profiles", return_value=[]):
            result, ret_renderer, profile_spec, _settings = self._call(profile_id=None)

        # No structured failure: lookup error without an explicit profile is recoverable.
        self._assert_no_terminal_report()
        renderer.render.assert_called_once()
        self.assertIs(result, render_result)
        self.assertIs(ret_renderer, renderer)
        # Fabricated minimal spec from provider + auth_mode.
        self.assertEqual(profile_spec.worker_type, "codex")
        self.assertEqual(profile_spec.auth_mode, "cli_native")

    # --- Q1: renderer.render() raising is fatal, no silent degrade-to-template ---
    def test_renderer_failure_writes_structured_failure(self):
        renderer = MagicMock()
        renderer.render.side_effect = RuntimeError("renderer blew up")
        config_service = MagicMock()
        config_service.resolve_executor_profile.return_value = None

        with patch("app.workers.provider_renderers.get_renderer_registry", _renderer_registry(renderer)), \
             patch("app.services.app_config_service.AppConfigService", return_value=config_service), \
             patch("app.core.config.get_settings", MagicMock(return_value=MagicMock())), \
             patch("app.models.executor_profiles.default_profiles", return_value=[]):
            with self.assertRaises(ace._SetupFailure):
                self._call(profile_id=None)

        terminal, failure = self._read_failure()
        self.assertEqual(terminal.terminal_kind, "synthetic_failure")
        self.assertEqual(failure.reason_code, "execution_error")
        self.assertIn("renderer blew up", failure.summary)
        self.assertEqual(failure.details.get("phase"), "render")

    # --- settings load failure is fatal ---
    def test_settings_load_failure_writes_structured_failure(self):
        renderer = MagicMock()
        config_service = MagicMock()
        config_service.resolve_executor_profile.return_value = None

        with patch("app.workers.provider_renderers.get_renderer_registry", _renderer_registry(renderer)), \
             patch("app.services.app_config_service.AppConfigService", return_value=config_service), \
             patch("app.core.config.get_settings", MagicMock(side_effect=RuntimeError("settings exploded"))), \
             patch("app.models.executor_profiles.default_profiles", return_value=[]):
            with self.assertRaises(ace._SetupFailure):
                self._call(profile_id=None)

        renderer.render.assert_not_called()
        terminal, failure = self._read_failure()
        self.assertEqual(failure.details.get("phase"), "settings_load")
        self.assertIn("settings exploded", failure.summary)

    # --- genuine empty (no stored, no default, no error) still fabricates, no failure ---
    def test_genuinely_empty_fabricates_and_renders(self):
        renderer = MagicMock()
        render_result = MagicMock()
        renderer.render.return_value = render_result
        config_service = MagicMock()
        config_service.resolve_executor_profile.return_value = None  # not configured (legit)

        with patch("app.workers.provider_renderers.get_renderer_registry", _renderer_registry(renderer)), \
             patch("app.services.app_config_service.AppConfigService", return_value=config_service), \
             patch("app.core.config.get_settings", MagicMock(return_value=MagicMock())), \
             patch("app.models.executor_profiles.default_profiles", return_value=[]):
            result, _renderer, profile_spec, _settings = self._call(provider="opencode", auth_mode="cli_native", profile_id=None)

        self._assert_no_terminal_report()
        self.assertIs(result, render_result)
        self.assertEqual(profile_spec.profile_id, "opencode-cli_native")
        render_result.write_provider_config_plan.assert_called_once()

    # --- no renderer registered -> graceful (None,...) degrade, no failure file ---
    def test_no_renderer_returns_none_without_failure(self):
        with patch("app.workers.provider_renderers.get_renderer_registry", _renderer_registry(None)):
            result = self._call(provider="codex", auth_mode="cli_native")
        self.assertEqual(result, (None, None, None, None))
        self._assert_no_terminal_report()


if __name__ == "__main__":
    unittest.main()
