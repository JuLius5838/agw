# Operations

## Command reference

| Command | Purpose |
|---------|---------|
| `agw setup [--default-model M] [--provider-owner M=P] [--agent-teams/--no-agent-teams] [--enable-shell SHELL\|--no-shell]` | Install local state, resolve models, persist choices. Non-destructive; idempotent. |
| `agw auth chatgpt [--model M] [--force]` | Authenticate a ChatGPT/Codex subscription (needs a TTY). Claude authentication remains in native Claude Code. |
| `agw models list [--all] [--json]` | Active models (with `--all`, inactive candidates too). |
| `agw models verify [MODEL]` | Anthropic-protocol compatibility checks through the proxy. |
| `agw usage [--json]` | Unified Claude subscription windows and live Codex limits, reset credits, and token activity. |
| `agw proxy start\|stop\|restart\|status` | Manage the loopback proxy. `restart` is the only path that applies a changed config/version. |
| `agw claude [ARGS...]` | Launch Claude Code through the gateway (args forwarded verbatim). |
| `agw shell enable\|disable\|status [zsh\|bash]` | Manage the bare-`claude` integration (setup enables it by default unless `--no-shell`). |
| `agw doctor` | Prerequisites, paths/perms, models, provider auth, proxy state. Secrets redacted. |
| `agw status` | Managed proxy identity + model→provider mapping. |
| `agw logs [--lines N]` | Sanitized operational logs. |
| `agw uninstall [--credentials [--yes]]` | Stop proxy, remove generated files. Credentials only with explicit ack. |

Exit codes: `0` ok · `1` general · `3` config · `4` auth · `5` model-unavailable ·
`6` port-conflict · `7` prerequisite · `8` proxy · `9` not-implemented · `10` internal.

## First run

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap.sh"   # install runtime + agw setup
agw setup --provider-owner gpt-5.6-sol=chatgpt      # enable one exact external route
agw auth chatgpt                                    # only if a ChatGPT model is enabled
claude                                              # native default; one external picker entry available
```

After restarting Claude Code and completing one response, `/agw-usage` opens the same
report in Claude Code's native interactive dialog. A deterministic plugin hook calls
AGW's local MCP tool and blocks expansion before a model sees the command, so opening or
refreshing the report consumes no model tokens. A parallel command guard fails closed
during MCP startup or disconnection and renders the report directly in the same terminal
instead of allowing model expansion. Setup installs the inert command
registration under `${CLAUDE_CONFIG_DIR:-~/.claude}/skills/agw-usage/`, the companion
plugin under `skills/agent-gateway/`, and, when no custom status line exists, a silent
collector that caches only Claude's documented rate-limit fields. Existing custom status
lines or unrelated files are preserved and reported. `agw uninstall` removes only
unmodified AGW-owned artifacts.

Codex usage requires the tested Codex CLI `0.145.x` series. AGW rejects other versions
and verifies the executable path and SHA-256 pinned by `agw setup` before passing its
ChatGPT token to the experimental external-token bridge. After reviewing or upgrading
Codex CLI, rerun `agw setup`. If the AGW access token has expired, use a Codex model once
so the normal provider path refreshes it, then retry; `agw usage` never mutates the live
credential.

The proxy will not start until **every active model's provider** is authenticated (this
avoids LiteLLM's unauthenticated-provider startup hang). Authenticate each provider you
have active models for, or disable the ones you are not using.

## Enabling an external model and choosing its provider

External candidates ship disabled. Enable the exact model/provider pair you intend to use:

```bash
agw setup --provider-owner gpt-5.6-sol=chatgpt
```

The same option keeps provider selection explicit and leaves room for additional
subscription backends in a future release.

## Switching models

- Start on a model: `claude --model gpt-5.6-sol`
- Switch mid-session: type `/model gpt-5.6-sol` in Claude
- The visual picker shows one exact configured external name; select other active names
  directly because preserving native subscription OAuth prevents multi-model discovery in
  Claude Code 2.1.218.

## Upgrades & rollback

Runtime and LiteLLM versions are pinned in `uv.lock`. An upgrade is a reviewed change that
bumps the plugin and runtime versions. If a proxy is running under an older
config/version, new launches fail with an explicit `agw proxy restart` instruction rather
than restarting under an active session. Rollback pins the prior plugin/runtime version and
restarts the proxy; it never touches OAuth credentials. When an upgrade retires a
provider route, setup stores the original registry at
`~/.config/agent-gateway/models.pre-retired-providers.yaml`; restore that file as
`models.yaml` before reinstalling the prior runtime.

## Troubleshooting

| Symptom | Action |
|---------|--------|
| `provider not authenticated` | Enable only the external model you need, then run `agw auth chatgpt` |
| `legacy copilot creds` | Copilot support is removed. Review and delete `~/.config/agent-gateway/credentials/copilot/`, or run `agw uninstall --credentials` to remove every stored provider credential. |
| `port … held by an unrelated process` | free the port or change `port` in `~/.config/agent-gateway/config.yaml`, then `agw proxy restart` |
| `proxy running with a different config/version` | `agw proxy restart` |
| `did not become ready within 10s` | check `agw logs`; usually an unauthenticated provider |
| bare `claude` not using gateway | `agw shell enable`; open a new shell, or `source ~/.config/agent-gateway/shell/agw.<shell>` |
| `/agw-usage` says Claude unavailable | restart Claude Code and complete one Claude response; setup never replaces an existing custom status line |
| `/agw-usage` says Codex unavailable | install/review Codex CLI `0.145.x`, run `agw setup` to pin it, then run `agw auth chatgpt`; if the credential needs refresh, use a Codex model once or reauthenticate with `--force` |
| need native Claude | `command claude` |
