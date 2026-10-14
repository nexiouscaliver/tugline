# tugline

Push notifications and phone replies for coding-agent sessions, over ntfy.

## What it does

- Tells you when an agent session finishes a turn, goes idle, or blocks on a
  permission prompt.
- Takes replies from your phone: whatever you send to the reply topic is
  streamed into a local inbox file, ready for the next prompt.
- Can put Approve and Deny buttons on permission notifications. This is the
  approve toggle, off by default; the daemon answers the presses on a
  loopback receiver. See [docs/adapters.md](docs/adapters.md) for the
  receiver port, the external_url setting, and how to expose it to your
  phone safely.
- Lets an agent ask you a multiple choice question and waits for the
  answer: `tug ask --session <id> --option "A: ship" --option "B: roll
  back" "which deploy?"` pushes one high priority notification, blocks
  up to the timeout (default 300s), prints the chosen option, and exits
  0 (exit 1 with `no answer` on timeout). Answers come from the console
  (a tap or a reply), a bound ntfy reply whose body is just the letter,
  or `tug answer --thread <nonce> <key>`.
- Names every session by itself: the first prompt auto-labels a session,
  the first sighting auto-tags it with its adapter and project, and one
  display rule (the label, else a collision-proof short id) renders the
  same name everywhere.

The local console is the at-a-glance side of the same machine: one web
page of sessions and their states, the live event stream, open approval
asks with Approve and Deny, and a reply box. `tug console` serves it on
loopback. See [docs/console.md](docs/console.md).

The phone client is the stock ntfy app. tugline ships no app of its own and
runs no hosted service: messages travel from your machine to an ntfy server
you point at, and that is the whole path.

## The 60-second taste

```
uvx --from git+https://github.com/nexiouscaliver/tugline tug test
```

The first run writes a config template at `~/.tugline/config.toml` (server
URL `http://127.0.0.1:2586` by default) and then tries to publish one test
message to the topic `tug`. It prints the HTTP status against a live server,
or the connection error against a dead one:

```
HTTP 200
```

```
error: request failed: [Errno 61] Connection refused
```

Edit `[server] url` in `~/.tugline/config.toml` to point at your ntfy server
(self-hosted or `https://ntfy.sh`), install the ntfy app on your phone,
subscribe to the topic `tug`, and run the command again. Your phone buzzes,
or you have a config problem to fix before anything else.

## Install

Python 3.10 or newer, installed through uv:

```
uv tool install git+https://github.com/nexiouscaliver/tugline
tug --version
```

No runtime dependencies. Everything runs on the standard library.

## Quickstart

The full 10-minute path lives in [docs/quickstart.md](docs/quickstart.md).
The short version, against a server you already run:

```
tug test                      # one message, prints the HTTP code
tug install zcode             # wire zcode hooks (see adapters doc)
tug daemon                    # flush the spool, stream replies
TUGLINE_SESSION=demo tug arm  # the next Stop for demo notifies
tug status                    # spool depth and the last phone reply
```

## How the pieces fit

Hooks never talk to the network. They append one line to a local spool and
return; the daemon does all the publishing with retry and backoff; replies
come back the same way, into a local file.

```
 harness events                  your machine                    phone
 --------------   ---------------------------------------     ----------
 zcode hooks
 Claude Code  -->  tug hook-zcode / tug hook-claude
 any CLI          tug notify
                        |
                        v
                    spool: ~/.tugline/spool/*.jsonl
                    (one JSON line per event)
                        |
                    tug daemon
                    publish with retry + backoff
                        |
                        v
                    your ntfy server, topics
                    tug and tug-warn    ---------------->   ntfy app
                                                              |
                    tug-reply topic     <----------------  you reply
                        |
                        reply stream (held subscribe)
                        |
                        v
                    ~/.tugline/replies.jsonl
                    tug status prints the last reply
                        |
                    wake dispatcher (idle session + bound reply)
                        |
                        v
                    claude --resume <session> -p "<reply>"
                    (or: queued for next-boundary injection)
```

## Adapters

- zcode: `tug install zcode` wires SessionStart, Stop, and PermissionRequest
  into `~/.zcode/cli/config.json`. `tug uninstall zcode` takes them back out.
- Claude Code: no installer command yet; paste one hook block into
  `~/.claude/settings.json`. The exact block is in
  [docs/adapters.md](docs/adapters.md).
- Any CLI tool: `tug notify --title "build done" --body "green"` queues a
  notification from a script, a cron job, or a make target.
- Codex: a two-line recipe using its `notify` config,
  [docs/recipes/codex.md](docs/recipes/codex.md).
- Aider: one flag, `--notifications-command`,
  [docs/recipes/aider.md](docs/recipes/aider.md).

Install, uninstall, arming, the idle threshold, and the approve toggle are
covered in [docs/adapters.md](docs/adapters.md).

## Three channels, one topic set

The spool makes delivery durable on your machine; the last hop to your phone
still depends on how your phone can reach the ntfy server. Three working
channels: your LAN (self-hosted ntfy on the local network), a tailnet (the
server reachable from anywhere over Tailscale or another VPN, optionally
fronted by a serve bridge for TLS), or a public URL (a proper HTTPS reverse
proxy in front of ntfy, or ntfy.sh itself). Pick one live server URL per
location: the ntfy app cannot dedupe one topic subscribed twice against two
server URLs, so subscribe one URL per place you sit, or you will see split
brain or double buzzes. Details, the subscription matrix, and reboot notes:
[docs/channels.md](docs/channels.md).

## Wake on reply

The whole point of replying from the phone is that the session acts
on it now, not at whatever turn boundary comes next. tugline delivers
a reply two ways, and they compose:

- Running session: the reply is injected into the session's context
  at its next prompt (the per-turn injection), so the work continues
  inside the turn you are already watching.
- Idle session (last spooled event a stop or a session start): the
  daemon's wake dispatcher spawns the adapter's driver with the reply
  body as the prompt, resuming the session on the spot. For claude
  sessions that is `claude --resume <session> -p "<your reply>"`
  (flag order verified against the CLI's own help), run in the
  session's project directory. The woken reply is marked consumed so
  the injection path never double-delivers it.

Rails: one wake per reply, one wake in flight per session, a global
concurrent cap (default 1), a per-wake timeout that kills the driver
(default 1800s), and every decision journaled to
`~/.tugline/state/wake.jsonl` and visible as a wake chip on the
console's reply list. `toggles.wake = false` restores the pre-wake
behavior exactly. `wake.dry_run = true` (the shipped default) records
the exact command without spawning anything: check
`~/.tugline/state/wake.jsonl`, then flip it to false to arm.

One honest limitation: this change ships no zcode wake driver. The
ZCode desktop app does bundle a CLI (ZCode.app/Contents/Resources/
glm/zcode.cjs, v0.16.9) whose help documents headless resume flags
(`-p, --prompt <text>`, `--resume <sessionId>`, `--cwd <path>`), but
wiring that driver is a queued follow-up lane; until it lands,
replies to idle zcode sessions are logged unsupported and stay queued
for the session's next-boundary injection. The driver registry
(`wake.adapters`) and the claude command template
(`wake.claude_command`) are config-driven, so the zcode driver is an
additive change when it lands. Details:
[docs/console.md](docs/console.md#wake-on-reply).

## Privacy

Messages stay on the ntfy server you configure. tugline makes exactly two
kinds of outgoing network call, both to `[server] url`: publish a
notification, and hold the reply stream. When the approve toggle is on,
the daemon additionally listens on `127.0.0.1` (the `approve_port`
setting) for the button presses; it never listens on any other
interface. There is no telemetry, no account, and no hosted
service. One honest caveat for iOS: the stock ntfy iOS app gets instant
delivery through the Apple Push Notification service, and a self-hosted
server reaches APNs only if you configure it to forward through the ntfy.sh
upstream relay. With the relay on, Apple and ntfy.sh see delivery metadata
for your messages. With the relay off, iOS delivery is delayed polling. The
Android app connects to your server directly, no relay involved.

## License

MIT. See [LICENSE](LICENSE).
