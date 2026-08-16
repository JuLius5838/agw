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
- [ ] Cancelled forced reauth preserves prior credentials; revoked auth fails clearly.
- [ ] Raw-log canary: a throwaway canary prompt never appears in the persisted `proxy.log`.
- [ ] Port conflict, config-fingerprint mismatch, and concurrent launch behave per spec.

### External subagents

Automated by the `live` tests in `live_smoke.py`:

- [ ] `agw models verify NAME --claude-subagent` passes for at least two external models,
      and each stores `current` evidence bound to this exact Claude executable, canonical
      target, digest, version, model, provider, routing fingerprint, and contract version.
- [ ] Those models are then advertised as `agw-…` generated types with the read-only
      `Read`/`Grep`/`Glob` contract.
- [ ] No probe artifact survives, and no compatibility record or `proxy.log` line contains a
      prompt, credential, or the probe's fixture canary.

Operator-driven, in a real managed `claude` session (record sanitized notes):

- [ ] Ask for a multi-model review. Claude selects two different `agw-…` types by
      `subagent_type` only — never passing an external name to the Agent tool's `model`.
- [ ] The two children run concurrently and both results reach the parent, which synthesizes
      them. Timestamps must overlap for concurrency to count.
- [ ] Cancel mid-run: control returns to the parent promptly and no model is substituted.
- [ ] Force an external-provider failure: the subagent fails under its exact public
      name/provider, and the parent discloses the missing perspective rather than silently
      answering with a different model.
- [ ] Set a restrictive `availableModels` allowlist and re-verify: the probe **fails**.
      Native inheritance must never produce a passing record.

Record results and versions in `docs/compatibility-report.md`.
