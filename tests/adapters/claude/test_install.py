"""Installer tests: coexistence with the existing cc-status routing.

The contract under test, in one line each: tugline entries are
appended after the foreign ones and never reorder them, a round trip
through install and uninstall restores the original bytes exactly,
and the marker token tugline:claude is the only thing the uninstaller
is allowed to remove.
"""

import json
import os
from datetime import datetime

import pytest

from tugline.adapters.claude import install as install_module
from tugline.adapters.claude import uninstall as uninstall_module
from tugline.adapters.claude.install import MARKER

WIRED_EVENTS = [
    "SessionStart",
    "Stop",
    "Notification",
    "PermissionRequest",
]


def read(path):
    return path.read_text(encoding="utf-8")


def test_install_appends_tugline_groups_after_existing_entries(settings_path):
    result = install_module.install(settings_path)
    assert result["status"] == "installed"
    data = json.loads(read(settings_path))
    for event in WIRED_EVENTS:
        event_groups = data["hooks"][event]
        assert len(event_groups) == 2
        foreign = event_groups[0]["hooks"][0]["command"]
        assert foreign.startswith("/Users/shahil/.config/iterm2/cc-status")
        tug_hook = event_groups[1]["hooks"][0]
        assert tug_hook["type"] == "command"
        assert event in tug_hook["command"]
        assert MARKER in tug_hook["command"]


def test_round_trip_restores_the_original_bytes(settings_path, settings_text):
    install_module.install(settings_path)
    result = uninstall_module.uninstall(settings_path)
    assert result["removed"] == len(WIRED_EVENTS)
    assert read(settings_path) == settings_text


def test_foreign_events_and_keys_survive_install(settings_path):
    before = json.loads(read(settings_path))
    install_module.install(settings_path)
    after = json.loads(read(settings_path))
    assert after["permissions"] == before["permissions"]
    assert after["model"] == before["model"]
    assert after["env"] == before["env"]
    assert list(after["hooks"]) == list(before["hooks"])
    for event in before["hooks"]:
        if event not in WIRED_EVENTS:
            assert after["hooks"][event] == before["hooks"][event]
        else:
            assert after["hooks"][event][0] == before["hooks"][event][0]


def test_second_install_refuses_and_writes_nothing(settings_path):
    install_module.install(settings_path)
    installed_text = read(settings_path)
    os.chmod(settings_path, 0o444)
    try:
        result = install_module.install(settings_path)
    finally:
        os.chmod(settings_path, 0o644)
    assert result["status"] == "already-installed"
    assert result["changed"] is False
    assert read(settings_path) == installed_text


def test_install_writes_timestamped_backup_of_original_bytes(
    settings_path, settings_text
):
    stamp = datetime(2026, 10, 13, 5, 5, 5)
    result = install_module.install(settings_path, now=stamp)
    assert result["backup"].endswith("settings.json.tugline-20261013T050505")
    with open(result["backup"], encoding="utf-8") as fh:
        assert fh.read() == settings_text


def test_install_warns_once_about_session_restart(settings_path, capsys):
    install_module.install(settings_path)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("restart") == 1


def test_install_on_settings_without_hooks_creates_section(tmp_path):
    original = {"permissions": {"allow": []}, "model": "sonnet"}
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(original, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    original_text = read(path)
    install_module.install(path)
    data = json.loads(read(path))
    assert sorted(data["hooks"]) == sorted(WIRED_EVENTS)
    uninstall_module.uninstall(path)
    assert read(path) == original_text


def test_install_creates_a_missing_settings_file(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    result = install_module.install(path)
    assert result["status"] == "installed"
    assert result["backup"] is None
    data = json.loads(read(path))
    assert sorted(data["hooks"]) == sorted(WIRED_EVENTS)


def test_install_rejects_broken_settings_without_writing(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(install_module.SettingsError):
        install_module.install(path)
    assert read(path) == "{not json"


def test_uninstall_removes_only_marked_hooks_inside_mixed_groups(settings_path):
    install_module.install(settings_path)
    data = json.loads(read(settings_path))
    tug_group = data["hooks"]["Stop"][1]
    tug_group["hooks"].append(
        {"type": "command", "command": "/usr/local/bin/other-tool stop"}
    )
    path = settings_path
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    result = uninstall_module.uninstall(path)
    assert result["removed"] == len(WIRED_EVENTS)
    after = json.loads(read(path))
    stop_groups = after["hooks"]["Stop"]
    assert len(stop_groups) == 2
    assert stop_groups[1]["hooks"] == [
        {"type": "command", "command": "/usr/local/bin/other-tool stop"}
    ]
    assert MARKER not in read(path)


def test_uninstall_on_clean_file_changes_nothing(settings_path):
    result = uninstall_module.uninstall(settings_path)
    assert result["status"] == "clean"
    assert result["removed"] == 0
    os.chmod(settings_path, 0o444)
    try:
        again = uninstall_module.uninstall(settings_path)
    finally:
        os.chmod(settings_path, 0o644)
    assert again["status"] == "clean"
    assert again["removed"] == 0


def test_uninstall_on_missing_file_reports_missing(tmp_path):
    result = uninstall_module.uninstall(tmp_path / "nope.json")
    assert result["status"] == "missing"
    assert result["removed"] == 0


def test_hook_command_carries_event_and_marker():
    command = install_module.hook_command("Stop")
    assert command == "tug hook-claude Stop # " + MARKER
    custom = install_module.hook_command("Stop", command="/venv/bin/tug")
    assert custom.startswith("/venv/bin/tug hook-claude Stop")
    assert install_module.is_marked({"type": "command", "command": command})
    assert not install_module.is_marked(
        {"type": "command", "command": "tug hook-claude Stop"}
    )


def test_permission_request_entry_carries_a_longer_timeout(settings_path):
    install_module.install(settings_path)
    data = json.loads(read(settings_path))
    permission = data["hooks"]["PermissionRequest"][1]["hooks"][0]
    stop = data["hooks"]["Stop"][1]["hooks"][0]
    assert permission["timeout"] == 320
    assert stop["timeout"] == 20
