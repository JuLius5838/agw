# Policy Decision Record

> **Status:** DRAFT — requires named-owner sign-off before broad team rollout (Gate D).
> This file records the decision; it must not contain vendor credentials or terms text.

## Decision required

Approve (or decline) limited use of LiteLLM's **ChatGPT/Codex subscription** and **GitHub
Copilot subscription** bridges to back Claude Code via the agent gateway.

Two separate decisions:
1. **Internal experiment** — a small pilot on individual developer machines.
2. **General team rollout** — broad distribution via the private marketplace.

## Owners

| Role | Owner | Decision date |
|------|-------|---------------|
| Runtime / maintainer | _TBD_ | |
| Security | _TBD_ | |
| Data-flow / privacy | _TBD_ | |
| Enterprise Copilot policy | _TBD_ | |

## What reviewers must weigh

- **Data flow.** Prompts and tool schemas are sent to the selected upstream (OpenAI for
  ChatGPT/Codex, GitHub for Copilot) under **that provider's account and data policy**, not
  Anthropic's. Each developer uses their own OAuth subscription tokens.
- **Anthropic support boundary.** Anthropic does **not** support routing Claude Code to
  non-Claude models. This is a compatibility- and policy-gated use of third-party LiteLLM
  bridges, not a supported configuration.
- **GitHub Copilot terms.** LiteLLM's Copilot adapter injects Copilot client headers.
  Enterprise Copilot policy and vendor terms must be reviewed before rollout.
- **Supply chain.** LiteLLM is pinned in `uv.lock`; `1.82.7`/`1.82.8` are forbidden
  (compromised). Upgrades require a reviewed change.

## Decision log

| Date | Decision | Scope | Notes |
|------|----------|-------|-------|
| _TBD_ | _pending_ | experiment | |
| _TBD_ | _pending_ | team rollout | |

If either provider is found non-compliant, that provider is disabled or the scope reduced —
the decision is recorded here, never hidden behind the other provider.
