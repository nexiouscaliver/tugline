# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Permission asks in the console, mobile and desktop: the store now
  threads every permissionrequest it ingests (kind "permission", one
  per session, refreshed by each new ask) regardless of the approve
  toggle, so harness permission prompts (zcode workflow
  confirmations, tool permissions) are finally visible in the asks
  section while the toggle is off; the toggle governs the phone
  decision buttons, not read-side visibility. The card shows the
  question, the tool, the session identity (label, adapter and
  project badges), a waiting chip while the session still sits on
  the prompt, and the honest note ("approve is off - answer in the
  harness") instead of decision buttons no file backs; the reply
  link stays as the one console action. When the harness moves on
  (any later event for the session) the thread settles as gone,
  keeping its question and timestamp, and a schema v7 migration
  backfills the same one-per-session threads for permissionrequests
  a console already ingested before this change (open when the
  permissionrequest is still the session's newest event, gone
  otherwise).

- Asks section stability: thread rows now carry their own identity
  fields (label, tags, collision-proof display_id, adapter, project)
  and a decidable flag, served by one server-side view through both
  GET /api/threads and the SSE snapshot, so the section renders from
  one source of truth instead of stitching names from the sessions
  list. The stream (re)opening refetches the section, so a reconnect
  repaints instead of leaving a stale list; a failed fetch shows an
  inline error line instead of a silent blank; option and decision
  buttons keep 44-48px targets with taps stopped from bubbling, and
  buttons use touch-action manipulation so a fast second tap is
  never eaten by double-tap zoom.

- Ask cards in the console, mobile and desktop: open `tug ask`
  threads render as question cards under the approval cards, the
  question text in full and each lettered option a full-width
  tappable row (letter in monospace, 48px floor). A tap POSTs to the
  new `POST /api/asks/<nonce>` endpoint (token-gated like every /api
  route), which records the answer exactly like a bound reply (a
  console reply row plus the replies.jsonl line, body the bare
  option key, so the asking process and every reply consumer see it)
  and closes the thread itself; the option rows disable while the
  request is in flight, a failure shows an inline error and
  re-enables them, and the settled list shows the chosen letter in
  green. An already-answered ask answers 200 again with the recorded
  key and never writes a second reply row; an expired ask answers
  409, an unknown nonce 404, a wrong key or a non-ask thread 400.

- Session identity on every console stream row and detail event row:
  each row renders the display name the enriched row itself carries
  (the label when set, else the collision-proof unique suffix, the
  one server-side display rule), and desktop rows add small adapter
  and project badges under the name when the record carries them;
  phone rows show the name only.

- Group by session toggle in the console's live stream: a 44px chip
  in the panel header flips between the flat newest-first list and
  sections grouped by session, newest activity first, events
  newest-first inside each section. A section header carries the
  display name, a state chip, and a relative time, and tapping it
  aims the sessions filter bar at that one session (the full session
  id as the search term, the state chip reset). The choice lives in
  memory, so repaints and SSE deltas keep honoring it and a fresh
  page starts flat.

- Markdown rendering for the console's session detail full-text
  blocks: the last prompt and the last response render through an
  escape-first renderer (every character of payload text reaches the
  DOM through textContent, and only whitelisted tags are ever
  created) supporting fenced code blocks, inline code, bold, italic,
  http(s)-only links, #..#### headings, unordered and ordered lists,
  blockquotes, thematic breaks, and paragraphs. Text carrying no
  markdown construct renders exactly as the old single paragraph did;
  stream rows and card subtitles stay plain text.

- Sessions filter bar in the console, mobile and desktop: an inline
  bar at the top of the sessions panel with a case-insensitive search
  input (label, display id, full session id, adapter, project, and
  last-event text) and a single-line horizontally scrollable chip row
  (All, Running, Waiting on me, Idle, Done; Running covers running
  and active). Filtering is client side on the data in memory, the
  filter state survives repaints and SSE delta refreshes, a count
  line reads "N of M sessions" while a filter is active, and the
  panel empty state becomes "no sessions match the filter" when a
  filter clears the list.

- The tug ask primitive: one multiple choice question an operator
  answers from tugline itself. `tug ask --session <id> --option
  "A: description"... [--timeout N] "question"` creates an ask
  thread (the existing threads table, `kind: "ask"`, options stored
  as JSON), pushes one HIGH priority notification titled
  `[<harness>] <project> - asks: <question>` through the enriched
  title helpers with the lettered options and reply guidance in the
  body, then blocks polling for an answer up to the timeout
  (default 300s, max 1800s). Answers arrive as a console option tap
  or reply bound to the thread nonce (id > label > tag targeting),
  a reply journal line whose body is exactly one option key and
  whose thread/session binding resolves to the ask (a binding is
  required, and a session-bound reply lands on the session's newest
  open ask), or `tug answer --thread <nonce> <key>`. On an answer
  the command prints `key: text` and exits 0; on timeout it prints
  `no answer`, exits 1, and marks the thread expired. Wrong keys
  are rejected (the ask keeps waiting), no failure path raises, and
  a publish failure never kills an ask (it stays answerable from
  the console). Threads API rows for asks carry their parsed
  `options` plus the answer state so the console can render
  tappable choices (new `tugline.core.ask` module; schema version 5
  adds the threads.options column).
- Session auto-identity: every session is recognizable from its
  first moments with zero operator work. On ingest the store
  auto-labels an unlabeled session from its first suitable
  UserPromptSubmit record (the first 5-7 words of the prompt,
  sanitized to ASCII, collapsed whitespace, at most 40 characters,
  no trailing punctuation artifacts; set once, later prompts never
  overwrite) and auto-tags a session at first sight with its
  adapter and its project label from cwd (idempotent, operator tags
  never touched, and because auto-tags fire only at first sight an
  operator untag is permanent). An operator label always wins and
  pauses auto-labeling for that session, including clearing it (new
  `label_source` column, schema version 5). One display rule
  everywhere: the label when set, else the W7-L16 collision-proof
  unique suffix, never the leading `sess_...` characters, exported
  as `tugline.core.store.session_display_id` and attached to every
  session row (`display_id`) and every event row (`label`, `tags`,
  `display_id`) so any stream item answers who did what where.
  Store migrations are in place from v4 (existing labels and tags
  ride over untouched; v4 sessions without labels can still be
  auto-labeled by their next prompt).
- Session detail page in the console: tapping a session card (phone)
  or table row (desktop) opens a full detail view with everything
  about the session. Store schema v5 adds
  `events.response_preview` (empty string on every existing row);
  both adapters' stop records now carry a 600-character
  newline-keeping preview of the turn's last assistant message as
  record enrichment (code blocks and lists survive; titles, bodies,
  and priorities unchanged), and
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
- ntfy detail parity: phone buzzes carry the same identity and
  detail the web console shows. At publish time the daemon resolves
  each record's session to the console display name (the label when
  set, else the collision-lengthened unique suffix, via
  `tugline.core.store.session_display_id`) through one read-only
  SELECT over the sessions table per flush pass, rewrites the
  `session <frag>` token in the body to `session <name>`, and
  prefixes the title with `[<name>] ` when it does not already carry
  it; sessions with no store row publish exactly what the hook
  spooled. Stop records carrying `response_preview` append a blank
  line plus up to 300 characters of the preview (newlines kept,
  nothing collapsed). Adapter and project ride along as ntfy tags
  (lowercase, hyphens for spaces) after the per-event emoji tags;
  ntfy shows unknown tags as plain text chips. Session-scoped
  notifications (stop, permissionrequest, resumed sessionstart)
  carry a click URL from the new optional `[server] console_url`
  (default empty, validated as http(s) or empty) plus a
  `#session/<id>` fragment; the shared token never rides in the URL
  (the console page keeps its own token capture). Hook-time
  fragments now derive from the display rule (the last 6 characters)
  instead of the legacy first-8 slice in both adapters (zcode hook
  and approve paths, claude context-off bodies), so pre-publish
  fallback names and console names agree.

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
  none). The daemon's `--once` cycle result gains a `wakes` count.
- zcode wake driver: replies to idle zcode sessions wake them through
  the CLI the ZCode desktop app bundles, spawned as `node
  /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs --resume
  <session> -p "<body>" --cwd <project dir>`. Every flag was verified
  against the bundled CLI's own help (v0.16.9): `-p, --prompt <text>`
  runs a single prompt without opening the TUI, `--resume
  <sessionId>` resumes a persisted session, `--cwd <path>` sets the
  working directory; `--mode` is deliberately not passed because help
  documents yolo as the default permission mode for `--prompt`, and
  nothing is attached (the reply body is the whole prompt). The
  command template is config-driven through `wake.zcode_command`, and
  `wake.adapters` now defaults to `["claude", "zcode"]` (existing
  configs that list adapters explicitly keep their list). The path is
  the app-bundled CLI, so the driver is version-locked to the
  installed ZCode app: if an app update moves the bundle, re-point
  `wake.zcode_command`. An adapter with no driver entry (anything
  but claude and zcode) is still journaled unsupported and stays
  queued for the session's next-boundary injection.
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

- Daemon-context zcode wakes no longer die on the missing provider
  config: the zcode app injects ZCODE_BUILTIN_PROVIDER_CONFIG_FILE
  (and the personal variant) into its child processes, but the
  launchd daemon has neither and the CLI's fallback paths do not
  exist on disk, so every daemon-spawned wake exited 1 in 6s on
  "cannot locate CLI ZCode Built-in Provider Config". The zcode hook
  now records the app-injected config paths to state/zcode-runtime.json
  on every invocation (app upgrades move the versioned paths), and the
  wake spawn merges them back into the child environment (setdefault
  semantics, reread from the record at every dispatch). End-to-end
  proven with a real resumed session answering from its context under
  a daemon-equivalent stripped environment plus exactly those two
  variables.
- The zcode wake driver now continues the latest session in the
  session's project directory (`-c`) instead of resuming by id: the
  app-bundled CLI's `--resume <id> -p` form always exits 1 at model
  creation ("Select a model before continuing") because the
  rehydrated session loses its model selection, while `-c`
  genuinely resumes (live-verified: a second turn inside a
  pre-existing session, exit 0). A new guard skips the wake when the
  bound session is not the store's latest for its cwd, so `-c` can
  never deliver a reply to the wrong session; a `wake.zcode_command`
  override using the by-id `--resume` form skips the guard. A wake
  whose turn then fails or times out re-opens its reply for
  next-boundary injection: the reply is marked consumed at dispatch,
  so a dead turn previously left it silently swallowed and delivered
  nowhere.
- A console option tap no longer reads as an error inside the asking
  process: the console ask endpoint closes the thread before it
  records the reply row, and `tug ask` now treats the reply row it
  sees after that close as the answer (matching the thread's
  recorded key) instead of surfacing an already-answered race error.
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
