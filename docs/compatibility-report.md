# Compatibility Report (Gate A)

**Last updated:** 2026-07-23  
**Platform tested:** macOS (arm64, Darwin 25.5.0)

## Pinned versions

| Component | Version | Notes |
|-----------|---------|-------|
| Python | 3.12.9 | pinned `>=3.12,<3.13` |
| uv | 0.11.8 | |
| LiteLLM | **1.93.0** | `litellm[proxy]`, pinned exactly; compromised `1.82.7`/`1.82.8` are forbidden |
| Claude Code | **2.1.218** | original pilot version for main-session routing |
| Claude Code | **2.1.233** | external-subagent target, live-verified; the Agent tool's `model` argument accepts only `sonnet\|opus\|haiku\|fable`, so external models route through generated custom-agent definitions instead |

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
- ✅ **Copilot adapter registration.** LiteLLM 1.93.0 ships a `github_copilot` OAuth
  authenticator keyed by `GITHUB_COPILOT_TOKEN_DIR`, uses `access-token` plus a derived
  `api-key.json`, and has a cataloged `github_copilot/gpt-4.1` chat entry. AGW restores that
  provider behind an isolated adapter and a disabled candidate. The pinned catalog has no
  Kimi entry, so AGW does not publish or infer a Kimi slug.

## Key provider finding

LiteLLM's `chatgpt/` and `github_copilot/` providers can enter interactive device-code
flows when called without valid tokens. AGW therefore validates every enabled external
provider before starting the private LiteLLM child and applies a bounded readiness timeout.
The Copilot offline auth probe confirms only that device login produced a non-empty GitHub
access token. Because a GitHub sign-in can succeed for an account with no active Copilot
plan, `agw auth copilot` and an online `agw doctor` additionally perform a bounded,
redacted token→API-key exchange and report a lapsed subscription in plain language (GitHub's
own "your subscription has ended" message) instead of surfacing LiteLLM's opaque "no healthy
deployments" error only after a model is enabled and called. The full request path is still
confirmed by the live `agw models verify` call. With no enabled external model, LiteLLM is
intentionally absent and native Claude remains usable.

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
selectable with `/model NAME` and `--model NAME`. The authenticated `/v1/models` endpoint
remains available for a future Claude release that supports discovery without replacing
native subscription authentication.

## External subagents (Claude Code 2.1.232)

The picker and the Agent tool are separate surfaces with separate constraints. The Agent
tool validates its explicit `model` argument against `sonnet|opus|haiku|fable` and rejects
an external name client-side, before any request reaches AGW. A Claude Code custom-agent
*definition* carries its own `model` field, so AGW generates one read-only definition per
verified external model and injects them at launch with `--agents`; Claude then selects the
type by `subagent_type` and omits the override.

Anthropic does not support routing Claude Code to non-Claude models through a gateway, and
Claude Code may replace a definition-level model it rejects with the inherited parent model
and a warning — so a subagent that answers is not evidence that the external model ran. A
generated type is therefore advertised only from a successful, current probe run explicitly
by `agw models verify NAME --claude-subagent`, and the evidence is bound to the exact Claude
executable path, canonical target, SHA-256, version string, model, provider, AGW routing
fingerprint, and definition contract version. Any change makes it stale.

This is a verification gate, not per-spawn runtime enforcement: it proves the behavior of
one Claude build under a controlled probe. Ordinary sessions register no probe and persist
no per-request evidence, so a later Claude regression is surfaced by re-verifying rather
than caught retroactively on an unobserved spawn.

### Live result (2026-08-16, Claude Code 2.1.233)

✅ `gpt-5.6-luna`, `gpt-5.6-sol`, and `gpt-5.6-terra` each pass
`agw models verify NAME --claude-subagent`. Correlated evidence shows the parent selecting
the generated type with no explicit model override, the child starting under that type, and
that child's request reaching the router under the exact public name and `chatgpt` provider.
The run's own `modelUsage` independently reports both `claude-sonnet-5` (parent) and the
external model (child), so native inheritance is ruled out. Probe state is removed
afterwards, and no canary, prompt, or credential appears in any record or in `proxy.log`.

Three client behaviors the probe has to tolerate, all observed live:

- **Non-zero exit is not failure.** Claude Code exits `1` merely for printing
  `[claude-code:unrecognized_model]` to stderr about an external name it does not know —
  after routing that name, running the child on it, and completing successfully. The final
  result object (`subtype: success`, `is_error: false`) is the authoritative outcome.
- **The answer can arrive in a second result.** Claude may spawn the child in the
  background, answer "it's running now", and deliver the real content later in another
  result carrying `origin: {"kind": "task-notification"}`. Every successful result must be
  considered, not just the first.
- **`--print --output-format=stream-json` requires `--verbose`,** and `--mcp-config` rejects
  a bare `{}` — the empty configuration must be spelled `{"mcpServers":{}}`.

One unrelated rough edge, not a gate failure: the ChatGPT-backed child sometimes calls
`Read` with `"pages": ""`, which the tool rejects (`Invalid pages parameter`). The model
retries and completes, so the round trip succeeds, but it wastes a turn.

## Still requires the live pilot

Real subscription flows are deliberately `live`-marked and are not part of offline CI:

- [ ] End-to-end native Claude response through the front router, confirming saved-login
      usage and subscription accounting.
- [ ] End-to-end ChatGPT/Codex response through `/v1/messages`, including streaming, tools,
      and the single custom picker entry on the pinned Claude Code version.
- [ ] `agw auth copilot`, derived entitlement exchange, and an end-to-end
      `github_copilot/gpt-4.1` response under a real Copilot subscription.
- [ ] `agw models verify` full Anthropic-protocol contract against each real external
      upstream.
- [ ] Per-task subagent and agent-team routing with agent-ID-correlated evidence.
