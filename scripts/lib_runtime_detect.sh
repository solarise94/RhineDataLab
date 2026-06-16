#!/usr/bin/env bash
# Shared runtime detection helpers used by install/deploy scripts.
# This file is sourced by deploy_release.sh and install_blueprint_re.sh.
# Do not execute it directly.

# Return the full ordered list of conda base candidates. Matches the runtime
# resolver candidate list in backend/app/core/config.py so deploy-time detection
# and run-time resolution agree.
conda_base_candidates() {
  local candidates=()
  [[ -n "${BLUEPRINT_EXECUTOR_CONDA_BASE:-}" ]] && candidates+=("${BLUEPRINT_EXECUTOR_CONDA_BASE}")
  [[ -n "${BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX:-}" ]] && candidates+=("${BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX}")
  candidates+=(
    "${HOME}/.local/share/blueprint-re/mamba"
    "${HOME}/miniforge3"
    "${HOME}/miniconda3"
    "${HOME}/anaconda3"
    "/opt/conda"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    [[ -n "${candidate}" ]] || continue
    printf '%s\n' "${candidate}"
  done
}

# Return the first candidate that contains a conda-family solver binary.
# This is the single base used for BLUEPRINT_EXECUTOR_CONDA_BASE.
detect_conda_base() {
  local candidate
  while IFS= read -r candidate; do
    [[ -n "${candidate}" ]] || continue
    if [[ -x "${candidate}/bin/conda" || -x "${candidate}/bin/micromamba" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done < <(conda_base_candidates)
  return 1
}

# Search all candidate bases for the best default Python runtime.
# The optional conda_base argument is accepted for backward compatibility but
# ignored; detection now uses the full candidate list.
detect_default_python_runtime() {
  local env_name="${BLUEPRINT_DEFAULT_PYTHON_RUNTIME:-}"
  if [[ -n "${env_name}" ]]; then
    printf '%s\n' "${env_name}"
    return 0
  fi
  local candidates=(omicverse analysis base)
  local conda_base name
  while IFS= read -r conda_base; do
    [[ -n "${conda_base}" ]] || continue
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
  done < <(conda_base_candidates)
  return 1
}

# Search all candidate bases for the best default R runtime.
# The optional conda_base argument is accepted for backward compatibility but
# ignored; detection now uses the full candidate list.
detect_default_r_runtime() {
  local env_name="${BLUEPRINT_DEFAULT_R_RUNTIME:-}"
  if [[ -n "${env_name}" ]]; then
    printf '%s\n' "${env_name}"
    return 0
  fi
  local candidates=(blueprint-re-r bioconductor r-bio base)
  local conda_base name
  while IFS= read -r conda_base; do
    [[ -n "${conda_base}" ]] || continue
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
  done < <(conda_base_candidates)
  if command -v Rscript >/dev/null 2>&1; then
    printf '%s\n' "__system__"
    return 0
  fi
  return 1
}
