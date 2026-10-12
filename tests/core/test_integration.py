"""End to end integration: spool records, one daemon cycle, one fake ntfy.

Exercises the whole path with no mocks: spool writes, daemon flush with
policy decisions, HTTP publishes against the in-process fake server,
reply polling with cursor persistence, and delivery marks.
"""

import json

from tugline.core import config as cfg
from tugline.core.daemon import Daemon
from tugline.core.spool import append, undelivered


def test_end_to_end_three_records_delivered_and_replies_captured(tmp_path):
    from fake_ntfy import start_fake_ntfy

    server = start_fake_ntfy()
    try:
        config = cfg.Config(
            server=cfg.ServerConfig(server.url, None),
            topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
            policy=cfg.PolicyConfig(600),
            toggles=cfg.TogglesConfig(True, True, False),
            path="/none",
        )
        daemon = Daemon(
            config,
            spool_root=str(tmp_path / "spool"),
            replies_path=str(tmp_path / "replies.jsonl"),
            state_root=str(tmp_path / "state"),
            poll_timeout=2.0,
            backoff_base=0.01,
            cycle_idle=0.0,
        )

        root = str(tmp_path / "spool")
        append(
            root,
            event="PermissionRequest",
            session_id="sess-e2e",
            topic="warn",
            priority="high",
            title="allow bash?",
            body="rm -rf build/",
        )
        append(
            root,
            event="notify",
            session_id="sess-e2e",
            topic="main",
            priority="default",
            title="note",
            body="spooled note",
        )
        append(
            root,
            event="Stop",
            session_id="sess-e2e",
            topic="main",
            priority="default",
            title="turn done",
            body="short turn",
        )
        server.queue_reply("approved, continue", title="phone", msg_id="rep-1")
        server.queue_reply("second reply", msg_id="rep-2")

        result = daemon.run_once()

        # 3 of 3 records handled and delivered
        assert result["handled"] == 3
        assert list(undelivered(root)) == []

        # the permission request went to warn, the note and the stop to main
        # (the stop is unarmed and short, so only two posts land)
        warn_posts = server.posts("/tug-warn")
        main_posts = server.posts("/tug")
        assert len(warn_posts) == 1
        assert warn_posts[0]["headers"].get("x-priority") == "high"
        assert len(main_posts) == 1
        assert main_posts[0]["body"] == "spooled note"

        # both replies captured, unconsumed, with ids, in order
        entries = [
            json.loads(line)
            for line in (tmp_path / "replies.jsonl").read_text().splitlines()
        ]
        assert [e["id"] for e in entries] == ["rep-1", "rep-2"]
        assert all(e["consumed"] is False for e in entries)
        assert entries[0]["body"] == "approved, continue"
        assert entries[0]["title"] == "phone"

        # a second cycle is a no-op: nothing new to do
        again = daemon.run_once()
        assert again == {"handled": 0, "replies": 0}
        assert len(server.posts()) == 2
    finally:
        server.shutdown()
        server.server_close()
