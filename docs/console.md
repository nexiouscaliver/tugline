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
- Live stream: the newest events with time, event name, session id,
  and title. Each notifiable row carries a delivery chip: pending
  until the daemon handles the record, delivered after (delivered
  means handled, published or not). userpromptsubmit and
  sessionstart records never notify, so they show no chip rather than
  a false pending.
- Asks: every open approval ask with Approve and Deny buttons and a
  reply link, plus an answered and expired list underneath. The
  buttons write the same decision files the approve receiver reads,
  so a press on the page and a press on a phone notification have the
  same effect.
- Reply: a text box with an explicit target line: reply to session
  <id>, reply via thread <id>, or no target selected. Pick a target by
  clicking a session row or an ask reply link; Cmd+Enter or
  Ctrl+Enter sends. A sent reply lands in the store and is appended
  to `~/.tugline/replies.jsonl` in the same format the daemon writes
  for phone replies.
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
| GET `/api/state` | one render in one read: sessions (decorated with `display_state` and a collision-proof `display_id`, active-now first), the last 50 events, threads, the last 20 replies, headline counts, health, spool depth, last ingest time |
| GET `/api/sessions` | all sessions with the same decoration and ordering as `/api/state` |
| GET `/api/events?limit=50&session=<id>` | events newest first, optionally scoped to one session; a non-integer limit is a 400 |
| GET `/api/threads?status=open` | approval threads, newest first, optionally one status |
| GET `/api/replies?limit=50` | replies, newest first; a non-integer limit is a 400 |
| GET `/api/events/stream` | server-sent events: one `snapshot` on connect, then a `delta` after each ingest pass (about every 2 seconds), a `: ping` keepalive when 15 seconds pass with nothing to send |
| POST `/api/replies` | body must be a JSON object with a string `body` and exactly one target, `thread_nonce` or `session_id`, or 400; an unknown thread nonce is a 404; records the reply and appends the replies.jsonl line |
| POST `/api/approvals/<nonce>?decision=allow` | `decision` must be `allow` or `deny` or 400; an unknown nonce is a 404; an already answered nonce returns 200 with `status: already answered` and never flips the recorded decision |
| PUT `/api/sessions/<id>/label` | body must be a JSON object with a string `label`: plain text (no control characters), trimmed, at most 60 characters, or 400; an empty label clears it; an unknown session id is a 404. Sets the row's operator label, the primary name the page shows |

Event queries are ordered by timestamp, not insertion: spool records
can arrive out of order, and the store sorts them so the stream reads
chronologically.

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
