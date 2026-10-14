"""Console wake-state display (W8-L19).

The reply journal lines the console writes carry the session binding
(session_id), and GET /api/replies decorates each store row with the
wake state recorded by the daemon dispatcher in <home>/state/wake.jsonl,
so the phone can see dispatched/finished/unsupported per reply.
"""

import json

import pytest

from console.test_server import Bench, wait_for
from tugline.core import paths, spool


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    return tmp_path


@pytest.fixture
def bench(home):
    bench = Bench(home)
    yield bench
    bench.stop()


def seed_idle_session(home, session_id="sess-1", adapter="claude"):
    return spool.append(
        paths.spool_root(),
        event="stop",
        session_id=session_id,
        topic="main",
        priority="default",
        title="turn finished",
        body="session %s turn finished" % session_id,
        extra={"adapter": adapter, "cwd": "/tmp/proj", "project": "proj"},
    )


def post_reply(bench, session_id="sess-1", body="go now"):
    status, raw = bench.request(
        "POST", "/api/replies", body={"session_id": session_id, "body": body}
    )
    return status, json.loads(raw)


def read_journal(home):
    from pathlib import Path

    path = Path(home) / "replies.jsonl"
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def write_wake_journal(home, entries):
    from pathlib import Path

    path = Path(home) / "state" / "wake.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def replies_view(bench):
    status, raw = bench.request("GET", "/api/replies")
    assert status == 200
    return json.loads(raw)["replies"]


# -- the journal line carries the session binding --


def test_post_reply_journal_line_carries_session_id(bench):
    seed_idle_session(bench.home)
    assert wait_for(lambda: bench.store.sessions())

    status, body = post_reply(bench, "sess-1", "wake up")

    assert status == 200
    lines = read_journal(bench.home)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["session_id"] == "sess-1"
    assert entry["body"] == "wake up"
    assert entry["consumed"] is False
    # the store row ts and the journal line ts are the same value, the
    # key the wake journal later records
    assert entry["ts"] == body["reply"]["ts"]


def test_thread_reply_journal_line_carries_session_id(bench):
    from console.test_server import seed_permission_request, wait_for_thread

    seed_permission_request("nonce1", session_id="sess-9")
    wait_for_thread(bench, "nonce1")

    status, body = bench.request(
        "POST", "/api/replies",
        body={"thread_nonce": "nonce1", "body": "approved, continue"},
    )

    assert status == 200
    entry = read_journal(bench.home)[-1]
    assert entry["session_id"] == "sess-9"


# -- wake state decoration --


def test_replies_decorated_with_wake_state(bench):
    seed_idle_session(bench.home)
    assert wait_for(lambda: bench.store.sessions())
    status, body = post_reply(bench, "sess-1", "please continue")
    assert status == 200
    reply = body["reply"]
    write_wake_journal(
        bench.home,
        [
            {
                "ts": "2026-10-14T07:00:01+00:00",
                "event": "dispatched",
                "reply_id": "abc123",
                "session_id": "sess-1",
                "adapter": "claude",
                "reply_ts": reply["ts"],
            },
            {
                "ts": "2026-10-14T07:01:01+00:00",
                "event": "finished",
                "reply_id": "abc123",
                "session_id": "sess-1",
                "adapter": "claude",
                "reply_ts": reply["ts"],
                "exit_code": 0,
                "duration_s": 60.0,
            },
        ],
    )

    rows = replies_view(bench)

    assert rows and rows[0]["wake"] == {
        "state": "finished",
        "ts": "2026-10-14T07:01:01+00:00",
    }


def test_replies_show_unsupported_wake_state(bench):
    seed_idle_session(bench.home, adapter="zcode")
    assert wait_for(lambda: bench.store.sessions())
    status, body = post_reply(bench, "sess-1", "anyone there")
    assert status == 200
    write_wake_journal(
        bench.home,
        [
            {
                "ts": "2026-10-14T07:00:01+00:00",
                "event": "unsupported",
                "session_id": "sess-1",
                "adapter": "zcode",
                "reply_ts": body["reply"]["ts"],
            }
        ],
    )

    rows = replies_view(bench)

    assert rows[0]["wake"]["state"] == "unsupported"


def test_replies_without_wake_events_have_null_state(bench):
    seed_idle_session(bench.home)
    assert wait_for(lambda: bench.store.sessions())
    status, _ = post_reply(bench, "sess-1", "no dispatcher ran")
    assert status == 200

    rows = replies_view(bench)

    assert rows[0]["wake"] is None


def test_wake_state_matches_only_the_same_session(bench):
    seed_idle_session(bench.home, "sess-1")
    seed_idle_session(bench.home, "sess-2")
    assert wait_for(lambda: len(bench.store.sessions()) == 2)
    status, first = post_reply(bench, "sess-1", "one")
    assert status == 200
    status, second = post_reply(bench, "sess-2", "two")
    assert status == 200
    # an identical ts on another session must not leak the state over
    write_wake_journal(
        bench.home,
        [
            {
                "ts": "2026-10-14T07:00:01+00:00",
                "event": "dispatched",
                "session_id": "sess-1",
                "adapter": "claude",
                "reply_ts": first["reply"]["ts"],
            }
        ],
    )

    rows = replies_view(bench)
    by_session = {row["session_id"]: row for row in rows}

    assert by_session["sess-1"]["wake"]["state"] == "dispatched"
    assert by_session["sess-2"]["wake"] is None


# -- the web assets render wake state --


def test_web_assets_carry_the_wake_display(bench):
    status, raw = bench.request("GET", "/app.js")
    assert status == 200
    text = raw.decode()
    # the recent-replies list consumes the wake decoration from the API
    assert "reply.wake" in text
    assert "wake-state" in text

    status, raw = bench.request("GET", "/style.css")
    assert status == 200
    assert "wake-state" in raw.decode()
