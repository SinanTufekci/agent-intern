---
name: stats
description: Show how agent-intern's delegation tools have been used, from a local file that holds numbers only. Calls per backend, errors, average time, backend health, recent swarms, tokens where a backend reports them and Antigravity's remaining quota. Use when the user asks how much they delegate, which backend fails, or what is left of the Antigravity quota.
argument-hint: "[days, 1 to 90 — defaults to 7] [--page to open the chart window]"
---

Show agent-intern usage statistics: $ARGUMENTS

Call `agent_stats` with `days` set to the number in the arguments (7 if there is none). Add
`watch=true` only if the user asked for the page or the charts; it opens a local window on
127.0.0.1. The tool spends no quota.

Then summarise in a few lines, not the whole table again:

- Which backends carry the work, and which have a high error rate or a recent error. Name the
  category (`timeout`, `quota`, `auth`, `not_installed`, `exit_error`, ...): the file never holds raw
  error text, so say so if the user wants the cause, and point to the backend's `*_status` tool.
- If a swarm saved long answers, say they are under `~/.agent-intern/runs/`.
- The Antigravity quota section appears only when agy is enabled and can be read.

Be straight about what the numbers are. Tokens show only for backends that report them (`n/a`
otherwise), and error categories are best effort. If the tool says recording is off, report that
line as it is: either the user set `AGENT_INTERN_NO_STATS=1` or this server could not set recording
up, and an empty table then means nothing.
