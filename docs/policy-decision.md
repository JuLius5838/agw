# Provider Policy Decision Record

> **Status:** DRAFT — each user or adopting organization must review provider terms and
> data-flow policy before enabling an external route. This file must not contain vendor
> credentials or copied confidential terms.

AGW is a personal open-source project. Publishing the source or plugin does not approve the
use of any external subscription bridge for a particular person or organization.

## Decision required

Approve (or decline) use of LiteLLM's **ChatGPT/Codex subscription** and **GitHub Copilot
subscription** bridges to back Claude Code via Agent Gateway.

Decisions are provider-specific and deployment-specific:

1. **Individual use** — the user confirms their account terms and intended data flow.
2. **Organizational use** — the adopting organization names its own security, privacy, and
   provider-policy owners before enabling routes for company repository content.

## Project ownership

| Role | Owner |
|------|-------|
| Project maintainer | [Julien Fresnel](https://github.com/JuLius5838) |
| Adopter security/privacy/provider policy | The individual user or adopting organization |

## What reviewers must weigh

- **Data flow.** Prompts and tool schemas are sent to the selected upstream (OpenAI for
  ChatGPT/Codex, GitHub for Copilot) under **that provider's account and data policy**, not
  Anthropic's. Each developer uses their own OAuth subscription tokens.
- **Anthropic support boundary.** Anthropic does **not** support routing Claude Code to
  non-Claude models. This is a compatibility- and policy-gated use of third-party LiteLLM
  bridges, not a supported configuration.
- **GitHub Copilot terms.** LiteLLM's Copilot adapter injects Copilot client headers.
  The applicable individual or organizational Copilot policy and vendor terms must be
  reviewed before enabling that route.
- **Supply chain.** LiteLLM is pinned in `uv.lock`; `1.82.7`/`1.82.8` are forbidden
  (compromised). Upgrades require a reviewed change.

## Adopter decision template

Copy this table into the adopting repository or organization's policy system; do not commit
private vendor terms or internal approval evidence to AGW's public repository.

| Date | Provider | Decision | Scope | Owner | Notes/reference |
|------|----------|----------|-------|-------|-----------------|
| _TBD_ | ChatGPT/Codex | _pending_ | individual or organization | _TBD_ | |
| _TBD_ | GitHub Copilot | _pending_ | individual or organization | _TBD_ | |

If either provider is found non-compliant, disable that provider or reduce the approved
scope. AGW's no-fallback rule prevents that decision from being hidden behind another
provider.
