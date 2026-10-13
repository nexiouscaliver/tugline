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
import uuid

from tugline.core import approval_files, spool

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


def request_actions(config, nonce):
    """Approve/Deny buttons hitting the daemon decision endpoints.

    Both buttons POST to the receiver path /approvals/{nonce} with the
    decision in the query. The base comes from [server] external_url
    when set (a proxy host routing /approvals back to this machine),
    the ntfy url otherwise. When a token is configured the buttons
    carry it, so the receiver can authenticate the press.
    """
    from tugline.core.publisher import Publisher

    base = config.server.action_base.rstrip("/")
    headers = None
    token = Publisher(
        config.server.url, config.server.token_file
    ).read_token()
    if token:
        headers = {"Authorization": "Bearer " + token}
    actions = []
    for label, decision in (("Approve", "allow"), ("Deny", "deny")):
        action = {
            "action": "http",
            "label": label,
            "url": "%s/approvals/%s?decision=%s" % (base, nonce, decision),
            "method": "POST",
        }
        if headers:
            action["headers"] = dict(headers)
        actions.append(action)
    return actions


def register_pending(approvals_root, nonce):
    """Record the nonce so the receiver can tell known from unknown.

    Written before the spool record exists, so a button press can
    never beat the registration to the receiver. The write itself is
    core.approval_files, shared with the receiver and the console.
    """
    approval_files.register_pending(approvals_root, nonce)


def read_decision(approvals_root, nonce):
    """One decision value, or None when missing, malformed, or not a
    value this flow knows (allow, deny, ask)."""
    data = approval_files.read_decision(approvals_root, nonce)
    if data is None:
        return None
    value = data["decision"].strip().lower()
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
            actions=request_actions(config, token),
        )
    except OSError:
        return None

    deadline = now() + effective_wait(wait)
    max_polls = int(HARD_CAP / POLL_INTERVAL) + 2  # belt and braces:
    polls = 0  # a stuck clock must never spin the inline hook
    while now() < deadline and polls < max_polls:
        decision = read_decision(approvals_root, token)
        if decision is not None:
            return decision
        sleeper(POLL_INTERVAL)
        polls += 1
    return None
