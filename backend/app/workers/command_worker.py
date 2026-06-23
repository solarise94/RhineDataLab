from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from app.core.config import default_conda_base, default_conda_base_candidates, find_conda_solver
from app.models.runs import TaskPacket
from app.workers.base import PermissionRequest, WorkerAdapter, WorkerLaunchSpec
from app.workers.sandbox import resolve_renderer, settings_sandbox_mode
from app.workers.sandbox._utils import (
    resolve_project_path,
    resolve_rscript_runtime,
    r_user_library_paths,
)


class CommandTemplateWorkerAdapter(WorkerAdapter):
    command_template: str | None = None
    requires_configuration: bool = False
    declares_network_access: bool = False
    supports_sandbox: bool = True

    def build_launch_spec(
        self,
        *,
        packet: TaskPacket,
        packet_path: Path,
        run_dir: Path,
        project_root: Path,
        settings: object,
    ) -> WorkerLaunchSpec:
        if not self.is_configured(settings):
            raise RuntimeError(f"Worker adapter {self.name} is not configured.")
        run_dir.mkdir(parents=True, exist_ok=True)
        is_workspace_write = packet.execution_policy.mode == "workspace_write"
        work_dir = project_root / "work"
        if is_workspace_write:
            work_dir.mkdir(parents=True, exist_ok=True)
        self._validate_executor_policy(packet)
        library_paths = self._write_library_bindings(packet=packet, run_dir=run_dir)
        contract_paths = self._write_contract_files(packet=packet, run_dir=run_dir, project_root=project_root, library_paths=library_paths)
        r_profile_path = self._write_runtime_r_profile(run_dir)
        mapping = {
            "task_packet_path": str(packet_path),
            "run_dir": str(run_dir),
            "project_root": str(project_root),
            "result_dir": str(resolve_project_path(project_root, packet.run_context.result_dir)),
            "manifest_path": str(run_dir / "manifest.json"),
            "manifest_candidate_path": str(run_dir / "manifest.candidate.json"),
            "transcript_path": str(run_dir / "transcript.md"),
            "executor_brief_path": str(contract_paths["executor_brief_path"]),
            "executor_prompt_path": str(contract_paths["executor_prompt_path"]),
            "adapter_contract_path": str(contract_paths["adapter_contract_path"]),
            "manager_brief_path": str(run_dir / "manager_brief.json"),
            "executor_result_tool_path": str(contract_paths["executor_result_tool_path"]),
            "worker_type": self.name,
            "python": sys.executable,
            "repo_root": str(Path(__file__).resolve().parents[2].parent),
        }
        argv_template = self.resolve_command_argv_template(settings)
        if argv_template:
            command = self._render_argv_template(argv_template, mapping)
        else:
            template = self.resolve_command_template(settings)
            if not template:
                raise RuntimeError(f"Worker adapter {self.name} is not configured.")
            # Quote values to protect paths with spaces in the legacy string template
            quoted_mapping = {k: shlex.quote(v) for k, v in mapping.items()}
            command = shlex.split(template.format(**quoted_mapping))
        backend_root = Path(__file__).resolve().parents[2]
        pythonpath = os.environ.get("PYTHONPATH", "")
        merged_pythonpath = str(backend_root) if not pythonpath else f"{backend_root}{os.pathsep}{pythonpath}"
        runtime_env = packet.executor_context.runtime_bindings.env if packet.executor_context else {}
        conda_env = packet.executor_context.runtime_bindings.conda_env if packet.executor_context else None
        r_env = packet.executor_context.runtime_bindings.r_env if packet.executor_context else None
        command, python_extra_env = self._apply_conda_runtime(command, conda_env=conda_env, settings=settings)
        r_extra_env = self._apply_r_runtime(r_env, settings=settings, base_path=python_extra_env.get("PATH"))
        extra_env = {**python_extra_env, **r_extra_env}
        adapter_extra_env = self.extra_environment(packet=packet, settings=settings)
        effective_cwd = work_dir if is_workspace_write else run_dir
        effective_working_dir = str(effective_cwd)
        environment = {
            **os.environ,
            **runtime_env,
            **extra_env,
            "BLUEPRINT_PROJECT_ROOT": str(project_root),
            "BLUEPRINT_TASK_ID": packet.task_id,
            "BLUEPRINT_RUN_ID": packet.task_id,
            "BLUEPRINT_CARD_ID": packet.card_id,
            "BLUEPRINT_RUN_DIR": str(run_dir),
            "BLUEPRINT_RESULT_DIR": str(resolve_project_path(project_root, packet.run_context.result_dir)) if packet.run_context else str(run_dir),
            "BLUEPRINT_TASK_PACKET": str(packet_path),
            "BLUEPRINT_MANIFEST_PATH": str(run_dir / "manifest.json"),
            "BLUEPRINT_MANIFEST_CANDIDATE_PATH": str(run_dir / "manifest.candidate.json"),
            "BLUEPRINT_TRANSCRIPT_PATH": str(run_dir / "transcript.md"),
            "BLUEPRINT_EXECUTOR_BRIEF": str(contract_paths["executor_brief_path"]),
            "BLUEPRINT_EXECUTOR_PROMPT": str(contract_paths["executor_prompt_path"]),
            "BLUEPRINT_ADAPTER_CONTRACT": str(contract_paths["adapter_contract_path"]),
            "BLUEPRINT_MANAGER_BRIEF": str(run_dir / "manager_brief.json"),
            "BLUEPRINT_EXECUTOR_RESULT_TOOL": str(contract_paths["executor_result_tool_path"]),
            "BLUEPRINT_EXECUTOR_COMPLETION_PATH": str(run_dir / "executor_completion.json"),
            "BLUEPRINT_EXECUTOR_FAILURE_PATH": str(run_dir / "executor_failure.json"),
            "BLUEPRINT_TERMINAL_REPORT_PATH": str(run_dir / "terminal_report.json"),
            "BLUEPRINT_EXECUTOR_RESULT_STATE_PATH": str(run_dir / "executor_result_state.json"),
            "BLUEPRINT_DEPENDENCY_ISSUE_PATH": str(run_dir / "dependency_issue.json"),
            "BLUEPRINT_DEPENDENCY_REPORT_TOOL": str(contract_paths["dependency_report_tool_path"]),
            "BLUEPRINT_ALLOWED_PATHS": json.dumps(packet.allowed_paths),
            "BLUEPRINT_READONLY_PATHS": json.dumps(packet.readonly_paths),
            "BLUEPRINT_REFERENCE_PATHS": json.dumps(
                packet.executor_context.template_metadata.get("reference_paths", {})
                if packet.executor_context else {}
            ),
            "BLUEPRINT_FORBIDDEN_PATHS": json.dumps(packet.forbidden_paths),
            "BLUEPRINT_WORKER_TYPE": self.name,
            "BLUEPRINT_EXECUTOR_PROFILE": (
                packet.executor_context.executor_profile if packet.executor_context and packet.executor_context.executor_profile else ""
            ),
            "BLUEPRINT_EXECUTOR_PROFILE_ID": (
                packet.executor_context.executor_profile_id if packet.executor_context and packet.executor_context.executor_profile_id else ""
            ),
            "BLUEPRINT_EXECUTOR_SKILLS": json.dumps(packet.executor_context.skills if packet.executor_context else []),
            "BLUEPRINT_EXECUTOR_MCP_SERVERS": json.dumps(packet.executor_context.mcp_servers if packet.executor_context else []),
            "BLUEPRINT_EXECUTOR_SKILL_BINDINGS": str(library_paths["skill_bindings_path"]),
            "BLUEPRINT_EXECUTOR_MCP_BINDINGS": str(library_paths["mcp_bindings_path"]),
            "BLUEPRINT_EXECUTOR_MCP_CONFIG": str(library_paths["mcp_config_path"]),
            "BLUEPRINT_PI_SKILL_PATHS": json.dumps(library_paths["skill_paths"]),
            "BLUEPRINT_USER_WORKSPACE": str(work_dir),
            "BLUEPRINT_RUNTIME_WORKING_DIR": effective_working_dir,
            "BLUEPRINT_MANAGER_REPORT_STDOUT_PREFIX": (
                packet.manager_reporting_contract.stdout_prefix if packet.manager_reporting_contract else "BP_EVENT "
            ),
            "R_PROFILE_USER": str(r_profile_path),
            "R_DEFAULT_DEVICE": "pdf",
            "PYTHONPATH": merged_pythonpath,
        }
        environment.update(adapter_extra_env)
        renderer = resolve_renderer(settings_sandbox_mode(settings))
        sandboxed = renderer.should_sandbox()
        if not sandboxed and packet.mounted_data_directory:
            raise RuntimeError(
                "Runs with data_mount/... input assets require a sandboxed executor. "
                f"Set BLUEPRINT_EXECUTOR_SANDBOX_MODE to one of bwrap, container, or seatbelt (current: {renderer.mode})."
            )
        command, _sandbox_plan = renderer.render(
            command=command,
            packet=packet,
            project_root=project_root,
            run_dir=run_dir,
            environment=environment,
            adapter_extra_env_keys=set(adapter_extra_env),
            settings=settings,
        )
        return WorkerLaunchSpec(
            command=command,
            cwd=effective_cwd,
            environment=environment,
            permission_requests=self._build_permission_requests(packet),
            sandboxed=sandboxed,
        )

    def extra_environment(self, *, packet: TaskPacket, settings: object) -> dict[str, str]:
        return {}

    def uses_sandbox(self, settings: object) -> bool:
        if not self.supports_sandbox:
            return False
        return resolve_renderer(settings_sandbox_mode(settings)).should_sandbox()

    @staticmethod
    def _resolve_project_path(project_root: Path, value: str) -> Path:
        return resolve_project_path(project_root, value)

    @staticmethod
    def _resolve_rscript_runtime(r_env: str | None, settings: object) -> Path | None:
        return resolve_rscript_runtime(r_env, settings)

    @staticmethod
    def _apply_conda_runtime(command: list[str], *, conda_env: str | None, settings: object) -> tuple[list[str], dict[str, str]]:
        if not conda_env:
            return command, {}
        conda_base, env_path = CommandTemplateWorkerAdapter._resolve_conda_runtime(conda_env, settings)
        if not env_path.exists():
            return command, {
                "PATH": f"{env_path / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
                "CONDA_PREFIX": str(env_path),
                "CONDA_DEFAULT_ENV": conda_env,
            }
        solver = find_conda_solver(conda_base)
        if solver is not None:
            solver_name = solver.name
            if solver_name == "micromamba":
                if CommandTemplateWorkerAdapter._micromamba_run_supports_empty_attach(solver):
                    return [str(solver), "run", "-p", str(env_path), "-a", "", *command], {}
            elif solver_name in ("conda", "mamba"):
                return [str(solver), "run", "-p", str(env_path), "--no-capture-output", *command], {}
        return command, {
            "PATH": f"{env_path / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
            "CONDA_PREFIX": str(env_path),
            "CONDA_DEFAULT_ENV": conda_env,
        }

    @staticmethod
    @lru_cache(maxsize=None)
    def _micromamba_run_supports_empty_attach(micromamba_bin: Path) -> bool:
        """Check whether the bundled micromamba supports ``run -a ""`` passthrough."""
        try:
            result = subprocess.run(
                [str(micromamba_bin), "run", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            return False
        return bool(re.search(r"(^|\s)(-a|--attach)\b", result.stdout))

    @staticmethod
    def _resolve_conda_runtime(conda_env: str, settings: object) -> tuple[Path, Path]:
        configured_base = Path(getattr(settings, "executor_conda_base", default_conda_base()))
        candidates = default_conda_base_candidates(configured_base)
        if conda_env.startswith("/"):
            env_path = Path(conda_env)
            return env_path.parent.parent if env_path.parent.name == "envs" else configured_base, env_path
        for base in candidates:
            env_path = base / "envs" / conda_env
            if env_path.exists():
                return base, env_path
        return configured_base, configured_base / "envs" / conda_env

    @staticmethod
    def _apply_r_runtime(r_env: str | None, *, settings: object, base_path: str | None = None) -> dict[str, str]:
        rscript_path = resolve_rscript_runtime(r_env, settings)
        if rscript_path is None:
            return {}
        env: dict[str, str] = {
            "BLUEPRINT_RSCRIPT": str(rscript_path),
        }
        if r_env:
            env["BLUEPRINT_R_RUNTIME"] = r_env
            env["PATH"] = f"{rscript_path.parent}{os.pathsep}{base_path or os.environ.get('PATH', '')}"
        r_user_libs = r_user_library_paths(rscript_path)
        if r_user_libs:
            env["R_LIBS_USER"] = os.pathsep.join(str(path) for path in r_user_libs)
        return env

    def resolve_command_template(self, settings: object) -> str | None:
        if self.command_template:
            return self.command_template
        return None

    def resolve_command_argv_template(self, settings: object) -> list[str] | None:
        """Return a structured argv template (list of strings with placeholders).

        When provided, this is preferred over resolve_command_template() to avoid
        shlex.split issues with paths containing spaces.
        Subclasses may override to provide a structured argv list.
        """
        return None

    @staticmethod
    def _render_argv_template(template: list[str], mapping: dict[str, str]) -> list[str]:
        """Render each argv element by substituting placeholders."""
        try:
            return [item.format(**mapping) for item in template]
        except KeyError as exc:
            missing = exc.args[0]
            raise RuntimeError(f"Command argv template referenced unknown placeholder {{{missing}}}.") from exc

    def is_configured(self, settings: object) -> bool:
        return bool(self.resolve_command_template(settings) or self.resolve_command_argv_template(settings))

    def capability_metadata(self, settings: object) -> dict[str, object]:
        metadata = super().capability_metadata(settings)
        metadata["execution_mode"] = "command_template"
        return metadata

    def _build_permission_requests(self, packet: TaskPacket) -> list[PermissionRequest]:
        requests = [
            PermissionRequest(
                request_id=f"perm_{packet.task_id}_write_results",
                target=f"results/{packet.card_id}/{packet.task_id}/",
                action="write",
                reason="Worker needs to write outputs under the declared result directory.",
            ),
            PermissionRequest(
                request_id=f"perm_{packet.task_id}_write_run_dir",
                target=f"runs/{packet.task_id}/",
                action="write",
                reason="Worker needs to write transcript, logs, and manifest for the current run.",
            ),
            PermissionRequest(
                request_id=f"perm_{packet.task_id}_write_generated_scripts",
                target=f"scripts/generated/{packet.task_id}/",
                action="write",
                reason="Worker may generate reusable helper scripts under the current run's scripts/generated directory.",
            ),
        ]
        network_policy = packet.executor_context.tool_policy.network if packet.executor_context else "prompt"
        if self.declares_network_access and network_policy == "prompt":
            requests.append(
                PermissionRequest(
                    request_id=f"perm_{packet.task_id}_network",
                    target="network",
                    action="network",
                    reason="Worker requested conditional network access under the executor tool policy.",
                )
            )
        return requests

    @staticmethod
    def _write_runtime_r_profile(run_dir: Path) -> Path:
        path = run_dir / ".Rprofile"
        path.write_text(
            "options(device = function(...) grDevices::pdf(file = file.path(Sys.getenv('BLUEPRINT_RUN_DIR', '.'), 'Rplots.pdf'), ...))\n",
            encoding="utf-8",
        )
        return path

    def _validate_executor_policy(self, packet: TaskPacket) -> None:
        network_policy = packet.executor_context.tool_policy.network if packet.executor_context else "prompt"
        if self.declares_network_access and network_policy == "deny":
            raise RuntimeError(
                f"Worker adapter {self.name} requires model/network access, but executor_context.tool_policy.network=deny."
            )

    def _write_contract_files(
        self,
        *,
        packet: TaskPacket,
        run_dir: Path,
        project_root: Path,
        library_paths: dict[str, object],
    ) -> dict[str, Path]:
        executor_brief_path = run_dir / "executor_brief.md"
        executor_prompt_path = run_dir / "executor_prompt.md"
        adapter_contract_path = run_dir / "adapter_contract.json"
        executor_result_tool_path = run_dir / "report_executor_result.py"
        dependency_report_tool_path = run_dir / "report_dependency_issue.py"
        self._write_executor_result_tool(executor_result_tool_path)
        self._write_dependency_report_tool(dependency_report_tool_path)
        executor_brief_path.write_text(self._render_executor_brief(packet), encoding="utf-8")
        executor_prompt_path.write_text(self._render_executor_prompt(packet), encoding="utf-8")
        adapter_contract_path.write_text(
            json.dumps(
                {
                    "worker_type": self.name,
                    "task_packet_path": str(run_dir / "task_packet.json"),
                    "executor_prompt_path": str(run_dir / "executor_prompt.md"),
                    "compatibility": {
                        "manifest_candidate_path": str(run_dir / "manifest.candidate.json"),
                        "dependency_issue_path": str(run_dir / "dependency_issue.json"),
                        "dependency_report_tool_path": str(dependency_report_tool_path),
                    },
                    "executor_completion_path": str(run_dir / "executor_completion.json"),
                    "executor_failure_path": str(run_dir / "executor_failure.json"),
                    "terminal_report_path": str(run_dir / "terminal_report.json"),
                    "executor_result_state_path": str(run_dir / "executor_result_state.json"),
                    "executor_validation_path": str(run_dir / "executor_validation.json"),
                    "executor_result_tool_path": str(executor_result_tool_path),
                    "skill_bindings_path": str(library_paths["skill_bindings_path"]),
                    "mcp_bindings_path": str(library_paths["mcp_bindings_path"]),
                    "mcp_config_path": str(library_paths["mcp_config_path"]),
                    "allowed_paths": packet.allowed_paths,
                    "readonly_paths": packet.readonly_paths,
                    "forbidden_paths": packet.forbidden_paths,
                    "declares_network_access": self.declares_network_access,
                    "template_fields": [
                        "python",
                        "project_root",
                        "run_dir",
                        "result_dir",
                        "task_packet_path",
                        "manifest_candidate_path",
                        "transcript_path",
                        "executor_brief_path",
                        "executor_prompt_path",
                        "adapter_contract_path",
                        "executor_result_tool_path",
                        "worker_type",
                    ],
                    "expected_outputs": [item.model_dump() for item in packet.expected_outputs],
                    "executor_tools": [
                        {
                            "name": "report_executor_result",
                            "path": str(executor_result_tool_path),
                            "purpose": "Submit the terminal completion or failure report for this executor run.",
                            "subcommands": {
                                "complete": (
                                    f"python {executor_result_tool_path} complete "
                                    f"--manifest {run_dir / 'manifest.candidate.json'}"
                                ),
                                "fail": (
                                    f"python {executor_result_tool_path} fail "
                                    f"--reason-code runtime_dependency_missing "
                                    f"--summary 'Required enrichment packages are unavailable.' "
                                    f"--details-json '{{\"ecosystem\":\"R\",\"missing_packages\":[\"clusterProfiler\"]}}'"
                                ),
                            },
                        }
                    ],
                    "manifest_schema": {
                        "schema_version": "executor_manifest.v2",
                        "summary": "string",
                        "created_assets": "array of {role,path,label?,asset_id?,description?,artifact_class?,format?}",
                        "code_artifacts": "array of {path,language?,purpose?,sha256?}",
                        "manager_report": {
                            "summary": "short summary shown to Manager before reviewer projection",
                            "warnings": "array of strings",
                        },
                    },
                    "code_artifact_scope": f"scripts/generated/{packet.task_id}/",
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "executor_brief_path": executor_brief_path,
            "executor_prompt_path": executor_prompt_path,
            "adapter_contract_path": adapter_contract_path,
            "executor_result_tool_path": executor_result_tool_path,
            "dependency_report_tool_path": dependency_report_tool_path,
        }

    def _write_library_bindings(self, *, packet: TaskPacket, run_dir: Path) -> dict[str, object]:
        library_root = run_dir / "library"
        skills_root = library_root / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        skill_bindings = list(packet.executor_context.template_metadata.get("library_skill_bindings") or []) if packet.executor_context else []
        mcp_bindings = list(packet.executor_context.template_metadata.get("library_mcp_bindings") or []) if packet.executor_context else []

        copied_skill_paths: list[str] = []
        for binding in skill_bindings:
            source_path_value = binding.get("source_path")
            skill_id = str(binding.get("id") or "skill")
            if not source_path_value:
                continue
            source_path = Path(str(source_path_value))
            if not source_path.exists():
                continue
            source_dir = source_path.parent if source_path.is_file() else source_path
            destination_dir = skills_root / skill_id
            if destination_dir.exists():
                shutil.rmtree(destination_dir)
            shutil.copytree(source_dir, destination_dir)
            copied_skill_paths.append(str(destination_dir))
            binding["run_path"] = str(destination_dir)

        mcp_config_payload = {"mcpServers": {}}
        for binding in mcp_bindings:
            config = binding.get("config")
            if isinstance(config, dict):
                servers = config.get("mcpServers")
                if isinstance(servers, dict):
                    mcp_config_payload["mcpServers"].update(servers)

        skill_bindings_path = library_root / "skill_bindings.json"
        mcp_bindings_path = library_root / "mcp_bindings.json"
        mcp_config_path = library_root / "mcp.json"
        skill_bindings_path.write_text(json.dumps(skill_bindings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        mcp_bindings_path.write_text(json.dumps(mcp_bindings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        mcp_config_path.write_text(json.dumps(mcp_config_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "skill_bindings_path": skill_bindings_path,
            "mcp_bindings_path": mcp_bindings_path,
            "mcp_config_path": mcp_config_path,
            "skill_paths": copied_skill_paths,
        }

    @staticmethod
    def _write_executor_result_tool(path: Path) -> None:
        path.write_text(
            '''from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from pydantic import ValidationError

from app.models.runs import (
    ExecutorCompletionReport,
    ExecutorFailureReport,
    ExecutorResultState,
    FailureReasonCode,
    Manifest,
    TaskPacket,
    TerminalReport,
)
from app.services.artifact_format_service import detect_artifact_class, detect_artifact_format
from app.services.manifest_service import ManifestService
from app.services.utils import atomic_write_json


REASON_CODES = {
    "runtime_dependency_missing",
    "input_missing",
    "input_invalid",
    "permission_denied",
    "tool_unavailable",
    "execution_error",
    "contract_violation",
    "unknown",
}
MAX_REPORT_COMPLETE_FAILURES = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _run_dir() -> Path:
    return Path(os.environ["BLUEPRINT_RUN_DIR"]).resolve()


def _project_root() -> Path:
    return Path(os.environ["BLUEPRINT_PROJECT_ROOT"]).resolve()


def _run_id() -> str:
    return str(os.environ.get("BLUEPRINT_RUN_ID") or os.environ.get("BLUEPRINT_TASK_ID") or "").strip()


def _paths() -> dict[str, Path]:
    run_dir = _run_dir()
    return {
        "completion": Path(os.environ.get("BLUEPRINT_EXECUTOR_COMPLETION_PATH", run_dir / "executor_completion.json")),
        "failure": Path(os.environ.get("BLUEPRINT_EXECUTOR_FAILURE_PATH", run_dir / "executor_failure.json")),
        "terminal": Path(os.environ.get("BLUEPRINT_TERMINAL_REPORT_PATH", run_dir / "terminal_report.json")),
        "state": Path(os.environ.get("BLUEPRINT_EXECUTOR_RESULT_STATE_PATH", run_dir / "executor_result_state.json")),
        "task_packet": Path(os.environ.get("BLUEPRINT_TASK_PACKET", run_dir / "task_packet.json")),
    }


def _load_task_packet() -> TaskPacket:
    return TaskPacket.model_validate(json.loads(_paths()["task_packet"].read_text(encoding="utf-8")))


def _load_terminal_report() -> TerminalReport | None:
    payload = _read_json(_paths()["terminal"], None)
    if not isinstance(payload, dict):
        return None
    try:
        return TerminalReport.model_validate(payload)
    except ValidationError:
        return None


def _load_state() -> ExecutorResultState:
    payload = _read_json(_paths()["state"], {})
    if isinstance(payload, dict):
        try:
            return ExecutorResultState.model_validate(payload)
        except ValidationError:
            pass
    return ExecutorResultState()


def _write_state(state: ExecutorResultState) -> None:
    atomic_write_json(_paths()["state"], state.model_dump())


def _terminal_error_payload(report: TerminalReport) -> dict:
    return {
        "ok": False,
        "error_code": "run_already_terminal",
        "terminal_status": report.status,
        "terminal_kind": report.terminal_kind,
    }


def _normalize_reason_code(reason_code: str, details: dict) -> tuple[FailureReasonCode, dict]:
    normalized = str(reason_code or "").strip()
    if normalized in REASON_CODES:
        return normalized, details
    merged = dict(details)
    merged["original_reason_code"] = normalized or None
    return "unknown", merged


def _resolve_candidate_path(candidate_manifest_path: str) -> Path:
    return ManifestService.resolve_candidate_manifest_path(_run_dir(), candidate_manifest_path)


def _validate_candidate_manifest(candidate_path: Path, packet: TaskPacket) -> tuple[Manifest | None, list[str]]:
    errors: list[str] = []
    if not candidate_path.exists():
        return None, [f"{candidate_path.name} is missing."]
    try:
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"{candidate_path.name} is not valid JSON: {exc}"]
    try:
        manifest = ManifestService.normalize_manifest_payload(payload, run_id=_run_id())
    except (ValidationError, ValueError) as exc:
        return None, [str(exc)]

    expected_by_role = {
        item.role: item
        for item in packet.expected_outputs
        if item.role and item.path_hint and item.artifact_class
    }
    required_roles = {role for role, item in expected_by_role.items() if item.required}
    created_by_role = {item.role: item for item in manifest.created_assets if item.role}
    missing_roles = sorted(required_roles - set(created_by_role))
    if missing_roles:
        formatted = ", ".join(
            f"{role} ({expected_by_role[role].path_hint})"
            for role in missing_roles
            if role in expected_by_role
        )
        errors.append(f"{candidate_path.name} is missing created_assets for expected outputs: {formatted}")
    project_root = _project_root()
    allowed_prefixes = tuple(packet.allowed_paths)
    for role, asset in created_by_role.items():
        expected = expected_by_role.get(role)
        if expected is None:
            errors.append(f"{candidate_path.name} declared unexpected output role: {role}")
            continue
        if not asset.path.startswith(allowed_prefixes):
            errors.append(f"{candidate_path.name} output is outside allowed_paths: {asset.path}")
            continue
        # workspace_write allows free writes in work/, but formal expected outputs must go through results/.
        if (
            packet.execution_policy.mode == "workspace_write"
            and asset.path.startswith("work/")
        ):
            errors.append(
                f"{candidate_path.name} output must not be placed in work/ directory: {asset.path}"
            )
            continue
        output_path = (project_root / asset.path).resolve()
        project_root_resolved = project_root.resolve()
        if output_path != project_root_resolved and project_root_resolved not in output_path.parents:
            errors.append(f"{candidate_path.name} output path escapes project root: {asset.path}")
            continue
        if not output_path.exists():
            errors.append(f"Missing output file: {asset.path} ({role})")
            continue
        detected_class = detect_artifact_class(output_path)
        detected_format = detect_artifact_format(output_path)
        if detected_class != expected.artifact_class:
            errors.append(
                f"{candidate_path.name} output class mismatch for {role}: expected {expected.artifact_class}, got {detected_class or 'unknown'}"
            )
        if expected.accepted_formats and detected_format not in expected.accepted_formats:
            errors.append(
                f"{candidate_path.name} output format mismatch for {role}: expected one of {', '.join(expected.accepted_formats)}, got {detected_format or 'unknown'}"
            )
    return manifest, errors


def _record_complete_failure(errors: list[str]) -> ExecutorResultState:
    state = _load_state()
    state.report_complete_failure_count += 1
    state.last_validation_errors = list(errors)
    _write_state(state)
    return state


def _accept_terminal_report(report: TerminalReport) -> None:
    atomic_write_json(_paths()["terminal"], report.model_dump(exclude_none=True))


def _handle_complete(candidate_manifest_path: str) -> int:
    existing = _load_terminal_report()
    if existing is not None:
        print(json.dumps(_terminal_error_payload(existing), ensure_ascii=False), flush=True)
        return 2
    packet = _load_task_packet()
    try:
        candidate_path = _resolve_candidate_path(candidate_manifest_path)
    except ValueError as exc:
        state = _record_complete_failure([str(exc)])
        exhausted = state.report_complete_failure_count >= MAX_REPORT_COMPLETE_FAILURES
        payload = {
            "ok": False,
            "error_code": "report_complete_repair_budget_exhausted" if exhausted else "report_complete_invalid",
            "summary": "Manifest completion failed validation too many times." if exhausted else "Manifest completion failed validation.",
            "validation_errors": [str(exc)],
            "failure_reason_code": "contract_violation",
            "terminal": exhausted,
        }
        if exhausted:
            failure = ExecutorFailureReport(
                schema_version="executor_failure.v1",
                reason_code="contract_violation",
                summary="Manifest completion failed validation too many times.",
                details={
                    "validation_errors": [str(exc)],
                    "failed_report": "report_complete",
                    "attempt_count": state.report_complete_failure_count,
                },
            )
            atomic_write_json(_paths()["failure"], failure.model_dump())
            _accept_terminal_report(
                TerminalReport(
                    schema_version="executor_terminal_report.v1",
                    run_id=_run_id(),
                    terminal_kind="synthetic_failure",
                    accepted_at=_utc_now(),
                    summary=failure.summary,
                    reason_code=failure.reason_code,
                    status="failed",
                    failure_report_path=_paths()["failure"].name,
                )
            )
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return 2 if exhausted else 1
    manifest, errors = _validate_candidate_manifest(candidate_path, packet)
    if errors:
        state = _record_complete_failure(errors)
        exhausted = state.report_complete_failure_count >= MAX_REPORT_COMPLETE_FAILURES
        payload = {
            "ok": False,
            "error_code": "report_complete_repair_budget_exhausted" if exhausted else "report_complete_invalid",
            "summary": "Manifest completion failed validation too many times." if exhausted else "Manifest completion failed validation.",
            "validation_errors": errors,
            "failure_reason_code": "contract_violation",
            "terminal": exhausted,
        }
        if exhausted:
            failure = ExecutorFailureReport(
                schema_version="executor_failure.v1",
                reason_code="contract_violation",
                summary="Manifest completion failed validation too many times.",
                details={
                    "validation_errors": errors,
                    "failed_report": "report_complete",
                    "attempt_count": state.report_complete_failure_count,
                },
            )
            atomic_write_json(_paths()["failure"], failure.model_dump())
            _accept_terminal_report(
                TerminalReport(
                    schema_version="executor_terminal_report.v1",
                    run_id=_run_id(),
                    terminal_kind="synthetic_failure",
                    accepted_at=_utc_now(),
                    summary=failure.summary,
                    reason_code=failure.reason_code,
                    status="failed",
                    failure_report_path=_paths()["failure"].name,
                )
            )
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return 2 if exhausted else 1
    completion = ExecutorCompletionReport(
        schema_version="executor_completion.v1",
        candidate_manifest_path=str(candidate_path.relative_to(_run_dir())),
        canonical_manifest=manifest.model_dump(exclude_none=True),
    )
    atomic_write_json(_paths()["completion"], completion.model_dump(exclude_none=True))
    _write_state(ExecutorResultState())
    _accept_terminal_report(
        TerminalReport(
            schema_version="executor_terminal_report.v1",
            run_id=_run_id(),
            terminal_kind="report_complete",
            accepted_at=_utc_now(),
            summary=(manifest.manager_report.summary if manifest.manager_report and manifest.manager_report.summary else manifest.summary),
            status="pending_review",
            completion_report_path=_paths()["completion"].name,
            candidate_manifest_path=completion.candidate_manifest_path,
        )
    )
    print(json.dumps({"ok": True, "accepted_for_review": True, "candidate_manifest_path": completion.candidate_manifest_path}, ensure_ascii=False), flush=True)
    return 0


def _handle_fail(reason_code: str, summary: str, details_json: str | None) -> int:
    existing = _load_terminal_report()
    if existing is not None:
        print(json.dumps(_terminal_error_payload(existing), ensure_ascii=False), flush=True)
        return 2
    try:
        details = json.loads(details_json) if details_json else {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error_code": "invalid_details_json", "summary": str(exc)}, ensure_ascii=False), flush=True)
        return 1
    if not isinstance(details, dict):
        print(json.dumps({"ok": False, "error_code": "invalid_details_json", "summary": "details-json must decode to an object."}, ensure_ascii=False), flush=True)
        return 1
    normalized_reason, normalized_details = _normalize_reason_code(reason_code, details)
    failure = ExecutorFailureReport(
        schema_version="executor_failure.v1",
        reason_code=normalized_reason,
        summary=summary,
        details=normalized_details,
    )
    atomic_write_json(_paths()["failure"], failure.model_dump(exclude_none=True))
    _accept_terminal_report(
        TerminalReport(
            schema_version="executor_terminal_report.v1",
            run_id=_run_id(),
            terminal_kind="report_fail",
            accepted_at=_utc_now(),
            summary=failure.summary,
            reason_code=failure.reason_code,
            status="failed",
            failure_report_path=_paths()["failure"].name,
        )
    )
    print(json.dumps({"ok": True, "failed": True, "reason_code": failure.reason_code}, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = ArgumentParser(description="Submit the terminal result for a Blueprint executor run.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--manifest", required=True, dest="candidate_manifest_path")

    fail_parser = subparsers.add_parser("fail")
    fail_parser.add_argument("--reason-code", required=True)
    fail_parser.add_argument("--summary", required=True)
    fail_parser.add_argument("--details-json", default="")

    args = parser.parse_args()
    if args.command == "complete":
        return _handle_complete(args.candidate_manifest_path)
    return _handle_fail(args.reason_code, args.summary, args.details_json)


if __name__ == "__main__":
    raise SystemExit(main())
''',
            encoding="utf-8",
        )

    @staticmethod
    def _write_dependency_report_tool(path: Path) -> None:
        path.write_text(
            '''from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timezone
import json
import os
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = ArgumentParser(description="Report missing Blueprint executor runtime dependencies.")
    parser.add_argument("--ecosystem", default="other", choices=["python", "R", "conda", "system", "other"])
    parser.add_argument("--package", action="append", dest="packages", required=True)
    parser.add_argument("--manager", default="")
    parser.add_argument("--runtime", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--non-blocking", action="store_true")
    args = parser.parse_args()

    packages = [item for item in (args.packages or []) if item]
    runtime = args.runtime or (
        os.environ.get("BLUEPRINT_R_RUNTIME", "") if args.ecosystem == "R" else os.environ.get("BLUEPRINT_PYTHON_RUNTIME", "")
    )
    blocking = not args.non_blocking
    package_text = ", ".join(packages)
    message = args.message or f"Missing required {args.ecosystem} runtime dependencies: {package_text}."
    result_tool = Path(os.environ.get("BLUEPRINT_EXECUTOR_RESULT_TOOL", "report_executor_result.py"))
    event = {
        "type": "issue_report",
        "stage": "runtime_dependency_check",
        "severity": "high" if blocking else "medium",
        "needs_manager": blocking,
        "message": message,
        "suggested_actions": [
            "Install the missing dependencies in the selected runtime environment.",
            "Select a compatible Python/R runtime for this card and rerun it.",
        ],
        "metadata": {
            "issue_kind": "runtime_dependency_missing",
            "dependency_status": "missing",
            "ecosystem": args.ecosystem,
            "missing_packages": packages,
            "package_manager": args.manager,
            "runtime": runtime,
            "blocking": blocking,
        },
    }

    issue_path = Path(os.environ.get("BLUEPRINT_DEPENDENCY_ISSUE_PATH", "dependency_issue.json"))
    issue_payload = {
        "schema_version": "dependency_issue.v1",
        "run_id": os.environ.get("BLUEPRINT_RUN_ID") or os.environ.get("BLUEPRINT_TASK_ID"),
        "card_id": os.environ.get("BLUEPRINT_CARD_ID"),
        "created_at": _utc_now(),
        "blocking": blocking,
        "issues": [event],
    }
    _atomic_write_json(issue_path, issue_payload)

    brief_path = Path(os.environ.get("BLUEPRINT_MANAGER_BRIEF", "manager_brief.json"))
    try:
        brief = json.loads(brief_path.read_text(encoding="utf-8")) if brief_path.exists() else {}
    except json.JSONDecodeError:
        brief = {}
    brief["run_id"] = os.environ.get("BLUEPRINT_RUN_ID") or os.environ.get("BLUEPRINT_TASK_ID") or brief.get("run_id")
    issues = list(brief.get("issues") or [])
    issues.append(event)
    brief["issues"] = issues
    dependency_issues = list(brief.get("dependency_issues") or [])
    dependency_issues.append(event)
    brief["dependency_issues"] = dependency_issues
    _atomic_write_json(brief_path, brief)

    details = {
        "ecosystem": args.ecosystem,
        "missing_packages": packages,
        "package_manager": args.manager,
        "runtime": runtime,
        "blocking": blocking,
    }
    complete = __import__("subprocess").run(
        [
            os.environ.get("PYTHON", "python3"),
            str(result_tool),
            "fail",
            "--reason-code",
            "runtime_dependency_missing",
            "--summary",
            message,
            "--details-json",
            json.dumps(details, ensure_ascii=False),
        ],
        check=False,
    )
    prefix = os.environ.get("BLUEPRINT_MANAGER_REPORT_STDOUT_PREFIX", "BP_EVENT ")
    print(prefix + json.dumps(event, ensure_ascii=False), flush=True)
    if complete.returncode not in {0, 2}:
        return complete.returncode
    return 3 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
            encoding="utf-8",
        )

    def _render_executor_brief(self, packet: TaskPacket) -> str:
        lines = [
            f"# Executor Brief for {packet.task_id}",
            "",
            "## Task",
            f"- Project: {packet.project_id}",
            f"- Card: {packet.card_id} ({packet.card_title})",
            f"- Goal: {packet.goal}",
            "",
            "## Inputs",
            "Input asset paths are project-root-relative. Resolve them from $BLUEPRINT_PROJECT_ROOT, not from the current working directory.",
        ]
        if packet.execution_policy.mode == "workspace_write":
            lines.append(
                "In workspace_write mode, work/ is persistent across runs; inspect or clean only when instructed."
            )
        if packet.input_assets:
            lines.extend(f"- {item.asset_id}: {item.path} [{item.type}]" for item in packet.input_assets)
        else:
            lines.append("- No linked input assets.")
        lines.extend(
            [
                "",
                "## Expected Outputs",
            ]
        )
        lines.extend(
            f"- {item.role}: {item.path_hint} [{item.artifact_class}; accepted={', '.join(item.accepted_formats) if item.accepted_formats else 'any'}; preferred={item.preferred_format or 'auto'}]"
            for item in packet.expected_outputs
        )
        lines.extend(
            [
                "",
                "## Runtime Policy",
                f"- Allowed paths: {', '.join(packet.allowed_paths)}",
                f"- Readonly paths: {', '.join(packet.readonly_paths) if packet.readonly_paths else 'none'}",
                f"- Forbidden paths: {', '.join(packet.forbidden_paths)}",
            ]
        )
        if packet.executor_context:
            python_runtime = packet.executor_context.runtime_bindings.conda_env or "system"
            r_runtime = packet.executor_context.runtime_bindings.r_env or "system"
            lines.extend(
                [
                    "",
                    "## Executor Context",
                    f"- Profile: {packet.executor_context.executor_profile or 'none'}",
                    f"- Skills: {', '.join(packet.executor_context.skills) if packet.executor_context.skills else 'none'}",
                    f"- MCP servers: {', '.join(packet.executor_context.mcp_servers) if packet.executor_context.mcp_servers else 'none'}",
                    f"- Python runtime: {python_runtime}",
                    f"- R runtime: {r_runtime}",
                    f"- Skill bindings file: $BLUEPRINT_EXECUTOR_SKILL_BINDINGS",
                    f"- MCP bindings file: $BLUEPRINT_EXECUTOR_MCP_BINDINGS",
                    f"- MCP config file: $BLUEPRINT_EXECUTOR_MCP_CONFIG",
                ]
            )
            lines.extend(f"- Instruction: {item}" for item in packet.executor_context.instruction_blocks)
            lines.extend(
                f"- Reference: {item.path} ({item.type})" for item in packet.executor_context.references
            )
        lines.extend(
            [
                "",
                "## Reporting Contract",
                "- Use $BLUEPRINT_EXECUTOR_RESULT_TOOL to submit either the complete or fail terminal report.",
                "- Do not write or update manager_brief.json directly.",
                "- Preserve executed code under scripts/generated/{run_id}/ and declare it in manifest.code_artifacts.",
                "- Write a candidate manifest to $BLUEPRINT_MANIFEST_CANDIDATE_PATH first; the backend promotes it to manifest.json only after validation.",
                "- Backend validation will reject missing outputs, missing code evidence, path violations, and placeholder data.",
            ]
        )
        return "\n".join(lines) + "\n"

    def _render_executor_prompt(self, packet: TaskPacket) -> str:
        lines = [
            f"You are the {self.name} executor for Blueprint run {packet.task_id}.",
            "",
            "Primary objective:",
            packet.goal,
            "",
            "Executor contract:",
            "- The backend validates outputs using $BLUEPRINT_TASK_PACKET, $BLUEPRINT_ADAPTER_CONTRACT, "
            "$BLUEPRINT_MANIFEST_CANDIDATE_PATH, terminal reports, and preserved code artifacts.",
            "- If validation fails, the run will return structured errors for repair instead of being accepted by Manager.",
            "- Keep executable analysis code under scripts/generated/{run_id}/ and declare it in manifest.code_artifacts.",
            "- Write your candidate manifest to $BLUEPRINT_MANIFEST_CANDIDATE_PATH and then call the terminal result helper with the complete subcommand.",
            "- If required runtime dependencies are missing, do not install packages with pip, conda, install.packages, or BiocManager. "
            "Call $BLUEPRINT_EXECUTOR_RESULT_TOOL with the fail subcommand and stop the analysis until the runtime is fixed.",
            "- After your terminal result has been accepted, exit immediately. "
            "Do not keep chatting, inspecting files, or printing result contents.",
            "",
            "Task packet:",
            "- JSON path: $BLUEPRINT_TASK_PACKET",
            f"- Project root: {packet.run_context.project_root if packet.run_context else '.'}",
            f"- Run dir: {packet.run_context.run_dir if packet.run_context else 'runs/current'}",
            f"- Result dir: {packet.run_context.result_dir if packet.run_context else 'results/current'}",
            f"- Python runtime: {packet.executor_context.runtime_bindings.conda_env if packet.executor_context and packet.executor_context.runtime_bindings.conda_env else 'system'}",
            f"- R runtime: {packet.executor_context.runtime_bindings.r_env if packet.executor_context and packet.executor_context.runtime_bindings.r_env else 'system'}",
            "- For R work, prefer BLUEPRINT_RSCRIPT when set; do not assume the Python conda environment also contains R.",
            "",
            "Input asset paths are project-root-relative. Resolve them from $BLUEPRINT_PROJECT_ROOT, not from the current working directory.",
        ]
        if packet.execution_policy.mode == "workspace_write":
            lines.append(
                "In workspace_write mode, files under $BLUEPRINT_USER_WORKSPACE may persist across runs. "
                "Do not assume work/ is clean unless the user explicitly requested cleanup."
            )
        lines.extend(
            [
                "",
                "Input assets:",
            ]
        )
        if packet.input_assets:
            lines.extend(f"- {item.asset_id}: {item.path} ({item.type})" for item in packet.input_assets)
        else:
            lines.append("- No materialized input assets were attached.")
        lines.extend(
            [
                "",
                "Expected outputs:",
            ]
        )
        lines.extend(
            f"- {item.role}: {item.path_hint} [{item.artifact_class}; accepted={', '.join(item.accepted_formats) if item.accepted_formats else 'any'}; preferred={item.preferred_format or 'auto'}]"
            for item in packet.expected_outputs
        )
        if packet.executor_context:
            lines.extend(
                [
                    "",
                    "Executor context:",
                    f"- Profile: {packet.executor_context.executor_profile or 'none'}",
                    f"- Skills: {', '.join(packet.executor_context.skills) if packet.executor_context.skills else 'none'}",
                    f"- MCP servers: {', '.join(packet.executor_context.mcp_servers) if packet.executor_context.mcp_servers else 'none'}",
                    f"- Skill bindings file: $BLUEPRINT_EXECUTOR_SKILL_BINDINGS",
                    f"- MCP bindings file: $BLUEPRINT_EXECUTOR_MCP_BINDINGS",
                    f"- MCP config file: $BLUEPRINT_EXECUTOR_MCP_CONFIG",
                ]
            )
            lines.extend(f"- Instruction: {item}" for item in packet.executor_context.instruction_blocks)
            lines.extend(f"- Reference: {item.path} ({item.type})" for item in packet.executor_context.references)
        lines.extend(
            [
                "",
                "Stdout discipline:",
                "- Do not print tables, matrices, SVG, reports, JSON manifests, or file contents to stdout.",
                "- Do not run cat/head/tail on large outputs for the user. The user will inspect files through Blueprint previews.",
                "- Stdout should contain only short factual progress lines or one concise final sentence with output paths.",
                "- Do not rely on BP_EVENT, manager_brief.json, or free-form stdout as the completion contract.",
                "",
                "Runtime dependency policy:",
                "- Probe required Python/R/system packages before doing expensive analysis.",
                "- Do not create or modify conda environments and do not install missing analysis packages inside the run.",
                "- When a required dependency is missing, call the terminal result helper with fail, then stop rather than substituting a weaker method silently.",
                "- Tool: python $BLUEPRINT_EXECUTOR_RESULT_TOOL fail --reason-code runtime_dependency_missing --summary 'Required package is unavailable.' --details-json '{\"ecosystem\":\"R\",\"missing_packages\":[\"clusterProfiler\"],\"package_manager\":\"Bioconductor\"}'",
            ]
        )
        lines.extend(
            [
                "",
                "Output contract:",
                "- $BLUEPRINT_MANIFEST_CANDIDATE_PATH must declare every created asset in created_assets with role/path.",
                "- $BLUEPRINT_MANIFEST_CANDIDATE_PATH must declare preserved code in code_artifacts when assets are created.",
                "- $BLUEPRINT_MANIFEST_CANDIDATE_PATH must set schema_version=executor_manifest.v2.",
                "- $BLUEPRINT_MANIFEST_CANDIDATE_PATH must include summary and manager_report.summary.",
                "- Use created_assets, not outputs.",
                "- Do not include run_id, status, inputs_used, commands_executed, validation_evidence, top-level metrics, top-level key_findings, recommended_graph_updates, or top-level warnings.",
                "- Keep warnings under manager_report.warnings.",
                "- Completion tool: python $BLUEPRINT_EXECUTOR_RESULT_TOOL complete --manifest $BLUEPRINT_MANIFEST_CANDIDATE_PATH",
            ]
        )
        return "\n".join(lines) + "\n"
