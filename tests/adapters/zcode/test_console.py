"""Console surfaces: tug hook-zcode, tug install zcode, tug uninstall zcode.

The zcode commands dispatch through the adapter package before argparse
sees argv, so the core surface set and its dispatch stay untouched.
"""

import io
import json
import os
import sys

import pytest

from tugline import cli
from tugline.core import spool

CONFIG_TEXT = """
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
def env(tmp_path, monkeypatch):
    home = tmp_path / "tugline-home"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG_TEXT, encoding="utf-8")
    monkeypatch.setenv("TUGLINE_ZCODE_HOME", str(home))
    monkeypatch.setenv("TUGLINE_CONFIG", str(config))
    return str(home)


def test_tug_hook_zcode_dispatches(tmp_path, monkeypatch, env):
    payload = '{"hookEventName":"Stop","sessionId":"s-cli"}'
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as exc:
        cli.main(["hook-zcode", "Stop"])
    assert exc.value.code == 0
    records = list(spool.records(os.path.join(env, "spool")))
    assert len(records) == 1
    assert records[0]["event"] == "stop"
    assert records[0]["session_id"] == "s-cli"


def test_tug_hook_zcode_without_event_arg_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["hook-zcode"])
    assert exc.value.code == 2


def test_tug_install_zcode_dispatches(tmp_path, env):
    config = tmp_path / "zcode-config.json"
    config.write_text('{"mcp": {}}', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["install", "zcode", "--config", str(config)])
    assert exc.value.code == 0
    data = json.loads(config.read_text(encoding="utf-8"))
    assert set(data["hooks"]["events"]) == {
        "SessionStart",
        "Stop",
        "PermissionRequest",
        "UserPromptSubmit",
    }


def test_tug_uninstall_zcode_dispatches(tmp_path, env):
    config = tmp_path / "zcode-config.json"
    config.write_text('{"mcp": {}}', encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(["install", "zcode", "--config", str(config)])
    with pytest.raises(SystemExit) as exc:
        cli.main(["uninstall", "zcode", "--config", str(config)])
    assert exc.value.code == 0
    text = config.read_text(encoding="utf-8")
    assert "tugline:zcode" not in text


def test_install_without_zcode_falls_through_to_argparse():
    with pytest.raises(SystemExit):
        cli.main(["install"])


def test_hello_still_works(capsys):
    assert cli.main(["hello"]) == 0
    assert "hello from tugline" in capsys.readouterr().out
