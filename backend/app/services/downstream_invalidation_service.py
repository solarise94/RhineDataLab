from __future__ import annotations

from typing import Any

from app.models.cards import Card
from app.models.graph import Asset, Claim, GraphState
from app.services.dependency_attention_service import DependencyAttentionService
from app.services.module_group_state_service import ModuleGroupStateService


class DownstreamInvalidationService:
    """Push-time invalidation: mark downstream cards and their assets stale.

    Uses :meth:`DependencyAttentionService.build_consumer_edges` for the same
    producer-consumer traversal that the diagnostic tools use, so push and pull
    views of the dependency graph stay consistent.
    """

    # Cards that have produced results which can become outdated by upstream change.
    INVALIDATABLE_CARD_STATUSES = {"accepted", "needs_review"}
    # Asset statuses that should be flipped to stale when their producer card goes stale.
    INVALIDATABLE_ASSET_STATUSES = {"valid", "candidate"}
    # Transitive asset stale triggers.
    STALE_LINEAGE_STATUSES = {"stale", "superseded"}

    @classmethod
    def invalidate_from(
        cls,
        graph: GraphState,
        cards: list[Card],
        source_card_id: str,
        *,
        max_depth: int | None = None,
    ) -> dict[str, Any]:
        """Mark downstream cards (and their output assets) stale starting from ``source_card_id``.

        Only cards in ``INVALIDATABLE_CARD_STATUSES`` are mutated; running/reviewing cards
        are left alone so their in-flight work is not interrupted. Already stale/failed/planned
        cards are also left unchanged.

        Assets are marked stale if they are outputs of an invalidated card, or if their
        ``depends_on`` lineage contains a stale/superseded asset.

        Returns a summary dict with the list of invalidated card ids and asset ids.
        """
        consumers_by_card = DependencyAttentionService.build_consumer_edges(cards, graph)
        card_by_id = {card.card_id: card for card in cards}
        asset_by_id = {asset.asset_id: asset for asset in graph.assets}

        affected_card_ids: list[str] = []
        affected_asset_ids: list[str] = []

        # BFS downstream with optional depth limit.
        queue: list[tuple[str, int]] = [(source_card_id, 0)]
        seen: set[str] = {source_card_id}
        newly_stale_card_ids: set[str] = set()

        while queue:
            current_id, depth = queue.pop(0)
            for downstream_id in sorted(consumers_by_card.get(current_id, set())):
                if downstream_id in seen:
                    continue
                seen.add(downstream_id)
                downstream_depth = depth + 1
                if max_depth is not None and downstream_depth > max_depth:
                    continue

                downstream_card = card_by_id.get(downstream_id)
                if downstream_card and downstream_card.status in cls.INVALIDATABLE_CARD_STATUSES:
                    downstream_card.status = "stale"
                    downstream_card.progress_note = (
                        f"Marked stale due to upstream invalidation from {source_card_id}."
                    )
                    newly_stale_card_ids.add(downstream_id)
                    affected_card_ids.append(downstream_id)
                    ModuleGroupStateService.sync_linked_module_status_from_card(
                        downstream_card, graph.modules
                    )

                queue.append((downstream_id, downstream_depth))

        # Mark output assets of newly stale cards stale.
        for card_id in newly_stale_card_ids:
            card = card_by_id[card_id]
            for output in card.outputs:
                asset = asset_by_id.get(output.asset_id) if output.asset_id else None
                if asset and asset.status in cls.INVALIDATABLE_ASSET_STATUSES:
                    asset.status = "stale"
                    affected_asset_ids.append(asset.asset_id)

        # Transitive asset staleness via depends_on lineage.
        stale_asset_ids: set[str] = {
            asset.asset_id for asset in graph.assets if asset.status in cls.STALE_LINEAGE_STATUSES
        }
        for asset in graph.assets:
            if asset.status in cls.STALE_LINEAGE_STATUSES:
                continue
            if asset.status not in cls.INVALIDATABLE_ASSET_STATUSES:
                continue
            if any(
                upstream_id in stale_asset_ids and upstream_id != asset.asset_id
                for upstream_id in asset.depends_on
            ):
                asset.status = "stale"
                affected_asset_ids.append(asset.asset_id)
                stale_asset_ids.add(asset.asset_id)

        # Claims that depend on stale/superseded assets become stale.
        affected_claim_ids: list[str] = []
        for claim in graph.claims:
            if claim.status == "stale":
                continue
            if any(dep_id in stale_asset_ids for dep_id in claim.depends_on_assets):
                claim.status = "stale"  # type: ignore[assignment]
                affected_claim_ids.append(claim.claim_id)

        return {
            "source_card_id": source_card_id,
            "invalidated_card_ids": affected_card_ids,
            "invalidated_asset_ids": affected_asset_ids,
            "invalidated_claim_ids": affected_claim_ids,
        }

    @staticmethod
    def parse_propagate(propagate: str) -> dict[str, Any]:
        """Parse a propagate string into invalidation options.

        Supported forms:
        - ``"all"`` /→ full downstream cascade.
        - ``"none"`` /→ no invalidation.
        - ``"depth:N"`` /→ cascade only up to N edges.

        Raises :class:`ValueError` for unrecognized forms.
        """
        if propagate == "all":
            return {"enabled": True, "max_depth": None}
        if propagate == "none":
            return {"enabled": False, "max_depth": None}
        if propagate.startswith("depth:"):
            try:
                depth = int(propagate.split(":", 1)[1])
            except (ValueError, IndexError) as exc:
                raise ValueError(f"Invalid propagate depth: {propagate}") from exc
            if depth < 0:
                raise ValueError(f"Invalid propagate depth: {propagate}")
            return {"enabled": True, "max_depth": depth}
        raise ValueError(f"Invalid propagate value: {propagate}")
