"""The approval decision files under <state root>/approvals.

One convention, three writers and readers: adapters register a nonce
as <nonce>.pending before publishing the ask, the receiver and the
console race to answer by writing <nonce>.json and removing the
marker, and everyone polls or reads the same two names. The helpers
live here so the adapters, the receiver, and the console cannot drift
apart on payload shape or write discipline.

Every write is tmp file, flush, fsync, rename: a reader either sees
the whole file or the previous state, never a half-written line.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

VALID_DECISIONS = ("allow", "deny")


def pending_path(approvals_root, nonce) -> str:
    return os.path.join(str(approvals_root), nonce + ".pending")


def decision_path(approvals_root, nonce) -> str:
    return os.path.join(str(approvals_root), nonce + ".json")


def register_pending(approvals_root, nonce) -> None:
    """Record the nonce so a receiver can tell known from unknown.

    Written before the spool record exists, so a button press can
    never beat the registration. Payload is the shape the adapters
    have always written: {"ts": <epoch seconds>} plus a newline.
    """
    root = str(approvals_root)
    os.makedirs(root, exist_ok=True)
    path = pending_path(root, nonce)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time()}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_decision(approvals_root, nonce, decision) -> str:
    """Answer a nonce: <nonce>.json with the decision and an ISO ts.

    The decision must be allow or deny; anything else raises
    ValueError before anything is written. Returns the path written.
    """
    value = str(decision).strip().lower()
    if value not in VALID_DECISIONS:
        raise ValueError("decision must be allow or deny, got %r" % (decision,))
    root = str(approvals_root)
    os.makedirs(root, exist_ok=True)
    path = decision_path(root, nonce)
    payload = json.dumps(
        {"decision": value, "ts": datetime.now(timezone.utc).isoformat()},
        separators=(",", ":"),
    )
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def read_decision(approvals_root, nonce):
    """{"decision", "ts"} from the decision file, else None.

    Missing, unparseable, non-object, or carrying no decision all
    read as None. Extra keys (a deny message, say) pass through
    untouched so callers keep their own vocabulary on top.
    """
    try:
        with open(decision_path(approvals_root, nonce), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision") or "").strip()
    if not decision:
        return None
    out = dict(data)
    out["decision"] = decision
    out["ts"] = str(data.get("ts") or "")
    return out


def remove_pending(approvals_root, nonce) -> None:
    """Best effort: drop the pending marker, ignore any failure.

    The decision file is the truth once written; a stale marker only
    means a later press reads as already answered.
    """
    try:
        os.remove(pending_path(approvals_root, nonce))
    except OSError:
        pass
