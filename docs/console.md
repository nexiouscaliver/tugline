# The console

`tug console` serves one web page on 127.0.0.1 that shows everything
tugline knows about your agent sessions: which sessions are running,
which are waiting on you, the live event stream, the open approval
asks, and the replies you have sent. It reads the same home directory
the daemon and the hooks use, so there is nothing to sync and nothing
extra to configure. The store folds the whole spool on start, so past
sessions are on the page even when the console was not running while
they happened.

## What the page shows

- Sessions: one row per session id seen in the spool, sorted with
  active-now sessions first, then everything else by recency. Each
  row carries a name you can set: click rename and type (Enter
  saves, Escape cancels, an empty value clears it); the label, when
  set, is the row's primary name and the short id drops to a small
  secondary line underneath. Labels live in the store, so they
  survive restarts and re-ingest. Short ids are computed per page
  load to be unique: when two sessions would render the same tail,
  the id lengthens until they no longer collide (the full id in the
  worst case), so you never see two rows with the same short name.
- Tags: every session row also carries tag chips underneath the
  name. Click a chip's x to remove it, or the tag link to add one
  (Enter saves, Escape cancels). A tag is lowercase letters, digits,
  and hyphens only, at most 24 characters, at most 8 tags per
  session; anything else you type is dropped character by character
  (spaces and underscores disappear rather than becoming hyphens).
  Tags are for your reference and for reply targeting: agents can
  set their own from the CLI (see Naming sessions from a shell
  below), so a session can hang its name on itself the moment it
  starts.
- Auto-identity: sessions name themselves with zero operator work.
  The first prompt record auto-labels a session (the first 5-7 words
  of the prompt, ASCII, at most 40 characters), and the moment the
  store first sees a session it auto-tags it with its adapter and
  its project label. Auto-labeling happens once and never
  overwrites: an operator label always wins, and clearing the label
  pauses auto-labeling just as much as setting one. Auto-tags land
  only at first sighting, so removing one is permanent.
- One display rule, everywhere: the label when set, else a
  collision-proof unique suffix of the session id (lengthened until
  no other session ends with it). The leading `sess_...` characters
  are never a display id. The store exports the rule as
  `tugline.core.store.session_display_id` and attaches the result to
  every session row (`display_id`) and every event row
  (`label`, `tags`, `display_id`), so each stream row answers who
  did what where on its own.
- Session state is derived from recency, not just the last event:
  active now (green) when the session was seen within five minutes;
  running (green) while it keeps producing events inside a thirty
  minute window; idle (gray) once a running session has been silent
  for more than thirty minutes (a session whose last event is hours
  old never reads as running); waiting on me (amber) when the newest
  event is a permissionrequest, however old, because its age measures
  operator inaction; done (gray) after a stop. The store keeps the
  raw facts; the console derives what you see per response.
- The adapter and project columns fill in when hook records carry
  them. Click the session name to filter the live stream to that
  session and to aim the reply box at it.
- Live stream: the newest events with time, event name, session
  name, and title. Every row renders the session's display name (the
  label when set, else the collision-proof short id) straight from
  the enriched event row, and desktop rows add small adapter and
  project badges under the name when the record carries them (phone
  rows show the name only, to stay readable). Each notifiable row
  carries a delivery chip: pending until the daemon handles the
  record, delivered after (delivered means handled, published or
  not). userpromptsubmit and sessionstart records never notify, so
  they show no chip rather than a false pending. A group by session
  chip in the panel header toggles between the flat newest-first
  list and sections grouped by session, newest activity first, each
  with a header (display name, state chip, relative time) whose tap
  aims the sessions filter bar at that one session; the choice lives
  in memory, so a fresh page starts flat.
- Asks: every open approval ask with Approve and Deny buttons and a
  reply link, plus an answered and expired list underneath. The
  buttons write the same decision files the approve receiver reads,
  so a press on the page and a press on a phone notification have the
  same effect. Multiple choice asks (`tug ask`) render as question
  cards under the approval cards: the question text in full and each
  lettered option a full-width tappable row (the letter in
  monospace, a 48px floor). A tap POSTs to `/api/asks/<nonce>`,
  which records the answer exactly like a bound reply and closes the
  thread; the option rows disable while the request is in flight,
  a failure shows an inline error and re-enables them, and the
  settled list shows the chosen letter in green (see Asking
  questions below).
- Reply: a text box with an explicit target line: reply to session
  <name> (label first, short id second, so you always see exactly
  where it goes), reply via thread <id>, or no target selected. Pick
  a target by clicking a session row or an ask reply link; Cmd+Enter
  or Ctrl+Enter sends. A sent reply lands in the store and is
  appended to `~/.tugline/replies.jsonl` in the same format the
  daemon writes for phone replies, plus the session binding, so the
  wake dispatcher can act on it (see Wake on reply below).
- Wake state: each recent reply carries a chip with what the wake
  dispatcher did with it: dispatched (the session's driver was
  spawned with the reply as its prompt), finished (the driver exited;
  the chip replaces dispatched), timed_out (the per-wake timeout
  killed it), unsupported (the target session's adapter has no wake
  driver), or skipped (the session was busy or a wake was already in
  flight, so the reply waits for the session's own next-boundary
  injection). No chip means no wake decision was ever recorded for
  that reply, which is the honest reading for anything sent before
  the feature or while the daemon is stopped.
- Header health: daemon running, stopped, or unknown; ntfy ok or
  unreachable; how many spool records are pending; how long since the
  last ingest; and a live or reconnecting chip for the event stream
  connection.

The page is one HTML file, one stylesheet, and one script, served
from the console itself: no external assets, no CDN, no JavaScript
libraries. It renders at 390px (a phone) and 1440px (a desktop).

## Running it

```
tug console
```

Example output:

```
tug console listening on http://127.0.0.1:2588/
http://127.0.0.1:2588/
```

SIGINT or SIGTERM stops it. `--open` opens the page in your default
browser, and `--port` overrides the configured port for one run.

The switch and the port live in `~/.tugline/config.toml`:

```toml
[toggles]
console = true

[server]
console_port = 2588
```

The toggle defaults to on. With `console = false`, `tug console`
prints one line:

```
console is off in config, nothing to serve
```

and exits 0; nothing listens. `console_port = 0` binds a free port and
the startup line names it, which is also what `--port 0` does.

For an isolated home (a demo, a second instance, a test):

```
TUGLINE_HOME=~/tmp/tugline-demo tug console
```

TUGLINE_HOME moves the whole tree: spool, state, store, and replies
all land under that root, leaving `~/.tugline` alone.

## The API

Everything the page uses is plain HTTP on the same port, so you can
drive it with curl. GETs are the reads; POSTs are the writes. When
the configured token file holds a token, every call in the table
requires it, as a Bearer header or a `?token=` parameter (see
Security below).

| Method and path | What it does |
| --- | --- |
| GET `/` | the console page |
| GET `/api/state` | one render in one read: sessions (decorated with `display_state` and a collision-proof `display_id`, active-now first, each with its `tags`), the last 50 events, threads, the last 20 replies, headline counts, health, spool depth, last ingest time |
| GET `/api/sessions` | all sessions with the same decoration, `tags`, and ordering as `/api/state` |
| GET `/api/events?limit=50&session=<id>` | events newest first, optionally scoped to one session; every row also carries its session's `label`, `tags`, and `display_id` (the one display rule: label else unique suffix), so a stream row answers who did what where on its own; a non-integer limit is a 400 |
| GET `/api/threads?status=open` | threads newest first, optionally one status; ask rows (`kind: "ask"`) come with their lettered `options` parsed from JSON plus the `status`/`answer` state, approve rows carry `options: null` |
| GET `/api/replies?limit=50` | replies, newest first, each row decorated with its `wake` state (`{state, ts}` from the wake dispatcher journal, or null when no decision was recorded); a non-integer limit is a 400 |
| GET `/api/events/stream` | server-sent events: one `snapshot` on connect, then a `delta` after each ingest pass (about every 2 seconds), a `: ping` keepalive when 15 seconds pass with nothing to send |
| POST `/api/replies` | body must be a JSON object with a string `body` and exactly one target, `thread_nonce` or `session_id`, or 400; the `session_id` value is resolved as an exact session id first, then a label exactly one session carries, then a tag exactly one session carries; an ambiguous label or tag is a 400 whose `sessions` key lists every matching session id, an unknown name a 404, an unknown thread nonce a 404; records the reply against the resolved session and appends the replies.jsonl line |
| POST `/api/approvals/<nonce>?decision=allow` | `decision` must be `allow` or `deny` or 400; an unknown nonce is a 404; an already answered nonce returns 200 with `status: already answered` and never flips the recorded decision |
| PUT `/api/sessions/<id>/label` | body must be a JSON object with a string `label`: plain text (no control characters), trimmed, at most 60 characters, or 400; an empty label clears it; an unknown session id is a 404. Sets the row's operator label, the primary name the page shows |
| PUT `/api/sessions/<id>/tag/<tag>` | attaches one tag to the session: normalized to lowercase [a-z0-9-] (invalid characters dropped), at most 24 characters, at most 8 tags per session, or 400; an unknown session id is a 404; attaching a tag already present is a no-op 200. Returns the session row with its `tags` |
| DELETE `/api/sessions/<id>/tag/<tag>` | detaches one tag; removing a tag that is not attached is still a 200; an unknown session id is a 404. Returns the session row with its `tags` |

Event queries are ordered by timestamp, not insertion: spool records
can arrive out of order, and the store sorts them so the stream reads
chronologically.

## Wake on reply

A reply you send from the console used to wait for its session's next
turn boundary to be seen. With wake-on-reply the daemon closes that
gap: when a reply bound to a session lands while that session looks
idle (its last spooled event was a stop or a session start), the
daemon spawns the session adapter's driver with the reply body as the
prompt, resuming the session on the spot. The reply is marked
consumed the moment the driver spawns, so the next-boundary injection
never double-delivers it.

The pieces:

- The dispatcher runs inside the daemon, one bounded pass per cycle,
  after the spool flush. It only considers replies carrying a session
  binding, which today means replies sent from the console; a
  free-form phone reply (no target) stays on the injection path.
- Session facts (adapter, cwd, idleness) come from the store the
  console's ingest thread keeps current, so a session started after
  the last console restart is addressable once its first spool record
  lands.
- One wake per reply id, one wake in flight per session, a global
  concurrent cap (`wake.max_concurrent`, default 1), and a per-wake
  timeout that kills the driver (`wake.timeout_seconds`, default
  1800). Every decision and every exit is journaled to
  `~/.tugline/state/wake.jsonl` and shown as the wake chip on the
  reply.
- `toggles.wake = false` restores the pre-wake behavior exactly:
  nothing spawns, nothing is marked, the chip never appears.
- `wake.dry_run = true` (the shipped default) logs and journals the
  exact command without spawning anything, so you can verify what
  would run before arming it. Flip it to false in
  `~/.tugline/config.toml` once the journal looks right.

The zcode driver, stated plainly: idle zcode sessions wake through
the CLI the ZCode desktop app bundles, invoked as
`node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs
--resume <sessionId> -p "<reply body>" --cwd <project dir>`
(v0.16.9; every flag verified against the bundled CLI's own help:
`-p, --prompt <text>` runs a single prompt without opening the TUI,
`--resume <sessionId>` resumes a persisted session, `--cwd <path>`
sets the working directory; `--mode` is deliberately not passed
because help documents yolo as the default permission mode for
`--prompt`). Because that path is the app-bundled CLI, the driver is
version-locked to the installed ZCode app: if an app update moves or
renames the bundle, re-point `wake.zcode_command` in
`~/.tugline/config.toml`. Both drivers ship enabled
(`wake.adapters` defaults to `["claude", "zcode"]`), each with its
config-driven command template (`wake.claude_command`,
`wake.zcode_command`); an adapter with no driver entry logs
`wake unsupported for adapter X`, shows the unsupported chip, and
stays unconsumed until the session's own next-boundary injection
picks it up.

## Asking questions

`tug ask` is the multiple choice ask primitive: an agent poses one
question with lettered options, you answer from wherever you are,
and the command prints your choice to its stdout and exits 0.

```
tug ask --session <id> \
        --option "A: ship the release" \
        --option "B: roll back and investigate" \
        --timeout 300 \
        "which deploy path?"
```

- Options are lettered choices, 2 to 8 of them. An explicit letter
  prefix wins (`"A: ship"`), bare options are lettered automatically
  (`A`, `B`, `C`, ...). The timeout defaults to 300 seconds and tops
  out at 1800.
- The ask lands in the store as a thread with `kind: "ask"` and the
  options as JSON, then pushes one HIGH priority notification titled
  `[<harness>] <project> - asks: <question>` (the same enriched
  title helpers every other notification uses) whose body lists the
  lettered options and the reply guidance.
- The command then blocks, polling for an answer. On an answer it
  prints `key: text` (for example `B: roll back and investigate`)
  and exits 0. On timeout it prints `no answer`, exits 1, and the
  thread is marked `expired`. Nothing in the flow ever tracebacks:
- Answers, in the order they are checked each poll:
  1. a console option tap or a reply bound to the thread nonce, or a
     reply aimed at the session by id, label, or tag (the same
     id > label > tag targeting the reply box uses);
  2. a reply journal line (an ntfy reply) whose body is exactly one
     option key and whose thread/session binding resolves to this
     ask thread: a binding is required, so a bare free-form ntfy
     reply never answers an ask, and a session-bound reply lands on
     the session's newest open ask so two concurrent asks never
     steal each other's answers;
  3. `tug answer --thread <nonce> <key>` from another terminal
     (the agent or you).
- A reply whose body is not exactly one option key (`"Z"`, `"A:
  ship it please"`) is rejected: the ask stays open and keeps
  waiting. Answering with a key the ask does not carry is a clean
  error, never a wrong answer.

The console contract (for the page): threads with `kind: "ask"`
render with their `options` array (`[{key, text}, ...]`) and their
answer state (`status` open/answered/expired, `answer` the chosen
key). A tap POSTs `{"key": "<letter>"}` to `/api/asks/<nonce>`,
which records the answer exactly like a bound reply (a console
reply row plus the replies.jsonl line, body the bare option key)
and closes the thread itself, so the card flips without waiting
for the asking process to poll; an already-answered ask answers
200 again with the recorded key and never writes a second reply
row, an expired ask answers 409, an unknown nonce 404. A reply
sent through the normal reply endpoints with the thread nonce (or
the session as the target, body just the letter) answers the ask
too. Session rows and event rows carry
`label`, `tags`, and `display_id`; the display id is the one rule
(label else collision-proof suffix) exported as
`tugline.core.store.session_display_id`, and templates should render
exactly that.

## Naming sessions from a shell

Labels and tags are not console-only: the `tug` CLI writes the same
store, so an agent can name or tag its own session (agents know their
session id from context, and the id is always an explicit flag; there
is no environment fallback):

```
tug label --session <id> "night build"   # set the label
tug label --session <id> --clear         # clear it
tug tag --session <id> build w7          # attach one or more tags
tug untag --session <id> build           # detach them
tug sessions                             # list: id, label, tags, state
tug sessions --label "night build"       # filter by exact label
tug sessions --tag build                 # filter by tag
tug sessions --json                      # one JSON array instead
```

Every naming command first folds the spool into the store once, the
same idempotent pass the console performs, so the commands work on a
fresh home even when no console is running. An unknown session id
exits 2 with a note that sessions come from spooled hook events; tags
follow the same normalization and limits as the API (lowercase
[a-z0-9-], at most 24 characters, at most 8 per session, invalid
characters dropped).

Naming is also automatic: the store itself auto-labels a session from
its first prompt (first 5-7 words, once, operator labels always win
and pause the behavior) and auto-tags it with its adapter and project
at first sight. The commands above are for renaming and refinement on
top of that, not a prerequisite for a session to be recognizable.

Name-based reply targeting works from curl too, exactly as the reply
box does it: pass the label or tag as `session_id` in the POST body
and the console resolves it (id first, then unique label, then unique
tag; ambiguity is a 400 listing the matching sessions).

## Security

- The console binds 127.0.0.1 only. Nothing listens on any other
  interface, on any port.
- Every API endpoint, read or write, carries the same bearer posture
  as the approve receiver: when the configured token file exists and
  holds a token, a request without the token gets a 403. The token is
  accepted as an `Authorization: Bearer <token>` header or a
  `?token=<token>` query parameter, because phone browsers cannot set
  headers on an EventSource. With no token on file (the default),
  reads and writes are open to whatever can already reach loopback.
- The page itself is a static shell with no data, so it serves
  without a token. When the page URL carries `?token=`, the page
  captures it and appends it to every API and stream call; when a
  call comes back 403 instead, a paste-token bar appears. The page
  cannot show any data without a valid token either way.
- The token is never rendered. No API response and no page element
  includes the token or the token file contents.

## From a phone over a tailnet

Loopback means the page is local to the machine serving it. If that
machine and your phone share a tailnet, port-based forwarding works:
one `tailscale serve` rule pointing at the console port gives the page
an HTTPS name your phone can open. Path-based serve rules are not
available on every client, so prefer the port form. This is a
suggestion, not a setup tugline manages; think through what you expose
the same way you would for the approve receiver (see
[docs/adapters.md](adapters.md)).

Exposing the console on a tailnet REQUIRES the token file: configure
`token_file` in `~/.tugline/config.toml` with a token in it before
you add the serve rule, so the console's reads and writes are gated.
Share the page URL with the token in it
(`https://<tailnet-name>/?token=<token>`); the page captures it once
and every call after that carries it, and a phone user without the
link gets the paste-token bar instead of data.
