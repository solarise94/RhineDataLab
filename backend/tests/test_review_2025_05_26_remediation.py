import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.core.config import get_settings
from app.models.cards import Card, CardAssetRef, CardOutputSpec
from app.models.graph import Asset, GraphState, RunRecord
from app.models.runs import CreatedAsset, Manifest, TaskPacket
from app.services.executor_validation_service import ExecutorValidationService
from app.services.manifest_service import ManifestService
from app.services.project_service import ProjectService
from app.services.runtime_approval_service import RuntimeApprovalService
from app.services.utils import atomic_write_json
from app.services.worker_service import WorkerService


class RemediationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-remediation-test-")
        settings = get_settings()
        self._original_data_root = settings.data_root
        settings.data_root = Path(self.tmpdir)
        self.project_service = ProjectService()
        self.project_service.create_project(
            project_id="test-project",
            name="Test Project",
            current_goal="Test remediation",
            seed_demo=False,
        )
        self.manifest_service = ManifestService(self.project_service)
        self.runtime_approval_service = RuntimeApprovalService(self.project_service)
        self.worker = WorkerService(self.project_service, self.manifest_service, self.runtime_approval_service)
        self.validation_service = ExecutorValidationService(self.project_service)

    def tearDown(self) -> None:
        settings = get_settings()
        settings.data_root = self._original_data_root
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_run(self, run_id: str, status: str = "queued") -> None:
        from app.services.utils import utc_now
        store = self.project_service.graph_store("test-project")
        graph = store.load_graph()
        graph.runs.append(
            RunRecord(
                run_id=run_id,
                card_id="card_test",
                status=status,
                title="Test run",
                summary="Test",
                started_at=utc_now(),
            )
        )
        store.save_graph(graph)

    def test_get_run_not_found_raises_404(self) -> None:
        from app.api.runs import get_run

        with self.assertRaises(HTTPException) as ctx:
            get_run("test-project", "nonexistent-run", self.project_service)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("nonexistent-run", str(ctx.exception.detail))

    def test_get_run_existing_returns_run(self) -> None:
        from app.api.runs import get_run

        self._make_run("run_001", status="success")
        result = get_run("test-project", "run_001", self.project_service)
        self.assertEqual(result["run"].run_id, "run_001")

    def test_delete_project_with_active_run_returns_409(self) -> None:
        # Need a second project so delete_project doesn't hit the "only project" guard first.
        self.project_service.create_project(
            project_id="other-project",
            name="Other",
            current_goal="placeholder",
            seed_demo=False,
        )
        self._make_run("run_active", status="running")
        self.assertTrue(self.worker.has_active_runs("test-project"))
        with self.assertRaises(HTTPException) as ctx:
            self.project_service.delete_project("test-project")
        # ProjectService.delete_project now holds the project lock and checks
        # for active runs atomically before shutil.rmtree.
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertTrue(self.worker.has_active_runs("test-project"))

    def test_has_active_runs_false_when_no_runs(self) -> None:
        self.assertFalse(self.worker.has_active_runs("test-project"))

    def test_has_active_runs_includes_launching(self) -> None:
        self._make_run("run_launch", status="launching")
        self.assertTrue(self.worker.has_active_runs("test-project"))

    def test_missing_output_creates_error(self) -> None:
        root = self.project_service.project_path("test-project")
        run_id = "run_missing"
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (root / "results").mkdir(parents=True, exist_ok=True)

        from app.models.runs import RunContext
        task_packet = TaskPacket(
            task_id=run_id,
            project_id="test-project",
            card_id="card_test",
            card_title="Test",
            card_status="running",
            goal="test",
            input_assets=[],
            card_inputs=[],
            card_outputs=[],
            expected_outputs=[],
            allowed_paths=[],
            readonly_paths=[],
            forbidden_paths=[],
            execution_policy={},
            constraints=[],
            worker_instructions="",
            run_context=RunContext(run_id=run_id, worker_type="pi", project_root=str(root), run_dir=f"runs/{run_id}", result_dir="results"),
            executor_context={},
            manager_reporting_contract={},
        )
        manifest = Manifest(
            run_id=run_id,
            status="success",
            summary="test",
            created_assets=[
                CreatedAsset(path="results/missing_output.tsv", role="result", artifact_class="table", format="tsv"),
            ],
            code_artifacts=[],
            key_findings=[],
            validation_evidence={},
        )
        atomic_write_json(run_dir / "task_packet.json", task_packet.model_dump())
        atomic_write_json(run_dir / "manifest.json", manifest.model_dump())

        with patch.object(self.validation_service.reviewer_worker, "review", return_value={"verdict": "pass", "summary": "ok", "issues": [], "mode": "test"}):
            report = self.validation_service.validate_run("test-project", run_id)
        missing_issues = [i for i in report.issues if i.code == "missing_output"]
        self.assertEqual(len(missing_issues), 1)
        self.assertEqual(missing_issues[0].severity, "error")
        self.assertIn("missing_output.tsv", missing_issues[0].message)

    def test_sync_card_outputs_by_asset_id(self) -> None:
        card = Card(
            card_id="card_1",
            card_type="module",
            title="Test",
            status="accepted",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[
                CardOutputSpec(role="deg_table", label="DEG", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_deg"),
            ],
        )
        assets = [
            Asset(
                asset_id="real_deg_001",
                asset_type="table",
                title="DEG",
                status="valid",
                path="results/deg.tsv",
                summary="deg",
                metadata={"role": "deg_table", "planned_asset_id": "planned_deg"},
            )
        ]
        manifest_assets = [
            {"role": "deg_table", "asset_id": "planned_deg", "path": "results/deg.tsv"}
        ]
        WorkerService._sync_card_outputs(card, assets, manifest_created_assets=manifest_assets)
        # Logical output id is preserved; status tracks materialization.
        self.assertEqual(card.outputs[0].asset_id, "planned_deg")
        self.assertEqual(card.outputs[0].status, "valid")
        # Alias backfill should still happen
        self.assertEqual(assets[0].metadata.get("planned_asset_id"), "planned_deg")

    def test_sync_card_outputs_out_of_order(self) -> None:
        card = Card(
            card_id="card_1",
            card_type="module",
            title="Test",
            status="accepted",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[
                CardOutputSpec(role="a", label="A", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_a"),
                CardOutputSpec(role="b", label="B", artifact_class="figure", accepted_formats=["png"], asset_id="planned_b"),
            ],
        )
        assets = [
            Asset(asset_id="real_b", asset_type="figure", title="B", status="valid", path="b.png", summary="b", metadata={"role": "b", "planned_asset_id": "planned_b"}),
            Asset(asset_id="real_a", asset_type="table", title="A", status="valid", path="a.tsv", summary="a", metadata={"role": "a", "planned_asset_id": "planned_a"}),
        ]
        manifest_assets = [
            {"role": "b", "asset_id": "planned_b", "path": "b.png"},
            {"role": "a", "asset_id": "planned_a", "path": "a.tsv"},
        ]
        WorkerService._sync_card_outputs(card, assets, manifest_created_assets=manifest_assets)
        # Logical ids preserved; statuses updated.
        self.assertEqual(card.outputs[0].asset_id, "planned_a")
        self.assertEqual(card.outputs[0].status, "valid")
        self.assertEqual(card.outputs[1].asset_id, "planned_b")
        self.assertEqual(card.outputs[1].status, "valid")

    def test_sync_card_outputs_duplicate_role_disambiguates_by_planned_id(self) -> None:
        card = Card(
            card_id="card_1",
            card_type="module",
            title="Test",
            status="accepted",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[
                CardOutputSpec(role="dup", label="Dup", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_dup"),
            ],
        )
        assets = [
            Asset(asset_id="real_1", asset_type="table", title="D1", status="valid", path="1.tsv", summary="d1",
                  metadata={"role": "dup", "planned_asset_id": "planned_dup"}),
            Asset(asset_id="real_2", asset_type="table", title="D2", status="valid", path="2.tsv", summary="d2",
                  metadata={"role": "dup", "planned_asset_id": "planned_other"}),
        ]
        manifest_assets = [
            {"role": "dup", "asset_id": "planned_dup", "path": "1.tsv"},
        ]
        # planned_asset_id in metadata resolves the duplicate role correctly.
        unmapped = WorkerService._sync_card_outputs(card, assets, manifest_created_assets=manifest_assets)
        # Logical id preserved; binding resolved to real_1 via planned_asset_id.
        self.assertEqual(card.outputs[0].asset_id, "planned_dup")
        self.assertEqual(unmapped, [])

    def test_backfill_planned_asset_alias_recovers_logical_id_from_concrete_chain(self) -> None:
        previous = Asset(
            asset_id="asset_prev",
            asset_type="table",
            title="Previous",
            status="valid",
            path="prev.tsv",
            summary="prev",
            metadata={"role": "out", "planned_asset_id": "planned_out"},
        )
        current = Asset(
            asset_id="asset_current",
            asset_type="table",
            title="Current",
            status="candidate",
            path="current.tsv",
            summary="current",
            metadata={"role": "out", "planned_asset_id": "asset_prev"},
        )

        WorkerService._backfill_planned_asset_alias(
            current,
            "asset_prev",
            asset_lookup={
                "asset_prev": previous,
                "asset_current": current,
            },
        )

        self.assertEqual("planned_out", current.metadata.get("planned_asset_id"))

    def test_sync_card_outputs_duplicate_role_resolves_by_unique_path(self) -> None:
        # With duplicate roles but unique paths, the mapping resolves by path (Priority 4).
        card = Card(
            card_id="card_1",
            card_type="module",
            title="Test",
            status="accepted",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[
                CardOutputSpec(role="dup", label="Dup", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_dup"),
            ],
        )
        assets = [
            Asset(asset_id="real_1", asset_type="table", title="D1", status="valid", path="1.tsv", summary="d1",
                  metadata={"role": "dup"}),
            Asset(asset_id="real_2", asset_type="table", title="D2", status="valid", path="2.tsv", summary="d2",
                  metadata={"role": "dup"}),
        ]
        manifest_assets = [
            {"role": "dup", "asset_id": "planned_dup", "path": "1.tsv"},
        ]
        unmapped = WorkerService._sync_card_outputs(card, assets, manifest_created_assets=manifest_assets)
        # Path fallback resolves to real_1 because manifest path is "1.tsv".
        # Logical id preserved; binding resolved via path.
        self.assertEqual(card.outputs[0].asset_id, "planned_dup")
        self.assertEqual(unmapped, [])

    def test_sync_card_outputs_duplicate_role_and_path_stays_ambiguous(self) -> None:
        # When both role and path are duplicated (or path is missing), mapping stays ambiguous.
        card = Card(
            card_id="card_1",
            card_type="module",
            title="Test",
            status="accepted",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[
                CardOutputSpec(role="dup", label="Dup", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_dup"),
            ],
        )
        assets = [
            Asset(asset_id="real_1", asset_type="table", title="D1", status="valid", path="same.tsv", summary="d1",
                  metadata={"role": "dup"}),
            Asset(asset_id="real_2", asset_type="table", title="D2", status="valid", path="same.tsv", summary="d2",
                  metadata={"role": "dup"}),
        ]
        manifest_assets = [
            {"role": "dup", "asset_id": "planned_dup"},
        ]
        unmapped = WorkerService._sync_card_outputs(card, assets, manifest_created_assets=manifest_assets)
        # No unique path and no planned_asset_id -> stays ambiguous.
        self.assertEqual(card.outputs[0].asset_id, "planned_dup")
        self.assertEqual(unmapped, ["output[0] role=dup asset_id=planned_dup"])

    def test_sync_card_outputs_uses_same_role_fallback_as_review_path(self) -> None:
        card = Card(
            card_id="card_1",
            card_type="module",
            title="Test",
            status="accepted",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[
                CardOutputSpec(role="summary_table", label="Summary", artifact_class="table", accepted_formats=["tsv"]),
            ],
        )
        assets = [
            Asset(
                asset_id="real_summary",
                asset_type="table",
                title="Summary",
                status="valid",
                path="summary.tsv",
                summary="summary",
                metadata={"role": "summary_table"},
            )
        ]
        manifest_assets = [
            {"role": "summary_table", "path": "summary.tsv"},
        ]
        unmapped = WorkerService._sync_card_outputs(card, assets, manifest_created_assets=manifest_assets)
        # When output has no asset_id, role-based resolution fills it.
        # Since there is no planned id, the concrete asset_id is used.
        self.assertEqual(card.outputs[0].asset_id, "real_summary")
        self.assertEqual(card.outputs[0].status, "valid")
        self.assertEqual(unmapped, [])

    def test_event_source_explicit_executor_types(self) -> None:
        store = self.project_service.graph_store("test-project")
        self.worker._append_event("test-project", "run_1", "card_1", event_type="executor_output", message="line")
        self.worker._append_event("test-project", "run_1", "card_1", event_type="executor_progress", message="progress")
        self.worker._append_event("test-project", "run_1", "card_1", event_type="executor_issue", message="issue")
        self.worker._append_event("test-project", "run_1", "card_1", event_type="run_started", message="started")
        events = store.load_run_events("run_1")
        sources = {e.event_type: e.source for e in events}
        self.assertEqual(sources.get("executor_output"), "executor")
        self.assertEqual(sources.get("executor_progress"), "executor")
        self.assertEqual(sources.get("executor_issue"), "executor")
        self.assertEqual(sources.get("run_started"), "manager")

    def test_cleanup_run_blocked_while_cancelled_thread_still_alive(self) -> None:
        # Regression: after cancel_run(), the executor thread is still finishing
        # in its finally block. cleanup_run() must refuse (409) to avoid deleting
        # runs/<run_id> and results/ while the thread is still touching them.
        # The fix removed the premature self._threads.pop() from cancel_run().
        store = self.project_service.graph_store("test-project")
        cards = [
            Card(
                card_id="card_test",
                card_type="module",
                title="Test",
                status="running",
                step=1,
                summary="",
                why="",
                inputs=[],
                outputs=[],
            )
        ]
        store.save_cards(cards)
        self._make_run("run_cancel_cleanup", status="running")

        # Simulate an executor thread that has not yet exited its finally block.
        class _FakeAliveThread:
            def is_alive(self) -> bool:
                return True

        self.worker._threads["run_cancel_cleanup"] = _FakeAliveThread()  # type: ignore[assignment]

        # cancel_run sets the run to cancelled but must NOT pop the thread handle.
        result = self.worker.cancel_run("test-project", "run_cancel_cleanup")
        self.assertEqual(result["status"], "cancelled")
        self.assertIn("run_cancel_cleanup", self.worker._threads)

        # cleanup_run must refuse while the thread is still alive.
        with self.assertRaises(HTTPException) as ctx:
            self.worker.cleanup_run("test-project", "run_cancel_cleanup")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("live executor thread", str(ctx.exception.detail))

    def test_run_status_reads_under_lock(self) -> None:
        self._make_run("run_lock", status="queued")
        status = self.worker._run_status("test-project", "run_lock")
        self.assertEqual(status, "queued")

    def test_active_run_statuses_includes_launching(self) -> None:
        self.assertIn("launching", self.worker._active_run_statuses())

    def test_corrupt_proposals_file_raises_instead_of_silent_empty(self) -> None:
        # Regression: /advanced/proposals must not swallow parse errors into [].
        # load_proposals() already returns [] for missing files via read_json default,
        # so the only exceptions reaching the catch-all were data corruption or bugs.
        from app.api.advanced import get_proposals

        proposals_path = self.project_service.project_path("test-project") / "graph" / "proposals.json"
        proposals_path.parent.mkdir(parents=True, exist_ok=True)
        proposals_path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(Exception):
            get_proposals("test-project", self.project_service)

    def test_websocket_client_disconnect_does_not_raise(self) -> None:
        # Regression: after a normal client disconnect, the server must not throw
        # RuntimeError from a second websocket.close() in the finally block.
        # The WebSocket handler watches for client disconnect via a concurrent
        # receive task, so a non-terminal run no longer causes an infinite loop.
        from fastapi.testclient import TestClient
        from app.main import app

        self._make_run("run_ws", status="running")
        client = TestClient(app)
        with client.websocket_connect("/api/projects/test-project/runs/run_ws/ws") as ws:
            # Send a small sleep to let the handler start, then exit —
            # the client disconnect will be detected by the watcher task.
            import time
            time.sleep(0.3)
        # If we reach here without an exception, the fix works.

    def test_finalize_run_review_downgrades_to_needs_review_on_ambiguous_outputs(self) -> None:
        # Regression: when output mapping is ambiguous (duplicate roles without
        # planned_asset_id in metadata), the run must NOT be treated as accepted:
        # - assets stay as candidate (not valid)
        # - no claims created
        # - no downstream rebinds
        # - no report items written
        from app.models.runs import RunContext
        from app.services.utils import utc_now

        root = self.project_service.project_path("test-project")
        run_id = "run_ambiguous"
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (root / "results").mkdir(parents=True, exist_ok=True)

        store = self.project_service.graph_store("test-project")
        card = Card(
            card_id="card_ambig",
            card_type="module",
            title="Ambig Test",
            status="running",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[
                CardOutputSpec(role="dup", label="Dup", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_a"),
                CardOutputSpec(role="dup", label="Dup2", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_b"),
            ],
        )
        store.save_cards([card])
        graph = store.load_graph()
        graph.runs.append(
            RunRecord(
                run_id=run_id,
                card_id="card_ambig",
                status="running",
                title="Ambiguous run",
                summary="test",
                started_at=utc_now(),
            )
        )
        store.save_graph(graph)

        task_packet = TaskPacket(
            task_id=run_id,
            project_id="test-project",
            card_id="card_ambig",
            card_title="Ambig Test",
            card_status="running",
            goal="test",
            input_assets=[],
            card_inputs=[],
            card_outputs=[],
            expected_outputs=[],
            allowed_paths=[],
            readonly_paths=[],
            forbidden_paths=[],
            execution_policy={},
            constraints=[],
            worker_instructions="",
            run_context=RunContext(run_id=run_id, worker_type="pi", project_root=str(root), run_dir=f"runs/{run_id}", result_dir="results"),
            executor_context={},
            manager_reporting_contract={},
        )

        manifest = Manifest(
            run_id=run_id,
            status="success",
            summary="test",
            created_assets=[
                CreatedAsset(path="results/a.tsv", role="dup", artifact_class="table", format="tsv"),
                CreatedAsset(path="results/b.tsv", role="dup", artifact_class="table", format="tsv"),
            ],
            code_artifacts=[],
            key_findings=["finding 1"],
            validation_evidence={},
        )
        atomic_write_json(run_dir / "task_packet.json", task_packet.model_dump())
        atomic_write_json(run_dir / "manifest.json", manifest.model_dump())

        result = self.worker._finalize_run_review("test-project", run_id, accept=True, source="reviewer")
        self.assertEqual(result["accepted"], False)

        cards = store.load_cards()
        updated_card = next(c for c in cards if c.card_id == "card_ambig")
        self.assertEqual(updated_card.status, "needs_review")
        self.assertIn("歧义", updated_card.manager_review)
        self.assertIn("candidate", updated_card.manager_review)

        # Verify no accept side effects leaked through.
        graph = store.load_graph()
        created_assets = [a for a in graph.assets if a.created_by_run == run_id]
        self.assertTrue(len(created_assets) > 0, "Assets should be materialized as candidate")
        for asset in created_assets:
            self.assertEqual(asset.status, "candidate", f"Asset {asset.asset_id} should be candidate, not {asset.status}")

        created_claims = [c for c in graph.claims if c.created_by_run == run_id]
        self.assertEqual(len(created_claims), 0, "No claims should be created for ambiguous runs")

        report_items = [r for r in graph.report_items if r.item_id == f"report_{run_id}"]
        self.assertEqual(len(report_items), 0, "No report items should be written for ambiguous runs")

    def test_finalize_run_review_consistency_failure_has_no_accept_side_effects(self) -> None:
        # When preflight validation fails after mapping succeeds, NO accepted side effects
        # should leak into the persisted graph (claims, report items, linked_assets,
        # superseded assets, or rebound outputs).
        from app.models.runs import RunContext
        from app.services.utils import utc_now

        root = self.project_service.project_path("test-project")
        run_id = "run_consistency_fail"
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (root / "results").mkdir(parents=True, exist_ok=True)

        store = self.project_service.graph_store("test-project")
        card = Card(
            card_id="card_consistent",
            card_type="module",
            title="Consistency Test",
            status="running",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[CardOutputSpec(role="out", label="Out", artifact_class="table", accepted_formats=["tsv"])],
        )
        store.save_cards([card])
        graph = store.load_graph()
        graph.runs.append(
            RunRecord(
                run_id=run_id,
                card_id="card_consistent",
                status="running",
                title="Consistency test run",
                summary="test",
                started_at=utc_now(),
            )
        )
        store.save_graph(graph)

        task_packet = TaskPacket(
            task_id=run_id,
            project_id="test-project",
            card_id="card_consistent",
            card_title="Consistency Test",
            card_status="running",
            goal="test",
            input_assets=[],
            card_inputs=[],
            card_outputs=[],
            expected_outputs=[],
            allowed_paths=[],
            readonly_paths=[],
            forbidden_paths=[],
            execution_policy={},
            constraints=[],
            worker_instructions="",
            run_context=RunContext(run_id=run_id, worker_type="pi", project_root=str(root), run_dir=f"runs/{run_id}", result_dir="results"),
            executor_context={},
            manager_reporting_contract={},
        )
        manifest = Manifest(
            run_id=run_id,
            status="success",
            summary="test",
            created_assets=[CreatedAsset(path="results/out.tsv", role="out", artifact_class="table", format="tsv")],
            code_artifacts=[],
            key_findings=["finding 1"],
            validation_evidence={},
        )
        atomic_write_json(run_dir / "task_packet.json", task_packet.model_dump())
        atomic_write_json(run_dir / "manifest.json", manifest.model_dump())

        original_linked_assets = list(card.linked_assets)

        with patch.object(WorkerService, "_validate_acceptance_graph_consistent", return_value=["forced consistency error"]):
            result = self.worker._finalize_run_review("test-project", run_id, accept=True, source="reviewer")

        self.assertEqual(result["accepted"], False)

        cards = store.load_cards()
        updated_card = next(c for c in cards if c.card_id == "card_consistent")
        self.assertEqual(updated_card.status, "needs_review")

        graph = store.load_graph()
        created_assets = [a for a in graph.assets if a.created_by_run == run_id]
        self.assertTrue(len(created_assets) > 0)
        for asset in created_assets:
            self.assertEqual(asset.status, "candidate")

        created_claims = [c for c in graph.claims if c.created_by_run == run_id]
        self.assertEqual(len(created_claims), 0)

        report_items = [r for r in graph.report_items if r.item_id == f"report_{run_id}"]
        self.assertEqual(len(report_items), 0)

        self.assertEqual(updated_card.linked_assets, original_linked_assets)
        # Output should NOT have been rebound to the real asset id.
        self.assertIsNone(updated_card.outputs[0].asset_id)

    def test_validate_acceptance_graph_consistent_rejects_unbound_output(self) -> None:
        card = Card(
            card_id="c1",
            card_type="module",
            title="T",
            status="accepted",
            summary="s",
            outputs=[CardOutputSpec(role="out", label="Out", artifact_class="table")],
        )
        graph = GraphState()
        errors = WorkerService._validate_acceptance_graph_consistent(card, graph, "run_1")
        self.assertEqual(len(errors), 1)
        self.assertIn("has no asset_id", errors[0])
        with self.assertRaises(AssertionError):
            WorkerService._assert_acceptance_graph_consistent(card, graph, "run_1")

    def test_validate_acceptance_graph_consistent_rejects_candidate_output_asset(self) -> None:
        card = Card(
            card_id="c1",
            card_type="module",
            title="T",
            status="accepted",
            summary="s",
            outputs=[CardOutputSpec(role="out", label="Out", artifact_class="table", asset_id="a1", status="candidate")],
        )
        graph = GraphState(assets=[Asset(asset_id="a1", asset_type="table", title="A", status="candidate", path="p", summary="s")])
        errors = WorkerService._validate_acceptance_graph_consistent(card, graph, "run_1")
        self.assertEqual(len(errors), 1)
        self.assertIn("points to candidate asset", errors[0])
        with self.assertRaises(AssertionError):
            WorkerService._assert_acceptance_graph_consistent(card, graph, "run_1")

    def test_validate_acceptance_graph_consistent_passes_for_valid_bound_outputs(self) -> None:
        card = Card(
            card_id="c1",
            card_type="module",
            title="T",
            status="accepted",
            summary="s",
            outputs=[CardOutputSpec(role="out", label="Out", artifact_class="table", asset_id="a1", status="valid")],
        )
        graph = GraphState(assets=[Asset(asset_id="a1", asset_type="table", title="A", status="valid", path="p", summary="s")])
        errors = WorkerService._validate_acceptance_graph_consistent(card, graph, "run_1")
        self.assertEqual(errors, [])
        # Should not raise
        WorkerService._assert_acceptance_graph_consistent(card, graph, "run_1")

    def test_materialize_run_assets_does_not_demote_valid_to_candidate(self) -> None:
        # When re-reviewing an already-accepted run, valid assets must stay valid.
        graph = GraphState(
            assets=[Asset(asset_id="asset_run_1_out_1", asset_type="table", title="T", status="valid", path="p", summary="s", metadata={"role": "out"})]
        )
        card = Card(card_id="c1", card_type="module", title="T", status="accepted", summary="s")
        assets = WorkerService._materialize_run_assets(
            graph=graph,
            run_id="run_1",
            card=card,
            created_assets=[{"role": "out", "path": "p", "artifact_class": "table"}],
            status="candidate",
            input_asset_ids=[],
        )
        self.assertEqual(assets[0].status, "valid")

    def test_materialize_run_assets_sets_candidate_for_new_assets(self) -> None:
        graph = GraphState()
        card = Card(card_id="c1", card_type="module", title="T", status="accepted", summary="s")
        assets = WorkerService._materialize_run_assets(
            graph=graph,
            run_id="run_1",
            card=card,
            created_assets=[{"role": "out", "path": "p", "artifact_class": "table"}],
            status="candidate",
            input_asset_ids=[],
        )
        self.assertEqual(assets[0].status, "candidate")
        self.assertEqual(len(graph.assets), 1)

    def test_finalize_run_review_duplicate_role_bindings_written_by_index(self) -> None:
        # When a card has two outputs with the same role but different planned_asset_ids,
        # the index-based binding must write each real asset to the correct output slot.
        # The old role-based lookup would write both to the first matching slot.
        from app.models.runs import RunContext
        from app.services.utils import utc_now

        root = self.project_service.project_path("test-project")
        run_id = "run_dup_index"
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (root / "results").mkdir(parents=True, exist_ok=True)

        store = self.project_service.graph_store("test-project")
        card = Card(
            card_id="card_dup",
            card_type="module",
            title="Dup Index Test",
            status="running",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[
                CardOutputSpec(role="dup", label="D1", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_a"),
                CardOutputSpec(role="dup", label="D2", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_b"),
            ],
        )
        store.save_cards([card])
        graph = store.load_graph()
        graph.runs.append(
            RunRecord(
                run_id=run_id,
                card_id="card_dup",
                status="running",
                title="Dup run",
                summary="test",
                started_at=utc_now(),
            )
        )
        store.save_graph(graph)

        task_packet = TaskPacket(
            task_id=run_id,
            project_id="test-project",
            card_id="card_dup",
            card_title="Dup Index Test",
            card_status="running",
            goal="test",
            input_assets=[],
            card_inputs=[],
            card_outputs=[],
            expected_outputs=[],
            allowed_paths=[],
            readonly_paths=[],
            forbidden_paths=[],
            execution_policy={},
            constraints=[],
            worker_instructions="",
            run_context=RunContext(run_id=run_id, worker_type="pi", project_root=str(root), run_dir=f"runs/{run_id}", result_dir="results"),
            executor_context={},
            manager_reporting_contract={},
        )
        manifest = Manifest(
            run_id=run_id,
            status="success",
            summary="test",
            created_assets=[
                CreatedAsset(path="results/a.tsv", role="dup", artifact_class="table", format="tsv", asset_id="planned_a"),
                CreatedAsset(path="results/b.tsv", role="dup", artifact_class="table", format="tsv", asset_id="planned_b"),
            ],
            code_artifacts=[],
            key_findings=["finding 1"],
            validation_evidence={},
        )
        atomic_write_json(run_dir / "task_packet.json", task_packet.model_dump())
        atomic_write_json(run_dir / "manifest.json", manifest.model_dump())

        result = self.worker._finalize_run_review("test-project", run_id, accept=True, source="reviewer")
        self.assertTrue(result["accepted"], f"Expected accepted, got failure_reason={result.get('failure_reason')}, details={result.get('failure_details')}")

        cards = store.load_cards()
        updated_card = next(c for c in cards if c.card_id == "card_dup")
        self.assertEqual(updated_card.status, "accepted")
        # Both outputs must be bound to DIFFERENT real assets.
        self.assertIsNotNone(updated_card.outputs[0].asset_id)
        self.assertIsNotNone(updated_card.outputs[1].asset_id)
        self.assertNotEqual(updated_card.outputs[0].asset_id, updated_card.outputs[1].asset_id)

    def test_finalize_run_review_supersedes_all_duplicate_role_previous_outputs(self) -> None:
        from app.models.runs import RunContext
        from app.services.utils import utc_now

        root = self.project_service.project_path("test-project")
        run_id = "run_dup_rerun"
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (root / "results").mkdir(parents=True, exist_ok=True)

        store = self.project_service.graph_store("test-project")
        card = Card(
            card_id="card_dup_rerun",
            card_type="module",
            title="Dup Rerun Test",
            status="running",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[
                CardOutputSpec(role="dup", label="D1", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_a"),
                CardOutputSpec(role="dup", label="D2", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_b"),
            ],
            linked_assets=["old_a", "old_b"],
        )
        store.save_cards([card])
        graph = store.load_graph()
        graph.assets.extend(
            [
                Asset(asset_id="old_a", asset_type="table", title="Old A", status="valid", created_by_run="run_old", path="old_a.tsv", summary="old", metadata={"role": "dup"}),
                Asset(asset_id="old_b", asset_type="table", title="Old B", status="valid", created_by_run="run_old", path="old_b.tsv", summary="old", metadata={"role": "dup"}),
            ]
        )
        graph.runs.append(
            RunRecord(
                run_id=run_id,
                card_id="card_dup_rerun",
                status="running",
                title="Dup rerun",
                summary="test",
                started_at=utc_now(),
            )
        )
        store.save_graph(graph)

        task_packet = TaskPacket(
            task_id=run_id,
            project_id="test-project",
            card_id="card_dup_rerun",
            card_title="Dup Rerun Test",
            card_status="running",
            goal="test",
            input_assets=[],
            card_inputs=[],
            card_outputs=[],
            expected_outputs=[],
            allowed_paths=[],
            readonly_paths=[],
            forbidden_paths=[],
            execution_policy={},
            constraints=[],
            worker_instructions="",
            run_context=RunContext(run_id=run_id, worker_type="pi", project_root=str(root), run_dir=f"runs/{run_id}", result_dir="results"),
            executor_context={},
            manager_reporting_contract={},
        )
        manifest = Manifest(
            run_id=run_id,
            status="success",
            summary="test",
            created_assets=[
                CreatedAsset(path="results/a.tsv", role="dup", artifact_class="table", format="tsv", asset_id="planned_a"),
                CreatedAsset(path="results/b.tsv", role="dup", artifact_class="table", format="tsv", asset_id="planned_b"),
            ],
            code_artifacts=[],
            key_findings=[],
            validation_evidence={},
        )
        atomic_write_json(run_dir / "task_packet.json", task_packet.model_dump())
        atomic_write_json(run_dir / "manifest.json", manifest.model_dump())

        result = self.worker._finalize_run_review("test-project", run_id, accept=True, source="reviewer")
        self.assertTrue(result["accepted"], result)

        graph = store.load_graph()
        statuses = {asset.asset_id: asset.status for asset in graph.assets}
        self.assertEqual(statuses["old_a"], "superseded")
        self.assertEqual(statuses["old_b"], "superseded")

    def test_finalize_run_review_rerun_rewrites_concrete_planned_asset_alias(self) -> None:
        from app.models.runs import RunContext
        from app.services.utils import utc_now

        root = self.project_service.project_path("test-project")
        run_id = "run_rerun_alias_rewrite"
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (root / "results").mkdir(parents=True, exist_ok=True)

        store = self.project_service.graph_store("test-project")
        card = Card(
            card_id="card_rerun_alias",
            card_type="module",
            title="Rerun Alias Rewrite",
            status="running",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[
                CardOutputSpec(
                    role="out",
                    label="Out",
                    artifact_class="table",
                    accepted_formats=["tsv"],
                    asset_id="asset_prev_out",
                )
            ],
            linked_assets=["asset_prev_out"],
        )
        store.save_cards([card])
        graph = store.load_graph()
        graph.assets.append(
            Asset(
                asset_id="asset_prev_out",
                asset_type="table",
                title="Previous output",
                status="valid",
                created_by_run="run_prev",
                path="results/prev.tsv",
                summary="prev",
                metadata={"role": "out", "planned_asset_id": "planned_out"},
            )
        )
        graph.runs.append(
            RunRecord(
                run_id=run_id,
                card_id="card_rerun_alias",
                status="running",
                title="rerun alias rewrite",
                summary="test",
                started_at=utc_now(),
            )
        )
        store.save_graph(graph)

        task_packet = TaskPacket(
            task_id=run_id,
            project_id="test-project",
            card_id="card_rerun_alias",
            card_title="Rerun Alias Rewrite",
            card_status="running",
            goal="test",
            input_assets=[],
            card_inputs=[],
            card_outputs=[],
            expected_outputs=[],
            allowed_paths=[],
            readonly_paths=[],
            forbidden_paths=[],
            execution_policy={},
            constraints=[],
            worker_instructions="",
            run_context=RunContext(run_id=run_id, worker_type="pi", project_root=str(root), run_dir=f"runs/{run_id}", result_dir="results"),
            executor_context={},
            manager_reporting_contract={},
        )
        manifest = Manifest(
            run_id=run_id,
            status="success",
            summary="test",
            created_assets=[
                CreatedAsset(
                    path="results/out.tsv",
                    role="out",
                    artifact_class="table",
                    format="tsv",
                    asset_id="asset_prev_out",
                )
            ],
            code_artifacts=[],
            key_findings=[],
            validation_evidence={},
        )
        atomic_write_json(run_dir / "task_packet.json", task_packet.model_dump())
        atomic_write_json(run_dir / "manifest.json", manifest.model_dump())

        result = self.worker._finalize_run_review("test-project", run_id, accept=True, source="reviewer")
        self.assertTrue(result["accepted"], result)

        graph = store.load_graph()
        new_asset = next(asset for asset in graph.assets if asset.created_by_run == run_id and asset.metadata.get("role") == "out")
        self.assertEqual("planned_out", new_asset.metadata.get("planned_asset_id"))
        self.assertNotEqual("asset_prev_out", new_asset.metadata.get("planned_asset_id"))

    def test_finalize_run_review_consistency_failure_reason_not_mapping_ambiguous(self) -> None:
        # Preflight consistency failure must report failure_reason="consistency_failed"
        # and the manager_review must mention "一致性检查失败", NOT "映射存在歧义".
        from app.models.runs import RunContext
        from app.services.utils import utc_now

        root = self.project_service.project_path("test-project")
        run_id = "run_consistency_reason"
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (root / "results").mkdir(parents=True, exist_ok=True)

        store = self.project_service.graph_store("test-project")
        card = Card(
            card_id="card_reason",
            card_type="module",
            title="Reason Test",
            status="running",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[CardOutputSpec(role="out", label="Out", artifact_class="table", accepted_formats=["tsv"])],
        )
        store.save_cards([card])
        graph = store.load_graph()
        graph.runs.append(
            RunRecord(
                run_id=run_id,
                card_id="card_reason",
                status="running",
                title="Reason run",
                summary="test",
                started_at=utc_now(),
            )
        )
        store.save_graph(graph)

        task_packet = TaskPacket(
            task_id=run_id,
            project_id="test-project",
            card_id="card_reason",
            card_title="Reason Test",
            card_status="running",
            goal="test",
            input_assets=[],
            card_inputs=[],
            card_outputs=[],
            expected_outputs=[],
            allowed_paths=[],
            readonly_paths=[],
            forbidden_paths=[],
            execution_policy={},
            constraints=[],
            worker_instructions="",
            run_context=RunContext(run_id=run_id, worker_type="pi", project_root=str(root), run_dir=f"runs/{run_id}", result_dir="results"),
            executor_context={},
            manager_reporting_contract={},
        )
        manifest = Manifest(
            run_id=run_id,
            status="success",
            summary="test",
            created_assets=[CreatedAsset(path="results/out.tsv", role="out", artifact_class="table", format="tsv")],
            code_artifacts=[],
            key_findings=["finding 1"],
            validation_evidence={},
        )
        atomic_write_json(run_dir / "task_packet.json", task_packet.model_dump())
        atomic_write_json(run_dir / "manifest.json", manifest.model_dump())

        with patch.object(WorkerService, "_validate_acceptance_graph_consistent", return_value=["forced error"]):
            result = self.worker._finalize_run_review("test-project", run_id, accept=True, source="reviewer")

        self.assertEqual(result["accepted"], False)
        self.assertEqual(result.get("failure_reason"), "consistency_failed")
        self.assertEqual(result.get("failure_details"), ["forced error"])

        cards = store.load_cards()
        updated_card = next(c for c in cards if c.card_id == "card_reason")
        self.assertIn("一致性检查失败", updated_card.manager_review)
        self.assertNotIn("映射存在歧义", updated_card.manager_review)

    def test_system_outputs_do_not_block_card_output_acceptance(self) -> None:
        from app.models.runs import ExpectedOutput, RunContext
        from app.services.utils import utc_now

        root = self.project_service.project_path("test-project")
        run_id = "run_system_outputs"
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        store = self.project_service.graph_store("test-project")
        card = Card(
            card_id="card_system_outputs",
            card_type="module",
            title="System Outputs",
            status="running",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[
                CardOutputSpec(role="out", label="Out", artifact_class="table", accepted_formats=["tsv"], asset_id="planned_out"),
                CardOutputSpec(role="run_summary", label="Run summary", artifact_class="document", accepted_formats=["md"], asset_id="planned_summary"),
                CardOutputSpec(role="run_preview", label="Run preview", artifact_class="figure", accepted_formats=["svg"], asset_id="planned_preview"),
            ],
        )
        store.save_cards([card])
        graph = store.load_graph()
        graph.runs.append(
            RunRecord(
                run_id=run_id,
                card_id=card.card_id,
                status="running",
                title="System output run",
                summary="test",
                started_at=utc_now(),
            )
        )
        store.save_graph(graph)
        task_packet = TaskPacket(
            task_id=run_id,
            project_id="test-project",
            card_id=card.card_id,
            card_title=card.title,
            card_status="running",
            goal="test",
            input_assets=[],
            card_inputs=[],
            card_outputs=[],
            expected_outputs=[
                ExpectedOutput(role="out", label="Out", artifact_class="table", accepted_formats=["tsv"], path_hint="results/out.tsv", asset_id="planned_out"),
                ExpectedOutput(role="run_summary", label="Run summary", artifact_class="document", accepted_formats=["md"], path_hint="results/run_summary.md", asset_id="planned_summary"),
                ExpectedOutput(role="run_preview", label="Run preview", artifact_class="figure", accepted_formats=["svg"], path_hint="results/run_preview.svg", asset_id="planned_preview"),
            ],
            allowed_paths=[],
            readonly_paths=[],
            forbidden_paths=[],
            execution_policy={},
            constraints=[],
            worker_instructions="",
            run_context=RunContext(run_id=run_id, worker_type="pi", project_root=str(root), run_dir=f"runs/{run_id}", result_dir="results"),
            executor_context={},
            manager_reporting_contract={},
        )
        manifest = Manifest(
            run_id=run_id,
            status="success",
            summary="test",
            created_assets=[
                CreatedAsset(path="results/out.tsv", role="out", asset_id="planned_out", artifact_class="table", format="tsv"),
                CreatedAsset(path="results/run_summary.md", role="run_summary", asset_id="planned_summary", artifact_class="document", format="md"),
                CreatedAsset(path="results/run_preview.svg", role="run_preview", asset_id="planned_preview", artifact_class="figure", format="svg"),
            ],
            code_artifacts=[],
            key_findings=["finding 1"],
            validation_evidence={},
        )
        atomic_write_json(run_dir / "task_packet.json", task_packet.model_dump())
        atomic_write_json(run_dir / "manifest.json", manifest.model_dump())

        result = self.worker._finalize_run_review("test-project", run_id, accept=True, source="reviewer")

        self.assertTrue(result["accepted"], result)
        updated_card = next(c for c in store.load_cards() if c.card_id == card.card_id)
        self.assertEqual("accepted", updated_card.status)
        # All logical output ids are preserved; materialization bindings are written.
        self.assertEqual("planned_out", updated_card.outputs[0].asset_id)
        self.assertEqual("planned_summary", updated_card.outputs[1].asset_id)
        self.assertEqual("planned_preview", updated_card.outputs[2].asset_id)

    def test_review_run_event_messages_branch_by_failure_reason(self) -> None:
        # review_run() must emit different event messages for each failure reason.
        from app.models.runs import RunContext
        from app.services.utils import utc_now

        root = self.project_service.project_path("test-project")
        run_id = "run_event_msg"
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (root / "results").mkdir(parents=True, exist_ok=True)

        store = self.project_service.graph_store("test-project")
        card = Card(
            card_id="card_event",
            card_type="module",
            title="Event Test",
            status="running",
            step=1,
            summary="",
            why="",
            inputs=[],
            outputs=[CardOutputSpec(role="out", label="Out", artifact_class="table", accepted_formats=["tsv"])],
        )
        store.save_cards([card])
        graph = store.load_graph()
        graph.runs.append(
            RunRecord(
                run_id=run_id,
                card_id="card_event",
                status="running",
                title="Event run",
                summary="test",
                started_at=utc_now(),
            )
        )
        store.save_graph(graph)

        task_packet = TaskPacket(
            task_id=run_id,
            project_id="test-project",
            card_id="card_event",
            card_title="Event Test",
            card_status="running",
            goal="test",
            input_assets=[],
            card_inputs=[],
            card_outputs=[],
            expected_outputs=[],
            allowed_paths=[],
            readonly_paths=[],
            forbidden_paths=[],
            execution_policy={},
            constraints=[],
            worker_instructions="",
            run_context=RunContext(run_id=run_id, worker_type="pi", project_root=str(root), run_dir=f"runs/{run_id}", result_dir="results"),
            executor_context={},
            manager_reporting_contract={},
        )
        manifest = Manifest(
            run_id=run_id,
            status="success",
            summary="test",
            created_assets=[CreatedAsset(path="results/out.tsv", role="out", artifact_class="table", format="tsv")],
            code_artifacts=[],
            key_findings=["finding 1"],
            validation_evidence={},
        )
        atomic_write_json(run_dir / "task_packet.json", task_packet.model_dump())
        atomic_write_json(run_dir / "manifest.json", manifest.model_dump())

        # Case 1: explicit reject -> event says "已拒绝"
        with patch.object(self.manifest_service, "validate_manifest", return_value=(True, [])):
            result = self.worker.review_run("test-project", run_id, accept=False)
        self.assertFalse(result["accepted"])
        events = store.load_run_events(run_id)
        reject_events = [e for e in events if e.event_type == "manager_review" and "已拒绝" in e.message]
        self.assertTrue(len(reject_events) > 0, f"Expected reject event, got: {[e.message for e in events if e.event_type == 'manager_review']}")

        # Reset
        card.status = "running"
        store.save_cards([card])
        graph = store.load_graph()
        run = next(r for r in graph.runs if r.run_id == run_id)
        run.status = "running"
        store.save_graph(graph)
        store.save_run_events(run_id, [])

        # Case 2: consistency failure -> event says "一致性检查未通过"
        with patch.object(self.manifest_service, "validate_manifest", return_value=(True, [])):
            with patch.object(WorkerService, "_validate_acceptance_graph_consistent", return_value=["forced error"]):
                result = self.worker.review_run("test-project", run_id, accept=True)
        self.assertFalse(result["accepted"])
        events = store.load_run_events(run_id)
        consistency_events = [e for e in events if e.event_type == "manager_review" and "一致性检查未通过" in e.message]
        self.assertTrue(len(consistency_events) > 0, f"Expected consistency event, got: {[e.message for e in events if e.event_type == 'manager_review']}")


if __name__ == "__main__":
    unittest.main()
