"""Context-enriched notification titles and bodies for the claude hook.

The claude shapes the operator complained about ("claude idle_prompt"
with no session or project) become:

- title: "[claude] <project> - <plain summary>" where the summary is
  "waiting for your input", "turn finished", "session resumed", or
  "notification"; the raw notification type strings never reach the
  phone again.
- body:  "session <last 6 of the session id>" plus a comma separated
  detail (the notification message, an 80 character turn preview, or
  "permission: <tool> <summary>" for plain permission records).

With [toggles] context_titles = false every shape below must be
byte-identical to the strings the hook emitted before the enrichment.
"""

import io
import json

import pytest

from tugline.adapters.claude import hook as hook_module

CONFIG_TEXT = """\
[server]
url = "http://127.0.0.1:2586"
token_file = "/dev/null"

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
context_titles = %s
"""

SID = "sess-4f2a"
SHORT6 = "s-4f2a"


@pytest.fixture
def titles_home(tmp_path, monkeypatch):
    home = tmp_path / "tugline-home"
    monkeypatch.setenv("TUGLINE_HOME", str(home))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    return home


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    def write(context_titles=True):
        path = tmp_path / "config.toml"
        path.write_text(
            CONFIG_TEXT % ("true" if context_titles else "false"),
            encoding="utf-8",
        )
        monkeypatch.setenv("TUGLINE_CONFIG", str(path))
        return path

    return write


def payload_for(event, **extra):
    payload = {
        "session_id": SID,
        "transcript_path": "/tmp/transcripts/%s.jsonl" % SID,
        "cwd": "/Users/shahil/work/tugline",
        "hook_event_name": event,
    }
    payload.update(extra)
    return payload


def run(event, payload):
    return hook_module.run_event(event, io.StringIO(json.dumps(payload)))


def spool_records(tug_home):
    records = []
    spool_dir = tug_home / "spool"
    if not spool_dir.is_dir():
        return records
    for day in sorted(spool_dir.glob("*.jsonl")):
        for line in day.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


# Enriched shapes (context_titles defaults to true: no config at all)


def test_stop_carries_context_title_and_preview(titles_home):
    assert run("Stop", payload_for("Stop", last_assistant_message="Done.\nShipped the fix.")) == 0
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "[claude] tugline - turn finished"
    assert rec["body"] == "session %s, Done. Shipped the fix." % SHORT6


def test_stop_without_preview_keeps_plain_detail(titles_home):
    assert run("Stop", payload_for("Stop")) == 0
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "[claude] tugline - turn finished"
    assert rec["body"] == "session %s, turn finished" % SHORT6


def test_session_start_resume_carries_context(titles_home):
    assert run("SessionStart", payload_for("SessionStart", source="resume")) == 0
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "[claude] tugline - session resumed"
    assert rec["body"] == "session %s, resumed" % SHORT6


def test_idle_notification_reads_waiting_for_input(titles_home):
    assert (
        run("Notification", payload_for("Notification", message="Claude is waiting for your input"))
        == 0
    )
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "[claude] tugline - waiting for your input"
    assert rec["body"] == "session %s, Claude is waiting for your input" % SHORT6


def test_completed_notification_reads_turn_finished(titles_home):
    assert (
        run("Notification", payload_for("Notification", message="Claude has finished the task")) == 0
    )
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "[claude] tugline - turn finished"
    assert rec["body"] == "session %s, Claude has finished the task" % SHORT6


def test_generic_notification_keeps_the_message_as_detail(titles_home):
    assert run("Notification", payload_for("Notification", message="A tool wants attention")) == 0
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "[claude] tugline - notification"
    assert rec["body"] == "session %s, A tool wants attention" % SHORT6


def test_plain_permission_record_carries_context(titles_home, write_config):
    write_config(context_titles=True)
    assert (
        run(
            "PermissionRequest",
            payload_for(
                "PermissionRequest",
                tool_name="Bash",
                tool_input={"command": "git push"},
            ),
        )
        == 0
    )
    rec = spool_records(titles_home)[0]
    assert rec["event"] == "permissionrequest"
    assert rec["topic"] == "warn"
    assert rec["priority"] == "high"
    assert "actions" not in rec
    assert rec["title"] == "[claude] tugline - waiting for your input"
    assert rec["body"] == "session %s, permission: Bash" % SHORT6


def test_project_dir_used_when_cwd_is_missing(titles_home):
    assert run("Stop", payload_for("Stop", cwd="", project_dir="/Users/shahil/work/other")) == 0
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "[claude] other - turn finished"


def test_missing_cwd_falls_back_to_unknown_project(titles_home):
    payload = {
        "session_id": SID,
        "hook_event_name": "Stop",
    }
    assert run("Stop", payload) == 0
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "[claude] unknown - turn finished"


def test_toggle_probe_never_writes_a_config_file(titles_home):
    assert run("Stop", payload_for("Stop")) == 0
    assert spool_records(titles_home)
    assert not (titles_home / "config.toml").exists()


# Toggle off: byte-identical legacy shapes


def test_off_stop_is_byte_identical_legacy(titles_home, write_config):
    write_config(context_titles=False)
    assert run("Stop", payload_for("Stop", last_assistant_message="no preview")) == 0
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "claude turn finished"
    assert rec["body"] == "session %s turn finished" % SID


def test_off_session_start_resume_is_byte_identical_legacy(titles_home, write_config):
    write_config(context_titles=False)
    assert run("SessionStart", payload_for("SessionStart", source="resume")) == 0
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "claude session resumed"
    assert rec["body"] == "session %s resumed" % SID


def test_off_idle_notification_is_byte_identical_legacy(titles_home, write_config):
    write_config(context_titles=False)
    assert (
        run("Notification", payload_for("Notification", message="Claude is waiting for your input"))
        == 0
    )
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "claude idle_prompt"
    assert rec["body"] == "idle_prompt: Claude is waiting for your input"


def test_off_completed_notification_is_byte_identical_legacy(titles_home, write_config):
    write_config(context_titles=False)
    assert (
        run("Notification", payload_for("Notification", message="Claude has finished the task")) == 0
    )
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "claude agent_completed"
    assert rec["body"] == "agent_completed: Claude has finished the task"


def test_off_generic_notification_is_byte_identical_legacy(titles_home, write_config):
    write_config(context_titles=False)
    assert run("Notification", payload_for("Notification", message="A tool wants attention")) == 0
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "claude generic"
    assert rec["body"] == "generic: A tool wants attention"


def test_off_plain_permission_record_is_byte_identical_legacy(titles_home, write_config):
    write_config(context_titles=False)
    assert (
        run(
            "PermissionRequest",
            payload_for(
                "PermissionRequest",
                tool_name="Bash",
                tool_input={"command": "git push"},
            ),
        )
        == 0
    )
    rec = spool_records(titles_home)[0]
    assert rec["title"] == "claude permission request"
    assert rec["body"] == 'Bash {"command":"git push"}'
