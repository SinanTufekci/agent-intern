# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

agent-intern is a stdio MCP server (FastMCP, Python 3.10+) that runs other coding CLIs headless as
sub-agents: agy, codex, copilot, cursor-agent and opencode, plus grok, kimi and muse (experimental,
never answered by a real model). Users run it from PyPI as `uvx agent-intern`.

## Commands

```sh
pip install -e ".[dev]"     # what CI runs; a uv-made .venv has no pip: uv pip install -e ".[dev]"
ruff check .                # CI gate
ruff format --check .       # CI gate; tree is clean. Format only the files you touched
pytest -q                   # offline suite, ~930 tests in ~30 s, fake CLIs, no quota spent
pytest test_server.py::test_every_runtime_module_is_listed_in_py_modules   # one test
python server.py            # the server on stdio (the `agent-intern` script is the same)
python test_smoke.py        # LIVE: real agy calls on your own quota; never pass it to pytest
```

A bare `pytest` collects the same files CI names in `.github/workflows/ci.yml`, because
`conftest.py` ignores `test_smoke.py`. CI lists them one by one, so a new `test_*.py` has to be
added there (and to the command in CONTRIBUTING.md) or CI never runs it.

## Rules

**Fetch before you start.** Local `main` can lag `origin/main`: releases are sometimes cut from
another checkout (0.32.1 was). Run `git fetch origin` and `git log --oneline main..origin/main`
first, and branch from `origin/main`.

**The server must keep working in both Claude Code and Antigravity.** Both host the same stdio
server. `plugin/` and `.claude-plugin/` (the slash-command skills) are a Claude Code-only extra;
nothing the server does may depend on them. `SERVER_INSTRUCTIONS` and the tool descriptions speak
to "the host model" and must not assume Claude is the caller. Unit tests cannot show host
compatibility: a change to tool names, signatures, instructions or startup needs a live load in
both hosts before it ships.

**stdout is the JSON-RPC channel.** Diagnostics go to stderr through `server.log`; a `print()` in
server code writes into the protocol stream.

**No AI attribution.** No `Co-Authored-By:` trailer naming an AI and no "Generated with" line, in
commits or PR bodies. Commit subjects are `type(scope): summary` with fix, feat, docs, test,
refactor, build or release.

**Backslashes go through Write/Edit, never a Bash heredoc.** On the Windows dev machine the Bash
tool halves `\\` inside heredocs, even quoted `<<'EOF'`. It once put `\v` and `\b` into
docs/status.md and broke a Windows path in two test helpers while the suite stayed green. After
any edit with Windows paths, regexes or `\r\n`, look for control characters:
`git grep -I --untracked -nP '[\x00-\x08\x0b\x0c\x0e-\x1f]'`.

## Traps (CONTRIBUTING.md has the long form)

- **Flat layout, hand-listed wheel.** Every root module must be in `py-modules` in
  `pyproject.toml`. 0.30.1 shipped without `proc_tree.py` and could not start; the test above and
  CI's `package` job (imports the built wheel from a clean venv) now catch it.
- **A backend is two modules.** `<name>_bridge.py` drives the CLI; `<name>_tools.py` holds
  `*_ask`/`*_continue`/`*_status`. agy's bridge and the shared helpers are still in `server.py`. A
  new tool module goes in `server._TOOL_MODULES`. Tool modules look helpers up as `server.<name>`
  at call time, so tests patch `server.<name>`; `watch_server.py` calls its own names, so patch
  `watch_server.<name>` there.
- **Nine modules spawn a CLI** (`server.py`, `swarm.py`, seven `*_bridge.py`) with near-identical
  spawn, timeout and parse code. A bug in one is usually in all: fix every one, and hoist shared
  fixes into a shared module the way `proc_tree.py` holds the process-tree kill.
- **Every spawn goes through `proc_tree.check_args`.** On Windows a `.cmd`/`.bat` shim makes
  cmd.exe re-parse the prompt; that was a command injection before any sandbox applied (fixed in
  0.30.3). Resolve the native binary rather than launching a shim.
- **`conftest.py` fails any test that starts a real agent CLI or browser**, even when the code
  swallows the error. Mark a deliberate one `@pytest.mark.real_cli`.
- **CI is Ubuntu, macOS and Windows on py3.10-3.13**, all against fakes. Development is on Windows,
  so watch for case-insensitive paths, `os.path.normcase`, shell quoting and signals in tests.
- **Plugin skills are prose but tested:** `plugin/skills/*/SKILL.md` may only name tools the
  server registers.

## Upstream drift and docs

The CLIs update themselves, so most breakage is upstream, not the bridge. A verified CLI version is
written in several places: README ("Verified live against"), docs/setup.md, docs/status.md, tool
docstrings and `VERIFIED_AGY_VERSION`. On a re-verification, `git grep` the old version and update
every hit; docs/status.md records what changed, what was checked live and what was not. "Verified"
means a real call was made. A user-facing change goes in CHANGELOG.md under `[Unreleased]`.

## Releasing

The version is written in `pyproject.toml`, `server.__version__`, `server.json` (both fields) and
`plugin/.claude-plugin/plugin.json`; `test_version_is_the_same_everywhere_a_release_writes_it`
keeps them in step and guards the README's `mcp-name:` line. Move `[Unreleased]` under the new
version, commit `release: X.Y.Z — headline`, push, wait for green CI, then
`git tag vX.Y.Z && git push origin vX.Y.Z`. The tag fires `publish.yml` (PyPI via Trusted
Publishing, then the MCP Registry) and `release.yml` (GitHub Release). A PyPI upload cannot be
taken back: tag only when the owner says so.
