## What changed

<!-- Describe the change and why it is needed. -->

## Verification

<!-- List the commands or checks you ran. -->

- [ ] `uv run ruff format --check .`
- [ ] `uv run ruff check .`
- [ ] `uv run mypy src tests`
- [ ] `uv run pytest -m "not live"`
- [ ] `shellcheck scripts/bootstrap.sh`
- [ ] `claude plugin validate --strict .`

## Security and compatibility

- [ ] I did not add credentials, OAuth codes, prompt bodies, or sensitive logs.
- [ ] Model/provider routing remains exact and does not introduce fallback or substitution.
- [ ] Provider data-flow or credential-boundary changes are documented.
- [ ] Runtime, LiteLLM, Claude Code, or Codex compatibility changes are documented.

## Checklist

- [ ] I added or updated tests where behavior changed.
- [ ] I updated documentation and `CHANGELOG.md` where appropriate.
- [ ] I kept the plugin and runtime versions aligned when preparing a release.
