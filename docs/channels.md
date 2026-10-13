# Channels: making the phone hop durable

The spool on your machine is durable: if ntfy is down, records wait and the
daemon retries. The hop from the ntfy server to your phone is the fragile
one, because it depends on where your phone is and what it can reach. This
page is the playbook for the three channels that work, and the one
constraint that bites everyone first.

## The dedupe constraint

The ntfy app cannot dedupe one topic name subscribed twice against two
different server URLs. Subscribe to `tug` on `http://ntfy.lan:2586` and to
`tug` on `https://ntfy.example.com`, and you get two independent
subscriptions: double buzzes when both are reachable, split brain and stale
cursors when only one is. The rule: one live server URL per location, and
the topic names live on exactly one server. If you migrate servers, drop
the old subscription on the phone the same day.

## Channel 1: LAN

Self-hosted ntfy on a box on your local network. The phone subscribes while
on your wifi.

- Setup: ntfy listening on the LAN, phone on the same network. Plain HTTP is
  fine inside the LAN.
- Works: at home, fast, nothing leaves the house.
- Fails: the moment the phone hops to cellular. Notifications queue on the
  server and arrive when you are back on wifi.

Reboot notes: the server box must come back up on its own after a power
cut. On Linux that is systemd (`systemctl enable ntfy`, the packaged unit
exists). Android phones reconnect to wifi subscriptions on their own, give
or take battery doze.

## Channel 2: tailnet

The ntfy server reachable only from your tailnet (Tailscale or another VPN
that stays up on the phone). Two shapes:

- Direct: the phone subscribes to `http://100.x.y.z:2586` or a MagicDNS
  name like `http://ntfy-box:2586`, reachable only with the VPN up. No TLS
  unless the server has certs.
- Serve bridge: run ntfy on the box, and put a TLS front on it with
  `tailscale serve` (or any reverse proxy with real certs) so the phone
  subscribes to a plain https URL on your tailnet, for example
  `https://ntfy-box.example.ts.net`. The ntfy Android app is happier with
  a plain https URL than with a bare IP over http.

- Setup: Tailscale on the server box and on the phone, ntfy listening on
  the tailnet interface, config `[server] url` pointing agents at the same
  address.
- Works: anywhere the VPN is up, which is the point; no public exposure,
  no tokens strictly required (the tailnet is the ACL).
- Fails: VPN down on the phone means no notifications; iOS keeps the VPN
  alive better than it keeps websocket polling alive, so test yours.

Reboot notes: the server box needs Tailscale up before ntfy serves
(systemd orders this for you if you install both as services), and the
phone needs VPN-on-demand or you will discover it was off at the worst
time.

## Channel 3: public URL

ntfy behind a proper HTTPS reverse proxy on a public name, or ntfy.sh
itself.

- Setup: a domain, a proxy (Caddy makes the cert part one line), ntfy as
  the upstream, and auth on: `auth-default-access: deny-all` in the server
  config, tokens minted per client with `ntfy token add <user>`. Agents
  publish over the public URL; the phone subscribes to the same URL.
- Works: everywhere, on every network, forever. This is the only channel
  that does not depend on where the phone sits.
- Fails: you are now on the public internet. Tokens required, rate limits
  required, and on iOS instant delivery pulls in the upstream relay trade
  (see the README privacy section).

If any topic name is the only thing guarding a topic, as it is on
ntfy.sh, treat the topic name as a secret: no dictionary words, no
project names. Rename the defaults (`tug`, `tug-warn`, `tug-reply`) in
`~/.tugline/config.toml` before you go public.

## Subscription matrix

| Location | Subscribe to | Transport | Notes |
| --- | --- | --- | --- |
| Home, phone on wifi | `http://ntfy.lan:2586` (example LAN name) | http, direct | fastest path, dies off-wifi |
| Anywhere, VPN up | `http://100.x.y.z:2586` or `https://ntfy-box.example.ts.net` | VPN or serve bridge | tailnet is the ACL, no public face |
| Anywhere, no VPN | `https://ntfy.example.com` or `https://ntfy.sh` | https public | tokens + rate limits + relay caveat on iOS |

One row active per location. The server side of all three is the same ntfy;
only the front door differs. The hosting sketch in
[docs/hosting/deadpi.md](hosting/deadpi.md) covers the box itself: ntfy,
the daemon as a service, and what to do about reboots.

## Reboot checklist, all channels

1. The ntfy server box comes back unattended: enabled services, ntfy cache
   directory on real disk.
2. `tug daemon` comes back too. It is a user service; a systemd user unit
   or a tmux habit both work, but decide which, because notifications
   queue silently in the spool while it is down.
3. The phone app is logged in, subscribed to the one live URL for where
   you are, and its battery settings allow background delivery. This is
   the item that decays quietly; `tug test` after any phone OS update.
