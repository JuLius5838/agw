# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities **privately** to the security owner (see
`docs/policy-decision.md`) — do not open a public issue. Include a description, a
reproduction, and the affected version. You will get an acknowledgement within a
few business days and a remediation/rollback plan for confirmed issues.

## Supported versions

Only the latest tagged release is supported. Runtime and LiteLLM versions are
pinned in `uv.lock`; upgrades require a reviewed change (see `docs/security.md`).

## Security model (summary)

- The public AGW router and private LiteLLM child bind to `127.0.0.1` only. The
  router requires a locally generated key; LiteLLM uses a separate ephemeral
  internal key. Neither is an upstream billing credential.
- OAuth credentials and the proxy key are per developer, stored `0600` under
  `0700` directories, and never committed to Git.
- Provider token directories are scoped to authentication commands and the managed
  LiteLLM process; they are never added to Claude Code's child environment.
- Claude request-time credentials are forwarded only on the native Anthropic path
  and stripped before every external LiteLLM request.
- Operational logs persist only allowlisted, redacted lifecycle/error metadata.
  Request/response bodies and prompt/tool content are never logged.
- The gateway never falls back to a different model and never load-balances across
  subscriptions.

## Forbidden dependencies

LiteLLM `1.82.7` and `1.82.8` are forbidden (identified as compromised by
Anthropic's gateway documentation). This is enforced by tests and CI.
