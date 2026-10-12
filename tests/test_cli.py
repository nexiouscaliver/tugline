"""Tests for the tug CLI (tugline.cli.main).

main(argv) follows the argparse convention: argv is the argument list WITHOUT
the program name (i.e. sys.argv[1:]).
"""

import pytest

from tugline import __version__
from tugline.cli import main


def test_version_prints_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out


def test_hello_prints_one_line(capsys):
    code = main(["hello"])
    assert code == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert lines[0] == "hello from tugline"


def test_hello_mentions_project(capsys):
    code = main(["hello"])
    assert code == 0
    assert "tugline" in capsys.readouterr().out


def test_no_command_prints_usage_and_fails(capsys):
    code = main([])
    assert code == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower() or "usage" in captured.out.lower()


def test_unknown_command_fails(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["bogus"])
    assert excinfo.value.code == 2


# -- core command surface --


import json
import os

import pytest

from fake_ntfy import start_fake_ntfy
from tugline.core import spool as spool_mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_SESSION", raising=False)
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    return tmp_path


def spool_lines(home):
    root = home / ".tugline" / "spool"
    if not root.exists():
        return []
    lines = []
    for day in sorted(root.glob("*.jsonl")):
        lines.extend(json.loads(l) for l in day.read_text().splitlines() if l)
    return lines


def test_version_command_prints_version(capsys):
    assert main(["version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_notify_writes_spool_and_prints_id(home, capsys):
    code = main(["notify", "--title", "build", "--body", "it broke"])
    assert code == 0
    out = capsys.readouterr().out.strip().splitlines()
    spool_id = out[-1]
    records = spool_lines(home)
    assert len(records) == 1
    assert records[0]["id"] == spool_id
    assert records[0]["title"] == "build"
    assert records[0]["body"] == "it broke"
    assert records[0]["topic"] == "main"
    assert records[0]["priority"] == "default"


def test_notify_honors_topic_priority_and_session_env(home, capsys, monkeypatch):
    monkeypatch.setenv("TUGLINE_SESSION", "sess-9")
    code = main(
        ["notify", "--title", "t", "--body", "b", "--topic", "warn", "--priority", "high"]
    )
    assert code == 0
    rec = spool_lines(home)[0]
    assert rec["topic"] == "warn"
    assert rec["priority"] == "high"
    assert rec["session_id"] == "sess-9"


def test_notify_off_in_config_writes_nothing(home, capsys):
    main(["notify", "--title", "warm", "--body", "creates config"])
    config_path = home / ".tugline" / "config.toml"
    config_path.write_text(config_path.read_text().replace("notify = true", "notify = false"))
    capsys.readouterr()

    code = main(["notify", "--title", "t", "--body", "b"])

    assert code == 0
    assert "off" in capsys.readouterr().out
    assert len(spool_lines(home)) == 1


def test_notify_missing_title_fails(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["notify", "--body", "b"])
    assert excinfo.value.code == 2


def test_arm_with_argument_creates_marker(home, capsys):
    code = main(["arm", "sess-1"])
    assert code == 0
    assert (home / ".tugline" / "state" / "armed" / "sess-1").exists()
    assert "armed sess-1" in capsys.readouterr().out


def test_arm_via_env(home, monkeypatch):
    monkeypatch.setenv("TUGLINE_SESSION", "sess-env")
    assert main(["arm"]) == 0
    assert (home / ".tugline" / "state" / "armed" / "sess-env").exists()


def test_disarm_removes_marker(home):
    main(["arm", "sess-1"])
    assert main(["disarm", "sess-1"]) == 0
    assert not (home / ".tugline" / "state" / "armed" / "sess-1").exists()


def test_arm_without_session_or_env_fails(home, capsys):
    assert main(["arm"]) == 2
    assert "session" in capsys.readouterr().err.lower()


def test_test_command_prints_http_code(home, capsys):
    server = start_fake_ntfy()
    try:
        config_path = home / ".tugline" / "config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            '[server]\nurl = "%s"\ntoken_file = "none"\n\n[topics]\nmain = "tug"\nwarn = "tug-warn"\nreply = "tug-reply"\n\n[policy]\nidle_seconds = 600\n\n[toggles]\nnotify = true\nreplies = true\napprove = false\n' % server.url
        )
        code = main(["test"])
        out = capsys.readouterr().out
        assert code == 0
        assert "HTTP 200" in out
        assert len(server.posts("/tug")) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_test_command_failure_prints_code_and_fails(home, capsys):
    server = start_fake_ntfy()
    try:
        config_path = home / ".tugline" / "config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            '[server]\nurl = "%s"\ntoken_file = "none"\n\n[topics]\nmain = "tug"\nwarn = "tug-warn"\nreply = "tug-reply"\n\n[policy]\nidle_seconds = 600\n\n[toggles]\nnotify = true\nreplies = true\napprove = false\n' % server.url
        )
        server.fail_posts = 1
        code = main(["test"])
        assert code == 1
        assert "HTTP 500" in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()


def test_status_reports_service_spool_and_last_reply(home, capsys):
    (home / ".tugline").mkdir(parents=True, exist_ok=True)
    (home / ".tugline" / "instance.json").write_text(
        json.dumps({"port": 2586, "topics": ["tug", "tug-warn", "tug-reply"]})
    )
    spool_mod.append(
        str(home / ".tugline" / "spool"),
        event="notify",
        session_id="s",
        topic="main",
        priority="default",
        title="one",
        body="b",
    )
    replies = home / ".tugline" / "replies.jsonl"
    replies.write_text(
        json.dumps({"id": "r1", "body": "ship it", "consumed": False}) + "\n"
    )

    code = main(["status"])

    assert code == 0
    out = capsys.readouterr().out
    assert "port 2586" in out
    assert "topics tug, tug-warn, tug-reply" in out
    assert "spool: 1 records on file, 1 undelivered" in out
    assert "last reply: ship it (consumed: no)" in out


def test_status_with_no_state(home, capsys):
    code = main(["status"])
    assert code == 0
    out = capsys.readouterr().out
    assert "service: unknown" in out
    assert "spool: 0 records on file, 0 undelivered" in out
    assert "last reply: none" in out


def test_daemon_once_flushes_and_polls(home, capsys):
    server = start_fake_ntfy()
    try:
        config_path = home / ".tugline" / "config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            '[server]\nurl = "%s"\ntoken_file = "none"\n\n[topics]\nmain = "tug"\nwarn = "tug-warn"\nreply = "tug-reply"\n\n[policy]\nidle_seconds = 600\n\n[toggles]\nnotify = true\nreplies = true\napprove = false\n' % server.url
        )
        spool_mod.append(
            str(home / ".tugline" / "spool"),
            event="notify",
            session_id="s",
            topic="main",
            priority="default",
            title="one",
            body="b",
        )
        server.queue_reply("ping", msg_id="r1")

        code = main(["daemon", "--once"])

        assert code == 0
        out = capsys.readouterr().out
        assert "handled 1, replies 1" in out
        assert len(server.posts()) == 1
        replies = [
            json.loads(l)
            for l in (home / ".tugline" / "replies.jsonl").read_text().splitlines()
        ]
        assert replies[0]["id"] == "r1"
    finally:
        server.shutdown()
        server.server_close()
