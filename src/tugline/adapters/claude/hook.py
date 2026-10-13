"""The Claude Code hook entry point (tug hook-claude <event>).

Claude Code runs each hook as a command and hands it one JSON payload
on stdin. This module is the tugline side of that contract:

- read stdin under a hard 2 second ceiling,
- normalize the payload through the core (Claude's snake_case field
  spellings are taken directly, camelCase maps onto them),
- map the event onto the spool:
    SessionStart -> low priority notify, only when the source says
                   resume (a fresh startup is not worth a ping)
    Stop         -> a stop record; the core armed/idle policy decides
                   downstream whether it notifies
    Notification -> a notify; the classified type maps onto a plain
                   summary ("waiting for your input" for idle prompts,
                   "turn finished" for completions) and the raw type
                   string never reaches the phone
    PermissionRequest -> a high priority warn record, plus the bounded
                   remote approve handoff when the toggle is on
- titles and bodies carry session context ("[claude] project - turn
  finished", body leading "session <short6>") unless the
  [toggles] context_titles switch is off, which restores the exact
  strings from before the enrichment,
- keep stdout EMPTY unless a permission decision is being returned,
- log exactly one line per run under ~/.tugline/logs/.

Every failure path is fail closed: log one line, exit 0, print
nothing. A hook that prints no decision falls through to Claude
Code's local permission prompt, which is exactly the behavior wanted
when anything is broken. Nothing here ever raises to the caller.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import sys
from datetime import datetime, timezone

from tugline.adapters.claude import approve, paths
from tugline.core import payload as payload_module
from tugline.core import spool
from tugline.core.config import ConfigError, load_config

STDIN_CEILING = 2.0


class _HookEventAction(argparse.Action):
    """Run the hook while the arguments are being parsed, then exit.

    Same pattern as argparse's own version action: the hook is the
    whole command, so once the event name is in hand there is nothing
    left for the CLI dispatcher to do.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        raise SystemExit(run_event(str(values)))


def register_hook_command(subparsers):
    """Wire the hook-claude subcommand onto the tug CLI."""
    parser = subparsers.add_parser(
        "hook-claude",
        help="run one Claude Code hook event (payload on stdin)",
    )
    parser.add_argument(
        "event",
        action=_HookEventAction,
        help="hook event name, for example Stop or PermissionRequest",
    )


def read_stdin(stream=None, timeout=STDIN_CEILING) -> str:
    """Read the hook payload under a hard time ceiling.

    A terminal or a pipe that stays silent past the ceiling reads as
    an empty payload; the caller treats that as "no input" and exits
    cleanly rather than hanging the hook.
    """
    if stream is None:
        stream = sys.stdin
    try:
        if stream.isatty():
            return ""
    except Exception:
        return ""
    try:
        stream.fileno()
    except Exception:
        fileno = None
    else:
        fileno = True
    if fileno is not None:
        try:
            ready, _, _ = select.select([stream], [], [], timeout)
        except Exception:
            ready = []
        if not ready:
            return ""
    try:
        return stream.read()
    except Exception:
        return ""


def parse_payload(raw):
    """Return the payload dict, or None for empty/garbage/non-object input."""
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def classify_notification(message) -> str:
    text = str(message or "").lower()
    if "waiting for your input" in text:
        return "idle_prompt"
    if "finished" in text or "completed" in text:
        return "agent_completed"
    return "generic"


# Plain notification summaries for the enriched titles. The classified
# ntype strings (idle_prompt, agent_completed, generic) stay internal:
# they never reach the phone raw again.
_NOTIFICATION_SUMMARIES = {
    "idle_prompt": "waiting for your input",
    "agent_completed": "turn finished",
    "generic": "notification",
}


def _context_titles_enabled() -> bool:
    """Read [toggles] context_titles without ever writing a config.

    A tolerant one-file probe: TUGLINE_CONFIG wins, otherwise the
    config next to the adapter home (which is ~/.tugline in
    production). Any missing file, unparsable line, or unknown value
    leaves the toggle at its default, and nothing is ever created.
    """
    path = os.environ.get("TUGLINE_CONFIG") or os.path.join(
        paths.home_root(), "config.toml"
    )
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return True
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "toggles":
            continue
        key, sep, rest = line.partition("=")
        if not sep or key.strip() != "context_titles":
            continue
        value = rest.split("#", 1)[0].strip().strip('"').lower()
        return value != "false"
    return True


def _session_cwd(normalized):
    return normalized.get("cwd") or normalized.get("project_dir")


def run_event(event_name, stdin=None) -> int:
    """Run one hook event. Never raises; always returns an exit code."""
    try:
        return _run(event_name, stdin)
    except Exception as exc:  # one bad payload, one line, exit clean
        _log(event_name, "error: %s" % (exc,))
        return 0


def _run(event_name, stdin) -> int:
    data = parse_payload(read_stdin(stdin))
    if data is None:
        _log(event_name, "no payload")
        return 0

    normalized = payload_module.normalize(data)
    event = str(event_name or normalized.get("hook_event_name") or "").strip()
    session = str(normalized.get("session_id") or "claude")
    key = event.lower()
    context_on = _context_titles_enabled()

    if key == "sessionstart":
        source = str(normalized.get("source") or "").strip().lower()
        if source != "resume":
            _log(event, "session start is not a resume")
            return 0
        if context_on:
            title = payload_module.context_title(
                "claude", _session_cwd(normalized), "session resumed"
            )
            body = payload_module.context_body(session, "resumed")
        else:
            title = "claude session resumed"
            body = "session %s resumed" % session
        spool.append(
            paths.spool_root(),
            event="notify",
            session_id=session,
            topic="main",
            priority="low",
            title=title,
            body=body,
        )
        _log(event, "spooled resume")
        return 0

    if key == "stop":
        if context_on:
            title = payload_module.context_title(
                "claude", _session_cwd(normalized), "turn finished"
            )
            detail = payload_module.one_line_preview(normalized) or "turn finished"
            body = payload_module.context_body(session, detail)
        else:
            title = "claude turn finished"
            body = "session %s turn finished" % session
        spool.append(
            paths.spool_root(),
            event="stop",
            session_id=session,
            topic="main",
            priority="default",
            title=title,
            body=body,
        )
        _log(event, "spooled stop")
        return 0

    if key == "notification":
        message = normalized.get("message") or normalized.get("title") or ""
        ntype = classify_notification(message)
        if context_on:
            title = payload_module.context_title(
                "claude", _session_cwd(normalized), _NOTIFICATION_SUMMARIES[ntype]
            )
            body = payload_module.context_body(session, message)
        else:
            title = "claude %s" % ntype
            body = ("%s: %s" % (ntype, message)).rstrip(": ")
        spool.append(
            paths.spool_root(),
            event="notify",
            session_id=session,
            topic="main",
            priority="default",
            title=title,
            body=body,
        )
        _log(event, "spooled notification %s" % ntype)
        return 0

    if key == "permissionrequest":
        decision = _permission_request(normalized, session, context_on)
        if decision is None:
            _log(event, "permission request spooled")
        else:
            sys.stdout.write(decision + "\n")
            sys.stdout.flush()
            _log(event, "decision returned")
        return 0

    _log(event, "event not wired")
    return 0


def _spool_plain_permission(normalized, session, context_on):
    """The permission record for when the approve flow is not active.

    Byte-identical to approve.publish_request's plain record when the
    context toggle is off; enriched with session context when on.
    """
    tool = str(normalized.get("tool_name") or "tool")
    summary = approve.summarize_input(normalized.get("tool_input"))
    if context_on:
        title = payload_module.context_title(
            "claude", _session_cwd(normalized), "waiting for your input"
        )
        body = payload_module.context_body(session, "permission: %s" % tool)
    else:
        title = "claude permission request"
        body = (tool + " " + summary).rstrip()
    spool.append(
        paths.spool_root(),
        event="permissionrequest",
        session_id=session,
        topic="warn",
        priority="high",
        title=title,
        body=body,
    )


def _permission_request(normalized, session, context_on):
    try:
        config = load_config()
    except ConfigError:
        config = None
    if config is None or not config.toggles.approve:
        # No usable config means no buttons and no wait: publish the
        # plain record and let the local prompt decide.
        _spool_plain_permission(normalized, session, context_on)
        return None
    return approve.handle(
        normalized,
        session_id=session,
        config=config,
        spool_root=paths.spool_root(),
        state_root=paths.state_root(),
    )


def _log(event, outcome) -> None:
    try:
        path = paths.log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                "%s %s %s\n"
                % (datetime.now(timezone.utc).isoformat(), event, outcome)
            )
    except OSError:
        pass
