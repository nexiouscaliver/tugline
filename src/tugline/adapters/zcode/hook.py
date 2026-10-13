"""Inline hook entry for the zcode harness.

Invoked as: tug hook-zcode <event>

The harness pipes one JSON line on stdin and waits. Exit code 2 would
block the session, so this entry always exits 0, even on garbage input,
and logs one line to <home>/logs/adapter-zcode.log instead of raising.
stdout must stay empty (strict schema) unless the approve path prints a
permission decision.

Flow: read stdin, normalize through the core payload module, apply core
policy, append one record to the core spool, exit 0.
"""

from __future__ import annotations

import json
import os
import re
import select
import sys
import time
from datetime import datetime, timezone

from tugline.core import payload, policy, spool
from tugline.core.config import (
    Config,
    ConfigError,
    PolicyConfig,
    ServerConfig,
    TopicsConfig,
    TogglesConfig,
    load_config,
)

STDIN_CEILING = 2.0
LOG_NAME = "adapter-zcode.log"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def read_stdin(stream=None, ceiling=STDIN_CEILING):
    """Read stdin with a hard ceiling. Returns whatever arrived in time."""
    if stream is None:
        stream = sys.stdin
    try:
        stream.fileno()
    except (OSError, ValueError, AttributeError):
        # in-memory stream (tests, embedders): cannot block, read it all
        return stream.read()
    chunks = []
    deadline = time.monotonic() + ceiling
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([stream], [], [], remaining)
        except (OSError, ValueError):
            break
        if not ready:
            break
        chunk = stream.read(4096)
        if chunk == "":
            break
        chunks.append(chunk)
    return "".join(chunks)


def log_line(home, message):
    directory = os.path.join(str(home), "logs")
    try:
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        clean = " ".join(str(message).split())[:300]
        with open(
            os.path.join(directory, LOG_NAME), "a", encoding="utf-8"
        ) as fh:
            fh.write("%s hook-zcode: %s\n" % (stamp, clean))
    except OSError:
        pass


def _fallback_config():
    return Config(
        server=ServerConfig(
            url="http://127.0.0.1:2586",
            token_file=os.path.expanduser("~/.tugline/credentials"),
        ),
        topics=TopicsConfig(main="tug", warn="tug-warn", reply="tug-reply"),
        policy=PolicyConfig(idle_seconds=600),
        toggles=TogglesConfig(notify=True, replies=True, approve=False),
        path="<defaults>",
    )


def _load_config(path):
    if path:
        try:
            return load_config(path)
        except ConfigError:
            return None
    try:
        return load_config()
    except ConfigError:
        return None


def _event_name(normalized, event_arg):
    name = normalized.get("hook_event_name") or event_arg or ""
    return str(name).strip()


def _session_id(normalized, env):
    sid = normalized.get("session_id") or env.get("CLAUDE_SESSION_ID") or ""
    return str(sid) or "unknown"


def _short(session_id):
    return str(session_id)[:8]


def _record_fields(event, normalized, decision, session_id):
    if event == "permissionrequest":
        tool = str(normalized.get("tool_name") or "tool use")
        title = "permission request"
        body = "session %s: %s" % (_short(session_id), tool)
    elif event == "sessionstart":
        title = "session resumed"
        body = "session %s resumed" % _short(session_id)
    elif event == "stop":
        title = "session stopped"
        body = "session %s stopped: %s" % (
            _short(session_id),
            decision.reason or "turn finished",
        )
    elif event == "userpromptsubmit":
        title = "prompt submitted"
        prompt = str(normalized.get("prompt") or "")
        body = prompt[:60] or "session %s" % _short(session_id)
    else:
        title = event or "hook event"
        body = "session %s" % _short(session_id)
    topic = decision.topic if decision.action == "publish" else "main"
    priority = decision.priority if decision.action == "publish" else "default"
    return topic, priority, title, body


def run(
    event_arg,
    stdin_text,
    *,
    config_path=None,
    home=None,
    env=None,
    _stdout=None,
):
    """Handle one hook event. Always returns an exit code of 0."""
    home = os.path.expanduser(home) if home else os.path.expanduser("~/.tugline")
    env = env if env is not None else dict(os.environ)
    out = _stdout if _stdout is not None else sys.stdout
    try:
        return _run(
            event_arg,
            stdin_text,
            home=home,
            env=env,
            config_path=config_path,
            out=out,
        )
    except Exception as exc:  # never traceback to the harness
        log_line(home, "unexpected error: %s" % exc)
        return 0


def _run(event_arg, stdin_text, *, home, env, config_path, out):
    raw = stdin_text
    if not raw.strip():
        log_line(home, "empty stdin, nothing to do")
        return 0
    try:
        parsed = json.loads(raw)
    except ValueError:
        log_line(home, "stdin is not json")
        return 0
    if not isinstance(parsed, dict):
        log_line(home, "stdin payload is not an object")
        return 0

    normalized = payload.normalize(parsed)
    event = _event_name(normalized, event_arg).lower()
    if not event:
        log_line(home, "no event name in payload or argv")
        return 0
    session_id = _session_id(normalized, env)

    config = _load_config(config_path)
    if config is None:
        log_line(home, "config unreadable, using defaults")
        config = _fallback_config()

    state_root = os.path.join(home, "state")
    policy_sid = session_id if _SAFE_ID.match(session_id) else "unknown"
    state = policy.SessionState(
        session_id=policy_sid,
        armed=policy.is_armed(policy_sid, state_root=state_root),
        turn_seconds=policy.load_session(policy_sid, state_root=state_root).get(
            "last_turn_seconds"
        ),
        idle_seconds=config.policy.idle_seconds,
    )
    decision = policy.decide(normalized, state)

    spool_root = os.path.join(home, "spool")

    if event == "permissionrequest" and config.toggles.approve:
        # the approve module publishes its own urgent record with
        # buttons, so the plain notify record is skipped here
        from tugline.adapters.zcode import approve

        outcome = approve.run(
            session_id,
            config=config,
            spool_root=spool_root,
            approvals_root=os.path.join(state_root, "approvals"),
        )
        if outcome is not None:
            out.write(approve.format_decision(outcome) + "\n")
            out.flush()
        return 0

    topic, priority, title, body = _record_fields(
        event, normalized, decision, session_id
    )
    spool.append(
        spool_root,
        event=event,
        session_id=session_id,
        topic=topic,
        priority=priority,
        title=title,
        body=body,
    )
    return 0


def main(argv=None):
    """Console path: pull the event off argv, read stdin, run."""
    args = list(sys.argv[1:] if argv is None else argv)
    event = args[0] if args else ""
    return run(event, read_stdin())


if __name__ == "__main__":
    sys.exit(main())
