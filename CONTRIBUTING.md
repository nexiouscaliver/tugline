# Contributing

Small project, few rules.

## Setup

```
uv sync
uv run --frozen pytest -q
```

Python 3.10 is the floor; CI runs the suite on 3.10 and 3.14. The runtime
dependency list is empty on purpose: the standard library covers
everything the client does, and keeping it that way keeps the hook fast
and the trust surface small. Do not add a runtime dependency without
opening an issue to argue for it first. Dev tooling (pytest) is fine.

## What lives where

- `src/tugline/core/` the engine: spool, policy, publisher, daemon,
  topics, config. No harness knows about these files and they know about
  no harness.
- `src/tugline/adapters/` one directory per harness.
  Adapters translate and get out of the way.
- `tests/` mirrors that layout. Adapter changes come with tests; the suite
  is the release gate.

## The rules that are actually load bearing

- Hooks must never block or crash a session. Every failure path in an
  adapter logs one line and exits 0. If you add a failure mode, add the
  test that proves it stays silent.
- Fail closed on permissions. Nothing in the approve path may ever
  auto-allow. Tests for that behavior exist; keep them passing.
- Config is strict: unknown keys and sections are loud errors, not
  silent ignores. New settings follow that rule.

## Commit style

Plain conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`,
`refactor:`. Subject in the imperative, lowercase, no trailing period. No
attribution trailers of any kind; commits look like they were typed by a
person at a terminal, because they were.

## The content rule

Applies to everything others will read: docs, commit messages, code
comments, changelog entries.

- No em dashes. Use a period, a comma, or a colon, or restructure the
  sentence. This is a hard rule; a sweep for dash bytes runs before every
  release.
- No filler phrasing: marketing hype words are banned outright and the
  forbidden-text sweep enforces the list. Write like a developer talking
  to a peer: short sentences, concrete commands, real output examples.
- Plain ASCII in prose. Code and data do what they must, but the words
  around them stay ASCII.
- Document only what the code does. If a documented behavior and the code
  disagree, either fix the code or fix the doc; do not ship the
  disagreement. Verify every command you write into the docs against the
  actual CLI.

## Releases

Versions follow semver loosely while pre-1.0. The version lives in
`pyproject.toml` and `src/tugline/__init__.py` and moves in one isolated
commit, last on the branch, before tagging. The changelog is Keep a
Changelog format; every user-visible change gets a line.
