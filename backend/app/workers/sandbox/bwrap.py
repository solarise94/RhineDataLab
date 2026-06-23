from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import default_conda_base, default_conda_base_candidates, find_conda_solver
from app.workers.sandbox._utils import (
    dedupe_paths,
    extra_ro_binds,
    launch_template_ro_binds,
    project_mask_paths,
    python_runtime_ro_binds,
    resolve_project_path,
    resolve_rscript_runtime,
    r_user_library_ro_binds,
)
from app.workers.sandbox.base import SandboxRenderer


_BWRAP_SMOKE_CACHE: dict[str, bool] = {}


def ensure_bwrap_runtime() -> str:
    configured = str(os.environ.get("BLUEPRINT_BWRAP_BIN", "") or "").strip()
    if configured:
        if os.path.sep in configured:
            bwrap = os.path.expanduser(configured)
            if not os.path.exists(bwrap):
                raise RuntimeError(
                    "BLUEPRINT_EXECUTOR_SANDBOX_MODE=bwrap was configured with "
                    f"BLUEPRINT_BWRAP_BIN={configured!r}, but that path does not exist."
                )
        else:
            bwrap = shutil.which(configured)
            if not bwrap:
                raise RuntimeError(
                    "BLUEPRINT_EXECUTOR_SANDBOX_MODE=bwrap was configured with "
                    f"BLUEPRINT_BWRAP_BIN={configured!r}, but that command was not found in PATH."
                )
    else:
        bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("BLUEPRINT_EXECUTOR_SANDBOX_MODE=bwrap requires the bubblewrap executable (bwrap).")
    if bwrap not in _BWRAP_SMOKE_CACHE:
        result = subprocess.run(
            [
                bwrap,
                "--die-with-parent",
                "--ro-bind",
                "/usr",
                "/usr",
                "--ro-bind",
                "/bin",
                "/bin",
                "--ro-bind-try",
                "/lib",
                "/lib",
                "--ro-bind-try",
                "/lib64",
                "/lib64",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--",
                "/bin/true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        _BWRAP_SMOKE_CACHE[bwrap] = result.returncode == 0
    if not _BWRAP_SMOKE_CACHE[bwrap]:
        raise RuntimeError(
            "BLUEPRINT_EXECUTOR_SANDBOX_MODE=bwrap requires a working bubblewrap namespace. "
            "Run scripts/deploy_user_systemd.sh and fix deploy/runtime-dependencies.yml requirements."
        )
    return bwrap


class BwrapRenderer(SandboxRenderer):
    """Bubblewrap-based sandbox renderer. Behavior is identical to the legacy
    ``command_worker._wrap_with_bwrap`` implementation; only the packaging
    has changed.
    """

    @property
    def mode(self) -> str:
        return "bwrap"

    def should_sandbox(self) -> bool:
        return True

    def render(
        self,
        *,
        command: list[str],
        packet: Any,
        project_root: Path,
        run_dir: Path,
        environment: dict[str, str],
        adapter_extra_env_keys: set[str],
        settings: object,
    ) -> tuple[list[str], dict[str, Any]]:
        bwrap = ensure_bwrap_runtime()
        result_dir = resolve_project_path(project_root, packet.run_context.result_dir)
        script_run_dir = project_root / "scripts" / "generated" / packet.task_id
        tmp_dir = run_dir / "tmp"
        cache_dir = run_dir / "cache"
        home_dir = run_dir / "home"
        state_dir = run_dir / "state"
        pi_agent_dir = state_dir / "pi-agent"
        pi_session_dir = state_dir / "pi-sessions"
        xdg_config_dir = run_dir / "config"
        xdg_data_dir = run_dir / "data"
        xdg_state_dir = state_dir / "xdg"
        for path in (
            result_dir,
            script_run_dir,
            tmp_dir,
            cache_dir,
            home_dir,
            pi_agent_dir,
            pi_session_dir,
            xdg_config_dir,
            xdg_data_dir,
            xdg_state_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

        host_root_readonly = bool(getattr(settings, "executor_host_root_readonly", True))
        work_dir = project_root / "work"
        is_workspace_write = packet.execution_policy.mode == "workspace_write"
        writable_binds = [run_dir, result_dir, script_run_dir]
        if is_workspace_write:
            writable_binds.append(work_dir)
        readonly_binds = [Path("/")] if host_root_readonly else [project_root]
        masked_paths = project_mask_paths(packet, project_root, run_dir)
        backend_root = Path(__file__).resolve().parents[3]
        repo_root = backend_root.parent
        current_python = Path(sys.executable)
        python_runtime_paths = python_runtime_ro_binds(current_python)
        launch_template_paths = launch_template_ro_binds(adapter_extra_env_keys, environment)
        r_user_libs = r_user_library_ro_binds(settings)
        reference_data_root = Path(getattr(settings, "data_root", "")) / "_system" / "reference-data"
        bind_args: list[str] = [
            bwrap,
            "--die-with-parent",
            "--clearenv",
        ]
        if host_root_readonly:
            bind_args.extend(["--ro-bind", "/", "/"])
        else:
            bind_args.extend(["--ro-bind", str(project_root), str(project_root)])
            system_ro_binds = ["/bin", "/usr", "/lib", "/lib64", "/etc", "/opt", "/run/systemd/resolve"]
            extra_ro = extra_ro_binds(settings)
            repo_runtime_binds = [str(backend_root), str(repo_root / "scripts")]
            for host_path in [
                *system_ro_binds,
                *extra_ro,
                *repo_runtime_binds,
                *python_runtime_paths,
                *launch_template_paths,
                *r_user_libs,
                str(reference_data_root),
            ]:
                if Path(host_path).exists():
                    bind_args.extend(["--ro-bind", host_path, host_path])
                    readonly_binds.append(Path(host_path))
        bind_args.extend(
            [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
            ]
        )
        for path in masked_paths:
            bind_args.extend(["--tmpfs", str(path)])

        data_mount_point: Path | None = None
        if packet.mounted_data_directory:
            source = Path(packet.mounted_data_directory)
            if not source.exists():
                raise RuntimeError(
                    f"Mounted data directory is not accessible: {source}. "
                    "Detach and remount a valid data directory before running cards with data_mount/... inputs."
                )
            data_mount_point = project_root / "data_mount"
            if data_mount_point.exists():
                try:
                    has_content = any(data_mount_point.iterdir())
                except PermissionError:
                    has_content = False
                if has_content:
                    raise RuntimeError(
                        f"data_mount/ already exists in the project root and is non-empty: {data_mount_point}. "
                        "Remove or rename it before running cards with data_mount/... inputs."
                    )
            else:
                data_mount_point.mkdir(parents=False, exist_ok=False)
            readonly_binds.append(source)
            bind_args.extend(["--ro-bind", str(source), str(data_mount_point)])

        if is_workspace_write and work_dir.exists():
            bind_args.extend(["--bind", str(work_dir), str(work_dir)])
        bind_args.extend(
            [
                "--bind",
                str(run_dir),
                str(run_dir),
                "--bind",
                str(result_dir),
                str(result_dir),
                "--bind",
                str(script_run_dir),
                str(script_run_dir),
                "--chdir",
                str(work_dir if is_workspace_write else run_dir),
            ]
        )
        conda_base = Path(getattr(settings, "executor_conda_base", default_conda_base()))
        mamba_root_prefix = getattr(settings, "executor_mamba_root_prefix", None)
        mamba_root_prefix_path = Path(mamba_root_prefix) if mamba_root_prefix else None
        mambarc = getattr(settings, "executor_mambarc", None)
        if not host_root_readonly and conda_base.exists() and str(conda_base) not in {"/bin", "/usr", "/lib", "/lib64", "/etc", "/opt"}:
            bind_args.extend(["--ro-bind", str(conda_base), str(conda_base)])
            readonly_binds.append(conda_base)
        if mamba_root_prefix_path:
            if (
                not host_root_readonly
                and mamba_root_prefix_path.exists()
                and str(mamba_root_prefix_path) != str(conda_base)
                and str(mamba_root_prefix_path) not in {"/bin", "/usr", "/lib", "/lib64", "/etc", "/opt"}
            ):
                bind_args.extend(["--ro-bind", str(mamba_root_prefix_path), str(mamba_root_prefix_path)])
                readonly_binds.append(mamba_root_prefix_path)
            environment["MAMBA_ROOT_PREFIX"] = str(mamba_root_prefix_path)
        if mambarc:
            mambarc_path = Path(mambarc)
            if mambarc_path.exists():
                environment["MAMBARC"] = str(mambarc_path)
        if not host_root_readonly:
            runtime_paths: set[Path] = set()
            conda_env = packet.executor_context.runtime_bindings.conda_env if packet.executor_context else None
            r_env = packet.executor_context.runtime_bindings.r_env if packet.executor_context else None
            if conda_env:
                _, env_path = _resolve_conda_runtime(conda_env, settings)
                if env_path.exists():
                    runtime_paths.add(env_path.resolve())
            if r_env:
                rscript_path = resolve_rscript_runtime(r_env, settings)
                if rscript_path is not None and rscript_path.exists():
                    runtime_paths.add(rscript_path.parent.parent.resolve())
            for runtime_path in sorted(runtime_paths):
                if str(runtime_path) in {"/bin", "/usr", "/lib", "/lib64", "/etc", "/opt"}:
                    continue
                if any(str(runtime_path).startswith(str(bound)) and str(runtime_path) != str(bound) for bound in readonly_binds):
                    continue
                if any(str(runtime_path) == str(bound) for bound in readonly_binds):
                    continue
                bind_args.extend(["--ro-bind", str(runtime_path), str(runtime_path)])
                readonly_binds.append(runtime_path)
        env_keys = {
            "BLUEPRINT_PROJECT_ROOT",
            "BLUEPRINT_RUN_DIR",
            "BLUEPRINT_RESULT_DIR",
            "BLUEPRINT_TASK_PACKET",
            "BLUEPRINT_MANIFEST_PATH",
            "BLUEPRINT_MANIFEST_CANDIDATE_PATH",
            "BLUEPRINT_TRANSCRIPT_PATH",
            "BLUEPRINT_EXECUTOR_BRIEF",
            "BLUEPRINT_EXECUTOR_PROMPT",
            "BLUEPRINT_ADAPTER_CONTRACT",
            "BLUEPRINT_MANAGER_BRIEF",
            "BLUEPRINT_ALLOWED_PATHS",
            "BLUEPRINT_READONLY_PATHS",
            "BLUEPRINT_FORBIDDEN_PATHS",
            "BLUEPRINT_WORKER_TYPE",
            "BLUEPRINT_EXECUTOR_PROFILE",
            "BLUEPRINT_EXECUTOR_PROFILE_ID",
            "BLUEPRINT_AUTH_MODE",
            "BLUEPRINT_API_PROTOCOL",
            "BLUEPRINT_EXECUTOR_SKILLS",
            "BLUEPRINT_RUNTIME_WORKING_DIR",
            "BLUEPRINT_USER_WORKSPACE",
            "BLUEPRINT_MANAGER_REPORT_STDOUT_PREFIX",
            "BLUEPRINT_RSCRIPT",
            "BLUEPRINT_R_RUNTIME",
            "R_PROFILE_USER",
            "R_DEFAULT_DEVICE",
            "R_LIBS_USER",
            "PYTHONPATH",
            "PATH",
            "CONDA_PREFIX",
            "CONDA_DEFAULT_ENV",
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "R_USER_CACHE_DIR",
            "MPLCONFIGDIR",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "CLAUDE_CONFIG_DIR",
            "OPENCODE_CONFIG_DIR",
            "CODEX_CONFIG_DIR",
            "MAMBA_ROOT_PREFIX",
            "MAMBARC",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "PI_CODING_AGENT_DIR",
            "PI_CODING_AGENT_SESSION_DIR",
            "PI_SKIP_VERSION_CHECK",
        }
        if packet.executor_context:
            env_keys.update(packet.executor_context.runtime_bindings.env)
        env_keys.update(adapter_extra_env_keys)
        sandbox_plan_path = run_dir / "sandbox_plan.json"
        environment.update(
            {
                "BLUEPRINT_SANDBOX_PLAN": str(sandbox_plan_path),
                "HOME": str(home_dir),
                "USER": environment.get("USER") or environment.get("LOGNAME") or "blueprint",
                "LOGNAME": environment.get("LOGNAME") or environment.get("USER") or "blueprint",
                "TMPDIR": str(tmp_dir),
                "XDG_CACHE_HOME": str(cache_dir),
                "XDG_CONFIG_HOME": str(xdg_config_dir),
                "XDG_DATA_HOME": str(xdg_data_dir),
                "XDG_STATE_HOME": str(xdg_state_dir),
                "R_USER_CACHE_DIR": str(cache_dir / "R"),
                "MPLCONFIGDIR": str(cache_dir / "matplotlib"),
                "PI_CODING_AGENT_DIR": str(pi_agent_dir),
                "PI_CODING_AGENT_SESSION_DIR": str(pi_session_dir),
                "PI_SKIP_VERSION_CHECK": environment.get("PI_SKIP_VERSION_CHECK", "1"),
            }
        )
        if r_user_libs and "R_LIBS_USER" not in environment:
            environment["R_LIBS_USER"] = os.pathsep.join(r_user_libs)

        host_home = os.environ.get("HOME", "")
        host_xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
        host_claude_config = os.environ.get("CLAUDE_CONFIG_DIR", "")
        host_opencode_config = os.environ.get("OPENCODE_CONFIG_DIR", "")
        host_codex_config = os.environ.get("CODEX_CONFIG_DIR", "")
        host_pi_agent_dir = os.environ.get("PI_CODING_AGENT_DIR", "")

        if host_home:
            environment["BLUEPRINT_HOST_HOME"] = host_home
        if host_xdg_config:
            environment["BLUEPRINT_HOST_XDG_CONFIG_HOME"] = host_xdg_config
        if host_claude_config:
            environment["BLUEPRINT_HOST_CLAUDE_CONFIG_DIR"] = host_claude_config
        if host_opencode_config:
            environment["BLUEPRINT_HOST_OPENCODE_CONFIG_DIR"] = host_opencode_config
        if host_codex_config:
            environment["BLUEPRINT_HOST_CODEX_CONFIG_DIR"] = host_codex_config
        if host_pi_agent_dir:
            environment["BLUEPRINT_HOST_PI_CODING_AGENT_DIR"] = host_pi_agent_dir

        env_keys.add("BLUEPRINT_SANDBOX_PLAN")
        env_keys.add("BLUEPRINT_HOST_HOME")
        env_keys.add("BLUEPRINT_HOST_XDG_CONFIG_HOME")
        env_keys.add("BLUEPRINT_HOST_CLAUDE_CONFIG_DIR")
        env_keys.add("BLUEPRINT_HOST_OPENCODE_CONFIG_DIR")
        env_keys.add("BLUEPRINT_HOST_CODEX_CONFIG_DIR")
        env_keys.add("BLUEPRINT_HOST_PI_CODING_AGENT_DIR")
        if "LANG" not in environment and os.environ.get("LANG"):
            environment["LANG"] = os.environ["LANG"]
        sandbox_plan = {
            "mode": "bwrap",
            "network": "host",
            "network_isolation": False,
            "host_root_readonly": host_root_readonly,
            "project_root": str(project_root),
            "readonly_binds": dedupe_paths(readonly_binds),
            "writable_binds": dedupe_paths(writable_binds),
            "masked_paths": dedupe_paths(masked_paths),
            "tmp_dir": str(tmp_dir),
            "cache_dir": str(cache_dir),
            "home_dir": str(home_dir),
            "pi_agent_dir": str(pi_agent_dir),
            "pi_session_dir": str(pi_session_dir),
            "conda_base": str(conda_base) if conda_base.exists() else None,
            "mamba_root_prefix": str(mamba_root_prefix_path) if mamba_root_prefix and mamba_root_prefix_path.exists() else None,
            "conda_env": packet.executor_context.runtime_bindings.conda_env if packet.executor_context else None,
            "r_env": packet.executor_context.runtime_bindings.r_env if packet.executor_context else None,
            "rscript": environment.get("BLUEPRINT_RSCRIPT"),
            "backend_root": str(backend_root),
            "python_executable": str(current_python),
            "reference_data_root": str(reference_data_root) if reference_data_root.exists() else None,
            "clearenv": True,
            "data_mount": {
                "source": packet.mounted_data_directory,
                "mount_point": str(data_mount_point) if data_mount_point else None,
            } if packet.mounted_data_directory else None,
            "env_keys": sorted(key for key in env_keys if key in environment),
            "runtime_env_keys": sorted(packet.executor_context.runtime_bindings.env) if packet.executor_context else [],
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        sandbox_plan_path.write_text(json.dumps(sandbox_plan, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        for key in sorted(env_keys):
            if key in environment:
                bind_args.extend(["--setenv", key, environment[key]])
        bind_args.extend(["--", *command])
        return bind_args, sandbox_plan


def _resolve_conda_runtime(conda_env: str, settings: object) -> tuple[Path, Path]:
    from app.core.config import default_conda_base_candidates

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
