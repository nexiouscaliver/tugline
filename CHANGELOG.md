# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `tug version` now reports the installed distribution version instead of a stale hardcoded 0.1.0.

## [0.2.0] - 2026-10-13

### Changed

- Notification titles and bodies now carry session context: titles read
  "[harness] project - plain summary" (for example "[zcode] mission-control -
  waiting for your input") and bodies lead with "session <last 6 of the id>"
  plus a comma separated detail such as "permission: Bash" or an 80 character
  turn preview; raw Claude notification type strings no longer reach the
  phone. Set `[toggles] context_titles = false` to restore the exact previous
  title and body strings in both adapters.
- Approve-path permission records (the urgent button records emitted while
  the approve toggle is on) carry the same session context: titles read
  "[zcode] project - waiting for your input" and "[claude] project - waiting
  for your input", bodies lead "session <last 6 of the id>, permission:
  <tool>" and keep the phone action guidance and the summarized tool input,
  reusing the shared core payload helpers. When the caller passes no cwd or
  tool the record degrades exactly as the hook records do ("unknown"
  project, "tool use"). The same `[toggles] context_titles = false` switch
  restores their exact previous strings too.

### Fixed

- Daemon flush path: a healthy ntfy reply stream never ended on its own
  (keepalive events every 45s reset the read timeout), so the serve loop
  never returned to flush and every record queued after daemon start sat
  undelivered. Each stream pass is now bounded by a flush interval
  (default 5s, deadline checked per line), so the spool flushes at least
  every 10 seconds even while the stream is held open.

### Added

- One `flush pass: scanned/handled/published/marked/suppressed` summary
  log line per flush pass (empty passes included), so an idle or starved
  flush is visible in the daemon log instead of silent.
- Session store: a sqlite database under `<home>/store/tugline.db` that
  folds the spool into sessions, events, and approval threads, with a
  tailer ingesting on a fixed interval and refreshing delivery state and
  thread status; ingest is idempotent against records already stored.
- Console HTTP API with server-sent events: state, sessions, events,
  threads, and replies reads, reply and approval writes, one `snapshot`
  event on connect and a `delta` after each ingest pass.
- Console web UI: sessions with running, waiting, and done states, the
  live event stream with delivery chips, open asks with Approve and
  Deny, a reply box with an explicit target, and header health; one
  page, no external assets.
- `tug console`: serves the console on 127.0.0.1 behind the
  `[toggles] console` switch (default on) and `[server] console_port`
  (default 2588, 0 picks a free port).
- `TUGLINE_HOME`: one environment variable moves the whole tree (spool,
  state, logs, store, replies).
- Shared approval file helper: one module for the pending markers and
  decision files, so the adapters, the approve receiver, and the console
  cannot drift apart on payload shape or write discipline.
- Hook records carry adapter, project, and cwd session fields through to
  events and sessions in the store.
- Event queries are ordered by timestamp with an id tiebreak, so spool
  records that arrive out of order still read chronologically.

## [0.1.0] - 2026-10-13

### Added

- Repository skeleton: MIT license, uv-based packaging with hatchling, src
  layout, CI on Python 3.10 and 3.14, release workflow using PyPI Trusted
  Publishing.
- Core library: payload normalization for hook input, TOML config with
  loud validation of every key, append-only spool with delivery marks,
  arming and idle-threshold policy, one-shot ntfy publisher with a bearer
  token file, daemon, CLI.
- Daemon publishing and replies: retry with capped exponential backoff;
  a held streaming subscribe on the reply topic appends each phone reply
  to `~/.tugline/replies.jsonl` and resumes from a saved cursor after a
  disconnect.
- `tug` CLI: `notify`, `arm`, `disarm`, `test`, `status`, `daemon`
  (with `--once`), `version`, and the adapter entries `hook-zcode`,
  `install zcode`, `uninstall zcode`, `hook-claude`.
- zcode adapter: hook entry, byte-preserving installer and uninstaller for
  `~/.zcode/cli/config.json`, and a bounded fail-closed approve flow behind
  the approve toggle (off by default).
- Claude Code adapter: hook entry, append-only installer and uninstaller
  for `~/.claude/settings.json`, and a bounded fail-closed approve flow
  behind the approve toggle (off by default).
- Approve receiver: loopback HTTP listener (`approve_port`) that records
  Approve/Deny button presses as decisions; `external_url` points the
  buttons at a host that proxies `/approvals` back to this machine.
- Documentation set: README, quickstart, channels playbook, adapter
  guides, Codex and Aider recipes, always-on hosting sketch, comparison
  table, security policy, contributing guide.

## [0.0.1]

- Repository skeleton: MIT license, uv-based packaging with hatchling, src
  layout; `tug` CLI entry point with `--version` and a `hello` smoke
  command; GitHub Actions CI with uv on a Python 3.10 and 3.14 matrix;
  release workflow for PyPI Trusted Publishing on `v*` tags.
