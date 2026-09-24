---
name: doctor
description: Check which agent-intern backends (Antigravity, Codex, Copilot, Cursor, opencode, Grok, Kimi) are installed, signed in and ready, without spending any quota. Use when setting up agent-intern, when a delegation fails, or when the user asks which sub-agents they can use.
allowed-tools:
  - mcp__plugin_agent-intern_intern__antigravity_status
  - mcp__plugin_agent-intern_intern__codex_status
  - mcp__plugin_agent-intern_intern__copilot_status
  - mcp__plugin_agent-intern_intern__cursor_status
  - mcp__plugin_agent-intern_intern__opencode_status
  - mcp__plugin_agent-intern_intern__grok_status
  - mcp__plugin_agent-intern_intern__kimi_status
---

Call every agent-intern status tool in parallel. None of them spends quota:

- `antigravity_status`
- `codex_status`
- `copilot_status`
- `cursor_status`
- `opencode_status`
- `grok_status`
- `kimi_status`

Report one table with a row per backend and these columns: Backend, Ready, Version, Next step.

- **Ready:**
  - ✅ installed and signed in
  - ⚠️ installed, but not signed in or misconfigured
  - ❌ CLI not found
- **Next step:** the one thing to do, taken from that backend's status output, such as the login
  command. Keep it under about 12 words, and leave it empty for a backend that's ready.

After the table:

- **A newer agent-intern release:** if any status output reports one, say so once and give the
  upgrade command it names.
- **No backend ready:** recommend opencode. `npm i -g opencode-ai` needs no account, and its free
  models answer with zero credentials, just slowly.
- **Grok and Kimi:** mark them experimental, because their answer path has never been verified. If
  the user has one of those subscriptions, point them to
  https://github.com/SinanTufekci/agent-intern/issues/new?template=backend_verification.yml.

Don't call any `*_ask`, `*_continue` or image tool from this skill.
