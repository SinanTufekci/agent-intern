# agent-intern (Claude Code plugin)

This plugin registers the `agent-intern` MCP server and adds three slash commands:
`/agent-intern:second-opinion` (the current change reviewed by another model family),
`/agent-intern:image` (an image drawn by Gemini and saved into your project) and
`/agent-intern:doctor` (which backends are installed and signed in; spends no quota).

```bash
claude plugin marketplace add SinanTufekci/agent-intern
claude plugin install agent-intern@agent-intern
```

**What it runs.** The MCP server is the `agent-intern` package from PyPI, started with `uvx` at the
exact version this plugin release names. It runs the coding CLIs you have installed (Antigravity,
Codex, Copilot, Cursor, opencode, optionally others) as headless sub-agents in a project directory
you name. To move to a newer server, update the plugin:
`claude plugin update agent-intern@agent-intern`.

**What it sends.** Your prompt, and any file contents the sub-agent reads, go to that CLI's own
vendor under your own login (opencode's free models run on opencode's hosted service). The server
keeps no keys of its own: API keys stay with each CLI. Swarm answers too long to return inline are
saved under `~/.agent-intern/runs/`; runs older than 14 days are deleted when the next one is
saved, and you can delete the folder any time. The only network request the server makes itself is
asking PyPI whether a newer release exists; set `AGENT_INTERN_NO_UPDATE_CHECK=1` to turn it off.

**Security.** Every backend is an autonomous agent; use trusted prompts on trusted code. See the
[security notes](https://github.com/SinanTufekci/agent-intern/blob/main/docs/security.md).

Source, docs and license (MIT): https://github.com/SinanTufekci/agent-intern
