# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/JuLius5838/agw/security/advisories/new).
Do not open a public issue and do not include credentials, OAuth codes, prompts, private
repository content, or unredacted logs in the report.

Include a description, the affected version, impact, and the smallest safe reproduction.
The maintainer aims to acknowledge reports within five business days and will coordinate a
remediation and disclosure timeline for confirmed issues. Please do not publicly disclose a
vulnerability before a fix or coordinated disclosure date is available.

## Supported versions

Only the latest tagged release is supported. Runtime and LiteLLM versions are
pinned in `uv.lock`; upgrades require a reviewed change (see `docs/security.md`).

## Security model (summary)

- The public AGW router and private LiteLLM child bind to `127.0.0.1` only. The
  router requires a locally generated key; LiteLLM uses a separate ephemeral
  internal key. Neither is an upstream billing credential.
- OAuth credentials and the proxy key are per developer, stored `0600` under
  `0700` directories, and never committed to Git.
- ChatGPT and GitHub Copilot use separate provider token directories. Those directories are
  scoped to authentication commands and the managed LiteLLM process; they are never added
  to Claude Code's child environment.
- Claude request-time credentials are forwarded only on the native Anthropic path
  and stripped before every external LiteLLM request.
- Operational logs persist only allowlisted, redacted lifecycle/error metadata.
  Request/response bodies and prompt/tool content are never logged.
- The gateway never falls back to a different model and never load-balances across
  subscriptions.

## Forbidden dependencies

LiteLLM `1.82.7` and `1.82.8` are forbidden (identified as compromised by
Anthropic's gateway documentation). This is enforced by tests and CI.
