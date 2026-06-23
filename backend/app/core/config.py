from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


def _parse_project_roots(raw: str) -> list[Path]:
    """Parse BLUEPRINT_PROJECT_ROOTS into a list of absolute Paths."""
    roots: list[Path] = []
    seen: set[Path] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        p = Path(part).expanduser().resolve()
        if p not in seen:
            seen.add(p)
            roots.append(p)
    return roots


def default_conda_base() -> Path:
    return Path.home() / "miniconda3"


def default_conda_base_candidates(configured_base: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in (
        configured_base,
        Path.home() / ".local/share/blueprint-re/mamba",  # bundled micromamba root prefix
        Path.home() / "miniforge3",
        Path.home() / "miniconda3",
        Path.home() / "anaconda3",
        Path("/opt/conda"),
    ):
        if candidate is None:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return candidates


def find_conda_solver(conda_base: Path) -> Path | None:
    """Search ``conda_base`` for a conda solver executable.

    Prefers ``micromamba`` (fastest, standalone), then ``mamba``, then ``conda``.
    Checks both ``bin/`` and ``condabin/``. Returns ``None`` when no solver is found.
    """
    for name in ("micromamba", "mamba", "conda"):
        for subdir in ("bin", "condabin"):
            candidate = conda_base / subdir / name
            if candidate.exists():
                return candidate
    return None


def derive_conda_base_from_runtime_path(runtime_path: Path) -> Path | None:
    """Derive the conda base directory from a resolved runtime path.

    Handles:
    - Conda env directory (e.g. ``/home/user/miniforge3/envs/myenv``)
    - Conda base directory (e.g. ``/home/user/miniconda3``)
    - Rscript path (e.g. ``/home/user/miniforge3/envs/R_env/bin/Rscript``)

    Returns ``None`` when ``runtime_path`` is falsy.
    """
    if not runtime_path:
        return None
    path = runtime_path
    if path.is_file():
        path = path.parent.parent  # Rscript -> env dir
    if path.parent.name == "envs":
        return path.parent.parent
    return path


class Settings(BaseSettings):
    app_name: str = "Blueprint RE v3"
    api_prefix: str = "/api"
    data_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[3] / "workspace")
    schema_version: str = "0.1.0"
    project_prefix: str = "proj"
    default_project_id: str = "demo-rnaseq"
    default_project_name: str = "RNA-seq Demo Project"
    frontend_origin: str = "http://127.0.0.1:3000"
    artifact_size_threshold_mb: int = 50
    deepseek_api_base_url: str = "https://api.deepseek.com/anthropic"
    deepseek_api_key: SecretStr | None = None
    pi_deepseek_base_url: str = "https://api.deepseek.com"
    anthropic_api_key: SecretStr | None = None
    anthropic_api_base_url: str = "https://api.anthropic.com"
    openai_api_key: SecretStr | None = None
    openai_api_base_url: str = "https://api.openai.com/v1"
    manager_api_key: SecretStr | None = None
    manager_api_base_url: str | None = None
    reviewer_api_key: SecretStr | None = None
    reviewer_api_base_url: str | None = None
    pi_api_key: SecretStr | None = None
    pi_anthropic_base_url: str | None = None
    opencode_api_key: SecretStr | None = None
    opencode_api_base_url: str | None = None
    opencode_api_protocol: str | None = None
    manager_model: str = "deepseek-v4-pro"
    executor_model: str = "deepseek-v4-flash"
    pi_executor_model: str | None = None
    opencode_executor_model: str | None = None
    reviewer_model: str = "deepseek-v4-flash"
    library_summarizer_model: str = "deepseek-v4-flash"
    manager_backend: str = "pi"
    manager_temperature: float = 0.2
    manager_max_tokens: int = 2400
    manager_timeout_seconds: int = 600
    reviewer_max_tokens: int = 1800
    reviewer_max_turns: int = 24
    pi_manager_url: str = "http://127.0.0.1:18002"
    backend_api_base_url: str = "http://127.0.0.1:18001/api"
    internal_tool_token: SecretStr | None = None
    default_worker_type: str = "pi"
    worker_timeout_seconds: int = 1800
    manifest_repair_timeout_seconds: int = 180
    executor_sandbox_mode: str = "bwrap"
    executor_max_concurrent_runs: int = 3
    executor_conda_base: Path = Field(default_factory=default_conda_base)
    executor_mamba_root_prefix: str | None = None
    executor_mambarc: str | None = None
    default_python_runtime: str | None = None
    default_r_runtime: str | None = None
    cran_mirror: str = ""
    bioconductor_mirror: str = ""
    pypi_mirror: str = ""

    # Egress proxy settings (Layer D). These are re-exported to os.environ at
    # startup so that subprocess installers (conda/pip/R) and urllib-based
    # downloads honor them automatically.
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = ""
    reference_download_timeout_s: int = 1800
    reference_download_retries: int = 2

    executor_host_root_readonly: bool = True
    executor_extra_ro_binds: str = Field(default_factory=lambda: f"{Path.home()}/.nvm,{Path.home()}/.local")
    opencode_command: str | None = None
    pi_command: str | None = None
    claude_code_command: str | None = None
    codex_command: str | None = None
    # Structured argv templates (preferred over string templates to avoid shlex.split issues with paths containing spaces)
    opencode_command_json: list[str] | None = None
    pi_command_json: list[str] | None = None
    claude_code_command_json: list[str] | None = None
    codex_command_json: list[str] | None = None

    # Chat single-source-of-truth feature flag (Doc 71 Phase 0)
    chat_single_source: bool = False

    # Runtime dependency resolver controls (P1).
    # Default policy is "allow_safe_registry_install" so resolver-approved
    # single-family fallback installs (pip / cran / bioconductor) may execute
    # through structured backend commands. Per-project overrides live in
    # ``graph.metadata.dependency_policy`` and will be honored in a later pass.
    runtime_dependency_fallback_policy: str = "allow_safe_registry_install"
    runtime_dependency_probe_timeout_seconds: int = 60
    runtime_dependency_cache_ttl_seconds: int = 3600
    project_roots: str = ""
    data_directory_roots: str = ""
    data_mount_hash_limit_bytes: int = 100 * 1024 * 1024  # 100 MB default

    model_config = {
        "env_prefix": "BLUEPRINT_",
        "env_file": ".env",
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
