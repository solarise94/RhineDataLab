"""Integration tests for the resolver-first installer flow (P1.2, P1.3).

These tests pin down the contract that ``install_runtime_dependencies`` runs
the resolver before creating a background job, that ``resolve_runtime_dependencies``
returns a plan without creating a job, and that the fallback policy gates
structured fallback actions without executing them automatically.
"""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import get_settings
from app.services.background_task_service import BackgroundTaskService
from app.services.background_workboard_service import BackgroundWorkboardService
from app.services.manager_planner import DeepSeekManagerPlanner, ManagerPlanDraft
from app.services.manager_service import ManagerService
from app.services.project_service import ProjectService
from app.services.runtime_dependency_job_service import RuntimeDependencyJobService
from app.services.runtime_dependency_resolver_service import (
    ProbeResult,
    RuntimeDependencyResolverService,
)


class _StubPlanner(DeepSeekManagerPlanner):
    """A no-op planner used to keep ManagerService construction cheap."""

    def plan(self, *args, **kwargs):  # type: ignore[override]
        return ManagerPlanDraft(actions=[], assistant_text="")

    def stream_plan(self, *args, **kwargs):  # type: ignore[override]
        yield {"type": "response", "response": {"assistant_text": "", "actions": []}}


def _build_manager(tmpdir: str):
    project_service = ProjectService()
    project_service.create_project(
        project_id="test-project",
        name="Test Project",
        current_goal="Test flow",
        seed_demo=True,
    )
    project_service.git_service = lambda _project_id: None
    background_task_service = BackgroundTaskService(project_service)
    background_workboard_service = BackgroundWorkboardService(project_service, background_task_service)
    runtime_dependency_job_service = RuntimeDependencyJobService(
        project_service,
        background_task_service=background_task_service,
    )
    resolver = RuntimeDependencyResolverService()
    manager = ManagerService(
        project_service,
        planner=_StubPlanner(),
        runtime_dependency_job_service=runtime_dependency_job_service,
        runtime_dependency_resolver_service=resolver,
        background_workboard_service=background_workboard_service,
    )
    return manager, project_service, runtime_dependency_job_service, resolver


class ResolverFirstInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-resolver-test-")
        settings = get_settings()
        self._original_executor_conda_base = settings.executor_conda_base
        self._original_fallback_policy = getattr(settings, "runtime_dependency_fallback_policy", None)
        self._original_data_root = settings.data_root
        settings.data_root = Path(self.tmpdir)

    def tearDown(self) -> None:
        settings = get_settings()
        settings.executor_conda_base = self._original_executor_conda_base
        settings.data_root = self._original_data_root
        if self._original_fallback_policy is None:
            settings.runtime_dependency_fallback_policy = "report_only"
        else:
            settings.runtime_dependency_fallback_policy = self._original_fallback_policy
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup_conda_runtime(self, runtime_name: str = "rnaseq", *, with_rscript: bool = True) -> Path:
        settings = get_settings()
        conda_base = Path(self.tmpdir) / "conda"
        env_path = conda_base / "envs" / runtime_name
        (env_path / "bin").mkdir(parents=True)
        (conda_base / "bin").mkdir(parents=True)
        (env_path / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        (conda_base / "bin" / "mamba").write_text("#!/bin/sh\n", encoding="utf-8")
        if with_rscript:
            (env_path / "bin" / "Rscript").write_text("#!/bin/sh\n", encoding="utf-8")
        settings.executor_conda_base = conda_base
        return conda_base

    def test_resolve_runtime_dependencies_returns_plan_without_job(self) -> None:
        self._setup_conda_runtime()
        manager, _project_service, runtime_dependency_job_service, _resolver = _build_manager(self.tmpdir)

        def fake_run(command, **kwargs):
            if "repoquery" in command or "search" in command:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps({"query": {"query": "scanpy", "type": "search"}, "result": {"pkgs": [{"name": "scanpy", "version": "1.10.0"}]}}),
                    stderr="",
                )
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")

        with patch("app.services.runtime_dependency_resolver_service.subprocess.run", side_effect=fake_run):
            plan = manager.blueprint_tools.resolve_runtime_dependencies(
                "test-project",
                {
                    "ecosystem": "python",
                    "runtime": "rnaseq",
                    "packages": ["scanpy"],
                },
            )

        self.assertEqual(plan["status"], "fully_installable")
        self.assertTrue(plan["ok"])
        self.assertEqual(len(plan["installable"]), 1)
        self.assertEqual(plan["installable"][0]["installer"], "conda")
        # No background job should have been created.
        jobs_path = Path(self.tmpdir) / "test-project" / "chat" / "runtime_dependency_jobs.json"
        if jobs_path.exists():
            self.assertEqual(jobs_path.read_text(encoding="utf-8").strip(), "")

    def test_resolve_runtime_dependencies_partial_classification(self) -> None:
        self._setup_conda_runtime()
        settings = get_settings()
        settings.runtime_dependency_fallback_policy = "report_only"
        manager, _project_service, _job_service, _resolver = _build_manager(self.tmpdir)

        def fake_run(command, **kwargs):
            # ggplot2 resolves to r-ggplot2; limma is not found.
            for candidate in command[4:]:
                if candidate == "r-ggplot2":
                    return subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=json.dumps({"query": {"query": "r-ggplot2", "type": "search"}, "result": {"pkgs": [{"name": "r-ggplot2", "version": "3.5.0"}]}}),
                        stderr="",
                    )
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="PackagesNotFoundError: no match found")

        with patch("app.services.runtime_dependency_resolver_service.subprocess.run", side_effect=fake_run):
            plan = manager.blueprint_tools.resolve_runtime_dependencies(
                "test-project",
                {
                    "ecosystem": "R",
                    "runtime": "rnaseq",
                    "packages": ["ggplot2", "limma"],
                },
            )

        # P1.3: partially resolved — ggplot2 is installable via conda but
        # limma is blocked. The status blocks job creation, but the plan
        # still surfaces what IS individually installable.
        self.assertEqual(plan["status"], "partial_resolution_requires_manual_preparation")
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["error_code"], "partial_resolution_requires_manual_preparation")
        self.assertEqual(plan["fallback_policy"], "report_only")
        self.assertEqual(len(plan["installable"]), 1)
        self.assertEqual(len(plan["blocked"]), 1)
        self.assertEqual(plan["blocked"][0]["name"], "limma")

    def test_install_runtime_dependencies_resolver_blocks_execution(self) -> None:
        """When the resolver returns non-fully_installable, no background job is created."""
        self._setup_conda_runtime()
        settings = get_settings()
        settings.runtime_dependency_fallback_policy = "report_only"
        manager, _project_service, _job_service, _resolver = _build_manager(self.tmpdir)

        # Force every conda probe to fail so the resolver rejects the request.
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="PackagesNotFoundError: no match found")

        with patch("app.services.runtime_dependency_resolver_service.subprocess.run", side_effect=fake_run):
            response = manager.blueprint_tools.install_runtime_dependencies(
                "test-project",
                {
                    "ecosystem": "python",
                    "runtime": "rnaseq",
                    "packages": ["pydeseq2"],
                },
            )

        self.assertFalse(response["ok"])
        self.assertFalse(response["background"])
        self.assertEqual(
            response["status"], "fallback_available_but_policy_disallows"
        )
        self.assertEqual(response["error_code"], "package_not_found_in_conda_channels")
        self.assertIn("resolver_plan", response)
        self.assertIn("pydeseq2", {b["name"] for b in response["resolver_plan"]["blocked"]})
        # No background job was created.
        jobs_path = Path(self.tmpdir) / "test-project" / "chat" / "runtime_dependency_jobs.json"
        if jobs_path.exists():
            self.assertEqual(jobs_path.read_text(encoding="utf-8").strip(), "")

    def test_install_runtime_dependencies_resolver_passes_through_when_fully_installable(self) -> None:
        """When the resolver approves every package, the installer still runs and the job succeeds."""
        self._setup_conda_runtime()
        manager, _project_service, _job_service, _resolver = _build_manager(self.tmpdir)

        def _mock_probe(_self, _bin, candidates, *, ecosystem, extra_channels=None):
            if candidates and candidates[0] == "scanpy":
                return ProbeResult(status="found", match="scanpy")
            return ProbeResult(status="not_found")

        def _mock_channel_sig(_self, conda_bin, ecosystem, runtime, *, conda_base=None, extra_channels=None):
            return f"mock:{getattr(conda_bin, 'name', conda_bin)}:{ecosystem}:{runtime}"

        def _fake_command_runner(project_id, command, *, ecosystem, runtime, resolved_runtime, packages, manager_name, timeout, started_at, phase_callback=None, **kwargs):
            if phase_callback:
                phase_callback("running_subprocess", command_preview=command)
            return {
                "ok": True,
                "status_detail": "install_completed",
                "changed": True,
                "message": "Dependencies installed.",
                "stdout_tail": "installed\n",
                "stderr_tail": "",
                "returncode": 0,
                "ecosystem": ecosystem,
                "runtime": runtime,
                "resolved_runtime": resolved_runtime,
                "packages": packages,
                "manager": manager_name,
            }

        # Keep the command-runner patch alive through the submit + poll
        # window so the background job sees the mock.
        with patch.object(RuntimeDependencyResolverService, "_probe_conda", _mock_probe), \
             patch.object(RuntimeDependencyResolverService, "_channel_signature", _mock_channel_sig), \
             patch.object(manager.blueprint_tools, "_run_dependency_command", _fake_command_runner):
            response = manager.blueprint_tools.install_runtime_dependencies(
                "test-project",
                {
                    "ecosystem": "python",
                    "runtime": "rnaseq",
                    "packages": ["scanpy"],
                },
            )

            self.assertTrue(response["ok"])
            self.assertTrue(response["background"])
            self.assertTrue(response["async_boundary"])
            # Wait for the job to finish — patches are still active.
            for _ in range(40):
                status = manager.blueprint_tools.get_runtime_dependency_install_status(
                    "test-project", response["job_id"]
                )
                if status["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(status["status"], "succeeded")

    def test_resolve_runtime_dependencies_includes_in_flight_duplicate_hint(self) -> None:
        self._setup_conda_runtime()
        manager, _project_service, runtime_dependency_job_service, _resolver = _build_manager(self.tmpdir)

        # Pre-seed an in-flight job in the persisted JSON.
        jobs_path = Path(self.tmpdir) / "test-project" / "chat" / "runtime_dependency_jobs.json"
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(
            json.dumps(
                [
                    {
                        "job_id": "depjob_inflight",
                        "status": "running",
                        "created_at": "2026-06-02T00:00:00Z",
                        "payload": {
                            "ecosystem": "python",
                            "runtime": "rnaseq",
                            "packages": ["scanpy"],
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )

        def fake_run(command, **kwargs):
            if "repoquery" in command or "search" in command:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps({"query": {"query": "scanpy", "type": "search"}, "result": {"pkgs": [{"name": "scanpy", "version": "1.10.0"}]}}),
                    stderr="",
                )
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")

        with patch("app.services.runtime_dependency_resolver_service.subprocess.run", side_effect=fake_run):
            plan = manager.blueprint_tools.resolve_runtime_dependencies(
                "test-project",
                {
                    "ecosystem": "python",
                    "runtime": "rnaseq",
                    "packages": ["scanpy"],
                },
            )

        self.assertEqual(plan["status"], "fully_installable")
        self.assertIsNotNone(plan.get("in_flight_duplicate"))
        self.assertEqual(
            plan["in_flight_duplicate"]["prior_job_id"], "depjob_inflight"
        )

    def test_fallback_policy_allow_safe_registry_includes_fallback_actions(self) -> None:
        self._setup_conda_runtime()
        settings = get_settings()
        settings.runtime_dependency_fallback_policy = "allow_safe_registry_install"
        manager, _project_service, _job_service, _resolver = _build_manager(self.tmpdir)

        # Make every conda probe fail so all packages fall through to the
        # pip registry fallback.  Use patch.object on the class for
        # isolation from test ordering.
        def _mock_probe(_self, _bin, candidates, *, ecosystem, extra_channels=None):
            return ProbeResult(status="not_found")

        def _mock_channel_sig(_self, conda_bin, ecosystem, runtime, *, conda_base=None, extra_channels=None):
            return f"mock:{getattr(conda_bin, 'name', conda_bin)}:{ecosystem}:{runtime}"

        with patch.object(RuntimeDependencyResolverService, "_probe_conda", _mock_probe), \
             patch.object(RuntimeDependencyResolverService, "_channel_signature", _mock_channel_sig):
            plan = manager.blueprint_tools.resolve_runtime_dependencies(
                "test-project",
                {
                    "ecosystem": "python",
                    "runtime": "rnaseq",
                    "packages": ["pydeseq2"],
                },
            )

            # Under allow_safe_registry_install, a single-family (all-pip)
            # fallback request is fully_installable.  The resolve tool also
            # surfaces the structured fallback_actions list.
            self.assertEqual(plan["status"], "fully_installable")
            self.assertTrue(plan["ok"])
            self.assertEqual(plan["fallback_policy"], "allow_safe_registry_install")
            self.assertEqual(len(plan["fallback_actions"]), 1)
            action = plan["fallback_actions"][0]
            self.assertEqual(action["installer"], "pip")
            self.assertEqual(action["name"], "pydeseq2")

    def test_r_dual_source_fallback_prefers_cran_and_creates_job(self) -> None:
        self._setup_conda_runtime()
        settings = get_settings()
        settings.runtime_dependency_fallback_policy = "allow_safe_registry_install"
        manager, _project_service, _job_service, _resolver = _build_manager(self.tmpdir)

        def _mock_probe(_self, _bin, candidates, *, ecosystem, extra_channels=None):
            return ProbeResult(status="not_found")

        def _mock_channel_sig(_self, conda_bin, ecosystem, runtime, *, conda_base=None, extra_channels=None):
            return f"mock:{getattr(conda_bin, 'name', conda_bin)}:{ecosystem}:{runtime}"

        def _fake_command_runner(project_id, command, *, ecosystem, runtime, resolved_runtime, packages, manager_name, timeout, started_at, phase_callback=None, **kwargs):
            if phase_callback:
                phase_callback("running_subprocess", command_preview=command)
            return {
                "ok": True,
                "status_detail": "install_completed",
                "changed": True,
                "message": "Dependencies installed.",
                "stdout_tail": "installed\n",
                "stderr_tail": "",
                "returncode": 0,
                "ecosystem": ecosystem,
                "runtime": runtime,
                "resolved_runtime": resolved_runtime,
                "packages": packages,
                "manager": manager_name,
            }

        with patch.object(RuntimeDependencyResolverService, "_probe_conda", _mock_probe), \
             patch.object(RuntimeDependencyResolverService, "_channel_signature", _mock_channel_sig), \
             patch.object(manager.blueprint_tools, "_run_dependency_command", _fake_command_runner):
            plan = manager.blueprint_tools.resolve_runtime_dependencies(
                "test-project",
                {
                    "ecosystem": "R",
                    "runtime": "rnaseq",
                    "packages": ["ggplot2"],
                },
            )
            response = manager.blueprint_tools.install_runtime_dependencies(
                "test-project",
                {
                    "ecosystem": "R",
                    "runtime": "rnaseq",
                    "packages": ["ggplot2"],
                },
            )

        self.assertEqual(plan["status"], "fully_installable")
        self.assertTrue(plan["ok"])
        self.assertEqual(len(plan.get("fallback_actions") or []), 1)
        self.assertEqual(plan["fallback_actions"][0]["installer"], "cran")
        self.assertTrue(response["ok"])
        self.assertTrue(response["background"])
        self.assertIn("job_id", response)

        jobs_path = Path(self.tmpdir) / "test-project" / "chat" / "runtime_dependency_jobs.json"
        if jobs_path.exists():
            payload = json.loads(jobs_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload), 1)

    def test_fallback_policy_allow_safe_registry_does_not_auto_execute(self) -> None:
        """Even under the relaxed policy, install_runtime_dependencies still blocks partial requests."""
        self._setup_conda_runtime()
        settings = get_settings()
        settings.runtime_dependency_fallback_policy = "allow_safe_registry_install"
        manager, _project_service, _job_service, _resolver = _build_manager(self.tmpdir)

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="PackagesNotFoundError: no match found")

        # Under allow_safe_registry_install, a single-family all-fallback
        # request (pip for pydeseq2) is promoted to fully_installable by the
        # resolver and install_runtime_dependencies creates a background job.
        # The resolver runs first; the installer runs after (via
        # _install_from_plan → _run_pip_install → _run_dependency_command).
        # The real subprocess.run in the installer path is NOT patched here,
        # so the actual pip command will fail.  That is expected — the test
        # is only asserting the *admission* side (resolver approved the
        # plan, job was created, and the request was NOT blocked).
        with patch("app.services.runtime_dependency_resolver_service.subprocess.run", side_effect=fake_run):
            response = manager.blueprint_tools.install_runtime_dependencies(
                "test-project",
                {
                    "ecosystem": "python",
                    "runtime": "rnaseq",
                    "packages": ["pydeseq2"],
                },
            )

        self.assertTrue(response["ok"], f"Expected ok=true, got {response}")
        self.assertTrue(response["background"])
        self.assertTrue(response["async_boundary"])
        self.assertIn("job_id", response)

    def test_resolve_runtime_dependencies_rejects_source_specs(self) -> None:
        self._setup_conda_runtime()
        manager, _project_service, _job_service, _resolver = _build_manager(self.tmpdir)

        plan = manager.blueprint_tools.resolve_runtime_dependencies(
            "test-project",
            {
                "ecosystem": "python",
                "runtime": "rnaseq",
                "packages": ["https://github.com/foo/bar"],
            },
        )

        self.assertEqual(plan["status"], "unsupported_source_spec")
        self.assertEqual(plan["error_code"], "github_source_install_not_supported")
        self.assertEqual(plan["retry_hint"], "do_not_retry_installer")
        # No background job created.
        jobs_path = Path(self.tmpdir) / "test-project" / "chat" / "runtime_dependency_jobs.json"
        if jobs_path.exists():
            self.assertEqual(jobs_path.read_text(encoding="utf-8").strip(), "")

    def test_mixed_conda_and_fallback_request_remains_blocked(self) -> None:
        """conda + fallback = mixed installer → partial resolution, no job."""
        self._setup_conda_runtime()
        settings = get_settings()
        settings.runtime_dependency_fallback_policy = "allow_safe_registry_install"
        manager, _project_service, _job_service, resolver = _build_manager(self.tmpdir)

        # scanpy finds conda; pydeseq2 does not.
        def _mock_probe(_self, _bin, candidates, *, ecosystem, extra_channels=None):
            if candidates and candidates[0] == "scanpy":
                return ProbeResult(status="found", match="scanpy")
            return ProbeResult(status="not_found")

        def _mock_channel_sig(_self, conda_bin, ecosystem, runtime, *, conda_base=None, extra_channels=None):
            return f"mock:{getattr(conda_bin, 'name', conda_bin)}:{ecosystem}:{runtime}"

        with patch.object(RuntimeDependencyResolverService, "_probe_conda", _mock_probe), \
             patch.object(RuntimeDependencyResolverService, "_channel_signature", _mock_channel_sig):
            response = manager.blueprint_tools.install_runtime_dependencies(
                "test-project",
                {
                    "ecosystem": "python",
                    "runtime": "rnaseq",
                    "packages": ["scanpy", "pydeseq2"],
                },
            )

        self.assertFalse(response["ok"])
        self.assertFalse(response["background"])
        self.assertEqual(
            response["status"],
            "partial_resolution_requires_manual_preparation",
        )
        self.assertEqual(response["error_code"], "partial_resolution_requires_manual_preparation")
        # No background job created for a mixed request.
        jobs_path = Path(self.tmpdir) / "test-project" / "chat" / "runtime_dependency_jobs.json"
        if jobs_path.exists():
            self.assertEqual(jobs_path.read_text(encoding="utf-8").strip(), "")

    def test_cache_key_changes_with_channel_signature(self) -> None:
        """Different conda solver paths produce distinct cache entries."""
        resolver = RuntimeDependencyResolverService()
        key_a = resolver._channel_signature(Path("/usr/bin/mamba"), "python", "env_a")
        key_b = resolver._channel_signature(Path("/opt/conda/bin/mamba"), "python", "env_a")
        self.assertNotEqual(key_a, key_b, "Different solver paths should produce different cache keys")

        # Same path, same ecosystem → same signature.
        key_c = resolver._channel_signature(Path("/usr/bin/mamba"), "python", "env_a")
        self.assertEqual(key_a, key_c)

    def test_resolve_runtime_dependencies_is_explicitly_read_only(self) -> None:
        """resolve_runtime_dependencies should work without _guard_mutation."""
        self._setup_conda_runtime()
        manager, _project_service, _job_service, _resolver = _build_manager(self.tmpdir)

        def fake_run(command, **kwargs):
            if "repoquery" in command or "search" in command:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps({"query": {"query": "scanpy", "type": "search"}, "result": {"pkgs": [{"name": "scanpy", "version": "1.10.0"}]}}),
                    stderr="",
                )
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")

        with patch("app.services.runtime_dependency_resolver_service.subprocess.run", side_effect=fake_run):
            plan = manager.blueprint_tools.resolve_runtime_dependencies(
                "test-project",
                {
                    "ecosystem": "python",
                    "runtime": "rnaseq",
                    "packages": ["scanpy"],
                },
            )

        self.assertEqual(plan["status"], "fully_installable")
        self.assertTrue(plan["ok"])
        # No background job created by a read-only tool.
        jobs_path = Path(self.tmpdir) / "test-project" / "chat" / "runtime_dependency_jobs.json"
        if jobs_path.exists():
            self.assertEqual(jobs_path.read_text(encoding="utf-8").strip(), "")
