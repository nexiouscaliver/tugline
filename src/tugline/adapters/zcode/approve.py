"""Bounded fail-closed approve flow for zcode PermissionRequest.

Called inline by hook.py only when config [toggles] approve is true.

1. Spool one urgent warn-topic record carrying Approve/Deny http action
   buttons that target the local decision endpoints, stamped with the
   session id and a fresh nonce.
2. Bounded-wait (default 120s, hard cap 300s, both overridable through
   TUGLINE_ZCODE_APPROVE_TIMEOUT) polling a decision file
   <approvals_root>/<nonce>.json that the daemon writes later.
3. On a decision, return it so the hook can print the strict-schema
   permission decision JSON. On timeout, or when the spool is
   unavailable, return None: the hook then prints nothing and exits 0,
   so the harness falls back to its own prompt. Never auto-allow.

Tests inject sleep and clock functions; nothing here really sleeps
unless the caller passes the real ones.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import uuid

from tugline.core import spool

DEFAULT_WAIT = 120.0
HARD_CAP = 300.0
POLL_INTERVAL = 0.5
WAIT_ENV = "TUGLINE_ZCODE_APPROVE_TIMEOUT"

sleep = time.sleep  # module level so tests can patch the defaults
monotonic = time.monotonic

_REASONS = {
    "allow": "approved from the phone",
    "deny": "denied from the phone",
    "ask": "handed back to the harness prompt",
}


def new_nonce():
    return uuid.uuid4().hex


def effective_wait(wait=None):
    """Resolve the bounded wait: explicit value or env, clamped to the cap."""
    raw = wait
    if raw is None:
        raw = os.environ.get(WAIT_ENV)
        try:
            raw = float(raw)
        except (TypeError, ValueError):
            raw = DEFAULT_WAIT
    return max(0.0, min(float(raw), HARD_CAP))


def request_actions(base_url, session_id, nonce):
    """Approve/Deny buttons hitting the daemon decision endpoints."""
    base = str(base_url).rstrip("/")
    sid = urllib.parse.quote(str(session_id), safe="")
    return [
        {
            "action": "http",
            "label": "Approve",
            "url": "%s/approve?session=%s&nonce=%s" % (base, sid, nonce),
        },
        {
            "action": "http",
            "label": "Deny",
            "url": "%s/deny?session=%s&nonce=%s" % (base, sid, nonce),
        },
    ]


def pending_path(approvals_root, nonce) -> str:
    return os.path.join(str(approvals_root), nonce + ".pending")


def register_pending(approvals_root, nonce):
    """Record the nonce so the receiver can tell known from unknown.

    Written before the spool record exists, so a button press can
    never beat the registration to the receiver.
    """
    os.makedirs(str(approvals_root), exist_ok=True)
    path = pending_path(approvals_root, nonce)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time()}) + "\n")
    os.replace(tmp, path)


def read_decision(path):
    """Read one decision file; None when missing, malformed, or unknown."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = str(data.get("decision", "")).strip().lower()
    return value if value in _REASONS else None


def format_decision(decision):
    """Strict-schema PermissionRequest decision JSON for stdout."""
    value = str(decision).strip().lower()
    if value not in _REASONS:
        raise ValueError("unknown permission decision: %r" % (decision,))
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "permissionDecision": value,
                "permissionDecisionReason": _REASONS[value],
            }
        }
    )


def run(
    session_id,
    *,
    config,
    spool_root,
    approvals_root,
    wait=None,
    nonce=None,
    sleep=None,
    clock=None,
):
    """Publish the approval request and bounded-wait for the decision.

    Returns "allow", "deny", or "ask", or None on timeout and on any
    failure to publish (the timeout-noop path: fail closed).
    """
    sleeper = sleep if sleep is not None else globals()["sleep"]
    now = clock if clock is not None else globals()["monotonic"]
    token = nonce if nonce is not None else new_nonce()
    short = str(session_id)[:8]

    try:
        register_pending(approvals_root, token)
        spool.append(
            str(spool_root),
            event="permissionrequest",
            session_id=str(session_id),
            topic="warn",
            priority="urgent",
            title="permission request",
            body="session %s asks permission, approve or deny from the phone"
            % short,
            actions=request_actions(config.server.url, session_id, token),
        )
    except OSError:
        return None

    deadline = now() + effective_wait(wait)
    path = os.path.join(str(approvals_root), token + ".json")
    max_polls = int(HARD_CAP / POLL_INTERVAL) + 2  # belt and braces:
    polls = 0  # a stuck clock must never spin the inline hook
    while now() < deadline and polls < max_polls:
        decision = read_decision(path)
        if decision is not None:
            return decision
        sleeper(POLL_INTERVAL)
        polls += 1
    return None
