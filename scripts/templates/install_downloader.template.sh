#!/usr/bin/env bash
set -euo pipefail

# RhineDataLab public installer downloader.
#
# This is a release-specific text-only bootstrap script. It downloads the
# paired versioned self-extracting installer from the same GitHub Release,
# verifies its SHA-256 checksum, and executes it.
#
# Usage:
#   curl -fsSL https://github.com/__RELEASE_REPO__/releases/download/v__RELEASE_VERSION__/install.sh | bash
#
# Forwarded flags:
#   --offline             Fail if the embedded package cache is missing
#   --upgrade             Require an existing installation and run upgrade flow
#   --rollback VERSION    Switch to a previous local release version
#
# Downloader options:
#   --keep-installer      Preserve the downloaded installer after execution
#   --help|-h             Show this message
#
# This script is safe for "curl | bash". It only downloads and verifies an
# installer artifact; all actual installation work is done by the downloaded
# Layer 2 installer.

RELEASE_VERSION="__RELEASE_VERSION__"
RELEASE_REPO="__RELEASE_REPO__"
RELEASE_ARCH="__RELEASE_ARCH__"
RELEASE_ARTIFACT_NAME="__RELEASE_ARTIFACT_NAME__"
RELEASE_INSTALLER_URL="__RELEASE_INSTALLER_URL__"
RELEASE_CHECKSUM_URL="__RELEASE_CHECKSUM_URL__"

KEEP_INSTALLER=0
FORWARD_ARGS=()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

die() {
  echo "[downloader] ERROR: $1" >&2
  exit 1
}

info() {
  echo "[downloader] $1"
}

warn() {
  echo "[downloader] WARNING: $1" >&2
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    die "Required command not found: $1"
  fi
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-installer)
      KEEP_INSTALLER=1
      shift
      ;;
    --offline)
      FORWARD_ARGS+=("$1")
      shift
      ;;
    --upgrade)
      FORWARD_ARGS+=("$1")
      shift
      ;;
    --rollback)
      if [[ -n "${2:-}" ]]; then
        FORWARD_ARGS+=("$1" "$2")
        shift 2
      else
        die "--rollback requires a version argument."
      fi
      ;;
    --skip-verify)
      die "--skip-verify is not accepted by the public downloader."
      ;;
    --help|-h)
      sed -n '/^# RhineDataLab/,/^# Layer 2 installer./s/^# //p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      # Forward anything else to the installer so the contract stays open.
      FORWARD_ARGS+=("$1")
      shift
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Phase 0: Host checks
# ---------------------------------------------------------------------------

info "RhineDataLab installer downloader v${RELEASE_VERSION}"

if [[ "$(uname -s)" != "Linux" ]]; then
  die "This installer supports Linux only. Detected: $(uname -s)"
fi

ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64)
    if [[ "${RELEASE_ARCH}" != "x86_64" ]]; then
      die "Architecture mismatch: host is x86_64 but this downloader is for ${RELEASE_ARCH}."
    fi
    ;;
  aarch64|arm64)
    if [[ "${RELEASE_ARCH}" != "aarch64" && "${RELEASE_ARCH}" != "arm64" ]]; then
      die "Architecture mismatch: host is ${ARCH} but this downloader is for ${RELEASE_ARCH}."
    fi
    ;;
  *)
    die "Unsupported architecture: ${ARCH}. This release supports ${RELEASE_ARCH}."
    ;;
esac

require_cmd mktemp
require_cmd chmod
require_cmd sha256sum

DOWNLOADER=""
if command -v curl >/dev/null 2>&1; then
  DOWNLOADER="curl"
elif command -v wget >/dev/null 2>&1; then
  DOWNLOADER="wget"
else
  die "Neither curl nor wget is available. Please download the installer manually from: https://github.com/${RELEASE_REPO}/releases/tag/v${RELEASE_VERSION}"
fi

# ---------------------------------------------------------------------------
# Phase 1: Download installer
# ---------------------------------------------------------------------------

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

ARTIFACT_PATH="${TMP_DIR}/${RELEASE_ARTIFACT_NAME}"

info "Downloading ${RELEASE_ARTIFACT_NAME}..."

if [[ "${DOWNLOADER}" == "curl" ]]; then
  if ! curl -fsSL -o "${ARTIFACT_PATH}" "${RELEASE_INSTALLER_URL}"; then
    die "Failed to download installer from ${RELEASE_INSTALLER_URL}"
  fi
else
  if ! wget -q -O "${ARTIFACT_PATH}" "${RELEASE_INSTALLER_URL}"; then
    die "Failed to download installer from ${RELEASE_INSTALLER_URL}"
  fi
fi

info "Download complete."

# ---------------------------------------------------------------------------
# Phase 2: Checksum verification (fail-closed)
# ---------------------------------------------------------------------------

info "Downloading checksum..."
CHECKSUM_PATH="${TMP_DIR}/${RELEASE_ARTIFACT_NAME}.sha256"

if [[ "${DOWNLOADER}" == "curl" ]]; then
  if ! curl -fsSL -o "${CHECKSUM_PATH}" "${RELEASE_CHECKSUM_URL}" 2>/dev/null; then
    die "Could not download checksum file from ${RELEASE_CHECKSUM_URL}. Refusing to run unverified installer."
  fi
else
  if ! wget -q -O "${CHECKSUM_PATH}" "${RELEASE_CHECKSUM_URL}" 2>/dev/null; then
    die "Could not download checksum file from ${RELEASE_CHECKSUM_URL}. Refusing to run unverified installer."
  fi
fi

info "Verifying checksum..."
EXPECTED_CHECKSUM="$(awk '{print $1}' "${CHECKSUM_PATH}")"
ACTUAL_CHECKSUM="$(sha256sum "${ARTIFACT_PATH}" | awk '{print $1}')"

if [[ "${EXPECTED_CHECKSUM}" != "${ACTUAL_CHECKSUM}" ]]; then
  die "Checksum mismatch! Expected ${EXPECTED_CHECKSUM}, got ${ACTUAL_CHECKSUM}. The download may be corrupted or tampered with."
fi

info "Checksum verified."

# ---------------------------------------------------------------------------
# Phase 3: Execute installer
# ---------------------------------------------------------------------------

chmod +x "${ARTIFACT_PATH}"

info "Launching installer..."
info ""

bash "${ARTIFACT_PATH}" "${FORWARD_ARGS[@]}"
INSTALLER_EXIT=$?

# ---------------------------------------------------------------------------
# Phase 4: Cleanup / keep
# ---------------------------------------------------------------------------

if [[ "${KEEP_INSTALLER}" -eq 1 ]]; then
  KEEP_PATH="${HOME}/.local/share/blueprint-re/downloads"
  mkdir -p "${KEEP_PATH}"
  mv "${ARTIFACT_PATH}" "${KEEP_PATH}/${RELEASE_ARTIFACT_NAME}"
  info "Installer preserved at: ${KEEP_PATH}/${RELEASE_ARTIFACT_NAME}"
fi

exit ${INSTALLER_EXIT}
