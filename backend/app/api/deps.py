from functools import lru_cache

from app.core.config import get_settings
from app.services.chat_session_service import ChatSessionService
from app.services.app_config_service import AppConfigService
from app.services.background_task_service import BackgroundTaskService
from app.services.background_workboard_service import BackgroundWorkboardService
from app.services.diagnostic_bundle_service import DiagnosticBundleService
from app.services.manager_service import ManagerService
from app.services.chat_job_service import ChatJobService
from app.services.flow_service import FlowService
from app.services.library_registry_service import LibraryRegistryService
from app.services.manager_auto_service import ManagerAutoService
from app.services.manifest_service import ManifestService
from app.services.package_service import PackageService
from app.services.patch_apply import PatchApplyService
from app.services.patch_validator import PatchValidator
from app.services.project_file_service import ProjectFileService
from app.services.project_event_service import ProjectEventService
from app.services.project_service import ProjectService
from app.services.result_asset_service import ResultAssetService
from app.services.report_service import ReportService
from app.services.runtime_dependency_job_service import RuntimeDependencyJobService
from app.services.runtime_dependency_resolver_service import RuntimeDependencyResolverService
from app.services.runtime_approval_service import RuntimeApprovalService
from app.services.card_library_service import CardLibraryService
from app.services.worker_service import WorkerService


@lru_cache
def get_project_service() -> ProjectService:
    service = ProjectService()
    service.ensure_seed_project()
    return service


@lru_cache
def get_patch_validator() -> PatchValidator:
    return PatchValidator(get_project_service())


@lru_cache
def get_patch_apply_service() -> PatchApplyService:
    return PatchApplyService(get_project_service(), get_patch_validator())


@lru_cache
def get_manager_service() -> ManagerService:
    return ManagerService(
        get_project_service(),
        worker_service=get_worker_service(),
        runtime_dependency_job_service=get_runtime_dependency_job_service(),
        runtime_dependency_resolver_service=get_runtime_dependency_resolver_service(),
        library_registry_service=get_library_registry_service(),
        manager_auto_service=get_manager_auto_service(),
        background_workboard_service=get_background_workboard_service(),
        package_service=get_package_service(),
    )


@lru_cache
def get_chat_job_service() -> ChatJobService:
    return ChatJobService()


@lru_cache
def get_runtime_dependency_job_service() -> RuntimeDependencyJobService:
    return RuntimeDependencyJobService(
        get_project_service(),
        project_event_service=get_project_event_service(),
        background_task_service=get_background_task_service(),
        background_terminal_callback=lambda project_id, run_id=None, job_id=None: get_manager_auto_service().notify_background_task_terminal(
            project_id,
            run_id=run_id,
            job_id=job_id,
        ),
        chat_session_service=get_chat_session_service(),
    )


@lru_cache
def get_runtime_dependency_resolver_service() -> RuntimeDependencyResolverService:
    settings = get_settings()
    return RuntimeDependencyResolverService(
        probe_timeout_seconds=int(getattr(settings, "runtime_dependency_probe_timeout_seconds", 60) or 60),
        cache_ttl_seconds=int(getattr(settings, "runtime_dependency_cache_ttl_seconds", 3600) or 3600),
    )


@lru_cache
def get_chat_session_service() -> ChatSessionService:
    return ChatSessionService(get_project_service(), get_manager_auto_service())


@lru_cache
def get_project_event_service() -> ProjectEventService:
    return ProjectEventService(get_project_service())


@lru_cache
def get_runtime_approval_service() -> RuntimeApprovalService:
    return RuntimeApprovalService(get_project_service())


@lru_cache
def get_worker_service() -> WorkerService:
    return WorkerService(
        get_project_service(),
        get_manifest_service(),
        get_runtime_approval_service(),
        get_library_registry_service(),
        project_event_service=get_project_event_service(),
        background_task_service=get_background_task_service(),
        background_terminal_callback=lambda project_id, run_id=None, job_id=None: get_manager_auto_service().notify_background_task_terminal(
            project_id,
            run_id=run_id,
            job_id=job_id,
        ),
    )


@lru_cache
def get_manifest_service() -> ManifestService:
    return ManifestService(get_project_service())


@lru_cache
def get_report_service() -> ReportService:
    return ReportService(get_project_service())


@lru_cache
def get_result_asset_service() -> ResultAssetService:
    return ResultAssetService(get_project_service())


@lru_cache
def get_project_file_service() -> ProjectFileService:
    return ProjectFileService(get_project_service())


@lru_cache
def get_flow_service() -> FlowService:
    return FlowService(get_project_service())


@lru_cache
def get_app_config_service() -> AppConfigService:
    return AppConfigService()


@lru_cache
def get_library_registry_service() -> LibraryRegistryService:
    return LibraryRegistryService(
        get_project_service(),
        get_app_config_service(),
    )


@lru_cache
def get_package_service() -> PackageService:
    return PackageService(
        get_library_registry_service(),
        get_project_service(),
        runtime_dependency_resolver=get_runtime_dependency_resolver_service(),
    )


@lru_cache
def get_manager_auto_service() -> ManagerAutoService:
    return ManagerAutoService(
        get_project_service(),
        get_project_event_service(),
        get_background_workboard_service(),
    )


def inject_wake_dispatch() -> None:
    """Doc 42: Inject chat_stream_relay into ManagerAutoService for transient wake dispatch
    and wire fuel_change_callback so fuel_revision stays in sync with actual mutations.

    Must be called AFTER all services are constructed (not inside lru_cache factory)
    to avoid circular dependency: ManagerAutoService -> ChatStreamRelay ->
    ChatSessionService -> ManagerAutoService.
    """
    from app.services.chat_stream_relay import ChatStreamRelay
    auto_svc = get_manager_auto_service()
    if auto_svc._chat_stream_relay is None:
        auto_svc.set_chat_stream_relay(ChatStreamRelay(get_chat_session_service(), get_manager_service()))
    # Wire fuel_revision bump to all fuel mutation paths (Fix 1)
    wb = get_background_workboard_service()
    wb.set_fuel_change_callback(lambda pid: auto_svc.increment_fuel_revision(pid))


@lru_cache
def get_background_task_service() -> BackgroundTaskService:
    return BackgroundTaskService(get_project_service())


@lru_cache
def get_background_workboard_service() -> BackgroundWorkboardService:
    return BackgroundWorkboardService(get_project_service(), get_background_task_service())


@lru_cache
def get_card_library_service() -> CardLibraryService:
    return CardLibraryService(
        get_project_service(),
        library_registry_service=get_library_registry_service(),
        runtime_dependency_resolver_service=get_runtime_dependency_resolver_service(),
    )


@lru_cache
def get_diagnostic_bundle_service() -> DiagnosticBundleService:
    return DiagnosticBundleService(
        get_project_service(),
        get_app_config_service(),
    )


@lru_cache
def get_manager_command_service():
    from app.services.manager_command_service import ManagerCommandService
    return ManagerCommandService(
        manager_auto_service=get_manager_auto_service(),
        chat_session_service=get_chat_session_service(),
    )
