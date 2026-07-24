# Manual / live tests

These require a **real** ChatGPT/Codex subscription and a TTY. They are **not** part
of the default suite (they carry the `live` marker and are skipped) and must never
run in CI.

## Prerequisites

```bash
uv sync --all-groups
uv run agw setup
uv run agw auth chatgpt
```

## Run

```bash
uv run pytest tests/manual -m live -v
```

## What the live pilot must record (Gate C)

Capture **sanitized** evidence (no prompts, tokens, or codes) for each:

- [ ] ChatGPT/Codex main-session streamed response; proxy evidence shows the ChatGPT upstream.
- [ ] `agw models verify` passes the full Anthropic-protocol contract against a real upstream.
- [ ] Default model used when no `--model` is passed; `/model` switch works.
- [ ] Single custom picker entry on the pinned Claude version; direct selection for the
      remaining external names.
- [ ] Two-model agent team: each teammate's spawned agent-ID correlates to its requested
      public name/provider. Any `availableModels` substitution **fails** this check.
- [ ] Cancelled forced reauth preserves prior credentials; revoked auth fails clearly.
- [ ] Raw-log canary: a throwaway canary prompt never appears in the persisted `proxy.log`.
- [ ] Port conflict, config-fingerprint mismatch, and concurrent launch behave per spec.

Record results and versions in `docs/compatibility-report.md`.
