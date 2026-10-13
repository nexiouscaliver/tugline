"""Hook tests: stdin contract, event mapping, fail-closed behavior.

Every path through the hook must exit 0 with an empty stdout unless a
PermissionRequest decision is being handed back, and every run leaves
exactly one line in the adapter log.
"""

import io
import json
import time

from tugline.adapters.claude import approve as approve_module
from tugline.adapters.claude import hook as hook_module


def run(event, payload, stdin=None):
    if stdin is None:
        stdin = io.StringIO(json.dumps(payload))
    return hook_module.run_event(event, stdin)


# Stop


def test_stop_spools_a_stop_record(tug_home, hook_payload, spool_records, capsys):
    rc = run("Stop", hook_payload("Stop"))
    assert rc == 0
    records = spool_records()
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "stop"
    assert record["session_id"] == "sess-4f2a"
    assert record["topic"] == "main"
    assert record["priority"] == "default"
    assert record["title"] == "[claude] tugline - turn finished"
    assert capsys.readouterr().out == ""


# SessionStart


def test_session_start_resume_spools_a_low_notify(
    tug_home, hook_payload, spool_records
):
    rc = run("SessionStart", hook_payload("SessionStart", source="resume"))
    assert rc == 0
    record = spool_records()[0]
    assert record["event"] == "notify"
    assert record["priority"] == "low"
    assert record["topic"] == "main"
    assert record["title"] == "[claude] tugline - session resumed"


def test_session_start_startup_spools_nothing(tug_home, hook_payload, spool_records):
    assert run("SessionStart", hook_payload("SessionStart", source="startup")) == 0
    assert spool_records() == []


def test_session_start_clear_spools_nothing(tug_home, hook_payload, spool_records):
    assert run("SessionStart", hook_payload("SessionStart", source="clear")) == 0
    assert spool_records() == []


def test_session_start_without_source_spools_nothing(
    tug_home, hook_payload, spool_records
):
    assert run("SessionStart", hook_payload("SessionStart")) == 0
    assert spool_records() == []


# Notification


def test_notification_idle_prompt_type_lands_in_the_body(
    tug_home, hook_payload, spool_records
):
    payload = hook_payload("Notification", message="Claude is waiting for your input")
    assert run("Notification", payload) == 0
    record = spool_records()[0]
    assert record["event"] == "notify"
    assert record["title"] == "[claude] tugline - waiting for your input"
    assert record["body"] == "session s-4f2a, Claude is waiting for your input"


def test_notification_agent_completed_type_lands_in_the_body(
    tug_home, hook_payload, spool_records
):
    payload = hook_payload("Notification", message="Claude has finished the task")
    assert run("Notification", payload) == 0
    record = spool_records()[0]
    assert record["title"] == "[claude] tugline - turn finished"
    assert record["body"] == "session s-4f2a, Claude has finished the task"


def test_notification_generic_keeps_the_raw_message(
    tug_home, hook_payload, spool_records
):
    payload = hook_payload("Notification", message="A tool wants attention")
    assert run("Notification", payload) == 0
    record = spool_records()[0]
    assert record["title"] == "[claude] tugline - notification"
    assert record["body"] == "session s-4f2a, A tool wants attention"


def test_notification_without_message_falls_back_to_the_title(
    tug_home, hook_payload, spool_records
):
    payload = hook_payload("Notification", title="Claude needs your permission")
    assert run("Notification", payload) == 0
    record = spool_records()[0]
    assert record["body"] == "session s-4f2a, Claude needs your permission"


# Payload normalization


def test_camel_case_payload_spellings_are_normalized(tug_home, spool_records):
    payload = {"hookEventName": "Stop", "sessionId": "sess-camel", "cwd": "/tmp"}
    assert hook_module.run_event("Stop", io.StringIO(json.dumps(payload))) == 0
    record = spool_records()[0]
    assert record["session_id"] == "sess-camel"
    assert record["event"] == "stop"


# Fail-closed stdin handling


def test_garbage_stdin_exits_zero_and_prints_nothing(tug_home, capsys, spool_records):
    rc = hook_module.run_event("Stop", io.StringIO("this is not json"))
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert spool_records() == []


def test_empty_stdin_exits_zero_and_prints_nothing(tug_home, capsys, spool_records):
    rc = hook_module.run_event("Stop", io.StringIO(""))
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert spool_records() == []


def test_non_object_payload_exits_zero_and_spools_nothing(
    tug_home, capsys, spool_records
):
    rc = hook_module.run_event("Stop", io.StringIO("[1, 2, 3]"))
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert spool_records() == []


def test_unwired_event_exits_zero_and_spools_nothing(
    tug_home, hook_payload, spool_records, capsys
):
    assert run("PreCompact", hook_payload("PreCompact")) == 0
    assert spool_records() == []
    assert capsys.readouterr().out == ""


# Logging


def test_every_invocation_appends_one_log_line(tug_home, hook_payload):
    run("Stop", hook_payload("Stop"))
    run("Stop", hook_payload("Stop"))
    log_file = tug_home / "logs" / "adapter-claude.log"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("spooled stop")


# PermissionRequest


def test_permission_request_without_approve_spools_a_plain_warn_record(
    tug_home, write_config, hook_payload, spool_records, capsys
):
    write_config(approve=False)
    payload = hook_payload(
        "PermissionRequest",
        tool_name="Bash",
        tool_input={"command": "git push"},
        tool_use_id="tool_9",
    )
    assert run("PermissionRequest", payload) == 0
    assert capsys.readouterr().out == ""
    record = spool_records()[0]
    assert record["event"] == "permissionrequest"
    assert record["topic"] == "warn"
    assert record["priority"] == "high"
    assert "actions" not in record
    assert record["body"] == "session s-4f2a, permission: Bash"


def test_permission_request_with_approve_prints_the_exact_decision_json(
    tug_home, write_config, hook_payload, spool_records, capsys, monkeypatch
):
    write_config(approve=True)
    monkeypatch.setattr(approve_module, "new_approve_id", lambda: "approveme1")
    decision_file = tug_home / "state" / "approvals" / "approveme1.json"
    decision_file.parent.mkdir(parents=True)
    decision_file.write_text(json.dumps({"decision": "allow"}), encoding="utf-8")
    payload = hook_payload(
        "PermissionRequest",
        tool_name="Bash",
        tool_input={"command": "git status"},
        tool_use_id="tool_1",
    )
    assert run("PermissionRequest", payload) == 0
    assert capsys.readouterr().out == '{"decision":{"behavior":"allow"}}\n'
    records = spool_records()
    assert len(records) == 1
    assert records[0]["actions"][0]["label"] == "Approve"


def test_permission_request_with_broken_config_spools_a_plain_record(
    tmp_path, monkeypatch, tug_home, hook_payload, spool_records, capsys
):
    broken = tmp_path / "broken.toml"
    broken.write_text("[server\nbroken", encoding="utf-8")
    monkeypatch.setenv("TUGLINE_CONFIG", str(broken))
    payload = hook_payload(
        "PermissionRequest",
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_use_id="tool_2",
    )
    assert run("PermissionRequest", payload) == 0
    assert capsys.readouterr().out == ""
    records = spool_records()
    assert len(records) == 1
    assert "actions" not in records[0]


def test_read_stdin_gives_up_at_the_ceiling_on_a_silent_pipe():
    import os

    read_fd, write_fd = os.pipe()
    try:
        stream = os.fdopen(read_fd, "r")
        start = time.perf_counter()
        assert hook_module.read_stdin(stream, timeout=0.2) == ""
        elapsed = time.perf_counter() - start
        assert 0.15 <= elapsed < 2.0
    finally:
        os.close(write_fd)
