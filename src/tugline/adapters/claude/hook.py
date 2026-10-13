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
    Notification -> a notify with the notification type in the body;
                   idle_prompt and agent_completed are the two that
                   matter
    PermissionRequest -> a high priority warn record, plus the bounded
                   remote approve handoff when the toggle is on
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

    if key == "sessionstart":
        source = str(normalized.get("source") or "").strip().lower()
        if source != "resume":
            _log(event, "session start is not a resume")
            return 0
        spool.append(
            paths.spool_root(),
            event="notify",
            session_id=session,
            topic="main",
            priority="low",
            title="claude session resumed",
            body="session %s resumed" % session,
        )
        _log(event, "spooled resume")
        return 0

    if key == "stop":
        spool.append(
            paths.spool_root(),
            event="stop",
            session_id=session,
            topic="main",
            priority="default",
            title="claude turn finished",
            body="session %s turn finished" % session,
        )
        _log(event, "spooled stop")
        return 0

    if key == "notification":
        message = normalized.get("message") or normalized.get("title") or ""
        ntype = classify_notification(message)
        spool.append(
            paths.spool_root(),
            event="notify",
            session_id=session,
            topic="main",
            priority="default",
            title="claude %s" % ntype,
            body=("%s: %s" % (ntype, message)).rstrip(": "),
        )
        _log(event, "spooled notification %s" % ntype)
        return 0

    if key == "permissionrequest":
        decision = _permission_request(normalized, session)
        if decision is None:
            _log(event, "permission request spooled")
        else:
            sys.stdout.write(decision + "\n")
            sys.stdout.flush()
            _log(event, "decision returned")
        return 0

    _log(event, "event not wired")
    return 0


def _permission_request(normalized, session):
    try:
        config = load_config()
    except ConfigError:
        config = None
    if config is None:
        # No usable config means no buttons and no wait: publish the
        # plain high priority record and let the local prompt decide.
        approve.publish_request(
            normalized,
            session_id=session,
            spool_root=paths.spool_root(),
        )
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
