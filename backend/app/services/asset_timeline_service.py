from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import re

from app.models.cards import Card, CardAssetRef
from app.models.output_contracts import CardOutputSpec
from app.models.graph import Asset, GraphState, Module, RunRecord


TERMINAL_CARD_STATUSES = {"accepted", "rejected", "cancelled"}
STARTABLE_CARD_STATUSES = {"planned", "failed", "stale", "superseded"}


@dataclass(frozen=True)
class CardAssetUse:
    asset_id: str
    label: str


class AssetTimelineService:
    """Derive card/asset order from card inputs and outputs."""

    def build(self, project_id: str, snapshot: dict) -> dict:
        cards: list[Card] = snapshot["cards"]
        graph: GraphState = snapshot["graph"]
        asset_map = {asset.asset_id: asset for asset in graph.assets}
        module_map = {module.module_id: module for module in graph.modules}
        producer_by_asset, producer_source, duplicate_outputs = self.producer_maps(cards, graph.assets, graph.runs)
        produced_assets_by_card = self.produced_assets_by_card(cards, graph.assets, graph.runs)
        required_uses_by_card = {
            card.card_id: self.required_asset_uses(card, module_map, asset_map)
            for card in cards
        }
        dependency_map = self.card_dependency_map(cards, required_uses_by_card, producer_by_asset)
        parallel_batches, cycle_card_ids = self.parallel_batches({card.card_id for card in cards}, dependency_map)

        card_steps = self.card_steps(cards)
        asset_steps = self.asset_steps(graph.assets, cards, card_steps, producer_by_asset)
        consumers_by_asset: dict[str, list[str]] = defaultdict(list)
        for card_id, uses in required_uses_by_card.items():
            for use in uses:
                if card_id not in consumers_by_asset[use.asset_id]:
                    consumers_by_asset[use.asset_id].append(card_id)

        materialized_asset_ids = set(asset_map)
        asset_records = [
            self.asset_record(
                asset=asset,
                step=asset_steps.get(asset.asset_id, 0),
                producer_card_id=producer_by_asset.get(asset.asset_id),
                producer_source=producer_source.get(asset.asset_id),
                consumer_card_ids=consumers_by_asset.get(asset.asset_id, []),
                materialized=True,
                planned=False,
            )
            for asset in graph.assets
        ]
        for card in cards:
            card_step = card_steps.get(card.card_id, card.step or 1)
            for output in card.outputs:
                if not output.asset_id or output.asset_id in materialized_asset_ids:
                    continue
                asset_records.append(
                    {
                        "asset_id": output.asset_id,
                        "asset_type": "planned_output",
                        "title": output.label,
                        "status": output.status or "planned",
                        "step": card_step,
                        "path": None,
                        "summary": f"Planned output from {card.title}",
                        "created_by_run": None,
                        "depends_on": [use.asset_id for use in required_uses_by_card.get(card.card_id, [])],
                        "producer_card_id": card.card_id,
                        "producer_run_id": None,
                        "producer_source": "card_output",
                        "consumer_card_ids": sorted(consumers_by_asset.get(output.asset_id, [])),
                        "materialized": False,
                        "planned": True,
                    }
                )

        card_records = [
            {
                "card_id": card.card_id,
                "title": card.title,
                "status": card.status,
                "card_type": card.card_type,
                "step": card_steps.get(card.card_id, card.step or 1),
                "stored_step": card.step,
                "linked_modules": card.linked_modules,
                "required_asset_ids": [use.asset_id for use in required_uses_by_card.get(card.card_id, [])],
                "produced_asset_ids": produced_assets_by_card.get(card.card_id, []),
                "depends_on_card_ids": sorted(dependency_map.get(card.card_id, set())),
            }
            for card in cards
        ]
        return {
            "project_id": project_id,
            "cards": card_records,
            "assets": sorted(asset_records, key=lambda item: (item["step"], item["asset_id"])),
            "card_steps": card_steps,
            "producer_by_asset": producer_by_asset,
            "producer_source": producer_source,
            "produced_assets_by_card": produced_assets_by_card,
            "required_uses_by_card": required_uses_by_card,
            "dependency_map": dependency_map,
            "parallel_batches": parallel_batches,
            "cycle_card_ids": cycle_card_ids,
            "duplicate_output_assets": duplicate_outputs,
        }

    def validate_card(self, snapshot: dict, candidate: Card, replacing_card_id: str | None = None) -> tuple[Card, list[str], list[str]]:
        graph: GraphState = snapshot["graph"]
        existing_cards = [card for card in snapshot["cards"] if card.card_id != replacing_card_id]
        existing_asset_ids = {asset.asset_id for asset in graph.assets}
        producer_by_asset, _producer_source, duplicate_outputs = self.producer_maps(existing_cards, graph.assets, graph.runs)
        explicit_output_ids = [output.asset_id for output in candidate.outputs if output.asset_id]
        reused_existing_assets = [
            asset_id
            for asset_id in explicit_output_ids
            if asset_id in existing_asset_ids and producer_by_asset.get(asset_id) != candidate.card_id
        ]
        if reused_existing_assets:
            return candidate, [
                "Planned output asset_id already exists as a materialized asset: " + ", ".join(sorted(set(reused_existing_assets)))
            ], []
        output_asset_ids = {
            output.asset_id
            for card in existing_cards
            for output in card.outputs
            if output.asset_id
        } | existing_asset_ids
        candidate = candidate.model_copy(update={"outputs": self.ensure_output_asset_ids(candidate.outputs, output_asset_ids)})
        candidate_cards = existing_cards + [candidate]
        candidate_snapshot = {**snapshot, "cards": candidate_cards}
        timeline = self.build(snapshot["project"].project_id, candidate_snapshot)
        errors: list[str] = []
        warnings: list[str] = []
        if duplicate_outputs or timeline["duplicate_output_assets"]:
            duplicate_assets = sorted(set(duplicate_outputs + timeline["duplicate_output_assets"]))
            errors.append("Duplicate planned output asset_id values: " + ", ".join(duplicate_assets))

        asset_by_id = {item["asset_id"]: item for item in timeline["assets"]}
        candidate_card = next(item for item in timeline["cards"] if item["card_id"] == candidate.card_id)
        min_step = 0
        for asset_id in candidate_card["required_asset_ids"]:
            asset = asset_by_id.get(asset_id)
            if not asset:
                errors.append(
                    f"Input asset {asset_id} is missing. Use list_data_assets to find the correct asset_id, "
                    "or add an upstream card output with that asset_id first."
                )
                continue
            min_step = max(min_step, int(asset["step"]) + 1)
        min_step = max(min_step, 1)
        if candidate.step is None:
            errors.append(f"Card step is required for {candidate.card_id}. Set step explicitly instead of relying on automatic ordering.")
        if candidate.step is not None and candidate.step < min_step:
            errors.append(
                f"Card step too early: card {candidate.card_id} is step {candidate.step}, "
                f"but its latest input asset requires step {min_step}. Increase step to at least {min_step}."
            )
        if candidate.step is not None and candidate.step > min_step:
            same_layer_siblings = [
                other.title for other in existing_cards
                if other.card_id != candidate.card_id
                and (other.step or 1) == min_step
            ]
            if same_layer_siblings:
                warnings.append(
                    f"Card {candidate.card_id} is step {candidate.step} but its inputs only require "
                    f"step {min_step}. {len(same_layer_siblings)} sibling card(s) are already at step "
                    f"{min_step}; prefer aligning to the same parallel layer unless a dependency reason exists."
                )
        if candidate.card_id in timeline["cycle_card_ids"]:
            errors.append(f"Dependency cycle detected around card {candidate.card_id}. Check card.inputs and card.outputs asset_ids.")
        if errors:
            return candidate, errors, warnings
        return candidate, [], warnings

    @classmethod
    def producer_maps(
        cls,
        cards: list[Card],
        assets: list[Asset],
        runs: list[RunRecord],
    ) -> tuple[dict[str, str], dict[str, str], list[str]]:
        run_card_by_id = {run.run_id: run.card_id for run in runs}
        producer_by_asset: dict[str, str] = {}
        producer_source: dict[str, str] = {}
        duplicate_outputs: set[str] = set()
        for asset in assets:
            metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
            if asset.created_by_run and asset.created_by_run in run_card_by_id:
                producer_card_id = run_card_by_id[asset.created_by_run]
                producer_by_asset[asset.asset_id] = producer_card_id
                producer_source[asset.asset_id] = "run_output"
                planned_asset_id = str(metadata.get("planned_asset_id") or "").strip()
                if planned_asset_id:
                    producer_by_asset.setdefault(planned_asset_id, producer_card_id)
                    producer_source.setdefault(planned_asset_id, "run_output_alias")
        for card in cards:
            for output in card.outputs:
                if not output.asset_id:
                    continue
                if output.asset_id in producer_by_asset and producer_by_asset[output.asset_id] != card.card_id:
                    duplicate_outputs.add(output.asset_id)
                    continue
                producer_by_asset[output.asset_id] = card.card_id
                producer_source[output.asset_id] = "card_output"
        return producer_by_asset, producer_source, sorted(duplicate_outputs)

    @classmethod
    def produced_assets_by_card(cls, cards: list[Card], assets: list[Asset], runs: list[RunRecord]) -> dict[str, list[str]]:
        run_card_by_id = {run.run_id: run.card_id for run in runs}
        produced: dict[str, list[str]] = {card.card_id: [] for card in cards}
        for asset in assets:
            if asset.created_by_run and asset.created_by_run in run_card_by_id:
                cls.append_unique(produced.setdefault(run_card_by_id[asset.created_by_run], []), asset.asset_id)
        for card in cards:
            for output in card.outputs:
                if output.asset_id:
                    cls.append_unique(produced.setdefault(card.card_id, []), output.asset_id)
        return produced

    @classmethod
    def required_asset_uses(cls, card: Card, module_map: dict[str, Module], asset_map: dict[str, Asset]) -> list[CardAssetUse]:
        uses: list[CardAssetUse] = []
        seen: set[str] = set()
        for item in card.inputs:
            if item.asset_id and item.asset_id not in seen:
                seen.add(item.asset_id)
                uses.append(CardAssetUse(asset_id=item.asset_id, label=item.label))
        for module_id in card.linked_modules:
            module = module_map.get(module_id)
            if not module:
                continue
            for asset_id in module.depends_on_assets:
                if asset_id in seen:
                    continue
                seen.add(asset_id)
                asset = asset_map.get(asset_id)
                uses.append(CardAssetUse(asset_id=asset_id, label=asset.title if asset else asset_id))
        return uses

    @staticmethod
    def card_dependency_map(
        cards: list[Card],
        required_uses_by_card: dict[str, list[CardAssetUse]],
        producer_by_asset: dict[str, str],
    ) -> dict[str, set[str]]:
        dependency_map: dict[str, set[str]] = {}
        for card in cards:
            dependencies = {
                producer
                for use in required_uses_by_card.get(card.card_id, [])
                if (producer := producer_by_asset.get(use.asset_id)) and producer != card.card_id
            }
            dependency_map[card.card_id] = dependencies
        return dependency_map

    @classmethod
    def card_steps(
        cls,
        cards: list[Card],
    ) -> dict[str, int]:
        return {card.card_id: card.step or 1 for card in cards}

    @classmethod
    def asset_steps(
        cls,
        assets: list[Asset],
        cards: list[Card],
        card_steps: dict[str, int],
        producer_by_asset: dict[str, str],
    ) -> dict[str, int]:
        asset_steps = {asset.asset_id: 0 for asset in assets}
        changed = True
        while changed:
            changed = False
            for asset in assets:
                next_step = asset_steps.get(asset.asset_id, 0)
                producer_card_id = producer_by_asset.get(asset.asset_id)
                if producer_card_id:
                    next_step = max(next_step, card_steps.get(producer_card_id, 0))
                for upstream_asset_id in asset.depends_on:
                    next_step = max(next_step, asset_steps.get(upstream_asset_id, 0) + 1)
                if next_step != asset_steps.get(asset.asset_id, 0):
                    asset_steps[asset.asset_id] = next_step
                    changed = True
        for card in cards:
            for output in card.outputs:
                if output.asset_id:
                    asset_steps[output.asset_id] = max(asset_steps.get(output.asset_id, 0), card_steps.get(card.card_id, card.step or 1))
        return asset_steps

    @staticmethod
    def parallel_batches(active_card_ids: set[str], dependency_map: dict[str, set[str]]) -> tuple[list[dict], list[str]]:
        active_dependencies = {
            card_id: {dep_id for dep_id in deps if dep_id in active_card_ids}
            for card_id, deps in dependency_map.items()
            if card_id in active_card_ids
        }
        remaining = {card_id: set(deps) for card_id, deps in active_dependencies.items()}
        ready = deque(sorted(card_id for card_id, deps in remaining.items() if not deps))
        batches: list[dict] = []
        scheduled: set[str] = set()

        while ready:
            batch_ids = list(ready)
            ready.clear()
            step = len(batches) + 1
            batches.append({"batch_index": len(batches), "step": step, "card_ids": batch_ids})
            scheduled.update(batch_ids)
            for card_id in batch_ids:
                for target_id, deps in remaining.items():
                    deps.discard(card_id)
                    if target_id not in scheduled and not deps and target_id not in ready:
                        ready.append(target_id)

        return batches, sorted(active_card_ids - scheduled)

    @classmethod
    def ensure_output_asset_ids(cls, outputs: list[CardOutputSpec], reserved_asset_ids: set[str]) -> list[CardOutputSpec]:
        next_outputs: list[CardOutputSpec] = []
        for output in outputs:
            asset_id = output.asset_id
            if not asset_id:
                asset_id = cls.unique_asset_id(output.label, reserved_asset_ids)
            reserved_asset_ids.add(asset_id)
            next_outputs.append(output.model_copy(update={"asset_id": asset_id, "status": output.status or "planned"}))
        return next_outputs

    @classmethod
    def unique_asset_id(cls, label: str, reserved: set[str]) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "planned_asset"
        base = f"asset_{slug}"
        candidate = base
        index = 1
        while candidate in reserved:
            index += 1
            candidate = f"{base}_{index}"
        return candidate

    @staticmethod
    def asset_record(
        asset: Asset,
        step: int,
        producer_card_id: str | None,
        producer_source: str | None,
        consumer_card_ids: list[str],
        materialized: bool,
        planned: bool,
    ) -> dict:
        return {
            "asset_id": asset.asset_id,
            "asset_type": asset.asset_type,
            "title": asset.title,
            "status": asset.status,
            "step": step,
            "path": asset.path,
            "summary": asset.summary,
            "created_by_run": asset.created_by_run,
            "depends_on": asset.depends_on,
            "producer_card_id": producer_card_id,
            "producer_run_id": asset.created_by_run,
            "producer_source": producer_source,
            "consumer_card_ids": sorted(consumer_card_ids),
            "materialized": materialized,
            "planned": planned,
        }

    @staticmethod
    def append_unique(items: list[str], value: str) -> None:
        if value not in items:
            items.append(value)
