"""Tests for WorkerService restart reconcile of orphaned active runs.

On backend restart, runs left in a restart-orphaned status (queued / launching /
running / reviewing) have no live executor thread in the new process and cannot
self-advance, so reconcile must mark them failed. needs_approval runs are paused
waiting for the user and resume from disk via continue_run, so reconcile must
leave them untouched. Terminal runs are never touched.

See RESTART_ORPHANED_RUN_STATUSES in app.models.graph.

#7 (audit §2.3): when reconcile marks an orphan failed it must honor any executor
terminal report already on disk rather than masking the real outcome with a
generic "backend restarted" message:
  - report_fail / synthetic_failure -> preserve the executor's real summary;
  - report_complete -> honest "re-run to recover" message (still failed, because
    startup cannot replay finalization -- that is §2.3b);
  - no report -> the generic restart message is the true cause.
The card's manager_review is appended to, never clobbered (annotate idiom).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.config import get_settings
from app.models.cards import Card
from app.models.graph import GraphState, RunRecord


PROJECT_ID = "proj-recon"
_STARTED_AT = "2026-01-01T00:00:00Z"


class ReconcileActiveRunsTest(unittest.TestCase):
    def setUp(self):
        self.data_root = Path(tempfile.mkdtemp())
        self._env = patch.dict(
            os.environ,
            {
                "BLUEPRINT_DATA_ROOT": str(self.data_root),
                "BLUEPRINT_EXECUTOR_SANDBOX_MODE": "none",
            },
            clear=False,
        )
        self._env.start()
        get_settings.cache_clear()

    def tearDown(self):
        self._env.stop()
        get_settings.cache_clear()
        shutil.rmtree(self.data_root, ignore_errors=True)

    @staticmethod
    def _card(card_id: str, status: str) -> Card:
        return Card(
            card_id=card_id,
            card_type="run",
            title=f"Card {card_id}",
            status=status,
            step=1,
            summary="",
        )

    @staticmethod
    def _run(run_id: str, card_id: str, status: str) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            task_id=None,
            card_id=card_id,
            status=status,
            title=f"Run {run_id}",
            summary="original summary",
            started_at=_STARTED_AT,
        )

    def _seed_project(self, runs: list[RunRecord], cards: list[Card]) -> None:
        from app.services.project_service import ProjectService

        project_service = ProjectService()
        store = project_service.graph_store(PROJECT_ID)
        store.save_cards(cards)
        store.save_graph(GraphState(runs=runs))

    def _seed_terminal_report(
        self,
        run_id: str,
        terminal_kind: str,
        *,
        summary: str,
        status: str = "failed",
        failure_summary: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        """Write the on-disk executor terminal contract for a run.

        Mirrors what the executor subprocess leaves in the run dir before the
        backend consumes it; reconcile reads these via _reconcile_run_summary.
        """
        run_dir = self.data_root / PROJECT_ID / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "schema_version": "executor_terminal_report.v1",
            "run_id": run_id,
            "terminal_kind": terminal_kind,
            "accepted_at": _STARTED_AT,
            "summary": summary,
            "status": status,
        }
        if reason_code is not None:
            report["reason_code"] = reason_code
        (run_dir / "terminal_report.json").write_text(json.dumps(report), encoding="utf-8")
        if failure_summary is not None:
            (run_dir / "executor_failure.json").write_text(
                json.dumps(
                    {
                        "schema_version": "executor_failure.v1",
                        "reason_code": reason_code or "execution_error",
                        "summary": failure_summary,
                    }
                ),
                encoding="utf-8",
            )

    def _run_reconcile(self) -> dict[str, RunRecord]:
        """Construct WorkerService (which reconciles in __init__) and reload runs."""
        from app.services.project_service import ProjectService
        from app.services.worker_service import WorkerService

        project_service = ProjectService()
        WorkerService(
            project_service,
            MagicMock(),  # manifest_service
            MagicMock(),  # runtime_approval_service
            library_registry_service=MagicMock(),
            background_task_service=MagicMock(),
        )
        graph = project_service.graph_store(PROJECT_ID).load_graph()
        return {run.run_id: run for run in graph.runs}

    def test_orphaned_active_runs_marked_failed(self):
        # Each restart-orphaned status, plus the live cards driving them.
        runs = [
            self._run("run-queued", "card-queued", "queued"),
            self._run("run-launching", "card-launching", "launching"),
            self._run("run-running", "card-running", "running"),
            self._run("run-reviewing", "card-reviewing", "reviewing"),
        ]
        cards = [
            self._card("card-queued", "running"),
            self._card("card-launching", "running"),
            self._card("card-running", "running"),
            self._card("card-reviewing", "reviewing"),
        ]
        self._seed_project(runs, cards)

        reconciled = self._run_reconcile()

        for run_id in ("run-queued", "run-launching", "run-running", "run-reviewing"):
            self.assertEqual(reconciled[run_id].status, "failed", run_id)
            self.assertIsNotNone(reconciled[run_id].finished_at, run_id)
            self.assertIn("restarted", reconciled[run_id].summary.lower(), run_id)

    def test_needs_approval_run_is_preserved(self):
        # needs_approval is a pre-launch gate that resumes from disk — reconcile
        # must NOT touch it, otherwise restarting the backend kills paused runs.
        runs = [self._run("run-pending", "card-pending", "needs_approval")]
        cards = [self._card("card-pending", "planned")]
        self._seed_project(runs, cards)

        reconciled = self._run_reconcile()

        self.assertEqual(reconciled["run-pending"].status, "needs_approval")
        self.assertIsNone(reconciled["run-pending"].finished_at)
        self.assertEqual(reconciled["run-pending"].summary, "original summary")

    def test_terminal_runs_are_unchanged(self):
        runs = [
            self._run("run-success", "card-success", "success"),
            self._run("run-failed", "card-failed", "failed"),
            self._run("run-cancelled", "card-cancelled", "cancelled"),
            self._run("run-reviewed", "card-reviewed", "reviewed"),
        ]
        cards = [
            self._card("card-success", "accepted"),
            self._card("card-failed", "failed"),
            self._card("card-cancelled", "cancelled"),
            self._card("card-reviewed", "accepted"),
        ]
        self._seed_project(runs, cards)

        reconciled = self._run_reconcile()

        self.assertEqual(reconciled["run-success"].status, "success")
        self.assertEqual(reconciled["run-failed"].status, "failed")
        self.assertEqual(reconciled["run-cancelled"].status, "cancelled")
        self.assertEqual(reconciled["run-reviewed"].status, "reviewed")
        for run in reconciled.values():
            self.assertEqual(run.summary, "original summary")

    def test_running_card_synced_to_failed_on_reconcile(self):
        runs = [self._run("run-running", "card-running", "running")]
        cards = [self._card("card-running", "running")]
        self._seed_project(runs, cards)

        from app.services.project_service import ProjectService

        self._run_reconcile()
        card = next(
            c for c in ProjectService().graph_store(PROJECT_ID).load_cards()
            if c.card_id == "card-running"
        )
        self.assertEqual(card.status, "failed")

    # --- #7 (audit §2.3): honor on-disk executor terminal reports -------------

    def _load_card(self, card_id: str) -> Card:
        from app.services.project_service import ProjectService

        return next(
            c for c in ProjectService().graph_store(PROJECT_ID).load_cards()
            if c.card_id == card_id
        )

    def test_failure_report_summary_is_preserved(self):
        # Executor wrote a real failure (report_fail + executor_failure.json) before
        # the restart: reconcile must surface THAT cause, not a generic message.
        self._seed_project(
            [self._run("run-fail", "card-fail", "running")],
            [self._card("card-fail", "running")],
        )
        self._seed_terminal_report(
            "run-fail",
            "report_fail",
            summary="terminal-level summary",
            failure_summary="ImportError: omicverse not installed",
            reason_code="runtime_dependency_missing",
        )

        reconciled = self._run_reconcile()

        self.assertEqual(reconciled["run-fail"].status, "failed")
        self.assertEqual(reconciled["run-fail"].summary, "ImportError: omicverse not installed")
        # And the real cause is appended to the card review.
        self.assertIn("ImportError: omicverse not installed", self._load_card("card-fail").manager_review)

    def test_synthetic_failure_falls_back_to_terminal_summary(self):
        # synthetic_failure with no separate failure_report: use terminal summary.
        self._seed_project(
            [self._run("run-synth", "card-synth", "running")],
            [self._card("card-synth", "running")],
        )
        self._seed_terminal_report(
            "run-synth",
            "synthetic_failure",
            summary="Executor process exited non-zero with no failure report",
        )

        reconciled = self._run_reconcile()

        self.assertEqual(
            reconciled["run-synth"].summary,
            "Executor process exited non-zero with no failure report",
        )

    def test_report_complete_gets_honest_rerun_summary(self):
        # Executor finished (report_complete, pending_review on disk) but the backend
        # restarted before finalizing. Reconcile cannot replay finalization (§2.3b),
        # so it marks failed with an honest re-run message -- NOT the generic restart
        # message, and NOT silently "success".
        self._seed_project(
            [self._run("run-done", "card-done", "reviewing")],
            [self._card("card-done", "reviewing")],
        )
        self._seed_terminal_report(
            "run-done",
            "report_complete",
            summary="executor completed; manifest pending review",
            status="pending_review",
        )

        reconciled = self._run_reconcile()

        run = reconciled["run-done"]
        self.assertEqual(run.status, "failed")
        self.assertIn("re-run", run.summary.lower())
        self.assertIn("could not be finalized", run.summary.lower())
        # Must not masquerade as the generic mid-flight orphan message.
        self.assertNotIn("before executor completed", run.summary.lower())

    def test_genuine_orphan_keeps_generic_summary(self):
        # No disk report at all -> truly mid-flight when the backend died.
        self._seed_project(
            [self._run("run-orphan", "card-orphan", "running")],
            [self._card("card-orphan", "running")],
        )

        reconciled = self._run_reconcile()

        self.assertEqual(reconciled["run-orphan"].status, "failed")
        self.assertIn("before executor completed", reconciled["run-orphan"].summary.lower())

    def test_card_review_is_appended_not_clobbered(self):
        # A pre-existing human/AI review on the card must survive reconcile.
        card = self._card("card-rev", "running")
        card.manager_review = "earlier reviewer note"
        self._seed_project([self._run("run-rev", "card-rev", "running")], [card])

        self._run_reconcile()

        review = self._load_card("card-rev").manager_review
        self.assertTrue(review.startswith("earlier reviewer note"))
        self.assertIn("before executor completed", review.lower())

    def test_reviewing_card_is_coerced_to_failed(self):
        # The card side must coerce both running AND reviewing to failed.
        self._seed_project(
            [self._run("run-rv", "card-rv", "reviewing")],
            [self._card("card-rv", "reviewing")],
        )

        self._run_reconcile()

        self.assertEqual(self._load_card("card-rv").status, "failed")


if __name__ == "__main__":
    unittest.main()
