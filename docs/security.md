# Security & Threat Model

Agent Gateway holds personal OAuth credentials, runs a local daemon, and can edit
a shell startup file. This document records the threats it defends against and how.

## Assets

- **Provider OAuth tokens** (ChatGPT/Codex, GitHub Copilot) under
  `~/.config/agent-gateway/credentials/<provider>/`.
- **Local proxy key** (`credentials/proxy-key`) — a loopback credential that
  authenticates Claude Code to the local proxy.
- **Prompts and tool schemas** in transit to the selected upstream provider.

## Threats and mitigations

| # | Threat | Mitigation |
|---|--------|-----------|
| T1 | Another local user/process reads credentials | `0700` dirs, `0600` files; created with restrictive perms *before* content is written; verified by `doctor`. |
| T2 | Secrets leak into logs/diagnostics | Central redaction + an allowlist supervisor that persists only lifecycle/error metadata; request/prompt/tool logging disabled in generated LiteLLM config. Raw-log canary tests inspect the persisted file directly. |
| T3 | Device code / verification URL captured | Shown only on the live TTY; denied by the log allowlist; never written to persistent output. |
| T4 | Proxy exposed off-host | Binds `127.0.0.1` only; requires the proxy key; readiness verified via authenticated `/v1/models`. |
| T5 | PID reuse causes signalling the wrong process | Process identity = PID + create_time; every stop/terminate re-checks identity. |
| T6 | Rogue listener on the configured port | Reuse only when identity + config fingerprint match and the socket is ours; otherwise reported as a port conflict, never reused. |
| T7 | Shell wrapper recursion | The launcher `execve`s the resolved absolute native Claude path; `command claude` bypasses the function. |
| T8 | Gateway bypass via inherited routing flags | Launcher strips `CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY` and `CLAUDE_CODE_SUBAGENT_MODEL` for the gateway child. |
| T9 | Silent model substitution / fallback | No fallback anywhere; a missing/unauthorized/incompatible model fails under its public name. Managed `availableModels` substitution is detected and reported, never called success. |
| T10 | Supply-chain (compromised LiteLLM) | Versions pinned in `uv.lock`; `1.82.7`/`1.82.8` forbidden by tests and CI; upgrades require a reviewed change. |
| T11 | Credential exfiltration into Git | `.gitignore` excludes runtime/state; gitleaks + a shipped-artifact secret scan run in CI. |
| T12 | Config drift under an active session | A changed runtime/config fingerprint fails new launches with an explicit `agw proxy restart`; it never restarts under a running session implicitly. |
| T13 | Claude credentials reach an external provider | The front router strips `Authorization` and `x-api-key` before an external request and injects only the private LiteLLM key; provider token directories are removed from the router environment. |
| T14 | Unified usage leaks session/provider credentials | Claude capture allowlists only `rate_limits.five_hour` and `rate_limits.seven_day`. Codex App Server receives the AGW token over stdin (never argv) only after a tested-version gate, in a `0700` temporary `CODEX_HOME` destroyed after the account RPCs. Its child environment is rebuilt from a minimal non-secret/runtime-network allowlist instead of inheriting Claude/AGW/provider variables. App Server responses are reduced to documented scalar allowlists before rendering/JSON output, and usage never mutates the live credential. |

## Data flow / privacy

Prompts and tool schemas are sent to the selected upstream provider (OpenAI for
ChatGPT/Codex, GitHub for Copilot) under **that provider's account and data
policy**. This is a compatibility- and policy-gated use of LiteLLM's subscription
bridges. Native Claude traffic remains direct to Anthropic using Claude Code's
saved login. See `docs/policy-decision.md`.

## Update & rollback

Runtime and LiteLLM versions are pinned in `uv.lock`. An upgrade is a reviewed
repository change that bumps both the plugin and runtime versions. Rollback pins
the prior marketplace/plugin version, reinstalls the prior runtime, and restarts
the managed proxy; it never rolls back or copies OAuth credentials.
