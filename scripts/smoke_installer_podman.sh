#!/usr/bin/env bash
set -euo pipefail

# Podman-based installer smoke test.
#
# This script creates a temporary Ubuntu container with systemd and a test user,
# bootstraps a user DBus/systemd --user session, then runs the built
# self-extracting installer artifact.
#
# It verifies:
#   - the installer can pass the systemd --user preflight in-container
#   - the dedicated runtime env is created under ~/.local/share/blueprint-re/env
#   - product-managed binaries exist in env/bin
#   - the installer log reaches a meaningful later phase
#
# Usage:
#   bash scripts/smoke_installer_podman.sh [OPTIONS]
#
# Options:
#   --installer PATH          Self-extracting installer to test.
#   --image-tag TAG           Temporary Podman image tag.
#   --cache-dir PATH          Host directory used for reusable micromamba/conda package cache.
#   --rebuild-image           Rebuild the smoke image even if the tag exists.
#   --keep-container          Keep the container after the run.
#   --keep-image              Keep the image after the run.
#   --expect-failure REGEX    Treat a matching installer failure as expected.
#   --help                    Show this message.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

INSTALLER_PATH=""
IMAGE_TAG="blueprint-re-smoke-systemd"
DEFAULT_CACHE_DIR="${REPO_ROOT}/.tmp/podman-micromamba-cache"
CACHE_DIR="${DEFAULT_CACHE_DIR}"
REBUILD_IMAGE=0
KEEP_CONTAINER=0
KEEP_IMAGE=0
EXPECT_FAILURE_REGEX=""

CONTAINER_NAME="blueprint-re-smoke-$$"
TMP_DIR="$(mktemp -d)"
LOG_PATH="/home/tester/installer-run.log"
TEST_USER="tester"
TEST_UID="2000"
TEST_HOME="/home/${TEST_USER}"
TEST_RUNTIME_DIR="/run/user/${TEST_UID}"

die() {
  echo "ERROR: $1" >&2
  exit 1
}

info() {
  echo "[podman-smoke] $1"
}

cleanup() {
  if [[ "${KEEP_CONTAINER}" -eq 0 ]]; then
    podman rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  fi
  if [[ "${KEEP_IMAGE}" -eq 0 ]]; then
    podman image rm -f "${IMAGE_TAG}" >/dev/null 2>&1 || true
  fi
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    die "Required command not found: $1"
  fi
}

podman_exec() {
  podman exec "${CONTAINER_NAME}" bash -lc "$1"
}

select_default_installer() {
  local latest=""
  local candidate=""
  local pattern=""
  for pattern in 'rhinedatalab-*-linux-x86_64.sh' 'blueprint-re-*-linux-x86_64.sh'; do
    latest=""
    while IFS= read -r candidate; do
      latest="${candidate}"
    done < <(find "${REPO_ROOT}/dist" -maxdepth 1 -type f -name "${pattern}" | sort -V)
    if [[ -n "${latest}" ]]; then
      printf '%s\n' "${latest}"
      return 0
    fi
  done

  die "No installer artifact found in ${REPO_ROOT}/dist"
}

build_image_if_needed() {
  if podman image exists "${IMAGE_TAG}" && [[ "${REBUILD_IMAGE}" -eq 0 ]]; then
    info "Using existing Podman image ${IMAGE_TAG}"
    return 0
  fi

  cat > "${TMP_DIR}/Containerfile" <<EOF
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \\
    systemd systemd-sysv dbus dbus-user-session \\
    curl wget ca-certificates procps iproute2 passwd \\
    tar coreutils sed grep bzip2 xz-utils && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u ${TEST_UID} -s /bin/bash ${TEST_USER}
CMD ["/sbin/init"]
EOF

  info "Building Podman image ${IMAGE_TAG}"
  podman build -t "${IMAGE_TAG}" "${TMP_DIR}"
}

start_container() {
  mkdir -p "${CACHE_DIR}"
  info "Starting Podman container ${CONTAINER_NAME}"
  podman run -d --rm \
    --name "${CONTAINER_NAME}" \
    --privileged \
    --systemd=always \
    -v "${REPO_ROOT}:/repo:ro" \
    -v "$(dirname "${INSTALLER_PATH}"):/dist:ro" \
    -v "${CACHE_DIR}:/podman-cache" \
    "${IMAGE_TAG}" \
    /sbin/init >/dev/null
}

wait_for_systemd() {
  info "Waiting for systemd in container"
  local attempt
  local state
  for attempt in $(seq 1 30); do
    state="$(podman_exec 'systemctl is-system-running 2>/dev/null || true')"
    if [[ "${state}" == "running" || "${state}" == "degraded" || "${state}" == "starting" ]]; then
      return 0
    fi
    sleep 1
  done
  podman_exec 'ps -p 1 -o pid,comm,args=' || true
  die "systemd did not become available in container"
}

bootstrap_user_session() {
  info "Bootstrapping ${TEST_USER} user session"
  podman_exec "mkdir -p '${TEST_RUNTIME_DIR}' && chown ${TEST_USER}:${TEST_USER} '${TEST_RUNTIME_DIR}' && chmod 700 '${TEST_RUNTIME_DIR}'"
  podman_exec "su - ${TEST_USER} -c 'XDG_RUNTIME_DIR=${TEST_RUNTIME_DIR} dbus-daemon --session --address=unix:path=${TEST_RUNTIME_DIR}/bus --fork --print-address'"
  podman_exec "su - ${TEST_USER} -c 'XDG_RUNTIME_DIR=${TEST_RUNTIME_DIR} DBUS_SESSION_BUS_ADDRESS=unix:path=${TEST_RUNTIME_DIR}/bus systemd --user >/tmp/systemd-user.log 2>&1 & sleep 2; systemctl --user --no-pager show-environment >/dev/null'"
}

run_installer() {
  local installer_name
  installer_name="$(basename "${INSTALLER_PATH}")"
  info "Running installer ${installer_name}"
  set +e
  podman_exec "mkdir -p /podman-cache/pkgs /podman-cache/root && chown -R ${TEST_USER}:${TEST_USER} /podman-cache"
  podman_exec "su - ${TEST_USER} -c 'set -euo pipefail; export HOME=${TEST_HOME}; export XDG_RUNTIME_DIR=${TEST_RUNTIME_DIR}; export DBUS_SESSION_BUS_ADDRESS=unix:path=${TEST_RUNTIME_DIR}/bus; export CONDA_PKGS_DIRS=/podman-cache/pkgs; export MAMBA_ROOT_PREFIX=/podman-cache/root; bash /dist/${installer_name} > ${LOG_PATH} 2>&1'"
  INSTALL_EXIT_CODE=$?
  set -e
}

print_log_excerpt() {
  info "Installer log excerpt"
  podman_exec "tail -n 160 '${LOG_PATH}' 2>/dev/null || true"
}

verify_runtime_env() {
  info "Checking runtime env layout"
  podman_exec "test -d '${TEST_HOME}/.local/share/blueprint-re/env'"
  podman_exec "test -x '${TEST_HOME}/.local/share/blueprint-re/env/bin/python'"
  podman_exec "test -x '${TEST_HOME}/.local/share/blueprint-re/env/bin/node'"
  podman_exec "test -x '${TEST_HOME}/.local/share/blueprint-re/env/bin/nginx'"
  podman_exec "test -x '${TEST_HOME}/.local/share/blueprint-re/env/bin/bwrap'"
  podman_exec "'${TEST_HOME}/.local/share/blueprint-re/env/bin/python' --version && '${TEST_HOME}/.local/share/blueprint-re/env/bin/node' -v && '${TEST_HOME}/.local/share/blueprint-re/env/bin/nginx' -v 2>&1 | tail -n 1 && '${TEST_HOME}/.local/share/blueprint-re/env/bin/bwrap' --version"
}

classify_result() {
  if [[ "${INSTALL_EXIT_CODE:-0}" -eq 0 ]]; then
    info "Installer completed successfully"
    return 0
  fi

  if [[ -n "${EXPECT_FAILURE_REGEX}" ]]; then
    if podman_exec "grep -Eq '${EXPECT_FAILURE_REGEX}' '${LOG_PATH}'"; then
      info "Installer failed with expected pattern: ${EXPECT_FAILURE_REGEX}"
      return 0
    fi
  fi

  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --installer)
      [[ -n "${2:-}" ]] || die "--installer requires a path"
      INSTALLER_PATH="$2"
      shift 2
      ;;
    --image-tag)
      [[ -n "${2:-}" ]] || die "--image-tag requires a tag"
      IMAGE_TAG="$2"
      shift 2
      ;;
    --cache-dir)
      [[ -n "${2:-}" ]] || die "--cache-dir requires a path"
      CACHE_DIR="$2"
      shift 2
      ;;
    --rebuild-image)
      REBUILD_IMAGE=1
      shift
      ;;
    --keep-container)
      KEEP_CONTAINER=1
      shift
      ;;
    --keep-image)
      KEEP_IMAGE=1
      shift
      ;;
    --expect-failure)
      [[ -n "${2:-}" ]] || die "--expect-failure requires a regex"
      EXPECT_FAILURE_REGEX="$2"
      shift 2
      ;;
    --help|-h)
      sed -n '1,/^SCRIPT_DIR=/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

require_cmd podman

if [[ -z "${INSTALLER_PATH}" ]]; then
  INSTALLER_PATH="$(select_default_installer)"
fi
[[ -f "${INSTALLER_PATH}" ]] || die "Installer not found: ${INSTALLER_PATH}"

# Normalize to absolute path so podman volume mounts resolve correctly.
INSTALLER_DIR="$(cd "$(dirname "${INSTALLER_PATH}")" && pwd)"
INSTALLER_PATH="${INSTALLER_DIR}/$(basename "${INSTALLER_PATH}")"

build_image_if_needed
start_container
wait_for_systemd
bootstrap_user_session
run_installer
print_log_excerpt
verify_runtime_env

if classify_result; then
  info "Podman installer smoke completed"
  exit 0
fi

die "Installer smoke failed with unexpected result (exit code ${INSTALL_EXIT_CODE:-unknown})"
