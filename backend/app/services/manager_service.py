from __future__ import annotations

import re
import json
import logging
import socket
from collections.abc import Iterator
from collections import deque
from urllib import error, request as url_request

from app.models.chat import ChatAction, ChatRequest, ChatResponse
from app.models.patches import GraphPatch, Proposal
from app.core.config import get_settings
from app.services.app_config_service import AppConfigService
from app.services.background_workboard_service import BackgroundWorkboardService
from app.services.chat_stream_events import iter_sse_payloads
from app.services.manager_blueprint_tools import ManagerBlueprintTools
from app.services.manager_intent import ManagerIntentRouter
from app.services.manager_patch_compiler import ManagerPatchCompiler
from app.services.manager_planner import DeepSeekManagerPlanner, ManagerPlanDraft
from app.services.manager_planner import ManagerPlanningError
from app.services.manager_tools import ManagerToolLayer
from app.services.library_registry_service import LibraryRegistryService
from app.services.manager_auto_service import ManagerAutoService
from app.services.patch_validator import PatchValidator
from app.services.project_service import ProjectService
from app.services.runtime_dependency_job_service import RuntimeDependencyJobService
from app.services.worker_service import WorkerService
from app.services.utils import utc_now


logger = logging.getLogger(__name__)


class ManagerService:
    def __init__(
        self,
        project_service: ProjectService,
        planner: DeepSeekManagerPlanner | None = None,
        tool_layer: ManagerToolLayer | None = None,
        worker_service: WorkerService | None = None,
        runtime_dependency_job_service: RuntimeDependencyJobService | None = None,
        runtime_dependency_resolver_service: "RuntimeDependencyResolverService | None" = None,
        library_registry_service: LibraryRegistryService | None = None,
        manager_auto_service: ManagerAutoService | None = None,
        background_workboard_service: BackgroundWorkboardService | None = None,
        package_service: Any | None = None,
    ) -> None:
        self.project_service = project_service
        self._uses_default_planner = planner is None
        self.planner = planner or DeepSeekManagerPlanner()
        self.tool_layer = tool_layer or ManagerToolLayer()
        self.intent_router = ManagerIntentRouter()
        self.settings = get_settings()
        self.app_config_service = AppConfigService(self.settings)
        self.manager_auto_service = manager_auto_service
        self.blueprint_tools = ManagerBlueprintTools(
            project_service=self.project_service,
            worker_service=worker_service,
            runtime_dependency_job_service=runtime_dependency_job_service,
            runtime_dependency_resolver_service=runtime_dependency_resolver_service,
            library_registry_service=library_registry_service,
            background_workboard_service=background_workboard_service,
            package_service=package_service,
        )

    def chat(self, project_id: str, request: ChatRequest) -> ChatResponse:
        # Compatibility wrapper for legacy synchronous callers such as /chat and chat-jobs.
        # Production Manager execution must go through stream_chat(); this wrapper only
        # aggregates the final response from that stream. The local path below is kept
        # for tests and explicitly injected planner stubs, not for new runtime flows.
        if self._uses_default_planner:
            return self._chat_from_stream(project_id, request)
        return self._chat_via_local(project_id, request)

    def _chat_from_stream(self, project_id: str, chat_request: ChatRequest) -> ChatResponse:
        response_payload: dict | None = None
        try:
            payloads = iter_sse_payloads(
                self.stream_chat(project_id, chat_request),
                invalid_json_message="Pi manager stream returned invalid JSON",
            )
            for payload in payloads:
                if payload.get("type") == "error":
                    raise ManagerPlanningError(str(payload.get("detail") or "Pi manager stream failed."))
                if payload.get("type") == "response" and isinstance(payload.get("response"), dict):
                    response_payload = payload["response"]
        except RuntimeError as exc:
            raise ManagerPlanningError(str(exc)) from exc
        if response_payload is None:
            raise ManagerPlanningError("Pi manager stream ended without a final response.")
        return ChatResponse.model_validate(response_payload)

    def _sanitize_chat_request_messages(self, chat_request: ChatRequest) -> None:
        # Collect exact contents of all command and wake-notice messages to filter simplified history,
        # then filter out command/wake-notice messages from session_messages.
        command_contents = set()
        filtered_session_messages = []
        for msg in chat_request.session_messages:
            is_cmd = msg.id.startswith("cmd_")
            is_wake_notice = msg.id.startswith("wake_notice_")
            if not is_cmd and msg.timeline:
                for item in msg.timeline:
                    if getattr(item, "kind", "") == "command":
                        is_cmd = True
                        break
            if is_cmd or is_wake_notice:
                if msg.content:
                    command_contents.add(msg.content.strip())
            else:
                filtered_session_messages.append(msg)
        chat_request.session_messages = filtered_session_messages

        # Filter simplified history messages by matching exact content of command/wake-notice messages.
        # Fall back to using the authoritative parse_slash_command parser for user commands.
        from app.services.utils import parse_slash_command
        filtered_messages = []
        for item in chat_request.messages:
            stripped = item.content.strip()
            if stripped in command_contents:
                continue
            is_cmd, _, _ = parse_slash_command(stripped)
            if is_cmd:
                continue
            filtered_messages.append(item)
        chat_request.messages = filtered_messages

    def stream_chat(self, project_id: str, chat_request: ChatRequest) -> Iterator[bytes]:
        self._sanitize_chat_request_messages(chat_request)
        if self.settings.manager_backend != "pi":
            raise ManagerPlanningError("Only BLUEPRINT_MANAGER_BACKEND=pi is supported.")
        token = self.settings.internal_tool_token.get_secret_value() if self.settings.internal_tool_token else ""
        if not token:
            raise ManagerPlanningError("BLUEPRINT_INTERNAL_TOOL_TOKEN is not configured.")
        payload = {
            "project_id": project_id,
            "message": chat_request.message,
            "session_id": chat_request.session_id,
            "context": chat_request.context.model_dump(),
            "thinking_effort": chat_request.thinking_effort,
            "messages": [item.model_dump() for item in chat_request.messages],
            "auto_mode": self._auto_payload(project_id, chat_request.session_id),
            "backend_api_base_url": self.settings.backend_api_base_url.rstrip("/"),
            "internal_tool_token": token,
            "manager_config": self.app_config_service.manager_agent_config(include_secrets=True),
        }
        endpoint = f"{self.settings.pi_manager_url.rstrip('/')}/chat-stream"
        http_request = url_request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            upstream = url_request.urlopen(http_request, timeout=self.settings.manager_timeout_seconds)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ManagerPlanningError(f"Pi manager failed with HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise ManagerPlanningError(f"Pi manager request failed: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise ManagerPlanningError(f"Pi manager request failed: {exc}") from exc
        self._set_upstream_read_timeout(upstream, self.settings.manager_timeout_seconds)

        def iterator() -> Iterator[bytes]:
            try:
                while True:
                    try:
                        line = upstream.readline()
                    except (TimeoutError, socket.timeout, OSError) as exc:
                        payload = {"type": "error", "detail": f"Pi manager stream read timed out or failed: {exc}"}
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
                        break
                    if not line:
                        break
                    yield line
            finally:
                upstream.close()

        return iterator()

    def compact_chat_session(self, project_id: str, chat_request: ChatRequest) -> dict:
        self._sanitize_chat_request_messages(chat_request)
        if self.settings.manager_backend != "pi":
            raise ManagerPlanningError("Only BLUEPRINT_MANAGER_BACKEND=pi is supported.")
        token = self.settings.internal_tool_token.get_secret_value() if self.settings.internal_tool_token else ""
        if not token:
            raise ManagerPlanningError("BLUEPRINT_INTERNAL_TOOL_TOKEN is not configured.")
        payload = {
            "project_id": project_id,
            "message": chat_request.message,
            "session_id": chat_request.session_id,
            "context": chat_request.context.model_dump(),
            "thinking_effort": chat_request.thinking_effort,
            "messages": [item.model_dump() for item in chat_request.messages],
            "session_messages": [item.model_dump() for item in chat_request.session_messages],
            "auto_mode": self._auto_payload(project_id, chat_request.session_id),
            "backend_api_base_url": self.settings.backend_api_base_url.rstrip("/"),
            "internal_tool_token": token,
            "manager_config": self.app_config_service.manager_agent_config(include_secrets=True),
        }
        endpoint = f"{self.settings.pi_manager_url.rstrip('/')}/compact"
        http_request = url_request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            with url_request.urlopen(http_request, timeout=self.settings.manager_timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ManagerPlanningError(f"Pi manager failed with HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise ManagerPlanningError(f"Pi manager request failed: {exc.reason}") from exc
        except (TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ManagerPlanningError(f"Pi manager request failed: {exc}") from exc

    def _chat_via_local(self, project_id: str, request: ChatRequest) -> ChatResponse:
        snapshot = self.project_service.get_project_snapshot(project_id)
        intent = self.intent_router.classify(request.message)

        tool_draft = self.tool_layer.try_build_plan(snapshot, request, intent)
        if tool_draft:
            return self._save_or_answer(project_id, snapshot, tool_draft)

        if intent.kind == "mutation" and intent.action == "update_existing" and hasattr(self.planner, "agent_turn"):
            payload = self.planner.agent_turn([], [])
            message = self._extract_message_text(payload)
            if message:
                return ChatResponse(message=message)

        if intent.kind == "mutation" and hasattr(self.planner, "plan"):
            draft = self.planner.plan(snapshot, request)
            return self._save_or_answer(project_id, snapshot, draft)

        if hasattr(self.planner, "answer"):
            message = self.planner.answer(snapshot, request)
            return ChatResponse(message=message)

        if hasattr(self.planner, "agent_turn"):
            payload = self.planner.agent_turn([], [])
            message = self._extract_message_text(payload)
            if message:
                return ChatResponse(message=message)
            raise ManagerPlanningError("Local manager stub did not return a text response.")

        return ChatResponse(message=self.tool_layer.answer(snapshot, request))

    def _auto_payload(self, project_id: str, session_id: str | None) -> dict:
        if self.manager_auto_service is None:
            return {
                "enabled": False,
                "owner_session_id": None,
                "btw_mode": False,
                "state": "idle",
                "scope_objective": None,
                "fuel_revision": 0,
                "last_notified_revision": 0,
                "wake_in_flight": False,
                "completion_notified": False,
                "chain_count": 0,
                "stop_reason": None,
                "finished_at": None,
            }
        view = self.manager_auto_service.get_view(project_id, session_id)
        return {
            "enabled": view.state.enabled,
            "owner_session_id": view.state.owner_session_id,
            "btw_mode": view.btw_mode,
            "state": view.state.state,
            "pending_directives": [item.model_dump() for item in view.state.pending_directives if item.status == "pending"],
            "scope_objective": view.state.scope_objective,
            # Doc 42 latch fields
            "fuel_revision": view.state.fuel_revision,
            "last_notified_revision": view.state.last_notified_revision,
            "wake_in_flight": view.state.wake_in_flight,
            "completion_notified": view.state.completion_notified,
            "chain_count": view.state.chain_count,
            "stop_reason": view.state.stop_reason,
            "finished_at": view.state.finished_at,
        }

    def _save_or_answer(self, project_id: str, snapshot: dict, draft: ManagerPlanDraft) -> ChatResponse:
        if draft.response_type == "proposal":
            draft.ensure_valid()
            return self._save_proposal_draft(project_id, snapshot, draft)
        return ChatResponse(message=draft.message, thinking=None)

    @staticmethod
    def _extract_message_text(payload: dict) -> str:
        content = payload.get("content", []) if isinstance(payload, dict) else []
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts).strip()

    def _save_proposal_draft(self, project_id: str, snapshot: dict, draft: ManagerPlanDraft) -> ChatResponse:
        proposal, patch = self._materialize_proposal(draft)
        patch, warnings, diagnostics = self._normalize_and_validate_patch(project_id, snapshot, patch)
        store = self.project_service.graph_store(project_id)
        proposals = store.load_proposals()
        proposals = [item for item in proposals if item.proposal_id != proposal.proposal_id]
        proposal.consistency_warnings = warnings
        proposals.append(proposal)
        store.save_proposals(proposals)
        store.save_patch(patch.patch_id, patch.model_dump())
        return ChatResponse(
            message=proposal.summary,
            proposal=proposal,
            actions=[
                ChatAction(label="接受提案", action="accept_proposal"),
                ChatAction(label="修改提案", action="modify_proposal"),
                ChatAction(label="查看影响", action="view_impact"),
            ],
            warnings=warnings,
            metadata=diagnostics,
        )

    def get_proposal(self, project_id: str, proposal_id: str) -> Proposal:
        store = self.project_service.graph_store(project_id)
        proposals = store.load_proposals()
        proposal = next((item for item in proposals if item.proposal_id == proposal_id), None)
        if not proposal:
            raise ManagerPlanningError(f"Proposal not found: {proposal_id}")
        return proposal

    def mark_proposal_status(self, project_id: str, proposal_id: str, status: str) -> Proposal:
        store = self.project_service.graph_store(project_id)
        proposals = store.load_proposals()
        proposal = next((item for item in proposals if item.proposal_id == proposal_id), None)
        if not proposal:
            raise ManagerPlanningError(f"Proposal not found: {proposal_id}")
        proposal.status = status
        proposal.updated_at = utc_now()
        store.save_proposals(proposals)
        return proposal

    def accept_proposal(self, project_id: str, proposal_id: str) -> Proposal:
        return self.mark_proposal_status(project_id, proposal_id, "accepted")

    def reject_proposal(self, project_id: str, proposal_id: str) -> Proposal:
        return self.mark_proposal_status(project_id, proposal_id, "rejected")

    def modify_proposal(self, project_id: str, proposal_id: str, request: ChatRequest) -> Proposal:
        store = self.project_service.graph_store(project_id)
        proposals = store.load_proposals()
        proposal = next((item for item in proposals if item.proposal_id == proposal_id), None)
        if not proposal:
            raise ManagerPlanningError(f"Proposal not found: {proposal_id}")
        existing_patch = store.load_patch(proposal.patch_id)
        if not existing_patch:
            raise ManagerPlanningError(f"Patch not found for proposal {proposal_id}")
        snapshot = self.project_service.get_project_snapshot(project_id)
        draft = self.planner.plan(
            snapshot,
            request,
            extra_context={
                "mode": "modify_proposal",
                "existing_proposal": proposal.model_dump(),
                "existing_patch": existing_patch,
            },
        )
        if draft.response_type != "proposal":
            raise ValueError("Manager modify_proposal must return a proposal response.")
        updated_proposal, patch = self._materialize_proposal(draft, proposal_id=proposal_id)
        patch, warnings, _diagnostics = self._normalize_and_validate_patch(project_id, snapshot, patch)
        updated_proposal.created_at = proposal.created_at
        updated_proposal.consistency_warnings = self._proposal_consistency_warnings(updated_proposal, patch, proposal, existing_patch)
        updated_proposal.consistency_warnings.extend(warnings)
        proposals = [item for item in proposals if item.proposal_id != proposal_id]
        proposals.append(updated_proposal)
        store.save_proposals(proposals)
        store.save_patch(patch.patch_id, patch.model_dump())
        return updated_proposal

    def replace_proposal_with_draft(self, project_id: str, proposal_id: str, draft: ManagerPlanDraft) -> Proposal:
        store = self.project_service.graph_store(project_id)
        proposals = store.load_proposals()
        proposal = next((item for item in proposals if item.proposal_id == proposal_id), None)
        if not proposal:
            raise ManagerPlanningError(f"Proposal not found: {proposal_id}")
        existing_patch = store.load_patch(proposal.patch_id)
        if not existing_patch:
            raise ManagerPlanningError(f"Patch not found for proposal {proposal_id}")
        if draft.response_type != "proposal":
            raise ValueError("Structured proposal replacement must be a proposal response.")
        snapshot = self.project_service.get_project_snapshot(project_id)
        updated_proposal, patch = self._materialize_proposal(draft, proposal_id=proposal_id)
        patch, warnings, _diagnostics = self._normalize_and_validate_patch(project_id, snapshot, patch)
        updated_proposal.created_at = proposal.created_at
        updated_proposal.consistency_warnings = self._proposal_consistency_warnings(updated_proposal, patch, proposal, existing_patch)
        updated_proposal.consistency_warnings.extend(warnings)
        proposals = [item for item in proposals if item.proposal_id != proposal_id]
        proposals.append(updated_proposal)
        store.save_proposals(proposals)
        store.save_patch(patch.patch_id, patch.model_dump())
        return updated_proposal

    @staticmethod
    def _set_upstream_read_timeout(upstream, timeout_seconds: int) -> None:
        try:
            upstream.fp.raw._sock.settimeout(timeout_seconds)
        except AttributeError:
            try:
                upstream.fp.raw._fp.fp.raw._sock.settimeout(timeout_seconds)
            except AttributeError:
                logger.warning("Unable to set upstream read timeout on manager streaming response.")

    @staticmethod
    def _materialize_proposal(draft: ManagerPlanDraft, proposal_id: str | None = None) -> tuple[Proposal, GraphPatch]:
        now = utc_now()
        timestamp = re.sub(r"[^0-9]", "", now)
        slug_source = draft.title or draft.patch_type or "manager_plan"
        slug = re.sub(r"[^a-z0-9]+", "_", slug_source.lower()).strip("_") or "manager_plan"
        is_modification = proposal_id is not None
        proposal_id = proposal_id or f"proposal_{slug}_{timestamp}"
        patch_id = proposal_id.replace("proposal_", "patch_", 1)
        if is_modification:
            patch_id = f"{patch_id}_{timestamp}"
        proposal = Proposal(
            proposal_id=proposal_id,
            patch_id=patch_id,
            title=draft.title or "Manager Proposal",
            summary=draft.summary or draft.message,
            impact_summary=draft.impact_summary or draft.message,
            status="proposed",
            created_at=now,
            updated_at=now,
        )
        patch = GraphPatch(
            patch_id=patch_id,
            patch_type=draft.patch_type,
            source="manager_ai",
            reason=draft.reason or draft.message,
            ops=draft.ops,
        )
        return proposal, patch

    @staticmethod
    def _proposal_consistency_warnings(new_proposal: Proposal, new_patch: GraphPatch, previous_proposal: Proposal, previous_patch: dict) -> list[str]:
        warnings: list[str] = []
        previous_patch_type = previous_patch.get("patch_type")
        if previous_patch_type and previous_patch_type != new_patch.patch_type:
            warnings.append(f"Patch type changed from {previous_patch_type} to {new_patch.patch_type}.")
        if previous_proposal.title != new_proposal.title:
            warnings.append(f"Proposal title changed from “{previous_proposal.title}” to “{new_proposal.title}”.")
        if not new_patch.ops:
            warnings.append("Modified proposal does not contain executable patch ops.")
        return warnings

    def _normalize_and_validate_patch(self, project_id: str, snapshot: dict, patch: GraphPatch) -> tuple[GraphPatch, list[str], dict]:
        compiler = ManagerPatchCompiler.from_snapshot(snapshot)
        normalized_patch = compiler.normalize_patch(patch)
        validator = PatchValidator(self.project_service)
        validation = validator.validate_patch(project_id, normalized_patch)
        if not validation.valid:
            raise ManagerPlanningError("Manager proposal validation failed: " + "; ".join(validation.errors))
        diagnostics = self._proposal_asset_diagnostics(snapshot, normalized_patch)
        sufficiency = diagnostics["proposal_asset_sufficiency"]
        if sufficiency["missing_asset_cards"]:
            raise ManagerPlanningError(
                "Manager proposal requires unavailable assets for: "
                + ", ".join(sufficiency["missing_asset_cards"])
                + ". Add a producing card output for each planned asset, or use an existing materialized asset."
            )
        if sufficiency["duplicate_output_assets"]:
            raise ManagerPlanningError(
                "Manager proposal has duplicate planned output assets: "
                + ", ".join(sufficiency["duplicate_output_assets"])
            )
        if sufficiency["cycle_card_ids"]:
            raise ManagerPlanningError(
                "Dependency cycle detected among proposal cards: "
                + ", ".join(sufficiency["cycle_card_ids"])
            )
        return normalized_patch, validation.warnings, diagnostics

    @staticmethod
    def _proposal_asset_diagnostics(snapshot: dict, patch: GraphPatch) -> dict:
        existing_assets = {asset.asset_id: asset for asset in snapshot["graph"].assets}
        existing_cards = {card.card_id: card for card in snapshot["cards"]}
        patch_cards: dict[str, dict] = {}
        producer_by_asset: dict[str, str] = {}
        producer_sources: dict[str, str] = {}
        duplicate_producers: dict[str, list[str]] = {}
        dependency_map: dict[str, set[str]] = {}
        card_reports: list[dict] = []

        for card in existing_cards.values():
            for output in card.outputs:
                if not output.asset_id:
                    continue
                if output.asset_id in producer_by_asset and producer_by_asset[output.asset_id] != card.card_id:
                    duplicate_producers.setdefault(output.asset_id, [producer_by_asset[output.asset_id]]).append(card.card_id)
                    continue
                producer_by_asset[output.asset_id] = card.card_id
                producer_sources[output.asset_id] = "existing_card_output"

        for op in patch.ops:
            if op.op not in {"create_card", "update_card"}:
                continue
            payload = dict(op.payload)
            card_id = payload.get("card_id")
            if not card_id:
                continue
            patch_cards[card_id] = payload
            for output in payload.get("outputs") or []:
                asset_id = output.get("asset_id")
                if not asset_id:
                    continue
                if asset_id in producer_by_asset and producer_by_asset[asset_id] != card_id:
                    duplicate_producers.setdefault(asset_id, [producer_by_asset[asset_id]]).append(card_id)
                    continue
                producer_by_asset[asset_id] = card_id
                producer_sources[asset_id] = "proposal_card_output"

        missing_asset_cards: list[str] = []
        duplicate_output_assets = sorted(duplicate_producers)

        for card_id, payload in patch_cards.items():
            dependencies: set[str] = set()
            assets_report: list[dict] = []
            unresolved = False
            for item in payload.get("inputs") or []:
                asset_id = item.get("asset_id")
                label = item.get("label", "")
                if not asset_id:
                    unresolved = True
                    assets_report.append({"label": label, "asset_id": None, "state": "unresolved"})
                    continue
                if asset_id in producer_by_asset and producer_by_asset[asset_id] != card_id:
                    upstream_card_id = producer_by_asset[asset_id]
                    dependencies.add(upstream_card_id)
                    state = "planned_from_same_proposal" if upstream_card_id in patch_cards else "planned_from_existing_card"
                    assets_report.append(
                        {
                            "label": label,
                            "asset_id": asset_id,
                            "state": state,
                            "producer_card_id": upstream_card_id,
                            "producer_source": producer_sources.get(asset_id),
                        }
                    )
                elif asset_id in existing_assets and existing_assets[asset_id].status in {"valid", "candidate"}:
                    assets_report.append(
                        {
                            "label": label,
                            "asset_id": asset_id,
                            "state": "available_now" if existing_assets[asset_id].status == "valid" else "candidate_input_available",
                        }
                    )
                elif asset_id in existing_assets:
                    unresolved = True
                    assets_report.append(
                        {
                            "label": label,
                            "asset_id": asset_id,
                            "state": f"existing_but_{existing_assets[asset_id].status}",
                        }
                    )
                else:
                    unresolved = True
                    assets_report.append({"label": label, "asset_id": asset_id, "state": "missing"})
            dependency_map[card_id] = dependencies
            if unresolved:
                missing_asset_cards.append(card_id)
            card_reports.append(
                {
                    "card_id": card_id,
                    "title": payload.get("title", card_id),
                    "required_assets": assets_report,
                }
            )

        layer_index = ManagerService._layer_indexes(dependency_map)
        for report in card_reports:
            report["layer_index"] = layer_index.get(report["card_id"], 0)
            report["ready_now"] = not dependency_map.get(report["card_id"]) and report["card_id"] not in missing_asset_cards

        return {
            "proposal_asset_sufficiency": {
                "cards": card_reports,
                "missing_asset_cards": missing_asset_cards,
                "downstream_layer_cards": [],
                "duplicate_output_assets": duplicate_output_assets,
                "cycle_card_ids": [],
                "max_layer_index": max(layer_index.values(), default=0),
            }
        }

    @staticmethod
    def _layer_indexes(dependency_map: dict[str, set[str]]) -> dict[str, int]:
        remaining = {card_id: set(deps) for card_id, deps in dependency_map.items()}
        dependents: dict[str, set[str]] = {card_id: set() for card_id in dependency_map}
        for card_id, deps in dependency_map.items():
            for dep_id in deps:
                dependents.setdefault(dep_id, set()).add(card_id)

        ready = deque(sorted(card_id for card_id, deps in remaining.items() if not deps))
        layer_index = {card_id: 0 for card_id in ready}

        while ready:
            card_id = ready.popleft()
            current_layer = layer_index.get(card_id, 0)
            for dependent_id in sorted(dependents.get(card_id, set())):
                deps = remaining.setdefault(dependent_id, set())
                deps.discard(card_id)
                layer_index[dependent_id] = max(layer_index.get(dependent_id, 0), current_layer + 1)
                if not deps:
                    ready.append(dependent_id)
        unresolved = {card_id: deps for card_id, deps in remaining.items() if deps}
        if unresolved:
            cycle_cards = ", ".join(sorted(unresolved))
            raise ManagerPlanningError(f"Dependency cycle detected among proposal cards: {cycle_cards}")
        return layer_index
