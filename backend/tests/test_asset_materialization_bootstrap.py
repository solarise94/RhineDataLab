import shutil
import tempfile
import unittest
from pathlib import Path

from app.core.config import get_settings
from app.models.cards import Card
from app.models.graph import Asset, GraphState, RunRecord
from app.models.output_contracts import CardOutputSpec
from app.services.asset_materialization_service import AssetMaterializationService
from app.services.project_service import ProjectService


def _output(role: str, asset_id: str) -> CardOutputSpec:
    return CardOutputSpec(
        role=role,
        label=role,
        artifact_class="table",
        accepted_formats=["tsv"],
        preferred_format="tsv",
        asset_id=asset_id,
    )


def _card(card_id: str, outputs: list[CardOutputSpec]) -> Card:
    return Card(
        card_id=card_id,
        card_type="module",
        title=card_id,
        status="accepted",
        step=1,
        summary=card_id,
        inputs=[],
        outputs=outputs,
    )


def _asset(asset_id: str, *, planned: str, run_id: str, status: str = "valid") -> Asset:
    return Asset(
        asset_id=asset_id,
        asset_type="table",
        title=asset_id,
        status=status,
        created_by_run=run_id,
        path=f"results/{asset_id}.tsv",
        summary=asset_id,
        metadata={"planned_asset_id": planned},
    )


def _run(run_id: str, card_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        card_id=card_id,
        status="reviewed",
        title=run_id,
        summary=run_id,
        started_at="2026-05-28T00:00:00Z",
    )


class BootstrapReportTest(unittest.TestCase):
    def test_unambiguous_alias_reports_created_only(self) -> None:
        graph = GraphState(
            assets=[_asset("deg_table_run1", planned="deg_table", run_id="run_1")],
            runs=[_run("run_1", "de")],
            metadata={},
        )
        cards = [_card("de", [_output("table", "deg_table")])]

        report = AssetMaterializationService.bootstrap_from_aliases(graph, cards)

        self.assertEqual(report.created, ["deg_table"])
        self.assertEqual(report.ambiguous, [])
        binding = graph.metadata["asset_materializations"]["deg_table"]
        self.assertEqual(binding["current_asset_id"], "deg_table_run1")

    def test_two_runs_same_alias_reports_ambiguous_guess(self) -> None:
        # Two runs produced the same logical alias; the binding must be flagged
        # ambiguous because it could resolve to the wrong concrete asset.
        graph = GraphState(
            assets=[
                _asset("deg_table_run1", planned="deg_table", run_id="run_1", status="superseded"),
                _asset("deg_table_run2", planned="deg_table", run_id="run_2", status="valid"),
            ],
            runs=[_run("run_1", "de"), _run("run_2", "de")],
            metadata={},
        )
        cards = [_card("de", [_output("table", "deg_table")])]

        report = AssetMaterializationService.bootstrap_from_aliases(graph, cards)

        self.assertEqual(report.created, ["deg_table"])
        self.assertEqual(len(report.ambiguous), 1)
        entry = report.ambiguous[0]
        self.assertEqual(entry["planned_asset_id"], "deg_table")
        self.assertEqual(entry["chosen_asset_id"], "deg_table_run2")  # valid beats superseded
        self.assertCountEqual(
            entry["candidate_asset_ids"], ["deg_table_run1", "deg_table_run2"]
        )

    def test_existing_binding_is_not_overwritten(self) -> None:
        graph = GraphState(
            assets=[_asset("deg_table_run1", planned="deg_table", run_id="run_1")],
            runs=[_run("run_1", "de")],
            metadata={
                "asset_materializations": {
                    "deg_table": {"planned_asset_id": "deg_table", "current_asset_id": "pinned"}
                }
            },
        )
        cards = [_card("de", [_output("table", "deg_table")])]

        report = AssetMaterializationService.bootstrap_from_aliases(graph, cards)

        self.assertEqual(report.created, [])
        self.assertEqual(report.ambiguous, [])
        self.assertEqual(
            graph.metadata["asset_materializations"]["deg_table"]["current_asset_id"], "pinned"
        )

    def test_empty_project_reports_nothing_and_writes_no_marker(self) -> None:
        graph = GraphState(metadata={})
        report = AssetMaterializationService.bootstrap_from_aliases(graph, [])
        self.assertEqual(report.created, [])
        self.assertEqual(report.ambiguous, [])
        # The vestigial "bootstrapped_at" marker is no longer written: reads do
        # not persist, so it never affected anything.
        self.assertNotIn("asset_materializations_bootstrapped_at", graph.metadata)


class SnapshotReadIdempotencyTest(unittest.TestCase):
    """A snapshot read must derive bindings in-memory without persisting graph.json."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="materialization-idempotency-test-")
        settings = get_settings()
        self._original_data_root = settings.data_root
        settings.data_root = Path(self.tmpdir)
        self.project_service = ProjectService()
        self.project_service.create_project(project_id="legacy", name="Legacy", current_goal="test")
        # Overwrite with a legacy graph: aliased assets, no materialization map.
        store = self.project_service.graph_store("legacy")
        store.save_graph(
            GraphState(
                assets=[_asset("deg_table_run1", planned="deg_table", run_id="run_1")],
                runs=[_run("run_1", "de")],
                metadata={"schema_version": settings.schema_version},
            )
        )
        store.save_cards([_card("de", [_output("table", "deg_table")])])
        self.graph_path = store.root / "graph" / "graph.json"

    def tearDown(self) -> None:
        get_settings().data_root = self._original_data_root
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_project_snapshot_does_not_persist_bootstrap(self) -> None:
        before = self.graph_path.read_bytes()
        snap = self.project_service.get_project_snapshot("legacy")
        after = self.graph_path.read_bytes()

        # Read is idempotent: graph.json on disk is untouched.
        self.assertEqual(before, after)
        self.assertNotIn("asset_materializations", before.decode("utf-8"))
        # But the response still surfaces the derived binding for display.
        binding = snap["graph"].metadata["asset_materializations"]["deg_table"]
        self.assertEqual(binding["current_asset_id"], "deg_table_run1")

    def test_get_project_snapshot_core_does_not_persist_bootstrap(self) -> None:
        before = self.graph_path.read_bytes()
        snap = self.project_service.get_project_snapshot_core("legacy")
        after = self.graph_path.read_bytes()

        self.assertEqual(before, after)
        binding = snap["graph"].metadata["asset_materializations"]["deg_table"]
        self.assertEqual(binding["current_asset_id"], "deg_table_run1")


if __name__ == "__main__":
    unittest.main()
