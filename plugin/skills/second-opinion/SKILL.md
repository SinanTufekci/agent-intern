---
name: second-opinion
description: Get an independent review of the current change from a different model family (GPT via Codex, Copilot or Cursor; Gemini via Antigravity) through agent-intern, then check each finding against the code and reconcile it with your own view. Use before merging a risky diff, when stuck on a bug, or when the user asks for a second opinion, a council, or another model's take.
argument-hint: "[what to review — defaults to uncommitted changes] [--council]"
---

Get a second opinion on: $ARGUMENTS

If that is empty, review the working tree's uncommitted changes. If you invoked this skill on your own
and the user didn't ask for a review, ask them in one line before spending their quota.

## 1. Build the brief

Put everything the reviewer needs **inline in the prompt**. Don't rely on it reading files: some
sandboxes can't. Codex on Windows is the known case, because its read-only sandbox currently refuses
every command.

- **The change.** Use `git diff HEAD`, or the plan, design or bug the user pointed at. If the diff is
  over ~1,500 lines, keep the most relevant files and say which ones you left out.
- **One paragraph of context.** What the change is meant to do, and anything the reviewer can't infer
  from the diff.
- **Not your opinion.** Leave out what you think of the change. The value is an independent read.

Ask the reviewer for correctness bugs first, each with a file:line and a concrete failing scenario.
Then risky edge cases, then anything it would do differently. It should rate each finding high,
medium or low confidence, and say "no issues found" rather than invent some.

## 2. Pick reviewers

- **Default:** one reviewer from a different model family than you. Try them in this order:
  - `codex_ask`
  - `copilot_ask` or `cursor_ask`
  - `antigravity_ask`
  - `opencode_ask`. Its free models are slow, so warn the user it can take several minutes.
- **`--council`, or the user wants several opinions:** run 2–3 reviewers from different families in
  parallel, in a single `agent_swarm` call.
- **Keep it read-only.** Use `sandbox: "read-only"` wherever a backend accepts it, and set
  `workspace` to the repo root. For Antigravity, pass `plan: true` to `antigravity_ask`, or
  `sandbox: "read-only"` in a swarm task, so it can't edit files.
- **If a backend isn't set up**, the failed call says so. Fall back to the next reviewer instead of
  stopping. If none work, suggest `/agent-intern:doctor`.

## 3. Reconcile

Don't just paste the review back. Check each finding against the actual code yourself, then file it
under one of these:

- **Agree:** it's a real issue. Give your fix or proposed fix.
- **Disagree:** it's wrong. Quote the code that shows why.
- **Unsure:** say what would settle it.

With several reviewers, lead with what they found independently. Agreement across model families is
the strongest signal you'll get. Then cover where they disagree.

End with a one-line verdict: **ship**, **fix first**, or **needs a closer look**. Don't apply any
fixes unless the user asks.
