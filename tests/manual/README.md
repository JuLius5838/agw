# Manual / live tests

These require a **real** external-provider subscription (ChatGPT/Codex or GitHub
Copilot, depending on the enabled model) and a TTY. They are **not** part of the
default suite (they carry the `live` marker and are skipped) and must never run in CI.

## Prerequisites

```bash
uv sync --all-groups
uv run agw setup
uv run agw auth chatgpt     # when a ChatGPT/Codex model is enabled
uv run agw auth copilot    # when a GitHub Copilot model is enabled
```

## Run

```bash
uv run pytest tests/manual -m live -v
```

## What the live pilot must record (Gate C)

Capture **sanitized** evidence (no prompts, tokens, or codes) for each:

- [ ] Each enabled provider produces a main-session streamed response; sanitized proxy
      evidence shows the exact selected public model/provider.
- [ ] `agw auth copilot` exchanges its staged GitHub token for Copilot entitlement during
      model verification; failures remain attributed to Copilot with no fallback.
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
