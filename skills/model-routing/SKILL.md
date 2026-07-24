---
name: model-routing
description: >-
  Select which gateway model runs a task, subagent, or agent-team teammate. Use
  when the user names a model for a piece of work — e.g. "use gpt-5.6-sol for
  this", "spawn a review subagent on <model>", "have one teammate use <model-a>
  and another use <model-b>", or "switch this session to <model>". Ensures the
  exact configured model name is passed through unchanged, with no invented
  aliases and no silent substitution.
---

# Agent Gateway — model routing

When the user asks for a specific model, route to it using the **exact** public
name the gateway exposes. Never invent role aliases (no `team-review`,
`fast-model`, etc.) and never silently substitute a different model.

## Steps

1. **Get the exact names** the gateway serves:

   ```bash
   agw models list --json
   ```

   Use a `name` from that output verbatim. If the user's requested model is not
   listed, say so and stop — do not pick a "close" one. (`--all` also shows
   inactive candidates, which are not usable until activated in setup.)

2. **Apply the name where the work runs:**
   - **Main session:** `claude --model <name>`, or `/model <name>` inside a
     running session. Claude Code 2.1.218 shows one exact AGW custom-model entry
     in its picker; use direct exact selection for the other active names.
   - **Subagent:** pass `<name>` unchanged as that subagent's model.
   - **Agent-team teammate:** spawn each teammate with its requested `<name>`.

3. **Teammate models are fixed at spawn.** Changing the lead's `/model` does not
   change teammates that are already running; spawn a replacement to change one.

4. **Verify routing after spawn.** Correlate each spawned agent's id with the
   public name/provider actually used (via `agw status` / sanitized proxy
   evidence). If Claude's managed `availableModels` allowlist substituted a
   different model before the request reached the gateway, report that mismatch —
   do **not** present the fallback run as the requested-model run.

5. **Surface errors, don't paper over them.** If a model is unauthorized,
   unavailable, removed upstream, or tool-incompatible, report the failure under
   its exact public name and provider. The gateway never falls back to another
   model.

## Example requests this handles

```text
Spawn a review subagent using gpt-5.6-sol for this change.

Create two teammates: use gpt-5.6-sol for implementation and <other-model>
for review. Wait for both and summarize their results.
```
