"""Tests for subgraph/batch run scheduling (docs/66 §4 form 1)."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import HTTPException

from app.core.config import Settings, get_settings
from app.models.cards import Card
from app.models.graph import Asset, GraphState, RunRecord
from app.models.output_contracts import CardOutputSpec
from app.services.app_config_service import AppConfigService
from app.services.project_service import ProjectService
from app.services.subgraph_run_service import SubgraphRunService
from app.services.worker_service import WorkerService


def output(role: str, asset_id: str | None = None) -> CardOutputSpec:
    return CardOutputSpec(
        role=role,
        label=role,
        artifact_class="table",
        accepted_formats=["tsv"],
        preferred_format="tsv",
        asset_id=asset_id,
    )


def card(
    card_id: str,
    *,
    status: str = "planned",
    inputs: list[dict] | None = None,
    outputs: list[CardOutputSpec] | None = None,
) -> Card:
    return Card(
        card_id=card_id,
        card_type="module",
        title=card_id,
        status=status,
        step=1,
        summary=card_id,
        inputs=inputs or [],
        outputs=outputs or [],
        linked_modules=[],
    )


def asset(asset_id: str, *, status: str = "valid", run_id: str | None = None) -> Asset:
    return Asset(
        asset_id=asset_id,
        asset_type="table",
        title=asset_id,
        status=status,
        created_by_run=run_id,
        path=f"results/{asset_id}.tsv",
        summary=asset_id,
        depends_on=[],
    )


def run(run_id: str, card_id: str, *, status: str = "reviewed") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        card_id=card_id,
        status=status,
        title=run_id,
        summary=run_id,
        started_at="2026-05-28T00:00:00Z",
    )


class FakeWorkerService:
    """Stand-in for WorkerService that records calls and simulates success."""

    def __init__(self) -> None:
        self.rerun_calls: list[tuple[str, str]] = []
        self.start_calls: list[tuple[str, str]] = []
        self._run_counter = 0

    def rerun_card(self, project_id: str, card_id: str, **kwargs) -> dict:
        self.rerun_calls.append((project_id, card_id))
        self._run_counter += 1
        return {"run_id": f"run_{card_id}_{self._run_counter}", "status": "queued"}

    def start_run(self, project_id: str, card_id: str, **kwargs) -> dict:
        self.start_calls.append((project_id, card_id))
        self._run_counter += 1
        return {"run_id": f"run_{card_id}_{self._run_counter}", "status": "queued"}


class TestSubgraphRunService(unittest.TestCase):
    def setUp(self):
        self._original_data_root = get_settings().data_root
        self.data_root = Path(tempfile.mkdtemp())
        self.settings = Settings(data_root=self.data_root)
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()
        get_settings().data_root = self._original_data_root

    def _project_store(self, cards: list[Card], assets: list[Asset], runs: list[RunRecord]) -> Path:
        project_id = "test-project"
        with unittest.mock.patch("app.services.project_service.get_settings", return_value=self.settings):
            project_service = ProjectService()
        project_service.create_project(project_id, "Test", "test goal")
        store = project_service.graph_store(project_id)
        graph = store.load_graph()
        graph.assets = assets
        graph.runs = runs
        store.save_graph(graph)
        store.save_cards(cards)
        return project_service

    def test_compute_subgraph_topological_order(self):
        """form-1 closure from A should include A, B, C in dependency order."""
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="accepted", inputs=[{"label": "table", "asset_id": "a_out"}], outputs=[output("table", "b_out")])
        c = card("C", status="accepted", inputs=[{"label": "table", "asset_id": "b_out"}], outputs=[output("table", "c_out")])
        assets = [
            asset("a_out", run_id="run_a"),
            asset("b_out", run_id="run_b"),
            asset("c_out", run_id="run_c"),
        ]
        project_service = self._project_store([a, b, c], assets, [run("run_a", "A"), run("run_b", "B"), run("run_c", "C")])
        fake_worker = FakeWorkerService()
        service = SubgraphRunService(project_service, fake_worker)

        result = service.start_from_card("test-project", "A")

        self.assertEqual(result["planned_cards"], ["A", "B", "C"])
        self.assertTrue(result["batch_run_id"].startswith("batch_"))
        # Give scheduler a moment to start A.
        time.sleep(0.2)
        self.assertEqual(fake_worker.rerun_calls, [("test-project", "A")])

    def test_compute_subgraph_excludes_unrelated_branches(self):
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="accepted", inputs=[{"label": "table", "asset_id": "a_out"}])
        c = card("C", status="accepted", outputs=[output("table", "c_out")])
        project_service = self._project_store(
            [a, b, c],
            [asset("a_out", run_id="run_a"), asset("c_out", run_id="run_c")],
            [run("run_a", "A"), run("run_c", "C")],
        )
        fake_worker = FakeWorkerService()
        service = SubgraphRunService(project_service, fake_worker)

        result = service.start_from_card("test-project", "A")

        self.assertEqual(result["planned_cards"], ["A", "B"])
        self.assertNotIn("C", result["planned_cards"])

    def test_cycle_detection_raises(self):
        a = card("A", status="accepted", inputs=[{"label": "table", "asset_id": "c_out"}], outputs=[output("table", "a_out")])
        b = card("B", status="accepted", inputs=[{"label": "table", "asset_id": "a_out"}], outputs=[output("table", "b_out")])
        c = card("C", status="accepted", inputs=[{"label": "table", "asset_id": "b_out"}], outputs=[output("table", "c_out")])
        project_service = self._project_store(
            [a, b, c],
            [asset("a_out"), asset("b_out"), asset("c_out")],
            [],
        )
        fake_worker = FakeWorkerService()
        service = SubgraphRunService(project_service, fake_worker)

        with self.assertRaises(HTTPException) as ctx:
            service.start_from_card("test-project", "A")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail.get("error_code"), "subgraph_cycle")

    def test_start_card_missing_raises_404(self):
        project_service = self._project_store([], [], [])
        fake_worker = FakeWorkerService()
        service = SubgraphRunService(project_service, fake_worker)

        with self.assertRaises(HTTPException) as ctx:
            service.start_from_card("test-project", "missing")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_persists_batch_state(self):
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="accepted", inputs=[{"label": "table", "asset_id": "a_out"}])
        project_service = self._project_store(
            [a, b],
            [asset("a_out", run_id="run_a")],
            [run("run_a", "A")],
        )
        fake_worker = FakeWorkerService()
        service = SubgraphRunService(project_service, fake_worker)

        result = service.start_from_card("test-project", "A")
        batch_run_id = result["batch_run_id"]

        state = service.get_batch_state("test-project", batch_run_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["planned_cards"], ["A", "B"])
        self.assertEqual(state["status"], "running")


class TestRerunCardPropagate(unittest.TestCase):
    """Verify rerun_card accepts and applies propagate parameter."""

    def setUp(self):
        self._original_data_root = get_settings().data_root
        self.data_root = Path(tempfile.mkdtemp())
        self.settings = Settings(data_root=self.data_root)
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()
        get_settings().data_root = self._original_data_root

    def test_rerun_card_propagate_none_does_not_invalidate_downstream(self):
        project_id = "test-project"
        with unittest.mock.patch("app.services.project_service.get_settings", return_value=self.settings):
            project_service = ProjectService()
        project_service.create_project(project_id, "Test", "test goal")
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="accepted", inputs=[{"label": "table", "asset_id": "a_out"}])
        store = project_service.graph_store(project_id)
        store.save_cards([a, b])
        store.save_graph(GraphState(assets=[asset("a_out", run_id="run_a")], runs=[run("run_a", "A")]))

        worker_service = WorkerService(
            project_service=project_service,
            manifest_service=MagicMock(),
            runtime_approval_service=MagicMock(),
        )
        # Mock start_run so we do not launch a real executor thread.
        worker_service.start_run = MagicMock(return_value={"run_id": "run_x", "status": "queued"})  # type: ignore[method-assign]

        result = worker_service.rerun_card(project_id, "A", propagate="none")

        self.assertEqual(result["run_id"], "run_x")
        # A is reset to planned; B should remain accepted because propagate=none.
        cards = {c.card_id: c for c in store.load_cards()}
        self.assertEqual(cards["A"].status, "planned")
        self.assertEqual(cards["B"].status, "accepted")

    def test_rerun_card_default_propagate_all_invalidates_downstream(self):
        project_id = "test-project"
        with unittest.mock.patch("app.services.project_service.get_settings", return_value=self.settings):
            project_service = ProjectService()
        project_service.create_project(project_id, "Test", "test goal")
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="accepted", inputs=[{"label": "table", "asset_id": "a_out"}])
        store = project_service.graph_store(project_id)
        store.save_cards([a, b])
        store.save_graph(GraphState(assets=[asset("a_out", run_id="run_a")], runs=[run("run_a", "A")]))

        worker_service = WorkerService(
            project_service=project_service,
            manifest_service=MagicMock(),
            runtime_approval_service=MagicMock(),
        )
        worker_service.start_run = MagicMock(return_value={"run_id": "run_x", "status": "queued"})  # type: ignore[method-assign]

        worker_service.rerun_card(project_id, "A")

        cards = {c.card_id: c for c in store.load_cards()}
        self.assertEqual(cards["A"].status, "planned")
        self.assertEqual(cards["B"].status, "stale")


if __name__ == "__main__":
    unittest.main()
