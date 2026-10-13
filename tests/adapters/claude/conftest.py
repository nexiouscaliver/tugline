"""Shared fixtures for the Claude Code adapter tests.

The settings model mirrors the operator's real ~/.claude/settings.json:
a hooks object keyed by event name, every event already routed to the
iterm2 status tool, plus foreign top level keys tugline must never
touch. Path and config overrides go through TUGLINE_HOME and
TUGLINE_CONFIG so no test ever reaches the real home directory.
"""

import json

import pytest

CC_STATUS_COMMAND = "/Users/shahil/.config/iterm2/cc-status"

# The ten hook events Claude Code routes today. The status tool owns
# all of them; tugline appends alongside four of them.
WIRED_EVENTS = [
    "SessionStart",
    "Stop",
    "Notification",
    "PermissionRequest",
]

ALL_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "Notification",
    "PermissionRequest",
    "PreToolUse",
    "PostToolUse",
    "PreCompact",
    "SessionEnd",
]

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
approve = %s
"""


def model_settings():
    """A settings file in the shape the operator runs today."""
    hooks = {}
    for event in ALL_EVENTS:
        hooks[event] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "%s %s" % (CC_STATUS_COMMAND, event.lower()),
                    }
                ]
            }
        ]
    return {
        "permissions": {"allow": ["Bash(git status:*)"]},
        "env": {"EDITOR": "vim"},
        "model": "sonnet",
        "hooks": hooks,
    }


def dump_settings(settings):
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


@pytest.fixture
def tug_home(tmp_path, monkeypatch):
    home = tmp_path / "tugline-home"
    monkeypatch.setenv("TUGLINE_HOME", str(home))
    return home


@pytest.fixture
def settings_model():
    return model_settings()


@pytest.fixture
def settings_path(tmp_path, settings_model):
    path = tmp_path / "settings.json"
    path.write_text(dump_settings(settings_model), encoding="utf-8")
    return path


@pytest.fixture
def settings_text(settings_path):
    return settings_path.read_text(encoding="utf-8")


@pytest.fixture
def hook_payload():
    def build(event, **extra):
        payload = {
            "session_id": "sess-4f2a",
            "transcript_path": "/tmp/transcripts/sess-4f2a.jsonl",
            "cwd": "/Users/shahil/work/tugline",
            "hook_event_name": event,
            "prompt_id": "prompt-91",
            "permission_mode": "default",
        }
        payload.update(extra)
        return payload

    return build


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    def write(approve=False, notify=True):
        text = CONFIG_TEXT % ("true" if approve else "false")
        if not notify:
            text = text.replace("notify = true", "notify = false")
        path = tmp_path / "config.toml"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv("TUGLINE_CONFIG", str(path))
        return path

    return write


@pytest.fixture
def spool_records(tug_home):
    def read_all():
        records = []
        spool_dir = tug_home / "spool"
        if not spool_dir.is_dir():
            return records
        for day in sorted(spool_dir.glob("*.jsonl")):
            for line in day.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
        return records

    return read_all
