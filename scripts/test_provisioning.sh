#!/usr/bin/env bash
# Behavior-based acceptance tests for bundled mamba + R runtime provisioning.
# Runs in an isolated temp HOME so it does not mutate the host.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_SCRIPT="${REPO_ROOT}/scripts/install_blueprint_re.sh"

# Use a temp HOME to avoid touching the real user environment.
TEST_HOME="$(mktemp -d)"
export HOME="${TEST_HOME}"
trap 'rm -rf "${TEST_HOME}"' EXIT

mkdir -p "${TEST_HOME}/.local/share/blueprint-re"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

pass() {
  echo "PASS: $1"
}

# Create a fake repo root with just the runtime artifacts the functions need.
FAKE_ROOT="${TEST_HOME}/fake-repo"
mkdir -p "${FAKE_ROOT}/runtime/bin"
mkdir -p "${FAKE_ROOT}/runtime/mirror-presets"
cp -a "${REPO_ROOT}/deploy/runtime/mirror-presets/"*.mambarc "${FAKE_ROOT}/runtime/mirror-presets/"
cp -a "${REPO_ROOT}/deploy/runtime/mirror-presets/mirror_env.sh" "${FAKE_ROOT}/runtime/mirror-presets/"
mkdir -p "${FAKE_ROOT}/deploy/runtime"
cat > "${FAKE_ROOT}/runtime/blueprint-re-r.yml" <<'EOF'
name: blueprint-re-r
channels:
  - conda-forge
dependencies:
  - r-base =4.4
EOF
cat > "${FAKE_ROOT}/runtime/blueprint-re-r-extras.yml" <<'EOF'
name: blueprint-re-r
channels:
  - conda-forge
dependencies:
  - bioconductor-clusterprofiler
EOF

# Mock micromamba binary: records invocations and creates a fake env on create.
cat > "${FAKE_ROOT}/runtime/bin/micromamba" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="${ROOT_DIR}/.micromamba-invocations.log"
printf '%s\n' "$*" >> "${LOG}"
if [[ "$1" == "--version" ]]; then
  echo "2.8.0"
  exit 0
fi
if [[ "$1" == "create" ]]; then
  # Parse -n value and env prefix.
  env_name=""
  prefix=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -n) env_name="$2"; shift 2 ;;
      -p) prefix="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  target="${prefix:-${MAMBA_ROOT_PREFIX}/envs/${env_name}}"
  mkdir -p "${target}/bin"
  echo '#!/bin/sh' > "${target}/bin/Rscript"
  chmod +x "${target}/bin/Rscript"
  mkdir -p "${target}/pkgs"
  exit 0
fi
if [[ "$1" == "install" ]]; then
  # Append-mode install: just create a marker file inside the target env.
  env_name=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -n) env_name="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  target="${MAMBA_ROOT_PREFIX}/envs/${env_name}"
  mkdir -p "${target}/bin"
  touch "${target}/.extras-installed"
  exit 0
fi
if [[ "$1" == "env" && "$2" == "remove" ]]; then
  # Best-effort cleanup for rollback tests.
  shift 2
  name=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -n) name="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  if [[ -n "${name}" ]]; then
    rm -rf "${MAMBA_ROOT_PREFIX}/envs/${name}"
  fi
  exit 0
fi
exit 0
EOF
chmod +x "${FAKE_ROOT}/runtime/bin/micromamba"

# Source the install script for its provisioning functions in the fake repo.
# We override ROOT_DIR after sourcing so functions look at the fake runtime tree.
# shellcheck disable=SC1091
source "${INSTALL_SCRIPT}"
ROOT_DIR="${FAKE_ROOT}"
BLUEPRINT_MIRROR_PRESET="tsinghua"

# ---------------------------------------------------------------------------
# Test provision_bundled_mamba
# ---------------------------------------------------------------------------
provision_bundled_mamba > "${TEST_HOME}/mamba_result.txt"
result="$(cat "${TEST_HOME}/mamba_result.txt")"
expected="${TEST_HOME}/.local/share/blueprint-re/mamba"
[[ "${result}" == "${expected}" ]] || fail "provision_bundled_mamba returned ${result}, expected ${expected}"
[[ -x "${expected}/bin/micromamba" ]] || fail "micromamba not copied to target"
[[ -f "${expected}/.mambarc" ]] || fail ".mambarc not created"
grep -q "root_prefix: ${expected}" "${expected}/.mambarc" || fail "root_prefix not written to .mambarc"
grep -q "mirrors.tuna.tsinghua.edu.cn" "${expected}/.mambarc" || fail "tsinghua mirror not in .mambarc"
[[ "${MAMBA_ROOT_PREFIX}" == "${expected}" ]] || fail "MAMBA_ROOT_PREFIX not exported (got '${MAMBA_ROOT_PREFIX}')"
[[ "${MAMBARC}" == "${expected}/.mambarc" ]] || fail "MAMBARC not exported (got '${MAMBARC}')"
pass "provision_bundled_mamba creates target, .mambarc, and exports env vars"

# Idempotency: second call should still return target and not break.
provision_bundled_mamba > "${TEST_HOME}/mamba_result2.txt"
result2="$(cat "${TEST_HOME}/mamba_result2.txt")"
[[ "${result2}" == "${expected}" ]] || fail "provision_bundled_mamba not idempotent"
pass "provision_bundled_mamba is idempotent"

# ---------------------------------------------------------------------------
# Test provision_bundled_r_runtime
# ---------------------------------------------------------------------------
provision_bundled_r_runtime "${expected}" > /dev/null
[[ -x "${expected}/envs/blueprint-re-r/bin/Rscript" ]] || fail "R env not created"
pass "provision_bundled_r_runtime creates blueprint-re-r env"

# Idempotency: existing env should be skipped and the name returned.
name="$(provision_bundled_r_runtime "${expected}")"
[[ "${name}" == "blueprint-re-r" ]] || fail "R runtime idempotency returned ${name}"
pass "provision_bundled_r_runtime is idempotent"

# Rebuild marker: changing the env spec should trigger a rebuild.
original_marker="$(cat "${expected}/envs/blueprint-re-r/.blueprint-re-r.build-info" 2>/dev/null)"
[[ -n "${original_marker}" ]] || fail "build-info marker not created"
printf '%s\n' "# trigger rebuild" >> "${FAKE_ROOT}/runtime/blueprint-re-r.yml"
provision_bundled_r_runtime "${expected}" > /dev/null
new_marker="$(cat "${expected}/envs/blueprint-re-r/.blueprint-re-r.build-info" 2>/dev/null)"
[[ "${new_marker}" != "${original_marker}" ]] || fail "R runtime was not rebuilt after spec change"
pass "provision_bundled_r_runtime rebuilds when spec changes"

# Missing marker: existing env without marker should be treated as stale and rebuilt.
rm -f "${expected}/envs/blueprint-re-r/.blueprint-re-r.build-info"
provision_bundled_r_runtime "${expected}" > /dev/null
[[ -f "${expected}/envs/blueprint-re-r/.blueprint-re-r.build-info" ]] || fail "R runtime missing marker was not rebuilt"
pass "provision_bundled_r_runtime rebuilds when marker is missing"

# ---------------------------------------------------------------------------
# Test BLUEPRINT_INSTALL_R_RUNTIME=0 skips R env creation
# ---------------------------------------------------------------------------
rm -rf "${expected}/envs/blueprint-re-r"
BLUEPRINT_INSTALL_R_RUNTIME=0
name="$(provision_bundled_r_runtime "${expected}")"
[[ -z "${name}" ]] || fail "R runtime should be skipped when BLUEPRINT_INSTALL_R_RUNTIME=0"
[[ ! -d "${expected}/envs/blueprint-re-r" ]] || fail "R env should not exist when skipped"
pass "BLUEPRINT_INSTALL_R_RUNTIME=0 skips R env creation"
BLUEPRINT_INSTALL_R_RUNTIME=1

# ---------------------------------------------------------------------------
# Test provision_bundled_r_extras
# ---------------------------------------------------------------------------
provision_bundled_r_runtime "${expected}" > /dev/null
BLUEPRINT_INSTALL_R_EXTRAS=1
provision_bundled_r_extras "${expected}"
[[ -f "${expected}/envs/blueprint-re-r/.extras-installed" ]] || fail "R extras were not installed"
[[ -f "${expected}/envs/blueprint-re-r/.blueprint-re-r-extras.build-info" ]] || fail "R extras marker not created"
pass "provision_bundled_r_extras appends packages to blueprint-re-r"

# Idempotency: unchanged extras spec should skip reinstall.
rm -f "${expected}/envs/blueprint-re-r/.extras-installed"
provision_bundled_r_extras "${expected}"
[[ ! -f "${expected}/envs/blueprint-re-r/.extras-installed" ]] || fail "R extras reinstalled despite unchanged marker"
pass "provision_bundled_r_extras is idempotent"

# Extras spec change should trigger reinstall.
printf '%s\n' "# trigger extras rebuild" >> "${FAKE_ROOT}/runtime/blueprint-re-r-extras.yml"
provision_bundled_r_extras "${expected}"
[[ -f "${expected}/envs/blueprint-re-r/.extras-installed" ]] || fail "R extras were not reinstalled after spec change"
pass "provision_bundled_r_extras reinstalls when spec changes"
BLUEPRINT_INSTALL_R_EXTRAS=0

# ---------------------------------------------------------------------------
# Test provision_bundled_r_extras without base runtime fails
# ---------------------------------------------------------------------------
rm -rf "${expected}/envs/blueprint-re-r"
BLUEPRINT_INSTALL_R_EXTRAS=1
if provision_bundled_r_extras "${expected}" 2>/dev/null; then
  fail "provision_bundled_r_extras should fail when base R runtime is missing"
fi
pass "provision_bundled_r_extras fails when base R runtime is missing"
BLUEPRINT_INSTALL_R_EXTRAS=0

# ---------------------------------------------------------------------------
# Test provision_bundled_r_runtime failure rollback
# ---------------------------------------------------------------------------
# Replace the active micromamba with a failing one to test rollback path.
cat > "${expected}/bin/micromamba" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "${expected}/bin/micromamba"
rm -rf "${expected}/envs/blueprint-re-r"
if provision_bundled_r_runtime "${expected}" 2>/dev/null; then
  fail "provision_bundled_r_runtime should have failed with broken micromamba"
fi
pass "provision_bundled_r_runtime fails gracefully"

# ---------------------------------------------------------------------------
# Test mirror preset expansion handles preset changes
# ---------------------------------------------------------------------------
# Subshell: source the installer with an explicit user mirror and a preset,
# then change the preset and verify the user mirror is preserved while the
# other script-derived mirrors follow the new preset.
(
  export HOME="${TEST_HOME}"
  # Simulate a fresh script invocation: clear mirror vars/flags inherited from
  # the parent source of install_blueprint_re.sh.
  unset BLUEPRINT_CRAN_MIRROR BLUEPRINT_BIOCONDUCTOR_MIRROR BLUEPRINT_PYPI_MIRROR
  unset _USER_CRAN_MIRROR_SET _USER_BIOCONDUCTOR_MIRROR_SET _USER_PYPI_MIRROR_SET
  unset _LAST_EXPANDED_MIRROR_PRESET
  export BLUEPRINT_MIRROR_PRESET="tsinghua"
  export BLUEPRINT_CRAN_MIRROR="https://user.example/cran"
  # shellcheck disable=SC1090
  source "${INSTALL_SCRIPT}"
  _tsinghua_cran="${BLUEPRINT_CRAN_MIRROR}"
  _tsinghua_bioconductor="${BLUEPRINT_BIOCONDUCTOR_MIRROR}"
  _tsinghua_pypi="${BLUEPRINT_PYPI_MIRROR}"
  [[ "${_tsinghua_cran}" == "https://user.example/cran" ]] || fail "user cran mirror not preserved initially"
  [[ -n "${_tsinghua_bioconductor}" ]] || fail "tsinghua bioconductor mirror not derived"
  [[ -n "${_tsinghua_pypi}" ]] || fail "tsinghua pypi mirror not derived"

  BLUEPRINT_MIRROR_PRESET="default"
  _expand_mirror_preset "default"
  [[ "${BLUEPRINT_CRAN_MIRROR}" == "https://user.example/cran" ]] || fail "user cran mirror overwritten on preset change"
  # Script-derived mirrors should follow the new preset.
  [[ "${BLUEPRINT_BIOCONDUCTOR_MIRROR}" != "${_tsinghua_bioconductor}" ]] || fail "bioconductor mirror not recomputed for default preset (got ${BLUEPRINT_BIOCONDUCTOR_MIRROR})"
  [[ "${BLUEPRINT_PYPI_MIRROR}" != "${_tsinghua_pypi}" ]] || fail "pypi mirror not recomputed for default preset (got ${BLUEPRINT_PYPI_MIRROR})"
)
pass "mirror preset change clears script-derived mirrors but preserves user values"

echo ""
echo "All provisioning acceptance tests passed."
