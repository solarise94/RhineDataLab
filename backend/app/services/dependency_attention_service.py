from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from app.models.cards import Card
from app.models.graph import ASSET_STATUS_RANK, VALID_INPUT_ASSET_STATUSES, Asset, GraphState, RunRecord
from app.services.asset_materialization_service import AssetMaterializationService
from app.services.input_resolution_service import InputResolutionService


class DependencyAttentionService:
    """Derive dependency attention issues from the current project snapshot."""

    ERROR_INPUT_STATUSES = {"rejected", "archived", "missing"}
    INACTIVE_PRODUCER_CARD_STATUSES = {"cancelled", "rejected", "superseded"}
    ATTENTION_INPUT_ELIGIBLE_STATUSES = {"accepted", "failed", "stale", "needs_review", "superseded"}
    SYSTEM_OUTPUT_ROLE_SUFFIXES = ("run_summary", "run_preview")

    def analyze_project(self, snapshot: dict[str, Any], *, max_lineage_nodes: int = 200) -> dict[str, Any]:
        cards: list[Card] = list(snapshot.get("cards") or [])
        graph: GraphState = snapshot["graph"]
        indexes = self._build_indexes(cards, graph)
        issues: list[dict[str, Any]] = []

        for card in cards:
            self._analyze_card_inputs(card, indexes, issues, max_lineage_nodes=max_lineage_nodes)
            self._analyze_card_outputs(card, indexes, issues, max_lineage_nodes=max_lineage_nodes)

        issues = self._dedupe_and_sort(issues)
        return self._result(issues, cards)

    def issues_for_card(self, snapshot: dict[str, Any], card_id: str) -> list[dict[str, Any]]:
        result = self.analyze_project(snapshot)
        return list(result["issues_by_card"].get(card_id, []))

    def inspect(
        self,
        snapshot: dict[str, Any],
        *,
        card_ids: list[str] | None = None,
        source_card_id: str | None = None,
        include_recursive_downstream: bool = False,
        max_issues: int = 50,
    ) -> dict[str, Any]:
        analysis = self.analyze_project(snapshot)
        selected_card_ids: set[str] | None = None
        affected_downstream: list[dict[str, Any]] = []
        repair_order: list[str] = []

        if source_card_id and include_recursive_downstream:
            affected_downstream = self.affected_downstream(snapshot, source_card_id)
            repair_order = [item["card_id"] for item in affected_downstream]
            selected_card_ids = set(repair_order)
        elif card_ids:
            selected_card_ids = {str(item).strip() for item in card_ids if str(item).strip()}

        issues = list(analysis["issues"])
        if selected_card_ids is not None:
            issues = [issue for issue in issues if issue.get("card_id") in selected_card_ids]
        limited = issues[: max(1, min(int(max_issues or 50), 200))]

        if selected_card_ids is not None and not affected_downstream:
            issue_counts = self._issue_counts_by_card(issues)
            affected_downstream = [
                {
                    "card_id": card_id,
                    "dependency_depth": 0,
                    "issue_count": issue_counts.get(card_id, 0),
                }
                for card_id in sorted(selected_card_ids)
            ]
            repair_order = [item["card_id"] for item in affected_downstream]
        elif affected_downstream:
            issue_counts = self._issue_counts_by_card(issues)
            affected_downstream = [
                {
                    **item,
                    "issue_count": issue_counts.get(item["card_id"], 0),
                }
                for item in affected_downstream
            ]

        severity_counts: dict[str, int] = {}
        for issue in limited:
            severity = str(issue.get("severity") or "warning")
            severity_counts[severity] = severity_counts.get(severity, 0) + 1

        return {
            "issue_count": len(issues),
            "returned_issue_count": len(limited),
            "fingerprint": self._fingerprint(issues),
            "dependency_attention": limited,
            "issues_by_card": self._issues_by_card(limited),
            "severity_counts": severity_counts,
            "affected_downstream": affected_downstream,
            "repair_execution_order": repair_order,
            "truncated": len(limited) < len(issues),
        }

    def affected_downstream(self, snapshot: dict[str, Any], source_card_id: str) -> list[dict[str, Any]]:
        cards: list[Card] = list(snapshot.get("cards") or [])
        graph: GraphState = snapshot["graph"]
        consumers_by_card = self.build_consumer_edges(cards, graph)

        card_by_id = {card.card_id: card for card in cards}
        queue = [(source_card_id, 0)]
        seen = {source_card_id}
        affected: list[dict[str, Any]] = []
        while queue:
            current_id, depth = queue.pop(0)
            for downstream_id in sorted(consumers_by_card.get(current_id, set())):
                if downstream_id in seen:
                    continue
                seen.add(downstream_id)
                downstream_depth = depth + 1
                reason = f"Depends on outputs from {current_id}."
                if downstream_id in card_by_id:
                    affected.append(
                        {
                            "card_id": downstream_id,
                            "dependency_depth": downstream_depth,
                            "reason": reason,
                        }
                    )
                    queue.append((downstream_id, downstream_depth))

        return sorted(affected, key=lambda item: (int(item["dependency_depth"]), str(item["card_id"])))

    @classmethod
    def build_consumer_edges(cls, cards: list[Card], graph: GraphState) -> dict[str, set[str]]:
        """Return a map from producer card id to the set of downstream card ids.

        Edges are derived from card input references (resolved to a producer card)
        and from asset lineage dependencies. The result is used both for diagnostic
        downstream traversal and for push-time invalidation scheduling.
        """
        producer_by_asset = cls._producer_by_asset(cards, graph.assets, graph.runs)

        consumers_by_card: dict[str, set[str]] = {}
        for card in cards:
            for input_ref in card.inputs:
                if not input_ref.asset_id:
                    continue
                producer_card_id = producer_by_asset.get(input_ref.asset_id)
                if producer_card_id and producer_card_id != card.card_id:
                    consumers_by_card.setdefault(producer_card_id, set()).add(card.card_id)

        for asset in graph.assets:
            target_card_id = producer_by_asset.get(asset.asset_id)
            if not target_card_id:
                continue
            for upstream_asset_id in asset.depends_on:
                upstream_card_id = producer_by_asset.get(upstream_asset_id)
                if upstream_card_id and upstream_card_id != target_card_id:
                    consumers_by_card.setdefault(upstream_card_id, set()).add(target_card_id)

        return consumers_by_card

    def mutation_hint(self, snapshot: dict[str, Any], source_card_id: str) -> dict[str, Any]:
        affected = self.affected_downstream(snapshot, source_card_id)
        if not affected:
            return {
                "dependency_attention_check_recommended": False,
                "affected_downstream": [],
            }
        return {
            "dependency_attention_check_recommended": True,
            "affected_downstream": affected,
            "recommended_next_tool": "inspect_dependency_attention",
            "repair_execution_order_hint": [item["card_id"] for item in affected],
        }

    @staticmethod
    def attention_severity(issues: list[dict[str, Any]]) -> str | None:
        severities = {str(issue.get("severity") or "") for issue in issues}
        if "error" in severities:
            return "error"
        if "warning" in severities:
            return "warning"
        if "info" in severities:
            return "info"
        return None

    def _build_indexes(self, cards: list[Card], graph: GraphState) -> dict[str, Any]:
        resolution_service = InputResolutionService()
        card_by_id = {card.card_id: card for card in cards}
        asset_by_id = {asset.asset_id: asset for asset in graph.assets}
        run_by_id = {run.run_id: run for run in graph.runs}
        run_card_by_id = {run.run_id: run.card_id for run in graph.runs}

        materializations = graph.metadata.get("asset_materializations") if isinstance(graph.metadata, dict) else {}
        materializations = materializations or {}

        # Build alias index by planned id for alias fallback
        alias_assets_by_planned_id: dict[str, list[Asset]] = {}
        for asset in graph.assets:
            metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
            planned = str(metadata.get("planned_asset_id") or "").strip()
            if planned:
                alias_assets_by_planned_id.setdefault(planned, []).append(asset)

        # Build current_asset_by_card_role: binding-first, alias fallback
        current_asset_by_card_role: dict[tuple[str, str], Asset] = {}
        run_order_by_id = {run.run_id: index for index, run in enumerate(graph.runs)}
        for card in cards:
            for output in card.outputs:
                if self._is_system_output_role(output.role):
                    continue
                if not output.asset_id or not output.role:
                    continue
                logical_id = output.asset_id
                concrete = None
                # Priority 1: explicit materialization binding
                binding = materializations.get(logical_id)
                if binding:
                    current_id = binding.get("current_asset_id")
                    if current_id:
                        concrete = asset_by_id.get(current_id)
                # Priority 2: alias candidates (pick latest valid)
                if concrete is None:
                    candidates = alias_assets_by_planned_id.get(logical_id, [])
                    if candidates:
                        def _sort_key(a: Asset) -> tuple[int, int]:
                            return (
                                ASSET_STATUS_RANK.get(a.status, 99),
                                -(run_order_by_id.get(a.created_by_run or "", -1)),
                            )
                        best = sorted(candidates, key=_sort_key)[0]
                        if best.status == "valid":
                            concrete = best
                if concrete is not None:
                    current_asset_by_card_role[(card.card_id, output.role)] = concrete

        # Frozen input bindings from accepted cards' output assets (for stale detection)
        frozen_input_bindings_by_card: dict[str, list[dict]] = {}
        for card in cards:
            if card.status != "accepted":
                continue
            binding_sources: list[tuple[Asset, int]] = []
            for asset in graph.assets:
                if asset.asset_id not in card.linked_assets:
                    continue
                metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
                bindings = metadata.get("input_bindings")
                if bindings:
                    binding_sources.append((asset, run_order_by_id.get(asset.created_by_run or "", -1)))
            if binding_sources:
                binding_sources.sort(key=lambda x: x[1], reverse=True)
                frozen_input_bindings_by_card[card.card_id] = list(binding_sources[0][0].metadata.get("input_bindings") or [])

        # Legacy current_output_by_card_role (output objects for producer lookup)
        current_output_by_card_role: dict[tuple[str, str], Any] = {}
        for card in cards:
            for output in card.outputs:
                if self._is_system_output_role(output.role):
                    continue
                if not output.asset_id:
                    continue
                if output.role:
                    current_output_by_card_role[(card.card_id, output.role)] = output

        producer_card_by_asset: dict[str, str] = {}
        role_by_asset: dict[str, str] = {}
        for asset in graph.assets:
            metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
            producer_card_id = run_card_by_id.get(asset.created_by_run or "")
            if producer_card_id:
                producer_card_by_asset[asset.asset_id] = producer_card_id
                planned_asset_id = str(metadata.get("planned_asset_id") or "").strip()
                if planned_asset_id:
                    producer_card_by_asset.setdefault(planned_asset_id, producer_card_id)
                    # NOTE: do NOT setdefault asset_by_id — alias lookups use
                    # current_asset_by_card_role or alias_assets_by_planned_id.
            role = str(metadata.get("role") or "").strip()
            if role:
                role_by_asset[asset.asset_id] = role
                planned_asset_id = str(metadata.get("planned_asset_id") or "").strip()
                if planned_asset_id:
                    role_by_asset.setdefault(planned_asset_id, role)

        return {
            "card_by_id": card_by_id,
            "asset_by_id": asset_by_id,
            "run_by_id": run_by_id,
            "run_card_by_id": run_card_by_id,
            "current_output_by_card_role": current_output_by_card_role,
            "current_asset_by_card_role": current_asset_by_card_role,
            "producer_card_by_asset": producer_card_by_asset,
            "role_by_asset": role_by_asset,
            "input_resolution_service": resolution_service,
            "input_resolution_index": resolution_service.build_index(cards, graph),
            "materializations": materializations,
            "alias_assets_by_planned_id": alias_assets_by_planned_id,
            "frozen_input_bindings_by_card": frozen_input_bindings_by_card,
        }

    def _analyze_card_inputs(
        self,
        card: Card,
        indexes: dict[str, Any],
        issues: list[dict[str, Any]],
        *,
        max_lineage_nodes: int,
    ) -> None:
        if card.status not in self.ATTENTION_INPUT_ELIGIBLE_STATUSES:
            return

        asset_by_id: dict[str, Asset] = indexes["asset_by_id"]
        producer_card_by_asset = indexes["producer_card_by_asset"]
        role_by_asset = indexes["role_by_asset"]
        card_by_id: dict[str, Card] = indexes["card_by_id"]
        current_output_by_card_role = indexes["current_output_by_card_role"]
        input_resolution_service: InputResolutionService = indexes["input_resolution_service"]
        input_resolution_index = indexes["input_resolution_index"]

        for input_ref in card.inputs:
            asset_id = input_ref.asset_id
            if not asset_id:
                continue
            resolution = input_resolution_service.resolve_input(asset_id, input_resolution_index)
            asset = resolution.asset
            if not asset:
                issues.append(
                    self._issue(
                        kind="input_asset_missing",
                        severity="error",
                        card=card,
                        asset_id=asset_id,
                        requested_asset_id=resolution.requested_asset_id,
                        resolved_asset_id=resolution.resolved_asset_id,
                        resolved_by=resolution.resolved_by,
                        producer_card_id=resolution.producer_card_id,
                        producer_role=resolution.producer_role,
                        label=input_ref.label,
                        message=f"Input asset {input_ref.label or asset_id} is missing.",
                    )
                )
                continue

            if asset.status not in VALID_INPUT_ASSET_STATUSES:
                issues.append(
                    self._issue(
                        kind="input_asset_not_valid",
                        severity="error" if asset.status in self.ERROR_INPUT_STATUSES else "warning",
                        card=card,
                        asset_id=asset.asset_id,
                        requested_asset_id=resolution.requested_asset_id,
                        resolved_asset_id=resolution.resolved_asset_id,
                        resolved_by=resolution.resolved_by,
                        asset_status=asset.status,
                        label=input_ref.label,
                        message=f"Input asset {input_ref.label or asset.asset_id} has status {asset.status}.",
                    )
                )
                continue
            if asset.status == "candidate":
                continue

            producer_card_id = resolution.producer_card_id or producer_card_by_asset.get(asset.asset_id)
            role = resolution.producer_role or role_by_asset.get(asset.asset_id)
            if producer_card_id:
                producer_card = card_by_id.get(producer_card_id)
                if producer_card and producer_card.status in self.INACTIVE_PRODUCER_CARD_STATUSES:
                    issues.append(
                        self._issue(
                            kind="input_producer_card_inactive",
                            severity="warning",
                            card=card,
                            asset_id=asset.asset_id,
                            requested_asset_id=resolution.requested_asset_id,
                            resolved_asset_id=resolution.resolved_asset_id,
                            resolved_by=resolution.resolved_by,
                            asset_status=asset.status,
                            label=input_ref.label,
                            producer_card_id=producer_card.card_id,
                            producer_card_status=producer_card.status,
                            message=f"Input asset {input_ref.label or asset.asset_id} comes from inactive card {producer_card.card_id}.",
                        )
                    )
                if producer_card and role:
                    producer_roles = {output.role for output in producer_card.outputs if output.role and not self._is_system_output_role(output.role)}
                    if role not in producer_roles:
                        issues.append(
                            self._issue(
                                kind="input_producer_output_removed",
                                severity="warning",
                                card=card,
                                asset_id=asset.asset_id,
                                requested_asset_id=resolution.requested_asset_id,
                                resolved_asset_id=resolution.resolved_asset_id,
                                resolved_by=resolution.resolved_by,
                                asset_status=asset.status,
                                label=input_ref.label,
                                producer_card_id=producer_card_id,
                                producer_role=role,
                                message=f"Input asset {input_ref.label or asset.asset_id} uses removed output role {role}.",
                            )
                        )
                    current_asset_from_binding = indexes.get("current_asset_by_card_role", {}).get((producer_card_id, role))
                    if current_asset_from_binding is None:
                        # Legacy fallback: look up current_output.asset_id through asset_by_id
                        current_output = current_output_by_card_role.get((producer_card_id, role))
                        if current_output and current_output.asset_id:
                            current_asset_from_binding = asset_by_id.get(current_output.asset_id)
                            # If not found, try materialization binding
                            if current_asset_from_binding is None:
                                mats = indexes.get("materializations", {})
                                binding = mats.get(current_output.asset_id)
                                if binding:
                                    current_asset_from_binding = asset_by_id.get(binding.get("current_asset_id"))
                    outdated_by_binding = False
                    if (
                        current_asset_from_binding
                        and not resolution.is_virtual
                        and current_asset_from_binding.asset_id != asset.asset_id
                        and current_asset_from_binding.status in VALID_INPUT_ASSET_STATUSES
                    ):
                        issues.append(
                            self._issue(
                                kind="input_asset_outdated",
                                severity="warning",
                                card=card,
                                asset_id=asset.asset_id,
                                requested_asset_id=resolution.requested_asset_id,
                                resolved_asset_id=resolution.resolved_asset_id,
                                resolved_by=resolution.resolved_by,
                                asset_status=asset.status,
                                label=input_ref.label,
                                producer_card_id=producer_card_id,
                                producer_role=role,
                                current_asset_id=current_asset_from_binding.asset_id,
                                message=f"Input asset {input_ref.label or asset.asset_id} references an older {producer_card_id} output for role {role}.",
                            )
                        )
                        outdated_by_binding = True

                    # Frozen input binding stale check:
                    # For accepted cards with produced outputs, compare the concrete asset
                    # that was frozen at run-time against the current materialization.
                    # This catches cases where the input kept a logical id and the binding
                    # was updated by an upstream rerun after this card was accepted.
                    if not outdated_by_binding and card.status == "accepted":
                        frozen_bindings = indexes.get("frozen_input_bindings_by_card", {}).get(card.card_id, [])
                        frozen = next((b for b in frozen_bindings if b.get("requested_asset_id") == asset_id), None)
                        if frozen:
                            frozen_resolved_id = frozen.get("resolved_asset_id")
                            current_resolved_id = resolution.resolved_asset_id or (resolution.asset.asset_id if resolution.asset else None)
                            if frozen_resolved_id and current_resolved_id and frozen_resolved_id != current_resolved_id:
                                issues.append(
                                    self._issue(
                                        kind="input_asset_outdated",
                                        severity="warning",
                                        card=card,
                                        asset_id=asset_id,
                                        requested_asset_id=resolution.requested_asset_id,
                                        resolved_asset_id=resolution.resolved_asset_id,
                                        resolved_by=resolution.resolved_by,
                                        asset_status=asset.status,
                                        label=input_ref.label,
                                        producer_card_id=producer_card_id,
                                        producer_role=role,
                                        current_asset_id=current_resolved_id,
                                        frozen_resolved_asset_id=frozen_resolved_id,
                                        message=f"Input asset {input_ref.label or asset_id} was frozen as {frozen_resolved_id} when this card last ran, but the current materialization is {current_resolved_id}.",
                                    )
                                )

            invalid_roots, truncated = self._find_invalid_lineage_roots(asset.asset_id, asset_by_id, max_nodes=max_lineage_nodes)
            if invalid_roots:
                issues.append(
                    self._issue(
                        kind="asset_lineage_invalid",
                        severity="warning",
                        card=card,
                        asset_id=asset.asset_id,
                        requested_asset_id=resolution.requested_asset_id,
                        resolved_asset_id=resolution.resolved_asset_id,
                        resolved_by=resolution.resolved_by,
                        asset_status=asset.status,
                        label=input_ref.label,
                        upstream_invalid_assets=invalid_roots[:8],
                        truncated=truncated,
                        message=f"Input asset {input_ref.label or asset.asset_id} has invalid upstream lineage.",
                    )
                )

    def _analyze_card_outputs(
        self,
        card: Card,
        indexes: dict[str, Any],
        issues: list[dict[str, Any]],
        *,
        max_lineage_nodes: int,
    ) -> None:
        if card.status != "accepted":
            return
        asset_by_id: dict[str, Asset] = indexes["asset_by_id"]
        materializations: dict[str, dict] = indexes.get("materializations") or {}
        for output in card.outputs:
            if self._is_system_output_role(output.role):
                continue
            if not output.asset_id:
                issues.append(
                    self._issue(
                        kind="output_asset_not_valid",
                        severity="error",
                        card=card,
                        asset_status="missing",
                        producer_role=output.role,
                        message=f"Accepted card output {output.role or output.label} has no asset_id.",
                    )
                )
                continue
            asset = asset_by_id.get(output.asset_id)
            if not asset:
                # Try materialization binding for logical output ids.
                binding = materializations.get(output.asset_id)
                if binding:
                    current_asset_id = binding.get("current_asset_id")
                    if current_asset_id:
                        asset = asset_by_id.get(current_asset_id)
                if not asset:
                    # Check if the output.asset_id is a planned alias with candidates.
                    planned_assets = indexes.get("alias_assets_by_planned_id", {}).get(output.asset_id, [])
                    if planned_assets:
                        valid_assets = [a for a in planned_assets if a.status == "valid"]
                        if valid_assets:
                            asset = sorted(valid_assets, key=lambda a: a.created_by_run or "", reverse=True)[0]
            if not asset:
                issues.append(
                    self._issue(
                        kind="output_materialization_missing",
                        severity="error",
                        card=card,
                        asset_id=output.asset_id,
                        producer_role=output.role,
                        message=f"Accepted card output {output.role or output.label} has no current materialization for {output.asset_id}.",
                    )
                )
                continue
            if asset.status != "valid":
                issues.append(
                    self._issue(
                        kind="output_asset_not_valid",
                        severity="warning" if asset.status == "candidate" else "error",
                        card=card,
                        asset_id=asset.asset_id,
                        asset_status=asset.status,
                        producer_role=output.role,
                        message=f"Accepted card output {output.role or output.label} points to {asset.status} asset {asset.asset_id}.",
                    )
                )
                continue
            invalid_roots, truncated = self._find_invalid_lineage_roots(asset.asset_id, asset_by_id, max_nodes=max_lineage_nodes)
            if invalid_roots:
                issues.append(
                    self._issue(
                        kind="asset_lineage_invalid",
                        severity="warning",
                        card=card,
                        asset_id=asset.asset_id,
                        asset_status=asset.status,
                        producer_role=output.role,
                        upstream_invalid_assets=invalid_roots[:8],
                        truncated=truncated,
                        message=f"Output asset {output.role or asset.asset_id} has invalid upstream lineage.",
                    )
                )

    def _find_invalid_lineage_roots(
        self,
        start_asset_id: str,
        asset_by_id: dict[str, Asset],
        *,
        max_nodes: int,
    ) -> tuple[list[dict[str, str]], bool]:
        start = asset_by_id.get(start_asset_id)
        if not start:
            return [], False
        invalid: list[dict[str, str]] = []
        seen: set[str] = set()
        queue = list(start.depends_on)
        truncated = False
        while queue:
            if len(seen) >= max_nodes:
                truncated = True
                break
            upstream_id = queue.pop(0)
            if upstream_id in seen:
                continue
            seen.add(upstream_id)
            upstream = asset_by_id.get(upstream_id)
            if upstream is None:
                invalid.append({"asset_id": upstream_id, "status": "missing"})
                continue
            if upstream.status not in VALID_INPUT_ASSET_STATUSES:
                invalid.append({"asset_id": upstream.asset_id, "status": upstream.status})
                continue
            queue.extend(upstream.depends_on)
        return invalid, truncated

    def _issue(self, *, kind: str, severity: str, card: Card, **kwargs: Any) -> dict[str, Any]:
        issue = {
            "kind": kind,
            "severity": severity,
            "card_id": card.card_id,
            "card_title": card.title,
            **{key: value for key, value in kwargs.items() if value is not None},
        }
        issue["issue_id"] = self._issue_id(issue)
        if "suggested_actions" not in issue:
            issue["suggested_actions"] = [
                "检查下游结果是否仍可信",
                "必要时重跑该 card",
                "若用户确认沿用旧结果，可忽略该 attention",
            ]
        return issue

    def _dedupe_and_sort(self, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for issue in issues:
            deduped.setdefault(str(issue["issue_id"]), issue)
        severity_rank = {"error": 0, "warning": 1, "info": 2}
        return sorted(
            deduped.values(),
            key=lambda item: (
                severity_rank.get(str(item.get("severity")), 9),
                str(item.get("card_id") or ""),
                str(item.get("kind") or ""),
                str(item.get("asset_id") or ""),
                str(item.get("current_asset_id") or ""),
            ),
        )

    def _result(self, issues: list[dict[str, Any]], cards: list[Card]) -> dict[str, Any]:
        severity_counts: dict[str, int] = {}
        for issue in issues:
            severity = str(issue.get("severity") or "warning")
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
        return {
            "issue_count": len(issues),
            "fingerprint": self._fingerprint(issues),
            "issues": issues,
            "issues_by_card": self._issues_by_card(issues),
            "severity_counts": severity_counts,
            "card_count": len(cards),
        }

    @staticmethod
    def _issues_by_card(issues: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for issue in issues:
            card_id = issue.get("card_id")
            if card_id:
                grouped.setdefault(str(card_id), []).append(issue)
        return grouped

    @staticmethod
    def _issue_counts_by_card(issues: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for issue in issues:
            card_id = str(issue.get("card_id") or "")
            if card_id:
                counts[card_id] = counts.get(card_id, 0) + 1
        return counts

    @staticmethod
    def _issue_id(issue: dict[str, Any]) -> str:
        parts = [
            issue.get("kind") or "-",
            issue.get("card_id") or "-",
            issue.get("asset_id") or "-",
            issue.get("current_asset_id") or "-",
            issue.get("producer_role") or "-",
        ]
        return ":".join(str(part) for part in parts)

    def _fingerprint(self, issues: list[dict[str, Any]]) -> str:
        stable_parts = [
            (
                issue.get("kind"),
                issue.get("card_id"),
                issue.get("asset_id"),
                issue.get("asset_status"),
                issue.get("current_asset_id"),
                issue.get("producer_card_id"),
                issue.get("producer_role"),
            )
            for issue in sorted(issues, key=lambda item: str(item.get("issue_id") or ""))
        ]
        return sha256(json.dumps(stable_parts, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _is_system_output_role(role: str | None) -> bool:
        normalized = str(role or "").strip()
        return normalized in DependencyAttentionService.SYSTEM_OUTPUT_ROLE_SUFFIXES or normalized.endswith(
            DependencyAttentionService.SYSTEM_OUTPUT_ROLE_SUFFIXES
        )

    @staticmethod
    def _producer_by_asset(cards: list[Card], assets: list[Asset], runs: list[RunRecord]) -> dict[str, str]:
        run_card_by_id = {run.run_id: run.card_id for run in runs}
        producer_by_asset: dict[str, str] = {}
        for asset in assets:
            metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
            producer_card_id = run_card_by_id.get(asset.created_by_run or "")
            if producer_card_id:
                producer_by_asset[asset.asset_id] = producer_card_id
                planned_asset_id = str(metadata.get("planned_asset_id") or "").strip()
                if planned_asset_id:
                    producer_by_asset.setdefault(planned_asset_id, producer_card_id)
        for card in cards:
            for output in card.outputs:
                if output.asset_id:
                    producer_by_asset[output.asset_id] = card.card_id
        return producer_by_asset
