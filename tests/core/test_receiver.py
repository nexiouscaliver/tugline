"""Approve receiver tests: the endpoint matrix the buttons land on.

The adapters publish Approve and Deny http buttons; the receiver is
the thing that finally answers them. A valid press carries the same
bearer token as every other tugline request, names a nonce the
adapter registered as a pending file, and a decision of allow or deny;
the receiver writes the decision file the approve modules poll for.
Everything else is refused, and nothing a request does may take the
listener down.
"""

import json
import os
import socket
import time
import urllib.error
import urllib.request

import pytest

from tugline.core import config as cfg
from tugline.core.receiver import ApproveReceiver

TOKEN = "tk_receiver_test"


def make_config(approve_port=0, token_file=None, approve=True):
    return cfg.Config(
        server=cfg.ServerConfig(
            url="http://127.0.0.1:2586",
            token_file=token_file,
            approve_port=approve_port,
        ),
        topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
        policy=cfg.PolicyConfig(600),
        toggles=cfg.TogglesConfig(True, True, approve),
        path="/none",
    )


@pytest.fixture
def state_root(tmp_path):
    return str(tmp_path / "state")


@pytest.fixture
def receiver(tmp_path, state_root):
    token_file = tmp_path / "creds"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    instance = ApproveReceiver(
        make_config(token_file=str(token_file)),
        state_root=state_root,
        read_token=lambda: TOKEN,
        log=lambda line: None,
    )
    assert instance.start() is True
    yield instance
    instance.stop()


def register_pending(state_root, nonce):
    path = os.path.join(str(state_root), "approvals", nonce + ".pending")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    return path


def decision_file(state_root, nonce):
    return os.path.join(str(state_root), "approvals", nonce + ".json")


def press(receiver, nonce, decision, token=TOKEN, method="POST", body=None):
    url = "http://127.0.0.1:%d/approvals/%s?decision=%s" % (
        receiver.port,
        nonce,
        decision,
    )
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    data = body.encode("utf-8") if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8").strip()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8").strip()


def test_allow_press_writes_the_decision_file(receiver, state_root):
    register_pending(state_root, "n1")

    status, text = press(receiver, "n1", "allow")

    assert status == 200
    assert text  # one line of body
    with open(decision_file(state_root, "n1"), encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["decision"] == "allow"
    assert "T" in data["ts"]  # an iso timestamp


def test_deny_press_writes_the_decision_file(receiver, state_root):
    register_pending(state_root, "n2")

    status, _ = press(receiver, "n2", "deny")

    assert status == 200
    with open(decision_file(state_root, "n2"), encoding="utf-8") as fh:
        assert json.load(fh)["decision"] == "deny"


def test_decision_accepted_from_a_form_body(receiver, state_root):
    register_pending(state_root, "n3")

    status, _ = press(receiver, "n3", "", body="decision=deny")

    assert status == 200
    with open(decision_file(state_root, "n3"), encoding="utf-8") as fh:
        assert json.load(fh)["decision"] == "deny"


def test_bad_token_gets_403(receiver, state_root):
    register_pending(state_root, "n4")

    assert press(receiver, "n4", "allow", token="tk_wrong")[0] == 403
    assert press(receiver, "n4", "allow", token=None)[0] == 403
    assert not os.path.exists(decision_file(state_root, "n4"))


def test_unknown_nonce_gets_404(receiver, state_root):
    assert press(receiver, "never-registered", "allow")[0] == 404


def test_bad_decision_value_gets_400(receiver, state_root):
    register_pending(state_root, "n5")

    assert press(receiver, "n5", "maybe")[0] == 400


def test_unknown_path_gets_404(receiver):
    url = "http://127.0.0.1:%d/other" % receiver.port
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + TOKEN}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 404


def test_second_press_on_a_decided_nonce_is_idempotent(receiver, state_root):
    register_pending(state_root, "n6")
    press(receiver, "n6", "allow")
    with open(decision_file(state_root, "n6"), encoding="utf-8") as fh:
        first = fh.read()

    status, _ = press(receiver, "n6", "deny")  # double tap, other button

    assert status == 200
    with open(decision_file(state_root, "n6"), encoding="utf-8") as fh:
        assert fh.read() == first  # the first decision stands


def test_listener_binds_loopback_only(receiver):
    assert receiver.host == "127.0.0.1"


def test_listener_survives_garbage(receiver, state_root):
    sock = socket.create_connection(("127.0.0.1", receiver.port), timeout=5)
    sock.sendall(b"NOT HTTP AT ALL\r\n\r\n\x00\x01\x02")
    sock.close()
    time.sleep(0.05)

    register_pending(state_root, "n7")
    status, _ = press(receiver, "n7", "allow")

    assert status == 200  # still answering ordinary requests


def test_daemon_starts_the_receiver_when_approve_is_on(fake_ntfy, tmp_path):
    from tugline.core.daemon import Daemon

    daemon = Daemon(
        make_config(),
        spool_root=str(tmp_path / "spool"),
        replies_path=str(tmp_path / "replies.jsonl"),
        state_root=str(tmp_path / "state"),
    )
    assert daemon.start_approve_receiver() is True
    try:
        register_pending(str(tmp_path / "state"), "d1")
        nonce_file = decision_file(str(tmp_path / "state"), "d1")
        status, _ = press(daemon._receiver, "d1", "allow", token=None)
        # no token file on this config: open posture, loopback only
        assert status == 200
        with open(nonce_file, encoding="utf-8") as fh:
            assert json.load(fh)["decision"] == "allow"
    finally:
        daemon.stop_approve_receiver()


def test_claude_approve_registers_a_pending_nonce(tmp_path):
    from tugline.adapters.claude import approve as approve_module

    config = cfg.Config(
        server=cfg.ServerConfig("http://127.0.0.1:2586", "/dev/null"),
        topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
        policy=cfg.PolicyConfig(600),
        toggles=cfg.TogglesConfig(True, True, True),
        path="/none",
    )

    class Clock:
        now = 1000.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    approve_module.handle(
        {"tool_name": "Bash"},
        session_id="s-rc",
        config=config,
        spool_root=str(tmp_path / "spool"),
        state_root=str(tmp_path / "state"),
        timeout=0,
        approve_id="pend1",
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    register = tmp_path / "state" / "approvals" / "pend1.pending"
    assert register.exists()


def test_zcode_approve_registers_a_pending_nonce(tmp_path):
    from tugline.adapters.zcode import approve as zcode_approve

    config = cfg.Config(
        server=cfg.ServerConfig("http://127.0.0.1:2586", "/dev/null"),
        topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
        policy=cfg.PolicyConfig(600),
        toggles=cfg.TogglesConfig(True, True, True),
        path="/none",
    )

    class Clock:
        now = 1000.0

        def sleep(self, seconds):
            self.now += seconds

        def clock(self):
            return self.now

    clock = Clock()
    zcode_approve.run(
        "s-zc",
        config=config,
        spool_root=str(tmp_path / "spool"),
        approvals_root=str(tmp_path / "approvals"),
        wait=0,
        nonce="pend2",
        sleep=clock.sleep,
        clock=clock.clock,
    )

    assert (tmp_path / "approvals" / "pend2.pending").exists()


def test_button_press_end_to_end_reaches_the_polling_module(
    receiver, state_root, tmp_path
):
    from tugline.adapters.zcode import approve as zcode_approve

    config = cfg.Config(
        server=cfg.ServerConfig("http://127.0.0.1:2586", "/dev/null"),
        topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
        policy=cfg.PolicyConfig(600),
        toggles=cfg.TogglesConfig(True, True, True),
        path="/none",
    )

    class Clock:
        now = 1000.0

        def sleep(self, seconds):
            self.now += seconds

        def clock(self):
            return self.now

    clock = Clock()
    kwargs = dict(
        config=config,
        spool_root=str(tmp_path / "spool"),
        approvals_root=state_root + "/approvals",
        wait=0,
        nonce="e2e",
        sleep=clock.sleep,
        clock=clock.clock,
    )
    zcode_approve.run("s-e2e", **kwargs)  # registers the pending nonce

    status, _ = press(receiver, "e2e", "deny")

    assert status == 200
    kwargs["wait"] = 0.5  # one poll: the decision is already on disk
    assert zcode_approve.run("s-e2e", **kwargs) == "deny"  # the poll sees it
