# Security policy

## What this software is

A local notifier. It reads hook payloads from harnesses, writes plain
files under `~/.tugline`, and talks to exactly one server: the ntfy URL in
your config. Threat model first, expectations second.

## Reporting

Open a private security advisory on this GitHub repository (Security tab,
"Report a vulnerability"), or an issue if the report is not sensitive.
Expectations, stated plainly: no bug bounty, best effort response, fixes
prioritized over disclosure timelines. Include the version (`tug version`),
the config with secrets redacted, and the log lines from
`~/.tugline/logs/` if the report involves a hook. Do not put tokens or
payload contents in public issues.

## Token handling

- The ntfy bearer token lives in one file, `[server] token_file`,
  default `~/.tugline/credentials`. It is sent only in the Authorization
  header to the configured server URL. It is never printed, logged, or
  included in any error message; that is enforced in the publisher, which
  never echoes it anywhere.
- Keep the file unreadable to others: `chmod 600 ~/.tugline/credentials`.
- A missing or empty token file means anonymous requests. ntfy accepts
  those when access is open. On any shared or public server, that is not
  the mode you want; see the checklist.
- The token file parser accepts a bare token on the first non-empty line
  or `ntfy_token=value` / `token=value` lines. Nothing else reads the file.

## Deny-all posture

The permission-approve path is built to fail closed everywhere:

- The approve toggle (`[toggles] approve`) defaults to false.
- A hook that cannot publish, cannot parse, or times out waiting for a
  phone decision prints nothing and exits 0. The harness then shows its
  own local permission prompt. No path auto-allows.
- The wait is bounded (default 120 s, hard cap 300 s), so a missing phone
  cannot wedge a session open beyond the cap.
- Hooks write plain JSON lines under `~/.tugline` and log one line per
  run. If you want forensics, that is where they are.

## Public exposure checklist

Before any ntfy server reachable off your LAN:

1. HTTPS only, via a reverse proxy with real certificates, or the ntfy.sh
   upstream. Plaintext http on the public internet hands the token to
   anyone on the path.
2. `auth-default-access: deny-all` in the ntfy server config, one token
   per client machine, tokens revocable per machine.
3. Rename the default topics. Topic names are capabilities: with open
   access, knowing the name is the whole auth. `tug`, `tug-warn`, and
   `tug-reply` are defaults precisely so you will change them; pick long
   random strings for all three in `~/.tugline/config.toml` and subscribe
   to the same names on the phone.
4. Rate limits on (visitor request limits in the ntfy config), because a
   public ntfy gets probed.
5. Decide about the iOS upstream relay with eyes open: instant delivery on
   iOS needs `upstream-base-url "https://ntfy.sh"` in your server config,
   and then Apple and ntfy.sh see delivery metadata for your messages.
   Without it, iOS falls back to delayed polling. Android needs no relay.
6. Remember what the messages contain: hook payloads include session ids,
  file paths, prompts, and tool names, and the warn topic carries the
  permission summary. Treat topic contents as sensitive when choosing a
  server.
