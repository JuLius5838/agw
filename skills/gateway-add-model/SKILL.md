---
name: gateway-add-model
description: >-
  Add or remove an external ChatGPT/Codex or GitHub Copilot model in Agent Gateway
  so it becomes selectable in Claude Code. Use when the user says things like "add
  gpt-5.6-luna", "enable a Copilot model", "make <model> available in the gateway",
  or "remove <model>". Uses the validated `agw models add` / `agw models remove`
  commands and never hand-edits the routing registry or invents model ids.
---

# Agent Gateway — add or remove a model

External models live in the validated registry `~/.config/agent-gateway/models.yaml`.
Use the `agw models` commands to change it; never edit that file by hand and never
guess an upstream model id. A public name maps to exactly one active provider, and
the gateway never falls back to a different model.

## Add a model

1. **Get the exact model id.** It must be the provider's real slug — never invented.
   - See what is already configured (active and inactive candidates):

     ```bash
     agw models list --all
     ```
   - For ChatGPT/Codex, obtain the exact slug from the user or the Codex CLI's model
     list. For Copilot, use an authoritative current Copilot/LiteLLM catalog or an
     exact slug supplied by the user. If you cannot confirm the exact id, stop and
     ask — do not guess. In particular, LiteLLM 1.93.0 has no cataloged Kimi slug;
     a product display name alone is not a routable id.

2. **Add it.** For a ChatGPT/Codex model the provider, upstream id, and mode default
   correctly, so the common case is just the exact name:

   ```bash
   agw models add gpt-5.6-luna
   ```

   For a verified Copilot slug, make the provider and published mode explicit. The
   upstream prefix defaults to `github_copilot/` when `-p copilot` is selected:

   ```bash
   agw models add gpt-4.1 --provider copilot --mode chat
   ```

   Options when needed:
   - `--display-name "GPT 5.6 Luna"` — picker label only; never changes routing.
   - `--no-enable` — add as a disabled candidate instead of activating it.
   - `--default` — also make it the startup model (implies enable).
   - `--provider`, `--upstream-model`, `--mode` — required when the provider, slug,
     or API mode differs from the ChatGPT defaults. Never infer mode from provider
     alone; use the exact model's published/cataloged mode.

   The command validates the whole registry before writing; an invalid entry
   (bad name, wrong prefix, duplicate active name) fails without changing the file.

3. **Authenticate the provider** if it is not already:

   ```bash
   agw auth chatgpt     # ChatGPT/Codex
   agw auth copilot    # GitHub Copilot
   ```

   For Copilot, `agw auth copilot` also verifies the account has an active Copilot
   subscription and reports plainly if it has lapsed (the credential is still
   stored for when the plan is reactivated).

4. **Verify Claude compatibility.** A model that fails here is unusable under that
   exact name — there is no fallback:

   ```bash
   agw models verify gpt-5.6-luna
   ```

5. **Apply to a running session.** A running gateway keeps its old registry until
   restarted:

   ```bash
   agw proxy restart
   ```

   Then select it with `/model gpt-5.6-luna`, `claude --model gpt-5.6-luna`, or
   per-agent routing (see the `model-routing` skill).

## Remove a model

```bash
agw models remove gpt-5.6-luna
```

If the same public name has candidates for more than one provider, disambiguate:

```bash
agw models remove gpt-5.6-luna --provider chatgpt
```

Removing the current `default_model` clears it back to native Claude selection when
no active entry with that name remains. Run `agw proxy restart` to apply the change
to a running gateway.

## Rules

- Exact names only: `name` has no provider prefix; `upstream_model` keeps it.
- One active provider per public name; the gateway never load-balances or falls back.
- Never invent a model id. If the exact slug is unknown, stop and ask.
- Enabling a model does not authenticate it or start the proxy — auth and verify
  before relying on it.
