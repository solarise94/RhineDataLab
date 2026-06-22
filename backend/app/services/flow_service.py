from __future__ import annotations

from typing import Any

from app.models.cards import Card
from app.models.graph import VALID_INPUT_ASSET_STATUSES, Asset, GraphState, Module, RunRecord
from app.services.asset_timeline_service import AssetTimelineService
from app.services.dependency_attention_service import DependencyAttentionService
from app.services.input_resolution_service import InputResolutionService
from app.services.project_service import ProjectService
from app.services.runtime_dependency_state_service import ACTIVE_RUNTIME_DEPENDENCY_JOB_STATUSES, dependency_blockers_by_card


class FlowService:
    def __init__(self, project_service: ProjectService) -> None:
        self.project_service = project_service
        self.timeline_service = AssetTimelineService()
        self.dependency_attention_service = DependencyAttentionService()
        self.input_resolution_service = InputResolutionService()

    def get_asset_flow(self, project_id: str) -> dict:
        snapshot = self.project_service.get_project_snapshot(project_id)
        timeline = self.timeline_service.build(project_id, snapshot)
        return {
            "project_id": project_id,
            "cards": timeline["cards"],
            "assets": timeline["assets"],
            "card_edges": self._card_edges(snapshot["cards"], snapshot["graph"], timeline["producer_by_asset"]),
            "asset_edges": self._asset_edges(snapshot["graph"].assets, timeline["producer_by_asset"]),
            "timeline": timeline,
        }

    def get_work_order(
        self,
        project_id: str,
        *,
        runtime_dependency_blockers: dict[str, dict[str, Any]] | None = None,
    ) -> dict:
        snapshot = self.project_service.get_project_snapshot(project_id)
        graph: GraphState = snapshot["graph"]
        timeline = self.timeline_service.build(project_id, snapshot)
        attention = self.dependency_attention_service.analyze_project(snapshot)
        attention_by_card = attention["issues_by_card"]
        card_map = {card.card_id: card for card in snapshot["cards"]}
        resolution_index = self.input_resolution_service.build_index(snapshot["cards"], graph)
        runtime_dependency_blockers = runtime_dependency_blockers or dependency_blockers_by_card(
            self.project_service.project_path(project_id)
        )
        work_items: list[dict] = []

        for card in snapshot["cards"]:
            required_uses = timeline["required_uses_by_card"].get(card.card_id, [])
            required_asset_ids = [use.asset_id for use in required_uses]
            dependency_ids = sorted(timeline["dependency_map"].get(card.card_id, set()))
            input_resolutions = [
                self.input_resolution_service.resolve_input(asset_id, resolution_index)
                for asset_id in required_asset_ids
            ]
            missing_asset_ids = [
                item.requested_asset_id
                for item in input_resolutions
                if item.resolved_asset_id is None
            ]
            logical_missing_asset_ids = [
                item.requested_asset_id
                for item in input_resolutions
                if item.resolved_asset_id is None
                and not item.producer_card_id
            ]
            materialization_missing_asset_ids = [
                item.requested_asset_id
                for item in input_resolutions
                if item.resolved_asset_id is None
                and item.producer_card_id
            ]
            nonvalid_asset_ids = [
                item.resolved_asset_id or item.requested_asset_id
                for item in input_resolutions
                if item.resolved_asset_id is not None and item.status not in VALID_INPUT_ASSET_STATUSES
            ]
            unmet_dependency_ids = [
                dep_id
                for dep_id in dependency_ids
                if dep_id in card_map and card_map[dep_id].status != "accepted"
            ]
            missing_script_binding_ids = self._missing_script_asset_binding_ids(card)
            runtime_dependency_blocker = runtime_dependency_blockers.get(card.card_id)
            can_start = (
                card.status in {"planned", "failed", "stale", "superseded"}
                and not missing_asset_ids
                and not nonvalid_asset_ids
                and not unmet_dependency_ids
                and not missing_script_binding_ids
                and runtime_dependency_blocker is None
            )
            block_reasons = self._block_reasons(
                card,
                missing_asset_ids,
                nonvalid_asset_ids,
                unmet_dependency_ids,
                missing_script_binding_ids,
                runtime_dependency_blocker,
                logical_missing_asset_ids=logical_missing_asset_ids,
                materialization_missing_asset_ids=materialization_missing_asset_ids,
            )
            card_attention = attention_by_card.get(card.card_id, [])
            work_items.append(
                {
                    "card_id": card.card_id,
                    "title": card.title,
                    "status": card.status,
                    "card_type": card.card_type,
                    "step": card.step or 1,
                    "required_asset_ids": required_asset_ids,
                    "produced_asset_ids": timeline["produced_assets_by_card"].get(card.card_id, []),
                    "depends_on_card_ids": dependency_ids,
                    "blocked_by_card_ids": unmet_dependency_ids,
                    "blocked_by_asset_ids": missing_asset_ids + nonvalid_asset_ids,
                    "blocked_by_job_ids": [runtime_dependency_blocker["job_id"]] if runtime_dependency_blocker else [],
                    "missing_script_asset_requirement_ids": missing_script_binding_ids,
                    "logical_missing_asset_ids": logical_missing_asset_ids,
                    "materialization_missing_asset_ids": materialization_missing_asset_ids,
                    "nonlaunchable_materialized_asset_ids": nonvalid_asset_ids,
                    "planned_input_asset_ids": [
                        item.requested_asset_id
                        for item in input_resolutions
                        if item.is_virtual and item.resolved_asset_id is not None
                    ],
                    "input_resolutions": [
                        {
                            "requested_asset_id": item.requested_asset_id,
                            "resolved_asset_id": item.resolved_asset_id,
                            "resolved_path": item.resolved_path,
                            "resolved_by": item.resolved_by,
                            "producer_card_id": item.producer_card_id,
                            "producer_role": item.producer_role,
                            "status": item.status,
                        }
                        for item in input_resolutions
                    ],
                    "runtime_dependency_blocker": (
                        {
                            "job_id": runtime_dependency_blocker["job_id"],
                            "task_id": runtime_dependency_blocker["task_id"],
                            "status": runtime_dependency_blocker["status"],
                            "runtime": runtime_dependency_blocker["runtime"],
                            "packages": runtime_dependency_blocker["packages"],
                            "run_id": runtime_dependency_blocker["run_id"] or None,
                            "session_id": runtime_dependency_blocker["session_id"] or None,
                            "retry_after_signal": "runtime_dependency_install_terminal",
                            "error_code": runtime_dependency_blocker.get("error_code"),
                            "message": runtime_dependency_blocker.get("message"),
                            "requested_package": runtime_dependency_blocker.get("requested_package"),
                            "attempted_candidates": runtime_dependency_blocker.get("attempted_candidates"),
                            "fallback_available": runtime_dependency_blocker.get("fallback_available"),
                            "retry_hint": runtime_dependency_blocker.get("retry_hint"),
                            "dedupe_key": runtime_dependency_blocker.get("dedupe_key"),
                        }
                        if runtime_dependency_blocker
                        else None
                    ),
                    "can_start": can_start,
                    "block_reasons": block_reasons,
                    "active": card.status not in {"accepted", "rejected", "cancelled"},
                    "dependency_attention": card_attention,
                    "dependency_attention_count": len(card_attention),
                    "attention_issue_ids": [issue["issue_id"] for issue in card_attention],
                    "attention_severity": DependencyAttentionService.attention_severity(card_attention),
                }
            )

        return {
            "project_id": project_id,
            "work_items": work_items,
            "parallel_batches": timeline["parallel_batches"],
            "dependency_edges": [
                {
                    "edge_id": self._edge_id(source_id, target_id, "work_dependency", "work_dependency"),
                    "source_card_id": source_id,
                    "target_card_id": target_id,
                    "edge_type": "work_dependency",
                }
                for target_id, source_ids in sorted(timeline["dependency_map"].items())
                for source_id in sorted(source_ids)
            ],
            "cycle_card_ids": timeline["cycle_card_ids"],
            "dependency_attention": attention["issues"],
            "dependency_attention_count": attention["issue_count"],
        }

    def get_timeline(self, project_id: str) -> dict:
        snapshot = self.project_service.get_project_snapshot(project_id)
        return self.timeline_service.build(project_id, snapshot)

    @staticmethod
    def _card_edges(cards: list[Card], graph: GraphState, producer_by_asset: dict[str, str]) -> list[dict]:
        asset_map = {asset.asset_id: asset for asset in graph.assets}
        card_edges: list[dict] = []
        seen_card_edges: set[tuple[str | None, str, str, str]] = set()
        for card in cards:
            for item in card.inputs:
                if not item.asset_id:
                    continue
                source_card_id = producer_by_asset.get(item.asset_id)
                if source_card_id == card.card_id:
                    continue
                asset = asset_map.get(item.asset_id)
                edge_type = "card_output_to_input" if source_card_id else "raw_asset_to_card"
                key = (source_card_id, card.card_id, item.asset_id, edge_type)
                if key in seen_card_edges:
                    continue
                seen_card_edges.add(key)
                card_edges.append(
                    {
                        "edge_id": FlowService._edge_id(source_card_id or "raw", card.card_id, item.asset_id, edge_type),
                        "edge_type": edge_type,
                        "source_card_id": source_card_id,
                        "target_card_id": card.card_id,
                        "asset_id": item.asset_id,
                        "asset_title": asset.title if asset else item.label,
                        "asset_status": asset.status if asset else "missing",
                        "label": item.label,
                    }
                )
        return card_edges

    @staticmethod
    def _asset_edges(assets: list[Asset], producer_by_asset: dict[str, str]) -> list[dict]:
        asset_map = {asset.asset_id: asset for asset in assets}
        asset_edges: list[dict] = []
        seen_asset_edges: set[tuple[str, str]] = set()
        for asset in assets:
            for upstream_asset_id in asset.depends_on:
                key = (upstream_asset_id, asset.asset_id)
                if key in seen_asset_edges:
                    continue
                seen_asset_edges.add(key)
                upstream = asset_map.get(upstream_asset_id)
                asset_edges.append(
                    {
                        "edge_id": FlowService._edge_id(upstream_asset_id, asset.asset_id, "asset_lineage", "asset_lineage"),
                        "edge_type": "asset_lineage",
                        "source_asset_id": upstream_asset_id,
                        "target_asset_id": asset.asset_id,
                        "source_card_id": producer_by_asset.get(upstream_asset_id),
                        "target_card_id": producer_by_asset.get(asset.asset_id),
                        "source_asset_title": upstream.title if upstream else upstream_asset_id,
                        "target_asset_title": asset.title,
                    }
                )
        return asset_edges

    @staticmethod
    def _block_reasons(
        card: Card,
        missing_asset_ids: list[str],
        nonvalid_asset_ids: list[str],
        unmet_dependency_ids: list[str],
        missing_script_binding_ids: list[str],
        runtime_dependency_blocker: dict | None,
        *,
        logical_missing_asset_ids: list[str] | None = None,
        materialization_missing_asset_ids: list[str] | None = None,
    ) -> list[str]:
        reasons: list[str] = []
        if card.status == "proposed":
            reasons.append("proposal_not_accepted")
        elif card.status in {"accepted", "rejected", "cancelled"}:
            reasons.append(f"terminal_status:{card.status}")
        elif card.status not in {"planned", "failed", "stale", "superseded"}:
            reasons.append(f"not_startable_status:{card.status}")
        if logical_missing_asset_ids:
            reasons.append("logical_dependency_missing")
        elif materialization_missing_asset_ids:
            reasons.append("input_materialization_missing")
        elif missing_asset_ids:
            reasons.append("missing_required_assets")
        if nonvalid_asset_ids:
            reasons.append("required_assets_not_valid")
        if unmet_dependency_ids:
            reasons.append("upstream_cards_not_accepted")
        if missing_script_binding_ids:
            reasons.append("missing_script_asset_bindings")
        if runtime_dependency_blocker:
            status = str(runtime_dependency_blocker.get("status") or "")
            reasons.append(
                "runtime_dependency_repair_in_progress"
                if status in ACTIVE_RUNTIME_DEPENDENCY_JOB_STATUSES
                else "runtime_dependency_repair_failed"
            )
        return reasons

    @staticmethod
    def _missing_script_asset_binding_ids(card: Card) -> list[str]:
        context = card.executor_context
        if context is None:
            return []
        bound_ids = {
            binding.requirement_id
            for binding in context.script_asset_bindings
            if binding.requirement_id and (binding.asset_id or binding.path)
        }
        return [
            requirement.requirement_id
            for requirement in context.script_asset_requirements
            if requirement.requirement_id and not requirement.optional and requirement.requirement_id not in bound_ids
        ]

    @staticmethod
    def _edge_id(source_id: str, target_id: str, asset_id: str, edge_type: str) -> str:
        return f"{edge_type}:{source_id}:{target_id}:{asset_id}"
