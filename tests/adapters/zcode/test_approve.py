"""Tests for the bounded fail-closed approve module.

Matrix under test: allow prints decision JSON, deny prints deny JSON,
timeout prints nothing, and every path exits 0. Waiting is bounded
(default 120s, hard cap 300s) and tests never really sleep: clocks and
sleep functions are injected.
"""

import io
import json

import pytest

from tugline.adapters.zcode import approve, hook
from tugline.core import spool

CONFIG_APPROVE_OFF = """
[server]
url = "http://127.0.0.1:2586"
token_file = "~/.tugline/credentials"

[topics]
main = "tug"
warn = "tug-warn"
reply = "tug-reply"

[policy]
idle_seconds = 600

[toggles]
notify = true
replies = true
approve = false
"""

CONFIG_APPROVE_ON = CONFIG_APPROVE_OFF.replace("approve = false", "approve = true")


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now


@pytest.fixture
def config_on(tmp_path):
    from tugline.core.config import load_config

    path = tmp_path / "config-on.toml"
    path.write_text(CONFIG_APPROVE_ON, encoding="utf-8")
    return load_config(str(path))


@pytest.fixture
def approvals(tmp_path):
    root = tmp_path / "approvals"
    root.mkdir()
    return str(root)


def write_decision(approvals, nonce, value):
    with open(
        approvals + "/" + nonce + ".json", "w", encoding="utf-8"
    ) as fh:
        json.dump({"decision": value}, fh)


def records_for(tmp_path):
    return list(spool.records(str(tmp_path / "spool")))


def test_allow_decision_returned_without_sleeping(
    config_on, tmp_path, approvals
):
    write_decision(approvals, "n-allow", "allow")
    fake = FakeTime()
    result = approve.run(
        "s-1",
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=30,
        nonce="n-allow",
        sleep=fake.sleep,
        clock=fake.clock,
    )
    assert result == "allow"
    assert fake.sleeps == []


def test_deny_decision_returned(config_on, tmp_path, approvals):
    write_decision(approvals, "n-deny", "deny")
    fake = FakeTime()
    result = approve.run(
        "s-1",
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=30,
        nonce="n-deny",
        sleep=fake.sleep,
        clock=fake.clock,
    )
    assert result == "deny"


def test_ask_decision_returned(config_on, tmp_path, approvals):
    write_decision(approvals, "n-ask", "ask")
    result = approve.run(
        "s-1",
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=30,
        nonce="n-ask",
        sleep=FakeTime().sleep,
        clock=FakeTime().clock,
    )
    assert result == "ask"


def test_timeout_returns_none_after_bounded_polls(
    config_on, tmp_path, approvals
):
    fake = FakeTime()
    result = approve.run(
        "s-1",
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=1.5,
        nonce="n-none",
        sleep=fake.sleep,
        clock=fake.clock,
    )
    assert result is None
    assert fake.sleeps == [0.5, 0.5, 0.5]


def test_hard_cap_clamps_wait_to_300():
    assert approve.effective_wait(9999) == 300.0


def test_negative_wait_clamps_to_zero():
    assert approve.effective_wait(-3) == 0.0


def test_default_wait_is_120(monkeypatch):
    monkeypatch.delenv("TUGLINE_ZCODE_APPROVE_TIMEOUT", raising=False)
    assert approve.effective_wait(None) == 120.0


def test_wait_env_override(monkeypatch):
    monkeypatch.setenv("TUGLINE_ZCODE_APPROVE_TIMEOUT", "5")
    assert approve.effective_wait(None) == 5.0


def test_wait_env_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("TUGLINE_ZCODE_APPROVE_TIMEOUT", "soon-ish")
    assert approve.effective_wait(None) == 120.0


def test_spool_unavailable_returns_none_without_waiting(
    config_on, tmp_path, approvals
):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    fake = FakeTime()
    result = approve.run(
        "s-1",
        config=config_on,
        spool_root=str(blocked),
        approvals_root=approvals,
        wait=30,
        nonce="n-x",
        sleep=fake.sleep,
        clock=fake.clock,
    )
    assert result is None
    assert fake.sleeps == []


def test_record_is_urgent_warn_with_button_actions(
    config_on, tmp_path, approvals
):
    approve.run(
        "s-1",
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=0,
        nonce="n-look",
    )
    rec = records_for(tmp_path)[0]
    assert rec["event"] == "permissionrequest"
    assert rec["topic"] == "warn"
    assert rec["priority"] == "urgent"
    actions = rec["actions"]
    assert [a["label"] for a in actions] == ["Approve", "Deny"]
    assert actions[0]["url"] == (
        "http://127.0.0.1:2586/approve?session=s-1&nonce=n-look"
    )
    assert actions[1]["url"] == (
        "http://127.0.0.1:2586/deny?session=s-1&nonce=n-look"
    )
    assert all(a["action"] == "http" for a in actions)


def test_nonce_differs_between_runs(config_on, tmp_path, approvals):
    for _ in range(2):
        approve.run(
            "s-1",
            config=config_on,
            spool_root=str(tmp_path / "spool"),
            approvals_root=approvals,
            wait=0,
        )
    recs = records_for(tmp_path)
    nonces = {r["actions"][0]["url"].split("nonce=")[1] for r in recs}
    assert len(nonces) == 2


def test_garbage_decision_file_ignored_until_timeout(
    config_on, tmp_path, approvals
):
    with open(approvals + "/n-bad.json", "w", encoding="utf-8") as fh:
        fh.write("notjson")
    fake = FakeTime()
    result = approve.run(
        "s-1",
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=1.0,
        nonce="n-bad",
        sleep=fake.sleep,
        clock=fake.clock,
    )
    assert result is None
    assert len(fake.sleeps) == 2


def test_unknown_decision_value_ignored(config_on, tmp_path, approvals):
    write_decision(approvals, "n-maybe", "maybe")
    result = approve.run(
        "s-1",
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=0.1,
        nonce="n-maybe",
        sleep=FakeTime().sleep,
        clock=FakeTime().clock,
    )
    assert result is None


def test_decision_value_case_normalized(config_on, tmp_path, approvals):
    write_decision(approvals, "n-cap", "ALLOW")
    result = approve.run(
        "s-1",
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=5,
        nonce="n-cap",
        sleep=FakeTime().sleep,
        clock=FakeTime().clock,
    )
    assert result == "allow"


def test_format_decision_allow_is_strict_schema():
    data = json.loads(approve.format_decision("allow"))
    assert set(data) == {"hookSpecificOutput"}
    block = data["hookSpecificOutput"]
    assert set(block) == {
        "hookEventName",
        "permissionDecision",
        "permissionDecisionReason",
    }
    assert block["hookEventName"] == "PermissionRequest"
    assert block["permissionDecision"] == "allow"
    assert block["permissionDecisionReason"]


def test_format_decision_deny_and_ask():
    assert json.loads(approve.format_decision("deny"))[
        "hookSpecificOutput"
    ]["permissionDecision"] == "deny"
    assert json.loads(approve.format_decision("ask"))[
        "hookSpecificOutput"
    ]["permissionDecision"] == "ask"


def test_format_decision_rejects_unknown_value():
    with pytest.raises(ValueError):
        approve.format_decision("maybe")


def test_permissionrequest_approve_off_never_waits(tmp_path, monkeypatch):
    from tugline.core.config import load_config

    cfg = tmp_path / "config-off.toml"
    cfg.write_text(CONFIG_APPROVE_OFF, encoding="utf-8")
    home = tmp_path / "home"

    def boom(*args, **kwargs):
        raise AssertionError("approve must not run when the toggle is off")

    monkeypatch.setattr(approve, "run", boom)
    code = hook.run(
        "PermissionRequest",
        '{"hookEventName":"PermissionRequest","sessionId":"s-9"}',
        config_path=str(cfg),
        home=str(home),
    )
    assert code == 0
    assert records_for(home)


def hook_matrix_run(tmp_path, monkeypatch, decision, payload_nonce="fx"):
    cfg = tmp_path / "config-on.toml"
    cfg.write_text(CONFIG_APPROVE_ON, encoding="utf-8")
    home = tmp_path / "home"
    fake = FakeTime()
    monkeypatch.setattr(approve, "new_nonce", lambda: payload_nonce)
    monkeypatch.setattr(approve, "sleep", fake.sleep)
    monkeypatch.setattr(approve, "monotonic", fake.clock)
    out = io.StringIO()
    code = hook.run(
        "PermissionRequest",
        '{"hookEventName":"PermissionRequest","sessionId":"s-mx",'
        '"toolName":"Bash"}',
        config_path=str(cfg),
        home=str(home),
        _stdout=out,
    )
    return code, out.getvalue(), home


def test_hook_allow_prints_decision_json(tmp_path, monkeypatch):
    approvals = tmp_path / "home" / "state" / "approvals"
    approvals.mkdir(parents=True)
    write_decision(str(approvals), "fx", "allow")
    code, stdout, home = hook_matrix_run(tmp_path, monkeypatch, "allow")
    assert code == 0
    data = json.loads(stdout)
    assert (
        data["hookSpecificOutput"]["permissionDecision"] == "allow"
    )
    assert records_for(home), "approve module must spool its urgent record"


def test_hook_deny_prints_deny_json(tmp_path, monkeypatch):
    approvals = tmp_path / "home" / "state" / "approvals"
    approvals.mkdir(parents=True)
    write_decision(str(approvals), "fx", "deny")
    code, stdout, home = hook_matrix_run(tmp_path, monkeypatch, "deny")
    assert code == 0
    assert (
        json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]
        == "deny"
    )


def test_hook_ask_prints_ask_json(tmp_path, monkeypatch):
    approvals = tmp_path / "home" / "state" / "approvals"
    approvals.mkdir(parents=True)
    write_decision(str(approvals), "fx", "ask")
    code, stdout, home = hook_matrix_run(tmp_path, monkeypatch, "ask")
    assert code == 0
    assert (
        json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]
        == "ask"
    )


def test_hook_timeout_prints_nothing_exits_zero(tmp_path, monkeypatch):
    code, stdout, home = hook_matrix_run(tmp_path, monkeypatch, None)
    assert code == 0
    assert stdout == ""
    assert records_for(home)
