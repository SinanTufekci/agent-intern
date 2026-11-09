# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Use
[**Report a vulnerability**](https://github.com/SinanTufekci/agent-intern/security/advisories/new)
on the repository's Security tab instead. Only the maintainer sees the report, and the fix can ship
before the details are public.

Include:

- the bridge version (`uvx agent-intern --version`, or the `bridge version` row of any `*_status`
  tool), your OS and the backend involved;
- the smallest prompt or setup that reproduces it;
- what you expected, and what happened instead.

Fixed vulnerabilities are published as
[security advisories](https://github.com/SinanTufekci/agent-intern/security/advisories) once a
release contains the fix.

## Supported versions

Only the latest release on [PyPI](https://pypi.org/project/agent-intern/) gets fixes. `uvx` pins the
version it first cached, so upgrade deliberately with `uvx agent-intern@latest` and restart your MCP
host.

## What counts

In scope: a flaw in the bridge itself. For example, a prompt that reaches a shell through the way
the bridge launches a CLI, a `sandbox` setting the bridge fails to pass on or enforce, or a
path-handling bug that lets a call read or write outside what it was given.

Out of scope here:

- **A bug in one of the CLIs the bridge drives** (agy, codex, copilot, cursor-agent, opencode, grok,
  kimi, muse). Report it to that CLI's vendor.
- **The documented limits of a backend's sandbox.** Every backend is an autonomous agent running with
  your privileges, and only some enforce a real OS boundary. What each `sandbox` setting does and
  doesn't enforce is written down in [docs/security.md](docs/security.md). Run the bridge in a
  container or VM when you need real isolation.

If you aren't sure which side a problem falls on, report it privately anyway.
