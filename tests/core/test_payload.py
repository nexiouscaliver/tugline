"""Tests for hook payload normalization (tugline.core.payload)."""

import pytest

from tugline.core.payload import normalize


def test_camel_case_keys_normalized():
    raw = {
        "hookEventName": "Stop",
        "sessionId": "sess-1",
        "transcriptPath": "/tmp/t.jsonl",
        "toolName": "Bash",
        "toolInput": {"command": "ls"},
        "responsePreview": "ok",
        "stopHookActive": False,
    }
    out = normalize(raw)
    assert out["hook_event_name"] == "Stop"
    assert out["session_id"] == "sess-1"
    assert out["transcript_path"] == "/tmp/t.jsonl"
    assert out["tool_name"] == "Bash"
    assert out["tool_input"] == {"command": "ls"}
    assert out["response_preview"] == "ok"
    assert out["stop_hook_active"] is False


def test_snake_case_keys_pass_through():
    raw = {
        "hook_event_name": "SessionStart",
        "session_id": "sess-2",
        "transcript_path": "/tmp/t2.jsonl",
        "stop_hook_active": True,
    }
    out = normalize(raw)
    assert out["hook_event_name"] == "SessionStart"
    assert out["session_id"] == "sess-2"
    assert out["transcript_path"] == "/tmp/t2.jsonl"
    assert out["stop_hook_active"] is True


def test_snake_case_wins_when_both_spellings_present():
    raw = {
        "hook_event_name": "Stop",
        "hookEventName": "PreToolUse",
        "session_id": "from-snake",
        "sessionId": "from-camel",
    }
    out = normalize(raw)
    assert out["hook_event_name"] == "Stop"
    assert out["session_id"] == "from-snake"


def test_camel_spelling_dropped_from_output():
    raw = {"sessionId": "sess-3", "hookEventName": "Stop"}
    out = normalize(raw)
    assert "sessionId" not in out
    assert "hookEventName" not in out


def test_last_assistant_message_snake_kept():
    raw = {"hook_event_name": "Stop", "last_assistant_message": "done"}
    out = normalize(raw)
    assert out["last_assistant_message"] == "done"


def test_last_assistant_message_camel_accepted():
    raw = {"lastAssistantMessage": "done"}
    out = normalize(raw)
    assert out["last_assistant_message"] == "done"


def test_unknown_keys_preserved():
    raw = {
        "hook_event_name": "SessionStart",
        "source": "resume",
        "cwd": "/tmp/project",
        "permission_mode": "default",
    }
    out = normalize(raw)
    assert out["source"] == "resume"
    assert out["cwd"] == "/tmp/project"
    assert out["permission_mode"] == "default"


def test_missing_optional_keys_absent():
    out = normalize({"hook_event_name": "Stop"})
    assert "session_id" not in out
    assert "tool_name" not in out
    assert "last_assistant_message" not in out


def test_input_not_mutated():
    raw = {"hookEventName": "Stop", "sessionId": "sess-4", "source": "resume"}
    snapshot = dict(raw)
    normalize(raw)
    assert raw == snapshot


def test_empty_payload_returns_empty_dict():
    assert normalize({}) == {}


def test_non_mapping_input_raises():
    with pytest.raises(TypeError):
        normalize("not a dict")
    with pytest.raises(TypeError):
        normalize(None)


def test_list_payload_raises():
    with pytest.raises(TypeError):
        normalize([{"hookEventName": "Stop"}])
