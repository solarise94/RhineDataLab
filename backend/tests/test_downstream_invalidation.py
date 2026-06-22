"""Tests for DownstreamInvalidationService push-time invalidation."""

from __future__ import annotations

import unittest

from app.models.cards import Card
from app.models.graph import Asset, Claim, GraphState, RunRecord
from app.models.output_contracts import CardOutputSpec
from app.services.downstream_invalidation_service import DownstreamInvalidationService


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
    linked_modules: list[str] | None = None,
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
        linked_modules=linked_modules or [],
    )


def asset(
    asset_id: str,
    *,
    status: str = "valid",
    run_id: str | None = None,
    role: str | None = None,
    depends_on: list[str] | None = None,
) -> Asset:
    metadata = {"role": role} if role else {}
    return Asset(
        asset_id=asset_id,
        asset_type="table",
        title=asset_id,
        status=status,
        created_by_run=run_id,
        path=f"results/{asset_id}.tsv",
        summary=asset_id,
        depends_on=depends_on or [],
        metadata=metadata,
    )


def run(run_id: str, card_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        card_id=card_id,
        status="reviewed",
        title=run_id,
        summary=run_id,
        started_at="2026-05-28T00:00:00Z",
    )


class TestDownstreamInvalidationService(unittest.TestCase):
    def setUp(self) -> None:
        self.service = DownstreamInvalidationService()

    def _graph(
        self,
        cards: list[Card],
        assets: list[Asset],
        runs: list[RunRecord],
        claims: list[Claim] | None = None,
    ) -> GraphState:
        return GraphState(
            modules=[],
            assets=assets,
            claims=claims or [],
            runs=runs,
        )

    def test_invalidate_marks_accepted_downstream_cards_stale(self):
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="accepted", inputs=[{"label": "table", "asset_id": "a_out"}], outputs=[output("table", "b_out")])
        c = card("C", status="accepted", inputs=[{"label": "table", "asset_id": "b_out"}], outputs=[output("table", "c_out")])
        assets = [
            asset("a_out", run_id="run_a", role="table"),
            asset("b_out", run_id="run_b", role="table"),
            asset("c_out", run_id="run_c", role="table"),
        ]
        graph = self._graph([a, b, c], assets, [run("run_a", "A"), run("run_b", "B"), run("run_c", "C")])

        result = DownstreamInvalidationService.invalidate_from(graph, [a, b, c], "A")

        self.assertEqual(sorted(result["invalidated_card_ids"]), ["B", "C"])
        self.assertEqual(b.status, "stale")
        self.assertEqual(c.status, "stale")
        self.assertEqual(a.status, "accepted")

    def test_invalidate_marks_output_assets_stale(self):
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="accepted", inputs=[{"label": "table", "asset_id": "a_out"}], outputs=[output("table", "b_out")])
        assets = [
            asset("a_out", run_id="run_a", role="table"),
            asset("b_out", run_id="run_b", role="table", status="valid"),
        ]
        graph = self._graph([a, b], assets, [run("run_a", "A"), run("run_b", "B")])

        DownstreamInvalidationService.invalidate_from(graph, [a, b], "A")

        self.assertEqual(graph.assets[1].status, "stale")
        self.assertEqual(graph.assets[0].status, "valid")

    def test_invalidate_respects_max_depth(self):
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="accepted", inputs=[{"label": "table", "asset_id": "a_out"}], outputs=[output("table", "b_out")])
        c = card("C", status="accepted", inputs=[{"label": "table", "asset_id": "b_out"}], outputs=[output("table", "c_out")])
        assets = [
            asset("a_out", run_id="run_a", role="table"),
            asset("b_out", run_id="run_b", role="table"),
            asset("c_out", run_id="run_c", role="table"),
        ]
        graph = self._graph([a, b, c], assets, [run("run_a", "A"), run("run_b", "B"), run("run_c", "C")])

        result = DownstreamInvalidationService.invalidate_from(graph, [a, b, c], "A", max_depth=1)

        self.assertEqual(result["invalidated_card_ids"], ["B"])
        self.assertEqual(b.status, "stale")
        self.assertEqual(c.status, "accepted")

    def test_invalidate_does_not_touch_running_or_reviewing_cards(self):
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="running", inputs=[{"label": "table", "asset_id": "a_out"}], outputs=[output("table", "b_out")])
        c = card("C", status="reviewing", inputs=[{"label": "table", "asset_id": "a_out"}], outputs=[output("table", "c_out")])
        assets = [
            asset("a_out", run_id="run_a", role="table"),
            asset("b_out", run_id="run_b", role="table"),
            asset("c_out", run_id="run_c", role="table"),
        ]
        graph = self._graph([a, b, c], assets, [run("run_a", "A"), run("run_b", "B"), run("run_c", "C")])

        result = DownstreamInvalidationService.invalidate_from(graph, [a, b, c], "A")

        self.assertEqual(result["invalidated_card_ids"], [])
        self.assertEqual(b.status, "running")
        self.assertEqual(c.status, "reviewing")

    def test_invalidate_does_not_touch_planned_or_failed_cards(self):
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="planned", inputs=[{"label": "table", "asset_id": "a_out"}], outputs=[output("table", "b_out")])
        c = card("C", status="failed", inputs=[{"label": "table", "asset_id": "a_out"}], outputs=[output("table", "c_out")])
        assets = [
            asset("a_out", run_id="run_a", role="table"),
            asset("b_out", run_id="run_b", role="table"),
            asset("c_out", run_id="run_c", role="table"),
        ]
        graph = self._graph([a, b, c], assets, [run("run_a", "A"), run("run_b", "B"), run("run_c", "C")])

        result = DownstreamInvalidationService.invalidate_from(graph, [a, b, c], "A")

        self.assertEqual(result["invalidated_card_ids"], [])
        self.assertEqual(b.status, "planned")
        self.assertEqual(c.status, "failed")

    def test_invalidate_marks_claims_depending_on_stale_assets_stale(self):
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="accepted", inputs=[{"label": "table", "asset_id": "a_out"}], outputs=[output("table", "b_out")])
        assets = [
            asset("a_out", run_id="run_a", role="table"),
            asset("b_out", run_id="run_b", role="table"),
        ]
        claim = Claim(claim_id="claim_1", text="claim", status="valid", depends_on_assets=["b_out"])
        graph = self._graph([a, b], assets, [run("run_a", "A"), run("run_b", "B")], claims=[claim])

        result = DownstreamInvalidationService.invalidate_from(graph, [a, b], "A")

        self.assertIn("claim_1", result["invalidated_claim_ids"])
        self.assertEqual(claim.status, "stale")

    def test_invalidate_transitive_asset_staleness_via_depends_on(self):
        a = card("A", status="accepted", outputs=[output("table", "a_out")])
        b = card("B", status="accepted", inputs=[{"label": "table", "asset_id": "a_out"}], outputs=[output("table", "b_out")])
        derived = asset("derived", depends_on=["b_out"], status="valid")
        assets = [
            asset("a_out", run_id="run_a", role="table"),
            asset("b_out", run_id="run_b", role="table"),
            derived,
        ]
        graph = self._graph([a, b], assets, [run("run_a", "A"), run("run_b", "B")])

        result = DownstreamInvalidationService.invalidate_from(graph, [a, b], "A")

        self.assertIn("derived", result["invalidated_asset_ids"])
        self.assertEqual(derived.status, "stale")

    def test_parse_propagate_all(self):
        self.assertEqual(
            DownstreamInvalidationService.parse_propagate("all"),
            {"enabled": True, "max_depth": None},
        )

    def test_parse_propagate_none(self):
        self.assertEqual(
            DownstreamInvalidationService.parse_propagate("none"),
            {"enabled": False, "max_depth": None},
        )

    def test_parse_propagate_depth(self):
        self.assertEqual(
            DownstreamInvalidationService.parse_propagate("depth:2"),
            {"enabled": True, "max_depth": 2},
        )

    def test_parse_propagate_rejects_invalid(self):
        for bad in ["depth:abc", "depth:-1", "other"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    DownstreamInvalidationService.parse_propagate(bad)


if __name__ == "__main__":
    unittest.main()
