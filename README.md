# Agent Gateway (`agw`)

[![CI](https://github.com/JuLius5838/agw/actions/workflows/ci.yml/badge.svg)](https://github.com/JuLius5838/agw/actions/workflows/ci.yml)
[![Security](https://github.com/JuLius5838/agw/actions/workflows/security.yml/badge.svg)](https://github.com/JuLius5838/agw/actions/workflows/security.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Route Claude Code through a local hybrid gateway: native Claude requests keep the saved
Claude subscription, while exact configured GPT names route to a private LiteLLM process
on your machine.

```text
                         ┌▶ Anthropic (native Claude Code OAuth)
claude ─▶ AGW front router ─┤
 /model switches inside Claude └▶ private LiteLLM ─▶ ChatGPT/Codex
```

You and Claude always name real models (for example `claude-opus-4-8`, `gpt-5.6-sol`);
provider prefixes such as `anthropic/` or `chatgpt/` remain private routing configuration.

AGW never mints, stores, or injects a second Anthropic token. Claude traffic is forwarded
with Claude Code's saved login; only external model selections enter LiteLLM.

## Install

AGW requires Claude Code, Python 3.12, and
[`uv`](https://docs.astral.sh/uv/). Add the public marketplace and install the plugin:

```bash
claude plugin marketplace add JuLius5838/agw
claude plugin install agent-gateway@agent-gateway
```

Restart Claude Code or run `/reload-plugins`, then invoke the bundled setup skill:

```text
/agent-gateway:gateway-setup
```

The skill runs the bundled bootstrap, which installs the pinned `agw` runtime with `uv`
and starts `agw setup`. To run the bootstrap directly from a local checkout instead:

```bash
git clone https://github.com/JuLius5838/agw.git
cd agw
bash ./scripts/bootstrap.sh
```

External routes ship disabled. Setup remains native-Claude-only until you explicitly enable
and authenticate an exact model/provider pair.

## Daily use

```bash
agw setup                         # install local state and configure external models
agw setup --provider-owner gpt-5.6-sol=chatgpt  # enable this exact external route
agw auth chatgpt                  # OAuth device flow for ChatGPT/Codex (needs a TTY)
claude                            # managed workflow after setup
agw claude                        # canonical/debug spelling
agw claude --model gpt-5.6-sol    # launch on a specific model
agw models list                   # show configured models
agw usage                         # Claude + Codex limits and activity in one view
agw doctor                        # diagnose problems (secrets redacted)
```

Inside a session: `/model gpt-5.6-sol` switches to Codex, `/model claude-opus-4-8` back,
and `/effort` controls either provider using Claude Code's native selector.
Set `default_effort: max` in `~/.config/agent-gateway/config.yaml` for an overridable
startup default; an explicit `--effort` or an in-session `/effort` still wins.
External candidates ship disabled so a fresh install stays native-only; enable only the
model/provider pairs approved for the repository and data you are working with.

Inside Claude Code, `/agw-usage` opens the same unified dashboard as a native interactive
dialog. A deterministic plugin hook invokes the local AGW MCP tool and blocks command
expansion before it reaches a model, so opening or refreshing the dashboard consumes no
Claude or external-model tokens. A parallel fail-closed guard blocks expansion even if
the MCP server is still connecting and uses a same-terminal fallback instead. AGW
installs the command registration as a
standalone user skill so it has the short command name instead of a plugin namespace.
Claude's documented five-hour/seven-day limits are captured locally after each response;
Codex limits, reset credits, and token activity are read live through Codex App Server
using the existing `agw auth chatgpt` credential. The credential bridge is capability-
and version-gated to the tested Codex CLI `0.145.x` series. `agw setup` also pins
that executable's canonical path and SHA-256 before AGW may share an OAuth token
with its App Server; rerun setup after a reviewed Codex upgrade. Native `/usage`
remains unchanged.

Claude Code 2.1.218 only performs multi-model gateway discovery when a gateway credential
replaces the saved Claude login. AGW does not make that tradeoff: it adds one exact external
name (the configured default, or first active name) to the visual picker through Claude's
supported custom-model option. Other active names remain selectable with `/model NAME`,
`claude --model NAME`, and per-agent model assignment.

```text
agw claude ...    # canonical: always uses the managed gateway
claude ...        # same, after setup or `agw shell enable`
command claude    # always bypasses the gateway → native Claude Code
```

Ask Claude in natural language to route work to a model:

```text
Spawn a review subagent using gpt-5.6-sol for this change.
Create two teammates: use gpt-5.6-sol for implementation and <other-model> for review.
```

## Safety model (summary)

- The proxy binds only to `127.0.0.1` and requires a locally generated key.
- OAuth credentials and the proxy key are per developer, `0600`, and never committed.
- The gateway never falls back to a different model or load-balances across subscriptions.
- The pinned LiteLLM runner routes ChatGPT through its Responses adapter so Claude's system
  prompt becomes `instructions`, not a rejected `system` message.
- Claude Code effort values reach GPT-5.6 as `reasoning.effort` without silently
  downgrading `xhigh` or `max`.
- Claude's hosted web-search declaration becomes Claude Code's client-side
  `WebSearch` function for ChatGPT subscription models, avoiding the unsupported
  `web_search_preview` backend type.
- Prompts/tool schemas go to the selected upstream under that provider's data policy.

See [`docs/`](docs/) for architecture, security/threat-model, operations, model selection,
the compatibility report, and the policy decision. Full command reference:
[`docs/operations.md`](docs/operations.md).

## Project status

AGW is an independent personal open-source project maintained by
[Julien Fresnel](https://github.com/JuLius5838). It is not affiliated with or endorsed by
Anthropic, OpenAI, GitHub, or LiteLLM. Anthropic does not support routing Claude Code to
non-Claude models; external-provider support is therefore compatibility-gated and may break
when upstream clients or provider adapters change.

The automated suite covers the offline routing and security contracts. Real-subscription
checks listed in [`docs/compatibility-report.md`](docs/compatibility-report.md) remain the
release gate for provider-backed claims. Use each provider only if its account terms and
data policy permit your intended workflow.

## Contributing and security

Contributions are welcome under the [Apache License 2.0](LICENSE). Read
[`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a pull request. Report suspected
vulnerabilities privately through [GitHub Security Advisories](https://github.com/JuLius5838/agw/security/advisories/new),
not a public issue; see [`SECURITY.md`](SECURITY.md).

## Development

```bash
uv sync --all-groups
uv run pytest -m "not live"   # offline unit, integration, and native-router contracts
uv run ruff check . && uv run mypy src tests
```

Requires Python 3.12, `uv`, and (for unified Codex usage) Codex CLI `0.145.x`. Live smoke
tests also require real provider subscriptions.
MVP supports macOS and Linux with zsh or bash.
