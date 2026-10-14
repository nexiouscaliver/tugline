"""Display derivation for the sessions panel.

The store keeps raw facts: state as derived from the newest event,
last_seen as the newest event timestamp. Everything an operator
should SEE is derived here, per response, without writing anything
back, so a stale "running" row can heal the moment it is asked about
and history rows never move.

Two derivations:

- display_state, honest liveness. last_seen within five minutes is
  "active" (the page renders "active now"). A raw "running" state
  with no further events for more than thirty minutes decays to
  "idle": a session whose last event is hours old must never read as
  running. Terminal states keep their meaning at any age ("done"),
  and so does an open ask ("waiting"): their age measures operator
  inaction, not session liveness. Anything still running inside the
  thirty minute window stays "running"; between five and thirty
  minutes with no events that is the honest reading (a long tool
  call, a big build).
- display_id, collision-proof short ids. A suffix of the session id,
  lengthened until exactly one session in the same response ends
  with it; the full id when even that cannot separate two rows.
  Computed per response so it adapts as sessions come and go. The
  operator label, when set, is the primary name everywhere; the
  display_id stays beside it as the unambiguous secondary.

Pure functions: no store, no IO; the clock arrives as a parameter.
"""

from __future__ import annotations

from datetime import datetime, timezone

ACTIVE_WINDOW_S = 5 * 60
IDLE_AFTER_S = 30 * 60
DISPLAY_SUFFIX_MIN = 6


def _parse_when(text):
    """One ISO-8601 timestamp as an aware datetime, or None."""
    if not text:
        return None
    try:
        when = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def derive_display_state(raw_state, last_seen, now) -> str:
    """One display state from the raw state and the age of last_seen.

    Returns one of "done", "waiting", "active", "running", "idle".
    An unparseable or missing last_seen reads as arbitrarily old:
    never active, and a raw running row decays to idle.
    """
    state = str(raw_state or "").strip().lower()
    if state == "done":
        return "done"
    if state == "waiting":
        return "waiting"
    when = _parse_when(last_seen)
    if when is None:
        return "idle"
    age_s = (now - when).total_seconds()
    if age_s <= ACTIVE_WINDOW_S:
        return "active"
    if age_s > IDLE_AFTER_S:
        return "idle"
    return "running"


def unique_display_ids(session_ids) -> dict:
    """A display suffix per session id, unique within this list.

    Starts at the shortest useful tail and lengthens one character at
    a time until exactly one id in the list ends with it; falls back
    to the full id (two byte-identical ids cannot be separated by any
    suffix, and session_id is the primary key so true duplicates do
    not occur).
    """
    ids = [str(i) for i in session_ids]
    display = {}
    for sid in ids:
        shown = sid
        n = DISPLAY_SUFFIX_MIN
        while n < len(sid):
            tail = sid[-n:]
            matches = sum(1 for other in ids if other.endswith(tail))
            if matches == 1:
                shown = tail
                break
            n += 1
        display[sid] = shown
    return display


def decorate_sessions(rows, now=None) -> list:
    """Session rows with display_state and display_id attached.

    The store hands back last_seen-descending rows; this keeps that
    order but floats every active-now row to the top, so the panel
    reads active sessions first, then everything else by recency.
    Input rows are not mutated; each output row is a copy.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    display = unique_display_ids(r.get("session_id") for r in rows)
    decorated = []
    for row in rows:
        out = dict(row)
        out["display_state"] = derive_display_state(
            row.get("state"), row.get("last_seen"), now
        )
        out["display_id"] = display.get(str(row.get("session_id")), "")
        decorated.append(out)
    active = [r for r in decorated if r["display_state"] == "active"]
    rest = [r for r in decorated if r["display_state"] != "active"]
    return active + rest
