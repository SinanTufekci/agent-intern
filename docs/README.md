# agent-intern docs

<sub>[← back to the README](../README.md)</sub>

The [README](../README.md) is the short version. These pages hold the detail.

| Page | What's in it |
|---|---|
| [Setup & requirements](setup.md) | Every install path (the Claude Code plugin, `claude mcp add`, uvx, from source), per-backend prerequisites, and the `*_BIN` overrides for CLIs that aren't reliably on `PATH`. |
| [Tool reference](tools.md) | All 29 tools, their arguments and defaults. |
| [Backends in depth](backends.md) | The backends side by side, how the bridge reads each CLI's answer, how `*_continue` finds the right session, model selection and auth, per-backend deep dives, and the three experimental backends. |
| [Watch mode & swarm](watch-and-swarm.md) | The live **Agent Intern** window, `agent_swarm`, and ready-made panels (`preset_swarm`: jury, research, red team, council). |
| [Security](security.md) | What each backend's `sandbox` really enforces, and what it doesn't. |
| [FAQ](faq.md) | Terms of service, cost, which backend to use when, getting Claude to *offer* delegation. |
| [Status & caveats](status.md) | Version-by-version compatibility notes: what changed upstream and what the bridge does about it. |

Release history lives in the [changelog](../CHANGELOG.md).
