# Compatibility Report (Gate A)

**Last updated:** 2026-07-23  
**Platform tested:** macOS (arm64, Darwin 25.5.0)

## Pinned versions

| Component | Version | Notes |
|-----------|---------|-------|
| Python | 3.12.9 | pinned `>=3.12,<3.13` |
| uv | 0.11.8 | |
| LiteLLM | **1.93.0** | `litellm[proxy]`, pinned exactly; compromised `1.82.7`/`1.82.8` are forbidden |
| Claude Code | **2.1.218** | current pilot version; per-invocation subagent models supported |

## Technical feasibility — verified offline

The automated non-live suite verifies the approved hybrid architecture without provider
credentials or network access:

- ✅ **Native-only startup.** The detached supervisor and authenticated front router start,
  reuse, recover stale state, reject port conflicts, and converge under concurrent launches
  without starting LiteLLM or requiring external OAuth.
- ✅ **Hybrid routing contract.** Native/unknown Claude names preserve request bodies,
  request-time Claude authentication, Anthropic beta headers, upstream status, and streaming
  responses on the direct Anthropic path.
- ✅ **External credential isolation.** Exact configured external names—and hidden picker
  IDs representing them—are rewritten for private LiteLLM. Claude credentials are removed
  and only the internal LiteLLM bearer is injected.
- ✅ **Authenticated discovery.** `/v1/models` rejects missing or wrong AGW keys and returns
  discovery-compatible hidden IDs with exact configured display names.
- ✅ **Deterministic state.** Registry/config rendering is deterministic and runtime
  fingerprints detect configuration or version drift.
- ✅ **Launcher contract.** The harness points Claude Code at the front router, adds only an
  `X-AGW-Key` custom header, removes inherited Anthropic API/token overrides, preserves the
  saved Claude Code login and native family defaults, forwards arguments verbatim, and
  returns Claude's exit code.
- ✅ **ChatGPT Responses dispatch.** LiteLLM 1.93.0's Anthropic→Responses adapter correctly
  maps Claude system content to `instructions`, but its provider allowlist omits
  `chatgpt`. AGW's pinned runner adds only that provider before invoking LiteLLM's normal
  CLI; a compatibility test fails if the internal surface changes.

## Key provider finding

LiteLLM's `chatgpt/` provider can enter an interactive device-code flow when it starts
without a valid token. AGW therefore validates every enabled external provider before
starting the private LiteLLM child and applies a bounded readiness timeout. With no enabled
external model, LiteLLM is intentionally absent and native Claude remains usable.

Claude is not configured as a LiteLLM provider. The front router forwards native requests
directly with Claude Code's request-time saved-login headers; AGW has no `auth anthropic`
command and no Anthropic credential store.

## Claude Code 2.1.218 picker constraint

The documented gateway discovery endpoint works only after Claude Code classifies the
session as gateway-authenticated. The 2.1.218 implementation obtains that state from a
gateway JWT and does not run discovery for AGW's saved-login plus custom-header
combination. Supplying the expected gateway credential would replace the saved Claude
subscription, violating the hybrid contract.

AGW therefore uses Claude's documented `ANTHROPIC_CUSTOM_MODEL_OPTION` fallback to add one
exact external model to the visual picker. The remaining active names are directly
selectable with `/model NAME`, `--model NAME`, and per-agent assignment. The authenticated
`/v1/models` endpoint remains available for a future Claude release that supports discovery
without replacing native subscription authentication.

## Still requires the live pilot

Real subscription flows are deliberately `live`-marked and are not part of offline CI:

- [ ] End-to-end native Claude response through the front router, confirming saved-login
      usage and subscription accounting.
- [ ] End-to-end ChatGPT/Codex response through `/v1/messages`, including streaming, tools,
      and the single custom picker entry on the pinned Claude Code version.
- [ ] `agw models verify` full Anthropic-protocol contract against each real external
      upstream.
- [ ] Per-task subagent and agent-team routing with agent-ID-correlated evidence.
