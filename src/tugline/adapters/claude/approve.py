"""Bounded remote approval for Claude Code permission requests.

When a PermissionRequest hook fires and the approve toggle is on, the
hook publishes one flagged spool record (the daemon turns it into a
high priority notification with Approve and Deny buttons) and then
waits for the phone to answer. The answer lands as a small JSON file
under <state root>/approvals/<approve id>.json, written by whatever
receives the button press upstream; this module only polls.

The wait is bounded: default 120 seconds, hard cap 300, an injected
clock in tests. On a decision it returns the exact JSON Claude Code
expects on stdout ({"decision":{"behavior":"allow"}} or the deny
shape with a message). On timeout it returns None, which makes the
hook print nothing and exit 0: the local permission prompt takes
over. It never auto-allows.

A decision file with an unknown value, or one that fails to parse,
counts as "not answered yet"; the wait simply continues.
"""

from __future__ import annotations

import json
import os
import time
import uuid

from tugline.core import approval_files, spool

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 300.0
POLL_INTERVAL = 0.5
APPROVALS_DIRNAME = "approvals"
DEFAULT_DENY_MESSAGE = "denied from phone"


def new_approve_id() -> str:
    return uuid.uuid4().hex[:16]


def effective_timeout(timeout) -> float:
    """Clamp a requested wait into [0, MAX_TIMEOUT], None means default."""
    if timeout is None:
        seconds = DEFAULT_TIMEOUT
    else:
        seconds = float(timeout)
    return max(0.0, min(seconds, MAX_TIMEOUT))


def action_buttons(config, approve_id):
    """The Approve and Deny http buttons for the flagged record.

    The base comes from [server] external_url when set (a proxy host
    routing /approvals back to this machine), the ntfy url otherwise.
    When a token is configured the buttons carry it, so the receiver
    can authenticate the press; without a token the buttons go out
    bare and the receiver runs open, loopback only.
    """
    from tugline.core.publisher import Publisher

    base = config.server.action_base.rstrip("/")
    headers = None
    token = Publisher(
        config.server.url, config.server.token_file
    ).read_token()
    if token:
        headers = {"Authorization": "Bearer " + token}
    buttons = []
    for label, decision in (("Approve", "allow"), ("Deny", "deny")):
        button = {
            "action": "http",
            "label": label,
            "url": "%s/%s/%s?decision=%s"
            % (base, APPROVALS_DIRNAME, approve_id, decision),
            "method": "POST",
        }
        if headers:
            button["headers"] = dict(headers)
        buttons.append(button)
    return buttons


def decision_path(state_root, approve_id) -> str:
    return os.path.join(str(state_root), APPROVALS_DIRNAME, approve_id + ".json")


def pending_path(state_root, approve_id) -> str:
    return os.path.join(str(state_root), APPROVALS_DIRNAME, approve_id + ".pending")


def register_pending(state_root, approve_id):
    """Record the nonce so the receiver can tell known from unknown.

    Written before the spool record exists, so a button press can
    never beat the registration to the receiver. The write itself is
    core.approval_files, shared with the receiver and the console.
    """
    approval_files.register_pending(
        os.path.join(str(state_root), APPROVALS_DIRNAME), approve_id
    )


def read_decision(state_root, approve_id):
    """Return a normalized decision dict, or None when there is no answer yet."""
    data = approval_files.read_decision(
        os.path.join(str(state_root), APPROVALS_DIRNAME), approve_id
    )
    if data is None:
        return None
    value = data["decision"].strip().lower()
    if value == "allow":
        return {"decision": "allow"}
    if value == "deny":
        return {
            "decision": "deny",
            "message": str(data.get("message") or DEFAULT_DENY_MESSAGE),
        }
    return None


def decision_json(decision) -> str:
    """The exact stdout JSON Claude Code parses for a permission decision."""
    if decision["decision"] == "allow":
        inner = {"behavior": "allow"}
    else:
        inner = {"behavior": "deny", "message": decision["message"]}
    return json.dumps({"decision": inner}, separators=(",", ":"))


def summarize_input(tool_input, limit=200) -> str:
    if tool_input is None:
        return ""
    if isinstance(tool_input, str):
        text = tool_input
    else:
        try:
            text = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(tool_input)
    return " ".join(text.split())[:limit]


def publish_request(
    payload,
    *,
    session_id,
    spool_root,
    config=None,
    approve_id=None,
    state_root=None,
):
    """Append the permission request record; buttons only when approve is on."""
    approve_id = approve_id or new_approve_id()
    tool = str(payload.get("tool_name") or "tool")
    summary = summarize_input(payload.get("tool_input"))
    body = (tool + " " + summary).rstrip()
    actions = None
    if config is not None and config.toggles.approve:
        actions = action_buttons(config, approve_id)
        if state_root:
            register_pending(state_root, approve_id)
    return (
        spool.append(
            spool_root,
            event="permissionrequest",
            session_id=session_id,
            topic="warn",
            priority="high",
            title="claude permission request",
            body=body,
            actions=actions,
        ),
        approve_id,
    )


def handle(
    payload,
    *,
    session_id,
    config,
    spool_root,
    state_root,
    timeout=None,
    approve_id=None,
    sleep=time.sleep,
    monotonic=time.monotonic,
):
    """Publish the flagged record and wait for the phone to answer.

    Returns the decision JSON string, or None on timeout (and always
    None when the toggles make waiting pointless). Never auto-allows.
    """
    record, approve_id = publish_request(
        payload,
        session_id=session_id,
        spool_root=spool_root,
        config=config,
        approve_id=approve_id,
        state_root=state_root,
    )
    if not (config.toggles.approve and config.toggles.notify):
        return None
    return wait_for_decision(
        approve_id,
        state_root,
        timeout=timeout,
        sleep=sleep,
        monotonic=monotonic,
    )


def wait_for_decision(
    approve_id,
    state_root,
    *,
    timeout=None,
    sleep=time.sleep,
    monotonic=time.monotonic,
):
    deadline = monotonic() + effective_timeout(timeout)
    while True:
        decision = read_decision(state_root, approve_id)
        if decision is not None:
            return decision_json(decision)
        if monotonic() >= deadline:
            return None
        sleep(POLL_INTERVAL)
