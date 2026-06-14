from __future__ import annotations

import json
import logging
import os
from hashlib import sha256
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.models.card_templates import CardTemplate, TemplateBundle, TemplateBundleFile, TemplateIoBinding, TemplateSpec
from app.models.cards import Card, CardAssetRef
from app.models.executor import ExecutorContext, ExecutorReference, ExecutorScriptAssetBinding
from app.models.memory import ProjectMemoryItem
from app.models.output_contracts import CardOutputSpec
from app.models.runs import Manifest
from app.services.app_config_service import AppConfigService
from app.services.asset_timeline_service import AssetTimelineService
from app.services.artifact_format_service import DEFAULT_CLASS_FORMAT, FORMAT_TO_CLASS
from app.services.background_workboard_service import BackgroundWorkboardService
from app.services.dependency_attention_service import DependencyAttentionService
from app.services.library_registry_service import LibraryRegistryService
from app.services.manager_planner import ManagerPlanningError
from app.services.module_group_state_service import ModuleGroupStateService
from app.services.mounted_data_directory_service import MountedDataDirectoryService
from app.services.package_service import PackageService
from app.services.project_service import ProjectService
from app.services.result_asset_service import ResultAssetService
from app.services.runtime_dependency_job_service import RuntimeDependencyJobService
from app.core.config import find_conda_solver
from app.services.runtime_dependency_resolver_service import (
    PACKAGE_STATUS_FALLBACK_REQUIRED,
    RESOLVER_STATUS_FALLBACK_AVAILABLE_BUT_AMBIGUOUS,
    RESOLVER_STATUS_FALLBACK_AVAILABLE_POLICY_DISALLOWS,
    RESOLVER_STATUS_FULLY_INSTALLABLE,
    RESOLVER_STATUS_PARTIAL_RESOLUTION,
    RESOLVER_STATUS_RUNTIME_MISSING,
    RESOLVER_STATUS_SOLVER_ERROR,
    RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC,
    RESOLVER_TO_P0_FIELDS,
    RuntimeDependencyResolverService,
    _contains_shell_danger as _contains_shell_danger_simple,
    collect_fallback_actions,
    is_registry_fallback_action_safe,
    normalize_fallback_policy,
)
from app.services.runtime_dependency_state_service import (
    find_duplicate_in_flight,
    find_duplicate_terminal_failure,
)
from app.services.utils import atomic_write_json, resolve_within, utc_now
from app.workers.command_worker import CommandTemplateWorkerAdapter
from app.services.worker_service import WorkerService


logger = logging.getLogger(__name__)

class DependencyResolutionError(ManagerPlanningError):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(str(payload.get("message") or payload.get("error_code") or "dependency resolution failed"))


class RuntimeBindingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conda_env: str | None = None
    r_env: str | None = None


class ConfigureCardExecutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    card_id: str | None = None
    card_ids: list[str] = Field(default_factory=list)
    skills: list[str] | None = None
    mcp_servers: list[str] | None = None
    runtime_bindings: RuntimeBindingsPayload | None = None
    instruction_blocks: list[str] | None = None


class StartCardRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    card_id: str


class StopCardRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    card_id: str
    reason: str | None = None


class ReviewCardRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    card_id: str


class CleanupRunHistoryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    card_id: str | None = None
    statuses: list[str] = Field(default_factory=list)
    keep_latest_per_card: bool = True
    include_valid_assets: bool = False
    dry_run: bool = False
    reason: str | None = None


class WorkboardItemPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str


class WorkboardSubmitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    todo_item_ids: list[str] = Field(default_factory=list)


class SearchCardTemplatesPayload(BaseModel):
    query: str = ""
    tags: list[str] = Field(default_factory=list)
    card_type: str | None = None
    limit: int = 5


class SaveCardTemplatePayload(BaseModel):
    card_id: str
    title: str | None = None
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)


class ScriptAssetBindingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_id: str
    asset_id: str


class InstantiateCardTemplatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str
    title: str | None = None
    step: int | None = None
    input_bindings: list[ManagerCardInputPayload] = Field(default_factory=list)
    script_asset_bindings: list[ScriptAssetBindingPayload] = Field(default_factory=list)


class InstallRuntimeDependenciesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ecosystem: str
    runtime: str
    packages: list[str] = Field(default_factory=list)
    timeout_seconds: int = 600
    source: dict[str, Any] = Field(default_factory=dict)
    # Optional per-request conda channels (e.g. ["bioconda"] for R bio pkgs).
    channels: list[str] = Field(default_factory=list)
    # Set by the resolver; never sent by Manager.
    installer_plan: list[dict[str, Any]] | None = None


class FindCardsPayload(BaseModel):
    query: str = ""
    status: str | None = None
    step: int | None = None
    asset_id: str | None = None
    limit: int = 12


class FindAssetsPayload(BaseModel):
    query: str = ""
    role: str | None = None
    artifact_class: str | None = None
    format: str | None = None
    producer_card_id: str | None = None
    status: str | None = None
    limit: int = 12


class InspectDependencyAttentionPayload(BaseModel):
    card_ids: list[str] = Field(default_factory=list)
    source_card_id: str | None = None
    include_recursive_downstream: bool = False
    max_issues: int = 50


class ManagerCardInputPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str


class ManagerCardOutputPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    artifact_class: str
    description: str | None = None


class CreateCardPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    summary: str
    step: int | None = None
    inputs: list[ManagerCardInputPayload] = Field(default_factory=list)
    outputs: list[ManagerCardOutputPayload] = Field(default_factory=list)


class UpdateCardPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str
    step: int | None = None
    inputs: list[ManagerCardInputPayload] | None = None
    outputs: list[ManagerCardOutputPayload] | None = None


class AnnotateCardPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str
    title: str | None = None
    summary: str | None = None
    manager_review: str | None = None
    manager_review_append: str | None = None


class CardWriteValidationError(ManagerPlanningError):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        first_message = ""
        errors = payload.get("errors") or []
        if errors:
            first_message = str(errors[0].get("message") or "")
        super().__init__(first_message or str(payload.get("error_type") or "card_write_validation_failed"))


class ManagerBlueprintTools:
    """Controlled tools exposed to the external manager agent runtime."""

    def __init__(
        self,
        project_service: ProjectService,
        worker_service: WorkerService | None = None,
        runtime_dependency_job_service: RuntimeDependencyJobService | None = None,
        runtime_dependency_resolver_service: RuntimeDependencyResolverService | None = None,
        library_registry_service: LibraryRegistryService | None = None,
        background_workboard_service: BackgroundWorkboardService | None = None,
        package_service: PackageService | None = None,
    ) -> None:
        self.project_service = project_service
        self.worker_service = worker_service
        self.runtime_dependency_job_service = runtime_dependency_job_service
        self.runtime_dependency_resolver_service = runtime_dependency_resolver_service
        self.library_registry_service = library_registry_service or LibraryRegistryService(
            project_service,
            AppConfigService(project_service.settings),
            project_service.settings,
        )
        self.package_service = package_service or PackageService(
            self.library_registry_service,
            project_service,
            runtime_dependency_resolver=self.runtime_dependency_resolver_service,
        )
        self.result_asset_service = ResultAssetService(project_service)
        self.asset_timeline_service = AssetTimelineService()
        self.dependency_attention_service = DependencyAttentionService()
        self.background_workboard_service = background_workboard_service
        self.mounted_data_directory_service = MountedDataDirectoryService(project_service, worker_service)

    def get_project_context(self, project_id: str) -> dict:
        snapshot = self.project_service.get_project_snapshot(project_id)
        project = snapshot["project"]
        graph = snapshot["graph"]
        return {
            "project": project.model_dump(),
            "cards": [card.model_dump() for card in snapshot["cards"]],
            "modules": [module.model_dump() for module in graph.modules],
            "assets": [asset.model_dump() for asset in graph.assets],
            "runs": [run.model_dump() for run in graph.runs],
            "claims": [claim.model_dump() for claim in graph.claims],
        }

    def inspect_project_summary(self, project_id: str) -> dict:
        snapshot = self.project_service.get_project_snapshot(project_id)
        project = snapshot["project"]
        graph = snapshot["graph"]
        timeline = self.asset_timeline_service.build(project_id, snapshot)
        timeline_cards = {item["card_id"]: item for item in timeline["cards"]}
        runs_by_card = self._runs_by_card(graph.runs)
        cards = [
            self._compact_card(card, timeline_cards.get(card.card_id), runs_by_card.get(card.card_id, []))
            for card in snapshot["cards"]
        ]
        status_counts: dict[str, int] = {}
        for card in snapshot["cards"]:
            status_counts[card.status] = status_counts.get(card.status, 0) + 1
        materialized_assets = len(graph.assets)
        planned_assets = len([asset for asset in timeline["assets"] if asset.get("planned")])
        active_runs = [
            self._compact_run(run)
            for run in graph.runs
            if run.status in {"queued", "running", "reviewing", "needs_approval"}
        ]
        blockers = self._project_blockers(snapshot, timeline)
        attention = self.dependency_attention_service.analyze_project(snapshot)
        attention_by_card = attention["issues_by_card"]
        cards = [
            {
                **card,
                "dependency_attention_count": len(attention_by_card.get(card["card_id"], [])),
                "attention_severity": DependencyAttentionService.attention_severity(
                    attention_by_card.get(card["card_id"], [])
                ),
            }
            for card in cards
        ]
        return {
            "project_id": project_id,
            "project": {
                "name": project.name,
                "status": project.status,
                "current_goal": project.current_goal,
                "runtime_preferences": project.runtime_preferences.model_dump(),
            },
            "cards": cards,
            "counts": {
                "cards": len(cards),
                "card_statuses": status_counts,
                "materialized_assets": materialized_assets,
                "planned_assets": planned_assets,
                "active_runs": len(active_runs),
                "blockers": len(blockers),
                "dependency_attention": attention["issue_count"],
            },
            "active_runs": active_runs[:8],
            "blockers": blockers[:12],
            "dependency_attention": attention["issues"][:12],
            "dependency_attention_count": attention["issue_count"],
            "dependency_attention_fingerprint": attention["fingerprint"],
            "timeline": {
                "parallel_batches": timeline["parallel_batches"],
                "cycle_card_ids": timeline["cycle_card_ids"],
                "duplicate_output_assets": timeline["duplicate_output_assets"],
            },
        }

    def get_background_workboard(self, project_id: str, session_id: str | None = None) -> dict:
        if self.background_workboard_service is None:
            raise ManagerPlanningError("background workboard service is unavailable.")
        view = self.background_workboard_service.get_workboard(project_id, session_id=session_id)
        result = view.model_dump()
        if self.worker_service is not None:
            result["available_slots"] = self.worker_service.get_available_run_slots(project_id)
        return result

    def promote_workboard_item_to_todo(self, project_id: str, payload: dict, session_id: str | None) -> dict:
        if self.background_workboard_service is None:
            raise ManagerPlanningError("background workboard service is unavailable.")
        request = WorkboardItemPayload.model_validate(payload)
        item = self.background_workboard_service.promote_workboard_item_to_todo(project_id, request.item_id, session_id or "")
        return {"ok": True, "item": item.model_dump()}

    def claim_workboard_item(self, project_id: str, payload: dict, session_id: str | None) -> dict:
        if self.background_workboard_service is None:
            raise ManagerPlanningError("background workboard service is unavailable.")
        request = WorkboardItemPayload.model_validate(payload)
        item = self.background_workboard_service.claim_workboard_item(project_id, request.item_id, session_id or "")
        return {"ok": True, "item": item.model_dump()}

    def complete_workboard_item(self, project_id: str, payload: dict, session_id: str | None) -> dict:
        if self.background_workboard_service is None:
            raise ManagerPlanningError("background workboard service is unavailable.")
        request = WorkboardItemPayload.model_validate(payload)
        item = self.background_workboard_service.complete_workboard_item(project_id, request.item_id, session_id or "")
        return {"ok": True, "item": item.model_dump()}

    def skip_workboard_item(self, project_id: str, payload: dict, session_id: str | None) -> dict:
        if self.background_workboard_service is None:
            raise ManagerPlanningError("background workboard service is unavailable.")
        request = WorkboardItemPayload.model_validate(payload)
        item = self.background_workboard_service.skip_workboard_item(project_id, request.item_id, session_id or "")
        return {"ok": True, "item": item.model_dump()}

    def defer_workboard_item(self, project_id: str, payload: dict, session_id: str | None) -> dict:
        if self.background_workboard_service is None:
            raise ManagerPlanningError("background workboard service is unavailable.")
        request = WorkboardItemPayload.model_validate(payload)
        item = self.background_workboard_service.defer_workboard_item(project_id, request.item_id, session_id or "")
        return {"ok": True, "item": item.model_dump()}

    def block_workboard_item_for_user(self, project_id: str, payload: dict, session_id: str | None) -> dict:
        if self.background_workboard_service is None:
            raise ManagerPlanningError("background workboard service is unavailable.")
        request = WorkboardItemPayload.model_validate(payload)
        item = self.background_workboard_service.block_workboard_item_for_user(project_id, request.item_id, session_id or "")
        return {"ok": True, "item": item.model_dump()}

    def reopen_workboard_item(self, project_id: str, payload: dict) -> dict:
        if self.background_workboard_service is None:
            raise ManagerPlanningError("background workboard service is unavailable.")
        request = WorkboardItemPayload.model_validate(payload)
        item = self.background_workboard_service.reopen_workboard_item(project_id, request.item_id)
        return {"ok": True, "item": item.model_dump()}

    def submit_claimed_workboard_items(self, project_id: str, payload: dict, session_id: str | None) -> dict:
        if self.background_workboard_service is None or self.worker_service is None:
            raise ManagerPlanningError("workboard run submission is unavailable.")
        request = WorkboardSubmitPayload.model_validate(payload)
        available_slots = self.worker_service.get_available_run_slots(project_id)
        result = self.background_workboard_service.submit_claimed_workboard_items(
            project_id,
            request.todo_item_ids,
            session_id=session_id or "",
            start_callback=lambda inner_project_id, card_id: self.worker_service.start_run(inner_project_id, card_id),
            max_starts=available_slots,
        )
        approval_required_count = sum(
            1
            for item in result["started"]
            if self._run_start_requires_manager_action(item)
        )
        rejected_count = sum(
            1
            for item in result["started"]
            if item.get("rejected_approvals") or item.get("status") == "cancelled"
        )
        background_started_count = sum(
            1
            for item in result["started"]
            if not self._run_start_requires_manager_action(item)
        )
        result["approval_required_count"] = approval_required_count
        result["rejected_count"] = rejected_count
        result["background_started_count"] = background_started_count
        result["deferred_count"] = len(result.get("deferred") or [])
        result["background"] = background_started_count > 0
        result["async_boundary"] = bool(background_started_count > 0 and approval_required_count == 0 and rejected_count == 0)
        result["do_not_poll"] = result["async_boundary"]
        result["wait_for_wake"] = result["async_boundary"]
        if result["started"]:
            first = result["started"][0]
            result["task_id"] = first.get("task_id")
            result["run_id"] = first.get("run_id")
            result["started_count"] = len(result["started"])
            result["batch"] = len(result["started"]) > 1
            if approval_required_count > 0:
                result["message"] = (
                    f"Submitted claimed workboard items, but {approval_required_count} run(s) still need approval. "
                    "Handle approvals in this turn; do not treat this batch as a background wait."
                )
            elif rejected_count > 0 and background_started_count == 0:
                result["message"] = "Claimed workboard items did not start because launch approvals were rejected."
            elif result["deferred_count"] > 0:
                result["message"] = (
                    f"Started {background_started_count} run(s); {result['deferred_count']} deferred due to capacity. "
                    "Deferred items will be available on the next wake after a slot frees up."
                )
            else:
                result["message"] = "Started claimed workboard run batch in the background. Do not poll in this turn."
        else:
            result["started_count"] = 0
            result["batch"] = False
            result["message"] = "No claimed workboard items could be started."
        return result

    @staticmethod
    def _run_start_requires_manager_action(response: dict[str, Any]) -> bool:
        return bool(
            response.get("status") == "needs_approval"
            or response.get("pending_approvals")
            or response.get("rejected_approvals")
        )

    def _manager_run_start_payload(self, response: dict[str, Any], *, rerun: bool = False) -> dict[str, Any]:
        requires_manager_action = self._run_start_requires_manager_action(response)
        rejected = bool(response.get("rejected_approvals"))
        if requires_manager_action:
            message = (
                "Rerun was created but requires approval before execution. Resolve the approval in this turn."
                if not rejected and rerun
                else "Run was created but requires approval before execution. Resolve the approval in this turn."
                if not rejected
                else "Run could not start because launch approval was rejected before execution."
            )
            return {
                "ok": not rejected,
                "can_start": False,
                "background": False,
                "async_boundary": False,
                "do_not_poll": False,
                "wait_for_wake": False,
                "message": message,
                **response,
            }
        return {
            "ok": True,
            "can_start": True,
            "background": True,
            "async_boundary": True,
            "do_not_poll": True,
            "wait_for_wake": True,
            "message": (
                "Rerun started in the background. Do not poll card status in this turn; wait for run events or a wake event."
                if rerun
                else "Run started in the background. Do not poll card status in this turn; wait for run events or a wake event."
            ),
            **response,
        }

    def find_cards(self, project_id: str, payload: dict) -> dict:
        request = FindCardsPayload.model_validate(payload)
        snapshot = self.project_service.get_project_snapshot(project_id)
        timeline = self.asset_timeline_service.build(project_id, snapshot)
        timeline_cards = {item["card_id"]: item for item in timeline["cards"]}
        runs_by_card = self._runs_by_card(snapshot["graph"].runs)
        query = self._normalize_query(request.query)
        asset_id = str(request.asset_id or "").strip()
        matches = []
        for card in snapshot["cards"]:
            if request.status and card.status != request.status:
                continue
            if request.step is not None and card.step != request.step:
                continue
            if asset_id and asset_id not in self._card_asset_ids(card):
                continue
            if query and query not in self._card_search_text(card):
                continue
            matches.append(self._compact_card(card, timeline_cards.get(card.card_id), runs_by_card.get(card.card_id, [])))
        limit = max(1, min(int(request.limit or 12), 50))
        return {
            "project_id": project_id,
            "items": matches[:limit],
            "total": len(matches),
            "query": request.query,
        }

    def find_assets(self, project_id: str, payload: dict) -> dict:
        request = FindAssetsPayload.model_validate(payload)
        snapshot = self.project_service.get_project_snapshot(project_id)
        timeline = self.asset_timeline_service.build(project_id, snapshot)
        materialized = {asset.asset_id: asset for asset in snapshot["graph"].assets}
        materializations = snapshot["graph"].metadata.get("asset_materializations") or {}
        # Build reverse index: current_asset_id -> logical planned_asset_id
        logical_by_concrete: dict[str, str] = {}
        for planned_id, binding in materializations.items():
            current_id = binding.get("current_asset_id")
            if current_id:
                logical_by_concrete.setdefault(current_id, planned_id)
        query = self._normalize_query(request.query)
        output_contracts = self._output_contracts_by_asset(snapshot["cards"])
        matches: list[dict[str, Any]] = []
        for record in timeline["assets"]:
            asset_id = str(record.get("asset_id") or "")
            contract = output_contracts.get(asset_id)
            compact = self._compact_asset_record(record, materialized.get(asset_id), contract)
            if not self._asset_matches(compact, query=query, request=request):
                continue
            # Enrich with materialization info
            mat_asset = materialized.get(asset_id)
            if mat_asset:
                metadata = mat_asset.metadata if isinstance(mat_asset.metadata, dict) else {}
                compact["planned_asset_id"] = metadata.get("planned_asset_id")
            logical_for = logical_by_concrete.get(asset_id)
            compact["current_for_logical"] = logical_for
            compact["is_current_materialization"] = logical_for is not None
            matches.append(compact)
        limit = max(1, min(int(request.limit or 12), 50))
        return {
            "project_id": project_id,
            "items": matches[:limit],
            "total": len(matches),
            "query": request.query,
        }

    def get_card_detail(self, project_id: str, card_id: str) -> dict:
        snapshot = self.project_service.get_project_snapshot(project_id)
        card = next((item for item in snapshot["cards"] if item.card_id == card_id), None)
        if not card:
            raise ManagerPlanningError(f"Card not found: {card_id}")
        timeline = self.asset_timeline_service.build(project_id, snapshot)
        timeline_card = next((item for item in timeline["cards"] if item["card_id"] == card_id), None)
        runs = [run for run in snapshot["graph"].runs if run.card_id == card_id]
        return {
            "project_id": project_id,
            "card": card.model_dump(),
            "timeline": timeline_card,
            "runs": [self._compact_run(run) for run in runs[-8:]],
            "dependency_attention": self.dependency_attention_service.issues_for_card(snapshot, card_id),
        }

    def inspect_dependency_attention(self, project_id: str, payload: dict | None = None) -> dict:
        request = InspectDependencyAttentionPayload.model_validate(payload or {})
        snapshot = self.project_service.get_project_snapshot(project_id)
        result = self.dependency_attention_service.inspect(
            snapshot,
            card_ids=request.card_ids,
            source_card_id=request.source_card_id,
            include_recursive_downstream=request.include_recursive_downstream,
            max_issues=request.max_issues,
        )
        if any(issue.get("kind") == "input_asset_outdated" and issue.get("current_asset_id") for issue in result.get("dependency_attention", [])):
            result["repair_guidance"] = (
                "input_asset_outdated means the downstream card still saves an old inputs[].asset_id. "
                "Use revise_card_plan to replace that input asset_id with current_asset_id, then use start_card_run; "
                "do not use rerun_card for dependency repair."
            )
        return {"project_id": project_id, **result}

    def get_asset_detail(self, project_id: str, asset_id: str) -> dict:
        if not asset_id:
            raise ManagerPlanningError("get_asset_detail requires asset_id.")
        return self.result_asset_service.get_asset_detail(project_id, asset_id)

    def list_data_assets(self, project_id: str) -> dict:
        snapshot = self.project_service.get_project_snapshot(project_id)
        timeline = self.asset_timeline_service.build(project_id, snapshot)
        assets_by_id = {asset.asset_id: asset for asset in snapshot["graph"].assets}
        return {
            "project_id": project_id,
            "assets": timeline["assets"],
            "cards": timeline["cards"],
            "materialized_assets": [asset.model_dump() for asset in snapshot["graph"].assets],
            "session_uploads": [asset.model_dump() for asset in snapshot["graph"].assets if self._is_session_upload(asset)],
            "workspace_files": [
                {
                    "asset_id": asset.asset_id,
                    "path": asset.path,
                    "title": asset.title,
                    "status": asset.status,
                    "asset_type": asset.asset_type,
                    "summary": asset.summary,
                }
                for asset in snapshot["graph"].assets
            ],
            "planned_assets": [
                asset
                for asset in timeline["assets"]
                if asset.get("planned") and asset.get("asset_id") not in assets_by_id
            ],
            "timeline": {
                "parallel_batches": timeline["parallel_batches"],
                "cycle_card_ids": timeline["cycle_card_ids"],
                "duplicate_output_assets": timeline["duplicate_output_assets"],
            },
            "tool_policy": self._tool_policy(snapshot),
        }

    def create_card(self, project_id: str, payload: dict) -> dict:
        snapshot = self.project_service.get_project_snapshot(project_id)
        card = self._normalize_create_card_payload(snapshot, payload)
        card, errors, warnings = self.asset_timeline_service.validate_card(snapshot, card)
        errors = self._append_output_role_errors(card, errors)
        if errors:
            raise CardWriteValidationError(self._card_write_error_response("create", card.card_id, errors))
        with self.project_service.lock_for(project_id):
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            if any(item.card_id == card.card_id for item in cards):
                raise CardWriteValidationError(
                    self._card_write_error_response(
                        "create",
                        card.card_id,
                        [f"Duplicate card_id: {card.card_id}"],
                    )
                )
            cards.append(card)
            graph = store.load_graph()
            self._sync_module_links(graph, card, previous_card=None)
            ModuleGroupStateService.sync_linked_module_status_from_card(card, graph.modules)
            ModuleGroupStateService.sync_group_hierarchy(cards, graph.modules)
            store.save_graph(graph)
            store.save_cards(cards)
            self._audit_card_tool(project_id, "create_card", card.card_id, payload)
        result = {"ok": True, "card": card.model_dump(), "card_id": card.card_id}
        if warnings:
            result["step_alignment_warnings"] = warnings
        result["parallel_group"] = f"step_{card.step or 1}"
        return result

    def update_card(self, project_id: str, payload: dict) -> dict:
        try:
            request = UpdateCardPayload.model_validate(payload)
        except ValidationError as exc:
            raise CardWriteValidationError(self._payload_validation_error_response("revise_card_plan", payload, exc)) from exc
        card_id = str(request.card_id or "").strip()
        snapshot = self.project_service.get_project_snapshot(project_id)
        existing = next((item for item in snapshot["cards"] if item.card_id == card_id), None)
        if not existing:
            raise CardWriteValidationError(
                {
                    "ok": False,
                    "error_type": "card_write_validation_failed",
                    "action": "revise_card_plan",
                    "errors": [
                        {
                            "code": "update_card_not_found",
                            "field": "card_id",
                            "card_id": card_id,
                            "message": f"Card not found: {card_id}",
                            "blocking": True,
                            "repair": {"tool": "find_cards", "query": card_id},
                        }
                    ],
                }
            )
        if request.step is None and request.inputs is None and request.outputs is None:
            raise CardWriteValidationError(
                {
                    "ok": False,
                    "error_type": "card_write_validation_failed",
                    "action": "revise_card_plan",
                    "errors": [
                        {
                            "code": "no_plan_fields_provided",
                            "field": None,
                            "card_id": card_id,
                            "message": "revise_card_plan requires at least one of step, inputs, or outputs.",
                            "blocking": True,
                        }
                    ],
                }
            )
        with self.project_service.lock_for(project_id):
            store = self.project_service.graph_store(project_id)
            graph = store.load_graph()
            cards = store.load_cards()
            index = next((idx for idx, item in enumerate(cards) if item.card_id == card_id), None)
            if index is None:
                raise CardWriteValidationError(
                    {
                        "ok": False,
                        "error_type": "card_write_validation_failed",
                        "action": "revise_card_plan",
                        "errors": [
                            {
                                "code": "update_card_not_found",
                                "field": "card_id",
                                "card_id": card_id,
                                "message": f"Card not found: {card_id}",
                                "blocking": True,
                            }
                        ],
                    }
                )
            previous = cards[index]
            locked_snapshot = {**snapshot, "cards": cards, "graph": graph}
            updated = self._normalize_update_card_payload(locked_snapshot, previous, request)
            updated, errors, warnings = self.asset_timeline_service.validate_card(locked_snapshot, updated, replacing_card_id=card_id)
            errors = self._append_output_role_errors(updated, errors)
            if errors:
                raise CardWriteValidationError(self._card_write_error_response("revise_card_plan", card_id, errors))
            updated = self._apply_plan_revision_status(previous, updated)
            cards[index] = updated
            self._sync_module_links(graph, updated, previous_card=previous)
            ModuleGroupStateService.sync_linked_module_status_from_card(updated, graph.modules)
            ModuleGroupStateService.sync_group_hierarchy(cards, graph.modules)
            store.save_graph(graph)
            store.save_cards(cards)
            self._audit_card_tool(project_id, "revise_card_plan", card_id, payload)
        hint = self.dependency_attention_service.mutation_hint(self.project_service.get_project_snapshot(project_id), updated.card_id)
        result = {"ok": True, "card": updated.model_dump(), "card_id": updated.card_id, **hint}
        if warnings:
            result["step_alignment_warnings"] = warnings
        result["parallel_group"] = f"step_{updated.step or 1}"
        return result

    def annotate_card(self, project_id: str, payload: dict) -> dict:
        try:
            request = AnnotateCardPayload.model_validate(payload)
        except ValidationError as exc:
            raise CardWriteValidationError(self._payload_validation_error_response("annotate", payload, exc)) from exc
        card_id = str(request.card_id or "").strip()
        if request.manager_review is not None and request.manager_review_append is not None:
            raise CardWriteValidationError(
                {
                    "ok": False,
                    "error_type": "card_write_validation_failed",
                    "action": "annotate",
                    "errors": [
                        {
                            "code": "ambiguous_manager_review_edit",
                            "field": "manager_review",
                            "card_id": card_id,
                            "message": "Use either manager_review for explicit replacement or manager_review_append for append-only edits, not both.",
                            "blocking": True,
                        }
                    ],
                }
            )
        if request.title is None and request.summary is None and request.manager_review is None and request.manager_review_append is None:
            raise CardWriteValidationError(
                {
                    "ok": False,
                    "error_type": "card_write_validation_failed",
                    "action": "annotate",
                    "errors": [
                        {
                            "code": "no_annotation_fields_provided",
                            "field": None,
                            "card_id": card_id,
                            "message": "annotate_card requires at least one of title, summary, manager_review, or manager_review_append.",
                            "blocking": True,
                        }
                    ],
                }
            )
        snapshot = self.project_service.get_project_snapshot(project_id)
        existing = next((item for item in snapshot["cards"] if item.card_id == card_id), None)
        if not existing:
            raise CardWriteValidationError(
                {
                    "ok": False,
                    "error_type": "card_write_validation_failed",
                    "action": "annotate",
                    "errors": [
                        {
                            "code": "update_card_not_found",
                            "field": "card_id",
                            "card_id": card_id,
                            "message": f"Card not found: {card_id}",
                            "blocking": True,
                            "repair": {"tool": "find_cards", "query": card_id},
                        }
                    ],
                }
            )
        with self.project_service.lock_for(project_id):
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            index = next((idx for idx, item in enumerate(cards) if item.card_id == card_id), None)
            if index is None:
                raise CardWriteValidationError(
                    {
                        "ok": False,
                        "error_type": "card_write_validation_failed",
                        "action": "annotate",
                        "errors": [
                            {
                                "code": "update_card_not_found",
                                "field": "card_id",
                                "card_id": card_id,
                                "message": f"Card not found: {card_id}",
                                "blocking": True,
                            }
                        ],
                    }
            )
            previous = cards[index]
            manager_review = previous.manager_review
            if request.manager_review is not None:
                manager_review = request.manager_review
            elif request.manager_review_append is not None:
                append_text = request.manager_review_append.strip()
                if append_text:
                    current = str(previous.manager_review or "").strip()
                    manager_review = f"{current}\n\n{append_text}" if current else append_text
            updated = previous.model_copy(
                update={
                    "title": request.title if request.title is not None else previous.title,
                    "summary": request.summary if request.summary is not None else previous.summary,
                    "manager_review": manager_review,
                }
            )
            cards[index] = updated
            store.save_cards(cards)
            self._audit_card_tool(project_id, "annotate_card", card_id, payload)
        return {"ok": True, "card": updated.model_dump(), "card_id": updated.card_id}

    def configure_card_execution(self, project_id: str, payload: dict) -> dict:
        try:
            request = ConfigureCardExecutionPayload.model_validate(payload)
        except ValidationError as exc:
            raise ManagerPlanningError(f"Invalid configure_card_execution payload: {exc}") from exc
        card_ids = [str(item).strip() for item in request.card_ids if str(item).strip()]
        if not card_ids:
            card_id = str(request.card_id or "").strip()
            if card_id:
                card_ids = [card_id]
        if not card_ids:
            raise ManagerPlanningError("configure_card_execution requires card_id or card_ids.")
        runtime_bindings = request.runtime_bindings.model_dump(exclude_unset=True) if request.runtime_bindings else {}
        with self.project_service.lock_for(project_id):
            store = self.project_service.graph_store(project_id)
            graph = store.load_graph()
            cards = store.load_cards()
            updated_cards: list[Card] = []
            missing = [card_id for card_id in card_ids if not any(card.card_id == card_id for card in cards)]
            if missing:
                raise ManagerPlanningError(f"Card not found: {', '.join(missing)}")
            active_card_ids = sorted(
                {
                    run.card_id
                    for run in graph.runs
                    if run.card_id in card_ids and run.status in WorkerService._active_run_statuses()
                }
            )
            if active_card_ids:
                raise ManagerPlanningError(
                    "configure_card_execution cannot modify cards with active runs: "
                    + ", ".join(active_card_ids)
                    + ". Wait for the run to finish or stop it first."
                )
            for card in cards:
                if card.card_id not in card_ids:
                    continue
                context = card.executor_context.model_copy(deep=True) if card.executor_context else ExecutorContext()
                if request.skills is not None:
                    context.skills = [str(item).strip() for item in request.skills if str(item).strip()]
                if request.mcp_servers is not None:
                    context.mcp_servers = [str(item).strip() for item in request.mcp_servers if str(item).strip()]
                touched_runtime = False
                if "conda_env" in runtime_bindings:
                    val = runtime_bindings.get("conda_env")
                    context.runtime_bindings.conda_env = val
                    context.runtime_bindings.python_runtime_source = "card_override" if val else None
                    touched_runtime = True
                if "r_env" in runtime_bindings:
                    val = runtime_bindings.get("r_env")
                    context.runtime_bindings.r_env = val
                    context.runtime_bindings.r_runtime_source = "card_override" if val else None
                    touched_runtime = True
                # Deprecated single-field runtime_source: derive from split sources so that
                # partial clears (e.g. conda_env=None but r_env still set) don't lose the signal.
                if touched_runtime:
                    py_src = context.runtime_bindings.python_runtime_source
                    r_src = context.runtime_bindings.r_runtime_source
                    context.runtime_bindings.runtime_source = py_src or r_src
                if request.instruction_blocks is not None:
                    new_blocks = [str(b).strip() for b in request.instruction_blocks if str(b).strip()]
                    merged = list(context.instruction_blocks or [])
                    for block in new_blocks:
                        if block not in merged:
                            merged.append(block)
                    context.instruction_blocks = merged
                card.executor_context = context
                updated_cards.append(card)
                self._audit_card_tool(project_id, "configure_card_execution", card.card_id, payload)
            store.save_cards(cards)
        return {"cards": [card.model_dump() for card in updated_cards], "updated_card_ids": [card.card_id for card in updated_cards]}

    def delete_card(self, project_id: str, payload: dict) -> dict:
        card_id = str(payload.get("card_id") or payload.get("module_id") or "").strip()
        if not card_id:
            raise ManagerPlanningError("delete_card requires card_id.")
        snapshot = self.project_service.get_project_snapshot(project_id)
        existing = next((item for item in snapshot["cards"] if item.card_id == card_id), None)
        if not existing:
            raise ManagerPlanningError(f"Card not found: {card_id}")
        updated = existing.model_copy(
            update={
                "status": "cancelled",
                "manager_review": str(payload.get("reason") or payload.get("message") or existing.manager_review or "").strip(),
                "next_actions": ["恢复卡片", "查看影响"],
            }
        )
        with self.project_service.lock_for(project_id):
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            index = next((idx for idx, item in enumerate(cards) if item.card_id == card_id), None)
            if index is None:
                raise ManagerPlanningError(f"Card not found: {card_id}")
            cards[index] = updated
            graph = store.load_graph()
            self._sync_module_links(graph, updated, previous_card=existing)
            ModuleGroupStateService.sync_linked_module_status_from_card(updated, graph.modules)
            ModuleGroupStateService.sync_group_hierarchy(cards, graph.modules)
            store.save_graph(graph)
            store.save_cards(cards)
            self._audit_card_tool(project_id, "delete_card", card_id, payload)
        after_snapshot = self.project_service.get_project_snapshot(project_id)
        hint = self.dependency_attention_service.mutation_hint(after_snapshot, updated.card_id)
        return {
            "card": updated.model_dump(),
            "timeline": self.asset_timeline_service.build(project_id, after_snapshot),
            **hint,
        }

    def get_tool_policy(self, project_id: str) -> dict:
        snapshot = self.project_service.get_project_snapshot(project_id)
        return self._tool_policy(snapshot)

    def set_tool_policy(self, project_id: str, payload: dict) -> dict:
        audit_card_tools = bool(payload.get("audit_card_tools", False))
        with self.project_service.lock_for(project_id):
            store = self.project_service.graph_store(project_id)
            graph = store.load_graph()
            graph.metadata["tool_policy"] = {"audit_card_tools": audit_card_tools}
            store.save_graph(graph)
        return {"tool_policy": {"audit_card_tools": audit_card_tools}}

    def read_result_asset(self, project_id: str, asset_id: str) -> dict:
        if not asset_id:
            raise ManagerPlanningError("read_result_asset requires asset_id.")
        return self.result_asset_service.get_asset_detail(project_id, asset_id)

    def list_data_directory(self, project_id: str, payload: dict | None = None) -> dict:
        body = payload or {}
        path = str(body.get("path") or "").strip()
        kind = str(body.get("kind") or "all").strip() or "all"
        cursor_value = body.get("cursor")
        cursor = str(cursor_value).strip() if cursor_value not in {None, ""} else None
        show_hidden = bool(body.get("show_hidden", False))
        try:
            return self.mounted_data_directory_service.list_entries(
                project_id,
                path=path,
                kind=kind,
                cursor=cursor,
                show_hidden=show_hidden,
            )
        except HTTPException as exc:
            raise ManagerPlanningError(str(exc.detail)) from exc

    def register_data_asset(self, project_id: str, payload: dict) -> dict:
        path = str(payload.get("path") or "").strip()
        if not path:
            raise ManagerPlanningError("register_data_asset requires path.")
        try:
            return self.mounted_data_directory_service.register_asset(project_id, path=path)
        except HTTPException as exc:
            raise ManagerPlanningError(str(exc.detail)) from exc

    def describe_data_asset(self, project_id: str, asset_id: str) -> dict:
        if not asset_id:
            raise ManagerPlanningError("describe_data_asset requires asset_id.")
        return self.result_asset_service.get_asset_detail(project_id, asset_id)

    def export_result(self, project_id: str, payload: dict) -> dict:
        asset_id = str(payload.get("asset_id") or "").strip()
        destination = str(payload.get("destination") or "").strip()
        overwrite = bool(payload.get("overwrite", False))
        if not asset_id:
            raise ManagerPlanningError("export_result requires asset_id.")
        if not destination:
            raise ManagerPlanningError("export_result requires destination.")
        try:
            return self.mounted_data_directory_service.export_asset(
                project_id,
                asset_id=asset_id,
                destination_path=destination,
                overwrite=overwrite,
                actor="manager",
            )
        except HTTPException as exc:
            raise ManagerPlanningError(str(exc.detail)) from exc

    def list_skill_library(self, project_id: str) -> dict:
        payload = self.library_registry_service.list_entries("skill", minimal=True)
        payload["project_id"] = project_id
        return payload

    def list_mcp_library(self, project_id: str) -> dict:
        payload = self.library_registry_service.list_entries("mcp", minimal=True)
        payload["project_id"] = project_id
        return payload

    def search_skill_library(self, project_id: str, payload: dict) -> dict:
        query = str(payload.get("query") or payload.get("q") or "").strip()
        runtime = str(payload.get("runtime") or "").strip() or None
        tags = [str(item).strip() for item in payload.get("tags") or [] if str(item).strip()]
        top_k = int(payload.get("top_k") or payload.get("limit") or 8)
        result = self.library_registry_service.search_entries(
            "skill",
            query=query,
            runtime=runtime,
            tags=tags,
            top_k=top_k,
        )
        result["project_id"] = project_id
        return result

    def search_mcp_library(self, project_id: str, payload: dict) -> dict:
        query = str(payload.get("query") or payload.get("q") or "").strip()
        runtime = str(payload.get("runtime") or "").strip() or None
        tags = [str(item).strip() for item in payload.get("tags") or [] if str(item).strip()]
        top_k = int(payload.get("top_k") or payload.get("limit") or 8)
        result = self.library_registry_service.search_entries(
            "mcp",
            query=query,
            runtime=runtime,
            tags=tags,
            top_k=top_k,
        )
        result["project_id"] = project_id
        return result

    def get_skill_library_item(self, project_id: str, skill_id: str) -> dict:
        try:
            payload = self.library_registry_service.get_entry("skill", skill_id)
        except ValueError as exc:
            raise ManagerPlanningError(str(exc)) from exc
        payload["project_id"] = project_id
        return payload

    def get_mcp_library_item(self, project_id: str, entry_id: str) -> dict:
        try:
            payload = self.library_registry_service.get_entry("mcp", entry_id)
        except ValueError as exc:
            raise ManagerPlanningError(str(exc)) from exc
        payload["project_id"] = project_id
        return payload

    # ------------------------------------------------------------------
    # Portable Card Package tools
    # ------------------------------------------------------------------

    def search_card_packages(self, project_id: str, payload: dict) -> dict:
        query = str(payload.get("query") or payload.get("q") or "").strip()
        runtime = str(payload.get("runtime") or "").strip() or None
        tags = [str(item).strip() for item in payload.get("tags") or [] if str(item).strip()]
        top_k = int(payload.get("top_k") or payload.get("limit") or 8)
        results = self.package_service.search_packages(
            query=query,
            runtime=runtime,
            tags=tags,
            top_k=top_k,
        )
        return {
            "project_id": project_id,
            "query": query,
            "items": results,
            "summary": f"{len(results)} package matches.",
        }

    def get_card_package_detail(self, project_id: str, package_id: str, version: str | None = None) -> dict:
        package = self.package_service.get_package(package_id, version)
        if package is None:
            raise ManagerPlanningError(f"Package not found: {package_id}")
        return {
            "project_id": project_id,
            "package_id": package_id,
            "version": package.manifest.version,
            "manifest": package.manifest.model_dump(),
            "bundle_files": list(package.bundle_files.keys()),
        }

    def import_card_package(self, project_id: str, payload: dict) -> dict:
        source_path = str(payload.get("source_path") or "").strip()
        overwrite = bool(payload.get("overwrite"))
        if not source_path:
            raise ManagerPlanningError("source_path is required.")
        result = self.package_service.import_package(source_path, overwrite=overwrite)
        return {
            "project_id": project_id,
            "status": result.status,
            "package_id": result.package_id,
            "version": result.version,
            "warnings": result.warnings,
            "blockers": result.blockers,
        }

    def instantiate_card_package(self, project_id: str, payload: dict) -> dict:
        package_id = str(payload.get("package_id") or "").strip()
        version = str(payload.get("version") or "").strip() or None
        input_bindings = dict(payload.get("input_bindings") or {})
        parameter_bindings = dict(payload.get("parameter_bindings") or {})
        runtime_override = dict(payload.get("runtime_override") or {})
        if not package_id:
            raise ManagerPlanningError("package_id is required.")
        result = self.package_service.instantiate_package(
            package_id=package_id,
            project_id=project_id,
            input_bindings=input_bindings,
            parameter_bindings=parameter_bindings,
            runtime_override=runtime_override or None,
            version=version,
        )
        if result.blockers:
            raise ManagerPlanningError("; ".join(result.blockers))
        return {
            "project_id": project_id,
            "card_id": result.card_id,
            "package_id": result.package_id,
            "version": result.version,
            "effective_python_runtime": result.effective_python_runtime,
            "effective_r_runtime": result.effective_r_runtime,
            "runtime_source": result.runtime_source,
            "python_runtime_source": result.python_runtime_source,
            "r_runtime_source": result.r_runtime_source,
            "warnings": result.warnings,
        }

    def install_runtime_dependencies(self, project_id: str, payload: dict, session_id: str | None = None) -> dict:
        if self.runtime_dependency_job_service is None:
            raise ManagerPlanningError("runtime dependency job service is unavailable.")
        try:
            request_payload = self._validated_runtime_dependency_payload(project_id, payload, session_id=session_id)
        except DependencyResolutionError as exc:
            return {
                "ok": False,
                "background": False,
                "async_boundary": False,
                "do_not_poll": False,
                "wait_for_wake": False,
                **exc.payload,
            }
        except ValidationError as exc:
            raise ManagerPlanningError(f"Invalid install_runtime_dependencies payload: {exc}") from exc

        ecosystem = request_payload["ecosystem"]
        runtime = request_payload["runtime"]
        packages = request_payload["packages"]
        project_path = self.project_service.project_path(project_id)

        # In-flight duplicate suppression
        in_flight_dup = find_duplicate_in_flight(project_path, ecosystem, runtime, packages)
        if in_flight_dup is not None:
            dedupe_key = f"dep:{ecosystem}:{runtime}:{','.join(sorted(str(p).strip().lower() for p in packages))}::"
            logger.info(
                "dep_cooling_rejected",
                extra={
                    "dedupe_key": dedupe_key,
                    "error_code": None,
                    "kind": "in_flight",
                    "project_id": project_id,
                    "runtime": runtime,
                },
            )
            phase = in_flight_dup.get("prior_phase") or in_flight_dup.get("prior_status") or "running"
            phase_messages = {
                "queued": "同一 runtime 的依赖任务已排队",
                "waiting_for_runtime_lock": "同一 runtime 正在等待前序依赖任务完成",
                "building_command": "同一 runtime 正在准备依赖安装命令",
                "launching_subprocess": "同一 runtime 正在启动依赖安装进程",
                "running_subprocess": "同一 runtime 正在执行依赖安装",
            }
            return {
                "ok": False,
                "background": False,
                "async_boundary": False,
                "do_not_poll": False,
                "wait_for_wake": False,
                "error_code": "duplicate_dependency_resolution_in_progress",
                "prior_job_id": in_flight_dup["prior_job_id"],
                "message": phase_messages.get(phase, "同一 runtime 的依赖安装正在进行中"),
                "retry_hint": "wait_for_existing_dependency_job",
            }

        # Terminal failure cooling
        terminal_dup = find_duplicate_terminal_failure(project_path, ecosystem, runtime, packages)
        if terminal_dup is not None and terminal_dup.get("prior_status") == "failed":
            dedupe_key = terminal_dup.get("dedupe_key") or ""
            prior_error_code = terminal_dup.get("prior_error_code")
            logger.info(
                "dep_cooling_rejected",
                extra={
                    "dedupe_key": dedupe_key,
                    "error_code": prior_error_code,
                    "kind": "terminal",
                    "project_id": project_id,
                    "runtime": runtime,
                },
            )
            return {
                "ok": False,
                "background": False,
                "async_boundary": False,
                "do_not_poll": False,
                "wait_for_wake": False,
                "error_code": "duplicate_dependency_resolution_failure",
                "prior_job_id": terminal_dup["prior_job_id"],
                "prior_error_code": prior_error_code,
                "dedupe_key": dedupe_key,
                "message": "The same dependency request already failed for this runtime.",
                "retry_hint": "do_not_retry_same_conda_request; modify package list, change runtime, or mark manually resolved",
                "fallback_available": terminal_dup.get("fallback_available"),
            }

        # Resolver-first planning (P1). The resolver inspects every package
        # before we create a background job, including under the active
        # fallback policy. When every package receives an approved action
        # (conda or single-family safe registry), the resolver produces a
        # structured ``installer_plan`` that the sync handler dispatches.
        installer_plan: list[dict[str, Any]] | None = None
        if self.runtime_dependency_resolver_service is not None:
            plan = self.runtime_dependency_resolver_service.resolve(
                project_id,
                request_payload,
                settings=self.project_service.settings,
                policy=self._fallback_policy(),
            )
            if plan.status != RESOLVER_STATUS_FULLY_INSTALLABLE:
                plan_dict = plan.to_dict()
                return self._resolver_blocked_response(
                    plan_dict,
                    policy=self._fallback_policy(),
                )
            # Approved actions are now in plan.installable.  Serialize
            # them so the sync handler never guesses.
            installer_plan = [act.to_dict() for act in plan.installable]

        if installer_plan is not None:
            # Set the installer plan on the request payload so the
            # sync handler can branch without introspecting raw packages.
            request_payload["installer_plan"] = installer_plan

        job = self.runtime_dependency_job_service.submit(
            project_id,
            request_payload,
            self._install_runtime_dependencies_sync,
        )
        manager_name = self._dependency_manager_label(request_payload["ecosystem"])
        return {
            "ok": True,
            "background": True,
            "async_boundary": True,
            "do_not_poll": True,
            "wait_for_wake": True,
            "task_id": job.task_id,
            "job_id": job.job_id,
            "status": job.status,
            "ecosystem": request_payload["ecosystem"],
            "runtime": request_payload["runtime"],
            "packages": request_payload["packages"],
            "manager": manager_name,
            "message": (
                f"Started background dependency installation for {request_payload['runtime']} "
                f"({len(request_payload['packages'])} package{'s' if len(request_payload['packages']) != 1 else ''})."
            ),
            "created_at": job.created_at,
        }

    def resolve_runtime_dependencies(self, project_id: str, payload: dict, session_id: str | None = None) -> dict:
        """Plan-only counterpart of ``install_runtime_dependencies``.

        Returns the resolver plan without creating a background job. Manager
        may call this to ask "what would happen if I tried this request?"
        before deciding to install.
        """
        if self.runtime_dependency_resolver_service is None:
            raise ManagerPlanningError("runtime dependency resolver service is unavailable.")
        try:
            request_payload = self._validated_runtime_dependency_payload(project_id, payload, session_id=session_id)
        except DependencyResolutionError as exc:
            payload_dict = dict(exc.payload)
            payload_dict.setdefault("status", self._status_for_error_code(payload_dict.get("error_code")))
            payload_dict.setdefault(
                "retry_hint", self._retry_hint_for_error_code(payload_dict.get("error_code"))
            )
            return {
                "ok": False,
                "tool": "resolve_runtime_dependencies",
                "background": False,
                "async_boundary": False,
                "do_not_poll": False,
                "wait_for_wake": False,
                **payload_dict,
            }
        except ValidationError as exc:
            raise ManagerPlanningError(f"Invalid resolve_runtime_dependencies payload: {exc}") from exc

        project_path = self.project_service.project_path(project_id)
        in_flight = find_duplicate_in_flight(
            project_path,
            request_payload["ecosystem"],
            request_payload["runtime"],
            request_payload["packages"],
        )
        terminal = find_duplicate_terminal_failure(
            project_path,
            request_payload["ecosystem"],
            request_payload["runtime"],
            request_payload["packages"],
        )

        plan = self.runtime_dependency_resolver_service.resolve(
            project_id,
            request_payload,
            settings=self.project_service.settings,
            policy=self._fallback_policy(),
        )
        plan_dict = plan.to_dict()
        plan_dict["ok"] = plan.status == RESOLVER_STATUS_FULLY_INSTALLABLE
        plan_dict["background"] = False
        plan_dict["async_boundary"] = False
        plan_dict["do_not_poll"] = False
        plan_dict["wait_for_wake"] = False
        if in_flight is not None:
            plan_dict["in_flight_duplicate"] = {
                "prior_job_id": in_flight["prior_job_id"],
                "error_code": "duplicate_dependency_resolution_in_progress",
                "retry_hint": "wait_for_existing_dependency_job",
            }
        if terminal is not None and terminal.get("prior_status") == "failed":
            plan_dict["terminal_duplicate"] = {
                "prior_job_id": terminal["prior_job_id"],
                "prior_error_code": terminal.get("prior_error_code"),
                "error_code": "duplicate_dependency_resolution_failure",
                "dedupe_key": terminal.get("dedupe_key"),
                "fallback_available": terminal.get("fallback_available"),
                "retry_hint": (
                    "do_not_retry_same_conda_request; modify package list, change runtime, "
                    "or mark manually resolved"
                ),
            }
        # Surface fallback policy so Manager can decide whether to suggest a
        # narrower fallback request.
        plan_dict["fallback_policy"] = self._fallback_policy()
        if plan_dict["fallback_policy"] == "allow_safe_registry_install":
            plan_dict["fallback_actions"] = [
                action.to_dict()
                for action in collect_fallback_actions(plan, policy=plan_dict["fallback_policy"])
                if is_registry_fallback_action_safe(action)
            ]
        return plan_dict

    @staticmethod
    def _status_for_error_code(error_code: str | None) -> str:
        mapping = {
            "github_source_install_not_supported": RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC,
            "external_source_install_not_supported": RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC,
            "dependency_install_start_failed": RESOLVER_STATUS_RUNTIME_MISSING,
            "package_not_found_in_conda_channels": "fallback_available_but_policy_disallows",
            "manual_preparation_required": "manual_preparation_required",
            "fallback_available_but_ambiguous": RESOLVER_STATUS_FALLBACK_AVAILABLE_BUT_AMBIGUOUS,
            "dependency_probe_failed": RESOLVER_STATUS_SOLVER_ERROR,
        }
        if not error_code:
            return "resolution_unknown"
        return mapping.get(error_code, "resolution_unknown")

    @staticmethod
    def _retry_hint_for_error_code(error_code: str | None) -> str:
        mapping = {
            "github_source_install_not_supported": "do_not_retry_installer",
            "external_source_install_not_supported": "do_not_retry_installer",
            "dependency_install_start_failed": "manual_runtime_preparation_required",
            "package_not_found_in_conda_channels": "choose_fallback",
            "manual_preparation_required": "manual_preparation_required",
            "fallback_available_but_ambiguous": "manual_preparation_required",
            "dependency_probe_failed": "inspect_stderr",
        }
        if not error_code:
            return "manual_preparation_required"
        return mapping.get(error_code, "manual_preparation_required")

    def _resolver_blocked_response(self, plan_dict: dict[str, Any], *, policy: str) -> dict[str, Any]:
        """Build the structured non-background response when the resolver blocks execution."""
        status = plan_dict.get("status") or ""
        retry_hint_map = {
            RESOLVER_STATUS_PARTIAL_RESOLUTION: "do_not_install_partial_request",
            RESOLVER_STATUS_FALLBACK_AVAILABLE_POLICY_DISALLOWS: "choose_fallback",
            RESOLVER_STATUS_FALLBACK_AVAILABLE_BUT_AMBIGUOUS: "manual_preparation_required",
            RESOLVER_STATUS_RUNTIME_MISSING: "manual_runtime_preparation_required",
            RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC: "do_not_retry_installer",
            RESOLVER_STATUS_SOLVER_ERROR: "inspect_stderr",
        }
        retry_hint = retry_hint_map.get(status, "manual_preparation_required")
        return {
            "ok": False,
            "background": False,
            "async_boundary": False,
            "do_not_poll": False,
            "wait_for_wake": False,
            "status": status,
            "error_code": plan_dict.get("error_code") or status,
            "message": plan_dict.get("message") or "The resolver blocked execution; do not submit the same request.",
            "retry_hint": retry_hint,
            "resolver_plan": {
                "request_dedupe_key": plan_dict.get("request_dedupe_key", ""),
                "installable": plan_dict.get("installable", []),
                "blocked": plan_dict.get("blocked", []),
            },
            "fallback_policy": policy,
            "fallback_available": self._fallback_families_from_plan(plan_dict),
        }

    @staticmethod
    def _fallback_families_from_plan(plan_dict: dict[str, Any]) -> list[str]:
        families: list[str] = []
        for entry in plan_dict.get("packages", []):
            for family in entry.get("fallback_available", []) or []:
                if family not in families:
                    families.append(family)
        return families

    def _fallback_policy(self) -> str:
        settings = getattr(self.project_service, "settings", None)
        if settings is None:
            return "allow_safe_registry_install"
        return normalize_fallback_policy(
            getattr(settings, "runtime_dependency_fallback_policy", None)
        )

    def get_runtime_dependency_install_status(self, project_id: str, job_id: str) -> dict:
        if self.runtime_dependency_job_service is None:
            raise ManagerPlanningError("runtime dependency job service is unavailable.")
        job = self.runtime_dependency_job_service.get_for_project(project_id, job_id)
        if job is None:
            raise ManagerPlanningError("Runtime dependency job not found.")
        if job.status in {"succeeded", "failed"} and job.future is not None:
            try:
                job.future.result(timeout=0.2)
            except Exception:
                pass
        result = job.result or {}
        payload = {
            "task_id": job.task_id,
            "job_id": job.job_id,
            "status": job.status,
            "phase": job.phase,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "payload": job.payload,
            "result": result or None,
            "error": job.error,
        }
        # P1 observability fields
        if job.command_preview is not None:
            payload["command_preview"] = job.command_preview
        if job.child_pid is not None:
            payload["child_pid"] = job.child_pid
        if job.log_path is not None:
            payload["log_path"] = job.log_path
        if job.last_heartbeat_at is not None:
            payload["last_heartbeat_at"] = job.last_heartbeat_at
        if job.status_detail is not None:
            payload["status_detail"] = job.status_detail
        if job.changed is not None:
            payload["changed"] = job.changed
        if result:
            payload.update(
                {
                    "ok": bool(result.get("ok")),
                    "message": result.get("message"),
                    "runtime": result.get("runtime"),
                    "resolved_runtime": result.get("resolved_runtime"),
                    "packages": result.get("packages"),
                    "manager": result.get("manager"),
                    "requested_package": result.get("requested_package"),
                    "attempted_candidates": result.get("attempted_candidates"),
                    "fallback_available": result.get("fallback_available"),
                    "error_code": result.get("error_code"),
                    "stdout_tail": result.get("stdout_tail"),
                    "stderr_tail": result.get("stderr_tail"),
                }
            )
        return payload

    def _install_runtime_dependencies_sync(self, project_id: str, payload: dict, phase_callback=None) -> dict:
        request = InstallRuntimeDependenciesPayload.model_validate(payload)
        ecosystem = self._normalize_dependency_ecosystem(request.ecosystem)
        packages = self._validate_dependency_packages(ecosystem, request.packages)
        runtime = str(request.runtime or "").strip()
        if not runtime or runtime == "__system__":
            raise ManagerPlanningError("install_runtime_dependencies requires a selected non-system runtime.")
        timeout = max(30, min(int(request.timeout_seconds or 600), 1800))
        started_at = utc_now()

        # P1.3: if the resolver attached an installer_plan from the approved
        # action set, dispatch directly on those structured actions. The
        # fallback plan is only set when the resolver has already verified
        # every action is safe (grammar-checked, single-family). We do not
        # allow partial or mixed-installer execution here — the resolver
        # blocks those before job creation.
        channels = [str(c).strip() for c in (payload.get("channels") or []) if str(c).strip()]
        installer_plan = payload.get("installer_plan")
        if installer_plan and isinstance(installer_plan, list):
            return self._install_from_plan(
                project_id,
                ecosystem=ecosystem,
                runtime=runtime,
                packages=packages,
                installer_plan=installer_plan,
                timeout=timeout,
                started_at=started_at,
                channels=channels,
                phase_callback=phase_callback,
            )

        # Legacy path (no resolver, or resolver not available): fall through
        # to the old per-ecosystem command builder.
        manager_name = self._dependency_manager_label(ecosystem)
        try:
            if ecosystem == "python":
                command, resolved_runtime = self._python_dependency_command(runtime, packages)
            else:
                command, resolved_runtime = self._r_dependency_command(runtime, packages, channels=channels)
        except DependencyResolutionError as exc:
            result = dict(exc.payload)
            result.update(
                {
                    "ok": False,
                    "ecosystem": ecosystem,
                    "runtime": runtime,
                    "packages": packages,
                    "manager": manager_name,
                    "started_at": started_at,
                    "finished_at": utc_now(),
                }
            )
            return result
        return self._run_dependency_command(
            project_id,
            command,
            ecosystem=ecosystem,
            runtime=runtime,
            resolved_runtime=resolved_runtime or "",
            packages=packages,
            manager_name=manager_name or "conda",
            timeout=timeout,
            started_at=started_at,
            phase_callback=phase_callback,
        )

    def _install_from_plan(
        self,
        project_id: str,
        *,
        ecosystem: str,
        runtime: str,
        packages: list[str],
        installer_plan: list[dict[str, Any]],
        timeout: int,
        started_at: str,
        channels: list[str] | None = None,
        phase_callback=None,
    ) -> dict:
        """Execute a resolver-approved installer_plan.

        The plan is guaranteed by the resolver to contain a single installer
        type for the entire request. We validate that invariant here so a
        corrupted payload cannot bypass the safety boundary, then build the
        command internally — never from Manager-provided text.
        """
        installer_types = {str(item.get("installer", "")).strip().lower() for item in installer_plan}
        if len(installer_types) != 1:
            return {
                "ok": False,
                "error_code": "mixed_installer_rejected",
                "ecosystem": ecosystem,
                "runtime": runtime,
                "packages": packages,
                "manager": "backend",
                "message": "Mixed-installer plans are not executable. Submit a narrower single-family request.",
                "started_at": started_at,
                "finished_at": utc_now(),
            }
        installer_type = next(iter(installer_types))
        names = [str(item.get("name", "")).strip() for item in installer_plan if item.get("name")]

        if installer_type == "conda":
            manager_name = "conda"
            # Use resolver-approved conda candidates from the plan to
            # avoid a redundant channel probe.
            conda_packages = [
                str(item.get("candidate") or item.get("name", "")).strip()
                for item in installer_plan
                if (item.get("candidate") or item.get("name"))
            ]
            if not conda_packages:
                return {
                    "ok": False,
                    "error_code": "package_not_found_in_conda_channels",
                    "ecosystem": ecosystem,
                    "runtime": runtime,
                    "packages": packages,
                    "manager": manager_name,
                    "message": "No conda candidates found in the installer_plan.",
                    "started_at": started_at,
                    "finished_at": utc_now(),
                }
            resolved_runtime, conda_bin = self._resolve_runtime_and_solver(runtime, ecosystem)
            effective_channels = list(channels or [])
            if ecosystem.lower() == "r":
                lower_set = {c.lower() for c in effective_channels}
                if "conda-forge" not in lower_set:
                    effective_channels.append("conda-forge")
                if "bioconda" not in lower_set:
                    effective_channels.append("bioconda")
            command = [str(conda_bin), "install", "-y", "-p", str(resolved_runtime)]
            for ch in effective_channels:
                command.extend(["-c", ch])
            command.extend(conda_packages)
            return self._run_dependency_command(
                project_id,
                command,
                ecosystem=ecosystem,
                runtime=runtime,
                resolved_runtime=resolved_runtime or "",
                packages=packages,
                manager_name=manager_name,
                timeout=timeout,
                started_at=started_at,
                phase_callback=phase_callback,
            )

        if installer_type == "pip":
            return self._run_pip_install(
                project_id,
                ecosystem=ecosystem,
                runtime=runtime,
                names=names,
                timeout=timeout,
                started_at=started_at,
                phase_callback=phase_callback,
            )

        if installer_type in {"cran", "bioconductor"}:
            return self._run_r_registry_install(
                project_id,
                ecosystem=ecosystem,
                runtime=runtime,
                names=names,
                installer_type=installer_type,
                timeout=timeout,
                started_at=started_at,
                phase_callback=phase_callback,
            )

        return {
            "ok": False,
            "error_code": "unsupported_installer",
            "ecosystem": ecosystem,
            "runtime": runtime,
            "packages": packages,
            "manager": "backend",
            "message": f"Installer {installer_type!r} is not supported.",
            "started_at": started_at,
            "finished_at": utc_now(),
        }

    def _run_dependency_command(
        self,
        project_id: str,
        command: list[str],
        *,
        ecosystem: str,
        runtime: str,
        resolved_runtime: str,
        packages: list[str],
        manager_name: str,
        timeout: int,
        started_at: str,
        phase_callback=None,
    ) -> dict:
        """Run a single subprocess command for dependency installation.

        If ``phase_callback`` is provided, it is called with ``(phase, **kwargs)``
        at ``launching_subprocess`` (before the call) and ``running_subprocess``
        (immediately before the blocking ``subprocess.run()``).  No ``child_pid``
        is provided yet because we still use ``subprocess.run()``; real child-pid
        tracking requires switching to ``Popen`` (future P1 work).
        """
        run_env = self._dependency_subprocess_env(ecosystem, manager_name, resolved_runtime) if ecosystem == "R" and manager_name != "conda" else None
        if phase_callback:
            phase_callback("launching_subprocess", command_preview=command)
            phase_callback("running_subprocess", command_preview=command)
        try:
            result = subprocess.run(
                command,
                cwd=self.project_service.project_path(project_id),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=run_env,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "error_code": "dependency_install_timeout",
                "ecosystem": ecosystem,
                "runtime": runtime,
                "resolved_runtime": resolved_runtime,
                "packages": packages,
                "manager": manager_name,
                "message": f"Dependency installation timed out after {timeout} seconds.",
                "stdout_tail": self._tail_text(exc.stdout),
                "stderr_tail": self._tail_text(exc.stderr),
                "started_at": started_at,
                "finished_at": utc_now(),
            }
        except OSError as exc:
            return {
                "ok": False,
                "error_code": "dependency_install_start_failed",
                "ecosystem": ecosystem,
                "runtime": runtime,
                "resolved_runtime": resolved_runtime,
                "packages": packages,
                "manager": manager_name,
                "message": f"Dependency installation could not start: {exc}",
                "stdout_tail": "",
                "stderr_tail": str(exc),
                "started_at": started_at,
                "finished_at": utc_now(),
            }
        ok = result.returncode == 0
        error_code = None
        if not ok:
            error_code = "dependency_install_failed"
        status_detail: str | None = None
        changed: bool | None = None
        message = "Dependency installation failed."
        if ok:
            if self._is_conda_noop(result.stdout):
                status_detail = "already_satisfied"
                changed = False
                message = "Dependencies already satisfied."
            else:
                status_detail = "install_completed"
                changed = True
                message = "Dependencies installed."
        return {
            "ok": ok,
            "error_code": error_code,
            "status_detail": status_detail,
            "changed": changed,
            "ecosystem": ecosystem,
            "runtime": runtime,
            "resolved_runtime": resolved_runtime,
            "packages": packages,
            "manager": manager_name,
            "returncode": result.returncode,
            "message": message,
            "stdout_tail": self._tail_text(result.stdout),
            "stderr_tail": self._tail_text(result.stderr),
            "started_at": started_at,
            "finished_at": utc_now(),
        }

    def _run_pip_install(
        self,
        project_id: str,
        *,
        ecosystem: str,
        runtime: str,
        names: list[str],
        timeout: int,
        started_at: str,
        phase_callback=None,
    ) -> dict:
        """Structured pip install — no Manager-built text."""
        from app.workers.command_worker import CommandTemplateWorkerAdapter
        conda_base, env_path = CommandTemplateWorkerAdapter._resolve_conda_runtime(
            runtime, self.project_service.settings
        )
        if not env_path.exists():
            return {
                "ok": False,
                "error_code": "dependency_install_start_failed",
                "ecosystem": ecosystem,
                "runtime": runtime,
                "resolved_runtime": str(env_path),
                "packages": names,
                "manager": "pip",
                "message": f"Python runtime not found: {env_path}",
                "stdout_tail": "",
                "stderr_tail": "",
                "started_at": started_at,
                "finished_at": utc_now(),
            }
        python_bin = env_path / "bin" / "python"
        if not python_bin.exists():
            return {
                "ok": False,
                "error_code": "dependency_install_start_failed",
                "ecosystem": ecosystem,
                "runtime": runtime,
                "resolved_runtime": str(env_path),
                "packages": names,
                "manager": "pip",
                "message": f"Python executable not found for runtime: {runtime}",
                "stdout_tail": "",
                "stderr_tail": "",
                "started_at": started_at,
                "finished_at": utc_now(),
            }
        # Use resolver-approved bare names; reject any that survived the
        # safety grammar by checking again before shelling out.
        safe_names: list[str] = []
        for name in names:
            stripped = str(name or "").strip()
            if not stripped or _contains_shell_danger_simple(stripped):
                return {
                    "ok": False,
                    "error_code": "unsupported_source_spec",
                    "ecosystem": ecosystem,
                    "runtime": runtime,
                    "resolved_runtime": str(env_path),
                    "packages": names,
                    "manager": "pip",
                    "message": f"Rejected unsafe package name: {stripped!r}.",
                    "started_at": started_at,
                    "finished_at": utc_now(),
                }
            safe_names.append(stripped)
        command = [str(python_bin), "-m", "pip", "install", *safe_names]
        return self._run_dependency_command(
            project_id,
            command,
            ecosystem=ecosystem,
            runtime=runtime,
            resolved_runtime=str(env_path),
            packages=safe_names,
            manager_name="pip",
            timeout=timeout,
            started_at=started_at,
            phase_callback=phase_callback,
        )

    def _run_r_registry_install(
        self,
        project_id: str,
        *,
        ecosystem: str,
        runtime: str,
        names: list[str],
        installer_type: str,
        timeout: int,
        started_at: str,
        phase_callback=None,
    ) -> dict:
        """Structured CRAN / Bioconductor install via Rscript."""
        from app.workers.command_worker import CommandTemplateWorkerAdapter
        rscript = CommandTemplateWorkerAdapter._resolve_rscript_runtime(
            runtime, self.project_service.settings
        )
        if rscript is None or not rscript.exists():
            return {
                "ok": False,
                "error_code": "dependency_install_start_failed",
                "ecosystem": ecosystem,
                "runtime": runtime,
                "resolved_runtime": str(rscript) if rscript else runtime,
                "packages": names,
                "manager": installer_type,
                "message": f"R runtime not found: {runtime}",
                "stdout_tail": "",
                "stderr_tail": "",
                "started_at": started_at,
                "finished_at": utc_now(),
            }
        env_path = rscript.parent.parent
        # Safety check: only bare R package names accepted.
        safe_names: list[str] = []
        for name in names:
            stripped = str(name or "").strip()
            if not stripped or _contains_shell_danger_simple(stripped) or "/" in stripped:
                return {
                    "ok": False,
                    "error_code": "unsupported_source_spec",
                    "ecosystem": ecosystem,
                    "runtime": runtime,
                    "resolved_runtime": str(env_path),
                    "packages": names,
                    "manager": installer_type,
                    "message": f"Rejected unsafe package name: {stripped!r}.",
                    "started_at": started_at,
                    "finished_at": utc_now(),
                }
            safe_names.append(stripped)
        import json as _json
        package_vector = "c(" + ", ".join(_json.dumps(item) for item in safe_names) + ")"
        if installer_type == "cran":
            expression = (
                'options(repos=c(CRAN="https://cloud.r-project.org")); '
                f"install.packages({package_vector}, dependencies=TRUE)"
            )
        else:
            expression = (
                'options(repos=c(CRAN="https://cloud.r-project.org")); '
                'if (!requireNamespace("BiocManager", quietly=TRUE)) install.packages("BiocManager"); '
                f"BiocManager::install({package_vector}, ask=FALSE, update=FALSE)"
            )
        command = [str(rscript), "--vanilla", "-e", expression]
        run_env = self._dependency_subprocess_env(ecosystem, installer_type, str(rscript))
        return self._run_dependency_command(
            project_id,
            command,
            ecosystem=ecosystem,
            runtime=runtime,
            resolved_runtime=str(env_path),
            packages=safe_names,
            manager_name=installer_type,
            timeout=timeout,
            started_at=started_at,
            phase_callback=phase_callback,
        )

    def _validated_runtime_dependency_payload(self, project_id: str, payload: dict, *, session_id: str | None = None) -> dict[str, Any]:
        request = InstallRuntimeDependenciesPayload.model_validate(payload)
        ecosystem = self._normalize_dependency_ecosystem(request.ecosystem)
        packages = self._validate_dependency_packages(ecosystem, request.packages)
        runtime = str(request.runtime or "").strip()
        if not runtime or runtime == "__system__":
            raise ManagerPlanningError("install_runtime_dependencies requires a selected non-system runtime.")
        timeout = max(30, min(int(request.timeout_seconds or 600), 1800))
        source = dict(request.source or {})
        run_id = str(source.get("run_id") or "").strip()
        card_id = str(source.get("card_id") or "").strip()
        if run_id and not card_id:
            graph = self.project_service.graph_store(project_id).load_graph()
            run = next((item for item in graph.runs if item.run_id == run_id), None)
            if run is not None:
                card_id = run.card_id
        if card_id:
            source["card_id"] = card_id
        if run_id:
            source["run_id"] = run_id
        if session_id:
            source["session_id"] = session_id
        return {
            "ecosystem": ecosystem,
            "runtime": runtime,
            "packages": packages,
            "timeout_seconds": timeout,
            "source": source,
            "channels": [str(c).strip() for c in (request.channels or []) if str(c).strip()],
        }

    def start_card_run(self, project_id: str, payload: dict) -> dict:
        if self.worker_service is None:
            raise ManagerPlanningError("worker service is unavailable for start_card_run.")
        request = StartCardRunPayload.model_validate(payload)
        try:
            response = self.worker_service.start_run(
                project_id,
                request.card_id,
            )
            return self._manager_run_start_payload(response, rerun=False)
        except HTTPException as exc:
            if exc.status_code == 409:
                detail = exc.detail
                block_details = detail.get("block_details") if isinstance(detail, dict) else {}
                return {
                    "ok": False,
                    "can_start": False,
                    "message": detail.get("message") if isinstance(detail, dict) else str(exc.detail),
                    "pending_approvals": [],
                    "rejected_approvals": [],
                    "error_code": detail.get("error_code") if isinstance(detail, dict) else None,
                    "card_id": detail.get("card_id") if isinstance(detail, dict) else request.card_id,
                    "job_id": detail.get("job_id") if isinstance(detail, dict) else None,
                    "retry_after_signal": detail.get("retry_after_signal") if isinstance(detail, dict) else None,
                    "block_reasons": block_details.get("block_reasons") if isinstance(block_details, dict) else [],
                    "block_details": block_details,
                }
            raise ManagerPlanningError(str(exc.detail)) from exc

    def stop_card_run(self, project_id: str, payload: dict) -> dict:
        if self.worker_service is None:
            raise ManagerPlanningError("worker service is unavailable for stop_card_run.")
        request = StopCardRunPayload.model_validate(payload)
        run_ids = self._active_run_ids_for_card(project_id, request.card_id)
        if not run_ids:
            return {
                "ok": False,
                "stopped": False,
                "message": "No active run found for the requested card.",
                "stopped_run_ids": [],
            }
        stopped_run_ids: list[str] = []
        failed_results: list[dict[str, Any]] = []
        for run_id in run_ids:
            try:
                self.worker_service.cancel_run(project_id, run_id, reason=request.reason)
                stopped_run_ids.append(run_id)
            except HTTPException as exc:
                failed_results.append({"run_id": run_id, "detail": str(exc.detail)})
        if not stopped_run_ids:
            raise ManagerPlanningError(
                "Failed to stop active runs for card "
                + request.card_id
                + (f": {failed_results[0]['detail']}" if failed_results else ".")
            )
        message = (
            f"Stopped {len(stopped_run_ids)} active run(s) for card {request.card_id}."
            if len(stopped_run_ids) > 1
            else f"Stopped active run {stopped_run_ids[0]} for card {request.card_id}."
        )
        return {
            "ok": True,
            "stopped": True,
            "card_id": request.card_id,
            "stopped_run_ids": stopped_run_ids,
            "failed_results": failed_results,
            "message": message,
        }

    def rerun_card(self, project_id: str, payload: dict) -> dict:
        if self.worker_service is None:
            raise ManagerPlanningError("worker service is unavailable for rerun_card.")
        request = StartCardRunPayload.model_validate(payload)
        self._assert_rerun_inputs_retryable(project_id, request.card_id)
        try:
            response = self.worker_service.rerun_card(
                project_id,
                request.card_id,
            )
            return self._manager_run_start_payload(response, rerun=True)
        except HTTPException as exc:
            raise ManagerPlanningError(str(exc.detail)) from exc

    def review_card_run(self, project_id: str, payload: dict) -> dict:
        if self.worker_service is None:
            raise ManagerPlanningError("worker service is unavailable for review_card_run.")
        request = ReviewCardRunPayload.model_validate(payload)
        run_id = self._latest_run_id_for_card(project_id, request.card_id)
        if not run_id:
            raise ManagerPlanningError("review_card_run requires a card with linked runs.")
        run = next(
            (item for item in self.project_service.graph_store(project_id).load_graph().runs if item.run_id == run_id),
            None,
        )
        if run is None:
            raise ManagerPlanningError(f"review_card_run run not found: {run_id}")
        if run.status in {"reviewed", "cancelled"}:
            raise ManagerPlanningError(f"review_card_run cannot finalize run {run_id} from status {run.status}.")
        try:
            response = self.worker_service.review_run(project_id, run_id, accept=True)
        except Exception as exc:
            raise ManagerPlanningError(str(exc)) from exc
        accepted = bool(response.get("accepted"))
        failure_reason = response.get("failure_reason")
        if accepted:
            message = "Run review accepted."
        elif failure_reason == "mapping_ambiguous":
            message = "Run review did not accept the result because output mapping is ambiguous."
        elif failure_reason == "consistency_failed":
            message = "Run review did not accept the result because graph consistency checks failed."
        else:
            message = "Run review did not accept the result."
        return {
            "ok": True,
            "review_completed": True,
            "card_id": request.card_id,
            "run_id": run_id,
            "accepted": accepted,
            "failure_reason": failure_reason,
            "failure_details": response.get("failure_details", []),
            "message": message,
        }

    def cleanup_run_history(self, project_id: str, payload: dict) -> dict:
        if self.worker_service is None:
            raise ManagerPlanningError("worker service is unavailable for cleanup_run_history.")
        request = CleanupRunHistoryPayload.model_validate(payload)
        snapshot = self.project_service.get_project_snapshot(project_id)
        graph = snapshot["graph"]
        requested_statuses = {str(status).strip() for status in request.statuses if str(status).strip()}
        default_statuses = {"failed", "cancelled", "reviewed"}
        statuses = requested_statuses or default_statuses
        valid_asset_run_ids = {
            asset.created_by_run
            for asset in graph.assets
            if asset.created_by_run and asset.status == "valid"
        }
        latest_by_card: dict[str, str] = {}
        for run in sorted(graph.runs, key=lambda item: item.started_at or item.finished_at or ""):
            latest_by_card[run.card_id] = run.run_id

        candidates = []
        for run in graph.runs:
            if request.run_id and run.run_id != request.run_id:
                continue
            if request.card_id and run.card_id != request.card_id:
                continue
            if run.status not in statuses:
                continue
            if run.cleanup_status == "completed":
                continue
            if request.keep_latest_per_card and not request.run_id and latest_by_card.get(run.card_id) == run.run_id:
                continue
            candidates.append(run)

        cleaned: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for run in candidates:
            if run.run_id in valid_asset_run_ids and not request.include_valid_assets:
                skipped.append(
                    {
                        "run_id": run.run_id,
                        "card_id": run.card_id,
                        "status": run.status,
                        "reason": "valid_assets_protected",
                    }
                )
                continue
            if request.dry_run:
                cleaned.append(
                    {
                        "run_id": run.run_id,
                        "card_id": run.card_id,
                        "status": run.status,
                        "dry_run": True,
                    }
                )
                continue
            try:
                response = self.worker_service.cleanup_run(
                    project_id,
                    run.run_id,
                    reason=request.reason or "Cleaned by Manager cleanup_run_history.",
                )
            except HTTPException as exc:
                skipped.append(
                    {
                        "run_id": run.run_id,
                        "card_id": run.card_id,
                        "status": run.status,
                        "reason": str(exc.detail),
                    }
                )
                continue
            cleaned.append({"card_id": run.card_id, **response})

        return {
            "ok": True,
            "dry_run": request.dry_run,
            "requested": {
                "run_id": request.run_id,
                "card_id": request.card_id,
                "statuses": sorted(statuses),
                "keep_latest_per_card": request.keep_latest_per_card,
                "include_valid_assets": request.include_valid_assets,
            },
            "cleaned": cleaned,
            "skipped": skipped,
            "cleaned_count": len(cleaned),
            "skipped_count": len(skipped),
            "message": f"Cleaned {len(cleaned)} run history item(s); skipped {len(skipped)}.",
        }

    def search_card_templates(self, project_id: str, payload: dict) -> dict:
        request = SearchCardTemplatesPayload.model_validate(payload)
        templates = self._load_card_templates()
        matches = self._match_templates(templates, request.query, request.tags, request.card_type)
        limit = max(1, min(int(request.limit or 5), 20))
        return {
            "project_id": project_id,
            "items": [item for item in matches[:limit]],
            "total": len(matches),
        }

    def save_card_template(self, project_id: str, payload: dict) -> dict:
        request = SaveCardTemplatePayload.model_validate(payload)
        snapshot = self.project_service.get_project_snapshot(project_id)
        card = next((item for item in snapshot["cards"] if item.card_id == request.card_id), None)
        if card is None:
            raise ManagerPlanningError(f"Card not found: {request.card_id}")
        run_id = self._latest_run_id_for_card(project_id, card.card_id)
        manifest = self._load_manifest_if_exists(project_id, run_id) if run_id else None
        if card.status != "accepted" and not self._run_reviewer_passed(project_id, run_id):
            raise ManagerPlanningError("Card template can only be saved from an accepted card or a run with reviewer pass.")

        template = self._build_card_template(project_id, card, manifest, request)
        templates = self._load_card_templates()
        templates = [item for item in templates if item.template_id != template.template_id]
        templates.append(template)
        self._save_card_templates(templates)
        self._save_template_bundle(template)
        return {"ok": True, "template": template.model_dump()}

    def instantiate_card_template(self, project_id: str, payload: dict) -> dict:
        request = InstantiateCardTemplatePayload.model_validate(payload)
        template = self._load_card_template(request.template_id)
        snapshot = self.project_service.get_project_snapshot(project_id)
        card_payload = self._instantiate_card_payload(project_id, snapshot, template, request)
        result = self.create_card(project_id, card_payload)
        self._apply_template_execution_context(project_id, result["card_id"], template, request)
        templates = self._load_card_templates()
        for index, item in enumerate(templates):
            if item.template_id != template.template_id:
                continue
            templates[index] = item.model_copy(
                update={
                    "reuse_count": item.reuse_count + 1,
                    "updated_at": utc_now(),
                }
            )
            break
        self._save_card_templates(templates)
        return {"template_id": template.template_id, **result}

    def _apply_template_execution_context(
        self,
        project_id: str,
        card_id: str,
        template: CardTemplate,
        request: InstantiateCardTemplatePayload,
    ) -> None:
        context = template.spec.executor_context.model_copy(deep=True)
        context.script_asset_requirements = [item.model_copy(deep=True) for item in template.spec.bundle.script_asset_requirements]
        context.template_metadata = {
            "template_id": template.template_id,
            "instantiated_at": utc_now(),
        }
        copied_references = self._copy_template_bundle_into_project(project_id, card_id, template)
        context.references.extend(copied_references)
        asset_map = {asset.asset_id: asset for asset in self.project_service.get_project_snapshot(project_id)["graph"].assets}
        bindings: list[ExecutorScriptAssetBinding] = []
        for binding_request in request.script_asset_bindings:
            asset = asset_map.get(binding_request.asset_id)
            if asset is None:
                raise ManagerPlanningError(f"Script asset not found: {binding_request.asset_id}")
            bindings.append(
                ExecutorScriptAssetBinding(
                    requirement_id=binding_request.requirement_id,
                    asset_id=asset.asset_id,
                    path=asset.path,
                    title=asset.title,
                    bound_at=utc_now(),
                )
            )
        context.script_asset_bindings = bindings
        runtime_bindings: dict[str, str | None] = {}
        if context.runtime_bindings.conda_env is not None:
            runtime_bindings["conda_env"] = context.runtime_bindings.conda_env
        if context.runtime_bindings.r_env is not None:
            runtime_bindings["r_env"] = context.runtime_bindings.r_env
        payload = {
            "card_id": card_id,
            "skills": list(context.skills),
            "mcp_servers": list(context.mcp_servers),
        }
        if runtime_bindings:
            payload["runtime_bindings"] = runtime_bindings
        self.configure_card_execution(project_id, payload)
        with self.project_service.lock_for(project_id):
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            index = next((idx for idx, item in enumerate(cards) if item.card_id == card_id), None)
            if index is None:
                raise ManagerPlanningError(f"Card not found: {card_id}")
            cards[index].executor_context = context
            store.save_cards(cards)

    def list_project_memory(self, project_id: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        kind = str(payload.get("kind") or "").strip()
        if kind and kind not in {"user_preference", "correction_memory"}:
            raise ManagerPlanningError("memory kind must be user_preference or correction_memory.")
        query = str(payload.get("query") or "").strip().lower()
        limit = self._memory_limit(payload.get("limit"))
        items = self.project_service.graph_store(project_id).load_project_memory()
        if kind:
            items = [item for item in items if item.kind == kind]
        if query:
            items = [item for item in items if query in item.summary.lower()]
        items = sorted(items, key=lambda item: item.updated_at, reverse=True)[:limit]
        summary_lines = [f"- {item.kind}: {item.summary}" for item in items]
        return {
            "project_id": project_id,
            "items": [item.model_dump() for item in items],
            "summary": "\n".join(summary_lines),
            "memory_policy": {
                "fact_source": "Use blueprint/cards/assets/runs for project execution facts.",
                "scope": "Project memory stores only explicit user preferences and corrections.",
            },
        }

    def write_project_memory(self, project_id: str, payload: dict) -> dict:
        kind = str(payload.get("kind") or "").strip()
        if kind not in {"user_preference", "correction_memory"}:
            raise ManagerPlanningError("write_project_memory requires kind user_preference or correction_memory.")
        summary = re.sub(r"\s+", " ", str(payload.get("summary") or "")).strip()
        if not summary:
            raise ManagerPlanningError("write_project_memory requires summary.")
        if len(summary) > 500:
            summary = summary[:497].rstrip() + "..."
        source = str(payload.get("source") or "manager_chat").strip() or "manager_chat"
        confidence = self._memory_confidence(payload.get("confidence"))
        memory_id = str(payload.get("memory_id") or "").strip() or self._memory_id(kind, summary)
        now = utc_now()
        with self.project_service.lock_for(project_id):
            store = self.project_service.graph_store(project_id)
            items = store.load_project_memory()
            existing_index = next((idx for idx, item in enumerate(items) if item.memory_id == memory_id), None)
            if existing_index is None:
                item = ProjectMemoryItem(
                    memory_id=memory_id,
                    kind=kind,
                    summary=summary,
                    source=source,
                    confidence=confidence,
                    created_at=now,
                    updated_at=now,
                )
                items.append(item)
            else:
                previous = items[existing_index]
                item = previous.model_copy(
                    update={
                        "kind": kind,
                        "summary": summary,
                        "source": source,
                        "confidence": confidence,
                        "updated_at": now,
                    }
                )
                items[existing_index] = item
            store.save_project_memory(items)
        return {"memory": item.model_dump(), "items_count": len(items)}

    @staticmethod
    def _normalize_card_payload(payload: dict, allow_missing_card_id: bool) -> Card:
        card_id = str(payload.get("card_id") or "").strip()
        if not card_id and not allow_missing_card_id:
            raise ManagerPlanningError("card_id is required.")
        inputs = [item if isinstance(item, CardAssetRef) else CardAssetRef.model_validate(item) for item in payload.get("inputs") or []]
        outputs = [item if isinstance(item, CardOutputSpec) else CardOutputSpec.model_validate(item) for item in payload.get("outputs") or []]
        card_payload = {
            "card_id": card_id,
            "card_type": payload.get("card_type") or "module",
            "title": str(payload.get("title") or "").strip(),
            "status": payload.get("status") or "planned",
            "step": payload.get("step"),
            "summary": str(payload.get("summary") or "").strip(),
            "why": str(payload.get("why") or "").strip(),
            "inputs": [item.model_dump() for item in inputs],
            "outputs": [item.model_dump() for item in outputs],
            "key_findings": list(payload.get("key_findings") or []),
            "manager_review": str(payload.get("manager_review") or "").strip(),
            "next_actions": list(payload.get("next_actions") or []),
            "linked_modules": list(payload.get("linked_modules") or []),
            "linked_runs": list(payload.get("linked_runs") or []),
            "linked_assets": list(payload.get("linked_assets") or []),
            "progress_note": payload.get("progress_note"),
            "executor_context": payload.get("executor_context"),
        }
        if not card_payload["title"]:
            raise ManagerPlanningError("card title is required.")
        if not card_payload["summary"]:
            raise ManagerPlanningError("card summary is required.")
        return Card.model_validate(card_payload)

    def _normalize_create_card_payload(self, snapshot: dict[str, Any], payload: dict) -> Card:
        try:
            request = CreateCardPayload.model_validate(payload)
        except ValidationError as exc:
            raise CardWriteValidationError(self._payload_validation_error_response("create", payload, exc)) from exc
        card_id = self._generated_card_id(request.title)
        return self._normalize_card_payload(
            {
                "card_id": card_id,
                "title": request.title,
                "status": "planned",
                "step": request.step,
                "summary": request.summary,
                "inputs": [self._normalized_input_ref(snapshot, item.asset_id) for item in request.inputs],
                "outputs": [
                    {
                        "role": item.role,
                        "label": self._display_label_from_role(item.role),
                        "artifact_class": item.artifact_class,
                        "accepted_formats": self._accepted_formats_for_class(item.artifact_class),
                        "preferred_format": self._preferred_format_for_class(item.artifact_class),
                        "description": item.description,
                    }
                    for item in request.outputs
                ],
            },
            allow_missing_card_id=False,
        )

    def _normalize_update_card_payload(self, snapshot: dict[str, Any], existing: Card, request: UpdateCardPayload) -> Card:
        update_payload: dict[str, Any] = {
            "card_id": existing.card_id,
            "status": existing.status,
            "step": request.step if request.step is not None else existing.step,
            "title": existing.title,
            "summary": existing.summary,
            "manager_review": existing.manager_review,
            "inputs": (
                [self._normalized_input_ref(snapshot, item.asset_id) for item in request.inputs]
                if request.inputs is not None
                else existing.inputs
            ),
        }
        if request.outputs is not None:
            prior_by_role = {str(output.role): output for output in existing.outputs}
            update_payload["outputs"] = [
                {
                    "role": item.role,
                    "label": self._display_label_from_role(item.role),
                    "artifact_class": item.artifact_class,
                    "accepted_formats": self._accepted_formats_for_class(item.artifact_class),
                    "preferred_format": self._preferred_format_for_class(item.artifact_class),
                    "description": item.description,
                    "asset_id": prior_by_role.get(str(item.role)).asset_id if prior_by_role.get(str(item.role)) else None,
                    "status": prior_by_role.get(str(item.role)).status if prior_by_role.get(str(item.role)) else None,
                }
                for item in request.outputs
            ]
        else:
            update_payload["outputs"] = existing.outputs
        return self._normalize_card_payload(update_payload, allow_missing_card_id=True)

    @staticmethod
    def _apply_plan_revision_status(previous: Card, updated: Card) -> Card:
        if updated.status == "planned":
            updated.progress_note = None
            return updated
        if updated.status in {"running", "reviewing"}:
            return updated
        updated.status = "planned"
        updated.progress_note = None
        if previous.status != "planned" and not str(updated.manager_review or "").strip():
            updated.manager_review = f"Card plan revised from previous status {previous.status}; awaiting a new run."
        return updated

    @staticmethod
    def _generated_card_id(title: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", str(title or "").strip().lower()).strip("_")[:32] or "card"
        stamp = utc_now().replace("-", "").replace(":", "").replace("T", "_").replace("Z", "")[:15]
        return f"card_{slug}_{stamp}"

    @staticmethod
    def _display_label_from_role(role: str) -> str:
        text = re.sub(r"[_\-]+", " ", str(role or "").strip())
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return "Output"
        return text.title()

    @staticmethod
    def _accepted_formats_for_class(artifact_class: str) -> list[str]:
        normalized = str(artifact_class or "").strip().lower()
        return [fmt for fmt, cls in FORMAT_TO_CLASS.items() if cls == normalized]

    @staticmethod
    def _preferred_format_for_class(artifact_class: str) -> str | None:
        return DEFAULT_CLASS_FORMAT.get(str(artifact_class or "").strip().lower())  # type: ignore[arg-type]

    @staticmethod
    def _append_output_role_errors(card: Card, errors: list[str]) -> list[str]:
        seen: set[str] = set()
        duplicate_roles: list[str] = []
        for output in card.outputs:
            role = str(output.role or "").strip()
            if not role:
                continue
            if role in seen and role not in duplicate_roles:
                duplicate_roles.append(role)
            seen.add(role)
        if not duplicate_roles:
            return errors
        return list(errors) + [f"Duplicate output role values: {', '.join(duplicate_roles)}"]

    @staticmethod
    def _input_label_from_asset_id(snapshot: dict[str, Any], asset_id: str) -> str:
        normalized_asset_id = str(asset_id or "").strip()
        if not normalized_asset_id:
            return "Input"
        graph = snapshot["graph"]
        materialized = next((asset for asset in graph.assets if asset.asset_id == normalized_asset_id), None)
        if materialized is not None:
            role = str(materialized.metadata.get("role") or "").strip() if isinstance(materialized.metadata, dict) else ""
            return role or materialized.title or normalized_asset_id
        for card in snapshot["cards"]:
            for output in card.outputs:
                if output.asset_id == normalized_asset_id:
                    return output.label or output.role or normalized_asset_id
        return normalized_asset_id

    def _normalized_input_ref(self, snapshot: dict[str, Any], asset_id: str) -> dict[str, Any]:
        normalized_asset_id = str(asset_id or "").strip()
        return {
            "asset_id": normalized_asset_id,
            "label": self._input_label_from_asset_id(snapshot, normalized_asset_id),
        }

    def _payload_validation_error_response(self, action: str, payload: dict, exc: ValidationError) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        for item in exc.errors():
            loc = item.get("loc") or ()
            field = ".".join(str(part) for part in loc)
            message = str(item.get("msg") or "Invalid payload.")
            code = "invalid_payload"
            if "title" in field:
                code = "empty_title"
            elif "summary" in field:
                code = "empty_summary"
            elif "artifact_class" in field:
                code = "invalid_artifact_class"
            errors.append(
                {
                    "code": code,
                    "field": field,
                    "message": message,
                    "blocking": True,
                }
            )
        return {
            "ok": False,
            "error_type": "card_write_validation_failed",
            "action": action,
            "errors": errors or [{"code": "invalid_payload", "message": "Invalid payload.", "blocking": True}],
        }

    def _card_write_error_response(self, action: str, card_id: str, messages: list[str]) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        for message in messages:
            lowered = message.lower()
            code = "card_write_validation_failed"
            field = None
            repair: dict[str, Any] | None = None
            cycle_card_ids: list[str] | None = None
            asset_id: str | None = None
            if "step too early" in lowered:
                code = "step_too_early"
                field = "step"
            elif "card step is required" in lowered:
                code = "missing_step"
                field = "step"
            elif "input asset" in lowered and "missing" in lowered:
                code = "input_asset_not_selectable"
                field = "inputs"
                asset_id = re.search(r"Input asset ([^ ]+)", message).group(1) if re.search(r"Input asset ([^ ]+)", message) else None
                repair = {"tool": "find_assets", "query": asset_id or ""}
            elif "planned output asset_id already exists as a materialized asset" in lowered:
                code = "duplicate_output_asset_id"
                field = "outputs"
            elif "duplicate planned output asset_id values" in lowered:
                code = "duplicate_output_asset_id"
                field = "outputs"
            elif "duplicate output role values" in lowered:
                code = "output_role_duplicate"
                field = "outputs"
            elif "dependency cycle" in lowered:
                code = "dependency_cycle"
                field = "inputs"
                cycle_card_ids = [card_id]
            elif "duplicate" in lowered and "asset_id" in lowered:
                code = "duplicate_output_asset_id"
                field = "outputs"
            errors.append(
                {
                    "code": code,
                    "field": field,
                    "card_id": card_id,
                    "asset_id": asset_id,
                    "cycle_card_ids": cycle_card_ids,
                    "message": message,
                    "blocking": True,
                    "repair": repair,
                }
            )
        return {
            "ok": False,
            "error_type": "card_write_validation_failed",
            "action": action,
            "errors": errors,
        }

    @staticmethod
    def _tool_policy(snapshot: dict) -> dict:
        metadata = snapshot["graph"].metadata if snapshot.get("graph") else {}
        policy = metadata.get("tool_policy") if isinstance(metadata, dict) else {}
        if not isinstance(policy, dict):
            policy = {}
        return {"audit_card_tools": bool(policy.get("audit_card_tools", False))}

    def _audit_card_tool(self, project_id: str, action: str, card_id: str, payload: dict) -> None:
        if not self._tool_policy(self.project_service.get_project_snapshot(project_id))["audit_card_tools"]:
            return
        store = self.project_service.graph_store(project_id)
        graph = store.load_graph()
        audit_log = list(graph.metadata.get("card_tool_audit") or [])
        audit_log.append(
            {
                "action": action,
                "card_id": card_id,
                "payload": payload,
                "created_at": utc_now(),
            }
        )
        graph.metadata["card_tool_audit"] = audit_log
        store.save_graph(graph)

    @staticmethod
    def _memory_id(kind: str, summary: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", summary.lower()).strip("_")[:32] or "memory"
        digest = sha256(f"{kind}:{summary}".encode("utf-8")).hexdigest()[:10]
        return f"{kind}_{slug}_{digest}"

    @staticmethod
    def _memory_limit(value: object) -> int:
        try:
            limit = int(value or 5)
        except (TypeError, ValueError):
            limit = 5
        return max(1, min(limit, 10))

    @staticmethod
    def _memory_confidence(value: object) -> float:
        try:
            confidence = float(value if value is not None else 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        return max(0.0, min(confidence, 1.0))

    @staticmethod
    def _normalize_dependency_ecosystem(ecosystem: str) -> str:
        normalized = str(ecosystem or "").strip().lower()
        if normalized == "python":
            return "python"
        if normalized == "r":
            return "R"
        raise ManagerPlanningError("install_runtime_dependencies ecosystem must be python or R.")

    @staticmethod
    def _validate_dependency_packages(ecosystem: str, packages: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in packages if str(item).strip()]
        if not cleaned:
            raise ManagerPlanningError("install_runtime_dependencies requires at least one package.")
        if len(cleaned) > 40:
            raise ManagerPlanningError("install_runtime_dependencies accepts at most 40 packages per call.")
        source_install_specs = [
            item for item in cleaned
            if "github.com" in item.lower()
            or item.lower().startswith("git+")
            or item.lower().startswith("http://")
            or item.lower().startswith("https://")
            or item.lower().endswith(".tar.gz")
            or "/" in item
        ]
        if source_install_specs:
            code = "github_source_install_not_supported" if any("github" in item.lower() or "/" in item for item in source_install_specs) else "external_source_install_not_supported"
            raise DependencyResolutionError(
                {
                    "error_code": code,
                    "requested_package": source_install_specs[0],
                    "attempted_candidates": [],
                    "fallback_available": [],
                    "message": (
                        "Source-install dependencies are not supported by install_runtime_dependencies. "
                        "Use a separate explicit environment-preparation workflow instead."
                    ),
                }
            )
        python_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(\[[A-Za-z0-9_,.-]+\])?([<>=!~]=?[A-Za-z0-9_.+*-]+)?$")
        r_re = re.compile(r"^[A-Za-z][A-Za-z0-9.]*$")
        invalid = [
            item
            for item in cleaned
            if not (python_re.fullmatch(item) if ecosystem == "python" else r_re.fullmatch(item))
        ]
        if invalid:
            raise ManagerPlanningError(f"Unsupported package specifier(s): {', '.join(invalid)}")
        return list(dict.fromkeys(cleaned))

    def _python_dependency_command(self, runtime: str, packages: list[str]) -> tuple[list[str], str]:
        manager_name = self._dependency_manager_label("python")
        conda_base, env_path = CommandTemplateWorkerAdapter._resolve_conda_runtime(runtime, self.project_service.settings)
        if not env_path.exists():
            raise ManagerPlanningError(f"Python runtime not found: {runtime}")
        if manager_name == "conda":
            conda_bin = self._resolve_conda_solver(conda_base)
            resolved_packages = [self._resolve_conda_python_package(conda_bin, package) for package in packages]
            return [str(conda_bin), "install", "-y", "-p", str(env_path), *resolved_packages], str(env_path)
        python_bin = env_path / "bin" / "python"
        if not python_bin.exists():
            raise ManagerPlanningError(f"Python executable not found for runtime: {runtime}")
        return [str(python_bin), "-m", "pip", "install", *packages], str(env_path)

    def _r_dependency_command(self, runtime: str, packages: list[str], channels: list[str] | None = None) -> tuple[list[str], str]:
        manager_name = self._dependency_manager_label("R")
        rscript = CommandTemplateWorkerAdapter._resolve_rscript_runtime(runtime, self.project_service.settings)
        if rscript is None or not rscript.exists():
            raise ManagerPlanningError(f"R runtime not found: {runtime}")
        env_path = rscript.parent.parent
        conda_base = env_path.parent.parent if env_path.parent.name == "envs" else env_path
        if manager_name == "conda":
            conda_bin = self._resolve_conda_solver(conda_base)
            conda_packages = [self._resolve_conda_r_package(conda_bin, package) for package in packages]
            effective_channels = list(channels or [])
            lower_set = {c.lower() for c in effective_channels}
            if "conda-forge" not in lower_set:
                effective_channels.append("conda-forge")
            if "bioconda" not in lower_set:
                effective_channels.append("bioconda")
            cmd = [
                str(conda_bin),
                "install",
                "-y",
                "-p",
                str(env_path),
            ]
            for ch in effective_channels:
                cmd.extend(["-c", ch])
            cmd.extend(conda_packages)
            return cmd, str(env_path)
        package_vector = "c(" + ", ".join(json.dumps(item) for item in packages) + ")"
        if manager_name == "cran":
            expression = (
                'options(repos=c(CRAN="https://cloud.r-project.org")); '
                f"install.packages({package_vector}, dependencies=TRUE)"
            )
        else:
            expression = (
                'options(repos=c(CRAN="https://cloud.r-project.org")); '
                'if (!requireNamespace("BiocManager", quietly=TRUE)) install.packages("BiocManager"); '
                f"BiocManager::install({package_vector}, ask=FALSE, update=FALSE)"
            )
        return [str(rscript), "--vanilla", "-e", expression], str(rscript)

    def _resolve_runtime_and_solver(
        self, runtime: str, ecosystem: str
    ) -> tuple[Path, Path]:
        """Return (resolved_runtime_path, conda_bin) without package probing."""
        from app.workers.command_worker import CommandTemplateWorkerAdapter
        conda_base, env_path = CommandTemplateWorkerAdapter._resolve_conda_runtime(
            runtime, self.project_service.settings
        )
        if not env_path.exists():
            raise ManagerPlanningError(f"Runtime not found: {runtime}")
        conda_bin = self._resolve_conda_solver(conda_base)
        return env_path, conda_bin

    @staticmethod
    def _dependency_manager_label(ecosystem: str) -> str:
        return "conda"

    @staticmethod
    def _dependency_subprocess_env(ecosystem: str, manager_name: str, resolved_runtime: str) -> dict[str, str] | None:
        if ecosystem != "R" or manager_name == "conda":
            return None
        runtime_bin = Path(resolved_runtime).parent
        env = dict(os.environ)
        prior_path = env.get("PATH", "")
        env["PATH"] = f"{runtime_bin}{os.pathsep}{prior_path}" if prior_path else str(runtime_bin)
        return env

    @staticmethod
    def _resolve_conda_solver(conda_base: Path) -> Path:
        solver = find_conda_solver(conda_base)
        if solver is not None:
            return solver
        micromamba = shutil.which("micromamba")
        if micromamba:
            return Path(micromamba)
        raise ManagerPlanningError(f"No conda solver found at {conda_base}/bin/ (mamba or conda).")

    def _resolve_conda_python_package(self, conda_bin: Path, package: str) -> str:
        candidates = self._python_conda_candidates(package)
        return self._resolve_conda_package_candidate(
            conda_bin,
            requested_package=package,
            candidates=candidates,
            fallback_available=["pip"],
        )

    def _resolve_conda_r_package(self, conda_bin: Path, package: str) -> str:
        candidates = self._r_conda_candidates(package)
        fallback_available: list[str] = ["cran", "bioconductor"]
        return self._resolve_conda_package_candidate(
            conda_bin,
            requested_package=package,
            candidates=candidates,
            fallback_available=fallback_available,
        )

    def _resolve_conda_package_candidate(
        self,
        conda_bin: Path,
        *,
        requested_package: str,
        candidates: list[str],
        fallback_available: list[str],
    ) -> str:
        attempted: list[str] = []
        for candidate in candidates:
            if candidate in attempted:
                continue
            attempted.append(candidate)
            if self._conda_package_exists(conda_bin, candidate):
                return candidate
        raise DependencyResolutionError(
            {
                "error_code": "package_not_found_in_conda_channels",
                "requested_package": requested_package,
                "attempted_candidates": attempted,
                "fallback_available": fallback_available,
                "message": (
                    f"Package {requested_package} was not found in conda channels. "
                    f"Attempted: {', '.join(attempted)}."
                ),
            }
        )

    @staticmethod
    def _python_conda_candidates(package: str) -> list[str]:
        normalized = str(package or "").strip()
        lowered = normalized.lower()
        swapped = lowered.replace("_", "-")
        underscored = lowered.replace("-", "_")
        return [normalized, lowered, swapped, underscored]

    @staticmethod
    def _r_conda_candidates(package: str) -> list[str]:
        normalized = str(package or "").strip()
        lowered = normalized.lower()
        return [
            f"r-{lowered}",
            f"bioconductor-{lowered}",
        ]

    @staticmethod
    def _conda_package_exists(conda_bin: Path, package_name: str) -> bool:
        solver_name = conda_bin.name.lower()
        if solver_name in {"mamba", "micromamba"}:
            repoquery_match = ManagerBlueprintTools._repoquery_package_exists(conda_bin, package_name)
            if repoquery_match is not None:
                return repoquery_match
        return ManagerBlueprintTools._json_search_package_exists(conda_bin, package_name)

    @staticmethod
    def _repoquery_package_exists(conda_bin: Path, package_name: str) -> bool | None:
        result = subprocess.run(
            [str(conda_bin), "repoquery", "search", package_name],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            return None
        stdout = str(result.stdout or "")
        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            matches = payload.get(package_name)
            if isinstance(matches, list) and len(matches) > 0:
                return True
        pattern = re.compile(rf"(?m)^\s*{re.escape(package_name)}(?:\s|$)")
        return bool(pattern.search(stdout))

    @staticmethod
    def _json_search_package_exists(conda_bin: Path, package_name: str) -> bool:
        result = subprocess.run(
            [str(conda_bin), "search", "--json", package_name],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            return False
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return False
        matches = payload.get(package_name)
        return isinstance(matches, list) and len(matches) > 0

    @staticmethod
    def _is_r_compilation_failure(stderr: object) -> bool:
        stderr_lower = str(stderr or "").lower()
        failure_terms = (
            "compilation failed",
            "installation of package",
            "had non-zero exit status",
            "cannot install",
            "error in",
        )
        return any(term in stderr_lower for term in failure_terms)

    @staticmethod
    def _tail_text(value: object, limit: int = 6000) -> str:
        if value is None:
            return ""
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        return text[-limit:]

    @staticmethod
    def _is_conda_noop(stdout: str) -> bool:
        """Detect conda-family no-op transaction from stdout.

        Heuristic: return True when stdout contains known no-op signatures.
        This is intentionally not tied to one exact English sentence so that
        solver-version-equivalent text can be added when observed.
        """
        if not stdout:
            return False
        lowered = stdout.lower()
        return any(sig in lowered for sig in (
            "all requested packages already installed",
            "nothing to do",
            "no actions to perform",
        ))

    def _active_run_ids_for_card(self, project_id: str, card_id: str | None) -> list[str]:
        if not card_id:
            return []
        graph = self.project_service.graph_store(project_id).load_graph()
        return [
            run.run_id
            for run in graph.runs
            if run.card_id == card_id and run.status in WorkerService._active_run_statuses()
        ]

    def _latest_run_id_for_card(self, project_id: str, card_id: str | None) -> str | None:
        if not card_id:
            return None
        snapshot = self.project_service.get_project_snapshot(project_id)
        card = next((item for item in snapshot["cards"] if item.card_id == card_id), None)
        if card and card.linked_runs:
            return card.linked_runs[-1]
        run_ids = [run.run_id for run in snapshot["graph"].runs if run.card_id == card_id]
        return run_ids[-1] if run_ids else None

    def _validate_run_card_selector(self, project_id: str, *, run_id: str | None, card_id: str | None, action: str) -> None:
        if not run_id or not card_id:
            return
        graph = self.project_service.graph_store(project_id).load_graph()
        run = next((item for item in graph.runs if item.run_id == run_id), None)
        if run is None:
            raise ManagerPlanningError(f"{action} run not found: {run_id}")
        if run.card_id != card_id:
            raise ManagerPlanningError(f"{action} selector mismatch: run {run_id} belongs to card {run.card_id}, not {card_id}.")

    def _assert_rerun_inputs_retryable(self, project_id: str, card_id: str) -> None:
        snapshot = self.project_service.get_project_snapshot(project_id)
        issues = self.dependency_attention_service.issues_for_card(snapshot, card_id)
        blocking_kinds = {
            "input_asset_missing",
            "input_asset_not_valid",
            "input_asset_outdated",
            "input_producer_card_inactive",
            "input_producer_output_removed",
            "asset_lineage_invalid",
        }
        blocking = [issue for issue in issues if issue.get("kind") in blocking_kinds]
        if not blocking:
            return
        first = blocking[0]
        current_asset = first.get("current_asset_id")
        guidance = ""
        if current_asset:
            guidance = f" Repair inputs first by selecting current asset {current_asset}."
        raise ManagerPlanningError(
            "rerun_card requires retryable current inputs. "
            + str(first.get("message") or "Dependency attention blocks rerun.")
            + guidance
        )

    def _run_reviewer_passed(self, project_id: str, run_id: str | None) -> bool:
        if not run_id:
            return False
        path = self.project_service.project_path(project_id) / "runs" / run_id / "executor_validation.json"
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        reviewer = payload.get("reviewer") if isinstance(payload, dict) else {}
        if not isinstance(reviewer, dict):
            return False
        return str(reviewer.get("verdict") or "") == "pass"

    def _load_manifest_if_exists(self, project_id: str, run_id: str | None) -> Manifest | None:
        if not run_id:
            return None
        path = self.project_service.project_path(project_id) / "runs" / run_id / "manifest.json"
        if not path.exists():
            return None
        return Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def _template_store_root(self) -> Path:
        root = self.project_service.settings.data_root / "_card_templates"
        (root / "bundles").mkdir(parents=True, exist_ok=True)
        return root

    def _template_index_path(self) -> Path:
        return self._template_store_root() / "templates.json"

    def _template_bundle_dir(self, template_id: str) -> Path:
        return self._template_store_root() / "bundles" / template_id

    def _load_card_templates(self) -> list[CardTemplate]:
        path = self._template_index_path()
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [CardTemplate.model_validate(item) for item in payload]

    def _save_card_templates(self, templates: list[CardTemplate]) -> None:
        atomic_write_json(self._template_index_path(), [item.model_dump() for item in templates])

    def _load_card_template(self, template_id: str) -> CardTemplate:
        template = next((item for item in self._load_card_templates() if item.template_id == template_id), None)
        if template is None:
            raise ManagerPlanningError(f"Card template not found: {template_id}")
        spec_path = self._template_bundle_dir(template_id) / "spec.json"
        if spec_path.exists():
            try:
                payload = json.loads(spec_path.read_text(encoding="utf-8"))
                template = CardTemplate.model_validate(payload)
            except json.JSONDecodeError as exc:
                raise ManagerPlanningError(f"Card template spec is invalid JSON: {template_id}") from exc
        return template

    def _save_template_bundle(self, template: CardTemplate) -> None:
        bundle_dir = self._template_bundle_dir(template.template_id)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(bundle_dir / "spec.json", template.model_dump())

    def _match_templates(
        self,
        templates: list[CardTemplate],
        query: str,
        tags: list[str],
        card_type: str | None,
    ) -> list[dict[str, Any]]:
        compact_query = query.strip().lower()
        tag_set = {item.strip().lower() for item in tags if item.strip()}
        matches: list[dict[str, Any]] = []
        for template in templates:
            if card_type and template.card_type != card_type:
                continue
            if tag_set and not tag_set.intersection({tag.lower() for tag in template.tags}):
                continue
            haystack = " ".join([template.title, template.summary, " ".join(template.tags), template.spec.summary_template]).lower()
            score = 0.2 + template.confidence_score
            if compact_query:
                for token in compact_query.split():
                    if token in haystack:
                        score += 0.4
            if compact_query and score <= 0.2 + template.confidence_score:
                continue
            matches.append(
                {
                    "template_id": template.template_id,
                    "title": template.title,
                    "summary": template.summary,
                    "tags": template.tags,
                    "card_type": template.card_type,
                    "status": template.status,
                    "confidence_score": round(min(score, 5.0), 3),
                    "reuse_count": template.reuse_count,
                    "script_asset_requirements": [item.model_dump() for item in template.spec.bundle.script_asset_requirements],
                }
            )
        matches.sort(key=lambda item: (item["confidence_score"], item["reuse_count"]), reverse=True)
        return matches

    def _build_card_template(
        self,
        project_id: str,
        card: Card,
        manifest: Manifest | None,
        request: SaveCardTemplatePayload,
    ) -> CardTemplate:
        now = utc_now()
        template_title = (request.title or card.title).strip()
        template_summary = (request.summary or card.summary).strip()
        template_id = self._slugged_template_id(template_title)
        context = card.executor_context.model_copy(deep=True) if card.executor_context else ExecutorContext()
        context.script_asset_bindings = []
        context.template_metadata = {}
        bundle = TemplateBundle(
            script_asset_requirements=[item.model_copy(deep=True) for item in context.script_asset_requirements],
            script_asset_bindings=[],
        )
        template = CardTemplate(
            template_id=template_id,
            title=template_title,
            summary=template_summary,
            tags=[item for item in request.tags if item.strip()],
            card_type=card.card_type,
            source_card_type=card.card_type,
            created_at=now,
            updated_at=now,
            last_verified_at=now if manifest is not None else None,
            confidence_score=0.8 if card.status == "accepted" else 0.7,
            status="active",
            source_card_id=card.card_id,
            source_project_id=project_id,
            spec=TemplateSpec(
                card_title_pattern=card.title,
                summary_template=card.summary,
                why_template=card.why,
                inputs_schema=[
                    TemplateIoBinding(label=item.label, status=item.status, required=True)
                    for item in card.inputs
                ],
                outputs_schema=[
                    TemplateIoBinding(
                        label=item.label,
                        role=item.role,
                        artifact_class=item.artifact_class,
                        accepted_formats=list(item.accepted_formats),
                        preferred_format=item.preferred_format,
                        asset_id=item.asset_id,
                        status=item.status,
                        required=item.required,
                        description=item.description,
                    )
                    for item in card.outputs
                ],
                executor_context=context,
                tool_policy=context.tool_policy.model_dump(),
                runtime_bindings=context.runtime_bindings.model_dump(),
                instruction_blocks=list(context.instruction_blocks),
                prompt_blocks=list(context.instruction_blocks),
                expected_artifacts=[item.role for item in manifest.created_assets] if manifest else [item.role for item in card.outputs],
                success_signals=list(card.key_findings or (manifest.key_findings if manifest else [])),
                failure_signals=list(manifest.warnings if manifest else []),
                bundle=bundle,
            ),
        )
        if manifest is not None:
            self._copy_template_code_artifacts(project_id, template, manifest)
        return template

    def _copy_template_code_artifacts(self, project_id: str, template: CardTemplate, manifest: Manifest) -> None:
        project_root = self.project_service.project_path(project_id)
        bundle_dir = self._template_bundle_dir(template.template_id)
        files_dir = bundle_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        for artifact in manifest.code_artifacts:
            try:
                source = resolve_within(project_root, artifact.path)
            except ValueError:
                continue
            if not source.is_file():
                continue
            stored_path = Path("files") / artifact.path
            target = bundle_dir / stored_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            template.spec.bundle.files.append(
                TemplateBundleFile(
                    original_path=artifact.path,
                    stored_path=stored_path.as_posix(),
                    description=getattr(artifact, "description", None),
                )
            )
            template.spec.bundle.path_rewrites[artifact.path] = stored_path.as_posix()

    def _instantiate_card_payload(
        self,
        project_id: str,
        snapshot: dict[str, Any],
        template: CardTemplate,
        request: InstantiateCardTemplatePayload,
    ) -> dict[str, Any]:
        card_title = (request.title or template.spec.card_title_pattern).strip()
        return {
            "title": card_title,
            "step": request.step,
            "summary": template.spec.summary_template,
            "inputs": [{"asset_id": item.asset_id} for item in request.input_bindings],
            "outputs": [
                {
                    "role": item.role,
                    "artifact_class": item.artifact_class,
                    "description": item.description,
                }
                for item in template.spec.outputs_schema
            ],
        }

    def _copy_template_bundle_into_project(self, project_id: str, card_id: str, template: CardTemplate) -> list[ExecutorReference]:
        bundle_dir = self._template_bundle_dir(template.template_id)
        project_root = self.project_service.project_path(project_id)
        target_root = project_root / "scripts" / "curated" / "templates" / card_id
        references: list[ExecutorReference] = []
        for file_entry in template.spec.bundle.files:
            source = bundle_dir / file_entry.stored_path
            if not source.is_file():
                continue
            target = target_root / Path(file_entry.original_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            relative_target = target.relative_to(project_root).as_posix()
            references.append(
                ExecutorReference(
                    type="file",
                    path=relative_target,
                    description=file_entry.description or f"Template file from {template.template_id}",
                )
            )
        return references

    @staticmethod
    def _runs_by_card(runs) -> dict[str, list[Any]]:
        grouped: dict[str, list[Any]] = {}
        for run in runs:
            grouped.setdefault(run.card_id, []).append(run)
        return grouped

    @staticmethod
    def _compact_run(run) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "card_id": run.card_id,
            "status": run.status,
            "title": run.title,
            "summary": run.summary,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "needs_manager_attention": run.needs_manager_attention,
        }

    @classmethod
    def _compact_card(cls, card: Card, timeline_card: dict[str, Any] | None, runs: list[Any]) -> dict[str, Any]:
        active_run = next((run for run in reversed(runs) if run.status in {"queued", "running", "reviewing", "needs_approval"}), None)
        latest_run = runs[-1] if runs else None
        required_asset_ids = timeline_card.get("required_asset_ids", []) if timeline_card else [item.asset_id for item in card.inputs if item.asset_id]
        produced_asset_ids = (
            timeline_card.get("produced_asset_ids", [])
            if timeline_card
            else [item.asset_id for item in card.outputs if item.asset_id]
        )
        return {
            "card_id": card.card_id,
            "title": card.title,
            "status": card.status,
            "card_type": card.card_type,
            "step": card.step,
            "summary": card.summary,
            "progress_note": card.progress_note,
            "required_asset_ids": required_asset_ids,
            "produced_asset_ids": produced_asset_ids,
            "depends_on_card_ids": timeline_card.get("depends_on_card_ids", []) if timeline_card else [],
            "active_run": cls._compact_run(active_run) if active_run else None,
            "latest_run": cls._compact_run(latest_run) if latest_run else None,
            "blockers": cls._card_blockers(card, required_asset_ids),
        }

    @staticmethod
    def _card_blockers(card: Card, required_asset_ids: list[str]) -> list[str]:
        blockers: list[str] = []
        if card.status in {"failed", "rejected"}:
            blockers.append(f"card_status:{card.status}")
        if card.progress_note and re.search(r"\b(missing|failed|blocked|缺少|失败|阻塞)\b", card.progress_note, re.I):
            blockers.append(card.progress_note)
        if any(item.asset_id is None for item in card.inputs):
            blockers.append("input_without_asset_id")
        if not required_asset_ids and card.inputs:
            blockers.append("input_asset_not_resolved")
        return blockers

    @classmethod
    def _project_blockers(cls, snapshot: dict[str, Any], timeline: dict[str, Any]) -> list[dict[str, Any]]:
        asset_ids = {str(asset.get("asset_id")) for asset in timeline["assets"] if asset.get("asset_id")}
        blockers: list[dict[str, Any]] = []
        for card in snapshot["cards"]:
            missing_inputs = [item.asset_id for item in card.inputs if item.asset_id and item.asset_id not in asset_ids]
            card_blockers = cls._card_blockers(card, [item.asset_id for item in card.inputs if item.asset_id])
            if missing_inputs:
                card_blockers.append(f"missing_inputs:{', '.join(missing_inputs)}")
            if card.card_id in timeline["cycle_card_ids"]:
                card_blockers.append("dependency_cycle")
            if card_blockers:
                blockers.append({"card_id": card.card_id, "title": card.title, "blockers": card_blockers})
        for asset_id in timeline["duplicate_output_assets"]:
            blockers.append({"asset_id": asset_id, "blockers": ["duplicate_output_asset_id"]})
        return blockers

    @staticmethod
    def _normalize_query(value: str | None) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    @classmethod
    def _card_search_text(cls, card: Card) -> str:
        values = [
            card.card_id,
            card.title,
            card.status,
            card.summary,
            card.why,
            card.progress_note or "",
            " ".join(card.key_findings),
            " ".join(card.next_actions),
            " ".join(item.label for item in card.inputs),
            " ".join(item.asset_id or "" for item in card.inputs),
            " ".join(item.label for item in card.outputs),
            " ".join(item.role for item in card.outputs),
            " ".join(item.asset_id or "" for item in card.outputs),
        ]
        return cls._normalize_query(" ".join(values))

    @staticmethod
    def _card_asset_ids(card: Card) -> set[str]:
        return {
            item
            for item in [*[input_ref.asset_id for input_ref in card.inputs], *[output.asset_id for output in card.outputs]]
            if item
        }

    @staticmethod
    def _output_contracts_by_asset(cards: list[Card]) -> dict[str, CardOutputSpec]:
        contracts: dict[str, CardOutputSpec] = {}
        for card in cards:
            for output in card.outputs:
                if output.asset_id:
                    contracts[output.asset_id] = output
        return contracts

    @staticmethod
    def _asset_format(path: str | None) -> str | None:
        if not path:
            return None
        lowered = path.lower()
        for suffix in (".tar.gz", ".tsv.gz", ".csv.gz"):
            if lowered.endswith(suffix):
                return suffix[1:]
        suffix = Path(path).suffix.lower().lstrip(".")
        return suffix or None

    @classmethod
    def _compact_asset_record(
        cls,
        record: dict[str, Any],
        materialized_asset,
        contract: CardOutputSpec | None,
    ) -> dict[str, Any]:
        path = materialized_asset.path if materialized_asset else record.get("path")
        asset_format = cls._asset_format(path)
        return {
            "asset_id": record.get("asset_id"),
            "title": materialized_asset.title if materialized_asset else record.get("title"),
            "status": materialized_asset.status if materialized_asset else record.get("status"),
            "asset_type": materialized_asset.asset_type if materialized_asset else record.get("asset_type"),
            "artifact_class": contract.artifact_class if contract else None,
            "role": contract.role if contract else None,
            "accepted_formats": list(contract.accepted_formats) if contract else [],
            "preferred_format": contract.preferred_format if contract else None,
            "format": asset_format,
            "path": path,
            "summary": materialized_asset.summary if materialized_asset else record.get("summary"),
            "producer_card_id": record.get("producer_card_id"),
            "producer_run_id": record.get("producer_run_id"),
            "consumer_card_ids": record.get("consumer_card_ids") or [],
            "materialized": bool(record.get("materialized")),
            "planned": bool(record.get("planned")),
            "step": record.get("step"),
        }

    @classmethod
    def _asset_matches(cls, asset: dict[str, Any], *, query: str, request: FindAssetsPayload) -> bool:
        if request.role and asset.get("role") != request.role:
            return False
        if request.artifact_class and asset.get("artifact_class") != request.artifact_class:
            return False
        if request.format:
            expected = str(request.format).strip().lower().lstrip(".")
            formats = {str(asset.get("format") or "").lower(), *[str(item).lower() for item in asset.get("accepted_formats") or []]}
            if expected not in formats:
                return False
        if request.producer_card_id and asset.get("producer_card_id") != request.producer_card_id:
            return False
        if request.status and asset.get("status") != request.status:
            return False
        if query:
            haystack = cls._normalize_query(
                " ".join(
                    str(asset.get(key) or "")
                    for key in ["asset_id", "title", "status", "asset_type", "artifact_class", "role", "format", "summary", "producer_card_id"]
                )
            )
            if query not in haystack:
                return False
        return True

    @staticmethod
    def _recommended_step(snapshot: dict[str, Any], card: Card) -> int:
        timeline = AssetTimelineService().build(snapshot["project"].project_id, snapshot)
        asset_by_id = {item["asset_id"]: item for item in timeline["assets"]}
        min_step = 1
        for input_ref in card.inputs:
            if not input_ref.asset_id:
                continue
            asset = asset_by_id.get(input_ref.asset_id)
            if asset:
                min_step = max(min_step, int(asset.get("step") or 0) + 1)
        return min_step

    @staticmethod
    def _retry_hint_for_validation_error(message: str) -> str:
        lowered = message.lower()
        if "step too early" in lowered:
            return "Increase card.step to at least the required downstream step."
        if "missing" in lowered and "asset" in lowered:
            return "Use find_assets to choose an existing asset_id, or create an upstream card output first."
        if "duplicate" in lowered:
            return "Choose a unique card_id or output asset_id."
        if "required" in lowered:
            return "Fill the required card fields before calling create_card or update_card."
        return "Inspect the referenced card or asset, then retry with corrected arguments."

    @staticmethod
    def _slugged_template_id(title: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or "card_template"
        return f"tpl_{slug}_{sha256(f'{title}:{utc_now()}'.encode('utf-8')).hexdigest()[:8]}"

    @staticmethod
    def _sync_module_links(graph, card: Card, previous_card: Card | None) -> None:
        previous_modules = set(previous_card.linked_modules if previous_card else [])
        current_modules = set(card.linked_modules)
        if not previous_modules and not current_modules:
            return
        for module in graph.modules:
            if module.module_id in current_modules and card.card_id not in module.linked_cards:
                module.linked_cards.append(card.card_id)
            if module.module_id in previous_modules and module.module_id not in current_modules:
                module.linked_cards = [item for item in module.linked_cards if item != card.card_id]
        graph.metadata["linked_cards_last_updated"] = card.card_id
        # Callers persist the graph after this helper returns.

    @staticmethod
    def _is_session_upload(asset) -> bool:
        source = str(asset.metadata.get("source") or "")
        if source:
            return source == "manager_chat_upload"
        return asset.path.startswith("data/uploads/")
