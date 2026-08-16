---
name: model-routing
description: >-
  Select which gateway model runs a task or subagent. Use when the user names a
  model for a piece of work — e.g. "use gpt-5.6-sol for this", "spawn a review
  subagent on <model>", "get a second opinion from another model", or "switch
  this session to <model>". Ensures the exact configured model name is passed
  through unchanged, with no invented aliases and no silent substitution.
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
     running session. Claude Code shows one exact AGW custom-model entry in its
     picker; use direct exact selection for the other active names.
   - **Subagent:** select the generated agent type whose definition carries that
     model, and pass **only** `subagent_type`. Generated types are named
     `agw-<sanitized-model>-<digest>`, e.g. `agw-gpt-5-6-sol-66127d2195a1`.

3. **Never put an external name in the Agent tool's `model` field.** That
   argument is validated against `sonnet|opus|haiku|fable` and an external name
   is rejected by the client before the gateway ever sees it. The external model
   comes from the generated agent's own definition; the invocation must omit the
   override entirely.

4. **A missing generated type is a status to report, not a fallback.** If no
   `agw-…` type exists for the requested model, the model is inactive,
   policy-excluded, unverified, failed, stale, or colliding with a user-defined
   agent id. Say which by running `agw doctor`, and offer:

   ```bash
   agw models verify <name> --claude-subagent
   ```

   That command makes real Claude and provider model calls, so ask before
   running it. Never route the work to a different model instead.

5. **Multiple models are fine when they add value.** Several generated types can
   run concurrently in one turn; compare their results and say plainly where they
   disagree. Do not fan out to spend quota or duplicate identical work.

6. **Generated agents are analysis-only.** They hold exactly `Read`, `Grep`, and
   `Glob` — no shell, editing, recursion, web, or MCP. Give one a task it can
   actually do with those tools, and remember that any repository content it
   reads is sent to that external provider.

7. **Surface errors, don't paper over them.** If a model is unauthorized,
   unavailable, removed upstream, or tool-incompatible, report the failure under
   its exact public name and provider. The gateway never falls back to another
   model. If Claude warns about a model restriction or substitution, report that
   mismatch rather than presenting the fallback run as the requested-model run.

## Example requests this handles

```text
Use gpt-5.6-sol for this session.

Review this change with several available models and synthesize the findings.

Get an independent read on this design from gpt-5.6-sol.
```
