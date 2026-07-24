# Contributing to Agent Gateway

Thanks for helping improve Agent Gateway (AGW). This is a maintainer-led personal
open-source project. Bug reports, focused feature proposals, documentation improvements,
and pull requests are welcome.

## Before contributing

- Read `README.md`, `docs/architecture.md`, and `docs/security.md`.
- Search existing issues and pull requests before starting overlapping work.
- Open an issue before a large architectural, provider, authentication, or dependency
  change so the approach can be discussed first.
- Never post credentials, OAuth device codes, proxy keys, prompts, private repository
  content, or raw provider request logs. Follow `SECURITY.md` for vulnerabilities.

## Development setup

AGW requires Python 3.12 and `uv`.

```bash
uv sync --all-groups
```

External provider credentials are not required for the default test suite. Tests marked
`live` are intentionally excluded from normal development and CI.

## Project invariants

Contributions must preserve these properties:

- Native Claude credentials never enter LiteLLM or an external-provider request.
- External-provider credentials never enter the Claude Code child or router process.
- The router and LiteLLM child remain loopback-only and authenticated.
- Public model names are exact; there is no silent aliasing, fallback, substitution, or
  load balancing across subscriptions.
- Operational logs do not persist prompts, tool bodies, OAuth codes, or credentials.
- Provider data flow stays explicit: selected context is governed by that provider's
  account and data policy.
- LiteLLM `1.82.7` and `1.82.8` remain forbidden. LiteLLM upgrades require review and
  compatibility testing.

## Make a change

1. Fork the repository and create a focused branch.
2. Follow the surrounding code's naming, typing, and documentation conventions.
3. Add or update tests for behavior changes.
4. Update documentation and `CHANGELOG.md` when user-visible behavior changes.
5. Keep the runtime version in `src/agent_gateway/__init__.py` aligned with
   `.claude-plugin/plugin.json` when preparing a release.
6. Open a pull request and complete the repository template.

By submitting a contribution, you agree that it is licensed under the Apache License 2.0,
as described in `LICENSE`.

## Verification

Run the same offline gates used by GitHub Actions:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest -m "not live" --cov=agent_gateway --cov-report=term-missing
shellcheck scripts/bootstrap.sh
claude plugin validate --strict .
gitleaks detect --source . --no-git --config .gitleaks.toml --redact --verbose
```

Provider-backed live checks require the contributor's own subscriptions and explicit
local setup. Do not place provider credentials in fixtures, CI, issues, or pull requests.

## Review and merging

The maintainer reviews all pull requests. Passing CI is necessary but does not guarantee
merge: changes must also fit the project's security model, compatibility scope, and
maintenance budget. The maintainer may close proposals that add excessive operational or
supply-chain complexity relative to their user value.
