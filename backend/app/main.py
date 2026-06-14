import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import advanced, app_settings, card_library, chat, chat_sessions, diagnostics, executor_profiles, files, library, manager_auto, manager_tools, project_events, projects, report, results, runs, workspace_roots
from app.api.deps import get_app_config_service, get_manager_auto_service, get_project_file_service, get_project_service, get_runtime_dependency_job_service, get_worker_service, inject_wake_dispatch
from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


def initialize_runtime_services() -> None:
    get_app_config_service()
    get_worker_service()
    # Doc 42: Initialize ManagerAutoService and inject wake dispatch.
    # inject_wake_dispatch must be called after all services are constructed
    # to avoid circular dependency through ChatSessionService.
    get_manager_auto_service()
    inject_wake_dispatch()
    get_manager_auto_service().reconcile_stale_wake_in_flight()
    get_runtime_dependency_job_service().reconcile_orphaned_active_jobs()
    get_worker_service().flush_reconciled_run_notifications()

    project_service = get_project_service()
    project_file_service = get_project_file_service()
    for summary in project_service.list_projects():
        try:
            result = project_file_service.reconcile_project_uploads(summary.project_id)
            if result["removed"]:
                logger.info(
                    "reconcile_project_uploads: project=%s removed=%d errors=%d",
                    summary.project_id,
                    len(result["removed"]),
                    result["errors"],
                )
        except Exception:
            logger.exception("reconcile_project_uploads failed: project=%s", summary.project_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_runtime_services()
    try:
        yield
    finally:
        pass


app = FastAPI(title=settings.app_name, lifespan=lifespan)
allowed_frontend_origins = list(
    dict.fromkeys(
        [
            settings.frontend_origin,
            "http://127.0.0.1:13001",
            "http://localhost:13001",
        ]
    )
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router, prefix=settings.api_prefix)
app.include_router(workspace_roots.router, prefix=settings.api_prefix)
app.include_router(app_settings.router, prefix=settings.api_prefix)
app.include_router(library.router, prefix=settings.api_prefix)
app.include_router(card_library.router, prefix=settings.api_prefix)
app.include_router(card_library.project_router, prefix=settings.api_prefix)
app.include_router(chat.router, prefix=settings.api_prefix)
app.include_router(manager_auto.router, prefix=settings.api_prefix)
app.include_router(manager_tools.router, prefix=settings.api_prefix)
app.include_router(diagnostics.router, prefix=settings.api_prefix)
app.include_router(results.router, prefix=settings.api_prefix)
app.include_router(report.router, prefix=settings.api_prefix)
app.include_router(runs.router, prefix=settings.api_prefix)
app.include_router(advanced.router, prefix=settings.api_prefix)
app.include_router(files.router, prefix=settings.api_prefix)
app.include_router(chat_sessions.router, prefix=settings.api_prefix)
app.include_router(project_events.router, prefix=settings.api_prefix)
app.include_router(executor_profiles.router, prefix=settings.api_prefix)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
