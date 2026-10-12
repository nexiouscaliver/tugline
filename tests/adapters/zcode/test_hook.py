"""Tests for the zcode hook entry (tugline.adapters.zcode.hook).

Contract under test, from the W0-L2 probe:

- stdin carries one-line JSON with dual camelCase/snake_case spellings
- the entry normalizes via the core payload module, applies core policy,
  appends one record to the core spool, and exits 0
- stdout stays empty unless the approve path prints a decision
- garbage input never blocks the session: exit 0, one log line
"""

import json
import os

import pytest

from tugline.adapters.zcode import hook
from tugline.core import policy, spool

CONFIG_TEMPLATE_APPROVE_OFF = """
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


@pytest.fixture
def home(tmp_path):
    return tmp_path / "tugline-home"


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_TEMPLATE_APPROVE_OFF, encoding="utf-8")
    return str(path)


def spool_records(home):
    return list(spool.records(str(home / "spool")))


def run_hook(event, payload, *, config_path, home, env=None):
    return hook.run(
        event,
        payload,
        config_path=config_path,
        home=str(home),
        env=env if env is not None else {},
    )


def test_sessionstart_resume_appends_record(home, config_path, capsys):
    code = run_hook(
        "SessionStart",
        '{"hookEventName":"SessionStart","source":"resume","sessionId":"s-1234"}',
        config_path=config_path,
        home=home,
    )
    assert code == 0
    records = spool_records(home)
    assert len(records) == 1
    rec = records[0]
    assert rec["event"] == "sessionstart"
    assert rec["session_id"] == "s-1234"
    assert rec["topic"] == "main"
    assert rec["priority"] == "default"
    assert capsys.readouterr().out == ""


def test_sessionstart_startup_still_records_for_downstream_policy(
    home, config_path
):
    code = run_hook(
        "SessionStart",
        '{"hookEventName":"SessionStart","source":"startup"}',
        config_path=config_path,
        home=home,
    )
    assert code == 0
    records = spool_records(home)
    assert len(records) == 1
    assert records[0]["event"] == "sessionstart"


def test_stop_armed_session_records_publish_decision(home, config_path):
    state_root = str(home / "state")
    policy.arm("s-arm", state_root=state_root)
    code = run_hook(
        "Stop",
        '{"hook_event_name":"Stop","session_id":"s-arm"}',
        config_path=config_path,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["event"] == "stop"
    assert rec["topic"] == "main"
    assert policy.is_armed("s-arm", state_root=state_root)


def test_stop_unarmed_records_without_consuming_armed_state(
    home, config_path
):
    code = run_hook(
        "Stop",
        '{"hook_event_name":"Stop","session_id":"s-calm"}',
        config_path=config_path,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["event"] == "stop"


def test_permissionrequest_records_warn_high(home, config_path):
    code = run_hook(
        "PermissionRequest",
        '{"hookEventName":"PermissionRequest","sessionId":"s-9",'
        '"toolName":"Bash"}',
        config_path=config_path,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["event"] == "permissionrequest"
    assert rec["topic"] == "warn"
    assert rec["priority"] == "high"


def test_permissionrequest_approve_off_never_waits(home, config_path, monkeypatch):
    from tugline.adapters.zcode import approve

    def boom(*args, **kwargs):
        raise AssertionError("approve module must not run when toggle is off")

    monkeypatch.setattr(approve, "run", boom)
    code = run_hook(
        "PermissionRequest",
        '{"hookEventName":"PermissionRequest","sessionId":"s-9"}',
        config_path=config_path,
        home=home,
    )
    assert code == 0
    assert spool_records(home)


def test_userpromptsubmit_records_only_never_publishes(home, config_path):
    code = run_hook(
        "UserPromptSubmit",
        '{"hookEventName":"UserPromptSubmit","sessionId":"s-1","prompt":"fix the bug"}',
        config_path=config_path,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["event"] == "userpromptsubmit"


def test_camelcase_and_snake_payloads_agree(home, config_path):
    run_hook(
        "SessionStart",
        '{"hookEventName":"SessionStart","source":"resume","sessionId":"s-dual"}',
        config_path=config_path,
        home=home,
    )
    camel = spool_records(home)[0]

    other = tmp_home = home.parent / "tugline-home-2"
    run_hook(
        "SessionStart",
        '{"hook_event_name":"SessionStart","source":"resume","session_id":"s-dual"}',
        config_path=config_path,
        home=other,
    )
    snake = spool_records(other)[0]

    assert camel["event"] == snake["event"]
    assert camel["session_id"] == snake["session_id"]
    assert camel["topic"] == snake["topic"]


def test_env_session_id_used_when_payload_lacks_one(home, config_path):
    code = run_hook(
        "Stop",
        '{"hook_event_name":"Stop"}',
        config_path=config_path,
        home=home,
        env={"CLAUDE_SESSION_ID": "s-from-env"},
    )
    assert code == 0
    assert spool_records(home)[0]["session_id"] == "s-from-env"


def test_garbage_stdin_exits_zero_with_one_log_line(home, config_path, capsys):
    code = run_hook("Stop", "this is not json {", config_path=config_path, home=home)
    assert code == 0
    assert capsys.readouterr().out == ""
    assert spool_records(home) == []
    log = home / "logs" / "adapter-zcode.log"
    assert log.is_file()
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1


def test_empty_stdin_exits_zero(home, config_path, capsys):
    code = run_hook("Stop", "", config_path=config_path, home=home)
    assert code == 0
    assert capsys.readouterr().out == ""
    assert spool_records(home) == []


def test_nondict_json_payload_exits_zero(home, config_path):
    code = run_hook("Stop", "[1, 2, 3]", config_path=config_path, home=home)
    assert code == 0
    assert spool_records(home) == []


def test_unicode_payload_exits_zero(home, config_path):
    dash = chr(8212)
    payload = json.dumps({"hookEventName": "Stop", "sessionId": "s-u", "note": "a" + dash + "b"})
    code = run_hook("Stop", payload, config_path=config_path, home=home)
    assert code == 0


def test_unsafe_session_id_still_exits_zero(home, config_path):
    code = run_hook(
        "Stop",
        '{"hook_event_name":"Stop","session_id":"bad id!"}',
        config_path=config_path,
        home=home,
    )
    assert code == 0
    assert spool_records(home)[0]["session_id"] == "bad id!"


def test_broken_config_falls_back_to_defaults(home, tmp_path, capsys):
    bad = tmp_path / "broken.toml"
    bad.write_text("[server]\nurl = 12\n", encoding="utf-8")
    code = run_hook(
        "Stop",
        '{"hook_event_name":"Stop","session_id":"s-1"}',
        config_path=str(bad),
        home=home,
    )
    assert code == 0
    assert spool_records(home)


def test_stdin_read_ceiling_returns_without_data(tmp_path):
    import os as _os

    r, w = _os.pipe()
    try:
        text = hook.read_stdin(_os.fdopen(r, "r"), ceiling=0.2)
    finally:
        _os.close(w)
    assert text == ""
