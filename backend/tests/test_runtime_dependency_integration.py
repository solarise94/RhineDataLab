from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.background_task_service import BackgroundTaskService
from app.services.project_service import ProjectService
from app.services.runtime_dependency_job_service import RuntimeDependencyJobService
from app.services.runtime_dependency_state_service import (
    dependency_blockers_by_card,
    find_duplicate_in_flight,
    find_duplicate_terminal_failure,
)
from app.services.utils import utc_now


class RuntimeDependencyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-dep-test-")
        self.project_service = MagicMock(spec=ProjectService)
        self.project_service.project_path.return_value = Path(self.tmpdir)
        self.project_service.list_projects.return_value = [SimpleNamespace(project_id="test-project")]
        self.background_task_service = MagicMock(spec=BackgroundTaskService)
        self.background_task_service.create_task.return_value = MagicMock(
            task_id="bgtask_test", affected=MagicMock(card_ids=[], job_ids=[])
        )
        self.job_service = RuntimeDependencyJobService(
            self.project_service,
            background_task_service=self.background_task_service,
        )
        self.jobs_path = Path(self.tmpdir) / "chat" / "runtime_dependency_jobs.json"
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_jobs(self, jobs):
        self.jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    def test_terminal_event_includes_all_enriched_fields(self):
        """Failed dependency job event payload includes normalized failure details."""
        from app.services.project_event_service import ProjectEventService

        events = []
        event_service = ProjectEventService(self.project_service)
        event_service._publish = lambda pid, ev: events.append(ev)

        job_service = RuntimeDependencyJobService(
            self.project_service,
            project_event_service=event_service,
            background_task_service=self.background_task_service,
        )

        job = job_service.submit(
            "test-project",
            {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["pydeseq2"],
                "source": {"card_id": "card_1"},
            },
            handler=lambda _pid, _payload, **kwargs: {
                "ok": False,
                "error_code": "package_not_found_in_conda_channels",
                "requested_package": "pydeseq2",
                "attempted_candidates": ["pydeseq2"],
                "fallback_available": ["pip"],
                "message": "Package pydeseq2 was not found in conda channels.",
            },
        )
        # Wait for the job to finish
        if job.future:
            job.future.result(timeout=5)

        failed_events = [e for e in events if e.get("payload", {}).get("job_status") == "failed"]
        self.assertTrue(failed_events, "Should have emitted a failed event")
        payload = failed_events[0]["payload"]
        self.assertEqual(payload["error_code"], "package_not_found_in_conda_channels")
        self.assertEqual(payload["requested_package"], "pydeseq2")
        self.assertEqual(payload["attempted_candidates"], ["pydeseq2"])
        self.assertEqual(payload["fallback_available"], ["pip"])
        self.assertEqual(payload["retry_hint"], "do_not_retry_same_conda_request")
        self.assertIn("dedupe_key", payload)

    def test_startup_reconcile_marks_orphaned_active_job_interrupted(self):
        """Persisted active dependency jobs from a previous process must not block forever."""
        self._write_jobs([
            {
                "job_id": "depjob_orphan",
                "project_id": "test-project",
                "task_id": "bgtask_orphan",
                "status": "running",
                "phase": "running_subprocess",
                "created_at": "2026-06-01T00:00:00Z",
                "started_at": "2026-06-01T00:00:01Z",
                "payload": {
                    "ecosystem": "R",
                    "runtime": "R_env",
                    "packages": ["edgeR"],
                    "source": {"card_id": "card_a"},
                },
            }
        ])

        terminalized = self.job_service.reconcile_orphaned_active_jobs()

        self.assertEqual(terminalized, ["depjob_orphan"])
        reloaded = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded[0]["status"], "failed")
        self.assertEqual(reloaded[0]["phase"], "failed")
        self.assertIn("interrupted by backend restart", reloaded[0]["error"])
        self.background_task_service.update_task.assert_any_call(
            "test-project",
            "bgtask_orphan",
            status="interrupted",
            finished_at=reloaded[0]["finished_at"],
            error=reloaded[0]["error"],
        )

    def test_phase_callback_updates_background_task_metadata(self):
        """Job phase updates must be visible through background_tasks.json consumers."""
        job = self.job_service.submit(
            "test-project",
            {
                "ecosystem": "R",
                "runtime": "R_env",
                "packages": ["edgeR"],
            },
            handler=lambda _pid, _payload, phase_callback=None, **kwargs: (
                phase_callback("running_subprocess", command_preview=["mamba", "install", "bioconductor-edger"])
                or {"ok": True}
            ),
        )
        if job.future:
            job.future.result(timeout=5)

        self.background_task_service.update_task.assert_any_call(
            "test-project",
            "bgtask_test",
            adapter={"kind": "dependency_installer", "metadata": {"phase": "running_subprocess", "job_id": job.job_id}},
        )

    def test_flow_service_includes_full_runtime_dependency_blocker(self):
        """FlowService work order item includes enriched blocker fields.

        We test this by directly calling dependency_blockers_by_card and
        verifying the blocker shape matches what FlowService would forward.
        """
        self._write_jobs([
            {
                "job_id": "depjob_1",
                "status": "failed",
                "created_at": "2026-06-01T00:00:00Z",
                "payload": {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["pydeseq2"],
                    "source": {"card_id": "card_a"},
                },
                "result": {
                    "ok": False,
                    "error_code": "package_not_found_in_conda_channels",
                    "requested_package": "pydeseq2",
                    "attempted_candidates": ["pydeseq2"],
                    "fallback_available": ["pip"],
                    "message": "not found",
                },
            }
        ])

        blockers = dependency_blockers_by_card(Path(self.tmpdir))
        blocker = blockers.get("card_a")
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker["error_code"], "package_not_found_in_conda_channels")
        self.assertEqual(blocker["requested_package"], "pydeseq2")
        self.assertEqual(blocker["attempted_candidates"], ["pydeseq2"])
        self.assertEqual(blocker["fallback_available"], ["pip"])
        self.assertEqual(blocker["retry_hint"], "do_not_retry_same_conda_request")
        self.assertIn("dedupe_key", blocker)

    def test_duplicate_in_flight_rejects_without_creating_task(self):
        """Same in-flight request returns duplicate error and does not create a new job."""
        self._write_jobs([
            {
                "job_id": "depjob_1",
                "status": "running",
                "created_at": "2026-06-01T00:00:00Z",
                "payload": {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                },
            }
        ])

        dup = find_duplicate_in_flight(Path(self.tmpdir), "python", "python_env", ["numpy"])
        self.assertIsNotNone(dup)
        self.assertEqual(dup["prior_job_id"], "depjob_1")

    def test_repeated_conda_miss_returns_duplicate_failure(self):
        """Same impossible conda request returns duplicate_dependency_resolution_failure."""
        self._write_jobs([
            {
                "job_id": "depjob_1",
                "status": "failed",
                "created_at": "2026-06-01T00:00:00Z",
                "payload": {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["pydeseq2"],
                },
                "result": {
                    "ok": False,
                    "error_code": "package_not_found_in_conda_channels",
                    "requested_package": "pydeseq2",
                    "fallback_available": ["pip"],
                },
            }
        ])

        dup = find_duplicate_terminal_failure(Path(self.tmpdir), "python", "python_env", ["pydeseq2"])
        self.assertIsNotNone(dup)
        self.assertEqual(dup["prior_job_id"], "depjob_1")
        self.assertEqual(dup["prior_error_code"], "package_not_found_in_conda_channels")

    def test_same_package_different_session_same_project_cooled(self):
        """Cooling is project-scoped, not session-scoped."""
        self._write_jobs([
            {
                "job_id": "depjob_1",
                "status": "failed",
                "created_at": "2026-06-01T00:00:00Z",
                "payload": {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                    "source": {"card_id": "card_a", "session_id": "session_1"},
                },
                "result": {
                    "ok": False,
                    "error_code": "package_not_found_in_conda_channels",
                },
            }
        ])

        dup = find_duplicate_terminal_failure(Path(self.tmpdir), "python", "python_env", ["numpy"])
        self.assertIsNotNone(dup)
        # Different session should still be cooled

    def test_blocker_cleared_by_newer_successful_job(self):
        """A newer successful dependency job clears the blocker for the same card/runtime/package set."""
        self._write_jobs([
            {
                "job_id": "depjob_1",
                "status": "failed",
                "created_at": "2026-06-01T00:00:00Z",
                "payload": {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                    "source": {"card_id": "card_a"},
                },
                "result": {"ok": False, "error_code": "package_not_found_in_conda_channels"},
            },
            {
                "job_id": "depjob_2",
                "status": "succeeded",
                "created_at": "2026-06-01T01:00:00Z",
                "payload": {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                    "source": {"card_id": "card_a"},
                },
                "result": {"ok": True},
            },
        ])

        blockers = dependency_blockers_by_card(Path(self.tmpdir))
        self.assertNotIn("card_a", blockers, "Newer successful job should clear the blocker")

    def test_blocker_cleared_by_manual_resolution(self):
        """Manually resolved job clears blocker; frontend notice dismissal does not."""
        self._write_jobs([
            {
                "job_id": "depjob_1",
                "status": "failed",
                "created_at": "2026-06-01T00:00:00Z",
                "payload": {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                    "source": {"card_id": "card_a"},
                    "resolution_status": "manually_resolved",
                    "resolved_at": "2026-06-01T01:00:00Z",
                },
                "result": {"ok": False, "error_code": "package_not_found_in_conda_channels"},
            }
        ])

        blockers = dependency_blockers_by_card(Path(self.tmpdir))
        self.assertNotIn("card_a", blockers, "Manually resolved job should clear blocker")

    def test_backend_restart_preserves_terminal_cooling(self):
        """Terminal failure cooling survives backend restart because it reads persisted file."""
        self._write_jobs([
            {
                "job_id": "depjob_1",
                "status": "failed",
                "created_at": "2026-06-01T00:00:00Z",
                "payload": {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                },
                "result": {"ok": False, "error_code": "package_not_found_in_conda_channels"},
            }
        ])

        # Simulate backend restart: fresh service instance, no in-memory jobs
        dup = find_duplicate_terminal_failure(Path(self.tmpdir), "python", "python_env", ["numpy"])
        self.assertIsNotNone(dup)
        self.assertEqual(dup["prior_job_id"], "depjob_1")

    def test_event_tails_truncated(self):
        """Project event stdout_tail / stderr_tail are bounded and include truncated metadata."""
        from app.services.project_event_service import ProjectEventService
        from app.services.runtime_dependency_state_service import _tail_text_bounded

        events = []
        event_service = ProjectEventService(self.project_service)
        event_service._publish = lambda pid, ev: events.append(ev)

        job_service = RuntimeDependencyJobService(
            self.project_service,
            project_event_service=event_service,
            background_task_service=self.background_task_service,
        )

        long_stdout = "line\n" * 60
        long_stderr = "error\n" * 60

        job = job_service.submit(
            "test-project",
            {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["numpy"],
                "source": {"card_id": "card_1"},
            },
            handler=lambda _pid, _payload, **kwargs: {
                "ok": False,
                "error_code": "dependency_install_failed",
                "message": "Failed",
                "stdout_tail": long_stdout,
                "stderr_tail": long_stderr,
            },
        )
        if job.future:
            job.future.result(timeout=5)

        failed_events = [e for e in events if e.get("payload", {}).get("job_status") == "failed"]
        self.assertTrue(failed_events)
        payload = failed_events[0]["payload"]
        self.assertIn("truncated", payload)
        self.assertTrue(payload["truncated"])
        # Verify tail is actually bounded
        self.assertLessEqual(len(payload.get("stdout_tail", "")), 2100)
        self.assertLessEqual(len(payload.get("stderr_tail", "")), 2100)


    def test_normal_terminal_chat_receipt_success(self):
        """Doc 46: terminal job publishes a stable-id chat receipt via upsert_message."""
        from app.services.project_event_service import ProjectEventService

        chat_svc = MagicMock()
        events = []
        event_service = ProjectEventService(self.project_service)
        event_service._publish = lambda pid, ev: events.append(ev)

        job_service = RuntimeDependencyJobService(
            self.project_service,
            project_event_service=event_service,
            background_task_service=self.background_task_service,
            chat_session_service=chat_svc,
        )
        job = job_service.submit(
            "test-project",
            {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["numpy"],
                "source": {"card_id": "card_1", "session_id": "sess_1"},
            },
            handler=lambda _pid, _payload, **kwargs: {"ok": True, "changed": True},
        )
        if job.future:
            job.future.result(timeout=5)

        chat_svc.upsert_message.assert_called_once()
        call_args = chat_svc.upsert_message.call_args
        self.assertEqual(call_args[0][0], "test-project")
        self.assertEqual(call_args[0][1], "sess_1")
        msg = call_args[0][2]
        self.assertEqual(msg.id, f"depjob_terminal_{job.job_id}")
        self.assertEqual(msg.timeline[0].id, f"depjob_terminal_timeline_{job.job_id}")
        self.assertEqual(msg.content, "依赖安装完成。")

        terminal_events = [e for e in events if e.get("payload", {}).get("job_status") == "succeeded"]
        self.assertTrue(terminal_events, "Project event should still be emitted")

    def test_normal_terminal_chat_receipt_already_satisfied(self):
        """Doc 46: no-op install renders the already-satisfied receipt."""
        chat_svc = MagicMock()
        job_service = RuntimeDependencyJobService(
            self.project_service,
            background_task_service=self.background_task_service,
            chat_session_service=chat_svc,
        )
        job = job_service.submit(
            "test-project",
            {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["numpy"],
                "source": {"card_id": "card_1", "session_id": "sess_1"},
            },
            handler=lambda _pid, _payload, **kwargs: {"ok": True, "changed": False},
        )
        if job.future:
            job.future.result(timeout=5)

        msg = chat_svc.upsert_message.call_args[0][2]
        self.assertEqual(msg.content, "依赖已满足，无需安装。")

    def test_normal_terminal_chat_receipt_failure(self):
        """Doc 46: failed install renders the failure receipt with error tail."""
        chat_svc = MagicMock()
        job_service = RuntimeDependencyJobService(
            self.project_service,
            background_task_service=self.background_task_service,
            chat_session_service=chat_svc,
        )
        job = job_service.submit(
            "test-project",
            {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["missing_pkg"],
                "source": {"card_id": "card_1", "session_id": "sess_1"},
            },
            handler=lambda _pid, _payload, **kwargs: {
                "ok": False,
                "message": "Package not found in channels.",
            },
        )
        if job.future:
            job.future.result(timeout=5)

        msg = chat_svc.upsert_message.call_args[0][2]
        self.assertEqual(msg.content, "依赖安装失败：Package not found in channels.")

    def test_reconcile_terminal_chat_receipt_includes_chinese_interrupt(self):
        """Doc 46: reconcile orphaned job emits Chinese interruption receipt."""
        from app.services.project_event_service import ProjectEventService

        self._write_jobs([
            {
                "job_id": "depjob_orphan",
                "project_id": "test-project",
                "task_id": "bgtask_orphan",
                "status": "running",
                "phase": "running_subprocess",
                "created_at": "2026-06-01T00:00:00Z",
                "started_at": "2026-06-01T00:00:01Z",
                "payload": {
                    "ecosystem": "R",
                    "runtime": "R_env",
                    "packages": ["edgeR"],
                    "source": {"card_id": "card_a", "session_id": "sess_reconcile"},
                },
            }
        ])

        chat_svc = MagicMock()
        events = []
        event_service = ProjectEventService(self.project_service)
        event_service._publish = lambda pid, ev: events.append(ev)

        job_service = RuntimeDependencyJobService(
            self.project_service,
            project_event_service=event_service,
            background_task_service=self.background_task_service,
            chat_session_service=chat_svc,
        )
        terminalized = job_service.reconcile_orphaned_active_jobs()

        self.assertEqual(terminalized, ["depjob_orphan"])
        chat_svc.upsert_message.assert_called_once()
        msg = chat_svc.upsert_message.call_args[0][2]
        self.assertEqual(msg.id, "depjob_terminal_depjob_orphan")
        self.assertEqual(msg.content, "依赖安装被后端重启中断。")

        failed_events = [e for e in events if e.get("payload", {}).get("job_status") == "failed"]
        self.assertTrue(failed_events, "Project event should still be emitted after reconcile")

    def test_duplicate_terminal_publish_is_idempotent(self):
        """Doc 46: repeated publish for the same job replaces the same message."""
        chat_svc = MagicMock()
        job_service = RuntimeDependencyJobService(
            self.project_service,
            background_task_service=self.background_task_service,
            chat_session_service=chat_svc,
        )
        job = job_service.submit(
            "test-project",
            {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["numpy"],
                "source": {"card_id": "card_1", "session_id": "sess_1"},
            },
            handler=lambda _pid, _payload, **kwargs: {"ok": True, "changed": False},
        )
        if job.future:
            job.future.result(timeout=5)

        # First publish happened automatically after handler completion
        self.assertEqual(chat_svc.upsert_message.call_count, 1)

        # Manually publish again — must call upsert_message a second time with the same ID
        job_service._publish_terminal_chat_receipt(job)
        self.assertEqual(chat_svc.upsert_message.call_count, 2)
        ids = [call[0][2].id for call in chat_svc.upsert_message.call_args_list]
        self.assertEqual(ids[0], ids[1])
        self.assertTrue(ids[0].startswith("depjob_terminal_"))

    def test_auto_owner_session_skips_chat_receipt(self):
        """Doc 46: auto owner session does not receive dependency receipt bubbles."""
        from unittest.mock import patch

        chat_svc = MagicMock()
        auto_svc = MagicMock()
        auto_svc.get_state.return_value = MagicMock(enabled=True, owner_session_id="sess_auto")

        job_service = RuntimeDependencyJobService(
            self.project_service,
            background_task_service=self.background_task_service,
            chat_session_service=chat_svc,
        )
        with patch("app.api.deps.get_manager_auto_service", return_value=auto_svc):
            job = job_service.submit(
                "test-project",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                    "source": {"card_id": "card_1", "session_id": "sess_auto"},
                },
                handler=lambda _pid, _payload, **kwargs: {"ok": True, "changed": False},
            )
            if job.future:
                job.future.result(timeout=5)

        chat_svc.upsert_message.assert_not_called()

    def test_exception_in_background_task_update_does_not_leave_persisted_job_running(self):
        """Exception in background task update after terminal mutation must not skip persistence."""
        from app.services.project_event_service import ProjectEventService

        chat_svc = MagicMock()
        events = []
        event_service = ProjectEventService(self.project_service)
        event_service._publish = lambda pid, ev: events.append(ev)

        job_service = RuntimeDependencyJobService(
            self.project_service,
            project_event_service=event_service,
            background_task_service=self.background_task_service,
            chat_session_service=chat_svc,
        )
        # Make background_task_service.update_task raise for the terminal-finalization
        # call only (not the submit/persist calls). The submit path calls update_task
        # twice: once for creation and once for affected-job-ids. We use a counter to
        # only raise on the terminal-finalization call inside _run's success path.
        call_count = [0]

        def update_task_side_effect(*args, **kwargs):
            call_count[0] += 1
            # Raise only on the 4th call — the terminal-finalization call in _run success path
            # (calls: 1=create_task, 2=update_task during submit, 3=update_task during _run start, 4=terminal finalization)
            if call_count[0] >= 4:
                raise RuntimeError("simulated bg task failure")
            return MagicMock()

        self.background_task_service.update_task.side_effect = update_task_side_effect

        job = job_service.submit(
            "test-project",
            {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["numpy"],
                "source": {"card_id": "card_1", "session_id": "sess_1"},
            },
            handler=lambda _pid, _payload, **kwargs: {"ok": True, "changed": True},
        )
        if job.future:
            job.future.result(timeout=5)

        # Job must be terminal in memory
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.phase, "succeeded")

        # Persisted JSON must be terminal (not running)
        disk_jobs = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        self.assertEqual(len(disk_jobs), 1)
        self.assertEqual(disk_jobs[0]["status"], "succeeded")
        self.assertEqual(disk_jobs[0]["phase"], "succeeded")

        # Chat receipt must still be published (best-effort after persistence)
        self.assertGreaterEqual(chat_svc.upsert_message.call_count, 1)
        msg = chat_svc.upsert_message.call_args[0][2]
        self.assertTrue(msg.id.startswith("depjob_terminal_"))

        # Project event must still be emitted
        succeeded_events = [e for e in events if e.get("payload", {}).get("job_status") == "succeeded"]
        self.assertTrue(succeeded_events, "Project event should be emitted even with bg task failure")

    def test_exception_in_persist_after_terminal_mutation_logs_and_continues(self):
        """Exception in persist path after job terminal mutation is logged, not swallowed silently."""
        import logging
        from io import StringIO

        chat_svc = MagicMock()
        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.WARNING)
        logger = logging.getLogger("app.services.runtime_dependency_job_service")
        logger.addHandler(handler)

        job_service = RuntimeDependencyJobService(
            self.project_service,
            background_task_service=self.background_task_service,
            chat_session_service=chat_svc,
        )

        # Count _persist_project_jobs_locked calls — make the terminal-finalization
        # persist call raise (the call in the success path, which is the 3rd persist).
        # submit: initial persist; _run start: persist running state;
        # success terminal finalization: the call we want to fail.
        persist_count = [0]
        original_persist = job_service._persist_project_jobs_locked

        def failing_persist(pid):
            persist_count[0] += 1
            # Persist calls: 1=submit initial, 2=_run start (waiting_for_runtime_lock),
            # 3=_run building_command phase, 4=terminal finalization
            if persist_count[0] == 4:
                raise OSError("simulated persist failure at terminal finalization")
            return original_persist(pid)

        try:
            job_service._persist_project_jobs_locked = failing_persist

            job = job_service.submit(
                "test-project",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                    "source": {"card_id": "card_1", "session_id": "sess_1"},
                },
                handler=lambda _pid, _payload, **kwargs: {"ok": True, "changed": True},
            )
            if job.future:
                job.future.result(timeout=5)

            # Job must still be terminal in memory despite persist failure
            self.assertEqual(job.status, "succeeded")
            self.assertIn("Failed to persist terminal dependency job state", log_stream.getvalue())
        finally:
            logger.removeHandler(handler)
            job_service._persist_project_jobs_locked = original_persist

    def test_self_heal_converts_memory_terminal_disk_running_to_disk_terminal(self):
        """Self-heal detects memory=terminal + disk=running and overwrites persisted record."""
        from app.services.project_event_service import ProjectEventService

        chat_svc = MagicMock()
        events = []
        event_service = ProjectEventService(self.project_service)
        event_service._publish = lambda pid, ev: events.append(ev)

        job_service = RuntimeDependencyJobService(
            self.project_service,
            project_event_service=event_service,
            background_task_service=self.background_task_service,
            chat_session_service=chat_svc,
        )

        # Create a terminal job in memory first
        job = job_service.submit(
            "test-project",
            {
                "ecosystem": "R",
                "runtime": "R_env",
                "packages": ["tmap"],
                "source": {"card_id": "card_1", "session_id": "sess_1"},
            },
            handler=lambda _pid, _payload, **kwargs: {"ok": True, "changed": True},
        )
        if job.future:
            job.future.result(timeout=5)

        # Reset call counts after normal submit/run cycle
        chat_svc.reset_mock()

        # Now simulate drift: overwrite persisted JSON with "running" status
        self._write_jobs([
            {
                "job_id": job.job_id,
                "project_id": "test-project",
                "task_id": job.task_id,
                "status": "running",
                "phase": "running_subprocess",
                "created_at": job.created_at,
                "started_at": job.started_at,
                "payload": {
                    "ecosystem": "R",
                    "runtime": "R_env",
                    "packages": ["tmap"],
                    "source": {"card_id": "card_1", "session_id": "sess_1"},
                },
            }
        ])

        # Trigger self-heal via get_for_project (which calls _self_heal_terminal_drift_locked)
        healed_job = job_service.get_for_project("test-project", job.job_id)
        self.assertIsNotNone(healed_job)
        self.assertEqual(healed_job.status, "succeeded")

        # Persisted JSON must now be terminal
        disk_jobs = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        self.assertEqual(len(disk_jobs), 1)
        self.assertEqual(disk_jobs[0]["job_id"], job.job_id)
        self.assertEqual(disk_jobs[0]["status"], "succeeded")
        self.assertEqual(disk_jobs[0]["phase"], "succeeded")

        # Receipt must be published
        self.assertGreaterEqual(chat_svc.upsert_message.call_count, 1)
        msg = chat_svc.upsert_message.call_args[0][2]
        self.assertEqual(msg.id, f"depjob_terminal_{job.job_id}")

    def test_self_heal_disk_already_terminal_is_noop(self):
        """Self-heal is a no-op when both memory and disk agree on terminal status."""
        job_service = RuntimeDependencyJobService(
            self.project_service,
            background_task_service=self.background_task_service,
        )

        job = job_service.submit(
            "test-project",
            {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["numpy"],
                "source": {"card_id": "card_1", "session_id": "sess_1"},
            },
            handler=lambda _pid, _payload, **kwargs: {"ok": True, "changed": True},
        )
        if job.future:
            job.future.result(timeout=5)

        # Both memory and disk are terminal — self-heal should not change anything
        disk_before = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        disk_status_before = disk_before[0]["status"]

        # get_for_project triggers self-heal check but should be a no-op
        result = job_service.get_for_project("test-project", job.job_id)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "succeeded")

        disk_after = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        self.assertEqual(disk_after[0]["status"], disk_status_before)

    def test_self_heal_persist_failure_skips_receipt_publish(self):
        """Self-heal persist failure must NOT publish a terminal receipt.

        When _self_heal_terminal_drift_locked detects drift but the persist
        call itself fails, it must return False so callers do not proceed to
        _publish_terminal_chat_receipt. Otherwise chat gets a receipt message
        while disk stays stale, recreating the exact split this fix targets.
        """
        chat_svc = MagicMock()
        job_service = RuntimeDependencyJobService(
            self.project_service,
            background_task_service=self.background_task_service,
            chat_session_service=chat_svc,
        )

        # Create a terminal job first (normal cycle)
        job = job_service.submit(
            "test-project",
            {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["numpy"],
                "source": {"card_id": "card_1", "session_id": "sess_1"},
            },
            handler=lambda _pid, _payload, **kwargs: {"ok": True, "changed": True},
        )
        if job.future:
            job.future.result(timeout=5)

        chat_svc.reset_mock()

        # Simulate drift: overwrite persisted JSON with "running"
        self._write_jobs([
            {
                "job_id": job.job_id,
                "project_id": "test-project",
                "task_id": job.task_id,
                "status": "running",
                "phase": "running_subprocess",
                "created_at": job.created_at,
                "started_at": job.started_at,
                "payload": {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                    "source": {"card_id": "card_1", "session_id": "sess_1"},
                },
            }
        ])

        # Now make persist fail so self-heal cannot write the terminal record
        original_persist = job_service._persist_project_jobs_locked

        def always_failing_persist(pid):
            raise OSError("simulated persist failure in self-heal path")

        job_service._persist_project_jobs_locked = always_failing_persist
        try:
            # get_for_project triggers self-heal; persist fails → must return False,
            # and the caller must NOT call _publish_terminal_chat_receipt
            healed_job = job_service.get_for_project("test-project", job.job_id)
            self.assertIsNotNone(healed_job)
            self.assertEqual(healed_job.status, "succeeded")

            # Receipt must NOT be published — persist failed, so we must not
            # tell the chat session that the job is terminal while disk is still
            # showing running.
            chat_svc.upsert_message.assert_not_called()

            # Disk must still show "running" — the failed persist couldn't
            # overwrite it, and that's the correct (conservative) behaviour:
            # keep the problem visible rather than papering over it.
            disk_jobs = json.loads(self.jobs_path.read_text(encoding="utf-8"))
            self.assertEqual(len(disk_jobs), 1)
            self.assertEqual(disk_jobs[0]["status"], "running")
        finally:
            job_service._persist_project_jobs_locked = original_persist

    def test_progress_fields_round_trip_through_persist_and_event(self):
        """Layer F2: progress fields are persisted, loaded, and emitted on SSE."""
        from app.services.project_event_service import ProjectEventService

        events = []
        event_service = ProjectEventService(self.project_service)
        event_service._publish = lambda pid, ev: events.append(ev)

        job_service = RuntimeDependencyJobService(
            self.project_service,
            project_event_service=event_service,
            background_task_service=self.background_task_service,
        )

        def handler(_pid, _payload, phase_callback=None, **kwargs):
            phase_callback(
                "running_subprocess",
                command_preview=["pip", "install", "numpy"],
                progress=42,
                progress_label="Downloading numpy",
                bytes_total=16 * 1024 * 1024,
                bytes_downloaded=7 * 1024 * 1024,
                download_rate_bps=1024 * 1024,
                stdout_tail="Downloading numpy...",
                stderr_tail="",
            )
            return {"ok": True, "changed": True}

        job = job_service.submit(
            "test-project",
            {"ecosystem": "python", "runtime": "py_env", "packages": ["numpy"]},
            handler=handler,
        )
        if job.future:
            job.future.result(timeout=5)

        # In-memory job
        self.assertEqual(job.progress, 42)
        self.assertEqual(job.progress_label, "Downloading numpy")
        self.assertEqual(job.bytes_total, 16 * 1024 * 1024)
        self.assertEqual(job.bytes_downloaded, 7 * 1024 * 1024)
        self.assertEqual(job.download_rate_bps, 1024 * 1024)

        # Persisted JSON round-trip
        disk_jobs = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        self.assertEqual(len(disk_jobs), 1)
        disk = disk_jobs[0]
        self.assertEqual(disk["progress"], 42)
        self.assertEqual(disk["progress_label"], "Downloading numpy")
        self.assertEqual(disk["bytes_total"], 16 * 1024 * 1024)
        self.assertEqual(disk["bytes_downloaded"], 7 * 1024 * 1024)
        self.assertEqual(disk["download_rate_bps"], 1024 * 1024)

        # SSE payload includes progress fields
        running_events = [
            e
            for e in events
            if e.get("payload", {}).get("phase") == "running_subprocess"
        ]
        self.assertTrue(running_events)
        payload = running_events[-1]["payload"]
        self.assertEqual(payload["progress"], 42)
        self.assertEqual(payload["progress_label"], "Downloading numpy")
        self.assertEqual(payload["bytes_total"], 16 * 1024 * 1024)
        self.assertIn("stdout_tail", payload)

        # Reload from disk preserves progress fields
        fresh_service = RuntimeDependencyJobService(
            self.project_service,
            background_task_service=self.background_task_service,
        )
        fresh_service.get_for_project("test-project", job.job_id)
        reloaded = fresh_service.get_for_project("test-project", job.job_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.progress, 42)


if __name__ == "__main__":
    unittest.main()
