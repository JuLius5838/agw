#!/usr/bin/env bash
# Agent Gateway bootstrap: install/upgrade the pinned `agw` runtime with uv, then
# run `agw setup`. Invoked by the gateway-setup skill from the plugin cache.
#
# Safety:
#   * requires `uv`; makes no privileged or system-wide changes.
#   * refuses to clobber an `agw` on PATH that this installation does not own
#     (detected via a marker file next to the resolved executable).
#   * every path is quoted.
set -euo pipefail

MARKER_NAME=".agent-gateway-owned"

log()  { printf 'agw-bootstrap: %s\n' "$1"; }
die()  { printf 'agw-bootstrap: error: %s\n' "$1" >&2; exit 1; }

# Resolve the plugin root (this script lives in <root>/scripts/bootstrap.sh).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
PLUGIN_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd -P)"
log "plugin root: ${PLUGIN_ROOT}"

command -v uv >/dev/null 2>&1 || die "uv is required but was not found on PATH. Install it from https://docs.astral.sh/uv/ and retry."

# Ownership-aware preflight: if an `agw` is already on PATH, only proceed when it
# is one we installed (marker file beside the resolved binary). This prevents
# clobbering an unrelated `agw`.
if existing="$(command -v agw 2>/dev/null)"; then
  resolved="$(cd -- "$(dirname -- "${existing}")" >/dev/null 2>&1 && pwd -P)/$(basename -- "${existing}")"
  if [ -e "$(dirname -- "${resolved}")/${MARKER_NAME}" ] || [ -e "${resolved}.${MARKER_NAME}" ]; then
    log "found an existing agent-gateway-owned agw; upgrading in place."
  else
    die "an unrelated 'agw' executable is already on PATH at '${existing}'. Refusing to overwrite it. Remove or rename it, then retry."
  fi
fi

log "installing the pinned Python 3.12 runtime with uv (from ${PLUGIN_ROOT})..."
uv tool install \
  --force \
  --refresh-package agent-gateway \
  --python 3.12 \
  --from "${PLUGIN_ROOT}" \
  agent-gateway

# Drop an ownership marker next to the installed executable so future runs can
# recognize this installation.
if installed="$(command -v agw 2>/dev/null)"; then
  bindir="$(cd -- "$(dirname -- "${installed}")" >/dev/null 2>&1 && pwd -P)"
  : > "${bindir}/${MARKER_NAME}" 2>/dev/null || true
  log "installed agw at ${installed}"
else
  die "installation completed but 'agw' is not on PATH. Ensure uv's tool bin dir is on PATH (run: uv tool update-shell)."
fi

log "running 'agw setup'..."
agw setup "$@"
log "done. Enable one exact route with 'agw setup --provider-owner MODEL=PROVIDER', authenticate that provider, then use 'claude'."
