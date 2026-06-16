#!/usr/bin/env bash
set -euo pipefail

# Blueprint RE Downloader Bootstrap Script
#
# Public text-only network bootstrap entrypoint.
# Safe to run via: curl -fsSL https://example.com/blueprint-re/install.sh | bash
#
# Responsibilities:
#   - detect supported OS and architecture
#   - select release version or channel
#   - check for curl or wget
#   - download the self-extracting installer to a temporary file
#   - download and verify the matching checksum
#   - make the downloaded artifact executable
#   - execute the artifact and forward installer arguments
#   - clean up the temporary artifact unless --keep-installer is passed
#
# This script does NOT:
#   - extract payloads
#   - create Conda environments
#   - write service files
#   - call sudo
#   - call apt-get
#   - mutate ~/.local/share/blueprint-re/ or ~/.config/blueprint-re/ directly

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL="https://example.com/blueprint-re"
CHANNEL="stable"
VERSION=""
OFFLINE_MODE=0
KEEP_INSTALLER=0
INSTALLER_URL=""
CHECKSUM_URL=""
CHECKSUM_URL_OVERRIDE=""

# Global array for forwarded installer arguments (preserves word boundaries).
FORWARD_ARGS=()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

die() {
  echo "ERROR: $1" >&2
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

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --version)
        if [[ -n "${2:-}" ]]; then
          VERSION="$2"
          shift 2
        else
          die "--version requires a version argument."
        fi
        ;;
      --channel)
        if [[ -n "${2:-}" ]]; then
          CHANNEL="$2"
          shift 2
        else
          die "--channel requires a channel argument (stable|latest|dev)."
        fi
        ;;
      --offline)
        OFFLINE_MODE=1
        FORWARD_ARGS+=("$1")
        shift
        ;;
      --keep-installer)
        KEEP_INSTALLER=1
        shift
        ;;
      --installer-url)
        if [[ -n "${2:-}" ]]; then
          INSTALLER_URL="$2"
          shift 2
        else
          die "--installer-url requires a URL argument."
        fi
        ;;
      --checksum-url)
        if [[ -n "${2:-}" ]]; then
          CHECKSUM_URL_OVERRIDE="$2"
          shift 2
        else
          die "--checksum-url requires a URL argument."
        fi
        ;;
      --help|-h)
        cat <<'EOF'
Usage: curl -fsSL <url> | bash -s -- [OPTIONS]

Downloader Options:
  --version VERSION       Select an exact release version
  --channel stable|latest|dev  Select a release channel (default: stable)
  --keep-installer        Do not delete the downloaded installer after execution
  --installer-url URL     Use a custom artifact mirror URL
  --checksum-url URL      Provide a SHA-256 checksum file URL for custom artifacts

Forwarded to Installer:
  --offline               Fail if the embedded package cache is missing
  --upgrade               Require an existing installation and run upgrade flow
  --rollback VERSION      Switch to a previous release version

Other:
  --help                  Show this message

Environment:
  BLUEPRINT_INSTALLER_BASE_URL  Override the default release base URL
EOF
        exit 0
        ;;
      --rollback)
        FORWARD_ARGS+=("$1")
        shift
        ;;
      --skip-verify)
        if [[ -n "${BLUEPRINT_ALLOW_UNSAFE_SKIP_VERIFY:-}" ]]; then
          warn "BLUEPRINT_ALLOW_UNSAFE_SKIP_VERIFY is set; forwarding --skip-verify (UNSAFE)."
          FORWARD_ARGS+=("$1")
        else
          die "--skip-verify is not accepted by the network downloader."
        fi
        shift
        ;;
      *)
        FORWARD_ARGS+=("$1")
        shift
        ;;
    esac
  done
}

parse_args "$@"

# ---------------------------------------------------------------------------
# Phase 0: Host checks
# ---------------------------------------------------------------------------

info "Blueprint RE Downloader"

if [[ "$(uname -s)" != "Linux" ]]; then
  die "This installer supports Linux only. Detected: $(uname -s)"
fi

ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64)
    ARTIFACT_ARCH="linux-x86_64"
    ;;
  aarch64|arm64)
    ARTIFACT_ARCH="linux-aarch64"
    ;;
  *)
    die "Unsupported architecture: ${ARCH}. Supported: x86_64, aarch64."
    ;;
esac

require_cmd mktemp
require_cmd chmod
require_cmd tar

# Check for a download tool
DOWNLOADER=""
if command -v curl >/dev/null 2>&1; then
  DOWNLOADER="curl"
  CURL_OPTS="-fsSL"
elif command -v wget >/dev/null 2>&1; then
  DOWNLOADER="wget"
else
  echo "Neither curl nor wget is available." >&2
  echo "Please download the installer manually from:" >&2
  echo "  ${DEFAULT_BASE_URL}/releases/" >&2
  die "Download tool required."
fi

# ---------------------------------------------------------------------------
# Phase 1: Version resolution
# ---------------------------------------------------------------------------

BASE_URL="${BLUEPRINT_INSTALLER_BASE_URL:-${DEFAULT_BASE_URL}}"

if [[ -n "${VERSION}" ]]; then
  # Explicit version
  ARTIFACT_NAME="blueprint-re-${VERSION}-${ARTIFACT_ARCH}.sh"
  CHECKSUM_NAME="${ARTIFACT_NAME}.sha256"
  ARTIFACT_URL="${BASE_URL}/releases/${ARTIFACT_NAME}"
  CHECKSUM_URL="${BASE_URL}/releases/${CHECKSUM_NAME}"
else
  # Channel-based resolution
  case "${CHANNEL}" in
    stable|latest)
      METADATA_URL="${BASE_URL}/latest.json"
      info "Resolving latest version from ${METADATA_URL}..."
      if [[ "${DOWNLOADER}" == "curl" ]]; then
        METADATA="$(curl -fsSL "${METADATA_URL}" 2>/dev/null || true)"
      else
        METADATA="$(wget -qO- "${METADATA_URL}" 2>/dev/null || true)"
      fi
      if [[ -z "${METADATA}" ]]; then
        die "Could not resolve version from channel '${CHANNEL}'. The release endpoint may be unreachable."
      fi
      # Extract version from simple JSON (no jq dependency)
      VERSION="$(printf '%s\n' "${METADATA}" | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)".*/\1/')"
      if [[ -z "${VERSION}" ]]; then
        die "Could not parse version from release metadata."
      fi
      info "Resolved version: ${VERSION}"
      ARTIFACT_NAME="blueprint-re-${VERSION}-${ARTIFACT_ARCH}.sh"
      CHECKSUM_NAME="${ARTIFACT_NAME}.sha256"
      ARTIFACT_URL="${BASE_URL}/releases/${ARTIFACT_NAME}"
      CHECKSUM_URL="${BASE_URL}/releases/${CHECKSUM_NAME}"
      ;;
    dev)
      ARTIFACT_NAME="blueprint-re-dev-${ARTIFACT_ARCH}.sh"
      CHECKSUM_NAME="${ARTIFACT_NAME}.sha256"
      ARTIFACT_URL="${BASE_URL}/releases/${ARTIFACT_NAME}"
      CHECKSUM_URL="${BASE_URL}/releases/${CHECKSUM_NAME}"
      ;;
    *)
      die "Unknown channel: ${CHANNEL}. Use stable, latest, or dev."
      ;;
  esac
fi

# Allow explicit override
if [[ -n "${INSTALLER_URL}" ]]; then
  ARTIFACT_URL="${INSTALLER_URL}"
  # Priority: explicit --checksum-url > .sh auto-derivation > fail closed.
  if [[ -n "${CHECKSUM_URL_OVERRIDE}" ]]; then
    CHECKSUM_URL="${CHECKSUM_URL_OVERRIDE}"
  elif [[ "${ARTIFACT_URL}" == *.sh ]]; then
    CHECKSUM_URL="${ARTIFACT_URL}.sha256"
  else
    die "Custom --installer-url must end with .sh or --checksum-url must be provided. Refusing to run unverified installer."
  fi
fi

# ---------------------------------------------------------------------------
# Phase 2: Offline check
# ---------------------------------------------------------------------------

if [[ "${OFFLINE_MODE}" -eq 1 && -z "${ARTIFACT_URL}" ]]; then
  die "--offline requires a local artifact path or a reachable release endpoint with embedded caches."
fi

# ---------------------------------------------------------------------------
# Phase 3: Download
# ---------------------------------------------------------------------------

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

ARTIFACT_PATH="${TMP_DIR}/${ARTIFACT_NAME}"

info "Downloading ${ARTIFACT_NAME}..."

if [[ "${DOWNLOADER}" == "curl" ]]; then
  if ! curl -fsSL -o "${ARTIFACT_PATH}" "${ARTIFACT_URL}"; then
    die "Failed to download installer from ${ARTIFACT_URL}"
  fi
else
  if ! wget -q -O "${ARTIFACT_PATH}" "${ARTIFACT_URL}"; then
    die "Failed to download installer from ${ARTIFACT_URL}"
  fi
fi

info "Download complete: ${ARTIFACT_PATH}"

# ---------------------------------------------------------------------------
# Phase 4: Checksum verification
# ---------------------------------------------------------------------------

if [[ -n "${CHECKSUM_URL}" ]]; then
  info "Downloading checksum..."
  CHECKSUM_PATH="${TMP_DIR}/${CHECKSUM_NAME}"
  if [[ "${DOWNLOADER}" == "curl" ]]; then
    if ! curl -fsSL -o "${CHECKSUM_PATH}" "${CHECKSUM_URL}" 2>/dev/null; then
      die "Could not download checksum file from ${CHECKSUM_URL}. Refusing to run unverified installer."
    fi
  else
    if ! wget -q -O "${CHECKSUM_PATH}" "${CHECKSUM_URL}" 2>/dev/null; then
      die "Could not download checksum file from ${CHECKSUM_URL}. Refusing to run unverified installer."
    fi
  fi

  info "Verifying checksum..."
  EXPECTED_CHECKSUM="$(awk '{print $1}' "${CHECKSUM_PATH}")"
  ACTUAL_CHECKSUM="$(sha256sum "${ARTIFACT_PATH}" | awk '{print $1}')"
  if [[ "${EXPECTED_CHECKSUM}" != "${ACTUAL_CHECKSUM}" ]]; then
    die "Checksum mismatch! Expected ${EXPECTED_CHECKSUM}, got ${ACTUAL_CHECKSUM}. The download may be corrupted or tampered with."
  fi
  info "Checksum verified."
else
  die "No checksum URL available. Refusing to run unverified installer."
fi

# ---------------------------------------------------------------------------
# Phase 5: Execute installer
# ---------------------------------------------------------------------------

chmod +x "${ARTIFACT_PATH}"

info "Launching installer..."
info ""

# Forward all installer arguments that were collected.
bash "${ARTIFACT_PATH}" "${FORWARD_ARGS[@]}"

INSTALLER_EXIT=$?

# ---------------------------------------------------------------------------
# Phase 6: Cleanup
# ---------------------------------------------------------------------------

if [[ "${KEEP_INSTALLER}" -eq 1 ]]; then
  KEEP_PATH="${HOME}/.local/share/blueprint-re/downloads"
  mkdir -p "${KEEP_PATH}"
  mv "${ARTIFACT_PATH}" "${KEEP_PATH}/${ARTIFACT_NAME}"
  info "Installer preserved at: ${KEEP_PATH}/${ARTIFACT_NAME}"
fi

exit ${INSTALLER_EXIT}
