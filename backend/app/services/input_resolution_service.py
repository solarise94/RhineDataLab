from __future__ import annotations

from dataclasses import dataclass

from app.models.cards import Card
from app.models.graph import ASSET_STATUS_RANK, VALID_INPUT_ASSET_STATUSES, Asset, GraphState
from app.services.asset_materialization_service import AssetMaterializationService
from app.services.asset_timeline_service import AssetTimelineService


@dataclass(frozen=True)
class InputResolution:
    requested_asset_id: str
    resolved_asset_id: str | None
    resolved_path: str | None
    resolved_by: str | None
    producer_card_id: str | None
    producer_role: str | None
    status: str
    current_asset_id: str | None
    is_virtual: bool
    asset: Asset | None


@dataclass(frozen=True)
class InputResolutionIndex:
    asset_by_id: dict[str, Asset]
    producer_by_asset: dict[str, str]
    planned_output_by_asset_id: dict[str, tuple[str, str]]
    role_by_asset: dict[str, str]
    alias_assets_by_planned_id: dict[str, list[Asset]]
    current_output_by_card_role: dict[tuple[str, str], Asset]
    run_order_by_id: dict[str, int]
    run_card_by_id: dict[str, str]
    materializations: dict[str, dict]


class InputResolutionService:
    def __init__(self) -> None:
        self.timeline_service = AssetTimelineService()

    def build_index(self, cards: list[Card], graph: GraphState) -> InputResolutionIndex:
        asset_by_id = {asset.asset_id: asset for asset in graph.assets}
        producer_by_asset, _producer_source, _duplicate_outputs = self.timeline_service.producer_maps(cards, graph.assets, graph.runs)
        planned_output_by_asset_id: dict[str, tuple[str, str]] = {}
        role_by_asset: dict[str, str] = {}
        alias_assets_by_planned_id: dict[str, list[Asset]] = {}
        run_order_by_id = {run.run_id: index for index, run in enumerate(graph.runs)}
        run_card_by_id = {run.run_id: run.card_id for run in graph.runs}

        for card in cards:
            for output in card.outputs:
                if not output.asset_id or self._is_system_output_role(output.role):
                    continue
                planned_output_by_asset_id[output.asset_id] = (card.card_id, output.role)

        for asset in graph.assets:
            metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
            role = str(metadata.get("role") or "").strip()
            if role:
                role_by_asset[asset.asset_id] = role
            stable_alias = self.recover_stable_planned_asset_alias(metadata.get("planned_asset_id"), asset_by_id)
            if stable_alias:
                alias_assets_by_planned_id.setdefault(stable_alias, []).append(asset)
                if role:
                    role_by_asset.setdefault(stable_alias, role)

        current_output_by_card_role: dict[tuple[str, str], Asset] = {}
        for card in cards:
            for output in card.outputs:
                if not output.asset_id or not output.role or self._is_system_output_role(output.role):
                    continue
                resolved = self._resolve_output_asset(
                    output.asset_id,
                    output.role,
                    card.card_id,
                    asset_by_id=asset_by_id,
                    alias_assets_by_planned_id=alias_assets_by_planned_id,
                    run_order_by_id=run_order_by_id,
                    run_card_by_id=run_card_by_id,
                )
                if resolved is not None:
                    current_output_by_card_role[(card.card_id, output.role)] = resolved

        materializations = graph.metadata.get("asset_materializations") if isinstance(graph.metadata, dict) else {}
        materializations = materializations or {}

        return InputResolutionIndex(
            asset_by_id=asset_by_id,
            producer_by_asset=producer_by_asset,
            planned_output_by_asset_id=planned_output_by_asset_id,
            role_by_asset=role_by_asset,
            alias_assets_by_planned_id=alias_assets_by_planned_id,
            current_output_by_card_role=current_output_by_card_role,
            run_order_by_id=run_order_by_id,
            run_card_by_id=run_card_by_id,
            materializations=materializations,
        )

    def resolve_input(self, requested_asset_id: str | None, index: InputResolutionIndex) -> InputResolution:
        requested = str(requested_asset_id or "").strip()
        if not requested:
            return InputResolution(
                requested_asset_id="",
                resolved_asset_id=None,
                resolved_path=None,
                resolved_by=None,
                producer_card_id=None,
                producer_role=None,
                status="missing",
                current_asset_id=None,
                is_virtual=False,
                asset=None,
            )

        direct_asset = index.asset_by_id.get(requested)
        if direct_asset is not None:
            producer_card_id = index.producer_by_asset.get(direct_asset.asset_id)
            producer_role = index.role_by_asset.get(direct_asset.asset_id)
            current_asset = index.current_output_by_card_role.get((producer_card_id, producer_role)) if producer_card_id and producer_role else None
            return InputResolution(
                requested_asset_id=requested,
                resolved_asset_id=direct_asset.asset_id,
                resolved_path=direct_asset.path,
                resolved_by="direct_asset_id",
                producer_card_id=producer_card_id,
                producer_role=producer_role,
                status=direct_asset.status,
                current_asset_id=current_asset.asset_id if current_asset is not None else None,
                is_virtual=False,
                asset=direct_asset,
            )

        # Binding-first: resolve logical id through explicit materialization map
        binding = index.materializations.get(requested)
        if binding:
            current_asset_id = binding.get("current_asset_id")
            if current_asset_id:
                bound_asset = index.asset_by_id.get(current_asset_id)
                if bound_asset is not None:
                    return InputResolution(
                        requested_asset_id=requested,
                        resolved_asset_id=bound_asset.asset_id,
                        resolved_path=bound_asset.path,
                        resolved_by="materialization_binding",
                        producer_card_id=binding.get("producer_card_id"),
                        producer_role=binding.get("producer_role"),
                        status=bound_asset.status,
                        current_asset_id=bound_asset.asset_id,
                        is_virtual=False,
                        asset=bound_asset,
                    )

        producer_card_id = index.producer_by_asset.get(requested)
        producer_role = index.role_by_asset.get(requested)
        if producer_role is None:
            planned_output = index.planned_output_by_asset_id.get(requested)
            if planned_output is not None:
                producer_role = planned_output[1]

        current_asset = index.current_output_by_card_role.get((producer_card_id, producer_role)) if producer_card_id and producer_role else None
        if current_asset is None:
            current_asset = self._pick_best_alias_candidate(
                requested,
                index,
                preferred_card_id=producer_card_id,
                preferred_role=producer_role,
            )
        if current_asset is None:
            # Distinguish: producer exists but no materialization vs truly missing producer.
            has_producer = producer_card_id is not None
            return InputResolution(
                requested_asset_id=requested,
                resolved_asset_id=None,
                resolved_path=None,
                resolved_by="materialization_missing" if has_producer else None,
                producer_card_id=producer_card_id,
                producer_role=producer_role,
                status="missing",
                current_asset_id=None,
                is_virtual=requested not in index.asset_by_id,
                asset=None,
            )

        return InputResolution(
            requested_asset_id=requested,
            resolved_asset_id=current_asset.asset_id,
            resolved_path=current_asset.path,
            resolved_by="planned_asset_alias",
            producer_card_id=producer_card_id or index.producer_by_asset.get(current_asset.asset_id),
            producer_role=producer_role or index.role_by_asset.get(current_asset.asset_id),
            status=current_asset.status,
            current_asset_id=current_asset.asset_id,
            is_virtual=True,
            asset=current_asset,
        )

    @staticmethod
    def recover_stable_planned_asset_alias(candidate_id: str | None, asset_by_id: dict[str, Asset]) -> str | None:
        alias = str(candidate_id or "").strip()
        if not alias:
            return None
        visited: set[str] = set()
        current = alias
        while current:
            if current in visited:
                return None
            visited.add(current)
            materialized = asset_by_id.get(current)
            if materialized is None:
                return current
            metadata = materialized.metadata if isinstance(materialized.metadata, dict) else {}
            current = str(metadata.get("planned_asset_id") or "").strip()
        return None

    @staticmethod
    def _resolve_output_asset(
        output_asset_id: str,
        output_role: str,
        card_id: str,
        *,
        asset_by_id: dict[str, Asset],
        alias_assets_by_planned_id: dict[str, list[Asset]],
        run_order_by_id: dict[str, int],
        run_card_by_id: dict[str, str],
    ) -> Asset | None:
        direct = asset_by_id.get(output_asset_id)
        if direct is not None:
            return direct
        candidates = list(alias_assets_by_planned_id.get(output_asset_id, []))
        if not candidates:
            return None
        return InputResolutionService._sort_candidates(
            candidates,
            run_order_by_id=run_order_by_id,
            run_card_by_id=run_card_by_id,
            preferred_card_id=card_id,
            preferred_role=output_role,
        )[0]

    def _pick_best_alias_candidate(
        self,
        requested_asset_id: str,
        index: InputResolutionIndex,
        *,
        preferred_card_id: str | None,
        preferred_role: str | None,
    ) -> Asset | None:
        candidates = list(index.alias_assets_by_planned_id.get(requested_asset_id, []))
        if not candidates:
            return None
        return self._sort_candidates(
            candidates,
            run_order_by_id=index.run_order_by_id,
            run_card_by_id=index.run_card_by_id,
            preferred_card_id=preferred_card_id,
            preferred_role=preferred_role,
        )[0]

    @staticmethod
    def _sort_candidates(
        candidates: list[Asset],
        *,
        run_order_by_id: dict[str, int],
        run_card_by_id: dict[str, str],
        preferred_card_id: str | None,
        preferred_role: str | None,
    ) -> list[Asset]:

        def sort_key(asset: Asset) -> tuple[int, int, int]:
            metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
            role = str(metadata.get("role") or "").strip()
            producer_match = 0
            if preferred_role and role == preferred_role:
                producer_match -= 1
            if preferred_card_id and asset.created_by_run and run_card_by_id.get(asset.created_by_run or "") == preferred_card_id:
                producer_match -= 1
            return (
                ASSET_STATUS_RANK.get(asset.status, 99),
                producer_match,
                -run_order_by_id.get(asset.created_by_run or "", -1),
            )

        return sorted(candidates, key=sort_key)

    @staticmethod
    def _is_system_output_role(role: str | None) -> bool:
        normalized = str(role or "").strip()
        return normalized in {"run_summary", "run_preview"} or normalized.endswith(("run_summary", "run_preview"))
