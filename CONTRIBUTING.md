# Contributing to Mustarrd

Thanks for wanting to help. Mustarrd is maintained by one person in spare time, so
these notes are mostly about keeping review cheap — the faster a change is to
review, the faster it gets merged.

## One change per pull request

**A pull request should do one thing.** If the description needs more than about
three bullets to explain, it's more than one thing and should be split.

This is the single biggest factor in how long a PR sits. A 200-line PR that fixes
one bug gets reviewed the evening it arrives. A 1,800-line PR that changes
classification *and* adds database columns *and* adds a template token needs a
free weekend, because every part has to be understood before any of it can be
merged — and if one part is wrong, all of it waits.

Splitting also means the good parts ship while the debatable part is still being
discussed.

Some signs a PR wants splitting:

- The title has an "and" in it
- It touches an area you didn't set out to change
- Part of it is a refactor and part of it is behavior
- You'd struggle to write a single CHANGELOG entry for it

If a change genuinely can't be split, that's fine — say so in the description and
explain why the parts depend on each other.

## Before you open a PR

- **Run both test suites.** CI runs them anyway, but a red PR reads as unfinished:
  ```bash
  cd backend && python -m unittest discover -s tests
  cd frontend && npm test
  ```
- **Don't leave an existing test failing.** If your change makes an old test wrong,
  update that test *in the same PR* and say in the description why the old
  behavior was wrong. A failing test with no explanation looks like a bug, and it
  will be treated as one.
- **Add a regression test for bug fixes.** A test that fails before your change and
  passes after is the clearest possible argument that the fix works.
- **Add a `CHANGELOG.md` entry for anything a user would notice.** Plain English,
  newest at top, following the existing "What you would notice" / "What changed"
  shape. If you can't describe the change without jargon, it may not be ready.
- **Include screenshots for UI changes** (see `.github/pr-screenshots/`).

## Working with the existing code

- Match the style of the file you're editing. There's no repo-wide formatter.
- Python is 4-space and async throughout; React is 2-space, Mantine 7.x, TanStack
  Query.
- **Edit code in place rather than wrapping or patching it at runtime.** If a
  method needs to behave differently, change the method. Replacing behavior by
  rebinding attributes at import time means the file no longer describes what the
  app does, which makes everything after it harder to debug.
- **Follow the existing pattern for schema changes.** Database columns are added by
  the lightweight `ALTER TABLE` checks in `backend/database.py`, which run on
  every startup. Please don't add a second mechanism alongside it.
- `CLAUDE.md` describes the architecture — the download pipeline, background
  tasks, auth, configuration. Worth a skim before a first change.

## Using AI coding agents

That's fine, plenty of contributions here are agent-assisted. Two requests:

- **Review the output before you send it.** You're the author, and the PR
  description should reflect what the code actually does — descriptions that
  overstate a change cost more review time than no description at all.
- **Watch the scope.** Agents don't naturally stop at one change, which is where
  most oversized PRs come from. The one-change rule above matters more, not less,
  when an agent wrote the diff.

## Review

Expect a response within a week or so; ping the thread if it's been longer.

Review comments are about the code, not about you, and "please rework this" on a
change usually means the problem is worth solving and the approach needs another
pass — not that the work was wasted. If a comment doesn't make sense or you
disagree, say so. Discussion is cheaper than a rewrite in the wrong direction.
