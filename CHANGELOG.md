# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses a single version for
the plugin and the runtime, bumped together on every release.

## [Unreleased]

### Removed
- GitHub Copilot routing and authentication are deferred until a real subscription
  can exercise the live compatibility gate. Setup safely removes legacy Copilot
  model-registry entries during upgrade without deleting stored credentials and
  keeps a permission-restricted pre-migration registry for rollback.

### Fixed
- Conversations can switch between ChatGPT/Codex and native Claude models
  without invalid historical reasoning blocks breaking the next request or
  `/compact`. GPT reasoning items remain provider-private, while native routes
  remove legacy empty-and-unsigned thinking placeholders and preserve signed
  Claude omitted-thinking and redacted-thinking blocks.
- `/agw-usage` can be reopened repeatedly in one Claude Code session. The
  fail-closed command guard now waits until the MCP elicitation has returned
  and the tool response is written before blocking slash-command expansion,
  so Claude does not cancel and strand the companion server after the first
  Close.
- ChatGPT/Codex Web Search requests now normalize Anthropic's generic
  `tool_choice` values to the scalar form required by the OpenAI Responses API.
- `/agw-usage` coordination, provider collection, native UI, and terminal
  fallback now share one bounded deadline below Claude Code's hook timeout.

### Added
- `agw` runtime and Claude Code plugin: route Claude Code through a per-developer,
  loopback-only hybrid router. Native Claude requests retain Claude Code's saved
  subscription login; ChatGPT/Codex models route through LiteLLM using each
  developer's provider login.
- ChatGPT/Codex Responses bridge for Claude Code's Anthropic Messages requests,
  including system-instruction, streaming, and tool-call translation.
- Native Claude Code `/effort` forwarding to ChatGPT/Codex reasoning effort,
  preserving `low`, `medium`, `high`, `xhigh`, and `max`.
- Claude Code Web Search compatibility for ChatGPT subscription models by
  translating Anthropic's hosted search declaration into the client-executed
  `WebSearch` function instead of unsupported `web_search_preview`.
- Overridable `default_effort`: applies a startup level without the hard-override
  behavior of `CLAUDE_CODE_EFFORT_LEVEL`.
- Optional per-model `display_name` for picker labels without changing exact
  routable model names.
- Unified `agw usage` and `/agw-usage` dashboard for Claude subscription windows,
  Codex limits/reset credits, and Codex token activity. Claude fields are captured
  through its documented status-line payload; Codex uses official App Server account RPCs
  with a tested `0.145.x` compatibility gate and a setup-pinned executable path/hash
  before receiving a ChatGPT OAuth token.
- Zero-model-call `/agw-usage` UI: a deterministic `UserPromptExpansion` hook invokes
  AGW's local plugin MCP tool directly, opens Claude Code's native interactive dialog,
  and blocks skill expansion before any Claude or external-model request. Refresh and
  Close actions stay inside the current terminal session. A parallel fail-closed command
  guard prevents model expansion during MCP startup/disconnection races and renders the
  same-terminal local fallback when the native dialog is unavailable.
- CLI: `setup`, `auth`, `models list|verify`, `proxy start|stop|restart|status`, `claude`,
  `shell enable|disable|status`, `doctor`, `status`, `logs`, `uninstall`.
- Isolated provider auth adapters with staged→atomic-swap credential handling and a TTY gate.
- Managed proxy lifecycle: PID+create_time process identity, single-daemon concurrency,
  stale-state recovery, port-conflict handling, private-backend readiness probing, and
  runtime-ABI/config fingerprints that prevent stale-daemon reuse after upgrades.
- Claude harness: FR-21 environment contract, verbatim argument passthrough, `execve` launch.
- Bare-`claude` shell integration (marker-guarded, idempotent, reversible).
- Deterministic LiteLLM config rendering; validated model registry with duplicate-owner
  rejection and no fallback.
- Plugin manifests + `gateway-setup`, `gateway-doctor`, and `model-routing` skills.
- Security & CI: gitleaks config, shellcheck, dependency-policy guard (forbids compromised
  LiteLLM `1.82.7`/`1.82.8`), secret-hygiene and raw-log canary tests, GitHub Actions CI,
  CodeQL, dependency review, and Dependabot.
- Public repository metadata: Apache-2.0 license, contribution guidance, issue and pull
  request templates, personal ownership, and public Claude Code marketplace instructions.
- Docs: architecture, security/threat-model, operations, model-selection, compatibility
  report (Gate A), and adopter policy-decision template.

### Pinned
- Agent Gateway `0.1.9`; LiteLLM `1.93.0`; Python `3.12`; minimum Claude Code
  `2.1.211` (tested on `2.1.218`).

### Notes
- Live provider pilot (Gate C) remains pending. Each user or adopting organization must also
  complete its own provider-policy review before enabling external routes; see
  `docs/compatibility-report.md` and `docs/policy-decision.md`.
