"""Arming and notification policy.

State files, all plain formats a human can read:

- Arm marker: <state_root>/armed/<session_id>
  A one-line file holding the ISO timestamp of arming. Presence means
  armed. tug arm creates it, tug disarm removes it, and the daemon
  clears it once a Stop for that session is consumed (one shot: the
  next Stop after arming is the one that notifies).

- Session state: <state_root>/sessions/<session_id>.json
  {"last_prompt_ts": <epoch float>, "last_stop_ts": <epoch float>,
   "last_turn_seconds": <epoch float>}
  Written by the daemon from UserPromptSubmit and Stop events found in
  the spool. Keys appear only once known. A file that fails to parse
  reads back as empty state rather than taking the daemon down.

state_root defaults to ~/.tugline/state.

decide(event, session_state) rules:

- PermissionRequest always publishes, priority high, warn topic.
- SessionStart publishes only when source is "resume".
- Stop publishes when the session is armed, or when the turn ran
  longer than idle_seconds.
- UserPromptSubmit never publishes (it only feeds session state).
- notify (direct tug notify calls) always publishes with the topic and
  priority stored on the record.
- Any other event is suppressed.

Event names are matched case-insensitively and may come from either a
normalized hook payload (hook_event_name) or a spool record (event).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

DEFAULT_STATE_ROOT = os.path.join(os.path.expanduser("~"), ".tugline", "state")

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class Decision:
    action: str  # "publish" or "suppress"
    topic: str | None = None
    priority: str | None = None
    reason: str = ""


@dataclass
class SessionState:
    session_id: str
    armed: bool = False
    turn_seconds: float | None = None
    idle_seconds: float = 600.0


def _event_name(event: Mapping) -> str:
    name = event.get("hook_event_name") or event.get("event") or ""
    return str(name).strip().lower()


def decide(event: Mapping, session_state: SessionState) -> Decision:
    name = _event_name(event)

    if name == "permissionrequest":
        return Decision("publish", "warn", "high", "permission request")

    if name == "sessionstart":
        source = str(event.get("source", "")).strip().lower()
        if source == "resume":
            return Decision("publish", "main", "default", "session resumed")
        return Decision("suppress", reason="session start is not a resume")

    if name == "stop":
        if session_state.armed:
            return Decision("publish", "main", "default", "session armed")
        if (
            session_state.turn_seconds is not None
            and session_state.turn_seconds > session_state.idle_seconds
        ):
            return Decision(
                "publish", "main", "default", "turn over idle threshold"
            )
        return Decision("suppress", reason="stop below threshold and not armed")

    if name == "userpromptsubmit":
        return Decision("suppress", reason="prompt submit only records state")

    if name == "notify":
        topic = event.get("topic") or "main"
        priority = event.get("priority") or "default"
        return Decision("publish", topic, priority, "direct notify")

    return Decision("suppress", reason="event type is not notified")


def _root(state_root: str | None) -> str:
    return state_root or DEFAULT_STATE_ROOT


def _check_id(session_id: str) -> str:
    if not _SAFE_ID.match(session_id or ""):
        raise ValueError("unsafe session id: %r" % (session_id,))
    return session_id


def arm(session_id: str, state_root: str | None = None) -> None:
    _check_id(session_id)
    armed_dir = os.path.join(_root(state_root), "armed")
    os.makedirs(armed_dir, exist_ok=True)
    path = os.path.join(armed_dir, session_id)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_now_iso() + "\n")


def disarm(session_id: str, state_root: str | None = None) -> None:
    _check_id(session_id)
    path = os.path.join(_root(state_root), "armed", session_id)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def is_armed(session_id: str, state_root: str | None = None) -> bool:
    _check_id(session_id)
    return os.path.isfile(os.path.join(_root(state_root), "armed", session_id))


def _session_path(session_id: str, state_root: str | None) -> str:
    return os.path.join(_root(state_root), "sessions", session_id + ".json")


def load_session(session_id: str, state_root: str | None = None) -> dict:
    _check_id(session_id)
    path = _session_path(session_id, state_root)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_session(session_id: str, state_root: str | None, data: dict) -> None:
    path = _session_path(session_id, state_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def record_prompt(session_id: str, ts: float, state_root: str | None = None) -> None:
    _check_id(session_id)
    data = load_session(session_id, state_root)
    data["last_prompt_ts"] = float(ts)
    _write_session(session_id, state_root, data)


def record_stop(session_id: str, ts: float, state_root: str | None = None) -> None:
    _check_id(session_id)
    data = load_session(session_id, state_root)
    data["last_stop_ts"] = float(ts)
    prompt_ts = data.get("last_prompt_ts")
    if prompt_ts is not None:
        data["last_turn_seconds"] = float(ts) - float(prompt_ts)
    _write_session(session_id, state_root, data)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
