---
name: gateway-doctor
description: >-
  Diagnose Agent Gateway problems. Use when the user reports that `agw claude`
  fails, the proxy will not start, a model is unavailable or unauthorized, a
  provider looks logged out, a port is in use, or they ask to "check/diagnose the
  gateway", "why is agw broken", or "gateway doctor". Runs `agw doctor` and
  interprets the results.
---

# Agent Gateway — doctor

Diagnose and explain gateway issues without ever revealing secrets.

## What to do

1. **Run diagnostics:**

   ```bash
   agw doctor
   ```

   It reports prerequisite versions, runtime paths and permissions, native Claude
   resolution, configured models, provider authentication state, an inspectable
   Claude `availableModels` conflict (if any), and managed-proxy state. It exits
   non-zero if it finds a blocking problem. Secrets are always redacted.

2. **Map each failing check to an action:**

   - *provider not authenticated / expired* → `agw auth chatgpt`.
   - *native claude not found* → install Claude Code, then `agw setup`.
   - *port in use by an unrelated process* → free the port or change `port` in
     `~/.config/agent-gateway/config.yaml`, then `agw proxy restart`.
   - *proxy running with a different config/version* → `agw proxy restart`
     (the only path that applies a changed fingerprint).
   - *availableModels excludes a configured model* → this is managed Claude policy
     the gateway cannot override; per-task routing to that model is unavailable
     until the policy allows it. Do not claim success via a fallback model.
   - *bad file permissions* → re-run `agw setup`, which restores 0700/0600.

3. **Inspect sanitized logs if needed:**

   ```bash
   agw status         # proxy identity + model→provider mapping
   agw logs --lines 100
   ```

   `agw logs` shows only sanitized operational lines (lifecycle, public model,
   provider, latency, error category) — never prompts, tool bodies, or secrets.

## Notes

- Never suggest editing managed Claude policy to force a model.
- If a model fails `agw models verify`, report it as unusable for Claude Code for
  that exact public name — the gateway never silently falls back to another model.
