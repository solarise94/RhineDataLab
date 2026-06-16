#!/usr/bin/env bash
set -euo pipefail

# Release deploy entrypoint.
#
# Responsibilities:
#   - consume an already prepared runtime environment
#   - generate env files
#   - install service units
#   - restart services
#
# This script does NOT call apt-get. Runtime dependencies must already be
# present (installed by the installer or manually by the operator).
#
# Environment:
#   BLUEPRINT_RELEASE_ROOT    default: ~/.local/share/blueprint-re/current
#   BLUEPRINT_DATA_ROOT       default: ~/.local/share/blueprint-re/data
#   BLUEPRINT_PYTHON_BIN      explicit Python path (default: search PATH)
#   BLUEPRINT_NODE_BIN        explicit Node path (default: search PATH)
#   BLUEPRINT_NPM_BIN         explicit npm path (default: search PATH)
#   BLUEPRINT_NGINX_BIN       explicit nginx path (default: search PATH)
#   BLUEPRINT_BWRAP_BIN       explicit bwrap path (default: search PATH)
#   BLUEPRINT_EXECUTOR_CONDA_BASE  default: auto-detected
#   BLUEPRINT_DEEPSEEK_API_KEY     optional (runtime credential source)
#
# Usage:
#   bash scripts/deploy_release.sh [--upgrade] [--services-stopped] [--allow-apt]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_ROOT="${BLUEPRINT_RELEASE_ROOT:-${HOME}/.local/share/blueprint-re/current}"
DATA_ROOT="${BLUEPRINT_DATA_ROOT:-${HOME}/.local/share/blueprint-re/data}"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
APP_ENV_DIR="${HOME}/.config/blueprint-re"

REQUIRED_PYTHON_VERSION="3.13.0"
REQUIRED_NODE_VERSION="22.19.0"

IS_UPGRADE=0
SERVICES_STOPPED=0
ALLOW_APT=0
DEPLOY_WARNINGS=()

# Restrict permissions for generated files.
umask 077

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

die() {
  echo "ERROR: $1" >&2
  exit 1
}

warn_deploy() {
  DEPLOY_WARNINGS+=("$1")
}

ensure_user_service_enabled() {
  local service_name="$1"
  if ! systemctl --user reenable "${service_name}" >/dev/null 2>&1; then
    systemctl --user enable "${service_name}" >/dev/null 2>&1 || \
      die "Failed to enable ${service_name}"
  fi
  systemctl --user is-enabled "${service_name}" >/dev/null 2>&1 || \
    die "Service ${service_name} is not enabled after deploy"
}

version_gte() {
  local actual="$1"
  local required="$2"
  [[ "$(printf '%s\n%s\n' "${required}" "${actual}" | sort -V | head -n1)" == "${required}" ]]
}

python_version_of() {
  local python_bin="$1"
  "${python_bin}" -c 'import sys; print(".".join(str(part) for part in sys.version_info[:3]))'
}

node_version_of() {
  local node_bin="$1"
  "${node_bin}" -p 'process.versions.node'
}

# Resolve binary: explicit env var > command -v.
resolve_bin() {
  local env_var_name="$1"
  local command_name="$2"
  local explicit
  explicit="${!env_var_name:-}"
  if [[ -n "${explicit}" && -x "${explicit}" ]]; then
    printf '%s\n' "${explicit}"
    return 0
  fi
  if command -v "${command_name}" >/dev/null 2>&1; then
    printf '%s\n' "$(command -v "${command_name}")"
    return 0
  fi
  return 1
}

# Escape a value for a systemd EnvironmentFile.
# systemd parses EnvironmentFile as shell-like syntax. We:
#   - reject literal newlines
#   - escape backslashes and double quotes
#   - wrap in double quotes when the value contains spaces/special chars
systemd_env_escape() {
  local value="$1"
  if [[ "${value}" == *$'\n'* ]]; then
    die "Refusing to write env value containing a newline"
  fi
  local escaped="${value//\\/\\\\}"
  escaped="${escaped//\"/\\\"}"
  # Quote if contains spaces, tabs, semicolons, pipes, or variable expansions.
  if [[ "${escaped}" == *[[:space:]]* || "${escaped}" == *';'* || "${escaped}" == *'|'* || "${escaped}" == *'$'* ]]; then
    escaped="\"${escaped}\""
  fi
  printf '%s\n' "${escaped}"
}

# Escape a literal string for safe use as the replacement side of sed s|...|...|.
sed_escape_replacement() {
  local value="$1"
  # Escape backslash first, then the delimiter, then '&'.
  value="${value//\\/\\\\}"
  value="${value//\|/\\|}"
  value="${value//\&/\\&}"
  # Remove literal newlines (they cannot be embedded in a sed replacement).
  value="${value//$'\n'/}"
  printf '%s\n' "${value}"
}

# Write a key=value line to an env file with proper escaping.
write_env_line() {
  local file="$1"
  local key="$2"
  local value="$3"
  printf '%s=%s\n' "${key}" "$(systemd_env_escape "${value}")" >> "${file}"
}

# Render a template file by replacing __NAME__ markers with escaped values.
render_template() {
  local template="$1"
  local output="$2"
  shift 2
  local -a replacements=("$@")
  cp "${template}" "${output}"
  local entry
  for entry in "${replacements[@]}"; do
    local marker="${entry%%=*}"
    local value="${entry#*=}"
    local escaped_value
    escaped_value="$(sed_escape_replacement "${value}")"
    sed -i -e "s|${marker}|${escaped_value}|g" "${output}"
  done
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

for arg in "$@"; do
  case "${arg}" in
    --upgrade)
      IS_UPGRADE=1
      ;;
    --services-stopped)
      SERVICES_STOPPED=1
      ;;
    --allow-apt)
      ALLOW_APT=1
      ;;
    *)
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

PYTHON_BIN=""
NODE_BIN=""
NPM_BIN=""
NGINX_BIN=""
BWRAP_BIN=""
GIT_BIN=""

PYTHON_BIN="$(resolve_bin BLUEPRINT_PYTHON_BIN python3)" || die "Python not found. Set BLUEPRINT_PYTHON_BIN."
NODE_BIN="$(resolve_bin BLUEPRINT_NODE_BIN node)" || die "Node.js not found. Set BLUEPRINT_NODE_BIN."
NPM_BIN="$(resolve_bin BLUEPRINT_NPM_BIN npm)" || die "npm not found. Set BLUEPRINT_NPM_BIN."
NGINX_BIN="$(resolve_bin BLUEPRINT_NGINX_BIN nginx)" || die "nginx not found. Set BLUEPRINT_NGINX_BIN."
BWRAP_BIN="$(resolve_bin BLUEPRINT_BWRAP_BIN bwrap)" || die "bwrap not found. Set BLUEPRINT_BWRAP_BIN."
GIT_BIN="$(resolve_bin BLUEPRINT_GIT_BIN git)" || die "git not found. Set BLUEPRINT_GIT_BIN."

# Validate versions
python_version="$(python_version_of "${PYTHON_BIN}")"
if ! version_gte "${python_version}" "${REQUIRED_PYTHON_VERSION}"; then
  die "Python ${REQUIRED_PYTHON_VERSION}+ required. Found ${python_version} at ${PYTHON_BIN}"
fi

node_version="$(node_version_of "${NODE_BIN}")"
if ! version_gte "${node_version}" "${REQUIRED_NODE_VERSION}"; then
  die "Node.js ${REQUIRED_NODE_VERSION}+ required. Found ${node_version} at ${NODE_BIN}"
fi

echo "Resolved binaries:"
echo "  Python:  ${PYTHON_BIN} (${python_version})"
echo "  Node:    ${NODE_BIN} (${node_version})"
echo "  npm:     ${NPM_BIN}"
echo "  nginx:   ${NGINX_BIN}"
echo "  bwrap:   ${BWRAP_BIN}"
echo "  git:     ${GIT_BIN}"

# ---------------------------------------------------------------------------
# Runtime dependency checks
# ---------------------------------------------------------------------------

if ! "${BWRAP_BIN}" \
  --die-with-parent \
  --ro-bind /usr /usr \
  --ro-bind /bin /bin \
  --ro-bind-try /lib /lib \
  --ro-bind-try /lib64 /lib64 \
  --proc /proc \
  --dev /dev \
  --tmpfs /tmp \
  -- /bin/true; then
  die "bubblewrap smoke test failed. Fix host namespace/setuid support before deploying Blueprint executors."
fi

if ! systemctl --user show-environment >/dev/null 2>&1; then
  die "systemd --user is not available in the current session."
fi

# ---------------------------------------------------------------------------
# Release root validation
# ---------------------------------------------------------------------------

if [[ ! -d "${RELEASE_ROOT}" ]]; then
  die "Release root does not exist: ${RELEASE_ROOT}"
fi

if [[ ! -f "${RELEASE_ROOT}/release.json" ]]; then
  die "release.json not found in release root: ${RELEASE_ROOT}"
fi

RELEASE_VERSION="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version','unknown'))" "${RELEASE_ROOT}/release.json")"
echo "Deploying release version: ${RELEASE_VERSION}"

# ---------------------------------------------------------------------------
# Detect optional executors
# ---------------------------------------------------------------------------

find_optional_bin() {
  local name="$1"
  command -v "${name}" 2>/dev/null || true
}

PI_BIN="$(find_optional_bin pi)"
OPENCODE_BIN="$(find_optional_bin opencode)"
CLAUDE_BIN="$(find_optional_bin claude)"
CODEX_BIN="$(find_optional_bin codex)"

[[ -n "${PI_BIN}" ]] && echo "Pi CLI:      ${PI_BIN}" || echo "Pi CLI:      not found (optional)"
[[ -n "${OPENCODE_BIN}" ]] && echo "OpenCode:    ${OPENCODE_BIN}" || echo "OpenCode:    not found (optional)"
[[ -n "${CLAUDE_BIN}" ]] && echo "Claude Code: ${CLAUDE_BIN}" || echo "Claude Code: not found (optional)"
[[ -n "${CODEX_BIN}" ]] && echo "Codex:       ${CODEX_BIN}" || echo "Codex:       not found (optional)"

# ---------------------------------------------------------------------------
# Conda base detection
# ---------------------------------------------------------------------------

# shellcheck source=lib_runtime_detect.sh
source "${SCRIPT_DIR}/lib_runtime_detect.sh"

CONDA_BASE=""
DEFAULT_PYTHON_RUNTIME=""
DEFAULT_R_RUNTIME=""
if CONDA_BASE="$(detect_conda_base 2>/dev/null)"; then
  DEFAULT_PYTHON_RUNTIME="$(detect_default_python_runtime "${CONDA_BASE}" 2>/dev/null || true)"
  DEFAULT_R_RUNTIME="$(detect_default_r_runtime "${CONDA_BASE}" 2>/dev/null || true)"
  # If the detected base is the bundled micromamba root, derive the bwrap
  # MAMBA_ROOT_PREFIX/MAMBARC settings unless explicitly configured.
  if [[ -x "${CONDA_BASE}/bin/micromamba" && -z "${BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX:-}" ]]; then
    BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX="${CONDA_BASE}"
    if [[ -f "${CONDA_BASE}/.mambarc" && -z "${BLUEPRINT_EXECUTOR_MAMBARC:-}" ]]; then
      BLUEPRINT_EXECUTOR_MAMBARC="${CONDA_BASE}/.mambarc"
    fi
  fi
else
  warn_deploy "No conda base detected. Python/R runtime defaults may be empty unless explicitly configured."
  DEFAULT_R_RUNTIME="$(detect_default_r_runtime "" 2>/dev/null || true)"
fi
if [[ -z "${BLUEPRINT_DEFAULT_PYTHON_RUNTIME:-}" && -z "${DEFAULT_PYTHON_RUNTIME}" ]]; then
  warn_deploy "No default Python runtime detected. Cards without an explicit python_runtime may require manual runtime selection."
fi
if [[ -z "${BLUEPRINT_DEFAULT_R_RUNTIME:-}" && -z "${DEFAULT_R_RUNTIME}" ]]; then
  warn_deploy "No default R runtime detected. R-capable cards may require BLUEPRINT_DEFAULT_R_RUNTIME or system Rscript."
fi

# ---------------------------------------------------------------------------
# Verify backend wheel is importable (do not reinstall; install.sh owns that)
# ---------------------------------------------------------------------------

WHEEL_PATH_IN_RELEASE="$("${PYTHON_BIN}" -c "
import json, sys
manifest = json.load(open(sys.argv[1]))
print(manifest.get('artifacts', {}).get('backend_wheel', {}).get('path', ''))
" "${RELEASE_ROOT}/release.json")"

if [[ -z "${WHEEL_PATH_IN_RELEASE}" ]]; then
  die "Backend wheel path not found in release.json"
fi

WHEEL_FILE="${RELEASE_ROOT}/${WHEEL_PATH_IN_RELEASE}"
if [[ ! -f "${WHEEL_FILE}" ]]; then
  die "Backend wheel not found at release path: ${WHEEL_PATH_IN_RELEASE}"
fi

if ! "${PYTHON_BIN}" -c "import app.main" >/dev/null 2>&1; then
  die "Backend package cannot be imported from ${PYTHON_BIN}. Did install.sh install the wheel into this environment?"
fi
echo "Backend package verified: $(basename "${WHEEL_FILE}")"

# ---------------------------------------------------------------------------
# Install manager-agent dependencies
# ---------------------------------------------------------------------------

if [[ -d "${RELEASE_ROOT}/manager-agent" ]]; then
  if [[ -d "${RELEASE_ROOT}/manager-agent/node_modules" ]]; then
    echo "Manager-agent node_modules already bundled; skipping npm install."
  else
    echo "Installing manager-agent dependencies..."
    pushd "${RELEASE_ROOT}/manager-agent" >/dev/null
    if [[ -f package-lock.json ]]; then
      "${NPM_BIN}" ci
    else
      "${NPM_BIN}" install
    fi
    popd >/dev/null
  fi
else
  warn_deploy "Manager-agent source not found in release."
fi

# ---------------------------------------------------------------------------
# Internal tool token
# ---------------------------------------------------------------------------

INTERNAL_TOOL_TOKEN="${BLUEPRINT_INTERNAL_TOOL_TOKEN:-$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)}"

# ---------------------------------------------------------------------------
# Check unknown env keys
# ---------------------------------------------------------------------------

# If a .env file exists in the current working directory (source-mode fallback),
# warn about keys that will not be forwarded.
check_unknown_blueprint_env_keys() {
  local env_file="$(pwd)/.env"
  [[ -f "${env_file}" ]] || return 0
  local known_keys=(
    BLUEPRINT_ANTHROPIC_API_BASE_URL
    BLUEPRINT_ANTHROPIC_API_KEY
    BLUEPRINT_BACKEND_API_BASE_URL
    BLUEPRINT_BIOCONDUCTOR_MIRROR
    BLUEPRINT_CLAUDE_CODE_COMMAND_JSON
    BLUEPRINT_CODEX_COMMAND_JSON
    BLUEPRINT_CRAN_MIRROR
    BLUEPRINT_DATA_DIRECTORY_ROOTS
    BLUEPRINT_DATA_MOUNT_HASH_LIMIT_BYTES
    BLUEPRINT_DATA_ROOT
    BLUEPRINT_DEEPSEEK_API_BASE_URL
    BLUEPRINT_DEEPSEEK_API_KEY
    BLUEPRINT_DEFAULT_PYTHON_RUNTIME
    BLUEPRINT_DEFAULT_R_RUNTIME
    BLUEPRINT_DEFAULT_WORKER_TYPE
    BLUEPRINT_EXECUTOR_CONDA_BASE
    BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS
    BLUEPRINT_EXECUTOR_HOST_ROOT_READONLY
    BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX
    BLUEPRINT_EXECUTOR_MAMBARC
    BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS
    BLUEPRINT_EXECUTOR_MODEL
    BLUEPRINT_EXECUTOR_SANDBOX_MODE
    BLUEPRINT_INTERNAL_TOOL_TOKEN
    BLUEPRINT_LIBRARY_SUMMARIZER_MODEL
    BLUEPRINT_MANAGER_API_BASE_URL
    BLUEPRINT_MANAGER_API_KEY
    BLUEPRINT_MANAGER_BACKEND
    BLUEPRINT_MANAGER_MAX_TOKENS
    BLUEPRINT_MANAGER_MODEL
    BLUEPRINT_MANAGER_TEMPERATURE
    BLUEPRINT_MANAGER_TIMEOUT_SECONDS
    BLUEPRINT_MANIFEST_REPAIR_TIMEOUT_SECONDS
    BLUEPRINT_OPENAI_API_BASE_URL
    BLUEPRINT_OPENAI_API_KEY
    BLUEPRINT_OPENCODE_API_BASE_URL
    BLUEPRINT_OPENCODE_API_KEY
    BLUEPRINT_OPENCODE_API_PROTOCOL
    BLUEPRINT_OPENCODE_COMMAND_JSON
    BLUEPRINT_OPENCODE_EXECUTOR_MODEL
    BLUEPRINT_PI_ANTHROPIC_BASE_URL
    BLUEPRINT_PI_API_KEY
    BLUEPRINT_PI_COMMAND_JSON
    BLUEPRINT_PI_DEEPSEEK_BASE_URL
    BLUEPRINT_PI_EXECUTOR_MODEL
    BLUEPRINT_PI_MANAGER_URL
    BLUEPRINT_PROJECT_ROOTS
    BLUEPRINT_PYPI_MIRROR
    BLUEPRINT_REVIEWER_API_BASE_URL
    BLUEPRINT_REVIEWER_API_KEY
    BLUEPRINT_REVIEWER_MAX_TOKENS
    BLUEPRINT_REVIEWER_MAX_TURNS
    BLUEPRINT_REVIEWER_MODEL
    BLUEPRINT_RUNTIME_DEPENDENCY_CACHE_TTL_SECONDS
    BLUEPRINT_RUNTIME_DEPENDENCY_FALLBACK_POLICY
    BLUEPRINT_RUNTIME_DEPENDENCY_PROBE_TIMEOUT_SECONDS
    BLUEPRINT_WORKER_TIMEOUT_SECONDS
  )
  local -A known_set=()
  local known_key
  for known_key in "${known_keys[@]}"; do
    known_set["${known_key}"]=1
  done
  local line key
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -n "${line}" && "${line}" != \#* && "${line}" == BLUEPRINT_*"="* ]] || continue
    key="${line%%=*}"
    if [[ -z "${known_set[$key]+x}" ]]; then
      warn_deploy "Unknown BLUEPRINT_* key in .env will not be written to managed env files: ${key}"
    fi
  done < "${env_file}"
}

check_unknown_blueprint_env_keys

if [[ -z "${BLUEPRINT_CRAN_MIRROR:-}" && -z "${BLUEPRINT_BIOCONDUCTOR_MIRROR:-}" && -z "${BLUEPRINT_PYPI_MIRROR:-}" ]]; then
  # Installers normally export these; fall back to deriving from the release bundle preset.
  _mirror_preset="${BLUEPRINT_MIRROR_PRESET:-tsinghua}"
  _mirror_env_sh="${RELEASE_ROOT}/runtime/mirror-presets/mirror_env.sh"
  if [[ -f "${_mirror_env_sh}" ]]; then
    # shellcheck disable=SC1091
    source "${_mirror_env_sh}"
    BLUEPRINT_CRAN_MIRROR="${BLUEPRINT_CRAN_MIRROR:-$(eval echo "\${${_mirror_preset}_cran_mirror:-}")}"
    BLUEPRINT_BIOCONDUCTOR_MIRROR="${BLUEPRINT_BIOCONDUCTOR_MIRROR:-$(eval echo "\${${_mirror_preset}_bioconductor_mirror:-}")}"
    BLUEPRINT_PYPI_MIRROR="${BLUEPRINT_PYPI_MIRROR:-$(eval echo "\${${_mirror_preset}_pypi_mirror:-}")}"
  fi
fi

# ---------------------------------------------------------------------------
# Env file generation (whitelist-based)
# ---------------------------------------------------------------------------

mkdir -p "${APP_ENV_DIR}" "${SYSTEMD_USER_DIR}" "${DATA_ROOT}"

# Warn if critical manager key is missing; do not block deploy.
# Runtime credential sources (user env, secrets manager, etc.) may provide it.
if [[ -z "${BLUEPRINT_DEEPSEEK_API_KEY:-}" ]]; then
  warn_deploy "BLUEPRINT_DEEPSEEK_API_KEY not set. Manager-agent will require runtime credentials to function."
fi

DEFAULT_EXTRA_RO_BINDS="${HOME}/.nvm,${HOME}/.local"
DEFAULT_PI_COMMAND_JSON="[\"bash\",\"${RELEASE_ROOT}/scripts/blueprint_pi_launch.sh\",\"{executor_prompt_path}\"]"
DEFAULT_OPENCODE_COMMAND_JSON="[\"${OPENCODE_BIN:-opencode}\",\"run\",\"--file\",\"{executor_prompt_path}\",\"--format\",\"json\",\"--dangerously-skip-permissions\",\"Read {executor_prompt_path} and complete the Blueprint executor contract exactly.\"]"
DEFAULT_CLAUDE_CODE_COMMAND_JSON="[\"${CLAUDE_BIN:-claude}\",\"-p\",\"@{executor_prompt_path}\",\"--output-format\",\"stream-json\",\"--verbose\"]"

# Build PATH safely: prefer the release env, then node dir, then user local,
# then an optional conda base. Never inject /bin when CONDA_BASE is empty.
BUILD_PATH="$(dirname "${PYTHON_BIN}"):${HOME}/.local/bin"
if [[ -n "${CONDA_BASE}" ]]; then
  BUILD_PATH="${BUILD_PATH}:${CONDA_BASE}/bin"
fi
BUILD_PATH="${BUILD_PATH}:${PATH}"

# Release deploy always requires bwrap; override any unsafe value.
SANDBOX_MODE="bwrap"
if [[ "${BLUEPRINT_EXECUTOR_SANDBOX_MODE:-bwrap}" != "bwrap" ]]; then
  warn_deploy "Overriding BLUEPRINT_EXECUTOR_SANDBOX_MODE=${BLUEPRINT_EXECUTOR_SANDBOX_MODE:-} to bwrap for release deployment"
fi

# ---------------------------------------------------------------------------
# Upgrade credential retention
# ---------------------------------------------------------------------------
# During upgrade, preserve existing credential lines in generated env files
# when the caller does not provide replacement values. This prevents an
# upgrade launched without env vars from silently stripping working credentials.
_preserve_credentials_from_backup() {
  local new_file="$1"
  local backup_file="$2"
  shift 2
  local key
  for key in "$@"; do
    if ! grep -q "^${key}=" "${new_file}" 2>/dev/null; then
      grep "^${key}=" "${backup_file}" 2>/dev/null >> "${new_file}" || true
    fi
  done
}

BACKEND_ENV_BACKUP=""
MANAGER_ENV_BACKUP=""

_cleanup_env_backups() {
  [[ -n "${BACKEND_ENV_BACKUP}" ]] && rm -f "${BACKEND_ENV_BACKUP}"
  [[ -n "${MANAGER_ENV_BACKUP}" ]] && rm -f "${MANAGER_ENV_BACKUP}"
  return 0
}
trap '_cleanup_env_backups' EXIT

if [[ "${IS_UPGRADE}" -eq 1 ]]; then
  if [[ -f "${APP_ENV_DIR}/backend.env" ]]; then
    BACKEND_ENV_BACKUP="$(mktemp)"
    cp "${APP_ENV_DIR}/backend.env" "${BACKEND_ENV_BACKUP}"
  fi
  if [[ -f "${APP_ENV_DIR}/manager-agent.env" ]]; then
    MANAGER_ENV_BACKUP="$(mktemp)"
    cp "${APP_ENV_DIR}/manager-agent.env" "${MANAGER_ENV_BACKUP}"
  fi
fi

: > "${APP_ENV_DIR}/backend.env"
write_env_line "${APP_ENV_DIR}/backend.env" PATH "${BUILD_PATH}"
write_env_line "${APP_ENV_DIR}/backend.env" BACKEND_HOST "127.0.0.1"
write_env_line "${APP_ENV_DIR}/backend.env" BACKEND_PORT "18001"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_FRONTEND_ORIGIN "http://127.0.0.1:13001"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_MANAGER_BACKEND "pi"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_DEFAULT_WORKER_TYPE "pi"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_PI_MANAGER_URL "http://127.0.0.1:18002"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_BACKEND_API_BASE_URL "http://127.0.0.1:18001/api"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_DATA_ROOT "${DATA_ROOT}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_DEFAULT_PYTHON_RUNTIME "${BLUEPRINT_DEFAULT_PYTHON_RUNTIME:-${DEFAULT_PYTHON_RUNTIME}}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_DEFAULT_R_RUNTIME "${BLUEPRINT_DEFAULT_R_RUNTIME:-${DEFAULT_R_RUNTIME}}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_EXECUTOR_CONDA_BASE "${BLUEPRINT_EXECUTOR_CONDA_BASE:-${CONDA_BASE}}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_BWRAP_BIN "${BWRAP_BIN}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX "${BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX:-${MAMBA_ROOT_PREFIX:-}}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_EXECUTOR_MAMBARC "${BLUEPRINT_EXECUTOR_MAMBARC:-${MAMBARC:-}}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_CRAN_MIRROR "${BLUEPRINT_CRAN_MIRROR:-}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_BIOCONDUCTOR_MIRROR "${BLUEPRINT_BIOCONDUCTOR_MIRROR:-}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_PYPI_MIRROR "${BLUEPRINT_PYPI_MIRROR:-}"
# Only write provider credentials when explicitly provided.
# Setting to an empty string explicitly clears the old value on upgrade.
if [[ -n "${BLUEPRINT_DEEPSEEK_API_KEY:-}" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_DEEPSEEK_API_KEY "${BLUEPRINT_DEEPSEEK_API_KEY}"
elif [[ "${BLUEPRINT_DEEPSEEK_API_KEY+set}" == "set" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_DEEPSEEK_API_KEY ""
fi
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_DEEPSEEK_API_BASE_URL "${BLUEPRINT_DEEPSEEK_API_BASE_URL:-https://api.deepseek.com/anthropic}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_PI_DEEPSEEK_BASE_URL "${BLUEPRINT_PI_DEEPSEEK_BASE_URL:-https://api.deepseek.com}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_MANAGER_MODEL "${BLUEPRINT_MANAGER_MODEL:-deepseek-v4-pro}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_MANAGER_TEMPERATURE "${BLUEPRINT_MANAGER_TEMPERATURE:-0.2}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_MANAGER_MAX_TOKENS "${BLUEPRINT_MANAGER_MAX_TOKENS:-2400}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_MANAGER_TIMEOUT_SECONDS "${BLUEPRINT_MANAGER_TIMEOUT_SECONDS:-600}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_EXECUTOR_MODEL "${BLUEPRINT_EXECUTOR_MODEL:-deepseek-v4-flash}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_REVIEWER_MODEL "${BLUEPRINT_REVIEWER_MODEL:-deepseek-v4-flash}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_LIBRARY_SUMMARIZER_MODEL "${BLUEPRINT_LIBRARY_SUMMARIZER_MODEL:-deepseek-v4-flash}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_REVIEWER_MAX_TOKENS "${BLUEPRINT_REVIEWER_MAX_TOKENS:-2400}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_REVIEWER_MAX_TURNS "${BLUEPRINT_REVIEWER_MAX_TURNS:-24}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_EXECUTOR_SANDBOX_MODE "${SANDBOX_MODE}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS "${BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS:-3}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_EXECUTOR_HOST_ROOT_READONLY "${BLUEPRINT_EXECUTOR_HOST_ROOT_READONLY:-true}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS "${BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS-${DEFAULT_EXTRA_RO_BINDS}}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_INTERNAL_TOOL_TOKEN "${INTERNAL_TOOL_TOKEN}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_PI_COMMAND_JSON "${BLUEPRINT_PI_COMMAND_JSON:-${DEFAULT_PI_COMMAND_JSON}}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_OPENCODE_COMMAND_JSON "${BLUEPRINT_OPENCODE_COMMAND_JSON:-${DEFAULT_OPENCODE_COMMAND_JSON}}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_CLAUDE_CODE_COMMAND_JSON "${BLUEPRINT_CLAUDE_CODE_COMMAND_JSON:-${DEFAULT_CLAUDE_CODE_COMMAND_JSON}}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_PROJECT_ROOTS "${BLUEPRINT_PROJECT_ROOTS:-}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_DATA_DIRECTORY_ROOTS "${BLUEPRINT_DATA_DIRECTORY_ROOTS:-}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_DATA_MOUNT_HASH_LIMIT_BYTES "${BLUEPRINT_DATA_MOUNT_HASH_LIMIT_BYTES:-104857600}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_WORKER_TIMEOUT_SECONDS "${BLUEPRINT_WORKER_TIMEOUT_SECONDS:-1800}"
write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_MANIFEST_REPAIR_TIMEOUT_SECONDS "${BLUEPRINT_MANIFEST_REPAIR_TIMEOUT_SECONDS:-180}"

# Optional API keys and URLs: forward only if set so the backend uses its defaults.
# Setting to an empty string explicitly clears the old value on upgrade.
if [[ -n "${BLUEPRINT_ANTHROPIC_API_KEY:-}" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_ANTHROPIC_API_KEY "${BLUEPRINT_ANTHROPIC_API_KEY}"
elif [[ "${BLUEPRINT_ANTHROPIC_API_KEY+set}" == "set" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_ANTHROPIC_API_KEY ""
fi
if [[ -n "${BLUEPRINT_ANTHROPIC_API_BASE_URL:-}" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_ANTHROPIC_API_BASE_URL "${BLUEPRINT_ANTHROPIC_API_BASE_URL}"
elif [[ "${BLUEPRINT_ANTHROPIC_API_BASE_URL+set}" == "set" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_ANTHROPIC_API_BASE_URL ""
fi
if [[ -n "${BLUEPRINT_OPENAI_API_KEY:-}" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_OPENAI_API_KEY "${BLUEPRINT_OPENAI_API_KEY}"
elif [[ "${BLUEPRINT_OPENAI_API_KEY+set}" == "set" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_OPENAI_API_KEY ""
fi
if [[ -n "${BLUEPRINT_OPENAI_API_BASE_URL:-}" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_OPENAI_API_BASE_URL "${BLUEPRINT_OPENAI_API_BASE_URL}"
elif [[ "${BLUEPRINT_OPENAI_API_BASE_URL+set}" == "set" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_OPENAI_API_BASE_URL ""
fi
if [[ -n "${BLUEPRINT_OPENCODE_API_KEY:-}" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_OPENCODE_API_KEY "${BLUEPRINT_OPENCODE_API_KEY}"
elif [[ "${BLUEPRINT_OPENCODE_API_KEY+set}" == "set" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_OPENCODE_API_KEY ""
fi
if [[ -n "${BLUEPRINT_OPENCODE_API_BASE_URL:-}" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_OPENCODE_API_BASE_URL "${BLUEPRINT_OPENCODE_API_BASE_URL}"
elif [[ "${BLUEPRINT_OPENCODE_API_BASE_URL+set}" == "set" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_OPENCODE_API_BASE_URL ""
fi
if [[ -n "${BLUEPRINT_OPENCODE_API_PROTOCOL:-}" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_OPENCODE_API_PROTOCOL "${BLUEPRINT_OPENCODE_API_PROTOCOL}"
elif [[ "${BLUEPRINT_OPENCODE_API_PROTOCOL+set}" == "set" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_OPENCODE_API_PROTOCOL ""
fi

# Codex is manual-only: forward if present.
if [[ -n "${BLUEPRINT_CODEX_COMMAND_JSON:-}" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_CODEX_COMMAND_JSON "${BLUEPRINT_CODEX_COMMAND_JSON}"
elif [[ "${BLUEPRINT_CODEX_COMMAND_JSON+set}" == "set" ]]; then
  write_env_line "${APP_ENV_DIR}/backend.env" BLUEPRINT_CODEX_COMMAND_JSON ""
fi

chmod 600 "${APP_ENV_DIR}/backend.env"

# Restore credentials missing from the new file during upgrade.
if [[ -n "${BACKEND_ENV_BACKUP}" ]]; then
  _preserve_credentials_from_backup "${APP_ENV_DIR}/backend.env" "${BACKEND_ENV_BACKUP}" \
    BLUEPRINT_DEEPSEEK_API_KEY \
    BLUEPRINT_ANTHROPIC_API_KEY \
    BLUEPRINT_OPENAI_API_KEY \
    BLUEPRINT_OPENCODE_API_KEY \
    BLUEPRINT_OPENCODE_API_BASE_URL \
    BLUEPRINT_OPENCODE_API_PROTOCOL \
    BLUEPRINT_CODEX_COMMAND_JSON
  rm -f "${BACKEND_ENV_BACKUP}"
fi

: > "${APP_ENV_DIR}/manager-agent.env"
write_env_line "${APP_ENV_DIR}/manager-agent.env" MANAGER_AGENT_HOST "127.0.0.1"
write_env_line "${APP_ENV_DIR}/manager-agent.env" MANAGER_AGENT_PORT "18002"
write_env_line "${APP_ENV_DIR}/manager-agent.env" MANAGER_AGENT_PROVIDER "deepseek"
write_env_line "${APP_ENV_DIR}/manager-agent.env" MANAGER_AGENT_MODEL "${BLUEPRINT_MANAGER_MODEL:-deepseek-v4-pro}"
write_env_line "${APP_ENV_DIR}/manager-agent.env" MANAGER_AGENT_TIMEOUT_MS "600000"
# Only write provider credentials when explicitly provided.
# Setting to an empty string explicitly clears the old value on upgrade.
if [[ -n "${BLUEPRINT_DEEPSEEK_API_KEY:-}" ]]; then
  write_env_line "${APP_ENV_DIR}/manager-agent.env" BLUEPRINT_DEEPSEEK_API_KEY "${BLUEPRINT_DEEPSEEK_API_KEY}"
elif [[ "${BLUEPRINT_DEEPSEEK_API_KEY+set}" == "set" ]]; then
  write_env_line "${APP_ENV_DIR}/manager-agent.env" BLUEPRINT_DEEPSEEK_API_KEY ""
fi
write_env_line "${APP_ENV_DIR}/manager-agent.env" BLUEPRINT_INTERNAL_TOOL_TOKEN "${INTERNAL_TOOL_TOKEN}"
write_env_line "${APP_ENV_DIR}/manager-agent.env" MANAGER_WEBSEARCH_ENABLED "${MANAGER_WEBSEARCH_ENABLED:-}"
if [[ -n "${TAVILY_API_KEY:-}" ]]; then
  write_env_line "${APP_ENV_DIR}/manager-agent.env" TAVILY_API_KEY "${TAVILY_API_KEY}"
elif [[ "${TAVILY_API_KEY+set}" == "set" ]]; then
  write_env_line "${APP_ENV_DIR}/manager-agent.env" TAVILY_API_KEY ""
fi
write_env_line "${APP_ENV_DIR}/manager-agent.env" TAVILY_BASE_URL "${TAVILY_BASE_URL:-https://api.tavily.com}"
write_env_line "${APP_ENV_DIR}/manager-agent.env" MANAGER_CONTEXT_WINDOW_TOKENS "${MANAGER_CONTEXT_WINDOW_TOKENS:-1000000}"
write_env_line "${APP_ENV_DIR}/manager-agent.env" MANAGER_COMPACTION_ENABLED "${MANAGER_COMPACTION_ENABLED:-true}"
write_env_line "${APP_ENV_DIR}/manager-agent.env" MANAGER_COMPACTION_KEEP_RECENT_TOKENS "${MANAGER_COMPACTION_KEEP_RECENT_TOKENS:-120000}"
write_env_line "${APP_ENV_DIR}/manager-agent.env" MANAGER_COMPACTION_RESERVE_TOKENS "${MANAGER_COMPACTION_RESERVE_TOKENS:-16000}"

chmod 600 "${APP_ENV_DIR}/manager-agent.env"

# Restore credentials missing from the new file during upgrade.
if [[ -n "${MANAGER_ENV_BACKUP}" ]]; then
  _preserve_credentials_from_backup "${APP_ENV_DIR}/manager-agent.env" "${MANAGER_ENV_BACKUP}" \
    BLUEPRINT_DEEPSEEK_API_KEY \
    TAVILY_API_KEY
  rm -f "${MANAGER_ENV_BACKUP}"
fi

: > "${APP_ENV_DIR}/frontend.env"
write_env_line "${APP_ENV_DIR}/frontend.env" FRONTEND_HOST "127.0.0.1"
write_env_line "${APP_ENV_DIR}/frontend.env" FRONTEND_PORT "13002"
write_env_line "${APP_ENV_DIR}/frontend.env" NEXT_PUBLIC_API_BASE_URL "/api"
write_env_line "${APP_ENV_DIR}/frontend.env" NEXT_PUBLIC_UPLOAD_API_BASE_URL "/upload-api"
write_env_line "${APP_ENV_DIR}/frontend.env" BACKEND_PROXY_TARGET "http://127.0.0.1:18001"

chmod 600 "${APP_ENV_DIR}/frontend.env"

# ---------------------------------------------------------------------------
# Service unit generation
# ---------------------------------------------------------------------------

echo "Generating systemd service units..."

# Ensure the release-specific templates exist.
SYSTEMD_TEMPLATE_DIR="${RELEASE_ROOT}/deploy"
if [[ ! -f "${SYSTEMD_TEMPLATE_DIR}/blueprint-re-backend.service" ]]; then
  die "Service template not found: ${SYSTEMD_TEMPLATE_DIR}/blueprint-re-backend.service"
fi

ENV_DIR="$(dirname "$(dirname "${PYTHON_BIN}")")"

render_template "${SYSTEMD_TEMPLATE_DIR}/blueprint-re-backend.service" \
  "${SYSTEMD_USER_DIR}/blueprint-re-backend.service" \
  "__RELEASE_ROOT__=${RELEASE_ROOT}" \
  "__ENV__=${ENV_DIR}" \
  "__NODE_BIN__=${NODE_BIN}"

render_template "${SYSTEMD_TEMPLATE_DIR}/blueprint-re-frontend.service" \
  "${SYSTEMD_USER_DIR}/blueprint-re-frontend.service" \
  "__RELEASE_ROOT__=${RELEASE_ROOT}" \
  "__NODE_BIN__=${NODE_BIN}"

render_template "${SYSTEMD_TEMPLATE_DIR}/blueprint-re-manager-agent.service" \
  "${SYSTEMD_USER_DIR}/blueprint-re-manager-agent.service" \
  "__RELEASE_ROOT__=${RELEASE_ROOT}" \
  "__NODE_BIN__=${NODE_BIN}"

mkdir -p "${APP_ENV_DIR}/nginx-tmp/body" "${APP_ENV_DIR}/nginx-tmp/proxy" \
         "${APP_ENV_DIR}/nginx-tmp/fastcgi" "${APP_ENV_DIR}/nginx-tmp/uwsgi" \
         "${APP_ENV_DIR}/nginx-tmp/scgi"

render_template "${RELEASE_ROOT}/deploy/blueprint-re.conf.template" \
  "${APP_ENV_DIR}/nginx.conf" \
  "__APP_ENV_DIR__=${APP_ENV_DIR}"

render_template "${SYSTEMD_TEMPLATE_DIR}/blueprint-re-nginx.service" \
  "${SYSTEMD_USER_DIR}/blueprint-re-nginx.service" \
  "__NGINX_BIN__=${NGINX_BIN}" \
  "__APP_ENV_DIR__=${APP_ENV_DIR}"

# ---------------------------------------------------------------------------
# Service management
# ---------------------------------------------------------------------------

systemctl --user daemon-reload
ensure_user_service_enabled blueprint-re-manager-agent.service
ensure_user_service_enabled blueprint-re-backend.service
ensure_user_service_enabled blueprint-re-frontend.service
ensure_user_service_enabled blueprint-re-nginx.service

if [[ "${IS_UPGRADE}" -eq 1 ]]; then
  if [[ "${SERVICES_STOPPED}" -eq 1 ]]; then
    echo "Upgrade mode: services already stopped by caller."
  else
    echo "Upgrade mode: stopping services..."
    systemctl --user stop blueprint-re-nginx.service || true
    systemctl --user stop blueprint-re-frontend.service || true
    systemctl --user stop blueprint-re-backend.service || true
    systemctl --user stop blueprint-re-manager-agent.service || true
    sleep 2
  fi
  echo "Starting services..."
  systemctl --user start blueprint-re-manager-agent.service
  systemctl --user start blueprint-re-backend.service
  systemctl --user start blueprint-re-frontend.service
  systemctl --user start blueprint-re-nginx.service
else
  # Initial install: restart backend/manager, start frontend/nginx
  systemctl --user stop blueprint-re-nginx.service || true
  systemctl --user stop blueprint-re-frontend.service || true
  systemctl --user restart blueprint-re-manager-agent.service
  systemctl --user restart blueprint-re-backend.service
  systemctl --user start blueprint-re-frontend.service
  systemctl --user start blueprint-re-nginx.service
fi

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

echo ""
echo "Running health checks..."
sleep 3

HEALTH_OK=1

_http_check() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "${url}" >/dev/null 2>&1
  elif "${PYTHON_BIN}" -c "import urllib.request" >/dev/null 2>&1; then
    "${PYTHON_BIN}" -c "import urllib.request; urllib.request.urlopen('${url}', timeout=2)" >/dev/null 2>&1
  else
    return 1
  fi
}

if ! _http_check http://127.0.0.1:18001/healthz; then
  warn_deploy "Backend health check failed (http://127.0.0.1:18001/healthz)"
  HEALTH_OK=0
fi

if ! _http_check http://127.0.0.1:13001; then
  warn_deploy "nginx gateway check failed (http://127.0.0.1:13001)"
  HEALTH_OK=0
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "========================================"
echo "Blueprint RE deployed."
echo "Version:  ${RELEASE_VERSION}"
echo "Release:  ${RELEASE_ROOT}"
echo "Data:     ${DATA_ROOT}"
echo ""
echo "Frontend: http://127.0.0.1:13001  (nginx gateway)"
echo "Backend:  http://127.0.0.1:18001"
echo "Next.js:  http://127.0.0.1:13002  (internal)"
echo "========================================"

if [[ -n "${CONDA_BASE}" ]]; then
  echo "Conda base: ${CONDA_BASE}"
fi
if [[ -n "${DEFAULT_PYTHON_RUNTIME}" ]]; then
  echo "Default Python runtime: ${DEFAULT_PYTHON_RUNTIME}"
fi
if [[ -n "${DEFAULT_R_RUNTIME}" ]]; then
  echo "Default R runtime: ${DEFAULT_R_RUNTIME}"
fi

if [[ "${#DEPLOY_WARNINGS[@]}" -gt 0 ]]; then
  echo ""
  echo "Deploy warnings:"
  for warning in "${DEPLOY_WARNINGS[@]}"; do
    echo "  - ${warning}"
  done
fi

if [[ "${HEALTH_OK}" -eq 0 ]]; then
  echo ""
  echo "WARNING: One or more health checks failed. Check service status with:"
  echo "  systemctl --user status blueprint-re-backend.service"
  echo "  systemctl --user status blueprint-re-manager-agent.service"
  echo "  systemctl --user status blueprint-re-frontend.service"
  echo "  systemctl --user status blueprint-re-nginx.service"
  exit 1
fi

echo ""
echo "Deploy complete."
exit 0
