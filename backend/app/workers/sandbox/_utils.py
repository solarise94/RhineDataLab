from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.core.config import default_conda_base, default_conda_base_candidates


def resolve_project_path(project_root: Path, value: str) -> Path:
    """Return an absolute path, treating ``value`` as relative to ``project_root`` unless already absolute."""
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def extra_ro_binds(settings: object) -> list[str]:
    raw = getattr(settings, "executor_extra_ro_binds", "") or ""
    paths: list[str] = []
    for item in str(raw).split(","):
        value = os.path.expanduser(os.path.expandvars(item.strip()))
        if value:
            paths.append(value)
    return paths


def project_mask_paths(packet: Any, project_root: Path, run_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for item in packet.forbidden_paths:
        value = item.strip().lstrip("/")
        if not value:
            continue
        path = project_root / value
        if run_dir == path or run_dir in path.parents:
            continue
        paths.append(path)
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def dedupe_paths(paths: list[Path]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        value = str(path)
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def python_runtime_ro_binds(python_path: Path) -> list[str]:
    paths = [python_path]
    try:
        resolved = python_path.resolve()
    except OSError:
        resolved = python_path
    paths.append(resolved)
    for parent in python_path.parents:
        if parent.name == ".venv":
            paths.append(parent)
            break
    return dedupe_paths([path for path in paths if path.exists()])


def launch_template_ro_binds(env_keys: set[str], environment: dict[str, str]) -> list[str]:
    import shlex

    paths: list[Path] = []
    for key in env_keys:
        value = environment.get(key, "")
        for token in shlex.split(value):
            if not token.startswith("/"):
                continue
            path = Path(os.path.expanduser(os.path.expandvars(token)))
            if not path.exists():
                continue
            paths.append(path.parent if path.is_file() else path)
    return dedupe_paths(paths)


def resolve_rscript_runtime(r_env: str | None, settings: object) -> Path | None:
    if not r_env:
        found = shutil.which("Rscript")
        return Path(found) if found else None
    if r_env.startswith("/"):
        runtime_path = Path(r_env)
        if runtime_path.name == "Rscript" and runtime_path.exists():
            return runtime_path
        rscript_path = runtime_path / "bin" / "Rscript"
        return rscript_path if rscript_path.exists() else None
    configured_base = Path(getattr(settings, "executor_conda_base", default_conda_base()))
    candidates = default_conda_base_candidates(configured_base)
    for base in candidates:
        rscript_path = base / "envs" / r_env / "bin" / "Rscript"
        if rscript_path.exists():
            return rscript_path
        if r_env == "base":
            base_rscript = base / "bin" / "Rscript"
            if base_rscript.exists():
                return base_rscript
    return None


def r_user_library_paths(rscript_path: Path | None = None) -> list[Path]:
    paths: list[Path] = []
    raw = os.environ.get("R_LIBS_USER", "")
    for item in raw.split(os.pathsep):
        value = os.path.expanduser(os.path.expandvars(item.strip()))
        if value and Path(value).exists():
            paths.append(Path(value))
    if rscript_path and rscript_path.exists():
        try:
            result = subprocess.run(
                [
                    str(rscript_path),
                    "--no-init-file",
                    "--no-site-file",
                    "-e",
                    "cat(Sys.getenv('R_LIBS_USER'))",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result and result.returncode == 0:
            for item in result.stdout.split(os.pathsep):
                value = os.path.expanduser(os.path.expandvars(item.strip()))
                if value and Path(value).exists():
                    paths.append(Path(value))
    r_home = Path.home() / "R"
    if r_home.exists():
        paths.append(r_home)
    seen: set[str] = set()
    result_paths: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result_paths.append(path)
    return result_paths


def r_user_library_ro_binds(settings: object) -> list[str]:
    return [str(p) for p in r_user_library_paths(resolve_rscript_runtime(None, settings))]
