"""Regression tests for dependency-install result serialization.

These guard against a class of bug where a successful dependency install
produced a result dict that contained a non-JSON-serializable value (a
``pathlib.Path`` in ``resolved_runtime``). The result is persisted by
``RuntimeDependencyJobService._persist_project_jobs_locked`` via
``atomic_write_json``; an unserializable value made the persist raise
``TypeError``, which left the job stuck: in-memory it was ``succeeded`` but
on disk it stayed ``running``/``stale`` forever. The frontend therefore
never received a terminal event and never showed the install chip.

See the live incident: "Terminal drift detected ... memory_status=succeeded
disk_status=stale" repeating every 30s in the backend journal.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.manager_blueprint_tools import ManagerBlueprintTools
from app.services.project_service import ProjectService
from app.services.utils import atomic_write_json


def _make_tools(project_path: Path) -> ManagerBlueprintTools:
    """Build a ManagerBlueprintTools without running its heavy __init__.

    We only exercise the install helpers, which only touch
    ``project_service`` and the module-level ``subprocess``/worker imports.
    """
    tools = ManagerBlueprintTools.__new__(ManagerBlueprintTools)
    project_service = MagicMock(spec=ProjectService)
    project_service.project_path.return_value = project_path
    project_service.settings = SimpleNamespace()
    tools.project_service = project_service
    return tools


class _FakeCompletedProcess:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CondaInstallSerializationTest(unittest.TestCase):
    """The conda installer_plan branch must emit a JSON-serializable result."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-dep-ser-")
        self.project_path = Path(self.tmpdir)
        self.tools = _make_tools(self.project_path)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _patched_resolve_runtime_and_solver(self):
        """Patch the runtime/solver resolver to return Path objects.

        This mirrors what _resolve_runtime_and_solver returns in production
        (tuple[Path, Path]) and is what originally triggered the bug.
        """

        env_path = Path("/fake/conda/envs/R_env")
        conda_bin = Path("/fake/conda/bin/mamba")
        return patch.object(
            self.tools,
            "_resolve_runtime_and_solver",
            return_value=(env_path, conda_bin),
        )

    def test_conda_plan_success_result_is_json_serializable(self):
        """A successful conda install must persist without TypeError."""
        with (
            self._patched_resolve_runtime_and_solver(),
            patch(
                "app.services.manager_blueprint_tools.subprocess.run",
                return_value=_FakeCompletedProcess(
                    returncode=0,
                    stdout="Preparing transaction: done\n",
                    stderr="",
                ),
            ),
        ):
            result = self.tools._install_from_plan(
                "test-project",
                ecosystem="R",
                runtime="R_env",
                packages=["complexheatmap"],
                installer_plan=[
                    {
                        "kind": "install",
                        "installer": "conda",
                        "name": "complexheatmap",
                        "candidate": "bioconductor-complexheatmap",
                    }
                ],
                timeout=600,
                started_at="2026-06-16T00:00:00Z",
                channels=[],
            )

        # The bug: resolved_runtime was a Path and broke json.dump.
        self.assertIsInstance(result["resolved_runtime"], str)
        self.assertTrue(result["ok"])
        # The exact contract the persistence layer depends on.
        json.dumps(result)  # must not raise TypeError

    def test_conda_plan_resolved_runtime_is_str_even_if_resolver_returns_path(self):
        """Defensive: even if a Path leaks through, result must serialize.

        This documents the two-layer defense: the conda branch coerces to
        str, and atomic_write_json uses default=str as a backstop.
        """
        with (
            self._patched_resolve_runtime_and_solver(),
            patch(
                "app.services.manager_blueprint_tools.subprocess.run",
                return_value=_FakeCompletedProcess(
                    returncode=0, stdout="", stderr=""
                ),
            ),
        ):
            result = self.tools._install_from_plan(
                "test-project",
                ecosystem="python",
                runtime="py_env",
                packages=["numpy"],
                installer_plan=[
                    {"kind": "install", "installer": "conda", "name": "numpy"}
                ],
                timeout=600,
                started_at="2026-06-16T00:00:00Z",
                channels=[],
            )

        self.assertIsInstance(result["resolved_runtime"], str)
        self.assertEqual(result["resolved_runtime"], "/fake/conda/envs/R_env")


class AtomicWriteJsonDefaultStrTest(unittest.TestCase):
    """atomic_write_json must never raise on a stray Path/datetime value."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-awj-")
        self.path = Path(self.tmpdir) / "state.json"

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_path_value_is_serialized_via_default_str(self):
        payload = {"resolved_runtime": Path("/some/env")}
        atomic_write_json(self.path, payload)
        # The file should contain the stringified path.
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["resolved_runtime"], "/some/env")

    def test_mixed_native_and_path_payload_roundtrips(self):
        from datetime import datetime, timezone

        payload = {
            "ok": True,
            "env": Path("/x"),
            "ts": datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc),
            "nested": {"p": Path("/y"), "list": [Path("/z")]},
        }
        atomic_write_json(self.path, payload)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["env"], "/x")
        self.assertEqual(data["nested"]["p"], "/y")
        self.assertEqual(data["nested"]["list"], ["/z"])


if __name__ == "__main__":
    unittest.main()
