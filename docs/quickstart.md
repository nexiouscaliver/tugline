# Quickstart

Ten minutes from nothing to a phone that buzzes when your agent needs you.
Every command here is copy-pasteable. Example outputs are labeled as
examples; your ids and paths will differ.

## 1. Install the CLI

Python 3.10 or newer, through uv:

```
uv tool install git+https://github.com/nexiouscaliver/tugline
tug --version
```

Example output:

```
tugline 0.1.0
```

No runtime dependencies. The `tug` command is the whole surface.

## 2. Have an ntfy server

You need an ntfy server you can publish to. Two options:

- The public one: `https://ntfy.sh`. Nothing to run. Topic names are the
  only access control there, so pick unguessable topic names (see
  [SECURITY.md](../SECURITY.md)).
- Your own: any Linux box can run one. The hosting sketch in
  [docs/hosting/deadpi.md](hosting/deadpi.md) shows a minimal setup. Note
  that macOS is a poor host for the ntfy server: the Homebrew formula
  installs the client only, and the server is unsupported on macOS.

## 3. Point tugline at the server

Run any command once to create the config template:

```
tug status
```

First run writes `~/.tugline/config.toml`. Edit `[server] url`:

```toml
[server]
url = "https://ntfy.sh"
```

Keep the other sections as written; keys are optional but section headers
are required, and anything unknown is a loud config error, not a silent
ignore.

## 4. Prove the path

```
tug test
```

This publishes one message straight to the server (bypassing the spool) and
prints the result:

```
HTTP 200
```

If your server requires a token, put it in the token file first (one line,
no quotes):

```
echo "tk_yourtoken" > ~/.tugline/credentials
chmod 600 ~/.tugline/credentials
```

Then `tug test` again until it prints `HTTP 200`.

## 5. Subscribe on the phone

Install the ntfy app (Android: Play Store or F-Droid; iOS: App Store), add a
subscription for the topic `tug`, and run `tug test` once more. The phone
buzzes. If it does not, fix this step before going further; nothing later in
this guide can work if the phone cannot see the topic.

## 6. Start the daemon

```
tug daemon
```

The daemon flushes the spool to ntfy (retry with exponential backoff, five
attempts per record) and streams the reply topic. Leave it running in a
terminal for now. For a single cycle, useful in scripts:

```
tug daemon --once
```

Example output:

```
handled 2, replies 1
```

SIGTERM stops it cleanly.

## 7. Feel the notify path

Queue a notification by hand and let the daemon deliver it:

```
tug notify --title "build done" --body "all green"
```

`tug notify` prints the spool record id and returns immediately; it never
touches the network. The daemon publishes it on the next cycle. Priorities
are `min`, `low`, `default`, `high`, `urgent`:

```
tug notify --title "nightly" --body "suite failed" --priority high --topic warn
```

`--topic` takes the logical names `main` and `warn`, or any raw ntfy topic
name.

## 8. Arming and Stop pings

Stop events are quiet by design, so a chatty agent does not buzz you every
turn. Two ways a Stop turns into a notification:

- The session is armed: `tug arm <session-id>` (or `TUGLINE_SESSION=<id>
  tug arm`). One shot; the daemon clears the marker once that Stop is
  delivered.
- The turn ran longer than the idle threshold: `[policy] idle_seconds` in
  config, default 600. This needs UserPromptSubmit records in the spool,
  which the stock adapter installs do not wire; add that hook event
  yourself if you want threshold pings.

Disarm without waiting for a Stop:

```
tug disarm <session-id>
```

## 9. Feel the reply path

In the ntfy app, subscribe to the reply topic (default name `tug-reply`)
and send a message to it. The daemon's stream picks it up within a cycle and
appends one JSON line to `~/.tugline/replies.jsonl`. Check it landed:

```
tug status
```

Example output:

```
service: unknown (no instance.json)
spool: 3 records on file, 0 undelivered
last reply: ship it (consumed: no)
```

Replies sit in that file with a `consumed` flag. In 0.1.0 nothing feeds
them into the agent for you: read the inbox, put the text into your next
prompt. Automating that handoff is future work.

## 10. Wire your harness

- zcode: `tug install zcode`, then restart running sessions (hooks are
  snapshotted at session start).
- Claude Code: paste the hook block from
  [docs/adapters.md](adapters.md) into `~/.claude/settings.json`, then
  restart sessions.
- Anything else: call `tug notify` from a script. Recipes for Codex and
  Aider live in [docs/recipes/](recipes/).

Where to go next: [docs/adapters.md](adapters.md) for the full adapter
behavior, [docs/channels.md](channels.md) for making the phone hop durable,
[SECURITY.md](../SECURITY.md) before you expose anything publicly.
