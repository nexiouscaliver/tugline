"""Tests for the daemon flush loop (tugline.core.daemon)."""

import pytest

from tugline.core import config as cfg
from tugline.core.daemon import Daemon
from tugline.core.spool import append, undelivered


def make_config(url):
    return cfg.Config(
        server=cfg.ServerConfig(url, None),
        topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
        policy=cfg.PolicyConfig(600),
        toggles=cfg.TogglesConfig(True, True, False),
        path="/none",
    )


def make_daemon(fake_ntfy, tmp_path, **kw):
    kw.setdefault("poll_timeout", 2.0)
    kw.setdefault("backoff_base", 0.01)
    kw.setdefault("cycle_idle", 0.0)
    return Daemon(
        make_config(fake_ntfy.url),
        spool_root=str(tmp_path / "spool"),
        replies_path=str(tmp_path / "replies.jsonl"),
        state_root=str(tmp_path / "state"),
        **kw,
    )


def spool_notify(root, title, **kw):
    kw.setdefault("event", "notify")
    kw.setdefault("session_id", "s1")
    kw.setdefault("topic", "tug")
    kw.setdefault("priority", "default")
    kw.setdefault("body", "body of %s" % title)
    return append(str(root), title=title, **kw)


def test_flush_publishes_all_undelivered_records(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = str(tmp_path / "spool")
    for i in range(3):
        spool_notify(root, "n%d" % i)

    delivered = daemon.flush()

    assert delivered == 3
    assert len(fake_ntfy.posts()) == 3
    assert list(undelivered(root)) == []


def test_flush_publishes_body_with_title_and_priority(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    spool_notify(str(tmp_path / "spool"), "hello", priority="high")

    daemon.flush()

    post = fake_ntfy.posts()[0]
    assert post["path"] == "/tug"
    assert post["body"] == "body of hello"
    assert post["headers"].get("x-title") == "hello"
    assert post["headers"].get("x-priority") == "high"


def test_flush_marks_prompt_records_without_publishing(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = str(tmp_path / "spool")
    spool_notify(root, "prompt", event="UserPromptSubmit")

    delivered = daemon.flush()

    assert delivered == 1
    assert fake_ntfy.posts() == []
    assert list(undelivered(root)) == []


# -- reply polling --


def read_replies(tmp_path):
    path = tmp_path / "replies.jsonl"
    if not path.exists():
        return []
    import json as _json

    return [_json.loads(line) for line in path.read_text().splitlines() if line]


def test_poll_captures_reply_messages(fake_ntfy, tmp_path):
    fake_ntfy.queue_reply("looks good, ship it", title="re", msg_id="r7")
    daemon = make_daemon(fake_ntfy, tmp_path)

    count = daemon.poll_replies()

    assert count == 1
    entries = read_replies(tmp_path)
    assert len(entries) == 1
    assert entries[0]["id"] == "r7"
    assert entries[0]["body"] == "looks good, ship it"
    assert entries[0]["title"] == "re"
    assert entries[0]["consumed"] is False


def test_poll_captures_multiple_replies_in_order(fake_ntfy, tmp_path):
    for i in range(3):
        fake_ntfy.queue_reply("reply %d" % i, msg_id="r%d" % i)
    daemon = make_daemon(fake_ntfy, tmp_path)

    assert daemon.poll_replies() == 3
    assert [e["id"] for e in read_replies(tmp_path)] == ["r0", "r1", "r2"]


def test_poll_sends_since_cursor_from_previous_run(fake_ntfy, tmp_path):
    fake_ntfy.queue_reply("one", msg_id="r1")
    daemon = make_daemon(fake_ntfy, tmp_path)
    daemon.poll_replies()
    fake_ntfy.queue_reply("two", msg_id="r2")
    daemon.poll_replies()

    paths = [r["path"] for r in fake_ntfy.gets()]
    assert paths[0].startswith("/tug-reply/json?poll=1&since=")
    assert "since=r1" in paths[1]


def test_poll_with_no_messages_appends_nothing(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    assert daemon.poll_replies() == 0
    assert read_replies(tmp_path) == []


def test_poll_disabled_by_toggle(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    daemon.config.toggles.replies = False
    assert daemon.poll_replies() == 0
    assert fake_ntfy.gets() == []


def test_poll_server_error_logged_not_raised(fake_ntfy, tmp_path, monkeypatch):
    daemon = make_daemon(fake_ntfy, tmp_path)
    fake_ntfy.fail_gets = 5
    lines = []
    monkeypatch.setattr(daemon, "_log_fn", lines.append)
    assert daemon.poll_replies() == 0
    assert any("poll" in line for line in lines)


# -- run_once and serve --


def test_run_once_flushes_and_polls(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = str(tmp_path / "spool")
    spool_notify(root, "a")
    spool_notify(root, "b")
    fake_ntfy.queue_reply("ok", msg_id="r1")

    result = daemon.run_once()

    assert result["handled"] == 2
    assert result["replies"] == 1
    assert len(fake_ntfy.posts()) == 2
    assert len(read_replies(tmp_path)) == 1


def test_serve_runs_cycles_until_stop_requested(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    daemon.config.toggles.replies = False  # keep the cycle to flush only
    cycles = []
    original = daemon.flush

    def counting():
        cycles.append(1)
        if len(cycles) >= 3:
            daemon.request_stop()
        return original()

    daemon.flush = counting
    daemon.serve()
    assert len(cycles) == 3


def test_sigterm_handler_requests_stop(fake_ntfy, tmp_path):
    import signal as signal_mod

    daemon = make_daemon(fake_ntfy, tmp_path)
    daemon._install_signal_handlers()
    try:
        import os as os_mod

        os_mod.kill(os_mod.getpid(), signal_mod.SIGTERM)
        assert daemon._stop is True
    finally:
        signal_mod.signal(signal_mod.SIGTERM, signal_mod.SIG_DFL)


# -- retry and backoff --


def test_retry_until_success(fake_ntfy, tmp_path):
    fake_ntfy.fail_posts = 2
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = str(tmp_path / "spool")
    spool_notify(root, "flaky")

    assert daemon.flush() == 1
    assert len(fake_ntfy.posts()) == 3
    assert list(undelivered(root)) == []


def test_retry_exhaustion_leaves_record_undelivered(fake_ntfy, tmp_path):
    fake_ntfy.fail_posts = 99
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = str(tmp_path / "spool")
    rec = spool_notify(root, "down")

    assert daemon.flush() == 0
    assert len(fake_ntfy.posts()) == 5
    pending = [r["id"] for r in undelivered(root)]
    assert pending == [rec["id"]]


def test_flush_pauses_after_three_failing_records(fake_ntfy, tmp_path):
    fake_ntfy.fail_posts = 99
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = str(tmp_path / "spool")
    for i in range(5):
        spool_notify(root, "r%d" % i)

    daemon.flush()

    assert len(fake_ntfy.posts()) == 15
    assert len(list(undelivered(root))) == 5


def test_failed_records_are_retried_next_cycle(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = str(tmp_path / "spool")
    rec = spool_notify(root, "later")
    fake_ntfy.fail_posts = 5
    daemon.flush()
    fake_ntfy.fail_posts = 0

    assert daemon.flush() == 1
    assert list(undelivered(root)) == []


# -- policy integration through the daemon --


def test_permission_request_publishes_to_warn_topic(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    spool_notify(
        str(tmp_path / "spool"), "may I?", event="PermissionRequest", topic="tug-warn"
    )

    daemon.flush()

    posts = fake_ntfy.posts()
    assert len(posts) == 1
    assert posts[0]["path"] == "/tug-warn"
    assert posts[0]["headers"].get("x-priority") == "high"
    assert "warning" in posts[0]["headers"].get("x-tags", "")


def test_stop_publishes_when_session_armed_and_clears_marker(fake_ntfy, tmp_path):
    from tugline.core import policy as policy_mod

    daemon = make_daemon(fake_ntfy, tmp_path)
    state_root = str(tmp_path / "state")
    policy_mod.arm("s1", state_root=state_root)
    spool_notify(str(tmp_path / "spool"), "done waiting", event="Stop")

    daemon.flush()

    assert len(fake_ntfy.posts()) == 1
    assert not policy_mod.is_armed("s1", state_root=state_root)


def test_short_stop_unarmed_stays_silent(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = str(tmp_path / "spool")
    spool_notify(root, "p", event="UserPromptSubmit")
    spool_notify(root, "s", event="Stop")

    assert daemon.flush() == 2
    assert fake_ntfy.posts() == []


def test_long_stop_publishes_from_session_state(fake_ntfy, tmp_path):
    from datetime import datetime, timedelta, timezone

    daemon = make_daemon(fake_ntfy, tmp_path)
    root = str(tmp_path / "spool")
    start = datetime(2026, 10, 13, 10, 0, 0, tzinfo=timezone.utc)
    append(
        root,
        event="UserPromptSubmit",
        session_id="s1",
        topic="tug",
        priority="default",
        title="p",
        body="",
        now=start,
    )
    append(
        root,
        event="Stop",
        session_id="s1",
        topic="tug",
        priority="default",
        title="s",
        body="",
        now=start + timedelta(seconds=700),
    )

    assert daemon.flush() == 2
    assert len(fake_ntfy.posts()) == 1


def test_notify_toggle_off_marks_records_without_publishing(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    daemon.config.toggles.notify = False
    root = str(tmp_path / "spool")
    spool_notify(root, "quiet")

    assert daemon.flush() == 1
    assert fake_ntfy.posts() == []
    assert list(undelivered(root)) == []


# -- resilience --


def test_corrupt_spool_line_is_logged_and_skipped(fake_ntfy, tmp_path, monkeypatch):
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = tmp_path / "spool"
    spool_notify(str(root), "good")
    day_file = next(root.glob("*.jsonl"))
    day_file.write_text(day_file.read_text() + "{broken line\n")
    lines = []
    monkeypatch.setattr(daemon, "_log_fn", lines.append)

    assert daemon.flush() == 1
    assert len(fake_ntfy.posts()) == 1
    assert any(line.endswith(":2") and "jsonl" in line for line in lines)


def test_record_without_id_is_skipped_not_fatal(fake_ntfy, tmp_path, monkeypatch):
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = tmp_path / "spool"
    spool_notify(str(root), "good")
    day_file = next(root.glob("*.jsonl"))
    day_file.write_text(day_file.read_text() + '{"event": "notify"}\n')
    lines = []
    monkeypatch.setattr(daemon, "_log_fn", lines.append)

    assert daemon.flush() == 1
    assert any("without id" in line for line in lines)
