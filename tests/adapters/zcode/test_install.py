"""Tests for the zcode config installer and uninstaller.

The installer edits ~/.zcode/cli/config.json (paths overridable; these
tests only ever touch fixture files) by exact-text splicing: every byte
that was there before is still there after, install is idempotent, and
install followed by uninstall restores the original file byte for byte.
"""

import glob
import json
import os

import pytest

from tugline.adapters.zcode import install, uninstall

OPERATOR_CONFIG = {
    "hooks": {
        "enabled": True,
        "events": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/Users/x/.local/bin/bm hook session-start --harness claude 2>/dev/null || true",
                            "timeout": 20,
                            "statusMessage": "Briefing from Basic Memory",
                        }
                    ]
                },
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'printf \'{"additionalContext":"Session id: ${CLAUDE_SESSION_ID}"}\'',
                            "timeout": 5,
                            "statusMessage": "Session id injection",
                        }
                    ]
                },
            ]
        },
    },
    "mcp": {"servers": {"shared-memory": {"command": "bm"}}},
    "permissions": {"allow": ["Bash(git:*)"]},
    "plugins": {"omniforge": {"enabled": True}},
}


def write_config(tmp_path, data, name="config.json", indent=2):
    path = tmp_path / name
    path.write_text(
        json.dumps(data, indent=indent) + "\n", encoding="utf-8"
    )
    return str(path)


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / "tugline-home")


@pytest.fixture
def real_shape(tmp_path):
    return write_config(tmp_path, OPERATOR_CONFIG)


def test_install_adds_four_event_entries(real_shape, home):
    code = install.install(real_shape, home=home)
    assert code == 0
    data = load(real_shape)
    events = data["hooks"]["events"]
    assert set(events) >= {
        "SessionStart",
        "Stop",
        "PermissionRequest",
        "UserPromptSubmit",
    }
    assert len(events["SessionStart"]) == 3
    ours = events["SessionStart"][2]["hooks"][0]
    assert ours["type"] == "command"
    assert ours["command"] == (
        "tug hook-zcode SessionStart # tugline:zcode"
    )
    assert ours["timeout"] == 10
    assert events["PermissionRequest"][0]["hooks"][0]["timeout"] == 310
    assert isinstance(ours["statusMessage"], str) and ours["statusMessage"]


def test_userpromptsubmit_entry_is_registered_with_the_prompt_timeout(
    real_shape, home
):
    code = install.install(real_shape, home=home)
    assert code == 0
    events = load(real_shape)["hooks"]["events"]
    entry = events["UserPromptSubmit"][0]["hooks"][0]
    assert entry["command"] == (
        "tug hook-zcode UserPromptSubmit # tugline:zcode"
    )
    assert entry["timeout"] == 10


def test_reinstall_over_a_legacy_three_event_config_appends_only_the_new_event(
    tmp_path, home
):
    """The live config carries the three pre-W7-L17 events; the
    upgraded installer must append UserPromptSubmit and touch nothing
    else (append-only installer discipline, idempotent)."""
    legacy = dict(OPERATOR_CONFIG)
    legacy["hooks"] = json.loads(json.dumps(OPERATOR_CONFIG["hooks"]))
    legacy["hooks"]["events"]["Stop"] = [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "tug hook-zcode Stop # tugline:zcode",
                    "timeout": 10,
                    "statusMessage": "tugline",
                }
            ]
        }
    ]
    legacy["hooks"]["events"]["PermissionRequest"] = [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "tug hook-zcode PermissionRequest # tugline:zcode",
                    "timeout": 310,
                    "statusMessage": "tugline",
                }
            ]
        }
    ]
    legacy["hooks"]["events"]["SessionStart"].append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "tug hook-zcode SessionStart # tugline:zcode",
                    "timeout": 10,
                    "statusMessage": "tugline",
                }
            ]
        }
    )
    path = write_config(tmp_path, legacy)

    before = read_bytes(path)
    assert install.install(path, home=home) == 0

    data = load(path)
    events = data["hooks"]["events"]
    assert len(events["SessionStart"]) == 3  # untouched
    assert len(events["Stop"]) == 1  # untouched
    assert len(events["PermissionRequest"]) == 1  # untouched
    assert events["UserPromptSubmit"][0]["hooks"][0]["command"] == (
        "tug hook-zcode UserPromptSubmit # tugline:zcode"
    )
    # idempotent: a second run changes nothing
    snapshot = read_bytes(path)
    assert install.install(path, home=home) == 0
    assert read_bytes(path) == snapshot
    assert before != snapshot  # sanity: the first run did append


def test_install_preserves_operator_entries(real_shape, home):
    install.install(real_shape, home=home)
    data = load(real_shape)
    session = data["hooks"]["events"]["SessionStart"]
    assert session[0] == OPERATOR_CONFIG["hooks"]["events"]["SessionStart"][0]
    assert session[1] == OPERATOR_CONFIG["hooks"]["events"]["SessionStart"][1]
    assert data["mcp"] == OPERATOR_CONFIG["mcp"]
    assert data["permissions"] == OPERATOR_CONFIG["permissions"]
    assert data["plugins"] == OPERATOR_CONFIG["plugins"]
    assert data["hooks"]["enabled"] is True


def test_install_is_idempotent(real_shape, home):
    assert install.install(real_shape, home=home) == 0
    snapshot = read_bytes(real_shape)
    assert install.install(real_shape, home=home) == 0
    assert read_bytes(real_shape) == snapshot


def test_round_trip_byte_identical(real_shape, home):
    original = read_bytes(real_shape)
    assert install.install(real_shape, home=home) == 0
    assert read_bytes(real_shape) != original
    assert uninstall.uninstall(real_shape, home=home) == 0
    assert read_bytes(real_shape) == original


def test_uninstall_removes_only_marker_entries(tmp_path, home):
    decoy = json.loads(json.dumps(OPERATOR_CONFIG))
    decoy["hooks"]["events"]["Stop"] = [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "echo tugline is mentioned but not ours",
                    "timeout": 3,
                    "statusMessage": "decoy",
                }
            ]
        }
    ]
    path = write_config(tmp_path, decoy)
    install.install(path, home=home)
    uninstall.uninstall(path, home=home)
    data = load(path)
    stop = data["hooks"]["events"]["Stop"]
    assert len(stop) == 1
    assert stop[0]["hooks"][0]["command"].startswith("echo tugline")
    assert "tugline:zcode" not in json.dumps(data)


def test_uninstall_is_idempotent(real_shape, home):
    install.install(real_shape, home=home)
    uninstall.uninstall(real_shape, home=home)
    snapshot = read_bytes(real_shape)
    assert uninstall.uninstall(real_shape, home=home) == 0
    assert read_bytes(real_shape) == snapshot


def test_install_on_config_without_hooks_round_trips(tmp_path, home):
    path = write_config(tmp_path, {"mcp": {}})
    original = read_bytes(path)
    assert install.install(path, home=home) == 0
    data = load(path)
    assert data["hooks"]["enabled"] is True
    assert set(data["hooks"]["events"]) == {
        "SessionStart",
        "Stop",
        "PermissionRequest",
        "UserPromptSubmit",
    }
    assert uninstall.uninstall(path, home=home) == 0
    assert read_bytes(path) == original


def test_install_on_hooks_without_events_round_trips(tmp_path, home):
    path = write_config(tmp_path, {"hooks": {"enabled": True}, "mcp": {}})
    original = read_bytes(path)
    assert install.install(path, home=home) == 0
    assert uninstall.uninstall(path, home=home) == 0
    assert read_bytes(path) == original


def test_install_on_empty_events_round_trips(tmp_path, home):
    path = write_config(
        tmp_path, {"hooks": {"enabled": True, "events": {}}}
    )
    original = read_bytes(path)
    assert install.install(path, home=home) == 0
    assert uninstall.uninstall(path, home=home) == 0
    assert read_bytes(path) == original


def test_install_on_missing_config_creates_file(tmp_path, home):
    path = str(tmp_path / "fresh" / "config.json")
    assert install.install(path, home=home) == 0
    data = load(path)
    assert set(data["hooks"]["events"]) == {
        "SessionStart",
        "Stop",
        "PermissionRequest",
        "UserPromptSubmit",
    }
    backups = glob.glob(path + ".tugline-bak-*")
    assert backups == []
    assert uninstall.uninstall(path, home=home) == 0
    text = open(path, encoding="utf-8").read()
    assert "tugline:zcode" not in text
    json.loads(text)


def test_install_refuses_malformed_json(tmp_path, home):
    path = tmp_path / "config.json"
    path.write_text("this is not json", encoding="utf-8")
    before = read_bytes(str(path))
    assert install.install(str(path), home=home) == 2
    assert read_bytes(str(path)) == before


def test_install_refuses_wrong_types(tmp_path, home):
    bad = json.loads(json.dumps(OPERATOR_CONFIG))
    bad["hooks"]["events"] = 42
    path = write_config(tmp_path, bad)
    before = read_bytes(path)
    assert install.install(path, home=home) == 2
    assert read_bytes(path) == before

    bad2 = json.loads(json.dumps(OPERATOR_CONFIG))
    bad2["hooks"]["events"]["SessionStart"] = "nope"
    path2 = write_config(tmp_path, bad2, name="config2.json")
    assert install.install(path2, home=home) == 2


def test_backup_created_with_original_bytes(real_shape, home):
    original = read_bytes(real_shape)
    install.install(real_shape, home=home)
    backups = glob.glob(real_shape + ".tugline-bak-*")
    assert len(backups) == 1
    assert read_bytes(backups[0]) == original


def test_atomic_write_leaves_no_tmp_files(real_shape, home, tmp_path):
    install.install(real_shape, home=home)
    uninstall.uninstall(real_shape, home=home)
    leftovers = [
        name
        for name in os.listdir(str(tmp_path))
        if name.endswith(".tmp")
    ]
    assert leftovers == []


def test_restart_warning_printed_once(real_shape, home, capsys):
    install.install(real_shape, home=home)
    out = capsys.readouterr().out
    assert out.count("restarted") == 1


def test_hooks_disabled_warns(tmp_path, home, capsys):
    disabled = json.loads(json.dumps(OPERATOR_CONFIG))
    disabled["hooks"]["enabled"] = False
    path = write_config(tmp_path, disabled)
    assert install.install(path, home=home) == 0
    assert "disabled" in capsys.readouterr().out


def test_sidecar_lives_under_home(real_shape, home, tmp_path):
    install.install(real_shape, home=home)
    sidecar = os.path.join(home, "state", "zcode-install.json")
    assert os.path.isfile(sidecar)
    with open(sidecar, encoding="utf-8") as fh:
        data = json.load(fh)
    assert os.path.abspath(real_shape) in data["configs"]


def test_uninstall_without_sidecar_still_removes_entries(real_shape, home):
    install.install(real_shape, home=home)
    os.remove(os.path.join(home, "state", "zcode-install.json"))
    assert uninstall.uninstall(real_shape, home=home) == 0
    text = open(real_shape, encoding="utf-8").read()
    assert "tugline:zcode" not in text
    data = json.loads(text)
    assert (
        data["hooks"]["events"]["SessionStart"]
        == OPERATOR_CONFIG["hooks"]["events"]["SessionStart"]
    )


def test_install_creates_parent_dirs_for_missing_config(tmp_path, home):
    path = str(tmp_path / "deep" / "nested" / "config.json")
    assert install.install(path, home=home) == 0
    assert os.path.isfile(path)
