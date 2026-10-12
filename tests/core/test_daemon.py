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
