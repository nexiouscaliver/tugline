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
    "UserPromptSubmit",
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


def test_userpromptsubmit_entry_is_registered(settings_path,
                                              fixed_resolver):
    result = install_module.install(settings_path)
    assert result["status"] == "installed"
    data = json.loads(read(settings_path))
    groups = data["hooks"]["UserPromptSubmit"]
    assert len(groups) == 2  # the status tool's group plus tugline's
    tug_hook = groups[1]["hooks"][0]
    assert tug_hook["command"] == (
        "%s hook-claude UserPromptSubmit # %s"
        % (fixed_resolver, MARKER)
    )
    assert tug_hook["timeout"] == 20


def test_reinstall_over_a_four_event_config_appends_only_userpromptsubmit(
    settings_path,
):
    """The live settings carry tugline entries for the four pre-W7-L17
    events. The installer is append-only per event: it must add the
    missing UserPromptSubmit group and leave every existing byte of
    tugline and foreign wiring alone."""
    settings_path.write_text(
        json.dumps(_legacy_four_event_settings(), indent=2) + "\n",
        encoding="utf-8",
    )
    before = read(settings_path)

    result = install_module.install(settings_path)

    assert result["status"] == "installed"
    assert result["changed"] is True
    data = json.loads(read(settings_path))
    for event in ["SessionStart", "Stop", "Notification", "PermissionRequest"]:
        assert len(data["hooks"][event]) == 2  # untouched foreign + tugline
    assert len(data["hooks"]["UserPromptSubmit"]) == 2
    second = install_module.install(settings_path)
    assert second["status"] == "already-installed"
    assert second["changed"] is False
    assert read(settings_path) != before


def _legacy_four_event_settings():
    """Settings exactly as the live host had them before W7-L17: every
    event routed to the status tool, tugline appended to the four
    pre-W7-L17 events, nothing on UserPromptSubmit beyond the status
    tool."""
    hooks = {}
    for event in [
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
    ]:
        hooks[event] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "/Users/shahil/.config/iterm2/cc-status %s"
                        % event.lower(),
                    }
                ]
            }
        ]
    for event in ["SessionStart", "Stop", "Notification", "PermissionRequest"]:
        hooks[event].append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "tug hook-claude %s # %s" % (event, MARKER),
                        "timeout": 20,
                    }
                ]
            }
        )
    return {"permissions": {"allow": []}, "model": "sonnet", "hooks": hooks}


def test_install_command_override_lands_in_every_entry(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    install_module.install(path, command="/opt/venv/bin/tug")
    data = json.loads(read(path))
    for event in WIRED_EVENTS:
        command = data["hooks"][event][0]["hooks"][0]["command"]
        assert command.startswith("/opt/venv/bin/tug hook-claude %s" % event)


# -- absolute hook commands (W10-L23) --
#
# The daemon (launchd) and the sessions it wakes run on
# PATH=/usr/bin:/bin:/usr/sbin:/sbin, where a bare "tug" cannot
# resolve: every hook in a wake-spawned session silently failed. New
# installs embed the absolute tug path, and existing installs with
# bare commands are upgraded in place (still idempotent).


@pytest.fixture
def fixed_resolver(monkeypatch):
    """Pin the resolver so assertions do not depend on the machine."""
    monkeypatch.setattr(
        install_module, "absolute_command",
        lambda name: "/opt/tug-test/bin/tug",
    )
    return "/opt/tug-test/bin/tug"


def test_absolute_command_resolves_via_which(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tug = bindir / "tug"
    tug.write_text("#!/bin/sh\n", encoding="utf-8")
    tug.chmod(0o755)
    resolved = install_module.absolute_command(
        "tug", which=lambda name: str(tug)
    )
    assert resolved == str(tug)


def test_absolute_command_falls_back_to_the_interpreter_dir(
    tmp_path, monkeypatch
):
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    tug = bindir / "tug"
    tug.write_text("#!/bin/sh\n", encoding="utf-8")
    tug.chmod(0o755)
    monkeypatch.setattr(install_module.sys, "executable",
                        str(bindir / "python"))
    resolved = install_module.absolute_command(
        "tug", which=lambda name: None
    )
    assert resolved == str(tug)


def test_absolute_command_stays_bare_when_unresolvable():
    name = "definitely-not-a-real-tug-binary"
    assert (
        install_module.absolute_command(name, which=lambda n: None)
        == name
    )


def test_absolute_command_passes_explicit_paths_through():
    assert (
        install_module.absolute_command("/opt/venv/bin/tug")
        == "/opt/venv/bin/tug"
    )


def test_install_embeds_the_absolute_tug_path(settings_path,
                                              fixed_resolver):
    result = install_module.install(settings_path)
    assert result["status"] == "installed"
    data = json.loads(read(settings_path))
    for event in WIRED_EVENTS:
        command = data["hooks"][event][1]["hooks"][0]["command"]
        assert command.startswith(
            "%s hook-claude %s" % (fixed_resolver, event)
        )
        assert MARKER in command


def test_existing_bare_commands_are_upgraded_in_place(
    settings_path, fixed_resolver
):
    install_module.install(settings_path)
    bare = json.loads(read(settings_path))
    for event in WIRED_EVENTS:
        bare["hooks"][event][1]["hooks"][0]["command"] = (
            "tug hook-claude %s # %s" % (event, MARKER)
        )
    settings_path.write_text(
        json.dumps(bare, indent=2) + "\n", encoding="utf-8"
    )

    result = install_module.install(settings_path)

    assert result["status"] == "installed"
    assert result["changed"] is True
    assert result["upgraded"] == len(WIRED_EVENTS)
    assert result["backup"] is not None
    data = json.loads(read(settings_path))
    for event in WIRED_EVENTS:
        command = data["hooks"][event][1]["hooks"][0]["command"]
        assert command == "%s hook-claude %s # %s" % (
            fixed_resolver, event, MARKER
        )
    # and the upgrade is itself idempotent
    installed_text = read(settings_path)
    again = install_module.install(settings_path)
    assert again["status"] == "already-installed"
    assert again["changed"] is False
    assert read(settings_path) == installed_text


def test_upgrade_leaves_explicit_paths_alone(settings_path,
                                             fixed_resolver):
    """A mixed install: four events carry bare pre-fix commands, one
    carries an explicit path. Only the bare ones are rewritten."""
    install_module.install(settings_path)
    data = json.loads(read(settings_path))
    explicit = "/custom/venv/bin/tug"
    for event in WIRED_EVENTS:
        if event == "Stop":
            command = "%s hook-claude %s # %s" % (explicit, event, MARKER)
        else:
            command = "tug hook-claude %s # %s" % (event, MARKER)
        data["hooks"][event][1]["hooks"][0]["command"] = command
    settings_path.write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )

    result = install_module.install(settings_path)

    assert result["upgraded"] == len(WIRED_EVENTS) - 1
    after = json.loads(read(settings_path))
    assert after["hooks"]["Stop"][1]["hooks"][0]["command"] == (
        "%s hook-claude Stop # %s" % (explicit, MARKER)
    )
