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

## Concurrency

`ensure_running` serializes on `proxy.lock` (filelock). Ten concurrent launchers converge
on exactly one daemon (verified by `tests/contract/test_proxy_lifecycle.py`).
