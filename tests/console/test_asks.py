"""Console ask answering (W9-L22): MCQ threads answer from the page.

POST /api/asks/<nonce> records a tap exactly like a bound reply (one
store reply row with source console plus the replies.jsonl line whose
body is the bare option key, so every existing ask consumer sees it)
and closes the thread itself, so the card flips without waiting for
the asking process to poll. The endpoint sits under the same token
gate as every /api route; an already-answered ask answers 200 again
with the recorded key and never writes a second reply row.
"""

import json

import pytest

from console.test_server import Bench, seed_permission_request, wait_for, wait_for_thread
from tugline.core import paths, spool

OPTIONS = [
    {"key": "A", "text": "ship the release"},
    {"key": "B", "text": "roll back and investigate"},
]


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


def seed_ask_session(home, session_id="sess-ask"):
    """One session the ask thread can bind to, with identity fields."""
    return spool.append(
        paths.spool_root(),
        event="userpromptsubmit",
        session_id=session_id,
        topic="main",
        priority="default",
        title="prompt submitted",
        body="prompt: which deploy path?",
        extra={"adapter": "zcode", "cwd": "/w/tugline", "project": "tugline"},
    )


def open_ask(bench, nonce="ask0001", session_id="sess-ask"):
    seed_ask_session(bench.home, session_id)
    assert wait_for(
        lambda: any(
            s["session_id"] == session_id for s in bench.store.sessions()
        )
    ), "session %s never appeared" % session_id
    return bench.store.create_ask_thread(
        session_id, "which deploy path?", OPTIONS, nonce=nonce
    )


def thread_row(bench, nonce):
    rows = [t for t in bench.store.threads() if t["nonce"] == nonce]
    assert rows, "thread %s missing from the store" % nonce
    return rows[0]


def read_journal(home):
    from pathlib import Path

    path = Path(home) / "replies.jsonl"
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


# -- the happy path: one tap answers, records, closes --


def test_ask_answer_records_a_bound_reply_and_closes_the_thread(bench):
    open_ask(bench)

    status, data = bench.post_json("/api/asks/ask0001", {"key": "b"})

    assert status == 200
    assert data["ok"] is True
    assert data["status"] == "answered"
    assert data["key"] == "B"  # the key lands canonical (upper)
    thread = thread_row(bench, "ask0001")
    assert thread["status"] == "answered"
    assert thread["answer"] == "B"
    assert thread["answer_ts"]
    # recorded exactly like a bound reply: console source, thread
    # binding, inherited session id, body the bare option key
    replies = bench.store.replies()
    assert len(replies) == 1
    assert replies[0]["thread_nonce"] == "ask0001"
    assert replies[0]["session_id"] == "sess-ask"
    assert replies[0]["source"] == "console"
    assert replies[0]["body"] == "B"
    entry = read_journal(bench.home)[-1]
    assert entry["body"] == "B"
    assert entry["session_id"] == "sess-ask"
    assert entry["source"] == "console"


def test_threads_read_carries_the_ask_state_for_the_card(bench):
    open_ask(bench)
    status, _ = bench.post_json("/api/asks/ask0001", {"key": "A"})
    assert status == 200

    status, data = bench.get_json("/api/threads")

    assert status == 200
    row = [t for t in data["threads"] if t["nonce"] == "ask0001"][0]
    assert row["kind"] == "ask"
    assert row["options"] == OPTIONS
    assert row["status"] == "answered"
    assert row["answer"] == "A"
    assert row["title"] == "which deploy path?"


# -- errors and idempotency --


def test_ask_answer_unknown_nonce_is_404(bench):
    status, data = bench.post_json("/api/asks/ghost", {"key": "A"})
    assert status == 404
    assert data["error"]


def test_ask_answer_already_answered_is_200_idempotent(bench):
    open_ask(bench)
    status, first = bench.post_json("/api/asks/ask0001", {"key": "A"})
    assert status == 200
    assert first["status"] == "answered"

    # a second tap, even for another letter, is the approval posture:
    # 200 with the recorded answer, never a second reply row
    status, second = bench.post_json("/api/asks/ask0001", {"key": "B"})

    assert status == 200
    assert second["ok"] is True
    assert second["status"] == "already answered"
    assert second["key"] == "A"
    thread = thread_row(bench, "ask0001")
    assert thread["status"] == "answered"
    assert thread["answer"] == "A"
    assert len(bench.store.replies()) == 1
    assert len(read_journal(bench.home)) == 1


def test_ask_answer_expired_ask_is_409(bench):
    open_ask(bench)
    assert bench.store.expire_ask_thread("ask0001") is True

    status, data = bench.post_json("/api/asks/ask0001", {"key": "A"})

    assert status == 409
    assert data["error"]
    assert bench.store.replies() == []


def test_ask_answer_wrong_key_is_400(bench):
    open_ask(bench)

    status, data = bench.post_json("/api/asks/ask0001", {"key": "Z"})

    assert status == 400
    assert data["error"]
    thread = thread_row(bench, "ask0001")
    assert thread["status"] == "open"
    assert bench.store.replies() == []


def test_ask_answer_on_an_approve_thread_is_400(bench):
    seed_permission_request("nonce-1")
    wait_for_thread(bench, "nonce-1")

    status, data = bench.post_json("/api/asks/nonce-1", {"key": "A"})

    assert status == 400
    assert "ask" in data["error"]


def test_ask_answer_bad_body_is_400(bench):
    open_ask(bench)
    status, data = bench.post_json("/api/asks/ask0001", {"key": 7})
    assert status == 400
    assert data["error"] == "key must be a string"
    status, data = bench.post_json("/api/asks/ask0001", {})
    assert status == 400
    status, raw = bench.request("POST", "/api/asks/ask0001", body=b"{not json")
    assert status == 400
    assert json.loads(raw)["error"] == "body must be json"


# -- auth posture --


def test_ask_answer_sits_under_the_token_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    (tmp_path / "credentials").write_text("sekret\n")
    bench = Bench(tmp_path, token_file=tmp_path / "credentials")
    try:
        open_ask(bench)

        status, data = bench.post_json("/api/asks/ask0001", {"key": "A"})
        assert status == 403
        assert data["error"] == "bad token"

        status, _ = bench.post_json(
            "/api/asks/ask0001",
            {"key": "A"},
            headers={"Authorization": "Bearer sekret"},
        )
        assert status == 200
    finally:
        bench.stop()


# -- the web assets render ask cards and the grouped stream --


def test_web_assets_carry_the_ask_cards_and_grouped_stream(bench):
    status, raw = bench.request("GET", "/app.js")
    assert status == 200
    text = raw.decode()
    # the option rows POST to the ask endpoint, letter in monospace
    assert "/api/asks/" in text
    assert "ask-opt" in text
    assert "opt-key" in text
    # the grouped stream: sections, headers that filter, the toggle
    assert "sgroup" in text
    assert "group by session" in text
    # stream rows render the server-provided display name and badges
    assert "display_id" in text

    status, raw = bench.request("GET", "/style.css")
    assert status == 200
    css = raw.decode()
    assert "ask-opt" in css
    assert "min-height: 48px" in css
    assert "sgroup" in css
