# tugline

Push notifications and phone replies for coding-agent sessions, over ntfy.

## What it does

- Tells you when an agent session finishes a turn, goes idle, or blocks on a
  permission prompt.
- Takes replies from your phone: whatever you send to the reply topic is
  polled into a local inbox file, ready for the next prompt.
- Can put Approve and Deny buttons on permission notifications. This is the
  approve toggle, off by default; see [docs/adapters.md](docs/adapters.md)
  for exactly what is wired in 0.1.0.

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
tug daemon                    # flush the spool, poll for replies
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
                    reply poll (long poll)
                        |
                        v
                    ~/.tugline/replies.jsonl
                    tug status prints the last reply
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

## Privacy

Messages stay on the ntfy server you configure. tugline makes exactly two
kinds of network call, both to `[server] url`: publish a notification, and
poll the reply topic. There is no telemetry, no account, and no hosted
service. One honest caveat for iOS: the stock ntfy iOS app gets instant
delivery through the Apple Push Notification service, and a self-hosted
server reaches APNs only if you configure it to forward through the ntfy.sh
upstream relay. With the relay on, Apple and ntfy.sh see delivery metadata
for your messages. With the relay off, iOS delivery is delayed polling. The
Android app connects to your server directly, no relay involved.

## License

MIT. See [LICENSE](LICENSE).
