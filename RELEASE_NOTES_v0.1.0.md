# tugline 0.1.0

Push notifications and phone replies for coding-agent sessions, over ntfy.
tugline ships no app of its own and runs no hosted service: messages travel
from your machine to an ntfy server you point at, and the phone client is
the stock ntfy app.

## What it does

- Tells you when an agent session finishes a turn, goes idle, or blocks on
  a permission prompt.
- Takes replies from your phone: whatever you send to the reply topic is
  streamed into a local inbox file (`~/.tugline/replies.jsonl`), ready for
  the next prompt.
- Can put Approve and Deny buttons on permission notifications, answered by
  a loopback receiver the daemon runs. This is the approve toggle, off by
  default.

## Install

Python 3.10 or newer, through uv:

```
uv tool install git+https://github.com/nexiouscaliver/tugline
tug --version
```

No runtime dependencies; everything runs on the standard library.

## Current state

The approve path shipped in this release: both adapters (zcode and Claude
Code) can put Approve/Deny buttons on permission notifications, and the
daemon records presses through a loopback receiver. It is still recommended
off until your notification channels are proven: run notify and replies
for a while first, then turn the toggle on and expose the receiver
deliberately (see `docs/adapters.md` for the port and `external_url`).

One iOS caveat: the stock ntfy iOS app gets instant delivery through the
Apple Push Notification service, and a self-hosted server reaches APNs
only if you configure it to forward through the ntfy.sh upstream relay.
With the relay on, Apple and ntfy.sh see delivery metadata for your
messages. With the relay off, iOS delivery is delayed polling. Android
connects to your server directly, no relay involved.

## Getting started

The full 10-minute path (server, config, phone app, adapter install) is
in `docs/quickstart.md`.
