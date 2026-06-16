#!/usr/bin/env bash
set -euo pipefail

# Blueprint RE Self-Extracting Installer
#
# This script is both the installer stub and the self-extracting archive.
# An appended tar.gz payload begins after the __PAYLOAD_START__ marker.
#
# Usage:
#   bash blueprint-re-<version>-linux-x86_64.sh [--offline] [--upgrade] [--rollback <version>]
#
# Note: this self-extracting file contains an appended binary tar.gz payload.
# Do not install it via `curl | bash`; download the file first, then execute it.

# ---------------------------------------------------------------------------
# Installer metadata (populated at build time)
# ---------------------------------------------------------------------------
INSTALLER_VERSION="__INSTALLER_VERSION__"
INSTALLER_ARCH="x86_64"
INSTALLER_PLATFORM="linux"
# At build time this is zero-padded to a fixed width so the script length stays
# constant when the placeholder is replaced. Strip padding for use with tail.
PAYLOAD_OFFSET="__PAYLOAD_OFFSET__"
PAYLOAD_OFFSET="$((10#${PAYLOAD_OFFSET}))"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
umask 077

INSTALL_USER="${USER:-$(id -un)}"

RELEASE_BASE="${BLUEPRINT_RELEASE_BASE:-${HOME}/.local/share/blueprint-re}"
RELEASES_DIR="${RELEASE_BASE}/releases"
CURRENT_LINK="${RELEASE_BASE}/current"
ENV_DIR="${RELEASE_BASE}/env"
DATA_ROOT="${RELEASE_BASE}/data"
APP_ENV_DIR="${HOME}/.config/blueprint-re"

OFFLINE_MODE=0
ROLLBACK_VERSION=""
SKIP_VERIFY=0
FORCE_UPGRADE=0

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --offline)
        OFFLINE_MODE=1
        shift
        ;;
      --rollback)
        if [[ -n "${2:-}" ]]; then
          ROLLBACK_VERSION="$2"
          shift 2
        else
          echo "ERROR: --rollback requires a version argument." >&2
          exit 1
        fi
        ;;
      --skip-verify)
        SKIP_VERIFY=1
        shift
        ;;
      --upgrade)
        FORCE_UPGRADE=1
        shift
        ;;
      --help|-h)
        cat <<'EOF'
Usage: bash install.sh [--offline] [--upgrade] [--rollback VERSION]

Options:
  --offline          Fail if the embedded package cache is missing.
  --upgrade          Require an existing installation and run upgrade flow.
  --rollback VERSION Switch to a previous release version.
  --skip-verify      Skip payload checksum verification (not recommended).
  --help             Show this message.

Environment:
  BLUEPRINT_RELEASE_BASE       Override the default release directory.
  BLUEPRINT_INSTALL_R_RUNTIME  1 (default) build slim R runtime; 0 skip.
  BLUEPRINT_INSTALL_R_EXTRAS   0 (default); 1 append enrichment packages.
EOF
        exit 0
        ;;
      *)
        echo "WARNING: Unknown argument: $1" >&2
        shift
        ;;
    esac
  done
}

parse_args "$@"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

die() {
  echo "ERROR: $1" >&2
  exit 1
}

info() {
  echo "[install] $1"
}

warn() {
  echo "[install] WARNING: $1" >&2
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    die "Required command not found: $1"
  fi
}

# Run bwrap with classified diagnostics. Exits on failure.
bwrap_smoke_test() {
  local bwrap_bin="$1"
  local test_label="$2"
  shift 2
  local exit_code=0
  if "${bwrap_bin}" "$@" -- /bin/true 2>/dev/null; then
    return 0
  fi
  exit_code=$?
  # Classification attempt
  echo ""
  echo "=== bubblewrap diagnostic ===" >&2
  echo "Smoke test failed: ${test_label}" >&2
  if ! "${bwrap_bin}" --version >/dev/null 2>&1; then
    echo "  -> bwrap binary is not executable or not found" >&2
    die "bubblewrap binary missing or unusable: ${bwrap_bin}"
  fi
  # Try progressively simpler invocations to classify the failure
  if ! "${bwrap_bin}" --dev /dev -- /bin/true 2>/dev/null; then
    echo "  -> unprivileged user namespace may be disabled" >&2
    echo "     (check /proc/sys/kernel/unprivileged_userns_clone or seccomp policy)" >&2
    die "bubblewrap namespace creation blocked. Host policy prevents unprivileged user namespaces."
  fi
  if ! "${bwrap_bin}" --tmpfs /tmp -- /bin/true 2>/dev/null; then
    echo "  -> mount namespace may be blocked" >&2
    die "bubblewrap mount namespace blocked. Check container/seccomp policy."
  fi
  echo "  -> generic sandbox failure (exit code ${exit_code})" >&2
  die "bubblewrap smoke test failed. Sandbox cannot operate on this host."
}

version_gte() {
  local actual="$1"
  local required="$2"
  [[ "$(printf '%s\n%s\n' "${required}" "${actual}" | sort -V | head -n1)" == "${required}" ]]
}

# Extract a top-level JSON string value without requiring Python.
# Only works for simple "key": "value" pairs on a single line.
json_get_string() {
  local file="$1"
  local key="$2"
  grep -E "\"${key}\"[[:space:]]*:[[:space:]]*\"" "${file}" 2>/dev/null | head -1 | sed -E 's/.*"'"${key}"'"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/'
}

# Export runtime binary env vars as a helper so normal deploy and rollback can
# share the same values.
export_runtime_bin_env() {
  export BLUEPRINT_RELEASE_ROOT="${CURRENT_LINK}"
  export BLUEPRINT_DATA_ROOT="${DATA_ROOT}"
  export BLUEPRINT_PYTHON_BIN="${ENV_PYTHON}"
  export BLUEPRINT_NODE_BIN="${ENV_NODE}"
  export BLUEPRINT_NPM_BIN="${ENV_NPM}"
  export BLUEPRINT_NGINX_BIN="${ENV_NGINX}"
  export BLUEPRINT_BWRAP_BIN="${ENV_BWRAP}"
  export BLUEPRINT_GIT_BIN="${ENV_GIT}"
  export BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX="${BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX:-}"
  export BLUEPRINT_EXECUTOR_MAMBARC="${BLUEPRINT_EXECUTOR_MAMBARC:-}"
  export BLUEPRINT_CRAN_MIRROR="${BLUEPRINT_CRAN_MIRROR:-}"
  export BLUEPRINT_BIOCONDUCTOR_MIRROR="${BLUEPRINT_BIOCONDUCTOR_MIRROR:-}"
  export BLUEPRINT_PYPI_MIRROR="${BLUEPRINT_PYPI_MIRROR:-}"
}

# Run deploy_release.sh with the current runtime binary env and optional flags.
run_deploy() {
  local deploy_flags=("$@")
  export_runtime_bin_env
  bash "${VERSION_DIR}/scripts/deploy_release.sh" "${deploy_flags[@]}"
}

# Run deploy_release.sh for an existing release directory (used by rollback).
# The runtime environment is global (shared across releases), not per-release.
run_deploy_for_release() {
  local release_dir="$1"
  shift
  export BLUEPRINT_RELEASE_ROOT="${release_dir}"
  export BLUEPRINT_DATA_ROOT="${DATA_ROOT}"
  export BLUEPRINT_PYTHON_BIN="${ENV_DIR}/bin/python"
  export BLUEPRINT_NODE_BIN="${ENV_DIR}/bin/node"
  export BLUEPRINT_NPM_BIN="${ENV_DIR}/bin/npm"
  export BLUEPRINT_NGINX_BIN="${ENV_DIR}/bin/nginx"
  export BLUEPRINT_BWRAP_BIN="${ENV_DIR}/bin/bwrap"
  export BLUEPRINT_GIT_BIN="${ENV_DIR}/bin/git"
  bash "${release_dir}/scripts/deploy_release.sh" "$@"
}

# Wait for backend/nginx health endpoints to come up.
# Prefers curl but falls back to Python urllib if curl is unavailable.
wait_for_health() {
  local timeout_secs=30
  local deadline=$(( $(date +%s) + timeout_secs ))
  local backend_ok=0 nginx_ok=0
  local http_check=""
  if command -v curl >/dev/null 2>&1; then
    http_check="curl"
  elif "${ENV_PYTHON}" -c "import urllib.request" >/dev/null 2>&1; then
    http_check="python"
  fi
  while [[ $(date +%s) -lt ${deadline} ]]; do
    if [[ ${backend_ok} -eq 0 ]]; then
      if [[ "${http_check}" == "curl" ]] && curl -fsS http://127.0.0.1:18001/healthz >/dev/null 2>&1; then
        backend_ok=1
      elif [[ "${http_check}" == "python" ]] && "${ENV_PYTHON}" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18001/healthz', timeout=2)" >/dev/null 2>&1; then
        backend_ok=1
      fi
    fi
    if [[ ${nginx_ok} -eq 0 ]]; then
      if [[ "${http_check}" == "curl" ]] && curl -sS -o /dev/null -I http://127.0.0.1:13001 >/dev/null 2>&1; then
        nginx_ok=1
      elif [[ "${http_check}" == "python" ]] && "${ENV_PYTHON}" -c "import urllib.request; opener=urllib.request.build_opener(urllib.request.HTTPRedirectHandler); opener.open('http://127.0.0.1:13001', timeout=2)" >/dev/null 2>&1; then
        nginx_ok=1
      fi
    fi
    if [[ ${backend_ok} -eq 1 && ${nginx_ok} -eq 1 ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# ---------------------------------------------------------------------------
# Phase 1: Host Preflight
# ---------------------------------------------------------------------------

info "Phase 1: Host preflight"

if [[ "$(uname -s)" != "Linux" ]]; then
  die "This installer supports Linux only. Detected: $(uname -s)"
fi

if [[ "$(uname -m)" != "x86_64" ]]; then
  die "This installer supports x86_64 only. Detected: $(uname -m)"
fi

if [[ -z "${HOME:-}" || ! -w "${HOME}" ]]; then
  die "HOME directory must be set and writable."
fi

if ! systemctl --user show-environment >/dev/null 2>&1; then
  die "systemd --user is not available. Log into a full user session."
fi

require_cmd tar
require_cmd sha256sum
require_cmd mktemp
require_cmd sed
require_cmd grep

# Check local port availability
PORTS_TO_CHECK=(13001 13002 18001 18002)
PORT_CONFLICTS=()
for port in "${PORTS_TO_CHECK[@]}"; do
  if ss -tln 2>/dev/null | grep -qE ":${port}[[:space:]]"; then
    PORT_CONFLICTS+=("${port}")
  elif netstat -tln 2>/dev/null | grep -qE ":${port}[[:space:]]"; then
    PORT_CONFLICTS+=("${port}")
  fi
done
if [[ "${#PORT_CONFLICTS[@]}" -gt 0 ]]; then
  if [[ ! -L "${CURRENT_LINK}" ]]; then
    die "Detected Blueprint ports already in use (${PORT_CONFLICTS[*]}), but no existing release installation was found at ${CURRENT_LINK}. This may be a legacy/source deployment (e.g. a manual uvicorn or an old source-tree systemd service). Stop the conflicting services before running this installer. If you intended to adopt an existing source deployment, use scripts/deploy_user_systemd.sh instead."
  fi
  warn "The following ports are already in use: ${PORT_CONFLICTS[*]}"
  warn "If these are from a previous Blueprint RE install, the deploy will reuse them."
  warn "If another service is using them, supply custom ports via BLUEPRINT_*_PORT env vars before running deploy."
fi

# Fail fast on --upgrade without an existing installation, before any expensive
# payload extraction or environment creation.
if [[ "${FORCE_UPGRADE}" -eq 1 && ! -L "${CURRENT_LINK}" ]]; then
  die "--upgrade was requested, but no existing Blueprint RE installation was found at ${CURRENT_LINK}"
fi

# Linger detection
LINGER_STATUS=""
if command -v loginctl >/dev/null 2>&1; then
  LINGER_STATUS="$(loginctl show-user "${INSTALL_USER}" -p Linger 2>/dev/null || true)"
fi
if [[ "${LINGER_STATUS}" == "Linger=no" ]]; then
  warn "User linger is disabled. systemd --user services may stop after you log out."
  warn "To enable linger (if your system policy allows): loginctl enable-linger ${INSTALL_USER}"
fi

# ---------------------------------------------------------------------------
# Rollback mode
# ---------------------------------------------------------------------------

if [[ -n "${ROLLBACK_VERSION}" ]]; then
  info "Rollback mode: switching to version ${ROLLBACK_VERSION}"
  ROLLBACK_TARGET="${RELEASES_DIR}/${ROLLBACK_VERSION}"
  if [[ ! -d "${ROLLBACK_TARGET}" ]]; then
    die "Rollback target not found: ${ROLLBACK_TARGET}"
  fi

  # Validate the global runtime environment is still usable.
  if [[ ! -x "${ENV_DIR}/bin/python" ]]; then
    die "Global runtime environment is missing: ${ENV_DIR}/bin/python"
  fi

  info "Stopping services..."
  systemctl --user stop blueprint-re-nginx.service || true
  systemctl --user stop blueprint-re-frontend.service || true
  systemctl --user stop blueprint-re-backend.service || true
  systemctl --user stop blueprint-re-manager-agent.service || true
  sleep 2

  ln -sfn "${ROLLBACK_TARGET}" "${CURRENT_LINK}"

  info "Re-deploying previous release..."
  if ! run_deploy_for_release "${ROLLBACK_TARGET}" --upgrade --services-stopped; then
    die "Rollback deploy failed. Services may be in an inconsistent state."
  fi

  info "Waiting for health checks..."
  if wait_for_health; then
    info "Rollback to ${ROLLBACK_VERSION} complete. Services are healthy."
  else
    warn "Rollback services started but health checks did not pass."
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Phase 2: Payload extraction
# ---------------------------------------------------------------------------

info "Phase 2: Extracting payload"

EXTRACT_DIR="$(mktemp -d)"
trap 'rm -rf "${EXTRACT_DIR}"' EXIT

# Direct execution only: the payload offset is embedded at build time.
SCRIPT_PATH=""
if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
  SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
fi

if [[ -z "${SCRIPT_PATH}" ]]; then
  die "This installer cannot be run from a pipe. Save the file to disk and execute it directly."
fi

if [[ ! "${PAYLOAD_OFFSET}" =~ ^[0-9]+$ ]]; then
  die "Invalid PAYLOAD_OFFSET (${PAYLOAD_OFFSET}). This script must be built into a self-extracting installer."
fi

info "Extracting payload to ${EXTRACT_DIR}..."
tail -c +"${PAYLOAD_OFFSET}" "${SCRIPT_PATH}" | tar -xzf - -C "${EXTRACT_DIR}"

PAYLOAD_DIR="${EXTRACT_DIR}/blueprint-re"
if [[ ! -d "${PAYLOAD_DIR}" ]]; then
  die "Payload extraction failed: blueprint-re directory not found."
fi

# ---------------------------------------------------------------------------
# Phase 3: Payload validation
# ---------------------------------------------------------------------------

info "Phase 3: Validating payload"

if [[ ! -f "${PAYLOAD_DIR}/release.json" ]]; then
  die "release.json not found in payload."
fi

# Shell-based JSON extraction (no host Python required for preflight).
RELEASE_VERSION="$(json_get_string "${PAYLOAD_DIR}/release.json" "version")"
RELEASE_ARCH="$(json_get_string "${PAYLOAD_DIR}/release.json" "arch")"
RELEASE_PLATFORM="$(json_get_string "${PAYLOAD_DIR}/release.json" "platform")"

RELEASE_VERSION="${RELEASE_VERSION:-unknown}"
RELEASE_ARCH="${RELEASE_ARCH:-unknown}"
RELEASE_PLATFORM="${RELEASE_PLATFORM:-unknown}"

info "Release version: ${RELEASE_VERSION}"
info "Release arch:    ${RELEASE_ARCH}"
info "Release platform: ${RELEASE_PLATFORM}"

if [[ "${RELEASE_VERSION}" != "${INSTALLER_VERSION}" ]]; then
  die "Version mismatch: installer=${INSTALLER_VERSION}, payload=${RELEASE_VERSION}"
fi

if [[ "${RELEASE_ARCH}" != "${INSTALLER_ARCH}" ]]; then
  die "Architecture mismatch: payload=${RELEASE_ARCH}, installer=${INSTALLER_ARCH}"
fi

if [[ "${RELEASE_PLATFORM}" != "${INSTALLER_PLATFORM}" ]]; then
  die "Platform mismatch: payload=${RELEASE_PLATFORM}, installer=${INSTALLER_PLATFORM}"
fi

# Verify checksums with sha256sum (no host Python required).
if [[ "${SKIP_VERIFY}" -eq 0 ]]; then
  if [[ -f "${PAYLOAD_DIR}/checksums.sha256" ]]; then
    info "Verifying payload checksums..."
    (cd "${PAYLOAD_DIR}" && sha256sum -c --status checksums.sha256) || die "Payload checksum verification failed."
    info "Checksums OK"
  else
    warn "checksums.sha256 not found; skipping checksum verification."
  fi
else
  info "Skipping checksum verification (--skip-verify)"
fi

# Check for offline package cache
HAS_OFFLINE_CACHE=0
if [[ -d "${PAYLOAD_DIR}/runtime/packages" && "$(ls -A "${PAYLOAD_DIR}/runtime/packages")" ]]; then
  HAS_OFFLINE_CACHE=1
  info "Embedded offline package cache detected."
fi

if [[ "${OFFLINE_MODE}" -eq 1 && "${HAS_OFFLINE_CACHE}" -eq 0 ]]; then
  die "--offline requested but embedded package cache is missing."
fi

# ---------------------------------------------------------------------------
# Phase 4: Runtime bootstrap (micromamba/conda)
# ---------------------------------------------------------------------------

info "Phase 4: Runtime bootstrap"

MAMBA_EXE=""
CONDA_EXE=""

# 1. Check for embedded micromamba (legacy runtime/micromamba or bundled runtime/bin/micromamba).
if [[ -f "${PAYLOAD_DIR}/runtime/bin/micromamba" ]]; then
  MAMBA_EXE="${PAYLOAD_DIR}/runtime/bin/micromamba"
  chmod +x "${MAMBA_EXE}"
  info "Using bundled micromamba."
elif [[ -f "${PAYLOAD_DIR}/runtime/micromamba" ]]; then
  MAMBA_EXE="${PAYLOAD_DIR}/runtime/micromamba"
  chmod +x "${MAMBA_EXE}"
  info "Using embedded micromamba (legacy path)."
fi

# 2. Check for existing micromamba/mamba/conda
if [[ -z "${MAMBA_EXE}" ]]; then
  if command -v micromamba >/dev/null 2>&1; then
    MAMBA_EXE="$(command -v micromamba)"
    info "Using host micromamba: ${MAMBA_EXE}"
  elif command -v mamba >/dev/null 2>&1; then
    CONDA_EXE="$(command -v mamba)"
    info "Using host mamba: ${CONDA_EXE}"
  elif command -v conda >/dev/null 2>&1; then
    CONDA_EXE="$(command -v conda)"
    info "Using host conda: ${CONDA_EXE}"
  fi
fi

# 3. Download micromamba if allowed
if [[ -z "${MAMBA_EXE}" && -z "${CONDA_EXE}" && "${OFFLINE_MODE}" -eq 0 ]]; then
  info "Downloading micromamba bootstrap..."
  # Pin to a specific release for verifiable integrity.
  MICROMAMBA_VERSION="2.1.0-0"
  MICROMAMBA_URL="https://github.com/mamba-org/micromamba-releases/releases/download/${MICROMAMBA_VERSION}/micromamba-linux-64.tar.bz2"
  EXPECTED_MICROMAMBA_SHA256="bec27dc583c8faede774bdf0f0a11c5c4d80b7c877c0f17f5aa477a2d48e42d2"
  MICROMAMBA_ARCHIVE="${EXTRACT_DIR}/micromamba.tar.bz2"

  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "${MICROMAMBA_ARCHIVE}" "${MICROMAMBA_URL}"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "${MICROMAMBA_ARCHIVE}" "${MICROMAMBA_URL}"
  else
    die "No curl or wget available. Cannot download micromamba bootstrap."
  fi
  info "Verifying micromamba archive integrity..."
  echo "${EXPECTED_MICROMAMBA_SHA256}  ${MICROMAMBA_ARCHIVE}" | sha256sum -c -
  tar -xjf "${MICROMAMBA_ARCHIVE}" -C "${EXTRACT_DIR}" bin/micromamba
  MAMBA_EXE="${EXTRACT_DIR}/bin/micromamba"
  if [[ ! -x "${MAMBA_EXE}" ]]; then
    die "micromamba download failed: binary not found after extraction."
  fi
  info "Downloaded micromamba ${MICROMAMBA_VERSION}."
fi

if [[ -z "${MAMBA_EXE}" && -z "${CONDA_EXE}" ]]; then
  die "No conda/mamba/micromamba available and offline mode is active."
fi

# ---------------------------------------------------------------------------
# Phase 5: Create/update dedicated environment
# ---------------------------------------------------------------------------

info "Phase 5: Creating runtime environment at ${ENV_DIR}"

# Ensure the parent directory exists, but let micromamba/conda create the
# prefix itself. Pre-creating the prefix as a plain directory causes
# "Non-conda folder exists at prefix" in recent micromamba versions.
mkdir -p "$(dirname "${ENV_DIR}")"

# Guard against a stale non-conda directory at the target prefix.
# An empty directory is harmless residue (e.g. from an older installer's
# `mkdir -p`) and can be removed safely; a non-empty directory without
# conda-meta is a real conflict that needs human judgement.
if [[ -d "${ENV_DIR}" && ! -d "${ENV_DIR}/conda-meta" ]]; then
  if [[ -n "$(find "${ENV_DIR}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    die "Runtime prefix exists but is not a conda environment: ${ENV_DIR}. Remove it and retry."
  fi
  info "Removing empty leftover prefix directory: ${ENV_DIR}"
  rmdir "${ENV_DIR}"
fi

if [[ "${HAS_OFFLINE_CACHE}" -eq 1 ]]; then
  info "Creating environment from offline package cache..."
  if [[ -n "${MAMBA_EXE}" ]]; then
    "${MAMBA_EXE}" create -y -p "${ENV_DIR}" --offline \
      --channel "${PAYLOAD_DIR}/runtime/packages" \
      -f "${PAYLOAD_DIR}/runtime/environment.yml"
  else
    "${CONDA_EXE}" create -y -p "${ENV_DIR}" --offline \
      --channel "${PAYLOAD_DIR}/runtime/packages" \
      -f "${PAYLOAD_DIR}/runtime/environment.yml"
  fi
else
  info "Creating environment from conda-forge (online)..."
  if [[ -n "${MAMBA_EXE}" ]]; then
    "${MAMBA_EXE}" create -y -p "${ENV_DIR}" -f "${PAYLOAD_DIR}/runtime/environment.yml"
  else
    "${CONDA_EXE}" create -y -p "${ENV_DIR}" -f "${PAYLOAD_DIR}/runtime/environment.yml"
  fi
fi

# ---------------------------------------------------------------------------
# Phase 5b: Provision bundled micromamba + R runtime
# ---------------------------------------------------------------------------

BUNDLED_MAMBA_ROOT="${RELEASE_BASE}/mamba"
BUNDLED_MAMBA_BIN="${BUNDLED_MAMBA_ROOT}/bin/micromamba"
BUNDLED_MAMBARC="${BUNDLED_MAMBA_ROOT}/.mambarc"
BLUEPRINT_DEFAULT_R_RUNTIME="${BLUEPRINT_DEFAULT_R_RUNTIME:-}"
BLUEPRINT_INSTALL_R_RUNTIME="${BLUEPRINT_INSTALL_R_RUNTIME:-1}"
BLUEPRINT_INSTALL_R_EXTRAS="${BLUEPRINT_INSTALL_R_EXTRAS:-0}"
# Shared across runtime/extras functions; set once after micromamba is copied.
micromamba_version=""

provision_bundled_mamba() {
  if [[ ! -x "${MAMBA_EXE}" ]]; then
    warn "No bundled micromamba available; skipping bundled mamba/R runtime provisioning."
    return 1
  fi

  info "Provisioning bundled micromamba root at ${BUNDLED_MAMBA_ROOT}"
  mkdir -p "${BUNDLED_MAMBA_ROOT}/bin" "${BUNDLED_MAMBA_ROOT}/envs"
  cp "${MAMBA_EXE}" "${BUNDLED_MAMBA_BIN}"
  chmod +x "${BUNDLED_MAMBA_BIN}"

  local preset="${BLUEPRINT_MIRROR_PRESET:-tsinghua}"
  if [[ -f "${PAYLOAD_DIR}/runtime/mirror-presets/${preset}.mambarc" ]]; then
    cp "${PAYLOAD_DIR}/runtime/mirror-presets/${preset}.mambarc" "${BUNDLED_MAMBARC}"
  elif [[ -f "${PAYLOAD_DIR}/runtime/mirror-presets/default.mambarc" ]]; then
    cp "${PAYLOAD_DIR}/runtime/mirror-presets/default.mambarc" "${BUNDLED_MAMBARC}"
  fi
  printf '\nroot_prefix: %s\n' "${BUNDLED_MAMBA_ROOT}" >> "${BUNDLED_MAMBARC}"

  export MAMBA_ROOT_PREFIX="${BUNDLED_MAMBA_ROOT}"
  export MAMBARC="${BUNDLED_MAMBARC}"

  # Expand mirror preset env vars for the deploy step.
  if [[ -f "${PAYLOAD_DIR}/runtime/mirror-presets/mirror_env.sh" ]]; then
    # shellcheck disable=SC1091
    source "${PAYLOAD_DIR}/runtime/mirror-presets/mirror_env.sh"
    BLUEPRINT_CRAN_MIRROR="${BLUEPRINT_CRAN_MIRROR:-$(eval echo "\${${preset}_cran_mirror:-}")}"
    BLUEPRINT_BIOCONDUCTOR_MIRROR="${BLUEPRINT_BIOCONDUCTOR_MIRROR:-$(eval echo "\${${preset}_bioconductor_mirror:-}")}"
    BLUEPRINT_PYPI_MIRROR="${BLUEPRINT_PYPI_MIRROR:-$(eval echo "\${${preset}_pypi_mirror:-}")}"
  fi

  micromamba_version="$("${BUNDLED_MAMBA_BIN}" --version 2>/dev/null || echo unknown)"
  return 0
}

provision_bundled_r_runtime() {
  if [[ "${BLUEPRINT_INSTALL_R_RUNTIME}" != "1" ]]; then
    info "Skipping bundled R runtime (BLUEPRINT_INSTALL_R_RUNTIME!=1)."
    return 0
  fi
  if [[ ! -x "${BUNDLED_MAMBA_BIN}" ]]; then
    warn "No bundled micromamba; skipping R runtime."
    return 1
  fi
  if [[ ! -f "${PAYLOAD_DIR}/runtime/blueprint-re-r.yml" ]]; then
    warn "No blueprint-re-r.yml in payload; skipping R runtime."
    return 1
  fi

  local r_env="blueprint-re-r"
  local r_env_dir="${BUNDLED_MAMBA_ROOT}/envs/${r_env}"
  local r_env_spec="${PAYLOAD_DIR}/runtime/blueprint-re-r.yml"
  local marker_file="${r_env_dir}/.blueprint-re-r.build-info"
  local spec_hash
  spec_hash="$(sha256sum "${r_env_spec}" | awk '{print $1}')"
  local expected_marker="${spec_hash} ${micromamba_version}"

  if [[ -x "${r_env_dir}/bin/Rscript" ]]; then
    if [[ -f "${marker_file}" ]]; then
      if [[ "$(cat "${marker_file}" 2>/dev/null)" == "${expected_marker}" ]]; then
        info "Bundled R runtime ${r_env} is up to date."
        BLUEPRINT_DEFAULT_R_RUNTIME="${r_env}"
        return 0
      fi
      info "Bundled R runtime spec or micromamba version changed; rebuilding ${r_env}."
    else
      info "Bundled R runtime exists but build marker is missing; rebuilding ${r_env}."
    fi
    "${BUNDLED_MAMBA_BIN}" env remove -n "${r_env}" -y 2>/dev/null || true
  fi

  info "Creating bundled R runtime (${r_env})... this may take 5-20 min."
  local create_cmd=("${BUNDLED_MAMBA_BIN}" create -y -n "${r_env}" -f "${r_env_spec}")
  if [[ -d "${PAYLOAD_DIR}/runtime/pkgs" && -n "$(ls -A "${PAYLOAD_DIR}/runtime/pkgs" 2>/dev/null)" ]]; then
    info "Using embedded R package cache for offline install."
    mkdir -p "${BUNDLED_MAMBA_ROOT}/pkgs"
    cp -a "${PAYLOAD_DIR}/runtime/pkgs/." "${BUNDLED_MAMBA_ROOT}/pkgs/"
    create_cmd+=(--offline)
  fi
  if ! "${create_cmd[@]}"; then
    warn "Bundled R runtime provisioning failed; cleaning partial env."
    "${BUNDLED_MAMBA_BIN}" env remove -n "${r_env}" -y 2>/dev/null || true
    return 1
  fi
  printf '%s %s\n' "${spec_hash}" "${micromamba_version}" > "${marker_file}"
  BLUEPRINT_DEFAULT_R_RUNTIME="${r_env}"
  info "Bundled R runtime ${r_env} ready."
}

provision_bundled_r_extras() {
  if [[ "${BLUEPRINT_INSTALL_R_EXTRAS}" != "1" ]]; then
    return 0
  fi
  local extras_spec="${PAYLOAD_DIR}/runtime/blueprint-re-r-extras.yml"
  if [[ ! -f "${extras_spec}" ]]; then
    warn "No blueprint-re-r-extras.yml in payload; skipping R extras."
    return 1
  fi
  if [[ ! -x "${BUNDLED_MAMBA_ROOT}/envs/blueprint-re-r/bin/Rscript" ]]; then
    die "BLUEPRINT_INSTALL_R_EXTRAS=1 but base R runtime is not built. Set BLUEPRINT_INSTALL_R_RUNTIME=1 as well."
  fi
  local r_env="blueprint-re-r"
  local r_env_dir="${BUNDLED_MAMBA_ROOT}/envs/${r_env}"
  local extras_marker="${r_env_dir}/.blueprint-re-r-extras.build-info"
  local spec_hash extras_expected
  spec_hash="$(sha256sum "${extras_spec}" | awk '{print $1}')"
  extras_expected="${spec_hash} ${micromamba_version}"
  if [[ -f "${extras_marker}" && "$(cat "${extras_marker}" 2>/dev/null)" == "${extras_expected}" ]]; then
    info "R extras already up to date."
    return 0
  fi
  info "Appending R extras (clusterProfiler + annotation DBs, ~220MB)..."
  if ! "${BUNDLED_MAMBA_BIN}" install -y -n "${r_env}" -f "${extras_spec}"; then
    warn "R extras provisioning failed; base R runtime remains usable."
    return 1
  fi
  printf '%s %s\n' "${spec_hash}" "${micromamba_version}" > "${extras_marker}"
  info "R extras ready."
}

if provision_bundled_mamba; then
  provision_bundled_r_runtime
  provision_bundled_r_extras
  export BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX="${BUNDLED_MAMBA_ROOT}"
  export BLUEPRINT_EXECUTOR_MAMBARC="${BUNDLED_MAMBARC}"
  export BLUEPRINT_CRAN_MIRROR="${BLUEPRINT_CRAN_MIRROR:-}"
  export BLUEPRINT_BIOCONDUCTOR_MIRROR="${BLUEPRINT_BIOCONDUCTOR_MIRROR:-}"
  export BLUEPRINT_PYPI_MIRROR="${BLUEPRINT_PYPI_MIRROR:-}"
fi

# ---------------------------------------------------------------------------
# Phase 6: Resolve binary paths from the environment
# ---------------------------------------------------------------------------

info "Phase 6: Resolving environment binaries"

ENV_PYTHON="${ENV_DIR}/bin/python"
ENV_NODE="${ENV_DIR}/bin/node"
ENV_NPM="${ENV_DIR}/bin/npm"
ENV_NGINX="${ENV_DIR}/bin/nginx"
ENV_BWRAP="${ENV_DIR}/bin/bwrap"
ENV_GIT="${ENV_DIR}/bin/git"

for bin_path in "${ENV_PYTHON}" "${ENV_NODE}" "${ENV_NPM}" "${ENV_NGINX}" "${ENV_BWRAP}" "${ENV_GIT}"; do
  if [[ ! -x "${bin_path}" ]]; then
    die "Expected binary missing after environment creation: ${bin_path}"
  fi
done

info "Python:  ${ENV_PYTHON} ($("${ENV_PYTHON}" --version))"
info "Node:    ${ENV_NODE} ($("${ENV_NODE}" -v))"
info "npm:     ${ENV_NPM}"
info "nginx:   ${ENV_NGINX}"
info "bwrap:   ${ENV_BWRAP}"
info "git:     ${ENV_GIT}"

# ---------------------------------------------------------------------------
# Phase 6b: bwrap host compatibility diagnostics
# ---------------------------------------------------------------------------

info "Phase 6b: Running bubblewrap smoke test"

bwrap_smoke_test "${ENV_BWRAP}" "full sandbox" \
  --die-with-parent \
  --ro-bind /usr /usr \
  --ro-bind /bin /bin \
  --ro-bind-try /lib /lib \
  --ro-bind-try /lib64 /lib64 \
  --proc /proc \
  --dev /dev \
  --tmpfs /tmp

info "bubblewrap smoke test passed."

# ---------------------------------------------------------------------------
# Phase 7: Install backend wheel into the environment
# ---------------------------------------------------------------------------

info "Phase 7: Installing backend wheel"

# Select the backend wheel path from the release manifest (P0 requirement).
WHEEL_PATH_IN_PAYLOAD="$(${ENV_PYTHON} -c "
import json, sys
manifest = json.load(open(sys.argv[1]))
print(manifest.get('artifacts', {}).get('backend_wheel', {}).get('path', ''))
" "${PAYLOAD_DIR}/release.json")"

if [[ -z "${WHEEL_PATH_IN_PAYLOAD}" ]]; then
  die "Backend wheel path not found in release.json artifacts.backend_wheel.path"
fi

WHEEL_FILE="${PAYLOAD_DIR}/${WHEEL_PATH_IN_PAYLOAD}"
if [[ ! -f "${WHEEL_FILE}" ]]; then
  die "Backend wheel not found at payload path: ${WHEEL_PATH_IN_PAYLOAD}"
fi

WHEEL_BASENAME="$(basename "${WHEEL_FILE}")"

# Verify wheel hash against release manifest before installing.
EXPECTED_WHEEL_HASH="$(${ENV_PYTHON} -c "
import json, sys
manifest = json.load(open(sys.argv[1]))
print(manifest.get('artifacts', {}).get('backend_wheel', {}).get('checksum_sha256', ''))
" "${PAYLOAD_DIR}/release.json")"

if [[ -z "${EXPECTED_WHEEL_HASH}" ]]; then
  die "Could not determine expected wheel hash from release.json."
fi

ACTUAL_WHEEL_HASH="$(sha256sum "${WHEEL_FILE}" | awk '{print $1}')"
if [[ "${ACTUAL_WHEEL_HASH}" != "${EXPECTED_WHEEL_HASH}" ]]; then
  die "Wheel hash mismatch for ${WHEEL_BASENAME}: expected ${EXPECTED_WHEEL_HASH}, got ${ACTUAL_WHEEL_HASH}"
fi
info "Wheel hash verified: ${ACTUAL_WHEEL_HASH}"

# Install the wheel and its bundled dependencies from the local wheels directory.
# The wheels/ directory may also contain vendored dependency wheels so this
# works fully offline when --offline is used.
"${ENV_PYTHON}" -m pip install --quiet --no-index --find-links "${PAYLOAD_DIR}/wheels" --force-reinstall "${WHEEL_FILE}"
info "Installed ${WHEEL_BASENAME}."

# ---------------------------------------------------------------------------
# Phase 8: Deploy release to releases directory
# ---------------------------------------------------------------------------

info "Phase 8: Deploying release"

VERSION_DIR="${RELEASES_DIR}/${RELEASE_VERSION}"
CURRENT_TARGET=""
if [[ -L "${CURRENT_LINK}" ]]; then
  CURRENT_TARGET="$(readlink -f "${CURRENT_LINK}" || true)"
fi
PREV_TARGET="${CURRENT_TARGET}"
VERSION_BACKUP_DIR=""

# If this version already exists, back it up.
if [[ -d "${VERSION_DIR}" ]]; then
  VERSION_BACKUP_DIR="${VERSION_DIR}.backup.$(date +%s)"
  info "Existing version found; backing up to ${VERSION_BACKUP_DIR}"
  mv "${VERSION_DIR}" "${VERSION_BACKUP_DIR}"
  if [[ -n "${CURRENT_TARGET}" && "${CURRENT_TARGET}" == "${VERSION_DIR}" ]]; then
    PREV_TARGET="${VERSION_BACKUP_DIR}"
    info "Current release points at the version being replaced; rollback target is ${PREV_TARGET}"
  fi
fi

mkdir -p "${VERSION_DIR}"
cp -a "${PAYLOAD_DIR}/." "${VERSION_DIR}/"

# ---------------------------------------------------------------------------
# Phase 9: Atomic symlink switch (with upgrade handling)
# ---------------------------------------------------------------------------

info "Phase 9: Switching current symlink"

# Determine if this is an upgrade.
IS_UPGRADE=0
if [[ -n "${PREV_TARGET}" ]]; then
  IS_UPGRADE=1
  if [[ -n "${VERSION_BACKUP_DIR}" && "${PREV_TARGET}" == "${VERSION_BACKUP_DIR}" ]]; then
    info "Reinstalling current release; rollback target is ${PREV_TARGET}"
  else
    info "Upgrading from ${PREV_TARGET}"
  fi
fi

# For upgrades: stop-the-world before switching.
if [[ "${IS_UPGRADE}" -eq 1 ]]; then
  info "Stopping services for upgrade..."
  systemctl --user stop blueprint-re-nginx.service 2>/dev/null || true
  systemctl --user stop blueprint-re-frontend.service 2>/dev/null || true
  systemctl --user stop blueprint-re-backend.service 2>/dev/null || true
  systemctl --user stop blueprint-re-manager-agent.service 2>/dev/null || true
  sleep 2
fi

# Ensure log directory exists for migration hooks.
mkdir -p "${RELEASE_BASE}/logs"

# Snapshot config and data root metadata before upgrade.
SNAPSHOT_DIR=""
if [[ "${IS_UPGRADE}" -eq 1 ]]; then
  SNAPSHOT_DIR="${RELEASE_BASE}/snapshots/upgrade-$(date +%s)"
  mkdir -p "${SNAPSHOT_DIR}"
  info "Snapshotting config to ${SNAPSHOT_DIR}..."
  cp -a "${APP_ENV_DIR}/." "${SNAPSHOT_DIR}/config/" 2>/dev/null || true
  info "Snapshotting data root metadata..."
  mkdir -p "${SNAPSHOT_DIR}/data"
  if [[ -d "${DATA_ROOT}/_system" ]]; then
    cp -a "${DATA_ROOT}/_system" "${SNAPSHOT_DIR}/data/" 2>/dev/null || true
  fi
  # Snapshot project metadata and graph state so rollback can restore project listing.
  for proj_dir in "${DATA_ROOT}"/*/; do
    if [[ ! -d "${proj_dir}" ]]; then
      continue
    fi
    proj_name="$(basename "${proj_dir}")"
    # Skip non-project directories.
    [[ "${proj_name}" == "_system" ]] && continue
    mkdir -p "${SNAPSHOT_DIR}/data/${proj_name}/graph"
    if [[ -f "${proj_dir}/project.json" ]]; then
      cp -a "${proj_dir}/project.json" "${SNAPSHOT_DIR}/data/${proj_name}/" 2>/dev/null || true
    fi
    if [[ -d "${proj_dir}/graph" ]]; then
      cp -a "${proj_dir}/graph/." "${SNAPSHOT_DIR}/data/${proj_name}/graph/" 2>/dev/null || true
    fi
  done
fi

# Run migration hooks for upgrades (env Python is available now).
MIGRATION_FAILED=0
if [[ "${IS_UPGRADE}" -eq 1 ]]; then
  MIGRATION_PREFLIGHT="$(${ENV_PYTHON} -c "import json,sys; print(json.load(open(sys.argv[1])).get('migrations',{}).get('preflight',''))" "${VERSION_DIR}/release.json")"
  MIGRATION_APPLY="$(${ENV_PYTHON} -c "import json,sys; print(json.load(open(sys.argv[1])).get('migrations',{}).get('apply',''))" "${VERSION_DIR}/release.json")"

  if [[ -n "${MIGRATION_PREFLIGHT}" && -x "${VERSION_DIR}/${MIGRATION_PREFLIGHT}" ]]; then
    info "Running migration preflight..."
    if ! BLUEPRINT_DATA_ROOT="${DATA_ROOT}" BLUEPRINT_SNAPSHOT_DIR="${SNAPSHOT_DIR}" \
         BLUEPRINT_PREV_RELEASE="${PREV_TARGET}" \
         "${VERSION_DIR}/${MIGRATION_PREFLIGHT}" >>"${RELEASE_BASE}/logs/preflight.log" 2>&1; then
      warn "Migration preflight failed. See ${RELEASE_BASE}/logs/preflight.log"
      MIGRATION_FAILED=1
    fi
  fi

  if [[ "${MIGRATION_FAILED}" -eq 0 && -n "${MIGRATION_APPLY}" && -x "${VERSION_DIR}/${MIGRATION_APPLY}" ]]; then
    info "Running migration apply..."
    if ! BLUEPRINT_DATA_ROOT="${DATA_ROOT}" BLUEPRINT_SNAPSHOT_DIR="${SNAPSHOT_DIR}" \
         BLUEPRINT_PREV_RELEASE="${PREV_TARGET}" \
         "${VERSION_DIR}/${MIGRATION_APPLY}" >>"${RELEASE_BASE}/logs/apply.log" 2>&1; then
      warn "Migration apply failed. See ${RELEASE_BASE}/logs/apply.log"
      MIGRATION_FAILED=1
    fi
  fi
fi

# Rollback helper.
rollback() {
  info "ROLLBACK: restoring previous release..."
  if [[ -n "${PREV_TARGET}" && -d "${PREV_TARGET}" ]]; then
    ln -sfn "${PREV_TARGET}" "${CURRENT_LINK}"
    info "Restored current symlink to ${PREV_TARGET}"
  fi
  if [[ -n "${SNAPSHOT_DIR}" && -d "${SNAPSHOT_DIR}/config" ]]; then
    rm -rf "${APP_ENV_DIR}"
    cp -a "${SNAPSHOT_DIR}/config" "${APP_ENV_DIR}"
    info "Restored config from snapshot."
  fi
  # Re-deploy previous release with full env and health checks.
  if [[ -n "${PREV_TARGET}" && -d "${PREV_TARGET}" ]]; then
    info "Re-deploying previous release..."
    if run_deploy_for_release "${PREV_TARGET}" --upgrade --services-stopped; then
      if wait_for_health; then
        info "Previous release is healthy after rollback."
      else
        warn "Previous release deployed but health checks did not pass."
      fi
    else
      warn "Previous release deploy failed during rollback."
    fi
  fi
  die "Upgrade failed. Previous release has been restored."
}

# If migration failed, rollback immediately.
if [[ "${MIGRATION_FAILED}" -eq 1 ]]; then
  rollback
fi

ln -sfn "${VERSION_DIR}" "${CURRENT_LINK}"

# ---------------------------------------------------------------------------
# Phase 10: Run deploy
# ---------------------------------------------------------------------------

info "Phase 10: Running deploy"

DEPLOY_ARGS=()
if [[ "${IS_UPGRADE}" -eq 1 ]]; then
  DEPLOY_ARGS+=("--upgrade")
  DEPLOY_ARGS+=("--services-stopped")
fi

if ! run_deploy "${DEPLOY_ARGS[@]}"; then
  if [[ "${IS_UPGRADE}" -eq 1 ]]; then
    rollback
  else
    die "Deploy failed."
  fi
fi

# Health check after fresh install or upgrade.
info "Waiting for health checks..."
if ! wait_for_health; then
  if [[ "${IS_UPGRADE}" -eq 1 ]]; then
    warn "Health checks failed after upgrade; rolling back..."
    rollback
  else
    die "Health checks failed after install."
  fi
fi
info "Health checks passed."

# ---------------------------------------------------------------------------
# Phase 11: Cleanup old releases (keep last 2)
# ---------------------------------------------------------------------------

info "Phase 11: Cleanup"

if [[ -d "${RELEASES_DIR}" ]]; then
  # Sort versions and keep the 2 most recent. Backup directories are not
  # considered release versions and are never auto-removed.
  mapfile -t ALL_VERSIONS < <(ls -1 "${RELEASES_DIR}" | grep -E '^[0-9]+(\.[0-9]+)*$' | sort -V -r)
  if [[ "${#ALL_VERSIONS[@]}" -gt 2 ]]; then
    for old_ver in "${ALL_VERSIONS[@]:2}"; do
      old_path="${RELEASES_DIR}/${old_ver}"
      # Never remove the currently active release.
      if [[ "$(readlink -f "${CURRENT_LINK}" 2>/dev/null || true)" == "$(readlink -f "${old_path}" 2>/dev/null || true)" ]]; then
        continue
      fi
      info "Removing old release: ${old_ver}"
      rm -rf "${old_path}"
    done
  fi
fi

# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------

info "Installation complete."
info "Version:  ${RELEASE_VERSION}"
info "Release:  ${CURRENT_LINK} -> ${VERSION_DIR}"
info "Data:     ${DATA_ROOT}"
info ""
info "Frontend: http://127.0.0.1:13001"
info "Backend:  http://127.0.0.1:18001"

# Stop here; anything after exit 0 is the binary payload.
exit 0

# Mark the end of the script; anything after this line is the payload.
__PAYLOAD_START__
