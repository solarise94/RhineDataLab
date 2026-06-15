#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
INSTALL_INTERACTIVE=0
REQUIRED_PYTHON_VERSION="3.13.0"
REQUIRED_NODE_VERSION="22.19.0"
BLUEPRINT_MIRROR_PRESET="${BLUEPRINT_MIRROR_PRESET:-tsinghua}"

# Remember which mirror variables were explicitly provided by the user so we
# do not overwrite them when the preset changes later (e.g. via .env or
# --interactive). Script-derived values are recomputed on preset changes.
_USER_CRAN_MIRROR_SET=0
_USER_BIOCONDUCTOR_MIRROR_SET=0
_USER_PYPI_MIRROR_SET=0
[[ -n "${BLUEPRINT_CRAN_MIRROR:-}" ]] && _USER_CRAN_MIRROR_SET=1
[[ -n "${BLUEPRINT_BIOCONDUCTOR_MIRROR:-}" ]] && _USER_BIOCONDUCTOR_MIRROR_SET=1
[[ -n "${BLUEPRINT_PYPI_MIRROR:-}" ]] && _USER_PYPI_MIRROR_SET=1
_INITIAL_MIRROR_PRESET="${BLUEPRINT_MIRROR_PRESET}"

# Expand mirror preset into registry-specific *_mirror variables.
# Try the source-tree layout first, then the release bundle layout.
MIRROR_PRESETS_DIR=""
if [[ -f "${ROOT_DIR}/deploy/runtime/mirror-presets/mirror_env.sh" ]]; then
  MIRROR_PRESETS_DIR="${ROOT_DIR}/deploy/runtime/mirror-presets"
elif [[ -f "${ROOT_DIR}/runtime/mirror-presets/mirror_env.sh" ]]; then
  MIRROR_PRESETS_DIR="${ROOT_DIR}/runtime/mirror-presets"
fi

_expand_mirror_preset() {
  local preset="$1"
  [[ -n "${MIRROR_PRESETS_DIR}" ]] || return 0
  # If the preset changed since the last expansion, clear script-derived
  # mirrors so they are recomputed. User-explicit values are preserved.
  if [[ "${preset}" != "${_LAST_EXPANDED_MIRROR_PRESET:-}" ]]; then
    [[ "${_USER_CRAN_MIRROR_SET}" -eq 0 ]] && BLUEPRINT_CRAN_MIRROR=""
    [[ "${_USER_BIOCONDUCTOR_MIRROR_SET}" -eq 0 ]] && BLUEPRINT_BIOCONDUCTOR_MIRROR=""
    [[ "${_USER_PYPI_MIRROR_SET}" -eq 0 ]] && BLUEPRINT_PYPI_MIRROR=""
  fi
  _LAST_EXPANDED_MIRROR_PRESET="${preset}"
  # shellcheck disable=SC1091
  source "${MIRROR_PRESETS_DIR}/mirror_env.sh"
  if [[ "${_USER_CRAN_MIRROR_SET}" -eq 0 ]]; then
    BLUEPRINT_CRAN_MIRROR="${BLUEPRINT_CRAN_MIRROR:-$(eval echo "\${${preset}_cran_mirror:-}")}"
  fi
  if [[ "${_USER_BIOCONDUCTOR_MIRROR_SET}" -eq 0 ]]; then
    BLUEPRINT_BIOCONDUCTOR_MIRROR="${BLUEPRINT_BIOCONDUCTOR_MIRROR:-$(eval echo "\${${preset}_bioconductor_mirror:-}")}"
  fi
  if [[ "${_USER_PYPI_MIRROR_SET}" -eq 0 ]]; then
    BLUEPRINT_PYPI_MIRROR="${BLUEPRINT_PYPI_MIRROR:-$(eval echo "\${${preset}_pypi_mirror:-}")}"
  fi
}

_expand_mirror_preset "${BLUEPRINT_MIRROR_PRESET}"

for arg in "$@"; do
  case "${arg}" in
    --interactive)
      INSTALL_INTERACTIVE=1
      ;;
    --yes|--non-interactive)
      INSTALL_INTERACTIVE=0
      ;;
    *)
      ;;
  esac
done

version_gte() {
  local actual="$1"
  local required="$2"
  [[ "$(printf '%s\n%s\n' "${required}" "${actual}" | sort -V | head -n1)" == "${required}" ]]
}

python_version_of() {
  local python_bin="$1"
  "${python_bin}" -c 'import sys; print(".".join(str(part) for part in sys.version_info[:3]))'
}

find_python_bin() {
  local candidate
  local version
  for candidate in python3.13 python3; do
    if ! command -v "${candidate}" >/dev/null 2>&1; then
      continue
    fi
    version="$(python_version_of "${candidate}" 2>/dev/null || true)"
    if [[ -n "${version}" ]] && version_gte "${version}" "${REQUIRED_PYTHON_VERSION}"; then
      printf '%s\n' "$(command -v "${candidate}")"
      return 0
    fi
  done
  return 1
}

node_version_of() {
  local node_bin="$1"
  "${node_bin}" -p 'process.versions.node'
}

find_node_bin() {
  local candidate
  local version
  candidate="$(command -v node 2>/dev/null || true)"
  [[ -n "${candidate}" ]] || return 1
  version="$(node_version_of "${candidate}" 2>/dev/null || true)"
  if [[ -n "${version}" ]] && version_gte "${version}" "${REQUIRED_NODE_VERSION}"; then
    printf '%s\n' "${candidate}"
    return 0
  fi
  return 1
}

detect_conda_base() {
  local candidates=(
    "${BLUEPRINT_EXECUTOR_CONDA_BASE:-}"
    "${CONDA_PREFIX:-}"
    "${HOME}/.local/share/blueprint-re/mamba"
    "${HOME}/miniconda3"
    "${HOME}/miniforge3"
    "${HOME}/anaconda3"
    "/opt/conda"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    [[ -n "${candidate}" ]] || continue
    if [[ -x "${candidate}/bin/conda" || -x "${candidate}/bin/micromamba" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

# Provision the bundled micromamba binary shipped in runtime/bin/micromamba.
# The target directory doubles as MAMBA_ROOT_PREFIX so envs land in a stable,
# resolver-discoverable location.
provision_bundled_mamba() {
  local bundled="${ROOT_DIR}/runtime/bin/micromamba"
  local target="${HOME}/.local/share/blueprint-re/mamba"
  [[ -x "${bundled}" ]] || return 1
  mkdir -p "${target}/bin" "${target}/envs"
  cp "${bundled}" "${target}/bin/micromamba"

  local preset="${BLUEPRINT_MIRROR_PRESET:-tsinghua}"
  if [[ -f "${ROOT_DIR}/runtime/mirror-presets/${preset}.mambarc" ]]; then
    cp "${ROOT_DIR}/runtime/mirror-presets/${preset}.mambarc" "${target}/.mambarc"
  elif [[ -f "${ROOT_DIR}/deploy/runtime/mirror-presets/${preset}.mambarc" ]]; then
    cp "${ROOT_DIR}/deploy/runtime/mirror-presets/${preset}.mambarc" "${target}/.mambarc"
  fi
  echo "root_prefix: ${target}" >> "${target}/.mambarc"

  export MAMBA_ROOT_PREFIX="${target}"
  export MAMBARC="${target}/.mambarc"
  printf '%s\n' "${target}"
}

provision_bundled_r_runtime() {
  local mamba_base="$1"
  local r_env="blueprint-re-r"
  local env_spec="${ROOT_DIR}/runtime/blueprint-re-r.yml"
  local local_pkgs="${ROOT_DIR}/runtime/pkgs"
  local micromamba_bin="${mamba_base}/bin/micromamba"
  [[ -x "${micromamba_bin}" && -f "${env_spec}" ]] || return 1

  local env_dir="${mamba_base}/envs/${r_env}"
  local marker_file="${env_dir}/.blueprint-re-r.build-info"
  local spec_hash
  spec_hash="$(sha256sum "${env_spec}" | awk '{print $1}')"
  local micromamba_version
  micromamba_version="$("${micromamba_bin}" --version 2>/dev/null || echo unknown)"
  local expected_marker="${spec_hash} ${micromamba_version}"

  if [[ -x "${env_dir}/bin/Rscript" ]]; then
    if [[ -f "${marker_file}" ]]; then
      if [[ "$(cat "${marker_file}" 2>/dev/null)" == "${expected_marker}" ]]; then
        printf '%s\n' "${r_env}"
        return 0
      fi
      echo "Bundled R runtime spec or micromamba version changed; rebuilding ${r_env}." >&2
    else
      echo "Bundled R runtime exists but build marker is missing; rebuilding ${r_env}." >&2
    fi
    "${micromamba_bin}" env remove -n "${r_env}" -y 2>/dev/null || true
  fi

  echo "Provisioning bundled R runtime (${r_env})... this may take 5-20 min." >&2
  export MAMBA_ROOT_PREFIX="${mamba_base}"
  export MAMBARC="${mamba_base}/.mambarc"

  local create_cmd=("${micromamba_bin}" create -y -n "${r_env}" -f "${env_spec}")
  if [[ -d "${local_pkgs}" ]] && [[ -n "$(ls -A "${local_pkgs}" 2>/dev/null)" ]]; then
    echo "Using embedded R package cache for offline install." >&2
    mkdir -p "${mamba_base}/pkgs"
    cp -a "${local_pkgs}/." "${mamba_base}/pkgs/"
    create_cmd+=(--offline)
  fi

  if ! "${create_cmd[@]}"; then
    echo "R runtime provisioning failed; cleaning partial env." >&2
    "${micromamba_bin}" env remove -n "${r_env}" -y 2>/dev/null || true
    return 1
  fi
  printf '%s %s\n' "${spec_hash}" "${micromamba_version}" > "${marker_file}"
  printf '%s\n' "${r_env}"
}

detect_default_python_runtime() {
  local conda_base="$1"
  [[ -n "${conda_base}" ]] || return 1
  local candidates=(omicverse analysis base)
  local name
  for name in "${candidates[@]}"; do
    if [[ "${name}" == "base" && -x "${conda_base}/bin/python" ]]; then
      printf '%s\n' "base"
      return 0
    fi
    if [[ -x "${conda_base}/envs/${name}/bin/python" ]]; then
      printf '%s\n' "${name}"
      return 0
    fi
  done
  return 1
}

detect_default_r_runtime() {
  local conda_base="$1"
  if [[ -n "${conda_base}" ]]; then
    local candidates=(bioconductor r-bio base)
    local name
    for name in "${candidates[@]}"; do
      if [[ "${name}" == "base" && -x "${conda_base}/bin/Rscript" ]]; then
        printf '%s\n' "base"
        return 0
      fi
      if [[ -x "${conda_base}/envs/${name}/bin/Rscript" ]]; then
        printf '%s\n' "${name}"
        return 0
      fi
    done
  fi
  if command -v Rscript >/dev/null 2>&1; then
    printf '%s\n' "__system__"
    return 0
  fi
  return 1
}

prompt_default() {
  local var_name="$1"
  local prompt_text="$2"
  local default_value="$3"
  local current_value="${!var_name:-}"
  if [[ "${INSTALL_INTERACTIVE}" -ne 1 ]]; then
    if [[ -n "${current_value}" ]]; then
      return
    fi
    printf -v "${var_name}" "%s" "${default_value}"
    return
  fi
  if [[ -n "${current_value}" ]]; then
    printf "%s [%s]: " "${prompt_text}" "${current_value}"
    read -r input || true
    if [[ -n "${input}" ]]; then
      printf -v "${var_name}" "%s" "${input}"
    fi
    return
  fi
  if [[ -n "${default_value}" ]]; then
    printf "%s [%s]: " "${prompt_text}" "${default_value}"
  else
    printf "%s: " "${prompt_text}"
  fi
  read -r input || true
  if [[ -n "${input}" ]]; then
    printf -v "${var_name}" "%s" "${input}"
  else
    printf -v "${var_name}" "%s" "${default_value}"
  fi
}

write_env_file() {
  umask 077
  cat > "${ENV_FILE}" <<EOF
BLUEPRINT_DEEPSEEK_API_BASE_URL=${BLUEPRINT_DEEPSEEK_API_BASE_URL}
BLUEPRINT_DEEPSEEK_API_KEY=${BLUEPRINT_DEEPSEEK_API_KEY}
BLUEPRINT_PI_DEEPSEEK_BASE_URL=${BLUEPRINT_PI_DEEPSEEK_BASE_URL}
BLUEPRINT_MANAGER_MODEL=${BLUEPRINT_MANAGER_MODEL}
BLUEPRINT_MANAGER_BACKEND=pi
BLUEPRINT_PI_MANAGER_URL=${BLUEPRINT_PI_MANAGER_URL}
BLUEPRINT_BACKEND_API_BASE_URL=${BLUEPRINT_BACKEND_API_BASE_URL}
BLUEPRINT_INTERNAL_TOOL_TOKEN=${BLUEPRINT_INTERNAL_TOOL_TOKEN}
BLUEPRINT_MANAGER_TEMPERATURE=${BLUEPRINT_MANAGER_TEMPERATURE}
BLUEPRINT_MANAGER_MAX_TOKENS=${BLUEPRINT_MANAGER_MAX_TOKENS}
BLUEPRINT_MANAGER_TIMEOUT_SECONDS=${BLUEPRINT_MANAGER_TIMEOUT_SECONDS}
BLUEPRINT_DEFAULT_WORKER_TYPE=pi
BLUEPRINT_EXECUTOR_MODEL=${BLUEPRINT_EXECUTOR_MODEL}
BLUEPRINT_REVIEWER_MODEL=${BLUEPRINT_REVIEWER_MODEL}
BLUEPRINT_LIBRARY_SUMMARIZER_MODEL=${BLUEPRINT_LIBRARY_SUMMARIZER_MODEL}
BLUEPRINT_REVIEWER_MAX_TOKENS=${BLUEPRINT_REVIEWER_MAX_TOKENS}
BLUEPRINT_REVIEWER_MAX_TURNS=${BLUEPRINT_REVIEWER_MAX_TURNS}
BLUEPRINT_EXECUTOR_SANDBOX_MODE=${BLUEPRINT_EXECUTOR_SANDBOX_MODE}
BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS=${BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS}
BLUEPRINT_EXECUTOR_CONDA_BASE=${BLUEPRINT_EXECUTOR_CONDA_BASE}
BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX=${MAMBA_ROOT_PREFIX:-${BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX:-${BLUEPRINT_EXECUTOR_CONDA_BASE:-}}}
BLUEPRINT_EXECUTOR_MAMBARC=${MAMBARC:-${BLUEPRINT_EXECUTOR_MAMBARC:-}}
BLUEPRINT_DEFAULT_PYTHON_RUNTIME=${BLUEPRINT_DEFAULT_PYTHON_RUNTIME}
BLUEPRINT_DEFAULT_R_RUNTIME=${BLUEPRINT_DEFAULT_R_RUNTIME}
BLUEPRINT_EXECUTOR_HOST_ROOT_READONLY=${BLUEPRINT_EXECUTOR_HOST_ROOT_READONLY}
BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS=${BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS}
BLUEPRINT_MIRROR_PRESET=${BLUEPRINT_MIRROR_PRESET}
BLUEPRINT_CRAN_MIRROR=${BLUEPRINT_CRAN_MIRROR:-}
BLUEPRINT_BIOCONDUCTOR_MIRROR=${BLUEPRINT_BIOCONDUCTOR_MIRROR:-}
BLUEPRINT_PYPI_MIRROR=${BLUEPRINT_PYPI_MIRROR:-}
MANAGER_WEBSEARCH_ENABLED=${MANAGER_WEBSEARCH_ENABLED}
TAVILY_API_KEY=${TAVILY_API_KEY}
TAVILY_BASE_URL=${TAVILY_BASE_URL}
MANAGER_CONTEXT_WINDOW_TOKENS=${MANAGER_CONTEXT_WINDOW_TOKENS}
MANAGER_COMPACTION_ENABLED=${MANAGER_COMPACTION_ENABLED}
MANAGER_COMPACTION_KEEP_RECENT_TOKENS=${MANAGER_COMPACTION_KEEP_RECENT_TOKENS}
MANAGER_COMPACTION_RESERVE_TOKENS=${MANAGER_COMPACTION_RESERVE_TOKENS}
EOF
}

# Main execution guard: allow sourcing this script for behavior-based tests.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

PYTHON_BIN="$(find_python_bin 2>/dev/null || true)"
NODE_BIN="$(find_node_bin 2>/dev/null || true)"

if [[ -z "${BLUEPRINT_EXECUTOR_CONDA_BASE:-}" ]]; then
  BLUEPRINT_EXECUTOR_CONDA_BASE="$(
    detect_conda_base 2>/dev/null \
    || provision_bundled_mamba 2>/dev/null \
    || true
  )"
fi
if [[ -z "${BLUEPRINT_DEFAULT_PYTHON_RUNTIME:-}" ]]; then
  BLUEPRINT_DEFAULT_PYTHON_RUNTIME="$(detect_default_python_runtime "${BLUEPRINT_EXECUTOR_CONDA_BASE:-}" 2>/dev/null || true)"
fi
if [[ -z "${BLUEPRINT_DEFAULT_R_RUNTIME:-}" ]]; then
  BLUEPRINT_DEFAULT_R_RUNTIME="$(
    detect_default_r_runtime "${BLUEPRINT_EXECUTOR_CONDA_BASE:-}" 2>/dev/null \
    || provision_bundled_r_runtime "${BLUEPRINT_EXECUTOR_CONDA_BASE:-}" 2>/dev/null \
    || true
  )"
fi

printf "Blueprint RE installer\n"
printf "Root: %s\n\n" "${ROOT_DIR}"
printf "Managed deployment requires BLUEPRINT_DEEPSEEK_API_KEY.\n"
printf "TAVILY_API_KEY remains optional and can be configured later.\n\n"
if [[ -z "${PYTHON_BIN}" ]]; then
  printf "Required backend Python: %s+\n" "${REQUIRED_PYTHON_VERSION}"
  printf "Detected python3: %s\n\n" "$(python3 --version 2>/dev/null || echo missing)"
else
  printf "Detected backend Python: %s (%s)\n" "$("${PYTHON_BIN}" --version 2>/dev/null)" "${PYTHON_BIN}"
fi
if [[ -z "${NODE_BIN}" ]]; then
  printf "Required Node.js: %s+\n" "${REQUIRED_NODE_VERSION}"
  printf "Detected node: %s\n\n" "$(node -v 2>/dev/null || echo missing)"
else
  printf "Detected Node.js: %s (%s)\n\n" "$("${NODE_BIN}" -v 2>/dev/null)" "${NODE_BIN}"
fi
if [[ "${INSTALL_INTERACTIVE}" -eq 1 ]]; then
  printf "Mode: interactive\n\n"
else
  printf "Mode: non-interactive defaults (pass --interactive to edit values during install)\n\n"
fi

prompt_default BLUEPRINT_DEEPSEEK_API_KEY "DeepSeek API key (required)" "${BLUEPRINT_DEEPSEEK_API_KEY:-}"

prompt_default BLUEPRINT_DEEPSEEK_API_BASE_URL "DeepSeek Anthropic-compatible base URL" "${BLUEPRINT_DEEPSEEK_API_BASE_URL:-https://api.deepseek.com/anthropic}"
prompt_default BLUEPRINT_PI_DEEPSEEK_BASE_URL "DeepSeek native base URL for Pi executor" "${BLUEPRINT_PI_DEEPSEEK_BASE_URL:-https://api.deepseek.com}"
prompt_default BLUEPRINT_MANAGER_MODEL "Manager model" "${BLUEPRINT_MANAGER_MODEL:-deepseek-v4-pro}"
prompt_default BLUEPRINT_EXECUTOR_MODEL "Executor model" "${BLUEPRINT_EXECUTOR_MODEL:-deepseek-v4-flash}"
prompt_default BLUEPRINT_REVIEWER_MODEL "Reviewer model" "${BLUEPRINT_REVIEWER_MODEL:-deepseek-v4-flash}"
prompt_default BLUEPRINT_LIBRARY_SUMMARIZER_MODEL "Library summarizer model" "${BLUEPRINT_LIBRARY_SUMMARIZER_MODEL:-deepseek-v4-flash}"
prompt_default BLUEPRINT_REVIEWER_MAX_TOKENS "Reviewer max tokens" "${BLUEPRINT_REVIEWER_MAX_TOKENS:-2400}"
prompt_default BLUEPRINT_REVIEWER_MAX_TURNS "Reviewer max turns" "${BLUEPRINT_REVIEWER_MAX_TURNS:-24}"
prompt_default BLUEPRINT_MANAGER_TEMPERATURE "Manager temperature" "${BLUEPRINT_MANAGER_TEMPERATURE:-0.2}"
prompt_default BLUEPRINT_MANAGER_MAX_TOKENS "Manager max tokens" "${BLUEPRINT_MANAGER_MAX_TOKENS:-2400}"
prompt_default BLUEPRINT_MANAGER_TIMEOUT_SECONDS "Manager timeout seconds" "${BLUEPRINT_MANAGER_TIMEOUT_SECONDS:-600}"
BLUEPRINT_DEFAULT_WORKER_TYPE="pi"
prompt_default BLUEPRINT_EXECUTOR_SANDBOX_MODE "Executor sandbox mode" "${BLUEPRINT_EXECUTOR_SANDBOX_MODE:-bwrap}"
prompt_default BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS "Max concurrent executor runs" "${BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS:-3}"
prompt_default BLUEPRINT_EXECUTOR_CONDA_BASE "Conda base path" "${BLUEPRINT_EXECUTOR_CONDA_BASE:-/home/${USER}/miniconda3}"
prompt_default BLUEPRINT_MIRROR_PRESET "Mirror preset (tsinghua/ustc/default)" "${BLUEPRINT_MIRROR_PRESET:-tsinghua}"
prompt_default BLUEPRINT_DEFAULT_PYTHON_RUNTIME "Default Python runtime" "${BLUEPRINT_DEFAULT_PYTHON_RUNTIME:-}"
prompt_default BLUEPRINT_DEFAULT_R_RUNTIME "Default R runtime" "${BLUEPRINT_DEFAULT_R_RUNTIME:-}"
prompt_default BLUEPRINT_EXECUTOR_HOST_ROOT_READONLY "Host root read-only" "${BLUEPRINT_EXECUTOR_HOST_ROOT_READONLY:-true}"
prompt_default BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS "Extra read-only binds (comma-separated)" "${BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS:-/home/${USER}/.nvm,/home/${USER}/.local}"
prompt_default MANAGER_WEBSEARCH_ENABLED "Enable manager web search (true/false)" "${MANAGER_WEBSEARCH_ENABLED:-false}"
if [[ "${MANAGER_WEBSEARCH_ENABLED}" == "true" ]]; then
  prompt_default TAVILY_API_KEY "Tavily API key" "${TAVILY_API_KEY:-}"
else
  TAVILY_API_KEY="${TAVILY_API_KEY:-}"
fi
prompt_default TAVILY_BASE_URL "Tavily base URL" "${TAVILY_BASE_URL:-https://api.tavily.com}"
prompt_default MANAGER_CONTEXT_WINDOW_TOKENS "Manager context window tokens" "${MANAGER_CONTEXT_WINDOW_TOKENS:-1000000}"
prompt_default MANAGER_COMPACTION_ENABLED "Enable automatic compaction (true/false)" "${MANAGER_COMPACTION_ENABLED:-true}"
prompt_default MANAGER_COMPACTION_KEEP_RECENT_TOKENS "Compaction keep-recent tokens" "${MANAGER_COMPACTION_KEEP_RECENT_TOKENS:-120000}"
prompt_default MANAGER_COMPACTION_RESERVE_TOKENS "Compaction reserve tokens" "${MANAGER_COMPACTION_RESERVE_TOKENS:-16000}"

# Re-expand mirror preset in case interactive mode changed it.
_expand_mirror_preset "${BLUEPRINT_MIRROR_PRESET}"

TOKEN_PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 2>/dev/null || true)}"
if [[ -z "${TOKEN_PYTHON_BIN}" ]]; then
  echo "Python ${REQUIRED_PYTHON_VERSION}+ is required to generate install secrets and deploy the backend." >&2
  exit 1
fi

BLUEPRINT_INTERNAL_TOOL_TOKEN="${BLUEPRINT_INTERNAL_TOOL_TOKEN:-$("${TOKEN_PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)}"

BLUEPRINT_PI_MANAGER_URL="${BLUEPRINT_PI_MANAGER_URL:-http://127.0.0.1:18002}"
BLUEPRINT_BACKEND_API_BASE_URL="${BLUEPRINT_BACKEND_API_BASE_URL:-http://127.0.0.1:18001/api}"

write_env_file

echo
echo "Wrote ${ENV_FILE}"
echo "Detected conda base: ${BLUEPRINT_EXECUTOR_CONDA_BASE:-<none>}"
echo "Detected default Python runtime: ${BLUEPRINT_DEFAULT_PYTHON_RUNTIME:-<none>}"
echo "Detected default R runtime: ${BLUEPRINT_DEFAULT_R_RUNTIME:-<none>}"

if [[ -z "${BLUEPRINT_DEEPSEEK_API_KEY:-}" ]]; then
  echo
  echo "ERROR: BLUEPRINT_DEEPSEEK_API_KEY is required for managed deployment." >&2
  echo "Set it in ${ENV_FILE} or environment, then rerun this script." >&2
  exit 1
fi

echo "Starting full deploy..."

bash "${ROOT_DIR}/scripts/deploy_user_systemd.sh" --allow-apt

echo
echo "Install complete."
echo "Frontend: http://127.0.0.1:13001  (nginx gateway)"
echo "Backend:  http://127.0.0.1:18001"
echo "Next.js:  http://127.0.0.1:13002  (internal)"
echo "Runtime config files:"
echo "  backend.env      -> ~/.config/blueprint-re/backend.env"
echo "  manager-agent.env -> ~/.config/blueprint-re/manager-agent.env"
echo "  frontend.env     -> ~/.config/blueprint-re/frontend.env"
echo "Note: editing ${ENV_FILE} requires rerunning deploy to update running services."
fi  # end main execution guard
