# Contributing to agent-intern

Thanks for helping. Here are the most useful contributions, roughly in order of impact:

1. **Verify a backend nobody has verified.** No real model has answered through Grok Build, Kimi
   Code or Muse Code yet. If you have any of those subscriptions, filing
   [one verification issue](https://github.com/SinanTufekci/agent-intern/issues/new?template=backend_verification.yml)
   takes about a minute and is worth more than any code change.
2. **Report upstream drift.** The CLIs behind the bridge update themselves, so most breakage is a CLI
   that changed, not a bridge bug. [A bug report](https://github.com/SinanTufekci/agent-intern/issues/new?template=bug_report.yml)
   that includes the relevant `*_status` output and the CLI's version usually pins it down straight away.
3. **Confirm macOS and Linux.** Development happens on Windows. CI runs on all three platforms, but
   only against fakes, so a real run elsewhere is very welcome.
4. **Code.** For anything bigger than a small fix, open an issue or a
   [discussion](https://github.com/SinanTufekci/agent-intern/discussions) first.

## Development setup

```bash
git clone https://github.com/SinanTufekci/agent-intern.git
cd agent-intern
pip install -e ".[dev]"
```

Before pushing, run the same three checks CI runs:

```bash
ruff check .
ruff format --check .
pytest test_server.py test_swarm.py test_codex.py test_copilot.py test_cursor.py \
       test_grok.py test_kimi.py test_opencode.py test_proc_tree.py test_muse.py \
       test_presets.py -q
```

These unit tests are offline: they use fake CLIs and spend no quota. `test_smoke.py` is different.
It's a live script that makes real calls on your own subscription, so run it by hand
(`python test_smoke.py`) and don't pass it to pytest.

## Things that will bite you

- **The layout is flat, and packaging is hand-listed.** Every runtime module sits at the repo root and
  has to be named in `py-modules` in `pyproject.toml`, or it's left out of the wheel. 0.30.1 shipped
  that way and could not start. `test_every_runtime_module_is_listed_in_py_modules` catches it now.
- **CI lists test files one by one.** A new `test_*.py` won't run in CI until you add it to
  `.github/workflows/ci.yml` (and to the pytest command above).
- **The bridges are near-parallel.** There are nine modules that spawn a CLI: `server.py`,
  `swarm.py` and the seven `*_bridge.py` files. They share their spawn, timeout and parse shapes, so a
  bug in one is usually in all of them. Fix it everywhere, and prefer hoisting the fix into a shared
  module, the way `proc_tree.py` holds the process-tree kill.
- **Test on three OSes.** CI runs Ubuntu, macOS and Windows. Watch for POSIX-only assumptions in
  tests, such as case-insensitive paths, `os.path.normcase`, shell quoting and signals.
- **Plugin skills are prose, but they're tested.** Each skill under `plugin/skills/` may only name
  tools the server actually registers.

## Releasing (maintainers)

A release writes the version into four places: `pyproject.toml`, `server.__version__`, `server.json`
(both fields) and `plugin/.claude-plugin/plugin.json`. A test keeps them in step. Add a
`CHANGELOG.md` entry, push, wait for CI to go green, then push a `vX.Y.Z` tag. From there the
workflows publish to PyPI (after importing the built wheel in a clean venv), list the release in the
MCP Registry, and cut the GitHub Release.
