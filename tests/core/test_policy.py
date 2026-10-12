"""Tests for the arming and notification policy (tugline.core.policy)."""

import json

import pytest

from tugline.core.policy import (
    SessionState,
    arm,
    decide,
    disarm,
    is_armed,
    load_session,
    record_prompt,
    record_stop,
)


def state(**kw):
    base = dict(session_id="s1", armed=False, turn_seconds=None, idle_seconds=600)
    base.update(kw)
    return SessionState(**base)


# decide() rules


def test_permission_request_always_publishes_high_to_warn():
    d = decide({"hook_event_name": "PermissionRequest"}, state())
    assert d.action == "publish"
    assert d.topic == "warn"
    assert d.priority == "high"


def test_permission_request_publishes_even_when_armed_off_short_turn():
    d = decide(
        {"hook_event_name": "PermissionRequest", "session_id": "s1"},
        state(armed=False, turn_seconds=1.0),
    )
    assert d.action == "publish"


def test_session_start_publishes_on_resume():
    d = decide(
        {"hook_event_name": "SessionStart", "source": "resume"}, state()
    )
    assert d.action == "publish"
    assert d.topic == "main"


def test_session_start_startup_suppressed():
    d = decide(
        {"hook_event_name": "SessionStart", "source": "startup"}, state()
    )
    assert d.action == "suppress"


def test_session_start_without_source_suppressed():
    d = decide({"hook_event_name": "SessionStart"}, state())
    assert d.action == "suppress"


def test_stop_publishes_when_session_armed():
    d = decide({"hook_event_name": "Stop"}, state(armed=True))
    assert d.action == "publish"
    assert d.topic == "main"


def test_stop_publishes_when_turn_over_idle_seconds():
    d = decide(
        {"hook_event_name": "Stop"},
        state(armed=False, turn_seconds=900.0, idle_seconds=600),
    )
    assert d.action == "publish"


def test_stop_suppressed_when_short_and_not_armed():
    d = decide(
        {"hook_event_name": "Stop"},
        state(armed=False, turn_seconds=60.0, idle_seconds=600),
    )
    assert d.action == "suppress"


def test_stop_suppressed_when_no_turn_data():
    d = decide({"hook_event_name": "Stop"}, state(armed=False, turn_seconds=None))
    assert d.action == "suppress"


def test_user_prompt_submit_never_publishes():
    d = decide({"hook_event_name": "UserPromptSubmit"}, state())
    assert d.action == "suppress"


def test_notify_event_publishes_with_record_topic_and_priority():
    d = decide(
        {"event": "notify", "topic": "tug-custom", "priority": "low"}, state()
    )
    assert d.action == "publish"
    assert d.topic == "tug-custom"
    assert d.priority == "low"


def test_notify_event_defaults_to_main_default():
    d = decide({"event": "notify"}, state())
    assert d.action == "publish"
    assert d.topic == "main"
    assert d.priority == "default"


def test_unknown_event_suppressed():
    d = decide({"hook_event_name": "PreToolUse"}, state())
    assert d.action == "suppress"


def test_decide_reads_event_name_from_record_spelling():
    d = decide({"event": "PermissionRequest"}, state())
    assert d.action == "publish"
    assert d.topic == "warn"


def test_event_names_matched_case_insensitively():
    d = decide({"hook_event_name": "permissionrequest"}, state())
    assert d.action == "publish"


# arm markers and session state files


def test_arm_creates_marker(tmp_path):
    arm("s1", state_root=tmp_path)
    assert (tmp_path / "armed" / "s1").exists()
    assert is_armed("s1", state_root=tmp_path)


def test_disarm_removes_marker(tmp_path):
    arm("s1", state_root=tmp_path)
    disarm("s1", state_root=tmp_path)
    assert not is_armed("s1", state_root=tmp_path)


def test_disarm_on_never_armed_session_is_silent(tmp_path):
    disarm("ghost", state_root=tmp_path)
    assert not is_armed("ghost", state_root=tmp_path)


def test_is_armed_false_when_no_state_dir(tmp_path):
    assert not is_armed("s1", state_root=tmp_path)


def test_arm_rejects_unsafe_session_id(tmp_path):
    with pytest.raises(ValueError):
        arm("../escape", state_root=tmp_path)


def test_record_prompt_then_stop_computes_turn_seconds(tmp_path):
    record_prompt("s1", 1000.0, state_root=tmp_path)
    record_stop("s1", 1090.5, state_root=tmp_path)
    s = load_session("s1", state_root=tmp_path)
    assert s["last_prompt_ts"] == 1000.0
    assert s["last_stop_ts"] == 1090.5
    assert s["last_turn_seconds"] == pytest.approx(90.5)


def test_record_stop_without_prompt_stores_stop_only(tmp_path):
    record_stop("s1", 2000.0, state_root=tmp_path)
    s = load_session("s1", state_root=tmp_path)
    assert s["last_stop_ts"] == 2000.0
    assert "last_turn_seconds" not in s


def test_load_session_missing_returns_empty(tmp_path):
    assert load_session("nope", state_root=tmp_path) == {}


def test_load_session_corrupt_file_returns_empty(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "bad.json").write_text("{not json")
    assert load_session("bad", state_root=tmp_path) == {}


def test_session_state_file_is_json(tmp_path):
    record_prompt("s1", 1000.0, state_root=tmp_path)
    path = tmp_path / "sessions" / "s1.json"
    data = json.loads(path.read_text())
    assert data["last_prompt_ts"] == 1000.0
