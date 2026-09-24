## What and why

<!-- What changes, and what was wrong or missing before. Link the issue if there is one. -->

## How it was verified

<!-- Unit tests, a live run against a real CLI (which version, which OS), or both. -->

## Checklist

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] The offline test suite passes (see CONTRIBUTING.md for the command)
- [ ] If this fixes a bridge, the other bridges that share the shape were checked too
- [ ] A new root module is in `py-modules`, and a new test file is in `ci.yml`
- [ ] User-facing change → `CHANGELOG.md` under `[Unreleased]`
