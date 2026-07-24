# Model Selection

## Principles

- **Names are real model names.** You and Claude always route with the exact public name
  (e.g. `gpt-5.6-sol`, `claude-opus-4-8`). Provider prefixes (`anthropic/`,
  `chatgpt/`) are private routing detail and never appear in prompts.
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

The front router sends native Claude names directly to Anthropic and sends only exact
external names to LiteLLM. It exposes discovery-compatible hidden `anthropic.agw.*` IDs
with configured picker labels for forward compatibility.

Claude Code 2.1.218 does not run gateway discovery without an
`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` gateway credential. Setting either would replace
the saved Claude subscription, so AGW intentionally leaves them unset. Instead, the
launcher places one exact external name—the configured external default, or otherwise the
first active name—in the visual picker using `ANTHROPIC_CUSTOM_MODEL_OPTION`. Every active
external name remains valid with direct `/model NAME`, `--model NAME`, or per-agent routing.

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
```

With this registry, all three external names are selectable, while `claude` still starts
with Claude Code's native model because `default_model` is `null`. Set `default_model` to
one enabled external name only if `claude` should start on that model by default.

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
agw models remove gpt-5.6-luna              # -p/--provider disambiguates a shared name
```

`add` never overwrites an existing name+provider, and `remove` clears `default_model` when
the removed name no longer resolves. Neither restarts the gateway — run `agw proxy restart`
to apply the change to a running session, and `agw models verify NAME` (after
`agw auth chatgpt`) to confirm a newly enabled model actually works.

To rename the picker entry, edit only `display_name` in
`~/.config/agent-gateway/models.yaml`, then start a new `claude` session. Keep `name` and
`upstream_model` unchanged because they are the routing contract.

## Selecting a model

| Where | How |
|-------|-----|
| Main session (start) | `claude --model gpt-5.6-sol` |
| Main session (switch) | `/model gpt-5.6-sol` inside Claude; one external name also appears in the picker |
| Default (no flag) | `claude` preserves Claude Code's native default when `default_model` is `null` |
| Subagent | ask Claude to use `<name>` as the subagent model |
| Agent-team teammate | assign `<name>` per teammate; fixed at spawn |

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
