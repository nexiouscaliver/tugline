# Recipe: Codex

Codex can run an external program when an agent turn completes: the
`notify` key in `~/.codex/config.toml`. Per the Codex docs it invokes the
configured command with one JSON payload appended as the final argument,
with fields like `type` (currently `agent-turn-complete`), `thread-id`,
`turn-id`, `cwd`, `input-messages`, and `last-assistant-message`.

## The constraint

`tug notify` takes `--title` and `--body` and rejects unknown arguments.
The JSON payload Codex appends is an extra positional argument, so a bare
one-liner fails:

```
notify = ["tug", "notify", "--title", "codex", "--body", "turn complete"]
```

That exits 2 with `unrecognized arguments` the moment Codex appends the
payload. The wrapper below exists to absorb the extra argument.

## The recipe

Plain fixed text, payload ignored:

```toml
notify = ["sh", "-c", "tug notify --title \"codex turn complete\" --body \"turn complete\"", "codex"]
```

Codex invokes this as `sh -c <script> codex <payload>`: `codex` lands in
`$0`, the JSON payload in `$1`, and the script never looks at either.

With the payload's text in the body (requires jq on PATH):

```toml
notify = ["sh", "-c", "tug notify --title \"codex turn complete\" --body \"$(printf %s \"$1\" | jq -r '.[\"last-assistant-message\"] // \"turn complete\"' | head -c 200)\"", "codex"]
```

Both forms verified end to end: `tug notify` prints the spool record id and
exits 0, and the daemon publishes on its next cycle.

Notes:

- `tug` must be on PATH for the Codex process; `uv tool install
  git+https://github.com/nexiouscaliver/tugline` arranges that.
- This recipe covers turn completion, the only event type Codex documents
  for `notify` today. There is no permission-approve path into Codex from
  tugline.
- The daemon must be running for the spooled record to go out.
