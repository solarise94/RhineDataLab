#!/usr/bin/env bash
# Run the backend test suite.
#
# Behavior:
#   - If pytest-xdist is importable -> parallel fast path (pytest -n auto) for
#     the bulk, plus the serial modules on a single non-xdist process. ~12x
#     faster than the serial fallback.
#   - Otherwise -> print an info note pointing at how to enable the fast path,
#     then fall back to the zero-dependency `unittest discover` run.
#
# This is the single entry point for backend tests. There is no companion
# command to also run — do not invoke pytest or unittest directly alongside it.
#
# Usage:
#   scripts/run_backend_tests.sh                  # full suite (auto path)
#   scripts/run_backend_tests.sh --serial-only    # only the serial modules
#   scripts/run_backend_tests.sh --parallel-only  # only the parallel modules
#   scripts/run_backend_tests.sh -k card_library  # extra args forward to pytest
#
# --serial-only / --parallel-only imply the fast (pytest) path; they are
# ignored (with a note) when pytest-xdist is unavailable.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PY="${BLUEPRINT_TEST_PYTHON:-${REPO_ROOT}/.venv/backend/bin/python}"
TESTS_DIR="${REPO_ROOT}/backend/tests"

if [[ ! -x "${PY}" ]]; then
  echo "Backend venv python not found at ${PY}" >&2
  echo "Create it with: python3.13 -m venv .venv/backend && .venv/backend/bin/pip install -e 'backend[test]'" >&2
  exit 1
fi

# Modules that must run serially (kept in sync with backend/tests/conftest.py
# SERIAL_TEST_MODULES). These run without xdist so they never share a process.
SERIAL_MODULES=(test_install_deploy)

ignore_args=()
serial_targets=()
for mod in "${SERIAL_MODULES[@]}"; do
  ignore_args+=(--ignore "${TESTS_DIR}/${mod}.py")
  serial_targets+=("${TESTS_DIR}/${mod}.py")
done

mode="full"
pytest_args=()
for arg in "$@"; do
  case "$arg" in
    --serial-only)   mode="serial" ;;
    --parallel-only) mode="parallel" ;;
    *) pytest_args+=("$arg") ;;
  esac
done

# Detect pytest-xdist by attempting an import in the target interpreter.
HAS_XDIST=0
if "${PY}" -c "import xdist" >/dev/null 2>&1; then
  HAS_XDIST=1
fi

if [[ "${HAS_XDIST}" -eq 0 ]]; then
  if [[ "${mode}" != "full" ]]; then
    echo "[info] --${mode}-only needs pytest-xdist; running the full serial fallback instead." >&2
  fi
  echo "[info] pytest-xdist not found in ${PY}." >&2
  echo "[info] Install it to enable the parallel fast path (~12x faster):" >&2
  echo "[info]   ${PY} -m pip install -e '${REPO_ROOT}/backend[test]'" >&2
  echo "[info] Falling back to: python -m unittest discover (serial)" >&2
  PYTHONPATH="${REPO_ROOT}/backend" "${PY}" -m unittest discover -s "${TESTS_DIR}"
  exit $?
fi

run_parallel() {
  "${PY}" -m pytest "${TESTS_DIR}" "${ignore_args[@]}" \
    -n auto -p no:cacheprovider "${pytest_args[@]}"
}

run_serial() {
  # -p no:xdist guarantees these run on a single process even if a project
  # config adds xdist by default.
  "${PY}" -m pytest "${serial_targets[@]}" \
    -p no:cacheprovider -p no:xdist "${pytest_args[@]}"
}

rc=0
if [[ "$mode" == "parallel" ]]; then
  run_parallel || rc=$?
elif [[ "$mode" == "serial" ]]; then
  run_serial || rc=$?
else
  run_parallel || rc=$?
  run_serial || rc=$?
fi

exit "$rc"
