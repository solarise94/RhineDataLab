from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
import threading
import traceback
from typing import Any, Callable
from uuid import uuid4

from fastapi import HTTPException

from app.models.chat import ChatSessionMessage, ChatSessionMessageTimelineItem
from app.services.background_task_service import BackgroundTaskService
from app.services.project_event_service import ProjectEventService
from app.services.project_service import ProjectService
from app.services.runtime_dependency_state_service import (
    ACTIVE_RUNTIME_DEPENDENCY_JOB_STATUSES,
    runtime_dependency_failure_details,
)
from app.services.utils import atomic_write_json, read_json, utc_now


JobStatus = str
logger = logging.getLogger(__name__)


@dataclass
class RuntimeDependencyJob:
    job_id: str
    project_id: str
    task_id: str
    payload: dict[str, Any]
    task_type: str = "runtime_dependency_install"
    status: JobStatus = "queued"
    phase: str = "queued"
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    future: Future | None = field(default=None, repr=False)
    # P1 observability fields
    status_detail: str | None = None
    changed: bool | None = None
    command_preview: list[str] | None = None
    child_pid: int | None = None
    log_path: str | None = None
    last_heartbeat_at: str | None = None
    # Repurposed from dead placeholders: timestamp of the most recent stdout/stderr
    # line the handler reported. Used by the watchdog to distinguish "slow but
    # alive" from "dead" (F3).
    last_stdout_at: str | None = None
    last_stderr_at: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    # Layer F2: real progress metering for package installs and reference downloads.
    progress: int = 0
    progress_label: str | None = None
    bytes_total: int | None = None
    bytes_downloaded: int | None = None
    download_rate_bps: int | None = None


class RuntimeDependencyJobService:
    def __init__(
        self,
        project_service: ProjectService,
        max_workers: int | None = None,
        project_event_service: ProjectEventService | None = None,
        background_task_service: BackgroundTaskService | None = None,
        background_terminal_callback: Callable[[str, str | None, str | None], None] | None = None,
        chat_session_service: Any | None = None,
        max_install_workers: int = 2,
        max_download_workers: int = 1,
    ) -> None:
        self.project_service = project_service
        effective_max_workers = max_workers if max_workers is not None else max_install_workers + max_download_workers
        self.executor = ThreadPoolExecutor(max_workers=effective_max_workers, thread_name_prefix="runtime-deps")
        self.jobs: dict[str, RuntimeDependencyJob] = {}
        self.lock = threading.Lock()
        self.runtime_locks: dict[tuple[str, str], threading.Lock] = {}
        # Layer C: kind-semaphores over the shared pool so downloads and
        # installs cannot starve one another.
        self._install_semaphore = threading.Semaphore(max_install_workers)
        self._download_semaphore = threading.Semaphore(max_download_workers)
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_handles: dict[str, Any] = {}
        self.project_event_service = project_event_service
        self.background_task_service = background_task_service or BackgroundTaskService(project_service)
        self.background_terminal_callback = background_terminal_callback
        self.chat_session_service = chat_session_service
        # P2 watchdog (currently scoped to future-done reconciliation only;
        # pre-launch stale kill and child-pid checks are disabled until we
        # switch from subprocess.run to Popen with real child pid tracking.)
        self._watchdog_interval_seconds = 30
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="runtime-deps-watchdog")
        self._watchdog_thread.start()

    def submit(
        self,
        project_id: str,
        payload: dict[str, Any],
        handler,
        *,
        task_type: str = "runtime_dependency_install",
    ) -> RuntimeDependencyJob:
        # P2: atomic submission — task and job are created as one logical unit.
        # If any step fails, roll back the partial record.
        task = None
        try:
            task = self.background_task_service.create_task(
                project_id,
                task_type=task_type,
                affected={"job_ids": [], "card_ids": [payload.get("source", {}).get("card_id")] if isinstance(payload.get("source"), dict) and payload.get("source", {}).get("card_id") else []},
                adapter={"kind": "dependency_installer"},
            )
            job = RuntimeDependencyJob(
                job_id=f"depjob_{uuid4().hex}",
                project_id=project_id,
                task_id=task.task_id,
                payload=payload,
                task_type=task_type,
            )
            self.background_task_service.update_task(
                project_id,
                task.task_id,
                affected={"job_ids": [job.job_id], "card_ids": task.affected.card_ids},
            )
            with self.lock:
                self._load_project_jobs_locked(project_id)
                self.jobs[job.job_id] = job
                self._persist_project_jobs_locked(project_id)
            self._emit_project_event(job)
            future = self.executor.submit(self._run, job.job_id, handler)
            with self.lock:
                job.future = future
            return job
        except Exception as submit_exc:
            logger.exception("Runtime dependency job submission failed, rolling back")
            # If the job was already persisted, mark it terminal so duplicate
            # suppression does not block on a dead record.
            if "job" in locals():
                try:
                    with self.lock:
                        job.status = "failed"
                        job.phase = "failed"
                        job.error = f"Runtime dependency job submission failed before executor scheduling: {submit_exc}"
                        job.finished_at = utc_now()
                        self._persist_project_jobs_locked(project_id)
                        self._emit_project_event(job)
                except Exception:
                    pass
            if task is not None:
                try:
                    self.background_task_service.update_task(
                        project_id,
                        task.task_id,
                        status="failed",
                        error="Submission rolled back due to creation failure.",
                    )
                except Exception:
                    pass
            raise

    def get(self, job_id: str) -> RuntimeDependencyJob | None:
        with self.lock:
            job = self.jobs.get(job_id)
        if job is not None:
            return job
        for project in self.project_service.list_projects():
            with self.lock:
                self._load_project_jobs_locked(project.project_id)
                job = self.jobs.get(job_id)
                if job is not None:
                    return job
        return None

    def get_for_project(self, project_id: str, job_id: str) -> RuntimeDependencyJob | None:
        needs_publish = False
        with self.lock:
            self._load_project_jobs_locked(project_id)
            job = self.jobs.get(job_id)
            if job is None or job.project_id != project_id:
                return None
            # Self-heal: if in-memory is terminal but disk may be stale, repair now.
            # This ensures REST consumers always see consistent state and workboard
            # derivation reads durable truth.
            if job.status in {"succeeded", "failed"} and job.phase in {"succeeded", "failed"}:
                needs_publish = self._self_heal_terminal_drift_locked(job)
        if needs_publish:
            self._publish_terminal_chat_receipt(job)
        return job

    def cancel(self, project_id: str, job_id: str) -> dict[str, Any]:
        """Best-effort cancellation of an active dependency job.

        Sets a per-job cancellation flag and closes any registered I/O handle
        (e.g. a urllib response for reference downloads). The handler is
        responsible for detecting the cancellation and returning a failed
        result with error_code ``cancelled``.
        """
        with self.lock:
            self._load_project_jobs_locked(project_id)
            job = self.jobs.get(job_id)
            if job is None or job.project_id != project_id:
                raise HTTPException(status_code=404, detail="Runtime dependency job not found")
            if job.status not in ACTIVE_RUNTIME_DEPENDENCY_JOB_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail=f"Job {job_id} is not active (status={job.status}).",
                )
            event = self._cancel_events.setdefault(job_id, threading.Event())
            event.set()
            handle = self._cancel_handles.get(job_id)
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        return {
            "job_id": job_id,
            "status": "cancelling",
            "phase": job.phase,
        }

    def _register_cancel_handle(self, job_id: str, handle: Any) -> None:
        """Register an I/O handle that can be closed to abort a download."""
        with self.lock:
            if job_id in self._cancel_events and self._cancel_events[job_id].is_set():
                # Already cancelled before registration; close immediately.
                try:
                    handle.close()
                except Exception:
                    pass
                return
            self._cancel_handles[job_id] = handle

    def _is_cancelled(self, job_id: str) -> bool:
        event = self._cancel_events.get(job_id)
        return event is not None and event.is_set()

    def _cleanup_cancel_state(self, job_id: str) -> None:
        """Remove cancellation event/handle for a finished job to avoid leaks."""
        self._cancel_events.pop(job_id, None)
        self._cancel_handles.pop(job_id, None)

    def _kind_lock_and_semaphore(
        self, job: RuntimeDependencyJob
    ) -> tuple[str, tuple[str, str], threading.Semaphore]:
        """Return (kind, runtime_lock_key, kind_semaphore) for a job.

        Reference-data downloads (Layer C) use a per-sha256 lock and the
        download semaphore; package installs keep the per-runtime lock and
        install semaphore.
        """
        payload = job.payload if isinstance(job.payload, dict) else {}
        task_type = str(job.task_type or "runtime_dependency_install")
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        spec = source.get("spec") if isinstance(source, dict) else None
        if task_type == "reference_data_download" and isinstance(spec, dict) and spec.get("sha256"):
            sha256 = str(spec["sha256"]).strip()
            return "reference", (job.project_id, f"ref:{sha256}"), self._download_semaphore
        runtime = str(payload.get("runtime") or "").strip()
        return "install", (job.project_id, runtime), self._install_semaphore

    def _run(self, job_id: str, handler) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job.status = "running"
            job.phase = "waiting_for_runtime_lock"
            job.started_at = utc_now()
            self.background_task_service.update_task(
                job.project_id, job.task_id, status="running", started_at=job.started_at,
                adapter={"kind": "dependency_installer", "metadata": {"phase": job.phase, "job_id": job.job_id}},
            )
            self._persist_project_jobs_locked(job.project_id)
            self._emit_project_event(job)
            kind, lock_key, semaphore = self._kind_lock_and_semaphore(job)
            runtime_lock = self.runtime_locks.setdefault(lock_key, threading.Lock())
            # C2: reuse an event created by a racing cancel() call so the
            # cancellation flag is not silently discarded.
            cancel_event = self._cancel_events.setdefault(job_id, threading.Event())
        try:
            with runtime_lock:
                with semaphore:
                    with self.lock:
                        job.phase = "building_command"
                        job.last_heartbeat_at = utc_now()
                        self._persist_project_jobs_locked(job.project_id)
                    result = handler(
                        job.project_id,
                        job.payload,
                        phase_callback=self._make_phase_callback(job_id),
                        job_id=job.job_id,
                        cancel_event=cancel_event,
                    )
        except Exception as exc:
            logger.exception("Runtime dependency job failed: %s", job_id)
            with self.lock:
                job.status = "failed"
                job.phase = "failed"
                job.error = "".join(traceback.format_exception(exc))
                job.finished_at = utc_now()
                persisted = False
                try:
                    self._persist_project_jobs_locked(job.project_id)
                    persisted = True
                except Exception:
                    logger.exception(
                        "Failed to persist failed dependency job state: project=%s job=%s",
                        job.project_id,
                        job.job_id,
                    )
                try:
                    self.background_task_service.update_task(
                        job.project_id,
                        job.task_id,
                        status="failed",
                        finished_at=job.finished_at,
                        error=job.error,
                        adapter={"kind": "dependency_installer", "metadata": {"phase": "failed", "job_id": job.job_id}},
                    )
                except Exception:
                    logger.exception(
                        "Failed to update background task for failed dependency job: project=%s job=%s",
                        job.project_id,
                        job.job_id,
                    )
            self._cleanup_cancel_state(job_id)
            if persisted:
                self._publish_terminal_chat_receipt(job)
            return
        with self.lock:
            self._cleanup_cancel_state(job_id)
            ok = bool(result.get("ok"))
            job.status = "succeeded" if ok else "failed"
            job.phase = "succeeded" if ok else "failed"
            job.result = result
            job.status_detail = result.get("status_detail") if isinstance(result, dict) else None
            job.changed = result.get("changed") if isinstance(result, dict) else None
            job.error = None if ok else str(result.get("message") or "Dependency installation failed.")
            job.finished_at = utc_now()
            # Persist dependency job state first — this is the source of truth for
            # workboard derivation. Must not be silently skipped by an exception in
            # background task update.
            persisted = False
            try:
                self._persist_project_jobs_locked(job.project_id)
                persisted = True
            except Exception:
                logger.exception(
                    "Failed to persist terminal dependency job state: project=%s job=%s",
                    job.project_id,
                    job.job_id,
                )
            # Background task update is best-effort; must not prevent terminal publish.
            try:
                self.background_task_service.update_task(
                    job.project_id,
                    job.task_id,
                    status=job.status,
                    finished_at=job.finished_at,
                    result=result,
                    error=job.error,
                    adapter={"kind": "dependency_installer", "metadata": {"phase": job.phase, "job_id": job.job_id}},
                )
            except Exception:
                logger.exception(
                    "Failed to update background task for terminal dependency job: project=%s job=%s",
                    job.project_id,
                    job.job_id,
                )
        if persisted:
            self._publish_terminal_chat_receipt(job)

    def _format_terminal_content(self, job: RuntimeDependencyJob) -> str:
        """Build the user-visible terminal receipt content from job state."""
        status = job.status
        result = job.result if isinstance(job.result, dict) else {}
        changed = result.get("changed")
        status_detail = result.get("status_detail")
        if status == "succeeded":
            if changed is False or status_detail == "already_satisfied":
                return "依赖已满足，无需安装。"
            return "依赖安装完成。"
        # Recognize the English backend-restart interruption text and
        # render a dedicated Chinese receipt instead.
        error = (job.error or "").strip()
        if "interrupted by backend restart" in error:
            return "依赖安装被后端重启中断。"
        error_lines = error.splitlines()
        raw_message = error_lines[-1].strip() if error_lines else ""
        if raw_message:
            return f"依赖安装失败：{raw_message}"
        return "依赖安装失败。"

    def _publish_terminal_chat_receipt(self, job: RuntimeDependencyJob) -> None:
        """Idempotent terminal receipt publisher.

        Must be called only after job state and background task state have
        been persisted, and only outside ``self.lock``.
        """
        self._emit_project_event(job)

        # Manual-session chat receipt branch
        if self.chat_session_service is None:
            self._notify_background_terminal(job.project_id, job_id=job.job_id)
            return
        source = job.payload.get("source") if isinstance(job.payload, dict) else {}
        source_dict = source if isinstance(source, dict) else {}
        session_id = str(source_dict.get("session_id") or "").strip()
        if not session_id:
            self._notify_background_terminal(job.project_id, job_id=job.job_id)
            return
        # Skip chat upsert when the session is the active auto owner.
        # Auto wake will produce its own summary; appending here would duplicate.
        try:
            from app.services.manager_auto_service import ManagerAutoService
            from app.api.deps import get_manager_auto_service
            auto_svc: ManagerAutoService = get_manager_auto_service()
            auto_state = auto_svc.get_state(job.project_id)
            if auto_state.enabled and auto_state.owner_session_id == session_id:
                self._notify_background_terminal(job.project_id, job_id=job.job_id)
                return
        except Exception:
            pass
        content = self._format_terminal_content(job)
        message_id = f"depjob_terminal_{job.job_id}"
        timeline_id = f"depjob_terminal_timeline_{job.job_id}"
        try:
            self.chat_session_service.upsert_message(
                job.project_id,
                session_id,
                ChatSessionMessage(
                    id=message_id,
                    role="manager",
                    content=content,
                    state="done",
                    timeline=[
                        ChatSessionMessageTimelineItem(
                            id=timeline_id,
                            kind="text",
                            content=content,
                            status="done",
                        ),
                    ],
                ),
            )
        except Exception:
            logger.exception(
                "Failed to upsert terminal dependency chat message: project=%s job=%s session=%s",
                job.project_id,
                job.job_id,
                session_id,
            )
        self._notify_background_terminal(job.project_id, job_id=job.job_id)

    def bump_heartbeat(self, job_id: str) -> None:
        """Best-effort heartbeat bump for stalled read loops.

        Called by long-running handlers (e.g. the Popen line loop in
        ``manager_blueprint_tools``) when no output has arrived for several
        seconds. Persists ``last_heartbeat_at`` and emits an event so the
        frontend can tell the job is still alive.
        """
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            job.last_heartbeat_at = utc_now()
            self._persist_project_jobs_locked(job.project_id)
            self._emit_project_event(job)

    def _make_phase_callback(self, job_id: str):
        """Return a callback that updates phase, progress, and heartbeat for a running job."""
        def callback(phase: str, **kwargs: Any) -> None:
            with self.lock:
                job = self.jobs.get(job_id)
                if job is None:
                    return
                job.phase = phase
                job.last_heartbeat_at = utc_now()
                if "child_pid" in kwargs:
                    job.child_pid = kwargs["child_pid"]
                if "command_preview" in kwargs:
                    job.command_preview = kwargs["command_preview"]
                if "log_path" in kwargs:
                    job.log_path = kwargs["log_path"]
                if "progress" in kwargs and kwargs["progress"] is not None:
                    job.progress = max(0, min(100, int(kwargs["progress"])))
                if "progress_label" in kwargs:
                    job.progress_label = kwargs["progress_label"]
                if "bytes_total" in kwargs:
                    job.bytes_total = kwargs["bytes_total"]
                if "bytes_downloaded" in kwargs:
                    job.bytes_downloaded = kwargs["bytes_downloaded"]
                if "download_rate_bps" in kwargs:
                    job.download_rate_bps = kwargs["download_rate_bps"]
                if "stdout_tail" in kwargs:
                    job.stdout_tail = str(kwargs["stdout_tail"])
                    job.last_stdout_at = utc_now()
                if "stderr_tail" in kwargs:
                    job.stderr_tail = str(kwargs["stderr_tail"])
                    job.last_stderr_at = utc_now()
                self.background_task_service.update_task(
                    job.project_id,
                    job.task_id,
                    adapter={"kind": "dependency_installer", "metadata": {"phase": job.phase, "job_id": job.job_id}},
                )
                self._persist_project_jobs_locked(job.project_id)
                self._emit_project_event(job)
        return callback

    def _emit_project_event(self, job: RuntimeDependencyJob) -> None:
        if self.project_event_service is None:
            return
        source_payload = job.payload.get("source") if isinstance(job.payload, dict) else {}
        source = source_payload if isinstance(source_payload, dict) else {}
        payload: dict[str, Any] = {
            "task_id": job.task_id,
            "task_type": job.task_type,
            "job_status": job.status,
            "phase": job.phase,
            "runtime": job.payload.get("runtime"),
            "packages": job.payload.get("packages"),
            "manager": job.payload.get("manager"),
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "ok": bool(job.result.get("ok")) if isinstance(job.result, dict) else None,
        }
        # Forward success classification fields so the frontend can render
        # the correct receipt (no-op vs real install) without an extra fetch.
        if isinstance(job.result, dict):
            if job.result.get("status_detail") is not None:
                payload["status_detail"] = job.result["status_detail"]
            if job.result.get("changed") is not None:
                payload["changed"] = job.result["changed"]
        # Enrich terminal events with normalized failure details
        if job.status in {"failed", "succeeded"}:
            try:
                details = runtime_dependency_failure_details(job)
                # Merge key failure fields into the event payload
                for key in (
                    "error_code",
                    "message",
                    "requested_package",
                    "attempted_candidates",
                    "fallback_available",
                    "retry_hint",
                    "dedupe_key",
                    "stdout_tail",
                    "stderr_tail",
                    "truncated",
                    "ecosystem",
                    "resolved_runtime",
                    "card_id",
                    "run_id",
                    "session_id",
                ):
                    if key in details:
                        payload[key] = details[key]
            except Exception:
                logger.exception(
                    "Failed to enrich runtime dependency event: project_id=%s job_id=%s",
                    job.project_id,
                    job.job_id,
                )
        # Layer F2: forward progress fields on mid-run events, not just terminal.
        # Terminal failure details already include stdout/stderr tails; for active
        # jobs we forward them directly so the live-log line is available.
        for key in (
            "progress",
            "progress_label",
            "bytes_total",
            "bytes_downloaded",
            "download_rate_bps",
            "stdout_tail",
            "stderr_tail",
        ):
            value = getattr(job, key)
            if value is not None:
                payload[key] = value
        # Include resolution status so consumers can distinguish manually resolved jobs.
        resolution_status = job.payload.get("resolution_status") if isinstance(job.payload, dict) else None
        if resolution_status:
            payload["resolution_status"] = resolution_status
        try:
            self.project_event_service.emit(
                job.project_id,
                reason="runtime_dependency_job_changed",
                card_id=source.get("card_id"),
                run_id=source.get("run_id"),
                job_id=job.job_id,
                task_id=job.task_id,
                status=job.status,
                payload=payload,
            )
        except Exception:
            logger.exception("Failed to emit runtime dependency project event: project_id=%s job_id=%s", job.project_id, job.job_id)

    def _jobs_path(self, project_id: str):
        return self.project_service.project_path(project_id) / "chat" / "runtime_dependency_jobs.json"

    def _load_project_jobs_locked(self, project_id: str) -> list[RuntimeDependencyJob]:
        now = utc_now()
        items = read_json(self._jobs_path(project_id), [])
        if not isinstance(items, list):
            return []
        changed = False
        terminalized: list[RuntimeDependencyJob] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                job_id = str(item["job_id"])
                existing = self.jobs.get(job_id)
                if existing is not None and existing.project_id == project_id:
                    continue
                payload = dict(item.get("payload") or {})
                # Restore resolution fields from top-level (written by _persist_project_jobs_locked)
                for key in ("resolution_status", "resolved_at", "resolved_by_session_id", "resolution_message"):
                    if key in item and item[key] is not None and key not in payload:
                        payload[key] = item[key]
                job = RuntimeDependencyJob(
                    job_id=job_id,
                    project_id=str(item.get("project_id") or project_id),
                    payload=payload,
                    task_type=str(item.get("task_type") or "runtime_dependency_install"),
                    status=str(item.get("status") or "failed"),
                    task_id=str(item.get("task_id") or f"bgtask_{job_id}"),
                    phase=str(item.get("phase") or item.get("status") or "failed"),
                    status_detail=item.get("status_detail") if item.get("status_detail") is not None else None,
                    changed=item.get("changed") if item.get("changed") is not None else None,
                    command_preview=list(item["command_preview"]) if isinstance(item.get("command_preview"), list) else None,
                    child_pid=int(item["child_pid"]) if isinstance(item.get("child_pid"), int) else None,
                    log_path=str(item["log_path"]) if item.get("log_path") is not None else None,
                    last_heartbeat_at=str(item["last_heartbeat_at"]) if item.get("last_heartbeat_at") is not None else None,
                    last_stdout_at=str(item["last_stdout_at"]) if item.get("last_stdout_at") is not None else None,
                    last_stderr_at=str(item["last_stderr_at"]) if item.get("last_stderr_at") is not None else None,
                    stdout_tail=str(item.get("stdout_tail") or ""),
                    stderr_tail=str(item.get("stderr_tail") or ""),
                    progress=int(item["progress"]) if isinstance(item.get("progress"), int) else 0,
                    progress_label=str(item["progress_label"]) if item.get("progress_label") is not None else None,
                    bytes_total=int(item["bytes_total"]) if isinstance(item.get("bytes_total"), int) else None,
                    bytes_downloaded=int(item["bytes_downloaded"]) if isinstance(item.get("bytes_downloaded"), int) else None,
                    download_rate_bps=int(item["download_rate_bps"]) if isinstance(item.get("download_rate_bps"), int) else None,
                    result=dict(item["result"]) if isinstance(item.get("result"), dict) else None,
                    error=str(item["error"]) if item.get("error") is not None else None,
                    created_at=str(item.get("created_at") or now),
                    started_at=str(item["started_at"]) if item.get("started_at") is not None else None,
                    finished_at=str(item["finished_at"]) if item.get("finished_at") is not None else None,
                )
            except (KeyError, TypeError, ValueError):
                continue
            if job.status in {"queued", "running", "waiting", "launching"}:
                job.status = "failed"
                job.phase = "failed"
                job.error = "Runtime dependency job was interrupted by backend restart."
                job.finished_at = job.finished_at or now
                changed = True
                terminalized.append(job)
                self.background_task_service.update_task(
                    job.project_id,
                    job.task_id,
                    status="interrupted",
                    finished_at=job.finished_at,
                    error=job.error,
                )
            self.jobs[job.job_id] = job
        if changed:
            self._persist_project_jobs_locked(project_id)
        return terminalized

    def reconcile_orphaned_active_jobs(self) -> list[str]:
        """Mark persisted active dependency jobs without a live Future as interrupted."""
        terminalized: list[RuntimeDependencyJob] = []
        now = utc_now()
        with self.lock:
            for project in self.project_service.list_projects():
                project_id = project.project_id
                before = len(terminalized)
                terminalized.extend(self._load_project_jobs_locked(project_id))
                for job in list(self.jobs.values()):
                    if job.project_id != project_id:
                        continue
                    if job.status not in {"queued", "running", "waiting", "launching"}:
                        continue
                    if job.future is not None:
                        continue
                    job.status = "failed"
                    job.phase = "failed"
                    job.error = "Runtime dependency job was interrupted by backend restart."
                    job.finished_at = job.finished_at or now
                    terminalized.append(job)
                    self.background_task_service.update_task(
                        job.project_id,
                        job.task_id,
                        status="interrupted",
                        finished_at=job.finished_at,
                        error=job.error,
                        adapter={"kind": "dependency_installer", "metadata": {"phase": job.phase, "job_id": job.job_id}},
                    )
                if len(terminalized) > before:
                    self._persist_project_jobs_locked(project_id)
        seen: set[str] = set()
        terminalized_ids: list[str] = []
        for job in terminalized:
            if job.job_id in seen:
                continue
            seen.add(job.job_id)
            terminalized_ids.append(job.job_id)
            self._publish_terminal_chat_receipt(job)
        return terminalized_ids

    def _persist_project_jobs_locked(self, project_id: str) -> None:
        project_jobs = [
            job
            for job in self.jobs.values()
            if job.project_id == project_id
        ]
        project_jobs.sort(key=lambda item: item.created_at)
        atomic_write_json(
            self._jobs_path(project_id),
            [
                {
                    "job_id": job.job_id,
                    "project_id": job.project_id,
                    "task_id": job.task_id,
                    "task_type": job.task_type,
                    "payload": job.payload,
                    "status": job.status,
                    "phase": job.phase,
                    "result": job.result,
                    "error": job.error,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "status_detail": job.status_detail,
                    "changed": job.changed,
                    "command_preview": job.command_preview,
                    "child_pid": job.child_pid,
                    "log_path": job.log_path,
                    "last_heartbeat_at": job.last_heartbeat_at,
                    "last_stdout_at": job.last_stdout_at,
                    "last_stderr_at": job.last_stderr_at,
                    "stdout_tail": job.stdout_tail,
                    "stderr_tail": job.stderr_tail,
                    "progress": job.progress,
                    "progress_label": job.progress_label,
                    "bytes_total": job.bytes_total,
                    "bytes_downloaded": job.bytes_downloaded,
                    "download_rate_bps": job.download_rate_bps,
                    "resolution_status": job.payload.get("resolution_status"),
                    "resolved_at": job.payload.get("resolved_at"),
                    "resolved_by_session_id": job.payload.get("resolved_by_session_id"),
                    "resolution_message": job.payload.get("resolution_message"),
                }
                for job in project_jobs
            ],
        )

    def _notify_background_terminal(self, project_id: str, *, job_id: str | None = None) -> None:
        if self.background_terminal_callback is None:
            return
        try:
            self.background_terminal_callback(project_id, None, job_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                logger.debug(
                    "Skipping background terminal notification for missing project=%s job=%s",
                    project_id,
                    job_id,
                )
                return
            logger.exception(
                "Failed to notify background terminal state for project=%s job=%s",
                project_id,
                job_id,
            )
        except Exception:
            logger.exception(
                "Failed to notify background terminal state for project=%s job=%s",
                project_id,
                job_id,
            )

    def _watchdog_loop(self) -> None:
        """Periodic reconciliation for active dependency jobs."""
        while not self._watchdog_stop.is_set():
            self._watchdog_stop.wait(self._watchdog_interval_seconds)
            if self._watchdog_stop.is_set():
                break
            try:
                self._reconcile_active_jobs()
            except Exception:
                logger.exception("Runtime dependency watchdog reconciliation failed")

    def _reconcile_active_jobs(self) -> None:
        """Check active jobs for future-done and terminal drift.

        Pre-launch stale kill and child-pid checks are disabled until we
        switch from subprocess.run to Popen with real child pid tracking.
        """
        terminalized: list[RuntimeDependencyJob] = []
        with self.lock:
            # Pass 1: future-done reconciliation for active jobs
            active_jobs = [
                job
                for job in self.jobs.values()
                if job.status in {"queued", "running"} and job.phase not in {"succeeded", "failed", "interrupted"}
            ]
            for job in active_jobs:
                try:
                    terminal_job = self._reconcile_single_job(job)
                    if terminal_job is not None:
                        terminalized.append(terminal_job)
                except Exception:
                    logger.exception("Reconciliation failed for job %s", job.job_id)
            # Pass 2: terminal drift self-heal — jobs whose in-memory state is
            # terminal but persisted JSON may still show non-terminal (e.g. after
            # a persist exception in _run).
            for job in list(self.jobs.values()):
                if job.status in {"succeeded", "failed"} and job.phase in {"succeeded", "failed"}:
                    # Already in terminalized from pass 1? Skip.
                    if any(t.job_id == job.job_id for t in terminalized):
                        continue
                    try:
                        if self._self_heal_terminal_drift_locked(job):
                            terminalized.append(job)
                    except Exception:
                        logger.exception("Terminal drift self-heal failed for job %s", job.job_id)
        for job in terminalized:
            self._publish_terminal_chat_receipt(job)

    def _reconcile_single_job(self, job: RuntimeDependencyJob) -> RuntimeDependencyJob | None:
        """Reconcile one active job. Must be called with self.lock held.

        Currently only finalizes jobs whose future has completed but whose
        status was not yet updated (e.g. after a backend restart).

        Returns the job if it was terminalized, otherwise None.
        Publishing must happen outside the lock by the caller.
        """
        if job.future is not None and job.future.done():
            try:
                job.future.result(timeout=0)
            except Exception as exc:
                job.status = "failed"
                job.phase = "failed"
                job.error = "".join(traceback.format_exception(exc))
            else:
                job.status = "succeeded"
                job.phase = "succeeded"
            job.finished_at = utc_now()
            self.background_task_service.update_task(
                job.project_id,
                job.task_id,
                status=job.status,
                finished_at=job.finished_at,
                error=job.error,
                adapter={"kind": "dependency_installer", "metadata": {"phase": job.phase, "job_id": job.job_id}},
            )
            self._persist_project_jobs_locked(job.project_id)
            return job
        # Terminal drift self-heal: memory is terminal but persisted JSON may still
        # show queued/running/waiting/launching because a previous persist failed.
        if job.status in {"succeeded", "failed"} and job.phase in {"succeeded", "failed"}:
            healed = self._self_heal_terminal_drift_locked(job)
            if healed:
                return job
        return None

    def _self_heal_terminal_drift_locked(self, job: RuntimeDependencyJob) -> bool:
        """Detect and repair terminal drift for a single job.

        Must be called with self.lock held.

        When the in-memory job is terminal (succeeded/failed) but the persisted
        JSON still shows a non-terminal status, overwrite the persisted record
        and signal that publish is needed.

        Returns True if drift was detected and healed (publish should follow).
        """
        # Only relevant for terminal jobs
        if job.status not in {"succeeded", "failed"}:
            return False
        if job.phase not in {"succeeded", "failed"}:
            return False

        # Check persisted state
        items = read_json(self._jobs_path(job.project_id), [])
        if not isinstance(items, list):
            return False
        disk_stale = False
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("job_id")) != job.job_id:
                continue
            disk_status = str(item.get("status") or "")
            if disk_status in {"queued", "running", "waiting", "launching"}:
                disk_stale = True
            break

        if not disk_stale:
            return False

        logger.warning(
            "Terminal drift detected: project=%s job=%s memory_status=%s disk_status=stale — overwriting persisted record",
            job.project_id,
            job.job_id,
            job.status,
        )
        try:
            self._persist_project_jobs_locked(job.project_id)
        except Exception:
            logger.exception(
                "Failed to self-heal persisted dependency job: project=%s job=%s — terminal receipt skipped to avoid chat/disk split",
                job.project_id,
                job.job_id,
            )
            return False
        return True

    def mark_job_resolved(
        self,
        project_id: str,
        job_id: str,
        session_id: str,
        resolution_message: str,
    ) -> dict[str, Any]:
        """Mark a runtime dependency job as manually resolved.

        Adds resolution metadata to the persisted job record and emits a project event.
        Does not create a new job; updates the existing one in-place.
        """
        with self.lock:
            self._load_project_jobs_locked(project_id)
            job = self.jobs.get(job_id)
            if job is None or job.project_id != project_id:
                raise HTTPException(status_code=404, detail="Runtime dependency job not found")
            now = utc_now()
            job.payload["resolution_status"] = "manually_resolved"
            job.payload["resolved_at"] = now
            job.payload["resolved_by_session_id"] = session_id
            job.payload["resolution_message"] = resolution_message
            self._persist_project_jobs_locked(project_id)
            self._emit_project_event(job)
        return {
            "job_id": job.job_id,
            "status": job.status,
            "resolution_status": "manually_resolved",
            "resolved_at": now,
            "resolved_by_session_id": session_id,
            "resolution_message": resolution_message,
        }
