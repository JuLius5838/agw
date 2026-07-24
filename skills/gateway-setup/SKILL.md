---
name: gateway-setup
description: >-
  Install and set up the Agent Gateway so Claude Code can use the developer's
  personal ChatGPT/Codex subscription as its model backend.
  Use when the user says things like "set up the agent gateway", "install agw",
  "let me use my ChatGPT/Codex subscription in Claude Code", "use Codex as the
  backend". Runs a bundled bootstrap that installs the pinned `agw` runtime with
  uv, then runs `agw setup`.
---

# Agent Gateway — setup

Set up the local hybrid gateway. Native Claude requests retain Claude Code's saved
subscription; configured ChatGPT/Codex names route through a private per-developer
LiteLLM process.

## What to do

1. **Confirm prerequisites.** The developer needs `uv` installed and the native
   Claude Code CLI on PATH. If `uv` is missing, point them to
   https://docs.astral.sh/uv/ and stop.

2. **Run the bundled bootstrap.** It performs an ownership-aware preflight (it
   refuses to overwrite an unrelated `agw`), installs/upgrades the pinned runtime
   as a `uv` tool, and runs `agw setup`:

   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap.sh"
   ```

   `agw setup` preserves user-edited registries. When an upgrade retires a
   provider entry, setup first saves a one-time permission-restricted rollback
   copy beside `models.yaml`. It enables the marked, reversible bare-`claude`
   integration by default when the shell is detectable; pass `--no-shell` to
   opt out.

3. **Enable the requested exact route.** Candidates ship disabled so installation
   remains native-only until the developer makes a provider choice. Inspect them,
   then enable the requested model/provider pair:

   ```bash
   agw models list --all
   agw setup --provider-owner gpt-5.6-sol=chatgpt
   ```

   Use the exact name/provider requested by the developer; never silently pick a
   candidate or enable every provider.

4. **Authenticate that provider** (interactive device flow — needs a terminal):

   ```bash
   agw auth chatgpt     # ChatGPT / Codex subscription
   ```

   The short-lived verification URL/code appears only on the terminal. Never copy
   it into logs or a message.

5. **Launch Claude through the gateway:**

   ```bash
   claude                            # native Claude default, gateway available
   claude --model gpt-5.6-sol        # a specific external model
   ```

   `command claude` always bypasses the gateway and runs native Claude Code.

6. **Shell control:** `agw shell enable` and `agw shell disable` explicitly manage
   the same marked block. `command claude` always bypasses it.

7. **Unified usage:** setup installs the standalone `/agw-usage` command. The same
   dashboard is available in a terminal with `agw usage`.

## Notes

- The proxy binds to `127.0.0.1` only and needs a locally generated key.
- Prompts and tool schemas are sent to the selected upstream provider under that
  provider's account and data policy. Say this plainly if the user asks.
- If something looks wrong, run the `gateway-doctor` skill or `agw doctor`.
