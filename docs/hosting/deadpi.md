# Hosting: the always-on box

The spool on your laptop is durable against ntfy outages. It is not durable
against your laptop being asleep: hooks append, nothing publishes until the
daemon runs, and the phone only hears from a server it can reach. At some
point you want one always-on box. This page is the migration sketch: ntfy
plus the daemon on any Linux host, agents on your machines publishing to it
over the tailnet. Working name in these notes: the dead pi, because the
first version of this sketch was written the day a Raspberry Pi died.

## What lives where

Split by what the code actually does:

- The spool and the daemon are per machine. Hooks append to
  `~/.tugline/spool` on the machine where the agent runs, and `tug daemon`
  flushes that local spool. A daemon on the host serves agents on the host;
  your laptop still needs its own daemon.
- The ntfy server is shared. That is the piece that wants an always-on,
  always-reachable box, and it is the only piece that must be Linux.

So the target shape: ntfy on the Linux box, reachable over your tailnet;
every machine with agents runs its own `tug daemon` pointed at the box; the
phone subscribes to the box through one of the channels in
[docs/channels.md](../channels.md).

## Why not macOS for the server

The Homebrew formula installs the ntfy CLI only (publish and subscribe).
The ntfy server is not supported on macOS; you can build and run it from
source for development, but that is not a hosting plan. The box should be
Linux. Client machines, including macOS laptops, are fine.

## The box: ntfy on Linux

From the ntfy install docs, the apt repository route:

```
sudo mkdir -p /etc/apt/keyrings
sudo curl -L -o /etc/apt/keyrings/ntfy.gpg https://archive.ntfy.sh/apt/keyring.gpg
sudo apt install apt-transport-https
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/ntfy.gpg] https://archive.ntfy.sh/apt stable main" | sudo tee /etc/apt/sources.list.d/ntfy.list
sudo apt update
sudo apt install ntfy
sudo systemctl enable ntfy
sudo systemctl start ntfy
```

Swap `amd64` for `arm64` on an ARM box. Or the container route, with a
restart policy so reboots are handled:

```
docker run -d --name ntfy --restart unless-stopped \
  -p 2586:80 \
  -v ntfy-cache:/var/cache/ntfy \
  binwiederhier/ntfy serve
```

Server config lives at `/etc/ntfy/server.yml`. The two settings worth
making before anything else: `base-url` set to the name the phone and the
agents will use, and `auth-default-access: deny-all` with tokens minted per
client (`ntfy token add <user>`) once you are past the first happy path.

## The box: daemon as a service (for agents that run on the box)

`~/.config/systemd/user/tugline.service`:

```
[Unit]
Description=tugline daemon (spool flush and reply poll)
After=network-online.target

[Service]
ExecStart=%h/.local/bin/tug daemon
Restart=on-failure

[Install]
WantedBy=default.target
```

```
systemctl --user daemon-reload
systemctl --user enable --now tugline
journalctl --user -u tugline -f
```

## The laptops: daemon at login (macOS example)

`~/Library/LaunchAgents/sh.tugline.daemon.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>sh.tugline.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/.local/bin/tug</string>
    <string>daemon</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```
launchctl load ~/Library/LaunchAgents/sh.tugline.daemon.plist
```

Adjust the tug path to where `uv tool install` put it (`which tug`).

## Pointing everything at the box

On every machine running agents, `~/.tugline/config.toml`:

```toml
[server]
url = "https://ntfy.example.ts.net"
```

The address is whatever your channel choice says: a tailnet name, a serve
bridge URL, or a public reverse proxy. The phone subscribes to the same
address. Then:

```
tug test
```

from each machine until it prints `HTTP 200`.

## Reboot and death checklist

1. The box comes back by itself: enabled services (`systemctl enable ntfy`,
   the docker restart policy, `loginctl enable-linger <user>` if the daemon
   runs as a user service), and the ntfy cache on persistent disk.
2. The spool is the safety net while anything is down: records wait, the
   daemon publishes on return, `tug status` shows the depth.
3. When the box actually dies (the pi lesson): build the replacement from
   this page, keep the same topic names and the same server address (the
   DNS name moves, the config does not), and the phones need nothing except
   the same URL resolving again.
4. After any box swap, `tug test` from one agent machine and from the
   phone before you trust it.
