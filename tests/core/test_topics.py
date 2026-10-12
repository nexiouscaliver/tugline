"""Tests for topic name resolution (tugline.core.topics)."""

from tugline.core import config as cfg
from tugline.core.topics import names, resolve, tags_for


def conf():
    return cfg.Config(
        server=cfg.ServerConfig("http://127.0.0.1:2586", "/creds"),
        topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
        policy=cfg.PolicyConfig(600),
        toggles=cfg.TogglesConfig(True, True, False),
        path="/none",
    )


def test_resolve_main_warn_reply():
    c = conf()
    assert resolve("main", c) == "tug"
    assert resolve("warn", c) == "tug-warn"
    assert resolve("reply", c) == "tug-reply"


def test_resolve_custom_topic_passes_through():
    assert resolve("tug-custom", conf()) == "tug-custom"


def test_names_returns_all_configured_topics():
    assert names(conf()) == {
        "main": "tug",
        "warn": "tug-warn",
        "reply": "tug-reply",
    }


def test_tags_for_known_events():
    assert "warning" in tags_for("PermissionRequest")
    assert tags_for("Stop") == ["white_check_mark"]
    assert tags_for("SessionStart") == ["arrow_forward"]
    assert tags_for("notify") == ["mailbox"]


def test_tags_for_unknown_event_empty():
    assert tags_for("Whatever") == []
    assert tags_for("") == []
