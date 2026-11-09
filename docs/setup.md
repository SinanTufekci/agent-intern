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
- **opencode:** install `opencode` (`npm i -g @opencode/cli` for 2.x; 1.x, `npm i -g opencode-ai`,
  still works). **That's it** — its free `opencode/*`
  models answer with no account. Run `opencode auth login` only if you want to point it at your own
  Anthropic/OpenAI/… key.
- **Claude Code** *(for hosts other than Claude Code)*: install
  [Claude Code](https://claude.com/claude-code) (2.1.259 or newer), then run `claude auth login`
  once, or set `ANTHROPIC_API_KEY`. Inside Claude Code itself these tools stay hidden.

You don't need all nine — the tools for a missing CLI simply report "not found" via their `*_status`
tool. To check what's installed and signed in, run `uvx agent-intern doctor` (see
[Check your setup](#check-your-setup)). If you have no subscriptions at all, start with opencode. To leave the ones you don't use out of
Claude's context, see [Load only the backends you use](#load-only-the-backends-you-use).

### Check your setup

```bash
uvx agent-intern doctor
```

`doctor` does what the plugin's `/agent-intern:doctor` skill does, in any host or in a plain
terminal. It runs the same checks as the `*_status` tools for every backend that
[`AGENT_INTERN_BACKENDS`](#load-only-the-backends-you-use) leaves on, and shows each backend as
`ready`, `warning` or `unusable` with the next step to fix it. It spends no quota.

| Flag | What it does |
|---|---|
| `-v`, `--verbose` | show every check, not just the summary line per backend |
| `--json` | print the result as JSON, for scripts |

The exit code is **0** when at least one backend is usable, **1** when none is, and **2** on a usage
error. `uvx agent-intern --version` (or `-V`) prints the bridge version. Your home
directory shows as `~`, e-mail addresses as `<email>` and API keys with a known prefix as `<key>`;
still read the output before you post it in a public issue.

### Give a host the routing guide

```bash
uvx agent-intern rules
```

Prints the routing guide: the same text the server sends as MCP instructions (byte for byte) for the
backends that [`AGENT_INTERN_BACKENDS`](#load-only-the-backends-you-use) leaves on, as UTF-8. Some
hosts never show MCP instructions to their model, so a host's model doesn't learn when to reach for
which tool unless you put the guide where that host reads its rules:

```bash
uvx agent-intern rules > AGENTS.md
```

or append it to an existing `AGENTS.md`. That one file is read by agy, Copilot CLI, Cursor CLI and
opencode. If your shell redirects as UTF-16 (Windows PowerShell 5 does), write the file with an
explicit UTF-8 encoding instead.

What each host does with the guide on its own, and which rules files it reads (measured on
2026-11-09 with planted markers in one workspace; home-level rules files weren't tested):

| Host | Does the model get the guide? | Rules files it reads |
|---|---|---|
| Claude Code 2.1.319 | Yes, whole: it is about 3.4 KB, and Claude Code cuts at 4,096 characters. It doesn't need `rules`. | `CLAUDE.md` only |
| Cursor CLI 2026.11.05 | Yes (with `--approve-mcps`). | `.cursor/rules`, `CLAUDE.md`, `AGENTS.md` |
| Copilot CLI 1.0.105 | Only with `--allow-all-mcp-server-instructions`. | `.github/copilot-instructions.md`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` |
| agy 1.5.3 | No instructions, and MCP tools are listed by name only. | `AGENTS.md`, `GEMINI.md` |
| opencode 2.2.5 | Unconfirmed: the source forwards them, three runs on a free model said the model had none. | `AGENTS.md` only |

When the guide doesn't arrive, write it into `AGENTS.md` as above, or name the tool in your prompt
(*"use opencode_ask to review this diff"*).

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

### Antigravity (the `agy` CLI)

The same server runs in Antigravity. Register it with one command; `agy` writes
`~/.gemini/config/mcp_config.json` for you, so there is nothing to hand-edit:

```bash
agy mcp add agent-intern uvx agent-intern
```

All 32 tools load under their bare names (`antigravity_ask`, `claude_ask`, …) with no prefix, and every
`agy` run picks the registration up with no restart (verified on agy 1.5.1).

```bash
# load only some backends (see "Load only the backends you use" below)
agy mcp add --env AGENT_INTERN_BACKENDS=opencode,antigravity agent-intern uvx agent-intern

agy mcp remove agent-intern     # take it out again
```

Three things differ from Claude Code:

- **Headless `agy -p` auto-denies MCP tool calls.** Add a rule such as
  `mcp(agent-intern/opencode_status)` to `permissions.allow` in
  `~/.gemini/antigravity-cli/settings.json`, or run `agy` interactively and approve each call.
  `{"permissions":{"allow":["mcp(*)"]}}` in that file let headless calls through too (verified on agy
  1.5.3). `mcp(*)` is a wildcard: treat it as allowing every MCP tool of every server you register,
  and prefer per-tool rules where you can.
- **agy shows the model no server instructions**, and lists an MCP tool by name only. agy reads
  `AGENTS.md` and `GEMINI.md`: write the guide into one with `uvx agent-intern rules > AGENTS.md` (see
  [Give a host the routing guide](#give-a-host-the-routing-guide)), or name the tool in your prompt:
  *"use opencode_ask to review this diff"*.
- **There are no slash commands** (they belong to the Claude Code plugin). Run
  `uvx agent-intern doctor` instead.

Tested: `antigravity_ask` called from inside `agy` answers, with no lock or conversation clash with
the host. Not tested: `watch=true` under `agy`, and the Antigravity IDE, which keeps its own MCP
server list that `agy mcp add` doesn't touch.

> [!CAUTION]
> Don't paste the output of `agy mcp list` into an issue or a chat: it prints each registered
> server's full command line, including any API keys passed in it.

### GitHub Copilot CLI

The same command shape registers it; `copilot mcp add` writes `~/.copilot/mcp-config.json` for you
(with `tools: ["*"]`):

```bash
copilot mcp add agent-intern -- uvx agent-intern
```

All 32 tools load, and `opencode_status` and `claude_status` were called (verified on copilot
1.0.105 / Windows against the 0.36 server, with the same command shape; the test launched the server
through Python rather than `uvx`). To try it without writing a file, pass the
server definition as JSON with `--additional-mcp-config '<json>'`.

Differences from Claude Code:

- **Tool names are `<server>-<tool>`**, e.g. `agent-intern-opencode_status`, not the bare names.
- **The server's routing guide reaches the model only with `--allow-all-mcp-server-instructions`.**
  Without it, it doesn't: pass the flag, write the guide into a file Copilot reads
  ([`uvx agent-intern rules > AGENTS.md`](#give-a-host-the-routing-guide)), or name the tool in your prompt
  (*"use opencode_ask to review this diff"*).
- Headless `copilot -p … -s` called the tool with no extra allow flag.
- There are no slash commands. Run `uvx agent-intern doctor` instead.

### Cursor CLI (`cursor-agent`)

Add the server to `.cursor/mcp.json` in your project, or to `~/.cursor/mcp.json` for every project:

```json
{
  "mcpServers": {
    "agent-intern": {
      "command": "uvx",
      "args": ["agent-intern"]
    }
  }
}
```

All 32 tools load, the server's routing guide reaches the model, and `opencode_status` and
`claude_status` both passed (verified on cursor-agent 2026.11.05 / Windows against the 0.36 server). **Only the project file was tested**; `~/.cursor/mcp.json` follows Cursor's
documented format and wasn't run.

Differences from Claude Code:

- **Headless runs reject MCP calls unless the run has both `--approve-mcps` and `--force`.** After
  that, the approval persists per workspace. Interactive runs just ask. Be aware that `--force` also
  lets `cursor-agent` itself write files without asking.
- There are no slash commands. Run `uvx agent-intern doctor` instead.

Not tested: the Cursor IDE.

### opencode

The same command shape registers it. Without `--global` it writes `opencode.json` in the current
directory (your project):

```bash
opencode mcp add agent-intern -- uvx agent-intern
```

`--global` writes `~/.config/opencode/opencode.jsonc` instead; that wasn't run. To write the entry
by hand:

```json
{
  "mcp": {
    "servers": {
      "agent-intern": {
        "type": "local",
        "command": ["uvx", "agent-intern"]
      }
    }
  }
}
```

All 32 tools load, and `opencode_status` and `claude_status` were called through Code Mode (verified
on opencode 2.2.5 / Windows against the 0.36 server, with a free model and the same command shape;
the test launched the server through Python rather than `uvx`).

Differences from Claude Code:

- **opencode 2.x doesn't expose MCP tools as separate tools.** The model reaches them through
  opencode's Code Mode `execute` tool, as `tools["agent-intern"].opencode_status()`. Any opencode
  policy that denies `execute` makes them unreachable (per opencode's docs).
- **Whether the server's routing guide reaches the model is unconfirmed:** opencode's source forwards
  it, but three runs on a free model said the model had none. opencode reads `AGENTS.md`: write the
  guide there ([`uvx agent-intern rules > AGENTS.md`](#give-a-host-the-routing-guide)), or name the tool in your prompt
  (*"use opencode_ask to review this diff"*).
- Headless `opencode run` called the tool with no extra approval.
- **Don't use `opencode mcp list` as a load check:** it printed "No MCP servers configured" while
  the server was loaded.
- There are no slash commands. Run `uvx agent-intern doctor` instead.

> [!NOTE]
> Any other stdio MCP host *should* work with the same `command` and `args`, but none has been
> tested yet.

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

Restart Claude Code. **Twenty-nine tools** appear, each prefixed `mcp__agent-intern__`. (Three more,
the `claude_*` tools below, appear in every other host: **thirty-two** in all.)

- **Antigravity (5):** `antigravity_ask`, `antigravity_continue`, `antigravity_image`,
  `antigravity_image_swarm`, `antigravity_status`
- **Codex (3):** `codex_ask`, `codex_continue`, `codex_status`
- **Copilot (3):** `copilot_ask`, `copilot_continue`, `copilot_status`
- **Cursor (3):** `cursor_ask`, `cursor_continue`, `cursor_status`
- **Grok (3, experimental):** `grok_ask`, `grok_continue`, `grok_status`
- **opencode (3):** `opencode_ask`, `opencode_continue`, `opencode_status`
- **Claude Code (3, not in Claude Code itself):** `claude_ask`, `claude_continue`, `claude_status`
- **Muse (3, experimental):** `muse_ask`, `muse_continue`, `muse_status`
- **Kimi (3, experimental):** `kimi_ask`, `kimi_continue`, `kimi_status`
- **Shared (3):** `agent_swarm` — fans a list of tasks out across **eight** backends in one run
  (everything but Kimi); `preset_swarm` and `swarm_presets` — run and list the ready-made panels

The single-prompt tools of every backend but Kimi take a **`watch=true`** flag for the live browser
view ([Watch mode](watch-and-swarm.md#watch-mode)). Kimi has no watch mode yet.

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

### Load only the backends you use

Every tool's description, and the routing guide, sit in the model's context in every session: about
13,500 tokens for all 32 tools (about 12,500 for the 29 inside Claude Code, which hides the three
Claude Code tools). List the backends you use in **`AGENT_INTERN_BACKENDS`** and the
server leaves the rest out, both their tools and their part of the routing guide. With
`antigravity,codex` that is 11 tools and about 5,800 tokens; with `codex` alone, 6 tools and about
3,000. These are estimates (characters of the instructions plus each tool's name, description and
schema, scaled to tokens), not exact tokenizer counts.

```bash
claude mcp add -s user agent-intern -e AGENT_INTERN_BACKENDS=antigravity,codex -- uvx agent-intern
```

The names are `antigravity`, `codex`, `copilot`, `cursor`, `claude`, `grok`, `opencode`, `kimi` and
`muse`, separated by commas or spaces. Unset, every backend is on, except that `claude` stays off
when the host is Claude Code (naming it here turns it on anyway). The plugin's server takes Claude Code's
own environment, so with the plugin, set the variable in the `env` block of
`~/.claude/settings.json` or in the shell you start Claude Code from. Restart Claude Code after a
change.

- A backend that's off has no tools at all, `*_status` included. Every other backend's `*_status`
  shows a `backends` row with what is on and off.
- An `agent_swarm` or `preset_swarm` worker on a backend that's off fails in place, as it would if
  that CLI weren't installed. The rest of the panel still runs.
- The three shared tools stay while any backend that can be a swarm worker is on (every one but
  Kimi).
- A name that matches no backend is ignored, and the `backends` row flags it. If no name matches,
  every backend stays on.

## Requirements

- Python 3.10+
- **For the Antigravity tools:** [`agy`](https://antigravity.google/) 1.0.0+ on `PATH` (state-file layout re-verified on **1.0.15**; behaviour re-verified on **1.5.3**) and an active Antigravity / AI Pro session
- **For the Codex tools:** [`codex`](https://developers.openai.com/codex/) on `PATH` and logged in (`codex login`) — verified on **codex-cli 0.162.1** (note its [Windows sandbox caveat](security.md#security))
- **For the Copilot tools:** [`copilot`](https://docs.github.com/en/copilot/how-tos/copilot-cli) on `PATH` and logged in (`copilot` → `/login`, or a `COPILOT_GITHUB_TOKEN`/`GH_TOKEN` env) — verified on **copilot 1.0.105**
- **For the Cursor tools:** [`cursor-agent`](https://cursor.com/cli) on `PATH` and logged in (`cursor-agent login`, or a `CURSOR_API_KEY` env) — verified on **cursor-agent 2026.11.05**
- **For the opencode tools:** [`opencode`](https://opencode.ai/) on `PATH` (`npm i -g @opencode/cli` for 2.x, `npm i -g opencode-ai` for 1.x) — **no login needed** for its free `opencode/*` models; `opencode auth login` (or a provider env var) for everything else — verified live through the bridge on **opencode 2.2.5** (free models, Windows); 1.x verified end-to-end on **1.18.29**, re-verified on **1.19.5**. Any other major version, or one the bridge can't read, is refused with a clear message instead of running unfenced
- **For the Claude Code tools (hosts other than Claude Code):** [`claude`](https://claude.com/claude-code) 2.1.259+ on `PATH` (override with `CLAUDE_BIN`), signed in with `claude auth login` or an `ANTHROPIC_API_KEY` env — verified on **claude 2.1.319** / Windows. `claude_status` shows the version and how you're signed in (method, plan, login expiry), never your e-mail or organisation
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
> `opencode.CMD` shim with exactly the same problem). **`CLAUDE_BIN`** does the same for `claude`; **`GROK_BIN`**, **`KIMI_BIN`** and **`MUSE_BIN`** do
> the same for the experimental backends — though the Grok and Muse bridges already fall back to their
> installer's own directory (`~/.grok/bin`; `%LOCALAPPDATA%\Programs\muse` or `~/.local/bin`) on a
> `PATH` miss, which matters because those installers edit the user PATH and the change never reaches
> an already-running server.

The bridge uses only cross-platform Python (`Path.home()`, `subprocess`) and reads paths under
`~/.gemini/antigravity-cli/`, `~/.codex/`, `~/.copilot/`, `~/.cursor/`, `~/.grok/`, `~/.kimi-code/`,
and `~/.local/share/opencode/`, which the CLIs write the same way on every OS. **Developed and verified on Windows; macOS and Linux should work unmodified
provided the CLIs run there.** If you test it on those platforms, please open an issue / PR to confirm.
