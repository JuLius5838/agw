# Model Selection

## Principles

- **Names are real model names.** You and Claude always route with the exact public name
  (e.g. `gpt-5.6-sol`, `claude-opus-4-8`). Provider prefixes (`anthropic/`,
  `chatgpt/`, `github_copilot/`) are private routing detail and never appear in prompts.
- **Picker labels are cosmetic.** An optional `display_name` can make the visual picker
  friendlier without becoming a routable alias.
- **One provider per name per machine.** A public name maps to exactly one active provider.
  The gateway never load-balances or falls back.
- **No role aliases.** There is no `team-review` / `fast-model`. If you want a specific
  model, name it.

## Native Claude plus optional external backends

Claude is not a LiteLLM provider. It stays on Claude Code's native subscription. The
registry contains only optional external routes:

| provider | prefix | subscription auth |
|----------|--------|-------------------|
| `chatgpt` | `chatgpt/` | `agw auth chatgpt` (OAuth device flow) |
| `copilot` | `github_copilot/` | `agw auth copilot` (OAuth device flow) |

The front router sends native Claude names directly to Anthropic and sends only exact
external names to LiteLLM. It exposes discovery-compatible hidden `anthropic.agw.*` IDs
with configured picker labels for forward compatibility.

Claude Code 2.1.218 does not run gateway discovery without an
`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` gateway credential. Setting either would replace
the saved Claude subscription, so AGW intentionally leaves them unset. Instead, the
launcher places one exact external name—the configured external default, or otherwise the
first active name—in the visual picker using `ANTHROPIC_CUSTOM_MODEL_OPTION`. Every active
external name remains valid with direct `/model NAME` or `--model NAME`, and a verified
name is additionally available as a generated subagent type (see "Selecting a model").

## The registry (`~/.config/agent-gateway/models.yaml`)

```yaml
default_model: null  # preserve Claude Code's native startup model

models:
  - name: gpt-5.6-sol
    display_name: Codex Sol  # optional; picker label only
    provider: chatgpt
    upstream_model: chatgpt/gpt-5.6-sol
    mode: responses
    enabled: true

  - name: gpt-5.6-terra
    display_name: GPT 5.6 Terra
    provider: chatgpt
    upstream_model: chatgpt/gpt-5.6-terra
    mode: responses
    enabled: true

  - name: gpt-5.6-luna
    display_name: GPT 5.6 Luna
    provider: chatgpt
    upstream_model: chatgpt/gpt-5.6-luna
    mode: responses
    enabled: true

  - name: gpt-4.1
    display_name: GPT 4.1 (Copilot)
    provider: copilot
    upstream_model: github_copilot/gpt-4.1
    mode: chat
    enabled: false
```

With this registry, the three enabled ChatGPT names are selectable and the Copilot
candidate remains unavailable, while `claude` still starts with Claude Code's native model
because `default_model` is `null`. Set `default_model` to one enabled external name only if
`claude` should start on that model by default.

Validation enforces: exact-name (no `/`), provider-prefix match, valid mode, one active
entry per name, and—when set—that `default_model` resolves to an active external entry.
`agw setup` installs the packaged registry on first run and preserves local edits.
Packaged candidates therefore appear automatically for new installations only. Existing
users add a new exact-name entry to their local `models.yaml`; AGW intentionally does not
merge candidates into a user-edited registry during an upgrade. If an upgrade retires a
provider entry, it first saves the original registry as
`models.pre-retired-providers.yaml` with mode `0600`, then migrates `models.yaml`.

`enabled` controls whether an external model is available to route. The top-level
`default_model` does not add a model; it only selects the startup model and must name an
enabled entry. Leave it `null` to preserve Claude Code's native startup selection.

## Adding and removing models

Prefer the validated commands over hand-editing `models.yaml`. They re-validate the whole
registry before writing, so a bad entry fails without changing the file:

```bash
agw models add gpt-5.6-luna                 # provider chatgpt, upstream chatgpt/gpt-5.6-luna, enabled
agw models add gpt-5.6-luna --no-enable     # add as a disabled candidate
agw models add gpt-5.6-luna --default       # also make it the startup model (implies enable)
agw models add my-model -u chatgpt/actual-slug --display-name "My Model"
agw models add gpt-4.1 -p copilot -m chat   # upstream defaults to github_copilot/gpt-4.1
agw models remove gpt-5.6-luna              # -p/--provider disambiguates a shared name
```

The default `--mode responses` is appropriate for ChatGPT/Codex models but is not inferred
from a provider catalog. Pass the mode published for the exact Copilot slug (`chat` for the
packaged `gpt-4.1` candidate). Never invent a Copilot slug: LiteLLM 1.93.0's pinned catalog
contains `github_copilot/gpt-4.1`, but it does not contain a Kimi model ID, so Kimi must not
be added until an exact slug is obtained and live-tested.

`add` never overwrites an existing name+provider, and `remove` clears `default_model` when
the removed name no longer resolves. Neither restarts the gateway — run `agw proxy restart`
to apply the change to a running session, and `agw models verify NAME` (after authenticating
the selected provider) to confirm a newly enabled model actually works.

To rename the picker entry, edit only `display_name` in
`~/.config/agent-gateway/models.yaml`, then start a new `claude` session. Keep `name` and
`upstream_model` unchanged because they are the routing contract.

## Selecting a model

| Where | How |
|-------|-----|
| Main session (start) | `claude --model gpt-5.6-sol` |
| Main session (switch) | `/model gpt-5.6-sol` inside Claude; one external name also appears in the picker |
| Default (no flag) | `claude` preserves Claude Code's native default when `default_model` is `null` |
| Subagent | select the generated `agw-…` type by `subagent_type`; never pass `<name>` to the Agent tool's `model` field |
| Agent-team teammate | not supported; AGW makes no agent-team claim in v1 |

### External models as subagents

The Agent tool's explicit `model` argument only accepts `sonnet`, `opus`, `haiku`, and
`fable`; an external name there is rejected by the client before AGW sees it. A Claude Code
*custom-agent definition*, however, carries its own `model` field, so AGW generates one
definition per verified external model and injects them at launch with `--agents`:

```text
gpt-5.6-sol -> agw-gpt-5-6-sol-66127d2195a1
```

Claude selects the type and omits the model override. It may run several external types
concurrently and synthesize their results.

Because Anthropic does not support routing Claude Code to non-Claude models through a
gateway, and Claude Code may silently replace a definition-level model it rejects, a type
is advertised only from a successful, current probe:

```bash
agw models verify gpt-5.6-sol --claude-subagent
```

That command makes bounded real Claude and external-provider calls, so it never runs
automatically from setup, `doctor`, or launch. Evidence is bound to the exact Claude
executable, version, model, provider, and AGW routing fingerprint; upgrading Claude or
changing routing makes it stale and the type disappears until re-verified. A missing type
always means inactive, policy-excluded, unverified, failed, stale, or colliding — never a
silent fallback to another model. `agw doctor` reports which.

Generated agents are analysis-only: exactly `Read`, `Grep`, and `Glob`, with no shell,
editing, recursion, web, or MCP access. Any repository content such an agent reads is sent
to that external provider as model input.

AGW does not claim per-spawn enforcement in ordinary sessions: the gate proves the behavior
for a specific Claude build under a controlled probe, and a later Claude regression is
surfaced by re-verifying, not caught retroactively on an unobserved spawn.

## Reasoning effort

Claude Code's native `/effort` selector also controls ChatGPT/Codex models through AGW.
The selected value is translated to the Responses API `reasoning.effort` field:

| Claude Code | GPT-5.6 |
|-------------|---------|
| `low` | `low` |
| `medium` | `medium` |
| `high` | `high` |
| `xhigh` | `xhigh` |
| `max` | `max` |
| `ultracode` | `xhigh` plus Claude Code's workflow orchestration |

The mapping is session-native: use `/effort` or the `+`/`-` controls exactly as with a
Claude model. AGW does not introduce a second effort command.

An optional `default_effort` in `~/.config/agent-gateway/config.yaml` sets the startup
level without locking the session:

```yaml
default_effort: max
```

AGW translates this setting to a launch-time `--effort` flag only when the invocation
does not already contain one. Therefore `claude --effort low` takes precedence and
`/effort` remains changeable after startup. This differs intentionally from
`CLAUDE_CODE_EFFORT_LEVEL`, which Claude Code treats as a hard override.

The `model-routing` skill guides Claude to pull exact names from `agw models list --json`,
pass them through unchanged, and report any managed-`availableModels` substitution instead
of masking it.

## Verifying a model

```bash
agw models verify gpt-5.6-sol
```

Runs bounded checks through the proxy's Anthropic endpoint (token counting, required
headers, system content, clean SSE, tool-use/tool-result). A model that fails is unusable
for Claude Code under that exact name — no fallback.

## Agent teams

Agent teams are an **experimental** Claude Code feature, opt-in during `agw setup`
(`--agent-teams`). A teammate's model is fixed when it spawns; changing the lead's `/model`
does not change existing teammates.
