# Architecture

## Overview

```text
                       claude / agw claude ARGS
                            │
              ┌─────────────▼──────────────┐
              │ ClaudeHarness.launch()     │
              │  • ensure proxy running    │
              │  • build FR-21 child env   │
              │  • os.execve(native claude)│
              └─────────────┬──────────────┘
   ANTHROPIC_BASE_URL=127.0.0.1:PORT, ANTHROPIC_CUSTOM_HEADERS=X-AGW-Key:…
                            │
              ┌─────────────▼──────────────┐
              │ AGW front router :PORT      │──── native Claude → Anthropic
              └─────────────┬──────────────┘
                            │ exact external model only
              ┌─────────────▼──────────────┐         supervisor
              │ private LiteLLM :free-port  │◀──────── sanitized 0600 log
              └──────────────┬───────────────┘
                       provider-isolated OAuth
```

## Module map (`src/agent_gateway/`)

| Module | Responsibility |
|--------|----------------|
| `cli.py` | Typer command tree; the frozen CLI surface; `agw claude` verbatim passthrough. |
| `errors.py` | Typed error hierarchy → stable process exit codes. |
| `paths.py` | XDG-aware paths; atomic writes; `0700`/`0600` permission primitives; packaged resources. |
| `config.py` | `config.yaml` (non-secret choices); native-Claude validation; reads Claude settings (never writes). |
| `secret_store.py` | The local loopback proxy key (generate-once, reuse). |
| `redaction.py` | Central secret redaction (masks secrets, preserves model/provider names). |
| `model_registry.py` | Typed external mappings, optional native default, hidden picker IDs. |
| `litellm_config.py` | Deterministic LiteLLM config rendering + fingerprint. |
| `litellm_runner.py` | Pinned compatibility entry point: routes ChatGPT through LiteLLM's existing Responses adapter. |
| `providers/` | `Provider` enum + prefixes; isolated ChatGPT/Codex and GitHub Copilot auth adapters over LiteLLM. |
| `auth.py` | Auth orchestration: stage → probe → atomic swap; TTY gate; preserve-on-cancel. |
| `process.py` | PID+create_time process identity (reuse-safe); port helpers. |
| `proxy.py` | Lifecycle: ensure/reuse/stop/restart; reuse classification; readiness. |
| `router.py` | Authenticated front router; native passthrough and external isolation. |
| `supervisor.py` | Detached daemon: launches router plus optional private LiteLLM child. |
| `observability.py` | Allowlist + redaction for what may be persisted from LiteLLM output. |
| `models.py` | `models list` (+JSON) and `models verify` (Anthropic-protocol checks). |
| `usage.py` | Unified usage model/renderer; Claude snapshot capture and isolated Codex App Server account RPC client. |
| `claude_integration.py` | Inert `/agw-usage` registration, personal companion-plugin installation, and non-destructive Claude status-line collector setup. |
| `claude_extension.py` | Local MCP server for the zero-model-call native usage dialog and same-terminal fallback. |
| `external_subagents.py` | Generated read-only agent definitions per verified external model; `--agents` merge; launch-time eligibility. |
| `claude_compat.py` | Claude executable identity, conservative `availableModels` policy, and one compatibility record per model. |
| `probe_events.py` | Secure append-only NDJSON evidence shared by the router and the probe hooks. |
| `subagent_probe.py` | The compatibility probe: `PreToolUse`/`SubagentStart` hooks, router registration, bounded verifier process, evidence join. |
| `harnesses/claude.py` | The Claude harness: FR-21 env contract + `execve` launch. |
| `shell.py` | Bare-`claude` integration (enabled by setup by default, marker-guarded, idempotent, reversible). |
| `setup.py` / `doctor.py` / `uninstall.py` | Lifecycle commands. |

## Key design decisions

- **Native Claude starts without external credentials.** LiteLLM is skipped when no
  external entry is active; enabling one requires that provider's OAuth first.
- **ChatGPT uses Responses translation.** LiteLLM 1.93.0 contains the required
  Anthropic↔Responses adapter but omits `chatgpt` from its dispatch set. The AGW runner
  extends that pinned set before starting the normal LiteLLM CLI, so Claude system content
  is sent as Responses `instructions`.
- **Copilot uses LiteLLM's native provider adapter.** `copilot` registry entries must use
  `github_copilot/` upstream IDs and an independent `GITHUB_COPILOT_TOKEN_DIR`. Device login
  stages only a GitHub access token; `agw models verify` performs the entitlement exchange
  and is required before compatibility is claimed for an exact model.
- **Reuse is HTTP-free.** A proxy is reused when its recorded supervisor, router, and
  optional LiteLLM
  identities are alive, the runtime/config fingerprint matches, and the socket is
  listening. `/v1/models` is verified once at boot; reuse doesn't depend on a fresh
  round-trip (which is flaky under CPU load from many cold launchers).
- **Process identity, not bare PIDs.** Every stop/terminate re-checks PID + create_time so
  a reused PID is never signalled.
- **No fallback, ever.** A missing/unauthorized/incompatible model fails under its public
  name. Managed `availableModels` substitution is detected and reported, never masked.
- **Model names are data.** The registry is the single source of truth; provider prefixes
  never appear in public names or normal prompts.
- **Usage credentials are transient.** Codex account RPCs receive the existing AGW
  ChatGPT access token over JSONL stdin in an isolated temporary `CODEX_HOME`; usage
  output and the Claude snapshot use strict field allowlists and never contain credentials.
  The bridge is gated to the tested Codex CLI series before receiving a token, and usage
  reads—but never refreshes or rewrites—the live AGW credential. App Server receives a
  minimal environment rather than inheriting Claude/AGW/provider variables.
- **External subagents are evidence-gated, not assumed.** The Agent tool's `model` argument
  is Claude-only, so an external model reaches a subagent through a generated custom-agent
  *definition*. Because Claude Code may replace a definition-level model it rejects, a type
  is injected only from a `current` record under `<state>/external-subagents/compatibility/`
  keyed to the exact Claude path, canonical target, digest, version, model, provider,
  routing fingerprint, and contract version. Statuses are `current`, `missing`, `invalid`,
  `failed`, `stale`, and `policy_excluded`; only `current` is advertised. A launch with no
  stored evidence at all skips executable identification entirely, so an ordinary session
  pays no `claude --version` or digest cost.
- **The probe is the only thing that records routes.** During `models verify
  --claude-subagent`, the `SubagentStart` hook appends the child's id and, in the same
  synchronous call, registers a one-shot authorization at
  `POST /_agw/control/v1/subagent-probes` (loopback peer, `X-AGW-Key`, 60-second expiry,
  in-memory only). Claude Code drains that hook before the child's first model request, so
  registration cannot race it. Exactly one matching request consumes the registration and
  appends one route event. Ordinary sessions register nothing and persist nothing, and a
  router restart loses registrations so verification fails closed.
- **Probe evidence is a trust boundary.** Events live in `0700` directories as `0600` files,
  are appended with `O_APPEND|O_NOFOLLOW` after re-validating containment, symlinks, owner,
  mode, type, and device/inode, and each record is a single `os.write` below `PIPE_BUF`.
  Records carry ids, model/provider names, and booleans — never prompts, responses, tool
  content, headers, or credentials. `x-claude-code-agent-id` and
  `x-claude-code-parent-agent-id` are stripped before any external forwarding.
- **The probe process is bounded and always cleaned.** It runs non-interactively in its own
  session and its own working directory, with a 90-second timeout, a spend cap, and
  identity-checked SIGTERM/SIGKILL to the *process group* so hook children cannot be
  orphaned. Event file, fixture, and run directory are removed on every exit path, including
  cancellation, and a previous success is invalidated before the probe starts.

## Concurrency

`ensure_running` serializes on `proxy.lock` (filelock). Ten concurrent launchers converge
on exactly one daemon (verified by `tests/contract/test_proxy_lifecycle.py`).
