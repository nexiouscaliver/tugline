# Recipe: Aider

Aider can run a command instead of ringing the terminal bell when it
finishes a response and waits for your input: the `--notifications-command`
flag, used together with `--notifications`.

## The recipe

```
aider --notifications --notifications-command "tug notify --title aider --body 'aider is waiting for input'"
```

Aider runs the command string as-is when it needs attention; the docs do
not describe any payload argument being passed, so the notification text is
fixed. That is the whole recipe.

Notes:

- `tug` must be on PATH for the Aider process; `uv tool install
  git+https://github.com/nexiouscaliver/tugline` arranges that.
- `tug notify` only spools. The daemon publishes on its next cycle, so
  keep `tug daemon` running.
- To distinguish Aider pings from agent pings, use a dedicated topic:
  add `--topic aider` (a raw ntfy topic) and subscribe to it on the phone.
- The same options can live in Aider's config file instead of the command
  line; the option names there match the documented flags, and the exact
  spelling is covered by Aider's options reference
  (https://aider.chat/docs/config/options.html).
