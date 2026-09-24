# Setup & requirements

<sub>[← back to the README](../README.md) · [all docs](README.md)</sub>

Every install path, per-backend prerequisites, and the `*_BIN` overrides for CLIs that aren't reliably on `PATH`.

## Set up

**Prerequisites — install whichever backend(s) you want, and sign in once each:**

- **Antigravity:** install `agy` and sign in to Antigravity once (via the IDE or `agy -i`).
- **Codex:** install `codex` and run `codex login` once (ChatGPT account or API key).
- **Copilot:** install `copilot` (`npm i -g @github/copilot`, or `winget install GitHub.Copilot`)
  and run `copilot` then `/login` once (or set a `COPILOT_GITHUB_TOKEN`/`GH_TOKEN` env var).
- **Cursor:** install `cursor-agent` (`curl https://cursor.com/install -fsSL | bash`) and run
  `cursor-agent login` once (or set a `CURSOR_API_KEY` env var).
- **opencode:** install `opencode` (`npm i -g opencode-ai`). **That's it** — its free `opencode/*`
  models answer with no account. Run `opencode auth login` only if you want to point it at your own
  Anthropic/OpenAI/… key.

You don't need all eight — the tools for a missing CLI simply report "not found" via their `*_status`
tool. If you have no subscriptions at all, start with opencode.

### Recommended — the Claude Code plugin

This repo is also a Claude Code plugin marketplace. The plugin registers the same server (through
`uvx agent-intern`, so updates work exactly as described in the next section) and adds three
skills: `/agent-intern:second-opinion`, `/agent-intern:image` and `/agent-intern:doctor`.

```bash
claude plugin marketplace add SinanTufekci/agent-intern
claude plugin install agent-intern@agent-intern
```

Inside a session, `/plugin marketplace add SinanTufekci/agent-intern` then
`/plugin install agent-intern@agent-intern` does the same. The plugin's tools are namespaced
`mcp__plugin_agent-intern_intern__*`. Use the plugin **or** one of the registrations below, not
both, since both would give Claude every tool twice. When a new plugin version ships,
`/plugin update agent-intern@agent-intern` picks up the new skills, and the server itself upgrades
the same way the uvx install does.

### No plugin — register the server yourself, you control updates

With [`uv`](https://docs.astral.sh/uv/) installed, register the bridge straight from
[PyPI](https://pypi.org/project/agent-intern/) — no path to hardcode, no `git pull` to remember:

```bash
claude mcp add -s user agent-intern -- uvx agent-intern
```

That writes the same entry you would otherwise add by hand under `mcpServers` in `~/.claude.json`:

```json
"agent-intern": {
  "command": "uvx",
  "args": ["agent-intern"]
}
```

uvx pins to the version it first caches and does **not** auto-upgrade, so you never run an update you
didn't choose — important, since the bridge runs [unsandboxed code](security.md#security): a surprise (or
compromised) release can't execute until you opt in. You still get told when there's something to
opt into: any `*_status` call reports whether a newer release is out, and Claude is instructed to
pass that on when it sees it. (There's a startup check too, but it writes to stderr — that only
reaches your MCP logs, not you.) Upgrade deliberately and restart Claude Code:

```bash
uvx agent-intern@latest      # fetch + run the newest release (refreshes uv's cache)
```

> [!TIP]
> Prefer hands-off auto-updates? Put `"args": ["agent-intern@latest"]` in the config instead —
> every launch runs the newest release. Convenient, but it pulls new code without asking each time.

### From source

Clone it instead if you want to hack on the bridge or pin a local copy:

```bash
git clone https://github.com/SinanTufekci/agent-intern.git
cd agent-intern
pip install fastmcp
python test_smoke.py        # 4 real round-trips (ask, continue, image, swarm) — prints four PASS lines
```

> [!NOTE]
> The smoke test costs a tiny bit of quota and takes ~30–60 s. It exercises the Antigravity path.

Then point Claude Code at the absolute path to `server.py` under `mcpServers` in `~/.claude.json`:

<table>
<tr><th>Windows</th><th>macOS / Linux</th></tr>
<tr><td>

```json
"agent-intern": {
  "command": "python",
  "args": ["C:\\path\\to\\server.py"]
}
```

</td><td>

```json
"agent-intern": {
  "command": "python3",
  "args": ["/path/to/server.py"]
}
```

</td></tr>
</table>

Restart Claude Code. **Twenty-seven tools** appear, each prefixed `mcp__agent-intern__`:

- **Antigravity (5):** `antigravity_ask`, `antigravity_continue`, `antigravity_image`,
  `antigravity_image_swarm`, `antigravity_status`
- **Codex (3):** `codex_ask`, `codex_continue`, `codex_status`
- **Copilot (3):** `copilot_ask`, `copilot_continue`, `copilot_status`
- **Cursor (3):** `cursor_ask`, `cursor_continue`, `cursor_status`
- **Grok (3, experimental):** `grok_ask`, `grok_continue`, `grok_status`
- **opencode (3):** `opencode_ask`, `opencode_continue`, `opencode_status`
- **Muse (3, experimental):** `muse_ask`, `muse_continue`, `muse_status`
- **Kimi (3, experimental):** `kimi_ask`, `kimi_continue`, `kimi_status`
- **Shared (1):** `agent_swarm` — fans a list of tasks out across **seven** backends in one run
  (everything but Kimi)

The single-prompt tools — Antigravity, Codex, Copilot, Cursor, **and** Grok — take a **`watch=true`**
flag for the live browser view ([Watch mode](watch-and-swarm.md#watch-mode)). Kimi has no watch mode yet.

> [!NOTE]
> **Your client learns how to use the bridge on its own.** The server ships MCP *instructions* — a
> short routing guide (when to reach for each tool, which backend to pick, and to pass `workspace` so
> the sub-agent has repo context) that a client like Claude Code injects into the model's context on
> connect, as an "MCP Server Instructions" block. So the host model knows how and when to drive these
> tools without you explaining them — you can just ask for the result.

> *"Use antigravity_ask to summarize the README of this repo in three bullets."* → Claude routes the
> prompt through the bridge, agy reads the file under the workspace root, and the answer comes back
> as a plain string. Swap in `codex_ask`, `copilot_ask`, or `cursor_ask` to have GPT, Copilot, or Cursor
do the same.

## Requirements

- Python 3.10+
- **For the Antigravity tools:** [`agy`](https://antigravity.google/) 1.0.0+ on `PATH` (state-file layout re-verified on **1.0.15**; behaviour re-verified on **1.1.25**) and an active Antigravity / AI Pro session
- **For the Codex tools:** [`codex`](https://developers.openai.com/codex/) on `PATH` and logged in (`codex login`) — verified on **codex-cli 0.149.1** (note its [Windows sandbox caveat](security.md#security))
- **For the Copilot tools:** [`copilot`](https://docs.github.com/en/copilot/how-tos/copilot-cli) on `PATH` and logged in (`copilot` → `/login`, or a `COPILOT_GITHUB_TOKEN`/`GH_TOKEN` env) — verified on **copilot 1.0.80**
- **For the Cursor tools:** [`cursor-agent`](https://cursor.com/cli) on `PATH` and logged in (`cursor-agent login`, or a `CURSOR_API_KEY` env) — verified on **cursor-agent 2026.07.23**
- **For the opencode tools:** [`opencode`](https://opencode.ai/) on `PATH` (`npm i -g opencode-ai`) — **no login needed** for its free `opencode/*` models; `opencode auth login` (or a provider env var) for everything else — verified end-to-end on **opencode 1.18.29**
- **For the Grok tools (experimental):** [`grok`](https://docs.x.ai/build/overview) on `PATH` and logged in (`grok login`, or an `XAI_API_KEY` env) plus a SuperGrok / X Premium+ subscription — flag surface verified on **grok 1.0.3**, [answer path unverified](backends.md#experimental-backends)
- **For the Muse tools (experimental):** [`muse`](https://dev.meta.ai/docs/muse-code) installed and logged in (`muse login`, or a `META_API_KEY` env) with a Muse Code plan — the whole exec pipeline verified on **Muse Code 1.3.0** through its offline echo provider, [real model unverified](backends.md#experimental-backends)
- **For the Kimi tools (experimental):** [`kimi`](https://github.com/MoonshotAI/kimi-code) on `PATH` and logged in (`kimi login`, or an API key in `~/.kimi-code/config.toml`) — flag surface verified on **kimi 0.29.1**, [answer path unverified](backends.md#experimental-backends)

Each backend is independent — install only the CLI(s) you plan to use; the other tools simply report "not found" via their `*_status` tool.

> [!TIP]
> If `agy` isn't reliably on `PATH` (e.g. a new terminal or reboot drops it on Windows), set the
> **`AGY_BIN`** env var to its full path and the bridge will use that instead of `"agy"` — e.g.
> `AGY_BIN=%LOCALAPPDATA%\agy\bin\agy.exe`. Likewise, set **`CODEX_BIN`** if `codex` isn't reliably on
> `PATH` (the native Windows installer puts it under `%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\`), and
> **`COPILOT_BIN`** if `copilot` isn't (the winget install lands under
> `%LOCALAPPDATA%\Microsoft\WinGet\Packages\GitHub.Copilot_*\copilot.exe`). Finally, set
> **`CURSOR_BIN`** if `cursor-agent` isn't reliably on `PATH` (the installer drops a `cursor-agent.CMD`
> shim a bare name can't launch on Windows), and **`OPENCODE_BIN`** if `opencode` isn't (npm drops an
> `opencode.CMD` shim with exactly the same problem). **`GROK_BIN`**, **`KIMI_BIN`** and **`MUSE_BIN`** do
> the same for the experimental backends — though the Grok and Muse bridges already fall back to their
> installer's own directory (`~/.grok/bin`; `%LOCALAPPDATA%\Programs\muse` or `~/.local/bin`) on a
> `PATH` miss, which matters because those installers edit the user PATH and the change never reaches
> an already-running server.

The bridge uses only cross-platform Python (`Path.home()`, `subprocess`) and reads paths under
`~/.gemini/antigravity-cli/`, `~/.codex/`, `~/.copilot/`, `~/.cursor/`, `~/.grok/`, `~/.kimi-code/`,
and `~/.local/share/opencode/`, which the CLIs write the same way on every OS. **Developed and verified on Windows; macOS and Linux should work unmodified
provided the CLIs run there.** If you test it on those platforms, please open an issue / PR to confirm.
