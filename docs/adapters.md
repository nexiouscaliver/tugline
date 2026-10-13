# Adapters

An adapter is the glue between one harness and the tugline core: it turns a
harness event into one spool record and gets out of the way. Publishing,
retrying, reply polling, and state all live in the core, so every adapter
behaves the same once the record is in the spool.

Two rules hold everywhere:

- Hooks never block a session. Read stdin under a hard 2 second ceiling,
  append one line to the spool, exit 0. Failures log one line under
  `~/.tugline/logs/` and vanish.
- Hooks never talk to the network. If ntfy is down, the record waits in the
  spool and the daemon retries.

## zcode

### Install

```
tug install zcode
```

Optional flag: `--config PATH` to target a different config (default
`~/.zcode/cli/config.json`).

The installer appends one hook entry per event under `hooks.events`:

- SessionStart, timeout 10
- Stop, timeout 10
- PermissionRequest, timeout 310

Each entry runs `tug hook-zcode <Event> # tugline:zcode`. The trailing
comment is the ownership marker; the uninstaller matches it exactly. The
file is edited by splicing bytes, so everything you had before is still
there after, a timestamped backup is written first, and a second install
run detects its own marker and changes nothing. If the file is not valid
JSON or its structure does not scan cleanly, the installer refuses to
touch it.

### Uninstall

```
tug uninstall zcode
```

Removes exactly the entries carrying the marker. Containers the installer
created are taken back out when they empty, so install then uninstall
restores the original file byte for byte.

### Session restart caveat

Hooks are snapshotted when a session starts. Sessions that were already
running when you installed keep the old hook set and will not notify until
you restart them. Both installers print this warning once; it is the number
one "why is nothing buzzing" cause.

### What the events do

- SessionStart notifies only when the payload says source is resume. A
  fresh startup is not worth a ping.
- Stop goes to the spool; the arming policy (below) decides whether it
  notifies.
- PermissionRequest always notifies: high priority, on the warn topic. This
  is the "the agent is blocked" signal, so it ignores arming entirely.

The hook reads the session id from the payload's `session_id`, falling back
to the `CLAUDE_SESSION_ID` environment variable. `TUGLINE_ZCODE_HOME`
relocates the tugline home for the installer and hook (tests use it; you
rarely need it).

## Claude Code

There is no installer command for this adapter yet; the installer and
uninstaller exist as library functions
(`tugline.adapters.claude.install` / `.uninstall`) but no `tug` subcommand
wraps them in 0.1.0. Until one lands, paste this into
`~/.claude/settings.json`, merging under `hooks` if you already have hooks
there:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "tug hook-claude SessionStart # tugline:claude", "timeout": 20 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "tug hook-claude Stop # tugline:claude", "timeout": 20 } ] }
    ],
    "Notification": [
      { "hooks": [ { "type": "command", "command": "tug hook-claude Notification # tugline:claude", "timeout": 20 } ] }
    ],
    "PermissionRequest": [
      { "hooks": [ { "type": "command", "command": "tug hook-claude PermissionRequest # tugline:claude", "timeout": 320 } ] }
    ]
  }
}
```

That block is byte for byte what the installer appends. To uninstall,
delete the groups whose command strings contain `tugline:claude` and leave
everything else in place.

What the events do:

- SessionStart: notifies only on a resume, low priority.
- Stop: spooled; the arming policy decides.
- Notification: the message text is classified (contains "waiting for your
  input" -> idle prompt; "finished" or "completed" -> turn complete;
  anything else -> generic) and the classification rides in the title.
- PermissionRequest: high priority warn record, plus the approve flow
  below when the toggle is on.

The same restart caveat applies: Claude Code snapshots hooks at session
start, so restart running sessions after editing settings.

`TUGLINE_HOME` relocates the whole tugline tree for this adapter (spool,
state, logs).

## Generic CLI

Any tool that can run a shell command can notify:

```
tug notify --title "build done" --body "all green"
tug notify --title "nightly" --body "suite failed" --priority high --topic warn
tug notify --title "deploy" --body "staging is up" --topic my-raw-topic
```

Flags: `--title` and `--body` are required; `--topic` takes `main`, `warn`,
or any raw ntfy topic (default `main`); `--priority` is one of `min`, `low`,
`default`, `high`, `urgent` (default `default`); `--session` records a
session id, defaulting to `$TUGLINE_SESSION` then `manual`. The command
spools only; the daemon publishes. Direct notify records always publish,
regardless of arming.

Recipes for two specific tools live next door:
[Codex](recipes/codex.md), [Aider](recipes/aider.md).

## Arming and the idle threshold

Stop events are quiet unless something says otherwise. The policy module
owns the rules:

- `tug arm <session-id>` (or `TUGLINE_SESSION=<id> tug arm`) writes a one
  line marker at `~/.tugline/state/armed/<session-id>`. The next Stop for
  that session notifies, and the daemon clears the marker after delivering
  it. One shot on purpose: arm the sessions you care about, not all of
  them.
- The idle threshold: a Stop whose turn ran longer than
  `[policy] idle_seconds` (default 600) notifies even when not armed. The
  turn length comes from UserPromptSubmit to Stop pairs in the spool.
  Neither stock install wires UserPromptSubmit, so with a stock install
  this rule never fires; add that hook event to your config if you want
  threshold pings.
- `tug disarm <session-id>` clears the marker early.
- PermissionRequest notifies always; arming is irrelevant for it.

## The approve toggle

`[toggles] approve` in `~/.tugline/config.toml`. Default `false`.

What the toggle does when on, per adapter, on a PermissionRequest:

- zcode: the hook spools one urgent record on the warn topic carrying
  Approve and Deny http action buttons targeting
  `<server url>/approve?session=<id>&nonce=<n>` and the matching `/deny`.
  It then polls `~/.tugline/state/approvals/<nonce>.json` every 0.5 s for a
  decision. Default wait 120 s, hard cap 300 s, overridable with the
  `TUGLINE_ZCODE_APPROVE_TIMEOUT` environment variable (clamped to the
  cap). The installed hook timeout is 310 s, sized to the cap plus margin.
- Claude Code: same shape, buttons POST to
  `<server url>/approvals/<id>?decision=allow` or `decision=deny`, default
  wait 120 s, hard cap 300 s. The pasted hook timeout is 320 s.

Fail closed, in every failure mode: on timeout, on a missing or broken
spool, on any error, the hook prints nothing and exits 0, and the harness
falls back to its own local permission prompt. Nothing in this code path
ever auto-allows.

Honest status for 0.1.0: the receiving side, the thing that answers those
button URLs and writes the decision files, is not in this repository yet.
With the toggle on today you get the urgent button-bearing notification and
a bounded wait that ends in the local prompt. Leave the toggle off until
that side ships; that is why it ships off.
