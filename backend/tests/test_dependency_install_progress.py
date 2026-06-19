"""Tests for Layer F2 real-time dependency-install progress.

Covers the Popen line loop, progress-token parsing, live tail capture,
and reference-download byte callbacks.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.manager_blueprint_tools import ManagerBlueprintTools
from app.services.project_service import ProjectService
from app.services.reference_data_service import ReferenceDataService
from app.services.reference_data_service import ReferenceDataSourceSpec


def _make_tools(project_path: Path) -> ManagerBlueprintTools:
    """Build a ManagerBlueprintTools instance without its heavy __init__."""
    tools = ManagerBlueprintTools.__new__(ManagerBlueprintTools)
    project_service = MagicMock(spec=ProjectService)
    project_service.project_path.return_value = project_path
    project_service.settings = SimpleNamespace()
    tools.project_service = project_service
    tools.runtime_dependency_job_service = None
    return tools


class _FakePopen:
    """Configurable Popen stand-in for the Layer F2 line loop."""

    def __init__(self, command, *, stdout="", stderr="", returncode=0, **kwargs):
        self._stdout = io.StringIO(stdout)
        self._stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.pid = 12345

    @property
    def stdout(self):
        return self._stdout

    @property
    def stderr(self):
        return self._stderr

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def _fake_popen_factory(stdout="", stderr="", returncode=0):
    def _factory(command, **kwargs):
        return _FakePopen(command, stdout=stdout, stderr=stderr, returncode=returncode)
    return _factory


class PipProgressParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-dep-progress-")
        self.project_path = Path(self.tmpdir)
        self.tools = _make_tools(self.project_path)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _patched_resolve_conda_runtime(self):
        env_path = Path("/fake/conda/envs/py_env")
        python_bin = env_path / "bin" / "python"
        return patch.object(
            self.tools,
            "_python_dependency_command",
            return_value=([str(python_bin), "-m", "pip", "install", "numpy"], str(env_path)),
        )

    def test_pip_install_emits_progress_and_live_tails(self):
        """The Popen loop parses pip progress tokens and forwards them via phase_callback."""
        stdout_lines = [
            "Collecting numpy\n",
            "  Downloading numpy-2.2.6-cp313-cp313-manylinux_2_17_x86_64.whl (16.0 MB)\n",
            "     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 16.0/16.0 MB 5.2 MB/s eta 0:00:00\n",
            "Successfully installed numpy-2.2.6\n",
        ]
        calls = []

        def phase_callback(phase, **kwargs):
            calls.append((phase, dict(kwargs)))

        with (
            self._patched_resolve_conda_runtime(),
            patch(
                "app.services.manager_blueprint_tools.subprocess.Popen",
                _fake_popen_factory(stdout="".join(stdout_lines), returncode=0),
            ),
        ):
            result = self.tools._install_runtime_dependencies_sync(
                "test-project",
                {
                    "ecosystem": "python",
                    "runtime": "py_env",
                    "packages": ["numpy"],
                    "timeout_seconds": 600,
                },
                phase_callback=phase_callback,
                job_id="depjob_test",
            )

        self.assertTrue(result["ok"])
        phases = [phase for phase, _ in calls]
        self.assertIn("launching_subprocess", phases)
        self.assertIn("running_subprocess", phases)
        running_calls = [kw for phase, kw in calls if phase == "running_subprocess"]
        self.assertTrue(running_calls, "Should have at least one running_subprocess callback")
        # At least one running callback should carry parsed progress.
        progress_calls = [kw for kw in running_calls if kw.get("progress") == 100]
        self.assertTrue(progress_calls, "Should have a callback with 100% progress")
        final = progress_calls[-1]
        self.assertIn("16.0 MB", final.get("progress_label", ""))
        self.assertEqual(final.get("bytes_total"), 16 * 1024 * 1024)
        self.assertEqual(final.get("bytes_downloaded"), 16 * 1024 * 1024)
        self.assertEqual(final.get("download_rate_bps"), int(5.2 * 1024 * 1024))
        # Terminal result carries the full captured stdout tail.
        self.assertIn("Successfully installed", result["stdout_tail"])

    def test_progress_state_parser_handles_variants(self):
        """_update_progress_from_line extracts progress from pip/conda/R lines."""
        state: dict = {
            "progress": 0,
            "progress_label": None,
            "bytes_total": None,
            "bytes_downloaded": None,
            "download_rate_bps": None,
        }
        self.tools._update_progress_from_line(
            "Downloading numpy-2.0.tar.gz (1.5 MB)", state
        )
        self.assertEqual(state["progress_label"], "Downloading numpy-2.0.tar.gz (1.5 MB)")
        self.tools._update_progress_from_line(
            "     ━━━━━━━━━━━━━━━━━━━━━ 1.5/1.5 MB 10.5 MB/s eta 0:00:00", state
        )
        self.assertEqual(state["progress"], 100)
        self.assertEqual(state["bytes_total"], int(1.5 * 1024 * 1024))
        self.assertEqual(state["download_rate_bps"], int(10.5 * 1024 * 1024))

        # Conda transaction percentage
        state2: dict = {
            "progress": 0,
            "progress_label": None,
            "bytes_total": None,
            "bytes_downloaded": None,
            "download_rate_bps": None,
        }
        self.tools._update_progress_from_line(
            "Executing transaction: / 45%", state2
        )
        self.assertEqual(state2["progress"], 45)


class ReferenceDownloadProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-ref-progress-")
        self.settings = SimpleNamespace(
            data_root=self.tmpdir,
            reference_download_retries=2,
            reference_download_timeout_s=30,
            http_proxy="",
            https_proxy="",
            no_proxy="",
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fetch_and_register_reports_byte_progress(self):
        """ReferenceDataService.fetch_and_register invokes progress_callback with byte counters."""
        payload = b"hello reference data\n" * 500
        source_path = Path(self.tmpdir) / "source.bin"
        source_path.write_bytes(payload)
        sha256 = "fake"
        try:
            import hashlib
            sha256 = hashlib.sha256(payload).hexdigest()
        except Exception:
            pass

        source = ReferenceDataSourceSpec(
            url=f"file://{source_path}",
            sha256=sha256,
            size_hint=len(payload),
            filename="source.bin",
        )
        service = ReferenceDataService(self.settings)
        progress_calls = []

        def progress_callback(info):
            progress_calls.append(info)

        meta = service.fetch_and_register(source, progress_callback=progress_callback)
        self.assertEqual(meta.size, len(payload))
        self.assertTrue(progress_calls, "progress_callback should have been invoked")
        last = progress_calls[-1]
        self.assertEqual(last["bytes_downloaded"], len(payload))
        self.assertEqual(last["bytes_total"], len(payload))
        self.assertEqual(last["progress"], 100)


class PopenErrorHandlingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-dep-err-")
        self.project_path = Path(self.tmpdir)
        self.tools = _make_tools(self.project_path)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_popen_start_failure_returns_serializable_error(self):
        """OSError from Popen yields a JSON-serializable result dict."""
        def _failing_factory(command, **kwargs):
            raise OSError("No such file: fake-conda")

        with patch(
            "app.services.manager_blueprint_tools.subprocess.Popen",
            _failing_factory,
        ):
            result = self.tools._run_dependency_command(
                "test-project",
                ["fake-conda", "install", "numpy"],
                ecosystem="python",
                runtime="py_env",
                resolved_runtime="/fake/env",
                packages=["numpy"],
                manager_name="conda",
                timeout=600,
                started_at="2026-06-16T00:00:00Z",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "dependency_install_start_failed")
        json.dumps(result)


if __name__ == "__main__":
    unittest.main()
