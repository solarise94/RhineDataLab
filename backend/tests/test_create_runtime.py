"""Tests for the create_runtime manager tool."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.manager_blueprint_tools import ManagerBlueprintTools
from app.services.project_service import ProjectService


def _make_tools(project_path: Path, *, mamba_root: str | None = None) -> ManagerBlueprintTools:
    """Build a ManagerBlueprintTools exercising only create_runtime paths."""
    tools = ManagerBlueprintTools.__new__(ManagerBlueprintTools)
    project_service = MagicMock(spec=ProjectService)
    project_service.project_path.return_value = project_path
    project_service.settings = SimpleNamespace(
        executor_mamba_root_prefix=mamba_root,
    )
    tools.project_service = project_service
    tools.runtime_dependency_job_service = MagicMock()
    return tools


class CreateRuntimeValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-create-runtime-")
        self.project_path = Path(self.tmpdir)
        self.mamba_root = str(self.project_path / "mamba")
        self.tools = _make_tools(self.project_path, mamba_root=self.mamba_root)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_invalid_env_name_rejected(self):
        for bad_name in ["123bad", "has space", "a" * 65, "bad/name"]:
            with self.subTest(name=bad_name):
                result = self.tools.create_runtime("test-project", {
                    "ecosystem": "python",
                    "env_name": bad_name,
                })
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "invalid_runtime_name")

    def test_reserved_env_name_rejected(self):
        for reserved in ["base", "__system__", "blueprint-re-foo"]:
            with self.subTest(name=reserved):
                result = self.tools.create_runtime("test-project", {
                    "ecosystem": "python",
                    "env_name": reserved,
                })
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "reserved_runtime_name")

    def test_invalid_version_rejected(self):
        for bad_version in ["latest", ">=3.12", "3"]:
            with self.subTest(version=bad_version):
                result = self.tools.create_runtime("test-project", {
                    "ecosystem": "python",
                    "env_name": "py311",
                    "python_version": bad_version,
                })
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "invalid_runtime_version")

    def test_unsafe_package_rejected(self):
        for bad_pkg in ["numpy; rm -rf /", "github.com/foo/bar", "./local", "pkg/with/slash"]:
            with self.subTest(pkg=bad_pkg):
                result = self.tools.create_runtime("test-project", {
                    "ecosystem": "python",
                    "env_name": "pypackages",
                    "packages": [bad_pkg],
                })
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "unsupported_source_spec")

    def test_existing_runtime_rejected(self):
        env_path = Path(self.mamba_root) / "envs" / "existingenv"
        env_path.mkdir(parents=True)
        (env_path / "bin").mkdir()

        result = self.tools.create_runtime("test-project", {
            "ecosystem": "python",
            "env_name": "existingenv",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "runtime_already_exists")


class CreateRuntimeCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-create-runtime-")
        self.project_path = Path(self.tmpdir)
        self.mamba_root = str(self.project_path / "mamba")
        (Path(self.mamba_root) / "bin").mkdir(parents=True)
        self.micromamba = Path(self.mamba_root) / "bin" / "micromamba"
        self.micromamba.write_text("#!/bin/sh\necho micromamba")
        self.micromamba.chmod(0o755)
        self.tools = _make_tools(self.project_path, mamba_root=self.mamba_root)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_python_command_built_correctly(self):
        fake_job = MagicMock(task_id="bgtask_1", job_id="depjob_1", status="queued", created_at="2026-06-16T00:00:00Z")
        self.tools.runtime_dependency_job_service.submit.return_value = fake_job

        result = self.tools.create_runtime("test-project", {
            "ecosystem": "python",
            "env_name": "py311",
            "python_version": "3.11",
            "packages": ["numpy", "pandas"],
            "auto_select": True,
        })

        self.assertTrue(result["ok"])
        self.tools.runtime_dependency_job_service.submit.assert_called_once()
        call_args = self.tools.runtime_dependency_job_service.submit.call_args
        self.assertEqual(call_args.kwargs.get("task_type"), "runtime_dependency_create")
        payload = call_args.args[1]
        self.assertEqual(payload["ecosystem"], "python")
        self.assertEqual(payload["env_name"], "py311")
        self.assertEqual(payload["auto_select"], True)
        command = payload["command"]
        self.assertEqual(command[0], str(self.micromamba))
        self.assertIn("create", command)
        self.assertIn("-n", command)
        self.assertIn("py311", command)
        self.assertIn("-p", command)
        self.assertIn(str(Path(self.mamba_root) / "envs" / "py311"), command)
        self.assertIn("-c", command)
        self.assertIn("conda-forge", command)
        self.assertIn("bioconda", command)
        self.assertIn("python=3.11", command)
        self.assertIn("numpy", command)
        self.assertIn("pandas", command)

    def test_r_command_built_correctly(self):
        fake_job = MagicMock(task_id="bgtask_2", job_id="depjob_2", status="queued", created_at="2026-06-16T00:00:00Z")
        self.tools.runtime_dependency_job_service.submit.return_value = fake_job

        result = self.tools.create_runtime("test-project", {
            "ecosystem": "r",
            "env_name": "renv44",
            "r_version": "4.4",
        })

        self.assertTrue(result["ok"])
        payload = self.tools.runtime_dependency_job_service.submit.call_args.args[1]
        self.assertEqual(payload["ecosystem"], "r")
        self.assertIn("r-base=4.4", payload["command"])

    @patch("app.services.manager_blueprint_tools.shutil.which", return_value=None)
    def test_solver_not_found_returns_error(self, _which_mock):
        tools = _make_tools(self.project_path, mamba_root=str(self.project_path / "empty"))
        tools.runtime_dependency_job_service = MagicMock()

        result = tools.create_runtime("test-project", {
            "ecosystem": "python",
            "env_name": "py312",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "solver_not_found")


class CreateRuntimeSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-create-runtime-")
        self.project_path = Path(self.tmpdir)
        self.tools = _make_tools(self.project_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_auto_select_writes_runtime_preferences(self):
        self.tools.project_service.update_project_runtime_preferences = MagicMock()
        with patch.object(self.tools, "_run_dependency_command", return_value={"ok": True}):
            result = self.tools._create_runtime_sync("test-project", {
                "ecosystem": "python",
                "env_name": "py311",
                "env_path": str(self.project_path / "envs" / "py311"),
                "command": ["micromamba", "create", "-y", "-n", "py311"],
                "timeout_seconds": 1200,
                "auto_select": True,
                "mamba_root": str(self.project_path / "mamba"),
            })
        self.assertTrue(result["ok"])
        self.tools.project_service.update_project_runtime_preferences.assert_called_once_with(
            "test-project", {"python_runtime": "py311"}
        )

    def test_failure_does_not_auto_select(self):
        self.tools.project_service.update_project_runtime_preferences = MagicMock()
        with patch.object(self.tools, "_run_dependency_command", return_value={"ok": False, "error_code": "dependency_install_failed"}):
            result = self.tools._create_runtime_sync("test-project", {
                "ecosystem": "r",
                "env_name": "renv",
                "env_path": str(self.project_path / "envs" / "renv"),
                "command": ["micromamba", "create", "-y", "-n", "renv"],
                "timeout_seconds": 1200,
                "auto_select": True,
                "mamba_root": str(self.project_path / "mamba"),
            })
        self.assertFalse(result["ok"])
        self.tools.project_service.update_project_runtime_preferences.assert_not_called()


if __name__ == "__main__":
    unittest.main()
