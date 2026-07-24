# Agent Gateway shell integration (generated file — safe to remove).
#
# Routes a bare `claude` command through the managed gateway (`agw claude`).
# To run the native Claude CLI directly, bypassing the gateway:  command claude
if command -v agw >/dev/null 2>&1; then
  claude() { command agw claude "$@"; }
fi
