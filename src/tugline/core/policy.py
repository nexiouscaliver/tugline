"""Arming and notification policy.

State files, all plain formats a human can read:

- Arm marker: <state_root>/armed/<session_id>
  An empty-ish file whose only content is the ISO timestamp of arming.
  Presence means armed. tug arm creates it, tug disarm removes it, and
  the daemon clears it once a Stop for that session is consumed.

- Session state: <state_root>/sessions/<session_id>.json
  {"last_prompt_ts": <epoch float>, "last_stop_ts": <epoch float>,
   "last_turn_seconds": <epoch float>}
  Written by the daemon from UserPromptSubmit and Stop events found in
  the spool. Keys are only present once known.

state_root defaults to ~/.tugline/state.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

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


def _event_name(event) -> str:
    name = event.get("hook_event_name") or event.get("event") or ""
    return str(name).strip().lower()


def decide(event, session_state: SessionState) -> Decision:
    raise NotImplementedError("policy decide not implemented yet")


def arm(session_id: str, state_root: str | None = None) -> None:
    raise NotImplementedError("arm not implemented yet")


def disarm(session_id: str, state_root: str | None = None) -> None:
    raise NotImplementedError("disarm not implemented yet")


def is_armed(session_id: str, state_root: str | None = None) -> bool:
    raise NotImplementedError("is_armed not implemented yet")


def load_session(session_id: str, state_root: str | None = None) -> dict:
    raise NotImplementedError("load_session not implemented yet")


def record_prompt(session_id: str, ts: float, state_root: str | None = None) -> None:
    raise NotImplementedError("record_prompt not implemented yet")


def record_stop(session_id: str, ts: float, state_root: str | None = None) -> None:
    raise NotImplementedError("record_stop not implemented yet")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
