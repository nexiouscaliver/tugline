# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Session detail page in the console: tapping a session card (phone)
  or table row (desktop) opens a full detail view with everything
  about the session. Store schema v5 adds
  `events.response_preview` (empty string on every existing row);
  both adapters' stop records now carry a 600-character one-line
  preview of the turn's last assistant message as record enrichment
  (titles, bodies, and priorities unchanged), and
  `Store.session_detail(session_id)` reads back the session row with
  tags, the newest userpromptsubmit body as `last_prompt`, the newest
  non-empty stop preview as `last_response`, and the newest 100
  events. `GET /api/sessions/<id>` serves it under the same token
  gate as every other read, with the session row decorated exactly
  like the sessions list. The web UI detail page shows a header line
  (label or id, display id, state chip, adapter and project badges),
  a meta grid (first seen, last seen, cwd, tags), big-target actions
  (reply to this session, rename, add/remove tags), the last prompt,
  the last response preview, and the session's events with delivery
  chips; it refreshes on every delta and a `< back` button (44px)
  returns to the previous tab/panel state. The fiddly inline
  rename/tag controls are gone from the session cards (mobile cards
  keep name/id, state, adapter, time, and last event text only);
  every control now lives on the detail page with 44px targets.

- Wake-on-reply: a reply bound to a session that lands while the
  session looks idle (its last spooled event a stop or a session
  start) wakes it at once instead of waiting for the next turn
  boundary. The daemon runs a wake dispatcher pass every cycle
  (`tugline.core.wake`): it spawns the session adapter's driver with
  the reply body as the prompt, resuming the session, with the cwd
  taken from the store's session row. The claude driver command is
  `claude --resume <session> -p "<body>"` (flag order verified
  against `claude --help`, v2.1.299) and is config-driven through
  `wake.claude_command`. Rails: one wake per reply id (deduped,
  relearned from the journal on restart), one wake in flight per
  session, a global concurrent cap (`wake.max_concurrent`, default
  1), a per-wake timeout that kills the driver
  (`wake.timeout_seconds`, default 1800), every decision and exit
  journaled to `~/.tugline/state/wake.jsonl`, and the woken reply
  marked consumed so the per-turn injection never double-delivers it.
  `toggles.wake` (default on) restores the pre-wake behavior
  exactly; `wake.dry_run` (default on) logs and journals the exact
  command without spawning anything, to be flipped off after live
  verification. Console reply lines now carry their session binding
  (`session_id`), and the replies view shows a wake-state chip per
  reply (dispatched, finished, timed_out, unsupported, skipped, or
  none). Stated plainly: this change ships no zcode wake driver; the
  ZCode app bundles a CLI (zcode.cjs v0.16.9) with documented
  headless resume flags (-p, --resume, --cwd), and wiring that driver
  is a queued follow-up lane. Until it lands, replies to idle zcode
  sessions are journaled unsupported and stay queued for the session's
  next-boundary injection. The daemon's `--once` cycle result gains a
  `wakes` count.
- Session tags, settable by the operator and by agents: a new `tags`
  table (schema version 4; existing stores migrate in place) holds up
  to 8 tags per session, each lowercase [a-z0-9-], at most 24
  characters, normalized by dropping invalid characters. The console
  gains `PUT` and `DELETE /api/sessions/<id>/tag/<tag>` (token-gated
  like the label PUT) and renders tag chips on every session row with
  an inline add editor and a remove x per chip; every session listing
  (`/api/sessions`, `/api/state`) carries each row's tags.
- Agent self-naming CLI: `tug label --session <id> <name>` (or
  `--clear`), `tug tag --session <id> <tag>...`, `tug untag
  --session <id> <tag>...`, and `tug sessions [--label X] [--tag Y]
  [--json]` to list and filter. The session id is always an explicit
  `--session` flag (no environment fallback); each command folds the
  spool into the store once first, so it works on a fresh home with
  no console running.
- Reply targeting by name: the `session_id` field of `POST
  /api/replies` now also accepts a label or a tag. Resolution order
  is exact session id, then a label exactly one session carries, then
  a tag exactly one session carries; an ambiguous label or tag is a
  400 whose `sessions` key lists every matching session id so the
  caller can show the choice. The reply box keeps showing the
  resolved target before send, label first, short id second.
- Unread operator replies now reach running sessions on every prompt:
  the zcode and Claude Code adapters handle `UserPromptSubmit` (the
  installers register the event, appending to existing installs
  without touching the already-wired events), and before the prompt
  is spooled the hook reads unconsumed replies from
  `~/.tugline/replies.jsonl`, prefers the ones the console store
  binds to that session, falls back to all unread, emits them through
  the harness context channel (one `additionalContext` JSON object on
  stdout), and marks what it injected `consumed` in the journal.
  Every reply read is bounded and best effort; any failure degrades
  silently to the previous behavior (no injection, exit 0) and never
  blocks the prompt.
- Operator session labels in the console: `PUT
  /api/sessions/<id>/label` sets or clears a plain-text label (max 60
  characters, trimmed; empty clears), stored in a new nullable
  `label` column on the sessions table (schema version 3; existing
  stores migrate in place). The page carries a rename affordance on
  every session row and shows the label as the row's primary name,
  with the short id secondary.
- Honest session liveness in the console: display state is derived
  per response from recency, not just the last event. last_seen
  within 5 minutes reads "active now"; a running session silent for
  more than 30 minutes decays to "idle"; stop stays "done" and an
  open permissionrequest stays "waiting on me" at any age. The
  sessions panel sorts active-now first, then by recency. The store
  keeps raw facts; nothing is rewritten.
- Collision-proof session ids in the console: short ids are computed
  per response as suffixes lengthened until unique within the
  current session list (the full id in the worst case), so sessions
  sharing a tail never render the same short id anywhere on the
  page.

### Changed

- `POST /api/replies` with a `session_id` that matches no session,
  label, or tag is now a 404 instead of silently recording a reply
  row for an unknown session: name resolution means a typo can no
  longer produce an undeliverable reply.
- The daemon reply leg now makes exactly one request per cycle: while
  the streaming subscribe connects, the poll fallback never runs, and
  when the stream errors the cycle spends its single request on the
  poll, reconnecting the stream on the next cycle after a quiet poll.
  The stream open and the poll no longer share a cycle, keeping the
  combined reply request rate under ntfy's one-token-per-5-seconds
  replenishment even while the server is rate limiting the visitor.

### Security

- Console GET endpoints (state, sessions, events, threads, replies,
  and the SSE stream) now require the shared token when the
  configured token file holds one, accepting it as a Bearer header or
  a `?token=` query parameter; the console page stays a static shell
  that captures `?token=` from its URL, and the loopback default
  without a token on file is unchanged. Tailnet exposure requires the
  token file.

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
