#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
APP_ENV_DIR="${HOME}/.config/blueprint-re"
APP_RELEASE_DIR="${HOME}/.local/share/blueprint-re"
DEPLOY_MARKERS_DIR="${APP_RELEASE_DIR}/.deploy-markers"
FRONTEND_RELEASE_DIR="${APP_RELEASE_DIR}/frontend-release"
REQUIRED_PYTHON_VERSION="3.13.0"
REQUIRED_NODE_VERSION="22.19.0"
NODE_BIN=""
PYTHON_BIN=""
PI_BIN=""
OPENCODE_BIN=""
CLAUDE_BIN=""
NGINX_BIN=""
DEPLOY_WARNINGS=()
ALLOW_APT=0
DEPLOY_MODE="build-if-changed"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
for arg in "$@"; do
  case "${arg}" in
    --allow-apt)
      ALLOW_APT=1
      ;;
    --mode=*)
      DEPLOY_MODE="${arg#--mode=}"
      ;;
    *)
      ;;
  esac
done

case "${DEPLOY_MODE}" in
  restart-only|build-if-changed|full-rebuild)
    ;;
  *)
    echo "Invalid deploy mode: ${DEPLOY_MODE}" >&2
    echo "Usage: $0 [--mode=restart-only|build-if-changed|full-rebuild] [--allow-apt]" >&2
    exit 1
    ;;
esac

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

find_optional_bin() {
  local name="$1"
  command -v "${name}" 2>/dev/null || true
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

find_nginx_bin() {
  local candidate
  for candidate in nginx /usr/sbin/nginx /usr/local/sbin/nginx; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      printf '%s\n' "$(command -v "${candidate}")"
      return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------------------
# Deploy-marker helpers
# ---------------------------------------------------------------------------
_marker_path() {
  echo "${DEPLOY_MARKERS_DIR}/${1}.hash"
}

_compute_hash() {
  local python_bin="${PYTHON_BIN:-python3}"
  local out=""
  out="$("${python_bin}" - "${ROOT_DIR}" "$@" <<'PY'
import sys, os, hashlib, glob
root = sys.argv[1]
args = sys.argv[2:]
files = []
for arg in args:
    if '*' in arg or '?' in arg:
        pattern = os.path.join(root, arg)
        paths = glob.glob(pattern, recursive=True)
    else:
        path = os.path.join(root, arg)
        if not os.path.exists(path):
            continue
        paths = [path]
    for p in paths:
        if os.path.isfile(p):
            files.append(p)
        elif os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames.sort()
                for fn in filenames:
                    files.append(os.path.join(dirpath, fn))
files = sorted(set(os.path.normpath(p) for p in files))
h = hashlib.sha256()
for p in files:
    rel = os.path.relpath(p, root)
    h.update(rel.encode('utf-8'))
    h.update(b'\x00')
    try:
        with open(p, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        sys.exit(1)
    h.update(b'\x00')
print(h.hexdigest())
PY
  )" || { echo ""; return 0; }
  printf '%s\n' "${out}"
}

_marker_changed() {
  local name="$1"
  shift
  local marker_path
  local current=""
  local new
  marker_path="$(_marker_path "${name}")"
  if [[ -f "${marker_path}" ]]; then
    current="$(cat "${marker_path}" 2>/dev/null || true)"
  fi
  new="$(_compute_hash "$@")"
  if [[ -z "${new}" ]]; then
    return 0
  fi
  if [[ "${current}" != "${new}" ]]; then
    return 0
  fi
  return 1
}

_write_marker() {
  local name="$1"
  shift
  local hash
  mkdir -p "${DEPLOY_MARKERS_DIR}"
  hash="$(_compute_hash "$@")"
  printf '%s\n' "${hash}" > "$(_marker_path "${name}")"
}

disable_system_nginx_service() {
  local system_nginx_active=0
  if [[ "$(id -u)" -eq 0 ]]; then
    systemctl is-active nginx >/dev/null 2>&1 && system_nginx_active=1
  elif command -v sudo >/dev/null 2>&1; then
    sudo -n systemctl is-active nginx >/dev/null 2>&1 && system_nginx_active=1
  fi

  if [[ "${system_nginx_active}" -eq 0 ]]; then
    return 0
  fi

  local disabled=0
  if [[ "$(id -u)" -eq 0 ]]; then
    systemctl disable --now nginx 2>/dev/null && disabled=1
  elif command -v sudo >/dev/null 2>&1; then
    sudo -n systemctl disable --now nginx 2>/dev/null && disabled=1
  fi

  if [[ "${disabled}" -eq 0 ]]; then
    warn_deploy "System-level nginx is active but could not be disabled. It may conflict with the Blueprint user-level gateway on port 13001."
  fi
}

warn_deploy() {
  DEPLOY_WARNINGS+=("$1")
}

# Print processes listening on a given TCP port, one per line.
# Falls back to netstat if ss is unavailable.
_list_port_owners() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -tlnp 2>/dev/null | grep -E ":${port}[[:space:]]" || true
  elif command -v netstat >/dev/null 2>&1; then
    netstat -tlnp 2>/dev/null | grep -E ":${port}[[:space:]]" || true
  fi
}

# Report which of the Blueprint ports are currently listening.
# Returns 0 if any port is occupied, 1 if all are free.
_report_port_occupants() {
  local label="$1"
  local port
  local owners
  local any=0
  for port in 18001 18002 13001 13002; do
    owners="$(_list_port_owners "${port}")"
    if [[ -n "${owners}" ]]; then
      if [[ "${any}" -eq 0 ]]; then
        echo "${label}"
        any=1
      fi
      echo "  Port ${port}:"
      echo "${owners}" | sed 's/^/    /'
    fi
  done
  if [[ "${any}" -eq 1 ]]; then
    return 0
  fi
  return 1
}

# Check that a URL returns a body containing the expected substring.
# Prefer curl; fall back to Python urllib.
_http_body_check() {
  local url="$1"
  local expected="$2"
  local body
  if command -v curl >/dev/null 2>&1; then
    body="$(curl -fsS "${url}" 2>/dev/null || true)"
  elif "${PYTHON_BIN}" -c "import urllib.request" >/dev/null 2>&1; then
    body="$("${PYTHON_BIN}" -c "import urllib.request; print(urllib.request.urlopen('${url}', timeout=2).read().decode('utf-8'))" 2>/dev/null || true)"
  else
    return 1
  fi
  [[ -n "${body}" && "${body}" == *"${expected}"* ]]
}

# Check that a URL's response headers contain the expected substring.
# Matching is case-insensitive. Prefer curl -I; fall back to Python urllib.
_http_header_check() {
  local url="$1"
  local expected="$2"
  local headers
  if command -v curl >/dev/null 2>&1; then
    headers="$(curl -fsSI "${url}" 2>/dev/null || true)"
  elif "${PYTHON_BIN}" -c "import urllib.request" >/dev/null 2>&1; then
    headers="$("${PYTHON_BIN}" -c "
import urllib.request
resp = urllib.request.urlopen('${url}', timeout=2)
print('\\n'.join(f'{k}: {v}' for k, v in resp.headers.items()))
" 2>/dev/null || true)"
  else
    return 1
  fi
  [[ -n "${headers}" && "${headers,,}" == *"${expected,,}"* ]]
}

# Check whether a user-level systemd service is active.
_is_service_active() {
  systemctl --user is-active "$1" >/dev/null 2>&1
}

check_unknown_blueprint_env_keys() {
  local env_file="${ROOT_DIR}/.env"
  [[ -f "${env_file}" ]] || return 0
  local known_keys=(
    BLUEPRINT_BACKEND_API_BASE_URL
    BLUEPRINT_CLAUDE_CODE_COMMAND_JSON
    BLUEPRINT_CODEX_COMMAND_JSON
    BLUEPRINT_DEEPSEEK_API_BASE_URL
    BLUEPRINT_DEEPSEEK_API_KEY
    BLUEPRINT_DEFAULT_PYTHON_RUNTIME
    BLUEPRINT_DEFAULT_R_RUNTIME
    BLUEPRINT_DEFAULT_WORKER_TYPE
    BLUEPRINT_EXECUTOR_CONDA_BASE
    BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS
    BLUEPRINT_EXECUTOR_HOST_ROOT_READONLY
    BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS
    BLUEPRINT_EXECUTOR_MODEL
    BLUEPRINT_EXECUTOR_SANDBOX_MODE
    BLUEPRINT_INTERNAL_TOOL_TOKEN
    BLUEPRINT_LIBRARY_SUMMARIZER_MODEL
    BLUEPRINT_MANAGER_BACKEND
    BLUEPRINT_MANAGER_MAX_TOKENS
    BLUEPRINT_MANAGER_MODEL
    BLUEPRINT_MANAGER_TEMPERATURE
    BLUEPRINT_MANAGER_TIMEOUT_SECONDS
    BLUEPRINT_OPENCODE_COMMAND_JSON
    BLUEPRINT_PI_COMMAND_JSON
    BLUEPRINT_PI_DEEPSEEK_BASE_URL
    BLUEPRINT_PI_MANAGER_URL
    BLUEPRINT_REVIEWER_MAX_TOKENS
    BLUEPRINT_REVIEWER_MAX_TURNS
    BLUEPRINT_REVIEWER_MODEL
    BLUEPRINT_RUNTIME_DEPENDENCY_FALLBACK_POLICY
    BLUEPRINT_RUNTIME_DEPENDENCY_PROBE_TIMEOUT_SECONDS
    BLUEPRINT_RUNTIME_DEPENDENCY_CACHE_TTL_SECONDS
    BLUEPRINT_PROJECT_ROOTS
    BLUEPRINT_DATA_DIRECTORY_ROOTS
    BLUEPRINT_DATA_MOUNT_HASH_LIMIT_BYTES
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

detect_conda_base() {
  local candidates=(
    "${BLUEPRINT_EXECUTOR_CONDA_BASE:-}"
    "${CONDA_PREFIX:-}"
    "${HOME}/miniconda3"
    "${HOME}/miniforge3"
    "${HOME}/anaconda3"
    "/opt/conda"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    [[ -n "${candidate}" ]] || continue
    if [[ -x "${candidate}/bin/conda" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

detect_default_python_runtime() {
  local conda_base="$1"
  local env_name="${BLUEPRINT_DEFAULT_PYTHON_RUNTIME:-}"
  if [[ -n "${env_name}" ]]; then
    printf '%s\n' "${env_name}"
    return 0
  fi
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
  local env_name="${BLUEPRINT_DEFAULT_R_RUNTIME:-}"
  if [[ -n "${env_name}" ]]; then
    printf '%s\n' "${env_name}"
    return 0
  fi
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

mkdir -p "${SYSTEMD_USER_DIR}" "${APP_ENV_DIR}" "${APP_RELEASE_DIR}" "${DEPLOY_MARKERS_DIR}"

install_runtime_dependencies() {
  local missing_runtime=0
  local venv_python_bin=""
  for command_name in bwrap npm git systemctl; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
      missing_runtime=1
    fi
  done
  if ! find_nginx_bin >/dev/null 2>&1; then
    missing_runtime=1
  fi
  if ! venv_python_bin="$(find_python_bin 2>/dev/null)"; then
    missing_runtime=1
  elif ! "${venv_python_bin}" -m venv "${ROOT_DIR}/.venv/deploy-smoke" >/dev/null 2>&1; then
    missing_runtime=1
  fi
  rm -rf "${ROOT_DIR}/.venv/deploy-smoke"
  if [[ "${missing_runtime}" -eq 0 ]]; then
    return
  fi
  if [[ "${ALLOW_APT}" -eq 0 ]]; then
    echo "Missing runtime dependencies. Install them manually or rerun with --allow-apt." >&2
    echo "See deploy/runtime-dependencies.yml for the required packages." >&2
    exit 1
  fi
  if command -v apt-get >/dev/null 2>&1; then
    if [[ "$(id -u)" -eq 0 ]]; then
      apt-get update
      apt-get install -y bubblewrap python3 python3-venv python3-pip nodejs npm git systemd nginx
    elif command -v sudo >/dev/null 2>&1; then
      sudo apt-get update
      sudo apt-get install -y bubblewrap python3 python3-venv python3-pip nodejs npm git systemd nginx
    else
      echo "Missing runtime dependencies from deploy/runtime-dependencies.yml and sudo is unavailable." >&2
      exit 1
    fi
  else
    echo "Automatic dependency installation currently supports apt-based hosts only." >&2
    echo "Install the required packages from deploy/runtime-dependencies.yml, then rerun this script." >&2
    exit 1
  fi
}

check_runtime_dependencies() {
  local bwrap_bin
  bwrap_bin="$(resolve_bin BLUEPRINT_BWRAP_BIN bwrap)" || { echo "Missing required runtime command: bwrap. See deploy/runtime-dependencies.yml." >&2; exit 1; }
  for command_name in npm git systemctl; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
      echo "Missing required runtime command: ${command_name}. See deploy/runtime-dependencies.yml." >&2
      exit 1
    fi
  done
  NGINX_BIN="$(resolve_bin BLUEPRINT_NGINX_BIN nginx)" || { echo "Missing required runtime command: nginx. See deploy/runtime-dependencies.yml." >&2; exit 1; }
  PYTHON_BIN="$(resolve_bin BLUEPRINT_PYTHON_BIN python3)" || { echo "Missing required Python ${REQUIRED_PYTHON_VERSION}+ runtime." >&2; exit 1; }
  local python_version
  python_version="$(python_version_of "${PYTHON_BIN}")"
  if ! version_gte "${python_version}" "${REQUIRED_PYTHON_VERSION}"; then
    echo "Python ${REQUIRED_PYTHON_VERSION}+ required. Found ${python_version} at ${PYTHON_BIN}" >&2
    exit 1
  fi
  if ! "${bwrap_bin}" \
    --die-with-parent \
    --ro-bind /usr /usr \
    --ro-bind /bin /bin \
    --ro-bind-try /lib /lib \
    --ro-bind-try /lib64 /lib64 \
    --proc /proc \
    --dev /dev \
    --tmpfs /tmp \
    -- /bin/true; then
    echo "bubblewrap smoke test failed. Fix host namespace/setuid support before deploying Blueprint executors." >&2
    exit 1
  fi
}

check_language_versions() {
  local python_version
  local node_version
  PYTHON_BIN="$(resolve_bin BLUEPRINT_PYTHON_BIN python3)" || { echo "Python ${REQUIRED_PYTHON_VERSION}+ is required for the backend." >&2; exit 1; }
  python_version="$(python_version_of "${PYTHON_BIN}")"
  if ! version_gte "${python_version}" "${REQUIRED_PYTHON_VERSION}"; then
    echo "Python ${REQUIRED_PYTHON_VERSION}+ required. Found ${python_version} at ${PYTHON_BIN}" >&2
    exit 1
  fi
  NODE_BIN="$(resolve_bin BLUEPRINT_NODE_BIN node)" || { echo "Node.js ${REQUIRED_NODE_VERSION}+ is required." >&2; exit 1; }
  node_version="$(node_version_of "${NODE_BIN}")"
  if ! version_gte "${node_version}" "${REQUIRED_NODE_VERSION}"; then
    echo "Node.js ${REQUIRED_NODE_VERSION}+ required. Found ${node_version} at ${NODE_BIN}" >&2
    exit 1
  fi
  node_version="$(node_version_of "${NODE_BIN}")"
  PI_BIN="$(find_optional_bin pi)"
  OPENCODE_BIN="$(find_optional_bin opencode)"
  CLAUDE_BIN="$(find_optional_bin claude)"
  if ! NGINX_BIN="$(resolve_bin BLUEPRINT_NGINX_BIN nginx)"; then
    echo "nginx not found. Install nginx for the upload gateway." >&2
    exit 1
  fi
  echo "Using Python ${python_version} via ${PYTHON_BIN}"
  echo "Using Node ${node_version} via ${NODE_BIN}"
  echo "Using nginx via ${NGINX_BIN}"
  [[ -n "${PI_BIN}" ]] && echo "Pi CLI found: ${PI_BIN}" || echo "Pi CLI not found (optional)"
  [[ -n "${OPENCODE_BIN}" ]] && echo "OpenCode found: ${OPENCODE_BIN}" || echo "OpenCode not found (optional)"
  [[ -n "${CLAUDE_BIN}" ]] && echo "Claude Code found: ${CLAUDE_BIN}" || echo "Claude Code not found (optional)"
}

check_systemd_user() {
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "systemd --user is not available in the current session." >&2
    echo "Log into a full user session or enable a user manager before deploying Blueprint RE." >&2
    exit 1
  fi
}

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi
check_unknown_blueprint_env_keys

if [[ "${DEPLOY_MODE}" != "restart-only" ]]; then
  install_runtime_dependencies
  check_runtime_dependencies
  check_language_versions
  # Disable the system-level nginx service so only the user-level gateway is active.
  disable_system_nginx_service
  check_systemd_user
fi

if [[ "${DEPLOY_MODE}" == "restart-only" ]]; then
  echo "restart-only: skipping builds and config regeneration."
fi

CONDA_BASE=""
DEFAULT_PYTHON_RUNTIME=""
DEFAULT_R_RUNTIME=""
if [[ "${DEPLOY_MODE}" != "restart-only" ]]; then
  if CONDA_BASE="$(detect_conda_base 2>/dev/null)"; then
    DEFAULT_PYTHON_RUNTIME="$(detect_default_python_runtime "${CONDA_BASE}" 2>/dev/null || true)"
    DEFAULT_R_RUNTIME="$(detect_default_r_runtime "${CONDA_BASE}" 2>/dev/null || true)"
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

  # Backend Python dependencies
  if [[ "${DEPLOY_MODE}" == "full-rebuild" ]] || [[ ! -x "${ROOT_DIR}/.venv/backend/bin/python" ]] || _marker_changed backend-install backend/pyproject.toml backend/uv.lock; then
    "${PYTHON_BIN}" -m venv "${ROOT_DIR}/.venv/backend"
    "${ROOT_DIR}/.venv/backend/bin/pip" install --upgrade pip
    "${ROOT_DIR}/.venv/backend/bin/pip" install -e "${ROOT_DIR}/backend"
    _write_marker backend-install backend/pyproject.toml backend/uv.lock
  else
    echo "Backend dependencies unchanged; skipping pip install."
  fi

  # Fail fast if critical manager key is missing in production deploy
  if [[ -z "${BLUEPRINT_DEEPSEEK_API_KEY:-}" ]]; then
    echo "BLUEPRINT_DEEPSEEK_API_KEY is required for production deployment." >&2
    echo "Set it in .env or environment before running this script." >&2
    exit 1
  fi

  INTERNAL_TOOL_TOKEN="${BLUEPRINT_INTERNAL_TOOL_TOKEN:-$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
  )}"
  DEFAULT_EXTRA_RO_BINDS="${HOME}/.nvm,${HOME}/.local"
  DEFAULT_PI_COMMAND_JSON="[\"bash\",\"{repo_root}/scripts/blueprint_pi_launch.sh\",\"{executor_prompt_path}\"]"
  DEFAULT_OPENCODE_COMMAND_JSON="[\"${OPENCODE_BIN:-opencode}\",\"run\",\"--file\",\"{executor_prompt_path}\",\"--format\",\"json\",\"--dangerously-skip-permissions\",\"Read {executor_prompt_path} and complete the Blueprint executor contract exactly.\"]"
  DEFAULT_CLAUDE_CODE_COMMAND_JSON="[\"${CLAUDE_BIN:-claude}\",\"-p\",\"@{executor_prompt_path}\",\"--output-format\",\"stream-json\",\"--verbose\"]"

  # Whitelist-based, single-write backend environment
  # NOTE: this list is the runtime contract. When backend Settings adds a new
  # deployment-relevant field, it must be added here so systemd services pick it up.
  _write_env_once() {
    local file="$1"
    shift
    : > "${file}"
    for entry in "$@"; do
      printf '%s\n' "${entry}" >> "${file}"
    done
  }

  _write_env_once "${APP_ENV_DIR}/backend.env" \
    "PATH=$(dirname "${NODE_BIN}"):${HOME}/.local/bin:${CONDA_BASE}/bin:${PATH}" \
    "BACKEND_HOST=127.0.0.1" \
    "BACKEND_PORT=18001" \
    "BLUEPRINT_FRONTEND_ORIGIN=http://127.0.0.1:13001" \
    "BLUEPRINT_MANAGER_BACKEND=pi" \
    "BLUEPRINT_DEFAULT_WORKER_TYPE=pi" \
    "BLUEPRINT_PI_MANAGER_URL=http://127.0.0.1:18002" \
    "BLUEPRINT_BACKEND_API_BASE_URL=http://127.0.0.1:18001/api" \
    "BLUEPRINT_DEFAULT_PYTHON_RUNTIME=${BLUEPRINT_DEFAULT_PYTHON_RUNTIME:-${DEFAULT_PYTHON_RUNTIME}}" \
    "BLUEPRINT_DEFAULT_R_RUNTIME=${BLUEPRINT_DEFAULT_R_RUNTIME:-${DEFAULT_R_RUNTIME}}" \
    "BLUEPRINT_EXECUTOR_CONDA_BASE=${BLUEPRINT_EXECUTOR_CONDA_BASE:-${CONDA_BASE}}" \
    "BLUEPRINT_DEEPSEEK_API_KEY=${BLUEPRINT_DEEPSEEK_API_KEY}" \
    "BLUEPRINT_DEEPSEEK_API_BASE_URL=${BLUEPRINT_DEEPSEEK_API_BASE_URL:-https://api.deepseek.com/anthropic}" \
    "BLUEPRINT_PI_DEEPSEEK_BASE_URL=${BLUEPRINT_PI_DEEPSEEK_BASE_URL:-https://api.deepseek.com}" \
    "BLUEPRINT_MANAGER_MODEL=${BLUEPRINT_MANAGER_MODEL:-deepseek-v4-pro}" \
    "BLUEPRINT_MANAGER_TEMPERATURE=${BLUEPRINT_MANAGER_TEMPERATURE:-0.2}" \
    "BLUEPRINT_MANAGER_MAX_TOKENS=${BLUEPRINT_MANAGER_MAX_TOKENS:-2400}" \
    "BLUEPRINT_MANAGER_TIMEOUT_SECONDS=${BLUEPRINT_MANAGER_TIMEOUT_SECONDS:-600}" \
    "BLUEPRINT_EXECUTOR_MODEL=${BLUEPRINT_EXECUTOR_MODEL:-deepseek-v4-flash}" \
    "BLUEPRINT_REVIEWER_MODEL=${BLUEPRINT_REVIEWER_MODEL:-deepseek-v4-flash}" \
    "BLUEPRINT_LIBRARY_SUMMARIZER_MODEL=${BLUEPRINT_LIBRARY_SUMMARIZER_MODEL:-deepseek-v4-flash}" \
    "BLUEPRINT_REVIEWER_MAX_TOKENS=${BLUEPRINT_REVIEWER_MAX_TOKENS:-2400}" \
    "BLUEPRINT_REVIEWER_MAX_TURNS=${BLUEPRINT_REVIEWER_MAX_TURNS:-24}" \
    "BLUEPRINT_EXECUTOR_SANDBOX_MODE=${BLUEPRINT_EXECUTOR_SANDBOX_MODE:-bwrap}" \
    "BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS=${BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS:-3}" \
    "BLUEPRINT_EXECUTOR_HOST_ROOT_READONLY=${BLUEPRINT_EXECUTOR_HOST_ROOT_READONLY:-true}" \
    "BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS=${BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS-${DEFAULT_EXTRA_RO_BINDS}}" \
    "BLUEPRINT_INTERNAL_TOOL_TOKEN=${INTERNAL_TOOL_TOKEN}" \
    "BLUEPRINT_PI_COMMAND_JSON=${BLUEPRINT_PI_COMMAND_JSON:-${DEFAULT_PI_COMMAND_JSON}}" \
    "BLUEPRINT_OPENCODE_COMMAND_JSON=${BLUEPRINT_OPENCODE_COMMAND_JSON:-${DEFAULT_OPENCODE_COMMAND_JSON}}" \
    "BLUEPRINT_CLAUDE_CODE_COMMAND_JSON=${BLUEPRINT_CLAUDE_CODE_COMMAND_JSON:-${DEFAULT_CLAUDE_CODE_COMMAND_JSON}}" \
    "BLUEPRINT_PROJECT_ROOTS=${BLUEPRINT_PROJECT_ROOTS:-}"

  # Codex is manual-only: if the operator added it to .env, forward it into
  # backend.env so the managed deploy path does not silently drop it.
  if [[ -n "${BLUEPRINT_CODEX_COMMAND_JSON:-}" ]]; then
    printf 'BLUEPRINT_CODEX_COMMAND_JSON=%s\n' "${BLUEPRINT_CODEX_COMMAND_JSON}" >> "${APP_ENV_DIR}/backend.env"
  fi

  cat > "${APP_ENV_DIR}/manager-agent.env" <<EOF
MANAGER_AGENT_HOST=127.0.0.1
MANAGER_AGENT_PORT=18002
MANAGER_AGENT_PROVIDER=deepseek
MANAGER_AGENT_MODEL=${BLUEPRINT_MANAGER_MODEL:-deepseek-v4-pro}
MANAGER_AGENT_TIMEOUT_MS=600000
BLUEPRINT_DEEPSEEK_API_KEY=${BLUEPRINT_DEEPSEEK_API_KEY}
BLUEPRINT_INTERNAL_TOOL_TOKEN=${INTERNAL_TOOL_TOKEN}
MANAGER_WEBSEARCH_ENABLED=${MANAGER_WEBSEARCH_ENABLED:-}
TAVILY_API_KEY=${TAVILY_API_KEY:-}
TAVILY_BASE_URL=${TAVILY_BASE_URL:-https://api.tavily.com}
MANAGER_CONTEXT_WINDOW_TOKENS=${MANAGER_CONTEXT_WINDOW_TOKENS:-1000000}
MANAGER_COMPACTION_ENABLED=${MANAGER_COMPACTION_ENABLED:-true}
MANAGER_COMPACTION_KEEP_RECENT_TOKENS=${MANAGER_COMPACTION_KEEP_RECENT_TOKENS:-120000}
MANAGER_COMPACTION_RESERVE_TOKENS=${MANAGER_COMPACTION_RESERVE_TOKENS:-16000}
EOF

  cat > "${APP_ENV_DIR}/frontend.env" <<EOF
FRONTEND_HOST=127.0.0.1
FRONTEND_PORT=13002
NEXT_PUBLIC_API_BASE_URL=/api
NEXT_PUBLIC_UPLOAD_API_BASE_URL=/upload-api
BACKEND_PROXY_TARGET=http://127.0.0.1:18001
EOF

  # Frontend dependencies
  if [[ "${DEPLOY_MODE}" == "full-rebuild" ]] || [[ ! -d "${ROOT_DIR}/frontend/node_modules" ]] || _marker_changed frontend-install frontend/package-lock.json; then
    pushd "${ROOT_DIR}/frontend" >/dev/null
    if [[ -f package-lock.json ]]; then
      npm ci
    else
      npm install
    fi
    popd >/dev/null
    _write_marker frontend-install frontend/package-lock.json
  else
    echo "Frontend dependencies unchanged; skipping npm ci."
  fi

  # Frontend build
  if [[ "${DEPLOY_MODE}" == "full-rebuild" ]] || [[ ! -f "${FRONTEND_RELEASE_DIR}/frontend/server.js" ]] || _marker_changed frontend-build frontend/package.json frontend/package-lock.json frontend/next.config.* frontend/tsconfig*.json frontend/app frontend/components frontend/lib frontend/public; then
    set -a
    source "${APP_ENV_DIR}/frontend.env"
    set +a
    pushd "${ROOT_DIR}/frontend" >/dev/null
    npm run build
    # Keep the deployed server isolated from repo-local builds so later `npm run build`
    # calls do not invalidate the live chunk manifest under the running process.
    rm -rf "${FRONTEND_RELEASE_DIR}"
    mkdir -p "${FRONTEND_RELEASE_DIR}/frontend/.next"
    cp -a "${ROOT_DIR}/frontend/.next/standalone/." "${FRONTEND_RELEASE_DIR}/"
    rm -rf "${FRONTEND_RELEASE_DIR}/frontend/.next/static"
    cp -a "${ROOT_DIR}/frontend/.next/static" "${FRONTEND_RELEASE_DIR}/frontend/.next/"
    if [[ -d "${ROOT_DIR}/frontend/public" ]]; then
      cp -a "${ROOT_DIR}/frontend/public" "${FRONTEND_RELEASE_DIR}/frontend/"
    fi
    popd >/dev/null
    _write_marker frontend-build frontend/package.json frontend/package-lock.json frontend/next.config.* frontend/tsconfig*.json frontend/app frontend/components frontend/lib frontend/public
  else
    echo "Frontend sources unchanged; skipping build."
  fi

  # Manager-agent dependencies
  if [[ "${DEPLOY_MODE}" == "full-rebuild" ]] || [[ ! -d "${ROOT_DIR}/manager-agent/node_modules" ]] || _marker_changed manager-install manager-agent/package-lock.json; then
    pushd "${ROOT_DIR}/manager-agent" >/dev/null
    if [[ -f package-lock.json ]]; then
      npm ci
    else
      npm install
    fi
    popd >/dev/null
    _write_marker manager-install manager-agent/package-lock.json
  else
    echo "Manager-agent dependencies unchanged; skipping npm ci."
  fi

  sed -e "s|__ROOT__|${ROOT_DIR}|g" -e "s|__NODE_BIN__|${NODE_BIN}|g" "${ROOT_DIR}/deploy/systemd/blueprint-re-manager-agent.service" > "${SYSTEMD_USER_DIR}/blueprint-re-manager-agent.service"
  sed -e "s|__ROOT__|${ROOT_DIR}|g" -e "s|__NODE_BIN__|${NODE_BIN}|g" "${ROOT_DIR}/deploy/systemd/blueprint-re-backend.service" > "${SYSTEMD_USER_DIR}/blueprint-re-backend.service"
  sed -e "s|__ROOT__|${ROOT_DIR}|g" -e "s|__FRONTEND_RELEASE_DIR__|${FRONTEND_RELEASE_DIR}|g" -e "s|__NODE_BIN__|${NODE_BIN}|g" "${ROOT_DIR}/deploy/systemd/blueprint-re-frontend.service" > "${SYSTEMD_USER_DIR}/blueprint-re-frontend.service"

  mkdir -p "${APP_ENV_DIR}/nginx-tmp/body" "${APP_ENV_DIR}/nginx-tmp/proxy" "${APP_ENV_DIR}/nginx-tmp/fastcgi" "${APP_ENV_DIR}/nginx-tmp/uwsgi" "${APP_ENV_DIR}/nginx-tmp/scgi"
  sed -e "s|__APP_ENV_DIR__|${APP_ENV_DIR}|g" "${ROOT_DIR}/deploy/nginx/blueprint-re.conf.template" > "${APP_ENV_DIR}/nginx.conf"
  sed -e "s|__NGINX_BIN__|${NGINX_BIN}|g" -e "s|__APP_ENV_DIR__|${APP_ENV_DIR}|g" "${ROOT_DIR}/deploy/systemd/blueprint-re-nginx.service" > "${SYSTEMD_USER_DIR}/blueprint-re-nginx.service"

  systemctl --user daemon-reload
  systemctl --user enable blueprint-re-manager-agent.service
  systemctl --user enable blueprint-re-backend.service
  systemctl --user enable blueprint-re-frontend.service
  systemctl --user enable blueprint-re-nginx.service
fi

# Report current port occupants before shutdown so conflicts are visible
# instead of being hidden behind a blind restart.
_report_port_occupants "Pre-deploy port occupants:"

# Stop all user-level services before restarting. This is more reliable than
# only stopping nginx/frontend because old backend/manager processes may also
# hold ports or long-running connections.
systemctl --user stop blueprint-re-nginx.service || true
systemctl --user stop blueprint-re-frontend.service || true
systemctl --user stop blueprint-re-backend.service || true
systemctl --user stop blueprint-re-manager-agent.service || true
sleep 2

# Confirm ports were released. If something outside this systemd set is still
# listening, warn but continue; the user can then decide whether to kill it.
if _report_port_occupants "Port occupants after stop:"; then
  warn_deploy "Some Blueprint ports are still occupied after stopping services."
fi

# Start in dependency order: manager-agent -> backend -> frontend -> nginx.
systemctl --user start blueprint-re-manager-agent.service
systemctl --user start blueprint-re-backend.service
systemctl --user start blueprint-re-frontend.service
systemctl --user start blueprint-re-nginx.service

# Wait for services to settle, then run health checks.
echo "Running health checks..."
sleep 3
HEALTH_OK=1

# Verify each user-level service is active before trusting the HTTP probes.
for service_name in \
  blueprint-re-manager-agent.service \
  blueprint-re-backend.service \
  blueprint-re-frontend.service \
  blueprint-re-nginx.service; do
  if ! _is_service_active "${service_name}"; then
    warn_deploy "Service ${service_name} is not active"
    HEALTH_OK=0
  fi
done

# Verify the backend response content, not just that the port speaks HTTP.
if ! _http_body_check http://127.0.0.1:18001/healthz '"status":"ok"'; then
  warn_deploy "Backend health check failed (expected {\"status\":\"ok\"} at http://127.0.0.1:18001/healthz)"
  HEALTH_OK=0
fi

# Verify the nginx gateway is actually proxying the Next.js frontend.
if ! _http_header_check http://127.0.0.1:13001 "location: /projects" && \
   ! _http_header_check http://127.0.0.1:13001 "X-Powered-By: Next.js"; then
  warn_deploy "nginx gateway check failed (expected Next.js redirect at http://127.0.0.1:13001)"
  HEALTH_OK=0
fi

echo ""
echo "Blueprint RE deployed."
echo "Frontend: http://127.0.0.1:13001  (nginx gateway)"
echo "Backend:  http://127.0.0.1:18001"
echo "Next.js:  http://127.0.0.1:13002  (internal)"
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
