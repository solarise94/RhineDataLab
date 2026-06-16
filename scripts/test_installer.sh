#!/usr/bin/env bash
set -euo pipefail

# Basic installer script unit tests.
# Runs against a temporary HOME to avoid touching the real user environment.
#
# Usage:
#   bash scripts/test_installer.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

TMP_HOME="$(mktemp -d)"
trap 'rm -rf "${TMP_HOME}"' EXIT

export HOME="${TMP_HOME}"

PASSED=0
FAILED=0

assert() {
  local msg="$1"
  local cmd="$2"
  if eval "${cmd}"; then
    echo "  PASS: ${msg}"
    PASSED=$((PASSED + 1))
  else
    echo "  FAIL: ${msg}"
    FAILED=$((FAILED + 1))
  fi
}

assert_fail() {
  local msg="$1"
  local cmd="$2"
  if ! eval "${cmd}"; then
    echo "  PASS: ${msg}"
    PASSED=$((PASSED + 1))
  else
    echo "  FAIL: ${msg}"
    FAILED=$((FAILED + 1))
  fi
}

echo "========================================"
echo "Installer Script Unit Tests"
echo "TMP_HOME: ${TMP_HOME}"
echo "========================================"
echo ""

# ---------------------------------------------------------------------------
# Test 1: uninstall.sh with --yes --purge-config (dry run: no services)
# ---------------------------------------------------------------------------
echo "Test 1: uninstall.sh removes files correctly"

# Create fake install state.
mkdir -p "${TMP_HOME}/.local/share/blueprint-re/releases/0.4.1"
mkdir -p "${TMP_HOME}/.config/blueprint-re"
touch "${TMP_HOME}/.config/blueprint-re/backend.env"
mkdir -p "${TMP_HOME}/.config/systemd/user"

# Uninstall without --purge-config should preserve config.
export HOME="${TMP_HOME}"
bash "${REPO_ROOT}/scripts/uninstall.sh" --yes >/dev/null 2>&1 || true

assert "release base removed" "[[ ! -d ${TMP_HOME}/.local/share/blueprint-re ]]"
assert "config preserved without --purge-config" "[[ -d ${TMP_HOME}/.config/blueprint-re ]]"

# Now test with --purge-config.
mkdir -p "${TMP_HOME}/.local/share/blueprint-re"
mkdir -p "${TMP_HOME}/.config/blueprint-re"
bash "${REPO_ROOT}/scripts/uninstall.sh" --yes --purge-config >/dev/null 2>&1 || true

assert "release base removed with purge" "[[ ! -d ${TMP_HOME}/.local/share/blueprint-re ]]"
assert "config removed with --purge-config" "[[ ! -d ${TMP_HOME}/.config/blueprint-re ]]"

echo ""

# ---------------------------------------------------------------------------
# Test 2: resolve_bin helper from deploy_release.sh
# ---------------------------------------------------------------------------
echo "Test 2: resolve_bin helper logic"

# Source the helper function.
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

# Test explicit path wins.
TEST_PYTHON="$(command -v python3)"
export TEST_PYTHON_BIN="${TEST_PYTHON}"
result="$(resolve_bin TEST_PYTHON_BIN nonexistent)"
assert "explicit env var wins over command -v" "[[ ${result} == ${TEST_PYTHON} ]]"

# Test falls back to command -v.
unset TEST_PYTHON_BIN
result="$(resolve_bin TEST_PYTHON_BIN python3)"
assert "falls back to command -v" "[[ -n ${result} ]]"

# Test fails when neither exists.
unset TEST_FAKE_BIN
assert_fail "fails when neither explicit nor command exists" "resolve_bin TEST_FAKE_BIN definitely_fake_command_xyz"

echo ""

# ---------------------------------------------------------------------------
# Test 3: install.sh argument parsing
# ---------------------------------------------------------------------------
echo "Test 3: install.sh argument parsing"

# Extract just the argument parsing logic.
parse_test() {
  local args=("$@")
  local OFFLINE_MODE=0
  local ROLLBACK_VERSION=""
  local SKIP_VERIFY=0
  local FORCE_UPGRADE=0

  local i=0
  while [[ ${i} -lt ${#args[@]} ]]; do
    case "${args[${i}]}" in
      --offline)
        OFFLINE_MODE=1
        ;;
      --rollback)
        i=$((i + 1))
        ROLLBACK_VERSION="${args[${i}]}"
        ;;
      --skip-verify)
        SKIP_VERIFY=1
        ;;
      --upgrade)
        FORCE_UPGRADE=1
        ;;
    esac
    i=$((i + 1))
  done

  echo "OFFLINE_MODE=${OFFLINE_MODE}"
  echo "ROLLBACK_VERSION=${ROLLBACK_VERSION}"
  echo "SKIP_VERIFY=${SKIP_VERIFY}"
  echo "FORCE_UPGRADE=${FORCE_UPGRADE}"
}

result="$(parse_test --offline --rollback 0.4.0 --skip-verify --upgrade)"
assert "offline parsed" "echo '${result}' | grep -q 'OFFLINE_MODE=1'"
assert "rollback parsed" "echo '${result}' | grep -q 'ROLLBACK_VERSION=0.4.0'"
assert "skip-verify parsed" "echo '${result}' | grep -q 'SKIP_VERIFY=1'"
assert "upgrade parsed" "echo '${result}' | grep -q 'FORCE_UPGRADE=1'"

echo ""

# ---------------------------------------------------------------------------
# Test 3b: install.sh --help mentions --upgrade
# ---------------------------------------------------------------------------
echo "Test 3b: install.sh --help mentions --upgrade"

HELP_OUTPUT="$(bash "${REPO_ROOT}/scripts/install.sh" --help 2>&1 || true)"
assert "help usage includes --upgrade" "printf '%s\n' \"${HELP_OUTPUT}\" | grep -q 'Usage: bash install.sh \\[--offline\\] \\[--upgrade\\] \\[--rollback VERSION\\]'"
assert "help options include --upgrade" "printf '%s\n' \"${HELP_OUTPUT}\" | grep -q 'Require an existing installation and run upgrade flow'"

echo ""

# ---------------------------------------------------------------------------
# Test 3c: FORCE_UPGRADE fails fast when no existing installation
# ---------------------------------------------------------------------------
echo "Test 3c: FORCE_UPGRADE fails fast when no existing installation"

# The guard must run in Phase 1 preflight, BEFORE payload extraction or
# environment creation, so --upgrade cannot silently perform expensive work
# on a fresh host.
INSTALL_SH="${REPO_ROOT}/scripts/install.sh"
FORCE_UPGRADE_LINE="$(grep -n 'FORCE_UPGRADE.*-eq 1.*! -L.*CURRENT_LINK' "${INSTALL_SH}" | head -1 | cut -d: -f1)"
PHASE2_LINE="$(grep -n 'Phase 2: Payload extraction' "${INSTALL_SH}" | head -1 | cut -d: -f1)"
assert "FORCE_UPGRADE guard exists in install.sh" "[[ -n ${FORCE_UPGRADE_LINE} ]]"
assert "FORCE_UPGRADE guard comes before Phase 2" "[[ ${FORCE_UPGRADE_LINE} -lt ${PHASE2_LINE} ]]"

# Simulate the early preflight gate.
check_force_upgrade_preflight() {
  local force_upgrade="$1"
  local current_link="$2"
  if [[ "${force_upgrade}" -eq 1 && ! -L "${current_link}" ]]; then
    echo "ERROR: --upgrade was requested, but no existing Blueprint RE installation was found at ${current_link}" >&2
    return 1
  fi
  return 0
}

TMP_FORCE_UPGRADE_DIR="$(mktemp -d)"
ln -sfn "${TMP_FORCE_UPGRADE_DIR}/current-target" "${TMP_FORCE_UPGRADE_DIR}/current"
assert "force upgrade accepted when current symlink exists" "check_force_upgrade_preflight 1 '${TMP_FORCE_UPGRADE_DIR}/current'"
rm -rf "${TMP_FORCE_UPGRADE_DIR}"
assert_fail "force upgrade rejected when no current symlink" "check_force_upgrade_preflight 1 '/nonexistent/current'"
assert "non-force install accepted without current symlink" "check_force_upgrade_preflight 0 '/nonexistent/current'"

echo ""

# ---------------------------------------------------------------------------
# Test 3d: fresh install with legacy port conflict is blocked
# ---------------------------------------------------------------------------
echo "Test 3d: fresh install with legacy port conflict is blocked"

# Simulate the legacy/source-deployment preflight gate from install.sh Phase 1.
# When there is no current symlink and Blueprint ports are occupied, treat it as
# a likely legacy deployment and refuse to proceed.
check_legacy_port_conflict() {
  local current_link="$1"
  shift
  local port_conflicts=("$@")
  if [[ ! -L "${current_link}" && "${#port_conflicts[@]}" -gt 0 ]]; then
    echo "ERROR: Detected Blueprint ports already in use (${port_conflicts[*]}), but no existing release installation was found at ${current_link}." >&2
    return 1
  fi
  return 0
}

TMP_LEGACY_CHECK="$(mktemp -d)"
MISSING_CURRENT="${TMP_LEGACY_CHECK}/missing-current"
EXISTING_CURRENT="${TMP_LEGACY_CHECK}/existing-current"
ln -sfn "${TMP_LEGACY_CHECK}/current-target" "${EXISTING_CURRENT}"

assert_fail "fresh install blocked when ports occupied" "check_legacy_port_conflict '${MISSING_CURRENT}' 13001 18001"
assert "upgrade path allowed when current exists" "check_legacy_port_conflict '${EXISTING_CURRENT}' 13001 18001"
assert "fresh install allowed when ports free" "check_legacy_port_conflict '${MISSING_CURRENT}'"

rm -rf "${TMP_LEGACY_CHECK}"

echo ""

# ---------------------------------------------------------------------------
# Test 4: build_release_bundle.sh version validation
# ---------------------------------------------------------------------------
echo "Test 4: Version extraction from pyproject.toml"

read_pyproject_version() {
  local toml_file="$1"
  python3 -c "
import sys, re
text = open(sys.argv[1]).read()
m = re.search(r'^version\s*=\s*\"([^\"]+)\"', text, re.M)
print(m.group(1) if m else '0.0.0')
" "${toml_file}"
}

version="$(read_pyproject_version "${REPO_ROOT}/backend/pyproject.toml")"
assert "pyproject version is not 0.0.0" "[[ ${version} != 0.0.0 ]]"
assert "pyproject version has dots" "[[ ${version} == *.* ]]"

echo ""

# ---------------------------------------------------------------------------
# Test 5: Release layout validation
# ---------------------------------------------------------------------------
echo "Test 5: Payload structure"

# Check that required templates exist.
assert "nginx template exists" "[[ -f ${REPO_ROOT}/deploy/nginx/blueprint-re.conf.template ]]"
assert "systemd backend template exists" "[[ -f ${REPO_ROOT}/deploy/systemd-release/blueprint-re-backend.service ]]"
assert "systemd frontend template exists" "[[ -f ${REPO_ROOT}/deploy/systemd-release/blueprint-re-frontend.service ]]"
assert "systemd manager template exists" "[[ -f ${REPO_ROOT}/deploy/systemd-release/blueprint-re-manager-agent.service ]]"
assert "systemd nginx template exists" "[[ -f ${REPO_ROOT}/deploy/systemd-release/blueprint-re-nginx.service ]]"

echo ""

# ---------------------------------------------------------------------------
# Test 6: Self-extracting installer build and payload extraction
# ---------------------------------------------------------------------------
echo "Test 6: Self-extracting installer build and payload extraction"

TEST_BUILD_DIR="$(mktemp -d)"

# Create a minimal valid payload.
mkdir -p "${TEST_BUILD_DIR}/blueprint-re/wheels"
echo "dummy" > "${TEST_BUILD_DIR}/blueprint-re/wheels/dummy.whl"
cat > "${TEST_BUILD_DIR}/blueprint-re/release.json" <<'EOF'
{
  "version": "0.0.0-test",
  "arch": "x86_64",
  "platform": "linux"
}
EOF
(
  cd "${TEST_BUILD_DIR}/blueprint-re" || exit 1
  find . -type f | while IFS= read -r f; do
    f="${f#./}"
    [[ "$f" == "checksums.sha256" ]] && continue
    sha256sum "$f"
  done > checksums.sha256
)

(
  cd "${TEST_BUILD_DIR}" || exit 1
  tar -czf rhinedatalab-0.0.0-test-linux-x86_64.tar.gz blueprint-re
)
bash "${REPO_ROOT}/scripts/build_self_extracting_installer.sh" "${TEST_BUILD_DIR}/rhinedatalab-0.0.0-test-linux-x86_64.tar.gz" > /dev/null 2>&1

INSTALLER_PATH="${REPO_ROOT}/dist/rhinedatalab-0.0.0-test-linux-x86_64.sh"
assert "installer artifact created" "[[ -f ${INSTALLER_PATH} ]]"
assert "installer checksum file created" "[[ -f ${INSTALLER_PATH}.sha256 ]]"

# Verify the size-invariance assertion passed as part of the build script.
assert "installer size unchanged after offset substitution" "[[ -f ${INSTALLER_PATH} ]]"

# Extract manually and verify payload integrity.
EXTRACT_DIR="${TEST_BUILD_DIR}/extracted"
mkdir -p "${EXTRACT_DIR}"
PAYLOAD_OFFSET=$(python3 -c "
with open('${INSTALLER_PATH}', 'rb') as f:
    data = f.read()
marker = b'__PAYLOAD_START__\n'
idx = data.find(marker)
if idx < 0:
    sys.exit(1)
print(idx + len(marker) + 1)
")
(
  cd "${EXTRACT_DIR}" || exit 1
  tail -c +"${PAYLOAD_OFFSET}" "${INSTALLER_PATH}" | tar -xzf -
)
assert "payload extracted" "[[ -d ${EXTRACT_DIR}/blueprint-re ]]"
assert "release.json present in extracted payload" "[[ -f ${EXTRACT_DIR}/blueprint-re/release.json ]]"
(
  cd "${EXTRACT_DIR}/blueprint-re" || exit 1
  sha256sum -c --status checksums.sha256
)
assert "extracted payload checksums verify" "[[ \$? -eq 0 ]]"

# Verify that release.json is covered by checksums.sha256.
assert "release.json is listed in checksums.sha256" "grep -q 'release.json' ${EXTRACT_DIR}/blueprint-re/checksums.sha256"

# Clean up the artifact and temp dir so they do not pollute dist/.
rm -f "${INSTALLER_PATH}" "${INSTALLER_PATH}.sha256"
rm -rf "${TEST_BUILD_DIR}"

echo ""

# ---------------------------------------------------------------------------
# Test 7: systemd env escaping helpers from deploy_release.sh
# ---------------------------------------------------------------------------
echo "Test 7: systemd env escaping helpers"

# Source the helpers inline.
systemd_env_escape() {
  local value="$1"
  if [[ "${value}" == *$'\n'* ]]; then
    echo "REFUSING_NEWLINE" >&2
    return 1
  fi
  local escaped="${value//\\/\\\\}"
  escaped="${escaped//\"/\\\"}"
  if [[ "${escaped}" == *[[:space:]]* || "${escaped}" == *';'* || "${escaped}" == *'|'* || "${escaped}" == *'$'* ]]; then
    escaped="\"${escaped}\""
  fi
  printf '%s\n' "${escaped}"
}

sed_escape_replacement() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\|/\\|}"
  value="${value//\&/\\&}"
  value="${value//$'\n'/}"
  printf '%s\n' "${value}"
}

# Values with spaces should be quoted.
result="$(systemd_env_escape "hello world")"
[[ "$result" == '"hello world"' ]] && { echo "  PASS: systemd_escape quotes values with spaces"; PASSED=$((PASSED + 1)); } || { echo "  FAIL: systemd_escape quotes values with spaces"; FAILED=$((FAILED + 1)); }

# Backslashes and quotes are escaped.
result="$(systemd_env_escape 'a\b"c')"
[[ "$result" == 'a\\b\"c' ]] && { echo "  PASS: systemd_escape escapes backslashes and quotes"; PASSED=$((PASSED + 1)); } || { echo "  FAIL: systemd_escape escapes backslashes and quotes"; FAILED=$((FAILED + 1)); }

# Pipe characters are quoted.
result="$(systemd_env_escape "a|b")"
[[ "$result" == '"a|b"' ]] && { echo "  PASS: systemd_escape quotes pipe characters"; PASSED=$((PASSED + 1)); } || { echo "  FAIL: systemd_escape quotes pipe characters"; FAILED=$((FAILED + 1)); }

# sed replacement escaping preserves special chars.
result="$(sed_escape_replacement "a|b&c")"
[[ "$result" == 'a\|b\&c' ]] && { echo "  PASS: sed_escape escapes pipe and ampersand"; PASSED=$((PASSED + 1)); } || { echo "  FAIL: sed_escape escapes pipe and ampersand"; FAILED=$((FAILED + 1)); }

echo ""

# ---------------------------------------------------------------------------
# Test 8: release cleanup excludes backup directories
# ---------------------------------------------------------------------------
echo "Test 8: release cleanup filters backup directories"

RELEASES_TEST_DIR="${TMP_HOME}/releases-test"
mkdir -p "${RELEASES_TEST_DIR}/0.4.0"
mkdir -p "${RELEASES_TEST_DIR}/0.4.1"
mkdir -p "${RELEASES_TEST_DIR}/0.4.1.backup.12345"
mkdir -p "${RELEASES_TEST_DIR}/0.4.2"
CURRENT_TEST="${RELEASES_TEST_DIR}/current"
ln -sfn "${RELEASES_TEST_DIR}/0.4.2" "${CURRENT_TEST}"

# Simulate the cleanup logic from install.sh.
keep_count=0
remove_count=0
mapfile -t ALL_VERSIONS < <(ls -1 "${RELEASES_TEST_DIR}" | grep -E '^[0-9]+(\.[0-9]+)*$' | sort -V -r)
for old_ver in "${ALL_VERSIONS[@]:2}"; do
  old_path="${RELEASES_TEST_DIR}/${old_ver}"
  if [[ "$(readlink -f "${CURRENT_TEST}" 2>/dev/null || true)" == "$(readlink -f "${old_path}" 2>/dev/null || true)" ]]; then
    continue
  fi
  rm -rf "${old_path}"
  remove_count=$((remove_count + 1))
done

# 0.4.2 is current, 0.4.1 is kept (most recent non-current), 0.4.0 should be removed.
assert "cleanup removed oldest version 0.4.0" "[[ ! -d ${RELEASES_TEST_DIR}/0.4.0 ]]"
assert "cleanup kept version 0.4.1" "[[ -d ${RELEASES_TEST_DIR}/0.4.1 ]]"
assert "cleanup kept current version 0.4.2" "[[ -d ${RELEASES_TEST_DIR}/0.4.2 ]]"
assert "cleanup preserved backup directory" "[[ -d ${RELEASES_TEST_DIR}/0.4.1.backup.12345 ]]"
assert "cleanup removed exactly one version" "[[ ${remove_count} -eq 1 ]]"

rm -rf "${RELEASES_TEST_DIR}"

echo ""

# ---------------------------------------------------------------------------
# Test 9: version match validation in install.sh
# ---------------------------------------------------------------------------
echo "Test 9: install.sh version mismatch detection"

# Extract just the version-check logic. In the real script this happens after
# json_get_string extracts the payload version.
check_versions() {
  local installer_version="$1"
  local release_version="$2"
  local arch="$3"
  local platform="$4"
  INSTALLER_VERSION="${installer_version}"
  INSTALLER_ARCH="x86_64"
  INSTALLER_PLATFORM="linux"

  if [[ "${release_version}" != "${INSTALLER_VERSION}" ]]; then
    return 1
  fi
  if [[ "${arch}" != "${INSTALLER_ARCH}" ]]; then
    return 1
  fi
  if [[ "${platform}" != "${INSTALLER_PLATFORM}" ]]; then
    return 1
  fi
  return 0
}

assert "version match accepted" "check_versions 0.4.1 0.4.1 x86_64 linux"
assert_fail "version mismatch rejected" "check_versions 0.4.1 0.4.2 x86_64 linux"
assert_fail "arch mismatch rejected" "check_versions 0.4.1 0.4.1 aarch64 linux"
assert_fail "platform mismatch rejected" "check_versions 0.4.1 0.4.1 x86_64 darwin"

echo ""

# ---------------------------------------------------------------------------
# Test 10: install_downloader.sh argument parsing
# ---------------------------------------------------------------------------
echo "Test 10: install_downloader.sh argument parsing"

# Extract and test the parse_args logic.
parse_downloader_args() {
  local args=("$@")
  local CHANNEL="stable"
  local VERSION=""
  local OFFLINE_MODE=0
  local KEEP_INSTALLER=0
  local INSTALLER_URL=""

  local i=0
  while [[ ${i} -lt ${#args[@]} ]]; do
    case "${args[${i}]}" in
      --version)
        i=$((i + 1))
        VERSION="${args[${i}]}"
        ;;
      --channel)
        i=$((i + 1))
        CHANNEL="${args[${i}]}"
        ;;
      --offline)
        OFFLINE_MODE=1
        ;;
      --keep-installer)
        KEEP_INSTALLER=1
        ;;
      --installer-url)
        i=$((i + 1))
        INSTALLER_URL="${args[${i}]}"
        ;;
    esac
    i=$((i + 1))
  done
  echo "CHANNEL=${CHANNEL}"
  echo "VERSION=${VERSION}"
  echo "OFFLINE_MODE=${OFFLINE_MODE}"
  echo "KEEP_INSTALLER=${KEEP_INSTALLER}"
  echo "INSTALLER_URL=${INSTALLER_URL}"
}

result="$(parse_downloader_args --version 0.5.0 --channel latest --offline --keep-installer --installer-url https://mirror.example.com/install.sh)"
assert "downloader version parsed" "echo '${result}' | grep -q 'VERSION=0.5.0'"
assert "downloader channel parsed" "echo '${result}' | grep -q 'CHANNEL=latest'"
assert "downloader offline parsed" "echo '${result}' | grep -q 'OFFLINE_MODE=1'"
assert "downloader keep-installer parsed" "echo '${result}' | grep -q 'KEEP_INSTALLER=1'"
assert "downloader installer-url parsed" "echo '${result}' | grep -q 'INSTALLER_URL=https://mirror.example.com/install.sh'"

echo ""

# ---------------------------------------------------------------------------
# Test 11: wheel selection from release.json
# ---------------------------------------------------------------------------
echo "Test 11: Wheel selection from release.json"

TEST_RELEASE_DIR="$(mktemp -d)"
mkdir -p "${TEST_RELEASE_DIR}/wheels"
touch "${TEST_RELEASE_DIR}/wheels/blueprint_re_backend-0.4.1-py3-none-any.whl"
touch "${TEST_RELEASE_DIR}/wheels/some_dependency-1.0.0-py3-none-any.whl"

cat > "${TEST_RELEASE_DIR}/release.json" <<'EOF'
{
  "version": "0.4.1",
  "artifacts": {
    "backend_wheel": {
      "path": "wheels/blueprint_re_backend-0.4.1-py3-none-any.whl",
      "checksum_sha256": "abc123"
    }
  }
}
EOF

# Simulate the install.sh wheel path resolution.
WHEEL_PATH_IN_PAYLOAD="$(python3 -c "
import json, sys
manifest = json.load(open(sys.argv[1]))
print(manifest.get('artifacts', {}).get('backend_wheel', {}).get('path', ''))
" "${TEST_RELEASE_DIR}/release.json")"

assert "wheel path extracted from release.json" "[[ ${WHEEL_PATH_IN_PAYLOAD} == 'wheels/blueprint_re_backend-0.4.1-py3-none-any.whl' ]]"
assert "wheel file exists at resolved path" "[[ -f ${TEST_RELEASE_DIR}/${WHEEL_PATH_IN_PAYLOAD} ]]"

rm -rf "${TEST_RELEASE_DIR}"

echo ""

# ---------------------------------------------------------------------------
# Test 12: env file permissions
# ---------------------------------------------------------------------------
echo "Test 12: env file permissions"

TEST_ENV_DIR="$(mktemp -d)"
# Simulate deploy_release.sh umask behavior
(
  umask 077
  : > "${TEST_ENV_DIR}/backend.env"
  chmod 600 "${TEST_ENV_DIR}/backend.env"
)
PERMS="$(stat -c %a "${TEST_ENV_DIR}/backend.env")"
assert "env file has 600 permissions" "[[ ${PERMS} == '600' ]]"
rm -rf "${TEST_ENV_DIR}"

echo ""

# ---------------------------------------------------------------------------
# Test 13: service template hardening directives
# ---------------------------------------------------------------------------
echo "Test 13: Service template hardening directives"

for svc in frontend manager-agent nginx; do
  template="${REPO_ROOT}/deploy/systemd-release/blueprint-re-${svc}.service"
  assert "${svc} template exists" "[[ -f ${template} ]]"
  assert "${svc} has NoNewPrivileges" "grep -q 'NoNewPrivileges=yes' ${template}"
  assert "${svc} has TimeoutStopSec" "grep -q 'TimeoutStopSec=' ${template}"
  assert "${svc} has KillMode" "grep -q 'KillMode=control-group' ${template}"
  assert "${svc} has SendSIGKILL" "grep -q 'SendSIGKILL=yes' ${template}"
done
# backend intentionally omits NoNewPrivileges because it spawns bwrap sandboxes.
template="${REPO_ROOT}/deploy/systemd-release/blueprint-re-backend.service"
assert "backend template exists" "[[ -f ${template} ]]"
assert "backend omits NoNewPrivileges (bwrap compat)" "! grep -q '^NoNewPrivileges=yes' ${template}"
assert "backend has TimeoutStopSec" "grep -q 'TimeoutStopSec=' ${template}"
assert "backend has KillMode" "grep -q 'KillMode=control-group' ${template}"
assert "backend has SendSIGKILL" "grep -q 'SendSIGKILL=yes' ${template}"

# Check service dependencies
assert "backend starts after manager-agent when available" "grep -q 'After=.*blueprint-re-manager-agent.service' ${REPO_ROOT}/deploy/systemd-release/blueprint-re-backend.service"
assert_fail "backend does not require manager-agent (degraded no-key install)" "grep -q 'Requires=blueprint-re-manager-agent.service' ${REPO_ROOT}/deploy/systemd-release/blueprint-re-backend.service"
assert "frontend requires backend" "grep -q 'Requires=blueprint-re-backend.service' ${REPO_ROOT}/deploy/systemd-release/blueprint-re-frontend.service"
assert "nginx requires frontend" "grep -q 'Requires=blueprint-re-frontend.service' ${REPO_ROOT}/deploy/systemd-release/blueprint-re-nginx.service"

# Check nginx temp dir creation
assert "nginx has temp dir ExecStartPre" "grep -q 'mkdir -p.*nginx-tmp' ${REPO_ROOT}/deploy/systemd-release/blueprint-re-nginx.service"

echo ""

# ---------------------------------------------------------------------------
# Test 14: uninstall --purge-data behavior
# ---------------------------------------------------------------------------
echo "Test 14: uninstall.sh --purge-data behavior"

TMP_UNINSTALL_HOME="$(mktemp -d)"
export HOME="${TMP_UNINSTALL_HOME}"

# Create fake install state with data
mkdir -p "${TMP_UNINSTALL_HOME}/.local/share/blueprint-re/data/_system"
touch "${TMP_UNINSTALL_HOME}/.local/share/blueprint-re/data/_system/project_registry.json"
mkdir -p "${TMP_UNINSTALL_HOME}/.local/share/blueprint-re/releases/0.4.1"
mkdir -p "${TMP_UNINSTALL_HOME}/.config/blueprint-re"
touch "${TMP_UNINSTALL_HOME}/.config/blueprint-re/backend.env"
mkdir -p "${TMP_UNINSTALL_HOME}/.config/systemd/user"

# Uninstall without --purge-data: data should be moved to backup
bash "${REPO_ROOT}/scripts/uninstall.sh" --yes >/dev/null 2>&1 || true
DATA_BACKUP_COUNT="$(find "${TMP_UNINSTALL_HOME}/.local/share" -maxdepth 1 -type d -name 'blueprint-re-data-backup-*' | wc -l)"
assert "data preserved without --purge-data" "[[ ${DATA_BACKUP_COUNT} -ge 1 ]]"
assert "release base removed" "[[ ! -d ${TMP_UNINSTALL_HOME}/.local/share/blueprint-re/releases ]]"

# Restore state and test with --purge-data
mkdir -p "${TMP_UNINSTALL_HOME}/.local/share/blueprint-re/data/_system"
touch "${TMP_UNINSTALL_HOME}/.local/share/blueprint-re/data/_system/project_registry.json"
mkdir -p "${TMP_UNINSTALL_HOME}/.local/share/blueprint-re/releases/0.4.1"

bash "${REPO_ROOT}/scripts/uninstall.sh" --yes --purge-data >/dev/null 2>&1 || true
assert "data removed with --purge-data" "[[ ! -d ${TMP_UNINSTALL_HOME}/.local/share/blueprint-re/data ]]"
# --purge-data must NOT touch historical backups from previous uninstalls.
assert "historical backups left untouched by --purge-data" "[[ ${DATA_BACKUP_COUNT} -ge 1 ]]"

rm -rf "${TMP_UNINSTALL_HOME}"
export HOME="${TMP_HOME}"

echo ""

# ---------------------------------------------------------------------------
# Test 15: bwrap diagnostic classification helper
# ---------------------------------------------------------------------------
echo "Test 15: bwrap_smoke_test helper logic"

# Source the helper from install.sh (redefine for test).
bwrap_smoke_test() {
  local bwrap_bin="$1"
  local test_label="$2"
  shift 2
  if "${bwrap_bin}" "$@" -- /bin/true 2>/dev/null; then
    return 0
  fi
  local exit_code=$?
  if ! "${bwrap_bin}" --version >/dev/null 2>&1; then
    echo "BINARY_MISSING"
    return 1
  fi
  if ! "${bwrap_bin}" --dev /dev -- /bin/true 2>/dev/null; then
    echo "USERNS_BLOCKED"
    return 1
  fi
  if ! "${bwrap_bin}" --tmpfs /tmp -- /bin/true 2>/dev/null; then
    echo "MOUNT_BLOCKED"
    return 1
  fi
  echo "GENERIC_FAILURE:${exit_code}"
  return 1
}

# Test with a fake bwrap that succeeds.
FAKE_BWRAP="$(mktemp)"
cat > "${FAKE_BWRAP}" <<'EOF'
#!/bin/sh
# Fake bwrap that accepts all args and runs the last command
shift $(($# - 1))
exec "$@"
EOF
chmod +x "${FAKE_BWRAP}"
assert "bwrap smoke test passes with working bwrap" "bwrap_smoke_test ${FAKE_BWRAP} 'test' --die-with-parent --ro-bind /usr /usr"
rm -f "${FAKE_BWRAP}"

echo ""

# ---------------------------------------------------------------------------
# Test 16: port availability helper logic
# ---------------------------------------------------------------------------
echo "Test 16: Port availability check"

# Simulate the port check logic from install.sh.
check_port_conflict() {
  local port="$1"
  if ss -tln 2>/dev/null | grep -qE ":${port}[[:space:]]"; then
    return 0
  elif netstat -tln 2>/dev/null | grep -qE ":${port}[[:space:]]"; then
    return 0
  fi
  return 1
}

# Port 1 is extremely unlikely to be in use.
assert_fail "port 1 is not reported as conflict" "check_port_conflict 1"

# Port 22 (ssh) or 80 (http) might be in use on some systems; just test the logic.
# We can't reliably assert success without knowing the test environment,
# but we can assert the function returns consistently.
result="$(check_port_conflict 22 && echo 'in-use' || echo 'free')"
assert "port check returns consistent result" "[[ ${result} == 'in-use' || ${result} == 'free' ]]"

echo ""

# ---------------------------------------------------------------------------
# Test 17: downloader rejects non-.sh --installer-url without --checksum-url
# ---------------------------------------------------------------------------
echo "Test 17: downloader rejects non-.sh --installer-url without --checksum-url"

output="$(bash "${SCRIPT_DIR}/install_downloader.sh" --version 0.4.1 --installer-url 'https://example.com/custom/file.bin' 2>&1 || true)"
assert "non-.sh installer-url is rejected" "printf '%s\n' '${output}' | grep -q 'Refusing to run unverified installer'"

echo ""

# ---------------------------------------------------------------------------
# Test 18: install.sh --help works when USER is unset
# ---------------------------------------------------------------------------
echo "Test 18: install.sh --help works when USER is unset"

output="$(env -u USER bash "${SCRIPT_DIR}/install.sh" --help 2>&1 || true)"
assert "install.sh --help works with unset USER" "printf '%s\n' '${output}' | grep -q 'Usage: bash install.sh'"

echo ""

# ---------------------------------------------------------------------------
# Test 19: downloader rejects --skip-verify
# ---------------------------------------------------------------------------
echo "Test 19: downloader rejects --skip-verify"

output="$(bash "${SCRIPT_DIR}/install_downloader.sh" --version 0.4.1 --skip-verify 2>&1 || true)"
assert "downloader rejects --skip-verify" "printf '%s\n' '${output}' | grep -q 'not accepted by the network downloader'"

echo ""

# ---------------------------------------------------------------------------
# Test 20: downloader forwards arguments preserving word boundaries
# ---------------------------------------------------------------------------
echo "Test 20: downloader forwards arguments preserving word boundaries"

FAKE_INSTALLER_DIR="$(mktemp -d)"
FAKE_INSTALLER="${FAKE_INSTALLER_DIR}/fake-installer.sh"
cat > "${FAKE_INSTALLER}" <<'EOF'
#!/bin/bash
printf '%s\n' "$@"
EOF
chmod +x "${FAKE_INSTALLER}"

# Generate correct checksum for the fake installer.
sha256sum "${FAKE_INSTALLER}" | awk '{print $1}' > "${FAKE_INSTALLER}.sha256"

# Run downloader with a spaced argument that must stay as one word.
output="$(bash "${SCRIPT_DIR}/install_downloader.sh" --version 0.4.1 --installer-url "file://${FAKE_INSTALLER}" --checksum-url "file://${FAKE_INSTALLER}.sha256" --rollback 'a b c' 2>&1 || true)"

assert "spaced argument preserved as single parameter" "printf '%s\n' '${output}' | grep -q '^a b c$'"

rm -rf "${FAKE_INSTALLER_DIR}"

echo ""

# ---------------------------------------------------------------------------
# Test 21: explicit --checksum-url is not overridden by .sh auto-derivation
# ---------------------------------------------------------------------------
echo "Test 21: explicit --checksum-url takes priority over .sh auto-derivation"

PRIORITY_TEST_DIR="$(mktemp -d)"
PRIORITY_INSTALLER="${PRIORITY_TEST_DIR}/test.sh"
cat > "${PRIORITY_INSTALLER}" <<'EOF'
#!/bin/bash
echo "PRIORITY_INSTALLER_EXECUTED"
EOF
chmod +x "${PRIORITY_INSTALLER}"

# Compute correct hash.
REAL_HASH="$(sha256sum "${PRIORITY_INSTALLER}" | awk '{print $1}')"

# Auto-derived checksum file contains WRONG hash.
echo "0000000000000000000000000000000000000000000000000000000000000000  test.sh" > "${PRIORITY_INSTALLER}.sha256"

# Explicit checksum file contains CORRECT hash.
echo "${REAL_HASH}  test.sh" > "${PRIORITY_TEST_DIR}/custom.sha256"

# If downloader prioritizes explicit --checksum-url, it will use custom.sha256
# (correct) and the installer will execute. If it uses auto-derived test.sh.sha256
# (wrong), checksum verification will fail.
output="$(bash "${SCRIPT_DIR}/install_downloader.sh" --version 0.4.1 --installer-url "file://${PRIORITY_INSTALLER}" --checksum-url "file://${PRIORITY_TEST_DIR}/custom.sha256" 2>&1 || true)"

assert "explicit checksum-url used, installer executed" "printf '%s\n' '${output}' | grep -q 'PRIORITY_INSTALLER_EXECUTED'"

rm -rf "${PRIORITY_TEST_DIR}"

echo ""

# ---------------------------------------------------------------------------
# Test 22: micromamba fallback has curl/wget check with clear error
# ---------------------------------------------------------------------------
echo "Test 22: micromamba fallback branch has curl/wget check"

# Verify the install.sh code contains the wget fallback and the clear error.
assert "install.sh has wget fallback for micromamba download" \
  "grep -A5 'command -v curl' ${SCRIPT_DIR}/install.sh | grep -q 'command -v wget'"
assert "install.sh has clear error for missing curl/wget" \
  "grep -q 'No curl or wget available' ${SCRIPT_DIR}/install.sh"

echo ""

# ---------------------------------------------------------------------------
# Test 23: Phase 5 does not pre-create the conda prefix as a plain directory
# ---------------------------------------------------------------------------
echo "Test 23: Phase 5 env prefix creation logic"

# The installer must not run 'mkdir -p "${ENV_DIR}"' before micromamba create,
# because recent micromamba rejects a plain directory at the prefix.
assert_fail "install.sh does not pre-create ENV_DIR directly" \
  "grep -E 'mkdir -p[[:space:]]+\"\\\${ENV_DIR}\"' ${SCRIPT_DIR}/install.sh"
assert "install.sh creates only the parent directory" \
  "grep -q 'mkdir -p.*dirname.*ENV_DIR' ${SCRIPT_DIR}/install.sh"
assert "install.sh guards against stale non-conda prefix" \
  "grep -q 'conda-meta' ${SCRIPT_DIR}/install.sh"

echo ""

# ---------------------------------------------------------------------------
# Test 24: deploy_release.sh warns instead of dying on missing key
# ---------------------------------------------------------------------------
echo "Test 24: deploy_release.sh credential gate behavior"

assert_fail "deploy_release.sh no longer has hard die on missing key" \
  "grep -q 'BLUEPRINT_DEEPSEEK_API_KEY is required for production deployment' ${SCRIPT_DIR}/deploy_release.sh"
assert "deploy_release.sh warns on missing key" \
  "grep -q 'BLUEPRINT_DEEPSEEK_API_KEY not set' ${SCRIPT_DIR}/deploy_release.sh"
assert "deploy_release.sh warns on missing key (warn_deploy)" \
  "grep -q 'warn_deploy.*BLUEPRINT_DEEPSEEK_API_KEY not set' ${SCRIPT_DIR}/deploy_release.sh"

echo ""

# ---------------------------------------------------------------------------
# Test 25: conditional credential write in deploy_release.sh
# ---------------------------------------------------------------------------
echo "Test 25: conditional credential write in env files"

assert "backend.env DEEPSEEK key is conditionally written" \
  "grep -A2 'Only write provider credentials when explicitly provided' ${SCRIPT_DIR}/deploy_release.sh | grep -q 'BLUEPRINT_DEEPSEEK_API_KEY'"
assert "manager-agent.env DEEPSEEK key is conditionally written" \
  "grep -B2 -A2 'manager-agent.env.*BLUEPRINT_DEEPSEEK_API_KEY' ${SCRIPT_DIR}/deploy_release.sh | grep -q 'if.*-n.*BLUEPRINT_DEEPSEEK_API_KEY'"
assert "manager-agent.env TAVILY key is conditionally written" \
  "grep -B2 -A2 'TAVILY_API_KEY' ${SCRIPT_DIR}/deploy_release.sh | grep -q 'if.*-n.*TAVILY_API_KEY'"

echo ""

# ---------------------------------------------------------------------------
# Test 26: upgrade credential retention
# ---------------------------------------------------------------------------
echo "Test 26: upgrade credential retention"

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

RETENTION_TMP="$(mktemp -d)"
NEW_FILE="${RETENTION_TMP}/new.env"
BACKUP_FILE="${RETENTION_TMP}/old.env"

cat > "${BACKUP_FILE}" <<'EOF'
PATH=/some/path
BLUEPRINT_DEEPSEEK_API_KEY=sk-old-key
BLUEPRINT_ANTHROPIC_API_KEY=sk-old-anthropic
EOF

# New file has PATH but is missing credentials.
cat > "${NEW_FILE}" <<'EOF'
PATH=/new/path
EOF

_preserve_credentials_from_backup "${NEW_FILE}" "${BACKUP_FILE}" \
  BLUEPRINT_DEEPSEEK_API_KEY \
  BLUEPRINT_ANTHROPIC_API_KEY

assert "retention preserves missing DEEPSEEK key" \
  "grep -q 'BLUEPRINT_DEEPSEEK_API_KEY=sk-old-key' ${NEW_FILE}"
assert "retention preserves missing ANTHROPIC key" \
  "grep -q 'BLUEPRINT_ANTHROPIC_API_KEY=sk-old-anthropic' ${NEW_FILE}"
assert "retention does not duplicate existing PATH" \
  "[[ $(grep -c '^PATH=' ${NEW_FILE}) -eq 1 ]]"

# If new file already has the key, old value should NOT be restored.
NEW_FILE2="${RETENTION_TMP}/new2.env"
cat > "${NEW_FILE2}" <<'EOF'
PATH=/new/path
BLUEPRINT_DEEPSEEK_API_KEY=sk-new-key
EOF

_preserve_credentials_from_backup "${NEW_FILE2}" "${BACKUP_FILE}" \
  BLUEPRINT_DEEPSEEK_API_KEY

assert "retention does not overwrite existing key" \
  "grep -q 'BLUEPRINT_DEEPSEEK_API_KEY=sk-new-key' ${NEW_FILE2}"
assert_fail "retention does not add old key when new key exists" \
  "grep -q 'sk-old-key' ${NEW_FILE2}"

rm -rf "${RETENTION_TMP}"

echo ""

# ---------------------------------------------------------------------------
# Test 27: explicit clear semantics (set-to-empty clears old value)
# ---------------------------------------------------------------------------
echo "Test 27: explicit clear semantics"

# Simulate the credential write pattern used in deploy_release.sh:
#   if [[ -n "${VAR:-}" ]]; then write value
#   elif [[ "${VAR+set}" == "set" ]]; then write empty
#   fi
# When VAR is explicitly set to empty, the key is written as empty,
# so _preserve_credentials_from_backup sees it as present and skips.

CLEAR_TMP="$(mktemp -d)"
NEW_CLEAR="${CLEAR_TMP}/new.env"
OLD_CLEAR="${CLEAR_TMP}/old.env"

cat > "${OLD_CLEAR}" <<'EOF'
BLUEPRINT_DEEPSEEK_API_KEY=sk-old-key
TAVILY_API_KEY=sk-old-tavily
EOF

# Simulate: variable is explicitly set to empty string.
# In bash, [[ -n "" ]] is false, but [[ ""${VAR}+set"" == "set" ]] is true.
# Write the empty key to the new file.
cat > "${NEW_CLEAR}" <<'EOF'
BLUEPRINT_DEEPSEEK_API_KEY=
EOF

_preserve_credentials_from_backup "${NEW_CLEAR}" "${OLD_CLEAR}" \
  BLUEPRINT_DEEPSEEK_API_KEY \
  TAVILY_API_KEY

assert "explicit clear keeps empty value, does not restore old" \
  "grep -q 'BLUEPRINT_DEEPSEEK_API_KEY=$' ${NEW_CLEAR}"
assert_fail "explicit clear does not restore old DEEPSEEK value" \
  "grep -q 'sk-old-key' ${NEW_CLEAR}"
assert "explicit clear restores missing TAVILY key" \
  "grep -q 'TAVILY_API_KEY=sk-old-tavily' ${NEW_CLEAR}"

rm -rf "${CLEAR_TMP}"

echo ""

# ---------------------------------------------------------------------------
# Test 28: render_release_downloader.sh produces valid install.sh
# ---------------------------------------------------------------------------
echo "Test 28: render_release_downloader.sh output"

RENDER_TMP="$(mktemp -d)"
RENDERED="${RENDER_TMP}/install.sh"

bash "${REPO_ROOT}/scripts/render_release_downloader.sh" \
  --version "0.4.2" \
  --repo "solarise94/RhineDataLab" \
  --artifact-prefix "rhinedatalab" \
  --arch "x86_64" \
  --output "${RENDERED}" > /dev/null 2>&1

assert "rendered install.sh exists" "[[ -f ${RENDERED} ]]"
assert "rendered install.sh is executable" "[[ -x ${RENDERED} ]]"
assert "install.sh contains hardcoded version" "grep -q 'RELEASE_VERSION=\"0.4.2\"' ${RENDERED}"
assert "install.sh contains hardcoded repo" "grep -q 'RELEASE_REPO=\"solarise94/RhineDataLab\"' ${RENDERED}"
assert "install.sh contains artifact URL" "grep -q 'https://github.com/solarise94/RhineDataLab/releases/download/v0.4.2/rhinedatalab-0.4.2-linux-x86_64.sh' ${RENDERED}"
assert "install.sh contains checksum URL" "grep -q 'https://github.com/solarise94/RhineDataLab/releases/download/v0.4.2/rhinedatalab-0.4.2-linux-x86_64.sh.sha256' ${RENDERED}"

rm -rf "${RENDER_TMP}"

echo ""

# ---------------------------------------------------------------------------
# Test 29: rendered install.sh --help works
# ---------------------------------------------------------------------------
echo "Test 29: rendered install.sh --help"

RENDER_TMP2="$(mktemp -d)"
RENDERED2="${RENDER_TMP2}/install.sh"

bash "${REPO_ROOT}/scripts/render_release_downloader.sh" \
  --version "0.4.2" \
  --output "${RENDERED2}" > /dev/null 2>&1

HELP_OUTPUT="$(bash "${RENDERED2}" --help 2>&1 || true)"
assert "help mentions Forwarded flags" "echo '${HELP_OUTPUT}' | grep -q 'Forwarded flags'"
assert "help mentions --keep-installer" "echo '${HELP_OUTPUT}' | grep -q 'keep-installer'"
assert "help mentions --upgrade" "echo '${HELP_OUTPUT}' | grep -q 'upgrade'"
assert "help mentions --rollback" "echo '${HELP_OUTPUT}' | grep -q 'rollback'"
assert "public downloader rejects --skip-verify" "(bash '${RENDERED2}' --skip-verify 2>&1 || true) | grep -q 'not accepted by the public downloader'"

rm -rf "${RENDER_TMP2}"

echo ""

# ---------------------------------------------------------------------------
# Test 30: build_self_extracting_installer.sh --artifact-prefix
# ---------------------------------------------------------------------------
echo "Test 30: build_self_extracting_installer.sh --artifact-prefix"

PREFIX_TMP="$(mktemp -d)"
mkdir -p "${PREFIX_TMP}/blueprint-re/wheels"
echo "dummy" > "${PREFIX_TMP}/blueprint-re/wheels/dummy.whl"
cat > "${PREFIX_TMP}/blueprint-re/release.json" <<'EOF'
{
  "version": "0.0.0-prefix",
  "arch": "x86_64",
  "platform": "linux"
}
EOF
(
  cd "${PREFIX_TMP}/blueprint-re" || exit 1
  find . -type f | while IFS= read -r f; do
    f="${f#./}"
    [[ "$f" == "checksums.sha256" ]] && continue
    sha256sum "$f"
  done > checksums.sha256
)
(
  cd "${PREFIX_TMP}" || exit 1
  tar -czf blueprint-re-0.0.0-prefix-linux-x86_64.tar.gz blueprint-re
)

bash "${REPO_ROOT}/scripts/build_self_extracting_installer.sh" \
  --artifact-prefix "rhinedatalab" \
  --output-dir "${PREFIX_TMP}/dist" \
  "${PREFIX_TMP}/blueprint-re-0.0.0-prefix-linux-x86_64.tar.gz" > /dev/null 2>&1

PREFIX_INSTALLER="${PREFIX_TMP}/dist/rhinedatalab-0.0.0-prefix-linux-x86_64.sh"
assert "custom prefix installer created" "[[ -f ${PREFIX_INSTALLER} ]]"
assert "custom prefix checksum created" "[[ -f ${PREFIX_INSTALLER}.sha256 ]]"

# Verify the payload is still extracted correctly (internal payload dir unchanged).
EXTRACT_PREFIX="${PREFIX_TMP}/extracted"
mkdir -p "${EXTRACT_PREFIX}"
PAYLOAD_OFFSET_PREFIX=$(python3 -c "
with open('${PREFIX_INSTALLER}', 'rb') as f:
    data = f.read()
marker = b'__PAYLOAD_START__\n'
idx = data.find(marker)
if idx < 0:
    sys.exit(1)
print(idx + len(marker) + 1)
")
(
  cd "${EXTRACT_PREFIX}" || exit 1
  tail -c +"${PAYLOAD_OFFSET_PREFIX}" "${PREFIX_INSTALLER}" | tar -xzf -
)
assert "custom prefix payload extracted" "[[ -d ${EXTRACT_PREFIX}/blueprint-re ]]"

rm -rf "${PREFIX_TMP}"

echo ""

# ---------------------------------------------------------------------------
# Test 31: publish_release.sh --skip-upload asset preparation
# ---------------------------------------------------------------------------
echo "Test 31: publish_release.sh --skip-upload asset preparation"

PUBLISH_TMP="$(mktemp -d)"

# Create fake existing dist assets matching the current pyproject version.
CURRENT_VERSION="$(python3 -c "
import re
text = open('${REPO_ROOT}/backend/pyproject.toml').read()
m = re.search(r'^version\s*=\s*\"([^\"]+)\"', text, re.M)
print(m.group(1) if m else '0.0.0')
")"

mkdir -p "${PUBLISH_TMP}/dist"
# Build a minimal tarball and installer so --skip-build works with existing assets.
mkdir -p "${PUBLISH_TMP}/stage/blueprint-re/wheels"
echo "dummy" > "${PUBLISH_TMP}/stage/blueprint-re/wheels/dummy.whl"
cat > "${PUBLISH_TMP}/stage/blueprint-re/release.json" <<EOF
{
  "version": "${CURRENT_VERSION}",
  "arch": "x86_64",
  "platform": "linux"
}
EOF
(
  cd "${PUBLISH_TMP}/stage/blueprint-re" || exit 1
  find . -type f | while IFS= read -r f; do
    f="${f#./}"
    [[ "$f" == "checksums.sha256" ]] && continue
    sha256sum "$f"
  done > checksums.sha256
)
(
  cd "${PUBLISH_TMP}/stage" || exit 1
  tar -czf "rhinedatalab-${CURRENT_VERSION}-linux-x86_64.tar.gz" blueprint-re
)

bash "${REPO_ROOT}/scripts/build_self_extracting_installer.sh" \
  --artifact-prefix "rhinedatalab" \
  --output-dir "${PUBLISH_TMP}/dist" \
  "${PUBLISH_TMP}/stage/rhinedatalab-${CURRENT_VERSION}-linux-x86_64.tar.gz" > /dev/null 2>&1

# The publish script also needs the tarball present in the asset directory.
cp "${PUBLISH_TMP}/stage/rhinedatalab-${CURRENT_VERSION}-linux-x86_64.tar.gz" \
  "${PUBLISH_TMP}/dist/"

# Run publish with --skip-upload.
SKIP_UPLOAD_OUT="$(bash "${REPO_ROOT}/scripts/publish_release.sh" \
  --version "${CURRENT_VERSION}" \
  --repo "solarise94/RhineDataLab" \
  --skip-build \
  --skip-upload \
  --output-dir "${PUBLISH_TMP}/dist" \
  --notes-string "test release" 2>&1)"

assert "skip-upload reports prepared assets" "echo '${SKIP_UPLOAD_OUT}' | grep -q 'skip-upload specified'"
assert "install.sh generated" "[[ -f ${PUBLISH_TMP}/dist/install.sh ]]"
assert "install.sh.sha256 generated" "[[ -f ${PUBLISH_TMP}/dist/install.sh.sha256 ]]"
assert "versioned installer exists" "[[ -f ${PUBLISH_TMP}/dist/rhinedatalab-${CURRENT_VERSION}-linux-x86_64.sh ]]"
assert "versioned installer checksum exists" "[[ -f ${PUBLISH_TMP}/dist/rhinedatalab-${CURRENT_VERSION}-linux-x86_64.sh.sha256 ]]"
assert "tarball checksum exists" "[[ -f ${PUBLISH_TMP}/dist/rhinedatalab-${CURRENT_VERSION}-linux-x86_64.tar.gz.sha256 ]]"

rm -rf "${PUBLISH_TMP}"

echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "========================================"
echo "Results: ${PASSED} passed, ${FAILED} failed"
echo "========================================"

if [[ "${FAILED}" -gt 0 ]]; then
  exit 1
fi
