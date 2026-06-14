from __future__ import annotations

from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import shutil
import signal
from threading import Lock, Semaphore, Thread
from typing import Any, Callable
from uuid import uuid4
import re
import subprocess
import traceback

from fastapi import HTTPException

from app.models.cards import Card, CardAssetRef
from app.models.executor import (
    ExecutorContext,
    ExecutorReference,
    ExecutorScriptAssetBinding,
    ExecutorScriptAssetRequirement,
    ExecutorStructuredEvent,
    ExecutorToolPolicy,
    ManagerReportingContract,
    RuntimeBindings,
)
from app.models.graph import Asset, Claim, GraphState, Module, ReportItem, RunRecord
from app.models.output_contracts import CardOutputSpec
from app.models.runs import (
    ExecutorCompletionReport,
    ExecutorFailureReport,
    FailureReasonCode,
    ExpectedOutput,
    Manifest,
    RunContext,
    RunEvent,
    TaskPacket,
    TaskPacketAsset,
    TaskPacketCardInput,
    TaskPacketCardOutput,
    TerminalReport,
)
from app.services.app_config_service import AppConfigService
from app.services.artifact_format_service import default_format_for_artifact_class
from app.services.background_task_service import BackgroundTaskService
from app.services.asset_materialization_service import AssetMaterializationService
from app.services.flow_service import FlowService
from app.services.executor_validation_service import ExecutorValidationService
from app.services.input_resolution_service import InputResolutionService, VALID_LAUNCHABLE_INPUT_STATUSES
from app.services.library_registry_service import LibraryRegistryService
from app.services.manifest_service import ManifestService
from app.services.module_group_state_service import ModuleGroupStateService
from app.services.project_event_service import ProjectEventService
from app.services.project_service import ProjectService
from app.services.runtime_approval_service import RuntimeApprovalService
from app.services.runtime_dependency_state_service import ACTIVE_RUNTIME_DEPENDENCY_JOB_STATUSES
from app.services.utils import atomic_write_json, utc_now
from app.workers import build_worker_registry


logger = logging.getLogger(__name__)

SYSTEM_OUTPUT_ROLES = {"run_summary", "run_preview"}


_SENSITIVE_COMMAND_KEYS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_EVENT_FLUSH_BATCH_SIZE = 25
_PROCESS_TERMINATE_GRACE_SECONDS = 5


def _is_sensitive_command_key(value: str) -> bool:
    upper = value.upper()
    return any(marker in upper for marker in _SENSITIVE_COMMAND_KEYS)


def _redact_command_for_log(command: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next_setenv_value = False
    redact_next_option_value = False
    previous_token = ""
    for token in command:
        if redact_next_setenv_value:
            redacted.append("[REDACTED]")
            redact_next_setenv_value = False
            previous_token = token
            continue
        if redact_next_option_value:
            redacted.append("[REDACTED]")
            redact_next_option_value = False
            previous_token = token
            continue

        if previous_token == "--setenv":
            redacted.append(token)
            redact_next_setenv_value = _is_sensitive_command_key(token)
            previous_token = token
            continue

        if token in {"--api-key", "--token", "--password"}:
            redacted.append(token)
            redact_next_option_value = True
            previous_token = token
            continue

        if token.startswith("--api-key=") or token.startswith("--token=") or token.startswith("--password="):
            option, _value = token.split("=", 1)
            redacted.append(f"{option}=[REDACTED]")
            previous_token = token
            continue

        if "=" in token:
            key, _value = token.split("=", 1)
            if _is_sensitive_command_key(key):
                redacted.append(f"{key}=[REDACTED]")
                previous_token = token
                continue

        redacted.append(token)
        previous_token = token
    return redacted


class _CompositeExecutionGuard:
    def __init__(self, card_lock: Lock, semaphore: Semaphore) -> None:
        self.card_lock = card_lock
        self.semaphore = semaphore

    def release(self) -> None:
        self.semaphore.release()
        self.card_lock.release()


class WorkerService:
    def __init__(
        self,
        project_service: ProjectService,
        manifest_service: ManifestService,
        runtime_approval_service: RuntimeApprovalService,
        library_registry_service: LibraryRegistryService | None = None,
        project_event_service: ProjectEventService | None = None,
        background_task_service: BackgroundTaskService | None = None,
        background_terminal_callback: Callable[[str, str | None, str | None], None] | None = None,
    ) -> None:
        self.project_service = project_service
        self.manifest_service = manifest_service
        self.runtime_approval_service = runtime_approval_service
        self.project_event_service = project_event_service
        self.background_task_service = background_task_service or BackgroundTaskService(project_service)
        self.background_terminal_callback = background_terminal_callback
        self.library_registry_service = library_registry_service or LibraryRegistryService(
            project_service,
            AppConfigService(project_service.settings),
            project_service.settings,
        )
        self.flow_service = FlowService(project_service)
        self.input_resolution_service = InputResolutionService()
        self.executor_validation_service = ExecutorValidationService(project_service)
        self.registry = build_worker_registry()
        self._threads: dict[str, Thread] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._execution_locks: dict[str, Lock] = {}
        self._execution_semaphores: dict[str, Semaphore] = {}
        self._card_locks: dict[tuple[str, str], Lock] = {}
        self._event_buffers: dict[tuple[str, str], list[RunEvent]] = {}
        self._event_sequences: dict[tuple[str, str], int] = {}
        self._execution_locks_guard = Lock()
        self._reconciled_run_ids: list[tuple[str, str]] = []
        self._reconcile_active_runs()

    def _terminate_process_group(self, run_id: str, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=_PROCESS_TERMINATE_GRACE_SECONDS)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            logger.exception("Failed to terminate process group for run=%s; falling back to process.kill()", run_id)

        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=_PROCESS_TERMINATE_GRACE_SECONDS)
            return
        except ProcessLookupError:
            return
        except Exception:
            logger.exception("Failed to kill process group for run=%s; falling back to process.kill()", run_id)

        try:
            process.kill()
            process.wait(timeout=_PROCESS_TERMINATE_GRACE_SECONDS)
        except Exception:
            logger.exception("Failed to kill process for run=%s", run_id)

    def start_run(
        self,
        project_id: str,
        card_id: str,
        worker_type: str | None = None,
        profile_id: str | None = None,
        python_runtime: str | None = None,
        r_runtime: str | None = None,
    ) -> dict:
        python_runtime = self._normalize_python_runtime(python_runtime)
        r_runtime = self._normalize_r_runtime(r_runtime)
        config_service = AppConfigService(self.project_service.settings)
        resolved_worker_type = worker_type
        resolved_profile_id = profile_id
        if resolved_worker_type is None and resolved_profile_id:
            resolved_profile = self._resolve_profile_across_workers(config_service, resolved_profile_id)
            if resolved_profile is None:
                raise HTTPException(status_code=409, detail=f"Executor profile {resolved_profile_id} is not configured.")
            resolved_worker_type = str(resolved_profile.get("worker_type") or "")
            resolved_profile_id = str(resolved_profile.get("profile_id") or resolved_profile_id)
        resolved_worker_type = self._resolve_worker_type(resolved_worker_type)
        if resolved_profile_id:
            resolved_profile = config_service.resolve_executor_profile(resolved_worker_type, profile_id=resolved_profile_id)
            if resolved_profile is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"Executor profile {resolved_profile_id} is not configured for worker_type={resolved_worker_type}.",
                )
            resolved_profile_id = str(resolved_profile.get("profile_id") or resolved_profile_id)
        adapter = self.registry.get(resolved_worker_type)
        if adapter is None:
            raise HTTPException(status_code=400, detail=f"Unknown worker_type: {resolved_worker_type}")

        runtime_prefs = self.project_service.get_project_runtime_preferences(project_id)
        execution_guard, guard_kind = self._acquire_execution_guard(
            project_id, card_id, sandboxed=adapter.uses_sandbox(self.project_service.settings), execution_mode=runtime_prefs.execution_mode
        )
        if execution_guard is None:
            if guard_kind == "workspace_write_project_lock":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Project workspace_write mode allows only one run at a time because work/ is shared across runs.",
                        "error_code": "workspace_write_lock_busy",
                    },
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Project already has an executor run in progress. Start another run after the current run finishes review.",
                    "error_code": "executor_capacity_full",
                },
            )
        try:
            return self._start_run_with_execution_guard(
                project_id,
                card_id,
                resolved_worker_type,
                adapter,
                execution_guard,
                guard_kind,
                profile_id=resolved_profile_id,
                python_runtime=python_runtime,
                r_runtime=r_runtime,
            )
        except Exception:
            self._release_execution_guard(execution_guard, guard_kind)
            raise

    def _start_run_with_execution_guard(
        self,
        project_id: str,
        card_id: str,
        resolved_worker_type: str,
        adapter: Any,
        execution_guard: Lock | Semaphore,
        guard_kind: str,
        profile_id: str | None = None,
        python_runtime: str | None = None,
        r_runtime: str | None = None,
    ) -> dict:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            graph = store.load_graph()
            card = next((item for item in cards if item.card_id == card_id), None)
            if card is None:
                raise HTTPException(status_code=404, detail=f"Card not found: {card_id}")
            work_item = self._get_work_item(project_id, card_id)
            runtime_dependency_blocker = work_item.get("runtime_dependency_blocker")
            if (
                isinstance(runtime_dependency_blocker, dict)
                and str(runtime_dependency_blocker.get("status") or "") in ACTIVE_RUNTIME_DEPENDENCY_JOB_STATUSES
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "ok": False,
                        "message": f"Card {card_id} is waiting for runtime dependency repair to finish.",
                        "error_code": "runtime_dependency_repair_in_progress",
                        "card_id": card_id,
                        "job_id": runtime_dependency_blocker.get("job_id"),
                        "retry_after_signal": "runtime_dependency_install_terminal",
                        "block_details": work_item,
                    },
                )
            if not work_item["can_start"]:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": f"Card {card_id} cannot start.",
                        "block_details": work_item,
                    },
                )

            run_id = self._new_run_id(graph.runs)
            task = self.background_task_service.create_task(
                project_id,
                task_type="card_run",
                affected={"card_ids": [card_id], "run_ids": [run_id]},
                adapter={"kind": "worker_service"},
            )
            run_dir = self.project_service.project_path(project_id) / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            packet = self._task_packet(
                project_id,
                run_id,
                card,
                graph.assets,
                resolved_worker_type,
                profile_id=profile_id,
                python_runtime=python_runtime,
                r_runtime=r_runtime,
                cards=cards,
                runs=graph.runs,
            )
            atomic_write_json(run_dir / "task_packet.json", packet.model_dump())
            try:
                launch_spec = adapter.build_launch_spec(
                    packet=packet,
                    packet_path=run_dir / "task_packet.json",
                    run_dir=run_dir,
                    project_root=self.project_service.project_path(project_id),
                    settings=self.project_service.settings,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            approvals = [
                self.runtime_approval_service.review_permission_request(
                    project_id,
                    run_id,
                    {
                        "request_id": request.request_id,
                        "target": request.target,
                        "action": request.action,
                    },
                    readonly_paths=packet.readonly_paths,
                )
                for request in launch_spec.permission_requests
            ]
            unresolved = [item for item in approvals if item["decision"] == "needs_user_confirmation"]
            rejected = [item for item in approvals if item["decision"] in {"rejected", "user_rejected"}]
            (run_dir / "transcript.md").write_text(f"# {run_id}\n\nRun created.\n", encoding="utf-8")
            redacted_command = _redact_command_for_log(launch_spec.command)
            (run_dir / "commands.log").write_text(" ".join(redacted_command) + "\n", encoding="utf-8")

            initial_status = "needs_approval" if unresolved else "cancelled" if rejected else "queued"
            summary = "等待用户确认运行期权限请求。" if unresolved else "启动前权限校验失败。" if rejected else "等待执行器启动。"
            graph.runs.append(
                RunRecord(
                    run_id=run_id,
                    task_id=task.task_id,
                    card_id=card_id,
                    module_id=card.linked_modules[0] if card.linked_modules else None,
                    status=initial_status,
                    title=f"{card.title} 执行",
                    summary=summary,
                    started_at=utc_now(),
                    finished_at=utc_now() if rejected else None,
                    worker_type=resolved_worker_type,
                )
            )
            self.background_task_service.update_task(
                project_id,
                task.task_id,
                status="cancelled" if rejected else "waiting" if unresolved else "queued",
                started_at=utc_now(),
                finished_at=utc_now() if rejected else None,
                error="Launch approval rejected before execution." if rejected else None,
                result={"message": summary},
            )
            card.status = "planned" if rejected else "running"
            card.progress_note = "执行器已创建，等待运行。" if not rejected else "启动前权限校验失败。"
            if rejected:
                card.manager_review = "运行期权限请求被拒绝，未启动执行器。"
            if run_id not in card.linked_runs:
                card.linked_runs.append(run_id)
            ModuleGroupStateService.sync_linked_module_status_from_card(card, graph.modules)
            ModuleGroupStateService.sync_group_hierarchy(cards, graph.modules)
            store.save_graph(graph)
            store.save_cards(cards)
            latest_event = RunEvent(
                event_id=f"evt_{run_id}_001",
                run_id=run_id,
                card_id=card_id,
                source="manager",
                event_type="run_created",
                visibility="bubble",
                preview_id=f"bubble_{card_id}",
                utterance_id=f"utt_{run_id}_001",
                stream_state="complete",
                message=f"已创建 run {run_id}，worker={resolved_worker_type}。",
                created_at=utc_now(),
            )
            store.save_run_events(run_id, [latest_event])
            for index, decision in enumerate(approvals, start=2):
                self._append_event(
                    project_id,
                    run_id,
                    card_id,
                    event_type="permission_decision",
                    message=f"[{decision['risk_level']}] {decision['target']} -> {decision['decision']}: {decision['reason']}",
                    sequence_hint=index,
                )

        response = {
            "task_id": task.task_id,
            "run_id": run_id,
            "card_id": card_id,
            "worker_type": resolved_worker_type,
            "status": initial_status,
            "latest_event": latest_event.model_dump(),
        }
        self._emit_project_event(
            project_id,
            reason="run_created",
            card_id=card_id,
            run_id=run_id,
            task_id=task.task_id,
            status=initial_status,
            payload={"card_status": "planned" if rejected else "running", "task_id": task.task_id},
        )
        if unresolved:
            response["pending_approvals"] = unresolved
            self._release_execution_guard(execution_guard, guard_kind)
            return response
        if rejected:
            response["rejected_approvals"] = rejected
            self._release_execution_guard(execution_guard, guard_kind)
            return response

        thread = Thread(
            target=self._execute_run_guarded,
            kwargs={
                "project_id": project_id,
                "run_id": run_id,
                "card_id": card_id,
                "worker_type": resolved_worker_type,
                "command": launch_spec.command,
                "cwd": launch_spec.cwd,
                "environment": launch_spec.environment,
                "execution_guard": execution_guard,
                "guard_kind": guard_kind,
                "sandboxed": launch_spec.sandboxed,
            },
            daemon=True,
        )
        self._threads[run_id] = thread
        thread.start()
        return response

    def continue_run_after_approval(self, project_id: str, run_id: str) -> dict:
        unresolved = self.runtime_approval_service.unresolved_user_requests(project_id, run_id)
        if unresolved:
            raise ValueError("Run still has unresolved approval requests.")
        decisions = self.runtime_approval_service.load_decisions(project_id, run_id)
        rejected = [item for item in decisions if item["decision"] in {"rejected", "user_rejected"}]
        store = self.project_service.graph_store(project_id)
        graph = store.load_graph()
        run = next(item for item in graph.runs if item.run_id == run_id)
        if rejected:
            self._append_event(
                project_id,
                run_id,
                run.card_id,
                event_type="run_cancelled",
                message="运行期权限请求被拒绝，run 已取消。",
            )
            self._set_run_status(
                project_id,
                run_id,
                run.card_id,
                status="cancelled",
                summary="运行期权限请求被拒绝。",
                progress_note=None,
                card_status="planned",
            )
            self._commit_run_stage(project_id, run_id, "cancelled")
            return {"run_id": run_id, "status": "cancelled", "rejected_approvals": rejected}
        if run.status != "needs_approval":
            return {"run_id": run_id, "status": run.status}

        adapter = self.registry.get(run.worker_type)
        if adapter is None:
            raise HTTPException(status_code=400, detail=f"Unknown worker_type: {run.worker_type}")
        packet = self.manifest_service.load_task_packet(project_id, run_id)
        execution_guard, guard_kind = self._acquire_execution_guard(
            project_id, run.card_id, sandboxed=adapter.uses_sandbox(self.project_service.settings), execution_mode=packet.execution_policy.mode
        )
        if execution_guard is None:
            if guard_kind == "workspace_write_project_lock":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Project workspace_write mode allows only one run at a time because work/ is shared across runs.",
                        "error_code": "workspace_write_lock_busy",
                    },
                )
            raise HTTPException(
                status_code=409,
                detail="Project already has an executor run in progress. Continue this run after the current run finishes review.",
            )
        lock_transferred_to_thread = False
        try:
            run_dir = self.project_service.project_path(project_id) / "runs" / run_id
            try:
                launch_spec = adapter.build_launch_spec(
                    packet=packet,
                    packet_path=run_dir / "task_packet.json",
                    run_dir=run_dir,
                    project_root=self.project_service.project_path(project_id),
                    settings=self.project_service.settings,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            thread = Thread(
                target=self._execute_run_guarded,
                kwargs={
                    "project_id": project_id,
                    "run_id": run_id,
                    "card_id": run.card_id,
                    "worker_type": run.worker_type,
                    "command": launch_spec.command,
                    "cwd": launch_spec.cwd,
                    "environment": launch_spec.environment,
                    "execution_guard": execution_guard,
                    "guard_kind": guard_kind,
                    "sandboxed": launch_spec.sandboxed,
                },
                daemon=True,
            )
            self._threads[run_id] = thread
            thread.start()
            lock_transferred_to_thread = True
            return {"run_id": run_id, "status": "queued"}
        finally:
            if not lock_transferred_to_thread:
                self._release_execution_guard(execution_guard, guard_kind)

    def cancel_run(self, project_id: str, run_id: str, reason: str | None = None) -> dict:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            graph = store.load_graph()
            run = next((item for item in graph.runs if item.run_id == run_id), None)
            if run is None:
                raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
            card = next(item for item in cards if item.card_id == run.card_id)
            if run.status == "cancelled":
                return {"run_id": run_id, "status": run.status, "summary": run.summary}
            if run.status not in {"queued", "launching", "needs_approval", "running", "reviewing"}:
                raise HTTPException(status_code=409, detail=f"Run {run_id} cannot be cancelled from status {run.status}.")
            message = (reason or "Run cancelled by operator.").strip()
            run.status = "cancelled"
            run.summary = message
            run.cancel_reason = message
            run.finished_at = utc_now()
            card.progress_note = None
            if not self._has_other_active_runs(graph.runs, card.card_id, exclude_run_id=run_id):
                card.status = "planned"
            card.manager_review = message
            ModuleGroupStateService.sync_linked_module_status_from_card(card, graph.modules)
            ModuleGroupStateService.sync_group_hierarchy(cards, graph.modules)
            store.save_graph(graph)
            store.save_cards(cards)

        process = self._processes.get(run_id)
        if process is not None and process.poll() is None:
            self._terminate_process_group(run_id, process)
        # Do NOT pop _threads here — the _execute_run finally block does that
        # when the thread actually finishes. Popping early would let cleanup_run
        # delete run/results directories while the thread is still using them.
        self._append_event(project_id, run_id, run.card_id, event_type="run_cancelled", message=message)
        self._commit_run_stage(project_id, run_id, "cancelled")
        self._update_background_task_for_run(project_id, run_id, run.task_id, "cancelled", message)
        self._emit_project_event(project_id, reason="run_cancelled", card_id=run.card_id, run_id=run_id, task_id=run.task_id, status="cancelled", payload={"task_id": run.task_id})
        self._notify_background_terminal(project_id, run_id=run_id)
        return {"run_id": run_id, "status": "cancelled", "summary": message}

    def cleanup_run(self, project_id: str, run_id: str, reason: str | None = None) -> dict:
        project_root = self.project_service.project_path(project_id)
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            graph = store.load_graph()
            run = next((item for item in graph.runs if item.run_id == run_id), None)
            if run is None:
                raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
            if run.cleanup_status == "completed":
                return {"run_id": run_id, "cleanup_status": run.cleanup_status, "archived_at": run.archived_at}
            if run.status not in {"success", "failed", "cancelled", "reviewed"}:
                raise HTTPException(status_code=409, detail=f"Run {run_id} cannot be cleaned up from status {run.status}.")
            if self._threads.get(run_id) and self._threads[run_id].is_alive():
                raise HTTPException(status_code=409, detail=f"Run {run_id} still has a live executor thread.")

            valid_assets = [asset.asset_id for asset in graph.assets if asset.created_by_run == run_id and asset.status == "valid"]
            if valid_assets:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": f"Run {run_id} has materialized valid assets and cannot be cleaned up.",
                        "asset_ids": valid_assets,
                    },
                )

            removed_asset_ids = {asset.asset_id for asset in graph.assets if asset.created_by_run == run_id}
            removed_claim_ids = {claim.claim_id for claim in graph.claims if claim.created_by_run == run_id}
            graph.assets = [asset for asset in graph.assets if asset.created_by_run != run_id]
            graph.claims = [claim for claim in graph.claims if claim.created_by_run != run_id]
            graph.report_items = [
                item
                for item in graph.report_items
                if item.item_id != f"report_{run_id}"
                and not removed_asset_ids.intersection(item.linked_asset_ids)
                and not removed_claim_ids.intersection(item.linked_claim_ids)
            ]
            for card in cards:
                card.linked_assets = [asset_id for asset_id in card.linked_assets if asset_id not in removed_asset_ids]
                card.outputs = [item for item in card.outputs if item.asset_id not in removed_asset_ids]

            run.cleanup_status = "completed"
            run.archived_at = utc_now()
            suffix = f" Cleanup reason: {reason.strip()}" if reason and reason.strip() else ""
            run.summary = f"{run.summary} Artifacts cleaned up.{suffix}".strip()
            graph.metadata["last_cleanup"] = {
                "run_id": run_id,
                "card_id": run.card_id,
                "created_at": run.archived_at,
            }
            store.save_graph(graph)
            store.save_cards(cards)

        shutil.rmtree(project_root / "runs" / run_id, ignore_errors=True)
        shutil.rmtree(project_root / "results" / run.card_id / run_id, ignore_errors=True)
        self._threads.pop(run_id, None)
        self._processes.pop(run_id, None)
        self._commit_run_stage(project_id, run_id, "cleanup")
        self._emit_project_event(project_id, reason="run_cleanup", card_id=run.card_id, run_id=run_id, task_id=run.task_id, status=run.status, payload={"task_id": run.task_id})
        return {"run_id": run_id, "cleanup_status": "completed", "archived_at": run.archived_at}

    def reset_card_run_state(self, project_id: str, card_id: str) -> dict:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            graph = store.load_graph()
            card = next((item for item in cards if item.card_id == card_id), None)
            if card is None:
                raise HTTPException(status_code=404, detail=f"Card not found: {card_id}")
            if card.status not in {"failed", "reviewing", "needs_review", "rejected", "cancelled"}:
                raise HTTPException(status_code=409, detail=f"Card {card_id} cannot reset from status {card.status}.")
            if self._has_active_run(graph.runs, card_id):
                raise HTTPException(status_code=409, detail=f"Card {card_id} still has an active run.")
            card.status = "planned"
            card.progress_note = None
            card.manager_review = "Card run state reset to planned."
            ModuleGroupStateService.sync_linked_module_status_from_card(card, graph.modules)
            ModuleGroupStateService.sync_group_hierarchy(cards, graph.modules)
            store.save_graph(graph)
            store.save_cards(cards)
        self._emit_project_event(project_id, reason="card_run_state_reset", card_id=card_id, status="planned")
        return {"card_id": card_id, "status": "planned"}

    def rerun_card(
        self,
        project_id: str,
        card_id: str,
        worker_type: str | None = None,
        profile_id: str | None = None,
        python_runtime: str | None = None,
        r_runtime: str | None = None,
    ) -> dict:
        python_runtime = self._normalize_python_runtime(python_runtime)
        r_runtime = self._normalize_r_runtime(r_runtime)
        requested_profile_id = profile_id
        old_execution_run_ids: list[str] = []
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            graph = store.load_graph()
            cards = store.load_cards()
            card = next((item for item in cards if item.card_id == card_id), None)
            if card is None:
                raise HTTPException(status_code=404, detail=f"Card not found: {card_id}")
            if card.status in {"running", "reviewing", "proposed", "superseded", "stale"}:
                raise HTTPException(status_code=409, detail=f"Card {card_id} cannot rerun from status {card.status}.")
            if self._has_active_run(graph.runs, card_id):
                raise HTTPException(status_code=409, detail=f"Card {card_id} already has an active run.")
            old_execution_run_ids = [
                run.run_id
                for run in graph.runs
                if run.card_id == card_id
                and run.status in {"success", "failed", "cancelled", "reviewed"}
                and not (self._threads.get(run.run_id) and self._threads[run.run_id].is_alive())
            ]
            if requested_profile_id is None and card.executor_context is not None:
                requested_profile_id = card.executor_context.executor_profile_id
                if not requested_profile_id and card.executor_context.executor_profile:
                    legacy_profile = card.executor_context.executor_profile
                    if not legacy_profile.endswith("_worker"):
                        requested_profile_id = legacy_profile
            if card.status != "planned":
                previous_status = card.status
                card.status = "planned"
                card.progress_note = None
                card.manager_review = f"Preparing rerun from previous status {previous_status}."
                ModuleGroupStateService.sync_linked_module_status_from_card(card, graph.modules)
                ModuleGroupStateService.sync_group_hierarchy(cards, graph.modules)
                store.save_graph(graph)
                store.save_cards(cards)
        self._cleanup_execution_files_for_runs(project_id, old_execution_run_ids)
        return self.start_run(
            project_id,
            card_id,
            worker_type=worker_type,
            profile_id=requested_profile_id,
            python_runtime=python_runtime,
            r_runtime=r_runtime,
        )

    def review_run(self, project_id: str, run_id: str, accept: bool = True) -> dict:
        valid, errors = self.manifest_service.validate_manifest(project_id, run_id)
        if accept and not valid:
            raise ValueError("; ".join(errors))
        result = self._finalize_run_review(project_id, run_id, accept=accept, source="manager")
        final_accepted = result["accepted"]
        if final_accepted:
            message = "Manager 已接受运行结果。"
        elif result.get("failure_reason") == "mapping_ambiguous":
            message = "Manager  review 失败：输出资产映射存在歧义，需要手动确认。"
        elif result.get("failure_reason") == "consistency_failed":
            message = "Manager  review 失败：接受前图一致性检查未通过，需要手动确认。"
        else:
            message = "Manager 已拒绝运行结果，保留 candidate 产物。"
        self._append_event(
            project_id,
            run_id,
            result["card_id"],
            event_type="manager_review",
            message=message,
        )
        stage_label = "reviewed" if final_accepted or result.get("failure_reason") == "explicit_reject" else "needs_review"
        self._commit_run_stage(project_id, run_id, stage_label)
        self._notify_background_terminal(project_id, run_id=run_id)
        return {
            "run_id": run_id,
            "accepted": final_accepted,
            "failure_reason": result.get("failure_reason"),
            "failure_details": result.get("failure_details", []),
        }

    def _cleanup_execution_files_for_runs(self, project_id: str, run_ids: list[str]) -> None:
        if not run_ids:
            return
        project_root = self.project_service.project_path(project_id)
        for run_id in dict.fromkeys(run_ids):
            shutil.rmtree(project_root / "runs" / run_id, ignore_errors=True)
            shutil.rmtree(project_root / "scripts" / "generated" / run_id, ignore_errors=True)

    def _finalize_run_review(self, project_id: str, run_id: str, *, accept: bool, source: str) -> dict:
        review_context = self.manifest_service.manifest_to_review_context(project_id, run_id)
        task_packet = self.manifest_service.load_task_packet(project_id, run_id)
        manifest = self.manifest_service.load_manifest(project_id, run_id)
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            graph = store.load_graph()
            run = next(item for item in graph.runs if item.run_id == run_id)
            card = next(item for item in cards if item.card_id == run.card_id)
            previous_output_asset_ids = {
                asset.asset_id
                for asset in self._current_output_assets(card, graph.assets, current_run_id=run_id)
            }

            # 0a. Bootstrap legacy materializations so accept can see previous bindings.
            AssetMaterializationService.bootstrap_from_aliases(graph, cards)

            # 1. Materialize run assets: reuse existing valid assets without demotion.
            frozen_input_bindings = [
                {
                    "label": item.label,
                    "requested_asset_id": item.requested_asset_id,
                    "resolved_asset_id": item.resolved_asset_id,
                    "resolved_by": item.resolved_by,
                    "producer_card_id": item.producer_card_id,
                    "producer_role": item.producer_role,
                }
                for item in task_packet.card_inputs
            ]
            created_assets = self._materialize_run_assets(
                graph=graph,
                run_id=run_id,
                card=card,
                created_assets=review_context.created_assets,
                status="candidate",
                input_asset_ids=[item.asset_id for item in task_packet.input_assets],
                input_bindings=frozen_input_bindings,
            )

            # 2. Resolve output mappings deterministically.
            declared_created_assets = self._card_declared_created_assets(review_context.created_assets)
            declared_manifest_assets = self._card_declared_created_assets(manifest.created_assets)
            declared_expected_outputs = self._card_declared_expected_outputs(task_packet.expected_outputs)
            planned_bindings, unmapped_outputs = self._resolve_output_bindings(
                card,
                [asset for asset in created_assets if str(asset.metadata.get("role") or "") not in SYSTEM_OUTPUT_ROLES],
                manifest_created_assets=declared_manifest_assets,
                expected_outputs=declared_expected_outputs,
            )

            # 3. Determine final acceptance (mapping ambiguity blocks acceptance).
            final_accept = accept and not unmapped_outputs
            failure_reason: str | None = None
            failure_details: list[str] = []

            if final_accept:
                # Pre-flight: compute all accepted side effects before committing anything.
                planned_bindings_for_commit = list(planned_bindings)

                # Simulate side effects on copies for validation
                card_copy = card.model_copy(deep=True)
                graph_copy = graph.model_copy(deep=True)

                # Promote candidate assets to valid on the copy.
                created_asset_ids = {a.asset_id for a in created_assets}
                for asset in graph_copy.assets:
                    if asset.asset_id in created_asset_ids:
                        asset.status = "valid"

                # Supersede previous outputs on the copy FIRST so recovery can see superseded assets.
                for asset in graph_copy.assets:
                    if (
                        asset.asset_id in previous_output_asset_ids
                        and asset.created_by_run
                        and asset.created_by_run != run_id
                        and asset.status == "valid"
                    ):
                        asset.status = "superseded"
                stale_assets = {a.asset_id for a in graph_copy.assets if a.status == "superseded"}
                for claim in graph_copy.claims:
                    if stale_assets.intersection(claim.depends_on_assets) and claim.status == "valid":
                        claim.status = "superseded"

                # Recover logical ids for outputs pointing to superseded concrete assets (legacy compat).
                WorkerService._recover_logical_output_ids_from_superseded(card_copy, graph_copy.assets)

                # Bind outputs on the copy: preserve logical output ids, write materialization binding.
                asset_by_id_copy = {a.asset_id: a for a in graph_copy.assets}
                for output_index, real_asset in planned_bindings_for_commit:
                    out = card_copy.outputs[output_index]
                    promoted_asset = asset_by_id_copy.get(real_asset.asset_id)
                    if promoted_asset is not None:
                        self._backfill_planned_asset_alias(
                            promoted_asset,
                            out.asset_id,
                            asset_lookup=asset_by_id_copy,
                        )
                    # Preserve logical output id when one exists; otherwise bind to concrete id.
                    if out.asset_id is None:
                        out.asset_id = real_asset.asset_id
                    out.status = promoted_asset.status if promoted_asset else real_asset.status
                    if out.asset_id:
                        AssetMaterializationService.set_current(
                            graph_copy,
                            planned_asset_id=out.asset_id,
                            concrete_asset=promoted_asset or real_asset,
                            producer_card_id=card_copy.card_id,
                            producer_role=out.role,
                            producer_run_id=run_id,
                        )

                # Attach assets.
                for asset in created_assets:
                    if asset.asset_id not in card_copy.linked_assets:
                        card_copy.linked_assets.append(asset.asset_id)

                # Materialize claims on the copy.
                claim_asset_ids = [asset.asset_id for asset in created_assets]
                for index, finding in enumerate(review_context.key_findings or [], start=1):
                    claim_id = f"claim_{run_id}_{index:02d}"
                    graph_copy.claims.append(
                        Claim(
                            claim_id=claim_id,
                            text=finding,
                            status="valid",
                            depends_on_assets=claim_asset_ids,
                            created_by_run=run_id,
                            report_selected=True,
                        )
                    )

                # Report item on the copy.
                graph_copy.report_items = [item for item in graph_copy.report_items if item.item_id != f"report_{run_id}"]
                new_claim_ids = [f"claim_{run_id}_{index:02d}" for index in range(1, len(review_context.key_findings or []) + 1)]
                graph_copy.report_items.append(
                    ReportItem(
                        item_id=f"report_{run_id}",
                        section=card_copy.title,
                        title=f"{card_copy.title} 结果摘要",
                        summary=review_context.summary,
                        linked_asset_ids=[asset.asset_id for asset in created_assets if asset.report_selected],
                        linked_claim_ids=new_claim_ids,
                    )
                )

                card_copy.status = "accepted"
                card_copy.progress_note = None
                card_copy.manager_review = "Reviewer 已验收执行器代码、manifest 和输出资产，结果已自动接受。" if source == "reviewer" else "结果已通过 manifest 校验并被 Manager 接受。"
                card_copy.key_findings = review_context.key_findings or ["结果已生成并完成审核。"]

                # Pre-flight validation: if the simulated state is inconsistent, do NOT commit.
                preflight_errors = self._validate_acceptance_graph_consistent(card_copy, graph_copy, run_id)
                if preflight_errors:
                    final_accept = False
                    failure_reason = "consistency_failed"
                    failure_details = preflight_errors
                    card.status = "needs_review"
                    card.progress_note = None
                    card.manager_review = (
                        f"接受时图一致性检查失败：{preflight_errors[0]}。"
                        "产出已保留为 candidate，请手动确认或重新运行。"
                    )
                    run.status = "success"
                    run.finished_at = utc_now()
                    run.needs_manager_attention = True
                else:
                    # All checks passed: commit the accepted side effects atomically.
                    for asset in created_assets:
                        asset.status = "valid"
                    self._attach_assets_to_card(card, created_assets)
                    self._supersede_previous_outputs(
                        card,
                        graph.assets,
                        graph.claims,
                        run_id,
                        previous_asset_ids=previous_output_asset_ids,
                    )
                    # Recover logical ids for outputs pointing to superseded concrete assets (legacy compat).
                    WorkerService._recover_logical_output_ids_from_superseded(card, graph.assets)
                    # Write materialization bindings after recovery so bindings use correct logical ids.
                    asset_by_id = {asset.asset_id: asset for asset in graph.assets}
                    for output_index, real_asset in planned_bindings_for_commit:
                        out = card.outputs[output_index]
                        planned_asset_id = out.asset_id
                        self._backfill_planned_asset_alias(
                            real_asset,
                            planned_asset_id,
                            asset_lookup=asset_by_id,
                        )
                        # Preserve logical output id when one exists; otherwise bind to concrete id.
                        if planned_asset_id is None:
                            out.asset_id = real_asset.asset_id
                        out.status = real_asset.status
                        if out.asset_id:
                            AssetMaterializationService.set_current(
                                graph,
                                planned_asset_id=out.asset_id,
                                concrete_asset=real_asset,
                                producer_card_id=card.card_id,
                                producer_role=out.role,
                                producer_run_id=run_id,
                            )
                    new_claim_ids = self._materialize_claims(
                        graph, run_id, review_context.key_findings, [asset.asset_id for asset in created_assets]
                    )
                    card.status = "accepted"
                    card.progress_note = None
                    card.manager_review = "Reviewer 已验收执行器代码、manifest 和输出资产，结果已自动接受。" if source == "reviewer" else "结果已通过 manifest 校验并被 Manager 接受。"
                    card.key_findings = review_context.key_findings or ["结果已生成并完成审核。"]
                    graph.report_items = [item for item in graph.report_items if item.item_id != f"report_{run_id}"]
                    graph.report_items.append(
                        ReportItem(
                            item_id=f"report_{run_id}",
                            section=card.title,
                            title=f"{card.title} 结果摘要",
                            summary=review_context.summary,
                            linked_asset_ids=[asset.asset_id for asset in created_assets if asset.report_selected],
                            linked_claim_ids=new_claim_ids,
                        )
                    )
                    run.status = "reviewed"
                    run.finished_at = utc_now()
                    run.needs_manager_attention = False

            if not final_accept:
                if failure_reason == "consistency_failed":
                    # Already handled in the pre-flight block above.
                    pass
                elif accept and unmapped_outputs:
                    failure_reason = "mapping_ambiguous"
                    failure_details = unmapped_outputs
                    card.status = "needs_review"
                    card.progress_note = None
                    card.manager_review = (
                        f"输出资产映射存在歧义，无法将以下 planned output 绑定到真实产物：{', '.join(unmapped_outputs)}。"
                        "产出已保留为 candidate，请手动确认或重新运行。"
                    )
                    run.status = "success"
                    run.finished_at = utc_now()
                    run.needs_manager_attention = True
                else:
                    # Explicit reject: demote produced assets to candidate.
                    failure_reason = "explicit_reject"
                    for asset in created_assets:
                        asset.status = "candidate"
                    card.status = "rejected"
                    card.progress_note = None
                    card.manager_review = "Reviewer 拒绝了这次运行结果，产出已保留为 candidate。" if source == "reviewer" else "Manager 拒绝了这次运行结果，产出已保留为 candidate。"
                    run.status = "reviewed"
                    run.finished_at = utc_now()
                    run.needs_manager_attention = False

            ModuleGroupStateService.sync_linked_module_status_from_card(card, graph.modules)
            ModuleGroupStateService.sync_group_hierarchy(cards, graph.modules)
            manifest = self.manifest_service.load_manifest(project_id, run_id)
            run.summary = self.manifest_service.manager_summary(manifest) or review_context.summary
            store.save_graph(graph)
            store.save_cards(cards)
            self._update_background_task_for_run(project_id, run_id, run.task_id, run.status, run.summary)
            self._emit_project_event(
                project_id,
                reason="run_review_finalized",
                card_id=card.card_id,
                run_id=run_id,
                task_id=run.task_id,
                status=run.status,
                payload={"card_status": card.status, "accepted": final_accept, "failure_reason": failure_reason, "task_id": run.task_id},
            )
            return {
                "run_id": run_id,
                "card_id": card.card_id,
                "accepted": final_accept,
                "failure_reason": failure_reason,
                "failure_details": failure_details,
            }

    def _execute_run(
        self,
        *,
        project_id: str,
        run_id: str,
        card_id: str,
        worker_type: str,
        command: list[str],
        cwd: Path,
        environment: dict[str, str],
        sandboxed: bool = False,
    ) -> None:
        if self._run_status(project_id, run_id) == "cancelled":
            self._threads.pop(run_id, None)
            return
        self._set_run_status(project_id, run_id, card_id, status="launching", summary="执行器正在启动。", progress_note="正在启动执行器。")
        transcript_path = self.project_service.project_path(project_id) / "runs" / run_id / "transcript.md"

        try:
            cwd.mkdir(parents=True, exist_ok=True)
            before_snapshot = self.manifest_service.capture_filesystem_snapshot(project_id)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
            except FileNotFoundError:
                cwd.mkdir(parents=True, exist_ok=True)
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
            self._processes[run_id] = process
            if self._run_status(project_id, run_id) == "cancelled":
                self._terminate_process_group(run_id, process)
                self._processes.pop(run_id, None)
                return
            self._set_run_status(project_id, run_id, card_id, status="running", summary="执行器已启动。", progress_note="正在执行分析任务。")
            self._append_event(project_id, run_id, card_id, event_type="run_started", message=f"执行器 {worker_type} 已启动。")

            def pump_stdout() -> None:
                with transcript_path.open("a", encoding="utf-8") as transcript:
                    if process.stdout is None:
                        return
                    try:
                        for raw_line in process.stdout:
                            if self._run_status(project_id, run_id) == "cancelled":
                                break
                            line = raw_line.rstrip()
                            if not line:
                                continue
                            if self._run_status(project_id, run_id) == "cancelled":
                                break
                            transcript.write(f"- {line}\n")
                            transcript.flush()
                            if self._handle_structured_executor_event(project_id, run_id, card_id, line):
                                continue
                            self._append_event(project_id, run_id, card_id, event_type="executor_output", message=line)
                    finally:
                        self._flush_run_event_buffer(project_id, run_id)

            reader = Thread(target=pump_stdout, daemon=True)
            reader.start()
            timed_out = False
            worker_timeout = self.project_service.settings.worker_timeout_seconds
            adapter = self.registry.get(worker_type)
            if adapter is not None and getattr(adapter, "wrapper_module", None) == "app.workers.agent_cli_executor":
                from app.workers.agent_cli_executor import MAX_MANIFEST_REPAIR_ATTEMPTS
                repair_timeout = getattr(self.project_service.settings, "manifest_repair_timeout_seconds", 180)
                wrapper_wait_timeout = worker_timeout + MAX_MANIFEST_REPAIR_ATTEMPTS * repair_timeout + 60
            else:
                wrapper_wait_timeout = worker_timeout
            try:
                return_code = process.wait(timeout=wrapper_wait_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_process_group(run_id, process)
                return_code = process.wait()
            reader.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()

            if self._run_status(project_id, run_id) == "cancelled":
                return

            audit_ok, audit_errors, _changes = self.manifest_service.audit_run_filesystem(
                project_id,
                run_id,
                before_snapshot,
                sandboxed=sandboxed,
            )
            terminal_report = self._load_terminal_report(project_id, run_id)
            if terminal_report is not None and terminal_report.terminal_kind in {"report_fail", "synthetic_failure"}:
                failure_report = self._load_executor_failure_report(project_id, run_id)
                if failure_report is None:
                    failure_report = ExecutorFailureReport(
                        schema_version="executor_failure.v1",
                        reason_code=terminal_report.reason_code or "unknown",
                        summary=terminal_report.summary,
                        details={},
                    )
                self._persist_backend_manager_brief(project_id, run_id, failure=failure_report)
                self._apply_failure_report(project_id, run_id, card_id, failure_report)
                return
            has_terminal_complete = terminal_report is not None and terminal_report.terminal_kind == "report_complete"
            dependency_issue_message = None
            dependency_issue_payload: dict[str, Any] = {}
            if not has_terminal_complete:
                dependency_issue_message, dependency_issue_payload = self._blocking_dependency_issue(project_id, run_id)
            if dependency_issue_message:
                self._append_event(
                    project_id,
                    run_id,
                    card_id,
                    event_type="runtime_dependency_missing",
                    message=dependency_issue_message,
                    payload=dependency_issue_payload,
                )
                self._set_run_attention(project_id, run_id, card_id, dependency_issue_message)
            recovered_from_timeout = False
            if timed_out and not has_terminal_complete and not dependency_issue_message and not audit_errors:
                recovered_from_timeout, recovery_message = self._recover_manifest_candidate_after_timeout(project_id, run_id)
                if recovered_from_timeout:
                    self._append_event(
                        project_id,
                        run_id,
                        card_id,
                        event_type="timeout_manifest_recovered",
                        message=recovery_message,
                    )
            if timed_out and not recovered_from_timeout and not has_terminal_complete:
                message = dependency_issue_message or "执行超时，已终止。"
                if audit_errors:
                    message = f"{message} {'; '.join(audit_errors)}"
                failure = self._synthesize_executor_failure(
                    project_id,
                    run_id,
                    reason_code="execution_error",
                    summary=message,
                    details={"timed_out": True, "audit_errors": audit_errors},
                )
                self._apply_failure_report(project_id, run_id, card_id, failure)
                return

            if return_code != 0 and not recovered_from_timeout and not has_terminal_complete:
                message = dependency_issue_message or f"执行器退出码 {return_code}。"
                if audit_errors:
                    message = f"{message} {'; '.join(audit_errors)}"
                transcript_tail = self._transcript_tail(transcript_path)
                if transcript_tail and not dependency_issue_message:
                    message = f"{message} 最近输出：{transcript_tail}"
                failure = self._synthesize_executor_failure(
                    project_id,
                    run_id,
                    reason_code="execution_error",
                    summary=message,
                    details={"exit_code": return_code, "audit_errors": audit_errors, "stderr_tail": transcript_tail},
                )
                self._apply_failure_report(project_id, run_id, card_id, failure)
                return

            if not audit_ok:
                message = "执行后文件系统审计失败：" + "; ".join(audit_errors)
                failure = self._synthesize_executor_failure(
                    project_id,
                    run_id,
                    reason_code="contract_violation",
                    summary=message,
                    details={"audit_errors": audit_errors},
                )
                self._apply_failure_report(project_id, run_id, card_id, failure)
                return

            if dependency_issue_message and terminal_report is None:
                failure = self._synthesize_executor_failure(
                    project_id,
                    run_id,
                    reason_code="runtime_dependency_missing",
                    summary=dependency_issue_message,
                    details=dependency_issue_payload,
                )
                self._apply_failure_report(project_id, run_id, card_id, failure)
                return

            if has_terminal_complete:
                manifest, materialize_errors = self._materialize_completed_manifest(project_id, run_id)
                if materialize_errors:
                    failure = self._synthesize_executor_failure(
                        project_id,
                        run_id,
                        reason_code="contract_violation",
                        summary="Manifest 校验失败：" + "; ".join(materialize_errors),
                        details={
                            "validation_errors": materialize_errors,
                            "failed_report": "report_complete",
                        },
                    )
                    self._apply_failure_report(project_id, run_id, card_id, failure)
                    return

            valid, errors = self.manifest_service.validate_manifest(project_id, run_id)
            if not valid:
                if self.manifest_service.attempt_auto_patch_manifest(project_id, run_id):
                    valid, errors = self.manifest_service.validate_manifest(project_id, run_id)
                    if valid:
                        recovered_from_timeout = True
                        self._append_event(
                            project_id,
                            run_id,
                            card_id,
                            event_type="timeout_manifest_recovered" if timed_out else "manifest_recovered",
                            message="Backend automatically patched missing required manifest declarations for files already present on disk.",
                        )
            if not valid:
                message = "Manifest 校验失败：" + "; ".join(errors)
                failure = self._synthesize_executor_failure(
                    project_id,
                    run_id,
                    reason_code="contract_violation",
                    summary=message,
                    details={"validation_errors": errors},
                )
                self._apply_failure_report(project_id, run_id, card_id, failure)
                return

            manifest = self.manifest_service.load_manifest(project_id, run_id)
            self._persist_backend_manager_brief(project_id, run_id, manifest=manifest)
            self._append_event(project_id, run_id, card_id, event_type="review_started", message="Reviewer 正在验收执行器代码、manifest 和输出资产。")
            self._set_run_status(
                project_id,
                run_id,
                card_id,
                status="reviewing",
                summary="Reviewer 正在验收执行器结果。",
                progress_note="Reviewer 正在验收结果。",
                card_status="reviewing",
            )
            validation_report = self.executor_validation_service.validate_run(project_id, run_id)
            if self._run_status(project_id, run_id) == "cancelled":
                return
            if validation_report.status == "fail":
                message = "Executor validation failed: " + validation_report.summary
                self._append_event(
                    project_id,
                    run_id,
                    card_id,
                    event_type="executor_issue",
                    message=message,
                    payload=validation_report.model_dump(),
                )
                self._set_run_status(project_id, run_id, card_id, status="failed", summary=message, progress_note=None)
                self._commit_run_stage(project_id, run_id, "failed")
                self._notify_background_terminal(project_id, run_id=run_id)
                return
            validation_warning_message = None
            if validation_report.status == "warn":
                warning_codes = ", ".join(sorted({issue.code for issue in validation_report.issues})) or "validation_warning"
                validation_warning_message = f"Executor validation warning ({warning_codes}): {validation_report.summary}"
            self._append_event(
                project_id,
                run_id,
                card_id,
                event_type="executor_validation",
                message=validation_report.summary,
                payload=validation_report.model_dump(),
            )
            self._persist_backend_manager_brief(
                project_id,
                run_id,
                manifest=manifest,
                reviewer=validation_report.reviewer if isinstance(validation_report.reviewer, dict) else None,
                validation=validation_report.model_dump(),
            )
            summary = self.manifest_service.manager_summary(manifest)
            review_result = self._finalize_run_review(project_id, run_id, accept=True, source="reviewer")
            if validation_warning_message:
                self._set_run_attention(project_id, run_id, card_id, validation_warning_message)
                self._append_event(
                    project_id,
                    run_id,
                    card_id,
                    event_type="executor_validation_warning",
                    message=validation_warning_message,
                    payload=validation_report.model_dump(),
                )
            if review_result["accepted"]:
                self._append_event(
                    project_id,
                    run_id,
                    card_id,
                    event_type="reviewer_acceptance",
                    message=f"Reviewer 已验收并接受运行结果。{summary}",
                    payload=validation_report.model_dump(),
                )
                self._commit_run_stage(project_id, run_id, "reviewed")
                self._notify_background_terminal(project_id, run_id=run_id)
            else:
                # Mapping ambiguity or consistency failure: run is "success" + card "needs_review"
                reason = review_result.get("failure_reason")
                if reason == "consistency_failed":
                    msg = f"Reviewer 验收时图一致性检查失败：{', '.join(review_result.get('failure_details', []))}"
                else:
                    msg = f"Reviewer 验收发现输出映射歧义：{', '.join(review_result.get('failure_details', []))}"
                self._append_event(
                    project_id,
                    run_id,
                    card_id,
                    event_type="reviewer_review_incomplete",
                    message=msg,
                    payload=validation_report.model_dump(),
                )
                self._commit_run_stage(project_id, run_id, "needs_review")
        except Exception as exc:
            message = f"执行器运行后处理失败：{exc}"
            logger.exception("Post-run handling failed for project=%s run=%s", project_id, run_id)
            try:
                self._append_event(project_id, run_id, card_id, event_type="run_failed", message=message)
                self._set_run_status(project_id, run_id, card_id, status="failed", summary=message, progress_note=None)
                self._commit_run_stage(project_id, run_id, "failed")
                self._notify_background_terminal(project_id, run_id=run_id)
            except Exception as secondary_exc:
                logger.exception("Failed to persist post-run failure state for project=%s run=%s", project_id, run_id)
                self._write_run_fatal_error(
                    project_id,
                    run_id,
                    "Failed to persist post-run failure state.",
                    primary_exception=exc,
                    secondary_exception=secondary_exc,
                )
        finally:
            self._flush_run_event_buffer(project_id, run_id, clear_sequence=True)
            self._processes.pop(run_id, None)
            self._threads.pop(run_id, None)
            # Cleanup temporary data_mount bind point if empty
            data_mount_point = self.project_service.project_path(project_id) / "data_mount"
            try:
                if data_mount_point.exists() and data_mount_point.is_dir():
                    data_mount_point.rmdir()
            except OSError:
                # Not empty or not accessible — leave it alone
                pass

    def _recover_manifest_candidate_after_timeout(self, project_id: str, run_id: str) -> tuple[bool, str]:
        run_dir = self.project_service.project_path(project_id) / "runs" / run_id
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            return True, "执行器超时但 manifest.json 已存在，继续进入校验与 Reviewer。"
        candidate_path = run_dir / "manifest.candidate.json"
        if not candidate_path.exists():
            return False, "manifest.candidate.json does not exist."
        try:
            payload = json.loads(candidate_path.read_text(encoding="utf-8"))
            manifest = self.manifest_service.normalize_manifest_payload(payload, run_id=run_id)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            return False, f"manifest.candidate.json is not valid: {exc}"
        atomic_write_json(manifest_path, manifest.model_dump(exclude_none=True))
        return True, "执行器超时但 manifest.candidate.json 已完整生成，已提升为 manifest.json 并继续进入校验与 Reviewer。"

    def _execute_run_guarded(
        self,
        *,
        project_id: str,
        run_id: str,
        card_id: str,
        worker_type: str,
        command: list[str],
        cwd: Path,
        environment: dict[str, str],
        execution_guard: Lock | Semaphore,
        guard_kind: str,
        sandboxed: bool = False,
    ) -> None:
        try:
            self._execute_run(
                project_id=project_id,
                run_id=run_id,
                card_id=card_id,
                worker_type=worker_type,
                command=command,
                cwd=cwd,
                environment=environment,
                sandboxed=sandboxed,
            )
        finally:
            self._release_execution_guard(execution_guard, guard_kind)

    def _acquire_execution_guard(self, project_id: str, card_id: str, *, sandboxed: bool, execution_mode: str = "guarded") -> tuple[Lock | Semaphore | None, str]:
        if execution_mode == "workspace_write":
            # Serialize workspace_write runs per project regardless of sandbox.
            card_lock = self._card_lock_for(project_id, card_id)
            if not card_lock.acquire(blocking=False):
                return None, "card_lock"
            execution_lock = self._execution_lock_for(project_id)
            if not execution_lock.acquire(blocking=False):
                card_lock.release()
                return None, "workspace_write_project_lock"
            return _CompositeExecutionGuard(card_lock, execution_lock), "composite"
        if not sandboxed:
            execution_lock = self._execution_lock_for(project_id)
            if not execution_lock.acquire(blocking=False):
                return None, "lock"
            return execution_lock, "lock"
        card_lock = self._card_lock_for(project_id, card_id)
        if not card_lock.acquire(blocking=False):
            return None, "card_lock"
        semaphore = self._execution_semaphore_for(project_id)
        if not semaphore.acquire(blocking=False):
            card_lock.release()
            return None, "semaphore"
        return _CompositeExecutionGuard(card_lock, semaphore), "composite"

    @staticmethod
    def _release_execution_guard(execution_guard: Lock | Semaphore, guard_kind: str) -> None:
        if guard_kind == "composite" and isinstance(execution_guard, _CompositeExecutionGuard):
            execution_guard.release()
            return
        execution_guard.release()

    def _execution_lock_for(self, project_id: str) -> Lock:
        with self._execution_locks_guard:
            if project_id not in self._execution_locks:
                self._execution_locks[project_id] = Lock()
            return self._execution_locks[project_id]

    def _execution_semaphore_for(self, project_id: str) -> Semaphore:
        with self._execution_locks_guard:
            if project_id not in self._execution_semaphores:
                self._execution_semaphores[project_id] = Semaphore(max(1, int(self.project_service.settings.executor_max_concurrent_runs)))
            return self._execution_semaphores[project_id]

    def get_available_run_slots(self, project_id: str) -> int:
        """Best-effort advisory count of free executor slots (sandboxed path only).

        Uses Semaphore._value which is not protected by the execution lock guard,
        so callers must tolerate the value being stale by the time they act on it.
        The 409 race path in submit_claimed_workboard_items handles this gracefully.
        """
        sem = self._execution_semaphore_for(project_id)
        return sem._value  # type: ignore[attr-defined]

    def _card_lock_for(self, project_id: str, card_id: str) -> Lock:
        key = (project_id, card_id)
        with self._execution_locks_guard:
            if key not in self._card_locks:
                self._card_locks[key] = Lock()
            return self._card_locks[key]

    @staticmethod
    def _transcript_tail(transcript_path: Path, *, max_lines: int = 6, max_chars: int = 800) -> str:
        if not transcript_path.exists():
            return ""
        lines = [
            line.removeprefix("- ").strip()
            for line in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        tail = " | ".join(lines[-max_lines:])
        if len(tail) > max_chars:
            return tail[-max_chars:]
        return tail

    def _set_run_status(
        self,
        project_id: str,
        run_id: str,
        card_id: str,
        *,
        status: str,
        summary: str,
        progress_note: str | None,
        card_status: str | None = None,
    ) -> None:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            runs = store.load_runs()
            modules = store.load_modules()
            run = next((item for item in runs if item.run_id == run_id), None)
            if run is None:
                return
            card = next(item for item in cards if item.card_id == card_id)
            run.status = status
            run.summary = summary
            if status in {"success", "failed", "cancelled", "reviewed"}:
                run.finished_at = utc_now()
            if card_status:
                card.status = card_status
            elif status == "failed":
                card.status = "failed"
                card.manager_review = summary
            card.progress_note = progress_note
            ModuleGroupStateService.sync_linked_module_status_from_card(card, modules)
            ModuleGroupStateService.sync_group_hierarchy(cards, modules)
            store.save_runs(runs)
            store.save_modules(modules)
            store.save_cards(cards)
        self._update_background_task_for_run(project_id, run_id, run.task_id, status, summary)
        self._emit_project_event(
            project_id,
            reason="run_status_changed",
            card_id=card_id,
            run_id=run_id,
            task_id=run.task_id,
            status=status,
            payload={"card_status": card_status or card.status, "task_id": run.task_id},
        )

    def _append_event(
        self,
        project_id: str,
        run_id: str,
        card_id: str,
        *,
        event_type: str,
        message: str,
        sequence_hint: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            if sequence_hint:
                sequence = sequence_hint
                key = (project_id, run_id)
                self._event_sequences[key] = max(self._event_sequences.get(key, 0), sequence)
            else:
                sequence = self._next_event_sequence_locked(project_id, run_id, store)
            event = RunEvent(
                event_id=f"evt_{run_id}_{sequence:03d}",
                run_id=run_id,
                card_id=card_id,
                source="executor" if event_type in {"executor_output", "executor_progress", "executor_issue"} else "manager",
                event_type=event_type,
                visibility="bubble",
                preview_id=f"bubble_{card_id}",
                utterance_id=f"utt_{run_id}_{sequence:03d}",
                stream_state="complete",
                message=message,
                created_at=utc_now(),
                payload=payload or {},
            )
            key = (project_id, run_id)
            if event_type == "executor_output":
                buffer = self._event_buffers.setdefault(key, [])
                buffer.append(event)
                if len(buffer) >= _EVENT_FLUSH_BATCH_SIZE:
                    self._flush_event_buffer_locked(project_id, run_id, store)
                return
            self._flush_event_buffer_locked(project_id, run_id, store)
            store.append_run_events(run_id, [event])

    def _next_event_sequence_locked(self, project_id: str, run_id: str, store: Any) -> int:
        key = (project_id, run_id)
        current = self._event_sequences.get(key)
        if current is None:
            current = len(store.load_run_events(run_id))
        current += 1
        self._event_sequences[key] = current
        return current

    def _flush_event_buffer_locked(self, project_id: str, run_id: str, store: Any) -> None:
        key = (project_id, run_id)
        events = self._event_buffers.pop(key, None)
        if not events:
            return
        store.append_run_events(run_id, events)

    def _flush_run_event_buffer(self, project_id: str, run_id: str, *, clear_sequence: bool = False) -> None:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            self._flush_event_buffer_locked(project_id, run_id, store)
            if clear_sequence:
                self._event_sequences.pop((project_id, run_id), None)

    def _write_run_fatal_error(
        self,
        project_id: str,
        run_id: str,
        message: str,
        *,
        primary_exception: Exception | None = None,
        secondary_exception: Exception | None = None,
    ) -> None:
        try:
            run_dir = self.project_service.project_path(project_id) / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            lines = [message, f"created_at: {utc_now()}"]
            if primary_exception is not None:
                lines.extend(
                    [
                        "",
                        "primary_exception:",
                        "".join(traceback.format_exception(primary_exception)).rstrip(),
                    ]
                )
            if secondary_exception is not None:
                lines.extend(
                    [
                        "",
                        "secondary_exception:",
                        "".join(traceback.format_exception(secondary_exception)).rstrip(),
                    ]
                )
            (run_dir / "fatal_error.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            logger.exception("Failed to write fatal run error log for project=%s run=%s", project_id, run_id)

    def _handle_structured_executor_event(self, project_id: str, run_id: str, card_id: str, line: str) -> bool:
        contract = self._build_manager_reporting_contract()
        if not line.startswith(contract.stdout_prefix):
            return False
        payload_text = line[len(contract.stdout_prefix) :].strip()
        try:
            event = ExecutorStructuredEvent.model_validate_json(payload_text)
        except Exception as exc:
            self._append_event(
                project_id,
                run_id,
                card_id,
                event_type="executor_output",
                message=f"Invalid BP_EVENT payload: {exc}",
            )
            return True

        if event.type == "progress_update":
            message = event.message or f"Executor progress update: {event.stage or 'unknown'}"
            self._append_event(
                project_id,
                run_id,
                card_id,
                event_type="executor_progress",
                message=message,
                payload=event.model_dump(),
            )
            if event.message:
                self._set_run_progress_note(project_id, run_id, card_id, event.message)
        elif event.type == "issue_report":
            message = event.message or "Executor reported an issue."
            self._append_event(
                project_id,
                run_id,
                card_id,
                event_type="executor_issue",
                message=message,
                payload=event.model_dump(),
            )
            if event.needs_manager:
                self._set_run_attention(project_id, run_id, card_id, message)
                self._append_event(
                    project_id,
                    run_id,
                    card_id,
                    event_type="run_blocked_on_manager",
                    message=message,
                    payload=event.model_dump(),
                )
                # Do NOT enqueue auto wake while the subprocess is still running.
                # Terminal post-process states will enqueue the appropriate wake event.
                # This prevents Manager from mutating graph state while outputs are still being produced.
        else:
            message = event.summary or event.message or "Executor final report received."
            self._append_event(
                project_id,
                run_id,
                card_id,
                event_type="executor_final_report",
                message=message,
                payload=event.model_dump(),
            )
            if event.summary:
                self._set_run_summary(project_id, run_id, event.summary)

        self._save_manager_brief(project_id, run_id, event)
        return True

    def _task_packet(
        self,
        project_id: str,
        run_id: str,
        card: Card,
        assets: list[Asset],
        worker_type: str,
        profile_id: str | None = None,
        python_runtime: str | None = None,
        r_runtime: str | None = None,
        cards: list[Card] | None = None,
        runs: list[RunRecord] | None = None,
    ) -> TaskPacket:
        if cards is None or runs is None:
            snapshot = self.project_service.get_project_snapshot(project_id)
            cards = cards or snapshot["cards"]
            runs = runs or snapshot["graph"].runs
        resolution_index = self.input_resolution_service.build_index(
            cards,
            GraphState(assets=assets, runs=runs),
        )
        blocking_resolutions = [
            resolution
            for item in card.inputs
            if item.asset_id
            for resolution in [self.input_resolution_service.resolve_input(item.asset_id, resolution_index)]
            if resolution.resolved_asset_id is None or resolution.status not in VALID_LAUNCHABLE_INPUT_STATUSES
        ]
        if blocking_resolutions:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"Card {card.card_id} has unresolved or non-launchable inputs.",
                    "error_code": "input_resolution_failed",
                    "card_id": card.card_id,
                    "inputs": [
                        {
                            "requested_asset_id": item.requested_asset_id,
                            "resolved_asset_id": item.resolved_asset_id,
                            "resolved_by": item.resolved_by,
                            "producer_card_id": item.producer_card_id,
                            "producer_role": item.producer_role,
                            "status": item.status,
                        }
                        for item in blocking_resolutions
                    ],
                },
            )

        input_assets_by_id: dict[str, TaskPacketAsset] = {}
        card_inputs: list[TaskPacketCardInput] = []
        for item in card.inputs:
            resolution = self.input_resolution_service.resolve_input(item.asset_id, resolution_index) if item.asset_id else None
            if resolution is None or resolution.asset is None:
                card_inputs.append(
                    TaskPacketCardInput(
                        label=item.label,
                        asset_id=item.asset_id,
                        requested_asset_id=item.asset_id,
                        resolved_asset_id=None,
                        resolved_by=None,
                        producer_card_id=None,
                        producer_role=None,
                        asset_path=None,
                        asset_type=None,
                        status="missing",
                    )
                )
                continue
            input_assets_by_id.setdefault(
                resolution.asset.asset_id,
                TaskPacketAsset(
                    asset_id=resolution.asset.asset_id,
                    path=resolution.asset.path,
                    type=resolution.asset.asset_type,
                    title=resolution.asset.title,
                    status=resolution.asset.status,
                    requested_asset_id=resolution.requested_asset_id,
                    resolved_by=resolution.resolved_by,
                    producer_card_id=resolution.producer_card_id,
                    producer_role=resolution.producer_role,
                ),
            )
            card_inputs.append(
                TaskPacketCardInput(
                    label=item.label,
                    asset_id=resolution.asset.asset_id,
                    requested_asset_id=resolution.requested_asset_id,
                    resolved_asset_id=resolution.asset.asset_id,
                    resolved_by=resolution.resolved_by,
                    producer_card_id=resolution.producer_card_id,
                    producer_role=resolution.producer_role,
                    asset_path=resolution.asset.path,
                    asset_type=resolution.asset.asset_type,
                    status=resolution.asset.status,
                )
            )
        input_assets = list(input_assets_by_id.values())

        # Fail fast if any data_mount input assets are stale or missing
        data_mount_input_assets = [a for a in input_assets if a.path.startswith("data_mount/")]
        if data_mount_input_assets:
            issues = self.project_service.check_data_mount_assets_freshness(project_id)
            if issues:
                # Backfill updated statuses into the packet snapshot for consistency
                fresh_graph = self.project_service.graph_store(project_id).load_graph()
                fresh_asset_by_id = {a.asset_id: a for a in fresh_graph.assets}
                for issue in issues:
                    fresh_asset = fresh_asset_by_id.get(issue["asset_id"])
                    if fresh_asset:
                        new_status = fresh_asset.status
                        if issue["asset_id"] in input_assets_by_id:
                            input_assets_by_id[issue["asset_id"]].status = new_status
                        for ci in card_inputs:
                            if ci.asset_id == issue["asset_id"]:
                                ci.status = new_status
            stale_ids = {i["asset_id"] for i in issues}
            bad_assets = [a for a in data_mount_input_assets if a.asset_id in stale_ids]
            if bad_assets:
                details = ", ".join(f"{a.path} ({next(i['reason'] for i in issues if i['asset_id'] == a.asset_id)})" for a in bad_assets)
                raise HTTPException(
                    status_code=409,
                    detail=f"Stale or missing mounted data assets cannot be used as inputs: {details}. "
                    "Please re-register the assets from the data directory.",
                )

        output_refs = list(card.outputs)
        if not output_refs:
            raise HTTPException(
                status_code=409,
                detail=f"Card {card.card_id} has no explicit output contract. Define structured outputs before starting a run.",
            )
        card_outputs: list[TaskPacketCardOutput] = []
        expected_outputs: list[ExpectedOutput] = []
        for item in output_refs:
            output_format = item.preferred_format or (item.accepted_formats[0] if item.accepted_formats else None)
            path_hint = self._default_output_path_hint(card.card_id, run_id, item.role, item.artifact_class, output_format)
            task_output = TaskPacketCardOutput(
                role=item.role,
                label=item.label,
                artifact_class=item.artifact_class,
                accepted_formats=list(item.accepted_formats),
                preferred_format=item.preferred_format,
                required=item.required,
                description=item.description,
                asset_id=item.asset_id,
                status=item.status,
                path_hint=path_hint,
            )
            card_outputs.append(task_output)
            expected_outputs.append(task_output.model_copy())

        expected_outputs.extend(self._system_expected_outputs(card.card_id, run_id))

        result_dir = f"results/{card.card_id}/{run_id}"
        execution_mode = self.project_service.get_project_runtime_preferences(project_id).execution_mode
        allowed_paths = [f"runs/{run_id}/", f"{result_dir}/", f"scripts/generated/{run_id}/"]
        if execution_mode == "workspace_write":
            allowed_paths.insert(0, "work/")

        # Detect data_mount/... input assets and attach mounted data directory path
        mounted_data_directory: str | None = None
        for asset in input_assets:
            if asset.path.startswith("data_mount/"):
                mount = self.project_service.get_project_data_directory(project_id)
                if mount is None:
                    raise HTTPException(
                        status_code=409,
                        detail="This run requires data_mount/... inputs but the project does not have a mounted data directory. "
                        "Please mount a data directory in project settings before running.",
                    )
                mounted_data_directory = mount.resolved_path
                break

        return TaskPacket(
            task_id=run_id,
            project_id=project_id,
            card_id=card.card_id,
            card_title=card.title,
            card_status=card.status,
            goal=card.summary,
            input_assets=input_assets,
            card_inputs=card_inputs,
            card_outputs=card_outputs,
            expected_outputs=expected_outputs,
            allowed_paths=allowed_paths,
            readonly_paths=[asset.path for asset in input_assets],
            forbidden_paths=[".git/", "graph/"],
            execution_policy={
                "mode": execution_mode,
                "network": "prompt",
                "write_policy": "allowed_paths_with_post_run_audit",
                "on_policy_violation": "fail_or_quarantine",
            },
            constraints=[
                "Do not overwrite existing valid assets.",
                f"Write outputs under {result_dir}/",
                "Write the candidate manifest to $BLUEPRINT_MANIFEST_CANDIDATE_PATH.",
                "Call $BLUEPRINT_EXECUTOR_RESULT_TOOL complete or fail exactly once before exiting.",
                "Do not modify graph/, .git/, or upstream input assets.",
                "Do not install missing runtime packages. If required packages/tools are unavailable, use $BLUEPRINT_EXECUTOR_RESULT_TOOL fail to report them and stop.",
            ],
            worker_instructions=(
                "You are a bioinformatics worker agent. Read $BLUEPRINT_TASK_PACKET, use the declared inputs, "
                "write only inside allowed_paths, and produce terminal reports plus a candidate manifest that matches expected_outputs."
            ),
            run_context=RunContext(
                run_id=run_id,
                worker_type=worker_type,
                project_root=str(self.project_service.project_path(project_id)),
                run_dir=f"runs/{run_id}",
                result_dir=result_dir,
            ),
            executor_context=self._build_executor_context(project_id, card, worker_type, profile_id=profile_id, python_runtime=python_runtime, r_runtime=r_runtime),
            manager_reporting_contract=self._build_manager_reporting_contract(),
            mounted_data_directory=mounted_data_directory,
        )

    @staticmethod
    def _normalize_python_runtime(python_runtime: str | None) -> str | None:
        if python_runtime in {None, "", "__system__"}:
            return None
        return python_runtime

    @staticmethod
    def _normalize_r_runtime(r_runtime: str | None) -> str | None:
        if r_runtime in {None, "", "__system__"}:
            return None
        return r_runtime

    def _build_executor_context(
        self,
        project_id: str,
        card: Card,
        worker_type: str,
        profile_id: str | None = None,
        python_runtime: str | None = None,
        r_runtime: str | None = None,
    ) -> ExecutorContext:
        graph = self.project_service.graph_store(project_id).load_graph()
        default_context = self._default_executor_context(
            graph,
            card,
            worker_type,
            profile_id=profile_id,
            python_runtime=python_runtime,
            r_runtime=r_runtime,
        )
        if card.executor_context is not None:
            context = self._merge_executor_context(default_context, card.executor_context)
            if profile_id is not None:
                context.executor_profile = f"{worker_type}_worker"
                context.executor_profile_id = profile_id
            if python_runtime:
                context.runtime_bindings.conda_env = python_runtime
                context.runtime_bindings.env["BLUEPRINT_PYTHON_RUNTIME"] = python_runtime
            if r_runtime:
                context.runtime_bindings.r_env = r_runtime
                context.runtime_bindings.env["BLUEPRINT_R_RUNTIME"] = r_runtime
        else:
            context = default_context
        self._apply_script_preference_block(graph, card, context)
        return self._attach_library_bindings(project_id, context)

    @staticmethod
    def _default_executor_context(
        graph: Any,
        card: Card,
        worker_type: str,
        profile_id: str | None = None,
        python_runtime: str | None = None,
        r_runtime: str | None = None,
    ) -> ExecutorContext:
        conda_env = python_runtime or graph.metadata.get("default_conda_env")
        r_env = r_runtime or graph.metadata.get("default_r_env")
        runtime_env = {}
        if python_runtime:
            runtime_env["BLUEPRINT_PYTHON_RUNTIME"] = python_runtime
        if r_runtime:
            runtime_env["BLUEPRINT_R_RUNTIME"] = r_runtime
        return ExecutorContext(
            executor_profile=f"{worker_type}_worker",
            executor_profile_id=profile_id,
            skills=[module_id.replace("module_", "") for module_id in card.linked_modules],
            instruction_blocks=[
                "Prefer reproducible scripts over ad-hoc shell pipelines.",
                "Summarize findings conservatively and keep outputs traceable to inputs.",
            ],
            references=[
                ExecutorReference(type="file", path="configs/params.yaml", description="Project-level runtime parameters."),
            ],
            tool_policy=ExecutorToolPolicy(network="allow", python=True, rscript=True, shell=True, git_write=False),
            runtime_bindings=RuntimeBindings(
                conda_env=conda_env,
                r_env=r_env,
                working_dir="{run_dir}",
                env=runtime_env,
            ),
        )

    @staticmethod
    def _apply_script_preference_block(graph: Any, card: Card, context: ExecutorContext) -> None:
        """Append a soft script-preference instruction block if one is resolved."""
        script_pref = (
            getattr(getattr(card, "executor_context", None), "script_preference", None)
            or (graph.metadata.get("runtime_preferences") or {}).get("script_preference")
        )
        if not script_pref or script_pref == "auto":
            return
        block = {
            "prefer_python": "Soft script preference: prefer Python scripts when practical. This is not a hard constraint; use R when more reliable.",
            "prefer_r": "Soft script preference: prefer R scripts when practical. This is not a hard constraint; use Python when more reliable.",
            "prefer_mixed": "Soft script preference: use whichever language (Python or R) is most natural for each sub-task.",
        }.get(script_pref)
        if block and block not in (context.instruction_blocks or []):
            context.instruction_blocks = list(context.instruction_blocks or []) + [block]

    @staticmethod
    def _merge_executor_context(default_context: ExecutorContext, override: ExecutorContext) -> ExecutorContext:
        context = default_context.model_copy(deep=True)
        if override.executor_profile is not None:
            context.executor_profile = override.executor_profile
        if override.executor_profile_id is not None:
            context.executor_profile_id = override.executor_profile_id
        if "skills" in override.model_fields_set:
            context.skills = list(override.skills)
        if "mcp_servers" in override.model_fields_set:
            context.mcp_servers = list(override.mcp_servers)
        if "instruction_blocks" in override.model_fields_set:
            context.instruction_blocks = list(override.instruction_blocks)
        if "references" in override.model_fields_set:
            # Merge (append + dedup by path) so card-specific reference-data
            # files augment rather than replace the default worker references.
            existing_paths = {ref.path for ref in context.references}
            merged = list(context.references)
            for item in override.references:
                if item.path not in existing_paths:
                    merged.append(item.model_copy(deep=True))
                    existing_paths.add(item.path)
            context.references = merged
        if "tool_policy" in override.model_fields_set:
            policy_fields = override.tool_policy.model_fields_set
            if "network" in policy_fields and override.tool_policy.network in {"allow", "deny"}:
                context.tool_policy.network = override.tool_policy.network
            if "python" in policy_fields:
                context.tool_policy.python = override.tool_policy.python
            if "rscript" in policy_fields:
                context.tool_policy.rscript = override.tool_policy.rscript
            if "shell" in policy_fields:
                context.tool_policy.shell = override.tool_policy.shell
            if "git_write" in policy_fields:
                context.tool_policy.git_write = override.tool_policy.git_write
        if "runtime_bindings" in override.model_fields_set:
            runtime_fields = override.runtime_bindings.model_fields_set
            if "conda_env" in runtime_fields:
                context.runtime_bindings.conda_env = override.runtime_bindings.conda_env
            if "r_env" in runtime_fields:
                context.runtime_bindings.r_env = override.runtime_bindings.r_env
            if "container_image" in runtime_fields:
                context.runtime_bindings.container_image = override.runtime_bindings.container_image
            if "env" in runtime_fields:
                context.runtime_bindings.env.update(override.runtime_bindings.env)
        if "script_asset_requirements" in override.model_fields_set:
            context.script_asset_requirements = [
                ExecutorScriptAssetRequirement.model_validate(item.model_dump() if hasattr(item, "model_dump") else item)
                for item in override.script_asset_requirements
            ]
        if "script_asset_bindings" in override.model_fields_set:
            context.script_asset_bindings = [
                ExecutorScriptAssetBinding.model_validate(item.model_dump() if hasattr(item, "model_dump") else item)
                for item in override.script_asset_bindings
            ]
        if "template_metadata" in override.model_fields_set:
            merged_meta = dict(context.template_metadata or {})
            merged_meta.update(override.template_metadata)
            context.template_metadata = merged_meta
        return context

    @staticmethod
    def _build_manager_reporting_contract() -> ManagerReportingContract:
        return ManagerReportingContract(
            transport="stdout_bp_event",
            stdout_prefix="BP_EVENT ",
            file_path="runs/{run_id}/manager_updates.jsonl",
        )

    def _attach_library_bindings(self, project_id: str, context: ExecutorContext) -> ExecutorContext:
        runtime_name = context.runtime_bindings.conda_env or None
        template_metadata = dict(context.template_metadata)
        template_metadata["library_skill_bindings"] = self.library_registry_service.resolve_skill_bindings(context.skills)
        template_metadata["library_mcp_bindings"] = self.library_registry_service.resolve_mcp_bindings(
            project_id,
            context.mcp_servers,
            runtime=runtime_name,
        )
        context.template_metadata = template_metadata
        return context

    def _save_manager_brief(self, project_id: str, run_id: str, event: ExecutorStructuredEvent) -> None:
        run_dir = self.project_service.project_path(project_id) / "runs" / run_id
        path = run_dir / "manager_brief.json"
        brief = self._load_manager_brief(project_id, run_id)
        brief["run_id"] = run_id
        if event.type == "progress_update":
            brief["latest_progress"] = event.model_dump()
        elif event.type == "issue_report":
            issues = list(brief.get("issues") or [])
            issues.append(event.model_dump())
            brief["issues"] = issues
        elif event.type == "final_report":
            brief["final_report"] = event.model_dump()
        atomic_write_json(path, brief)

    def _load_manager_brief(self, project_id: str, run_id: str) -> dict[str, Any]:
        path = self.project_service.project_path(project_id) / "runs" / run_id / "manager_brief.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _terminal_report_paths(self, project_id: str, run_id: str) -> dict[str, Path]:
        run_dir = self.project_service.project_path(project_id) / "runs" / run_id
        return {
            "run_dir": run_dir,
            "terminal": run_dir / "terminal_report.json",
            "completion": run_dir / "executor_completion.json",
            "failure": run_dir / "executor_failure.json",
            "state": run_dir / "executor_result_state.json",
        }

    def _load_terminal_report(self, project_id: str, run_id: str) -> TerminalReport | None:
        path = self._terminal_report_paths(project_id, run_id)["terminal"]
        if not path.exists():
            return None
        try:
            return TerminalReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError):
            return None

    def _load_executor_completion_report(self, project_id: str, run_id: str) -> ExecutorCompletionReport | None:
        path = self._terminal_report_paths(project_id, run_id)["completion"]
        if not path.exists():
            return None
        try:
            return ExecutorCompletionReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError):
            return None

    def _load_executor_failure_report(self, project_id: str, run_id: str) -> ExecutorFailureReport | None:
        path = self._terminal_report_paths(project_id, run_id)["failure"]
        if not path.exists():
            return None
        try:
            return ExecutorFailureReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError):
            return None

    def _persist_backend_manager_brief(
        self,
        project_id: str,
        run_id: str,
        *,
        manifest: Manifest | None = None,
        failure: ExecutorFailureReport | None = None,
        reviewer: dict[str, Any] | None = None,
        validation: dict[str, Any] | None = None,
    ) -> None:
        run_dir = self.project_service.project_path(project_id) / "runs" / run_id
        path = run_dir / "manager_brief.json"
        brief: dict[str, Any] = {"run_id": run_id}
        if manifest is not None:
            brief["final_report"] = {
                "summary": self.manifest_service.manager_summary(manifest),
                "warnings": self.manifest_service.manager_warnings(manifest),
            }
        if failure is not None:
            brief["failure_report"] = failure.model_dump(exclude_none=True)
            brief["final_report"] = {
                "summary": failure.summary,
                "warnings": [],
            }
        if reviewer:
            brief["reviewer"] = reviewer
        if validation:
            brief["executor_validation"] = validation
        atomic_write_json(path, brief)

    def _synthesize_executor_failure(
        self,
        project_id: str,
        run_id: str,
        *,
        reason_code: FailureReasonCode,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> ExecutorFailureReport:
        run_dir = self.project_service.project_path(project_id) / "runs" / run_id
        failure = ExecutorFailureReport(
            schema_version="executor_failure.v1",
            reason_code=reason_code,
            summary=summary,
            details=details or {},
        )
        atomic_write_json(run_dir / "executor_failure.json", failure.model_dump(exclude_none=True))
        atomic_write_json(
            run_dir / "terminal_report.json",
            TerminalReport(
                schema_version="executor_terminal_report.v1",
                run_id=run_id,
                terminal_kind="synthetic_failure",
                accepted_at=utc_now(),
                summary=summary,
                reason_code=reason_code,
                status="failed",
                failure_report_path="executor_failure.json",
            ).model_dump(exclude_none=True),
        )
        self._persist_backend_manager_brief(project_id, run_id, failure=failure)
        return failure

    def _materialize_completed_manifest(self, project_id: str, run_id: str) -> tuple[Manifest | None, list[str]]:
        completion = self._load_executor_completion_report(project_id, run_id)
        if completion is not None and isinstance(completion.canonical_manifest, dict):
            try:
                manifest = self.manifest_service.normalize_manifest_payload(completion.canonical_manifest, run_id=run_id)
            except (ValidationError, ValueError) as exc:
                return None, [f"Manifest schema validation failed: {exc}"]
            run_dir = self.project_service.project_path(project_id) / "runs" / run_id
            atomic_write_json(run_dir / "manifest.json", manifest.model_dump(exclude_none=True))
            return manifest, []
        return self.manifest_service.promote_candidate_manifest(project_id, run_id)

    def _apply_failure_report(
        self,
        project_id: str,
        run_id: str,
        card_id: str,
        failure: ExecutorFailureReport,
    ) -> None:
        payload = failure.model_dump(exclude_none=True)
        if failure.reason_code == "runtime_dependency_missing":
            self._append_event(
                project_id,
                run_id,
                card_id,
                event_type="runtime_dependency_missing",
                message=failure.summary,
                payload=payload,
            )
        else:
            self._append_event(
                project_id,
                run_id,
                card_id,
                event_type="run_failed",
                message=failure.summary,
                payload=payload,
            )
        self._set_run_status(project_id, run_id, card_id, status="failed", summary=failure.summary, progress_note=None)
        self._set_run_attention(project_id, run_id, card_id, failure.summary)
        self._commit_run_stage(project_id, run_id, "failed")
        self._notify_background_terminal(project_id, run_id=run_id)

    def _blocking_dependency_issue(self, project_id: str, run_id: str) -> tuple[str | None, dict[str, Any]]:
        failure_report = self._load_executor_failure_report(project_id, run_id)
        if failure_report is not None and failure_report.reason_code == "runtime_dependency_missing":
            message = failure_report.summary or "执行器报告运行环境依赖不足。"
            return message, {"issues": [failure_report.model_dump(exclude_none=True)]}
        issue_path = self.project_service.project_path(project_id) / "runs" / run_id / "dependency_issue.json"
        file_payload: dict[str, Any] = {}
        if issue_path.exists():
            try:
                file_payload = json.loads(issue_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                file_payload = {}
        brief = self._load_manager_brief(project_id, run_id)
        issues = list(file_payload.get("issues") or [])
        if not issues:
            issues = list(brief.get("dependency_issues") or [])
        if not issues:
            issues = [
                issue
                for issue in list(brief.get("issues") or [])
                if isinstance(issue, dict) and issue.get("metadata", {}).get("issue_kind") == "runtime_dependency_missing"
            ]
        blocking = [
            issue
            for issue in issues
            if isinstance(issue, dict) and issue.get("metadata", {}).get("blocking", True)
        ]
        if not blocking:
            return None, {"issues": issues}
        missing: list[str] = []
        ecosystems: list[str] = []
        for issue in blocking:
            metadata = issue.get("metadata") or {}
            ecosystem = metadata.get("ecosystem")
            if isinstance(ecosystem, str) and ecosystem and ecosystem not in ecosystems:
                ecosystems.append(ecosystem)
            for package in metadata.get("missing_packages") or []:
                if isinstance(package, str) and package and package not in missing:
                    missing.append(package)
        if missing:
            ecosystem_text = "/".join(ecosystems) if ecosystems else "runtime"
            message = f"执行器报告运行环境依赖不足：缺乏 {ecosystem_text} 包/工具 {', '.join(missing)}。"
        else:
            first_message = next((issue.get("message") for issue in blocking if isinstance(issue.get("message"), str)), None)
            message = first_message or "执行器报告运行环境依赖不足。"
        return message, {"issues": blocking}

    def _set_run_progress_note(self, project_id: str, run_id: str, card_id: str, progress_note: str) -> None:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            graph = store.load_graph()
            run = next(item for item in graph.runs if item.run_id == run_id)
            card = next(item for item in cards if item.card_id == card_id)
            run.summary = progress_note
            card.progress_note = progress_note
            store.save_runs(graph.runs)
            store.save_cards(cards)

    def _set_run_summary(self, project_id: str, run_id: str, summary: str) -> None:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            graph = store.load_graph()
            run = next(item for item in graph.runs if item.run_id == run_id)
            run.summary = summary
            store.save_runs(graph.runs)

    def _set_run_attention(self, project_id: str, run_id: str, card_id: str, message: str) -> None:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            graph = store.load_graph()
            run = next(item for item in graph.runs if item.run_id == run_id)
            card = next(item for item in cards if item.card_id == card_id)
            run.needs_manager_attention = True
            card.progress_note = message
            store.save_runs(graph.runs)
            store.save_cards(cards)
        self._emit_project_event(
            project_id,
            reason="run_attention_changed",
            card_id=card_id,
            run_id=run_id,
            task_id=run.task_id,
            status=run.status,
            payload={"needs_manager_attention": True, "message": message, "task_id": run.task_id},
        )

    @staticmethod
    def _active_run_statuses() -> set[str]:
        return {"queued", "launching", "needs_approval", "running", "reviewing"}

    def _has_active_run(self, runs: list[RunRecord], card_id: str) -> bool:
        active = self._active_run_statuses()
        return any(run.card_id == card_id and run.status in active for run in runs)

    def _has_other_active_runs(self, runs: list[RunRecord], card_id: str, exclude_run_id: str) -> bool:
        active = self._active_run_statuses()
        return any(
            run.card_id == card_id and run.run_id != exclude_run_id and run.status in active
            for run in runs
        )

    def has_active_runs(self, project_id: str) -> bool:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            runs = store.load_runs()
            active = self._active_run_statuses()
            return any(run.status in active for run in runs)

    def _run_status(self, project_id: str, run_id: str) -> str | None:
        lock = self.project_service.lock_for(project_id)
        with lock:
            return self.project_service.graph_store(project_id).get_run_status(run_id)

    def _update_background_task_for_run(self, project_id: str, run_id: str, task_id: str | None, run_status: str, summary: str) -> None:
        resolved_task_id = task_id
        if resolved_task_id is None:
            task = self.background_task_service.find_by_run_id(project_id, run_id)
            resolved_task_id = task.task_id if task is not None else None
        if not resolved_task_id:
            logger.debug("Background task missing for run status update project=%s run=%s status=%s", project_id, run_id, run_status)
            return
        mapped_status = self._background_task_status_for_run_status(run_status)
        updates: dict[str, Any] = {
            "status": mapped_status,
            "result": {"message": summary, "run_status": run_status},
        }
        now = utc_now()
        if mapped_status in {"launching", "running"}:
            updates["started_at"] = now
        if mapped_status in {"succeeded", "failed", "cancelled", "interrupted"}:
            updates["finished_at"] = now
        if mapped_status == "failed":
            updates["error"] = summary
        self.background_task_service.update_task(project_id, resolved_task_id, **updates)

    @staticmethod
    def _background_task_status_for_run_status(run_status: str) -> str:
        if run_status == "queued":
            return "queued"
        if run_status == "launching":
            return "launching"
        if run_status in {"running", "reviewing", "needs_approval"}:
            return "running"
        if run_status in {"success", "reviewed"}:
            return "succeeded"
        if run_status == "failed":
            return "failed"
        if run_status == "cancelled":
            return "cancelled"
        return "interrupted"

    @staticmethod
    def _validate_acceptance_graph_consistent(
        card: Card,
        graph: GraphState,
        run_id: str,
    ) -> list[str]:
        """Return a list of error messages if an accepted card violates the acceptance contract.

        Contract:
        - every accepted card.outputs[].asset_id has a current materialization
          (either as a concrete asset in graph.assets, or via asset_materializations binding)
        - each current materialization asset has status == "valid"
        - card.outputs[].status agrees with the real asset status
        """
        errors: list[str] = []
        asset_by_id = {a.asset_id: a for a in graph.assets}
        materializations = graph.metadata.get("asset_materializations") if isinstance(graph.metadata, dict) else {}
        materializations = materializations or {}
        for output in WorkerService._card_declared_outputs(card):
            if output.asset_id is None:
                errors.append(
                    f"Accepted card {card.card_id} output {output.role or output.label} has no asset_id."
                )
                continue
            # Try direct concrete asset lookup first (legacy or intentional concrete binding)
            asset = asset_by_id.get(output.asset_id)
            if asset is None:
                # Logical id: resolve through materialization binding
                binding = materializations.get(output.asset_id)
                if binding:
                    current_asset_id = binding.get("current_asset_id")
                    if current_asset_id:
                        asset = asset_by_id.get(current_asset_id)
                if asset is None:
                    errors.append(
                        f"Accepted card {card.card_id} output {output.role or output.label} points to missing asset {output.asset_id} (no current materialization)."
                    )
                    continue
            if asset.status != "valid":
                errors.append(
                    f"Accepted card {card.card_id} output {output.role or output.label} points to {asset.status} asset {asset.asset_id}."
                )
                continue
            if output.status != asset.status:
                errors.append(
                    f"Accepted card {card.card_id} output {output.role or output.label} status {output.status} disagrees with asset status {asset.status}."
                )
        return errors

    @staticmethod
    def _assert_acceptance_graph_consistent(
        card: Card,
        graph: GraphState,
        run_id: str,
    ) -> None:
        errors = WorkerService._validate_acceptance_graph_consistent(card, graph, run_id)
        if errors:
            raise AssertionError(errors[0])

    @staticmethod
    def _resolve_output_mappings(
        card: Card,
        assets: list[Asset],
        *,
        manifest_created_assets: list[object] | None = None,
        expected_outputs: list[object] | None = None,
    ) -> dict[str, Asset]:
        """Resolve planned/placeholder output IDs to real produced assets.

        Returns a dict mapping planned_id -> real Asset.
        Mapping priority:
        1. Exact manifest asset_id if present and resolvable.
        2. Manifest asset_id as planned_asset_id.
        3. Unique produced asset by metadata.role, scoped to the current run.
        4. Unique produced asset by normalized output path.

        System outputs (run_summary, run_preview) do not override card-declared outputs.
        """

        def _get(obj: object, name: str, default: Any = None) -> Any:
            if isinstance(obj, dict):
                return obj.get(name, default)
            return getattr(obj, name, default)

        assets_by_id = {a.asset_id: a for a in assets}
        assets_by_planned_id: dict[str, Asset] = {}
        for a in assets:
            planned = a.metadata.get("planned_asset_id")
            if planned:
                assets_by_planned_id[planned] = a

        assets_by_role: dict[str, Asset] = {}
        duplicate_roles: set[str] = set()
        for a in assets:
            role = str(a.metadata.get("role") or "")
            if role:
                if role in assets_by_role:
                    duplicate_roles.add(role)
                else:
                    assets_by_role[role] = a

        # Normalize paths for fallback matching
        assets_by_norm_path: dict[str, Asset] = {}
        duplicate_paths: set[str] = set()
        for a in assets:
            norm = str(a.path or "").replace("\\", "/").strip()
            if norm:
                if norm in assets_by_norm_path:
                    duplicate_paths.add(norm)
                else:
                    assets_by_norm_path[norm] = a

        expected_by_role: dict[str, str] = {}
        for item in (expected_outputs or []):
            role = _get(item, "role", "")
            asset_id = _get(item, "asset_id", None)
            if role:
                expected_by_role[role] = asset_id or role

        # Card-declared roles that are NOT system outputs
        card_declared_roles = {str(o.role or "") for o in card.outputs if o.role}
        system_roles = SYSTEM_OUTPUT_ROLES

        real_by_planned: dict[str, Asset] = {}
        matched_asset_ids: set[str] = set()

        if manifest_created_assets is not None:
            for manifest_asset in manifest_created_assets:
                m_asset_id = _get(manifest_asset, "asset_id", None)
                role = _get(manifest_asset, "role", "")
                actual_asset: Asset | None = None

                # Priority 1: exact manifest asset_id
                if m_asset_id and m_asset_id in assets_by_id:
                    actual_asset = assets_by_id[m_asset_id]
                # Priority 2: manifest asset_id as planned_asset_id
                elif m_asset_id and m_asset_id in assets_by_planned_id:
                    actual_asset = assets_by_planned_id[m_asset_id]
                # Priority 3: unique role match (skip system roles that conflict with card-declared outputs)
                elif role and role not in duplicate_roles and role in assets_by_role:
                    if role not in system_roles or role not in card_declared_roles:
                        actual_asset = assets_by_role[role]
                    elif role in card_declared_roles and len([a for a in assets if str(a.metadata.get("role") or "") == role]) == 1:
                        # If there's exactly one asset for this role, it's safe
                        actual_asset = assets_by_role[role]
                # Priority 4: unique normalized path match
                m_path = _get(manifest_asset, "path", "")
                norm_path = str(m_path or "").replace("\\", "/").strip()
                if actual_asset is None and norm_path and norm_path not in duplicate_paths and norm_path in assets_by_norm_path:
                    actual_asset = assets_by_norm_path[norm_path]

                if actual_asset:
                    planned_id = m_asset_id or expected_by_role.get(role, role)
                    if planned_id:
                        real_by_planned[planned_id] = actual_asset
                        matched_asset_ids.add(actual_asset.asset_id)
                else:
                    logger.error("Unmatched manifest output: role=%s asset_id=%s", role, m_asset_id)

        for a in assets:
            if a.asset_id not in matched_asset_ids:
                logger.warning("Produced asset not referenced in manifest: asset_id=%s role=%s", a.asset_id, a.metadata.get("role"))

        return real_by_planned

    @staticmethod
    def _card_declared_created_assets(items: list[object]) -> list[object]:
        return [item for item in items if WorkerService._object_role(item) not in SYSTEM_OUTPUT_ROLES]

    @staticmethod
    def _card_declared_expected_outputs(items: list[object]) -> list[object]:
        return [item for item in items if WorkerService._object_role(item) not in SYSTEM_OUTPUT_ROLES]

    @staticmethod
    def _card_declared_outputs(card: Card) -> list[CardOutputSpec]:
        return [output for output in card.outputs if str(output.role or "") not in SYSTEM_OUTPUT_ROLES]

    @staticmethod
    def _card_declared_output_items(card: Card) -> list[tuple[int, CardOutputSpec]]:
        return [(index, output) for index, output in enumerate(card.outputs) if str(output.role or "") not in SYSTEM_OUTPUT_ROLES]

    @staticmethod
    def _object_role(obj: object) -> str:
        if isinstance(obj, dict):
            return str(obj.get("role") or "")
        return str(getattr(obj, "role", "") or "")

    @staticmethod
    def _format_unmapped_output(output_index: int, output: CardOutputSpec) -> str:
        return f"output[{output_index}] role={output.role} asset_id={output.asset_id}"

    @staticmethod
    def _resolve_output_bindings(
        card: Card,
        assets: list[Asset],
        *,
        manifest_created_assets: list[object] | None = None,
        expected_outputs: list[object] | None = None,
    ) -> tuple[list[tuple[int, Asset]], list[str]]:
        """Resolve card output slots to produced assets using the canonical review rules."""
        real_by_planned = WorkerService._resolve_output_mappings(
            card,
            assets,
            manifest_created_assets=manifest_created_assets,
            expected_outputs=expected_outputs,
        )
        produced_asset_ids = {asset.asset_id for asset in assets}
        asset_by_id = {asset.asset_id: asset for asset in assets}
        bindings: list[tuple[int, Asset]] = []
        unmapped: list[str] = []
        for output_index, output in WorkerService._card_declared_output_items(card):
            if output.asset_id is not None and output.asset_id in real_by_planned:
                bindings.append((output_index, real_by_planned[output.asset_id]))
            elif output.asset_id is not None and output.asset_id in produced_asset_ids:
                bindings.append((output_index, asset_by_id[output.asset_id]))
            elif output.asset_id is None and output.role in real_by_planned:
                bindings.append((output_index, real_by_planned[output.role]))
            else:
                unmapped.append(WorkerService._format_unmapped_output(output_index, output))
        return bindings, unmapped

    @staticmethod
    def _materialize_run_assets(
        *,
        graph: object,
        run_id: str,
        card: Card,
        created_assets: list[dict],
        status: str,
        input_asset_ids: list[str],
        input_bindings: list[dict] | None = None,
    ) -> list[Asset]:
        root_graph = graph
        assets: list[Asset] = []
        frozen_bindings = list(input_bindings) if input_bindings else []
        for index, item in enumerate(created_assets, start=1):
            asset_id = f"asset_{run_id}_{item['role']}_{index}"
            existing = next((asset for asset in root_graph.assets if asset.asset_id == asset_id), None)
            artifact_class = item.get("artifact_class") or item.get("type") or "binary"
            if existing:
                # Never demote an existing valid asset back to candidate.
                if existing.status == "valid" and status == "candidate":
                    pass
                else:
                    existing.status = status
                existing.asset_type = artifact_class
                existing.path = item["path"]
                existing.summary = item.get("description") or existing.summary
                existing.metadata = {
                    **existing.metadata,
                    "role": item["role"],
                    "planned_asset_id": item.get("asset_id"),
                    "format": item.get("format"),
                    "sha256": item.get("sha256"),
                    "size_bytes": item.get("size_bytes"),
                    "input_bindings": frozen_bindings,
                }
                existing.report_selected = artifact_class == "document"
                asset = existing
            else:
                asset = Asset(
                    asset_id=asset_id,
                    asset_type=artifact_class,
                    title=f"{card.title} {item['role']}".strip(),
                    status=status,
                    created_by_run=run_id,
                    path=item["path"],
                    depends_on=input_asset_ids,
                    summary=item.get("description") or f"{card.title} 产出文件。",
                    metadata={
                        "role": item["role"],
                        "planned_asset_id": item.get("asset_id"),
                        "format": item.get("format"),
                        "sha256": item.get("sha256"),
                        "size_bytes": item.get("size_bytes"),
                        "input_bindings": frozen_bindings,
                    },
                    report_selected=artifact_class == "document",
                )
                root_graph.assets.append(asset)
            assets.append(asset)
        return assets

    @staticmethod
    def _materialize_claims(graph: object, run_id: str, findings: list[str], asset_ids: list[str]) -> list[str]:
        claim_ids: list[str] = []
        for index, finding in enumerate(findings, start=1):
            claim_id = f"claim_{run_id}_{index:02d}"
            existing = next((claim for claim in graph.claims if claim.claim_id == claim_id), None)
            if existing:
                existing.text = finding
                existing.status = "valid"
            else:
                graph.claims.append(
                    Claim(
                        claim_id=claim_id,
                        text=finding,
                        status="valid",
                        depends_on_assets=asset_ids,
                        created_by_run=run_id,
                        report_selected=True,
                    )
                )
            claim_ids.append(claim_id)
        return claim_ids

    @staticmethod
    def _attach_assets_to_card(card: Card, assets: list[Asset]) -> None:
        for asset in assets:
            if asset.asset_id not in card.linked_assets:
                card.linked_assets.append(asset.asset_id)

    @staticmethod
    def _detect_unmapped_outputs(
        card: Card,
        assets: list[Asset],
        *,
        manifest_created_assets: list[object] | None = None,
        expected_outputs: list[object] | None = None,
    ) -> list[str]:
        """Pre-flight check: returns planned output IDs that cannot be resolved to real assets."""
        _bindings, unmapped = WorkerService._resolve_output_bindings(
            card,
            assets,
            manifest_created_assets=manifest_created_assets,
            expected_outputs=expected_outputs,
        )
        return unmapped

    @staticmethod
    def _sync_card_outputs(
        card: Card,
        assets: list[Asset],
        graph: GraphState | None = None,
        *,
        manifest_created_assets: list[object] | None = None,
        expected_outputs: list[object] | None = None,
    ) -> list[str]:
        """Bind card output placeholders to real produced assets.

        Preserves logical output ids in card.outputs[].asset_id. Writes the
        materialization binding to graph.metadata when graph is provided.
        """
        bindings, unmapped = WorkerService._resolve_output_bindings(
            card,
            assets,
            manifest_created_assets=manifest_created_assets,
            expected_outputs=expected_outputs,
        )
        asset_lookup = {asset.asset_id: asset for asset in assets}
        for output_index, real_asset in bindings:
            output = card.outputs[output_index]
            planned_asset_id = output.asset_id
            WorkerService._backfill_planned_asset_alias(real_asset, planned_asset_id, asset_lookup=asset_lookup)
            # Preserve logical id when one exists; otherwise bind to concrete id.
            if planned_asset_id is None:
                output.asset_id = real_asset.asset_id
            output.status = real_asset.status
            if graph is not None and output.asset_id is not None:
                AssetMaterializationService.set_current(
                    graph,
                    planned_asset_id=output.asset_id,
                    concrete_asset=real_asset,
                    producer_card_id=card.card_id,
                    producer_role=output.role,
                    producer_run_id=real_asset.created_by_run or "",
                )
        return unmapped

    @staticmethod
    def _recover_stable_planned_asset_alias(
        candidate_id: str | None,
        asset_lookup: dict[str, Asset] | None = None,
    ) -> str | None:
        alias = str(candidate_id or "").strip()
        if not alias:
            return
        if not asset_lookup:
            return alias
        return InputResolutionService.recover_stable_planned_asset_alias(alias, asset_lookup)

    @staticmethod
    def _backfill_planned_asset_alias(
        asset: Asset,
        planned_asset_id: str | None,
        *,
        asset_lookup: dict[str, Asset] | None = None,
    ) -> None:
        metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
        current_alias = str(metadata.get("planned_asset_id") or "").strip()
        stable_current_alias = WorkerService._recover_stable_planned_asset_alias(current_alias, asset_lookup)
        stable_incoming_alias = WorkerService._recover_stable_planned_asset_alias(planned_asset_id, asset_lookup)
        alias = stable_current_alias or stable_incoming_alias
        if not alias:
            if asset.metadata is not metadata:
                asset.metadata = metadata
            return
        asset.metadata = {
            **metadata,
            "planned_asset_id": alias,
        }

    @staticmethod
    def _current_output_assets(card: Card, assets: list[Asset], *, current_run_id: str) -> list[Asset]:
        linked_asset_ids = set(card.linked_assets)
        input_asset_ids = {item.asset_id for item in card.inputs if item.asset_id}
        declared_roles = {item.role for item in card.outputs}
        outputs: list[Asset] = []
        for asset in assets:
            if asset.asset_id not in linked_asset_ids:
                continue
            if asset.asset_id in input_asset_ids:
                continue
            if asset.created_by_run == current_run_id:
                continue
            role = str(asset.metadata.get("role") or asset.asset_id or "")
            if role and role in declared_roles and asset.status == "valid":
                outputs.append(asset)
        return outputs

    @staticmethod
    def _supersede_previous_outputs(
        card: Card,
        assets: list[Asset],
        claims: list[Claim],
        current_run_id: str,
        *,
        previous_asset_ids: set[str] | None = None,
    ) -> None:
        current_asset_ids = set(card.linked_assets) if previous_asset_ids is None else previous_asset_ids
        for asset in assets:
            if (
                asset.asset_id in current_asset_ids
                and asset.created_by_run
                and asset.created_by_run != current_run_id
                and asset.status == "valid"
            ):
                asset.status = "superseded"
        stale_assets = {asset.asset_id for asset in assets if asset.status == "superseded"}
        for claim in claims:
            if stale_assets.intersection(claim.depends_on_assets) and claim.status == "valid":
                claim.status = "superseded"

    @staticmethod
    def _recover_logical_output_ids_from_superseded(
        card: Card,
        assets: list[Asset],
    ) -> None:
        """Recover logical planned ids for outputs that point to superseded concrete assets.

        Legacy compatibility: older projects may have card.outputs[].asset_id set to a
        concrete asset id. After superseding that asset on a new accept, this recovers
        the logical id from the asset's metadata.planned_asset_id so validation passes.
        """
        asset_by_id = {a.asset_id: a for a in assets}
        for out in card.outputs:
            if not out.asset_id:
                continue
            asset = asset_by_id.get(out.asset_id)
            if asset is None or asset.status != "superseded":
                continue
            metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
            logical_id = str(metadata.get("planned_asset_id") or "").strip()
            if logical_id and logical_id != out.asset_id:
                out.asset_id = logical_id

    def _resolve_worker_type(self, worker_type: str | None) -> str:
        candidate = worker_type or self.project_service.settings.default_worker_type
        if candidate and self._is_worker_configured(candidate):
            return candidate
        if worker_type:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Executor {worker_type} is not configured. "
                    "Set the matching command template before starting a run."
                ),
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Default executor {candidate or '<unset>'} is not configured. "
                "Set the matching command template before starting a run."
            ),
        )

    def _resolve_profile_across_workers(self, config_service: AppConfigService, profile_id: str) -> dict[str, Any] | None:
        for worker_type in self.registry:
            profile = config_service.resolve_executor_profile(worker_type, profile_id=profile_id)
            if profile:
                return profile
        return None

    def _is_worker_configured(self, worker_type: str) -> bool:
        adapter = self.registry.get(worker_type)
        if adapter is None:
            return False
        checker = getattr(adapter, "is_configured", None)
        if callable(checker):
            return bool(checker(self.project_service.settings))
        return True

    def _new_run_id(self, runs: list[RunRecord]) -> str:
        existing_ids = {run.run_id for run in runs}
        while True:
            candidate = f"run_{uuid4().hex[:12]}"
            if candidate not in existing_ids:
                return candidate

    def _get_work_item(self, project_id: str, card_id: str) -> dict:
        work_order = self.flow_service.get_work_order(project_id)
        return next(item for item in work_order["work_items"] if item["card_id"] == card_id)

    def _commit_run_stage(self, project_id: str, run_id: str, stage: str) -> None:
        if stage == "reviewed":
            message = f"Materialize reviewed run {run_id}"
        elif stage == "cleanup":
            message = f"Cleanup run {run_id}"
        else:
            message = f"Run lifecycle {run_id}: {stage}"
        try:
            self.project_service.git_service(project_id).commit(message)
        except Exception as exc:
            logger.exception("Git commit failed for project=%s run=%s stage=%s", project_id, run_id, stage)
            self._mark_project_needs_git_repair(project_id, f"Git commit failed for {stage} on {run_id}: {exc}")
            try:
                graph = self.project_service.graph_store(project_id).load_graph()
                run = next((item for item in graph.runs if item.run_id == run_id), None)
                if run is None:
                    return
                self._append_event(
                    project_id,
                    run_id,
                    run.card_id,
                    event_type="git_commit_failed",
                    message=f"Git commit failed for {stage}: {exc}",
                )
            except Exception:
                logger.exception("Failed to persist git commit failure event for project=%s run=%s", project_id, run_id)

    def _notify_background_terminal(self, project_id: str, *, run_id: str | None = None, job_id: str | None = None) -> None:
        if self.background_terminal_callback is None:
            return
        try:
            self.background_terminal_callback(project_id, run_id, job_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                logger.debug(
                    "Skipping background terminal notification for missing project=%s run=%s job=%s",
                    project_id,
                    run_id,
                    job_id,
                )
                return
            logger.exception(
                "Failed to notify background terminal state for project=%s run=%s job=%s",
                project_id,
                run_id,
                job_id,
            )
        except Exception:
            logger.exception(
                "Failed to notify background terminal state for project=%s run=%s job=%s",
                project_id,
                run_id,
                job_id,
            )

    def _emit_project_event(
        self,
        project_id: str,
        *,
        reason: str,
        card_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.project_event_service is None:
            return
        try:
            self.project_event_service.emit(
                project_id,
                reason=reason,
                card_id=card_id,
                run_id=run_id,
                task_id=task_id,
                status=status,
                payload=payload,
            )
        except Exception:
            logger.exception("Failed to emit project event for project=%s reason=%s run=%s", project_id, reason, run_id)

    def _mark_project_needs_git_repair(self, project_id: str, reason: str) -> None:
        root = self.project_service.project_path(project_id)
        try:
            store = self.project_service.graph_store(project_id)
            graph = store.load_graph()
            graph.metadata["needs_git_repair"] = {
                "reason": reason,
                "updated_at": utc_now(),
            }
            store.save_graph(graph)
        except Exception:
            logger.exception("Failed to mark project %s as needing git repair", project_id)
            try:
                atomic_write_json(
                    root / "project_recovery_required.json",
                    {
                        "reason": f"{reason}; failed to update graph metadata",
                        "created_at": utc_now(),
                    },
                )
            except Exception:
                logger.exception("Failed to write recovery marker for project=%s", project_id)

    def _reconcile_active_runs(self) -> None:
        for child in sorted(self.project_service.settings.data_root.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith("_"):
                continue
            project_id = child.name
            lock = self.project_service.lock_for(project_id)
            with lock:
                store = self.project_service.graph_store(project_id)
                try:
                    cards = store.load_cards()
                    graph = store.load_graph()
                except Exception as exc:
                    logger.exception("Failed to reconcile project %s", project_id)
                    try:
                        atomic_write_json(
                            child / "project_recovery_required.json",
                            {
                                "reason": f"Project failed to load during active-run reconcile: {exc}",
                                "created_at": utc_now(),
                            },
                        )
                    except Exception:
                        logger.exception("Failed to write reconcile recovery marker for project=%s", project_id)
                    continue
                card_map = {card.card_id: card for card in cards}
                changed = False
                reconciled_run_ids: list[str] = []
                for run in graph.runs:
                    if run.status not in {"queued", "running", "reviewing"}:
                        continue
                    thread = self._threads.get(run.run_id)
                    if thread and thread.is_alive():
                        continue
                    run.status = "failed"
                    run.summary = "Backend restarted before executor completed; run marked failed during reconcile."
                    run.finished_at = utc_now()
                    card = card_map.get(run.card_id)
                    if card and card.status in {"running", "reviewing"}:
                        card.status = "failed"
                        card.progress_note = None
                        card.manager_review = run.summary
                        ModuleGroupStateService.sync_linked_module_status_from_card(card, graph.modules)
                    events = store.load_run_events(run.run_id)
                    events.append(
                        RunEvent(
                            event_id=f"evt_{run.run_id}_{len(events) + 1:03d}",
                            run_id=run.run_id,
                            card_id=run.card_id,
                            source="manager",
                            event_type="run_reconciled",
                            visibility="bubble",
                            preview_id=f"bubble_{run.card_id}",
                            utterance_id=f"utt_{run.run_id}_{len(events) + 1:03d}",
                            stream_state="complete",
                            message=run.summary,
                            created_at=utc_now(),
                        )
                    )
                    store.save_run_events(run.run_id, events)
                    self._update_background_task_for_run(project_id, run.run_id, run.task_id, "failed", run.summary)
                    reconciled_run_ids.append(run.run_id)
                    changed = True
                if changed:
                    ModuleGroupStateService.sync_group_hierarchy(cards, graph.modules)
                    store.save_graph(graph)
                    store.save_cards(cards)
            for run_id in reconciled_run_ids:
                self._reconciled_run_ids.append((project_id, run_id))

    def flush_reconciled_run_notifications(self) -> list[tuple[str, str]]:
        """Send terminal notifications for runs reconciled during startup.

        Must be called AFTER inject_wake_dispatch() so that the wake
        dispatch pathway is fully wired.
        """
        pending = self._reconciled_run_ids[:]
        self._reconciled_run_ids.clear()
        for project_id, run_id in pending:
            self._notify_background_terminal(project_id, run_id=run_id)
        return pending

    @staticmethod
    def _default_output_path_hint(
        card_id: str,
        run_id: str,
        role: str,
        artifact_class: str,
        output_format: str | None = None,
    ) -> str:
        extension = output_format or default_format_for_artifact_class(artifact_class)  # type: ignore[arg-type]
        return f"results/{card_id}/{run_id}/{role}.{extension}"

    @classmethod
    def _system_expected_outputs(cls, card_id: str, run_id: str) -> list[ExpectedOutput]:
        return [
            ExpectedOutput(
                role="run_summary",
                label="Run summary",
                artifact_class="document",
                accepted_formats=["md", "html", "txt"],
                preferred_format="md",
                required=True,
                description="System-generated run summary for manager review.",
                path_hint=cls._default_output_path_hint(card_id, run_id, "run_summary", "document", "md"),
            ),
            ExpectedOutput(
                role="run_preview",
                label="Run preview",
                artifact_class="figure",
                accepted_formats=["svg", "png", "pdf"],
                preferred_format="svg",
                required=True,
                description="System-generated preview asset for quick inspection.",
                path_hint=cls._default_output_path_hint(card_id, run_id, "run_preview", "figure", "svg"),
            ),
        ]
