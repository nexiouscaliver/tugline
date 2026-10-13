"""Tests for the sqlite session store (tugline.core.store)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tugline.core import spool
from tugline.core.store import MIGRATIONS, Store, connect

WHEN = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
LATER = WHEN + timedelta(minutes=2)


def write(root, **kw):
    base = dict(
        event="notify",
        session_id="s1",
        topic="main",
        priority="default",
        title="hi",
        body="hello there",
        now=WHEN,
    )
    base.update(kw)
    return spool.append(str(root), **base)


def approve_actions(nonce):
    return [
        {
            "action": "http",
            "label": "Approve",
            "url": "http://127.0.0.1:2586/approvals/%s?decision=allow"
            % nonce,
            "method": "POST",
        }
    ]


def open_store(tmp_path):
    return Store(tmp_path / "store" / "tugline.db")


def thread_for(store, nonce):
    matches = [t for t in store.threads() if t["nonce"] == nonce]
    assert len(matches) == 1
    return matches[0]


# -- schema --


def test_schema_version_two_and_tables_created(tmp_path):
    store = open_store(tmp_path)
    version = store.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "2"
    names = {
        row[0]
        for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"meta", "sessions", "events", "threads", "replies"} <= names
    event_cols = {
        row[1] for row in store.conn.execute("PRAGMA table_info(events)")
    }
    assert {"adapter", "project", "cwd"} <= event_cols
    store.close()


def test_version_one_store_migrates_events_columns(tmp_path):
    path = tmp_path / "store" / "tugline.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0][1])
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    conn.commit()
    conn.close()

    store = Store(path)
    version = store.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "2"
    event_cols = {
        row[1] for row in store.conn.execute("PRAGMA table_info(events)")
    }
    assert {"project", "cwd"} <= event_cols
    store.close()


def test_reopen_is_idempotent(tmp_path):
    spool_root = tmp_path / "spool"
    write(spool_root, now=WHEN)
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    before = store.events()
    store.close()

    again = Store(tmp_path / "store" / "tugline.db")
    assert again.events() == before
    assert again.ingest_spool(str(spool_root)) == {
        "events": 0,
        "sessions": 0,
        "threads": 0,
    }
    again.close()


def test_connect_returns_open_store(tmp_path):
    store = connect(tmp_path / "store" / "tugline.db")
    assert isinstance(store, Store)
    assert store.sessions() == []
    store.close()


# -- ingest --


def test_ingest_creates_event_rows(tmp_path):
    spool_root = tmp_path / "spool"
    r1 = write(spool_root, now=WHEN, title="one")
    write(spool_root, now=LATER, event="stop", title="two")
    store = open_store(tmp_path)
    counts = store.ingest_spool(str(spool_root))
    assert counts == {"events": 2, "sessions": 1, "threads": 0}
    assert [e["title"] for e in store.events()] == ["two", "one"]
    row = store.events()[-1]
    assert row["spool_id"] == r1["id"]
    assert row["ts"] == r1["ts"]
    assert row["session_id"] == "s1"
    assert row["event"] == "notify"
    assert row["topic"] == "main"
    assert row["priority"] == "default"
    assert row["body"] == "hello there"
    assert row["delivered"] == 0
    store.close()


def test_record_without_session_keeps_empty_session_id(tmp_path):
    spool_root = tmp_path / "spool"
    write(spool_root, now=WHEN, session_id="", title="system")
    store = open_store(tmp_path)
    counts = store.ingest_spool(str(spool_root))
    assert counts == {"events": 1, "sessions": 0, "threads": 0}
    (row,) = store.events()
    assert row["session_id"] == ""
    assert store.sessions() == []
    store.close()


def test_reingest_returns_zeros_and_changes_nothing(tmp_path):
    spool_root = tmp_path / "spool"
    write(spool_root, now=WHEN)
    store = open_store(tmp_path)
    assert store.ingest_spool(str(spool_root)) == {
        "events": 1,
        "sessions": 1,
        "threads": 0,
    }
    before = store.events()
    assert store.ingest_spool(str(spool_root)) == {
        "events": 0,
        "sessions": 0,
        "threads": 0,
    }
    assert store.events() == before
    store.close()


# -- session enrichment (adapter, project, cwd) --


def test_ingest_persists_enrichment_on_events_and_sessions(tmp_path):
    spool_root = tmp_path / "spool"
    write(
        spool_root,
        now=WHEN,
        extra={"adapter": "zcode", "cwd": "/w/alpha", "project": "alpha"},
    )
    store = open_store(tmp_path)
    counts = store.ingest_spool(str(spool_root))
    assert counts == {"events": 1, "sessions": 1, "threads": 0}
    (event,) = store.events()
    assert event["adapter"] == "zcode"
    assert event["project"] == "alpha"
    assert event["cwd"] == "/w/alpha"
    (session,) = store.sessions()
    assert session["adapter"] == "zcode"
    assert session["project"] == "alpha"
    assert session["cwd"] == "/w/alpha"
    store.close()


def test_session_enrichment_is_last_writer_wins(tmp_path):
    spool_root = tmp_path / "spool"
    write(
        spool_root,
        now=WHEN,
        title="first",
        extra={"adapter": "zcode", "cwd": "/w/alpha", "project": "alpha"},
    )
    write(
        spool_root,
        now=LATER,
        event="stop",
        title="second",
        extra={"adapter": "claude", "cwd": "/w/beta", "project": "beta"},
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    (session,) = store.sessions()
    assert session["adapter"] == "claude"
    assert session["project"] == "beta"
    assert session["cwd"] == "/w/beta"
    by_title = {e["title"]: e for e in store.events()}
    assert by_title["first"]["project"] == "alpha"
    assert by_title["second"]["project"] == "beta"
    store.close()


def test_records_without_enrichment_keep_empty_strings(tmp_path):
    spool_root = tmp_path / "spool"
    write(spool_root, now=WHEN)
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    (event,) = store.events()
    assert event["adapter"] == ""
    assert event["project"] == ""
    assert event["cwd"] == ""
    (session,) = store.sessions()
    assert session["adapter"] == ""
    assert session["project"] == ""
    assert session["cwd"] == ""
    store.close()


def test_unenriched_record_leaves_session_enrichment_alone(tmp_path):
    """A later plain record (approve path) must not wipe what a hook
    record set; only records that carry the keys upsert them."""
    spool_root = tmp_path / "spool"
    write(
        spool_root,
        now=WHEN,
        title="enriched",
        extra={"adapter": "zcode", "cwd": "/w/alpha", "project": "alpha"},
    )
    write(spool_root, now=LATER, event="stop", title="plain")
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    (session,) = store.sessions()
    assert session["adapter"] == "zcode"
    assert session["project"] == "alpha"
    assert session["cwd"] == "/w/alpha"
    by_title = {e["title"]: e for e in store.events()}
    assert by_title["plain"]["adapter"] == ""
    assert by_title["plain"]["project"] == ""
    store.close()


# -- sessions --


def test_permissionrequest_leaves_session_waiting(tmp_path):
    spool_root = tmp_path / "spool"
    rec = write(
        spool_root, now=WHEN, event="permissionrequest", title="ask"
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    (session,) = store.sessions()
    assert session["state"] == "waiting"
    assert session["first_seen"] == rec["ts"]
    assert session["last_seen"] == rec["ts"]
    assert session["last_event"] == "permissionrequest"
    assert session["last_event_ts"] == rec["ts"]
    store.close()


def test_stop_after_permissionrequest_ends_done(tmp_path):
    spool_root = tmp_path / "spool"
    r1 = write(spool_root, now=WHEN, event="permissionrequest")
    r2 = write(spool_root, now=LATER, event="stop")
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    (session,) = store.sessions()
    assert session["state"] == "done"
    assert session["first_seen"] == r1["ts"]
    assert session["last_seen"] == r2["ts"]
    assert session["last_event"] == "stop"
    assert session["last_event_ts"] == r2["ts"]
    store.close()


def test_plain_events_keep_session_running(tmp_path):
    spool_root = tmp_path / "spool"
    write(spool_root, now=WHEN)
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    (session,) = store.sessions()
    assert session["state"] == "running"
    store.close()


def test_sessions_ordered_by_last_seen_desc(tmp_path):
    spool_root = tmp_path / "spool"
    write(spool_root, now=WHEN, session_id="s1")
    write(spool_root, now=LATER, session_id="s2")
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    assert [s["session_id"] for s in store.sessions()] == ["s2", "s1"]
    store.close()


def test_events_filter_by_session_and_limit(tmp_path):
    spool_root = tmp_path / "spool"
    write(spool_root, now=WHEN, session_id="s1", title="a")
    write(
        spool_root, now=WHEN + timedelta(minutes=1), session_id="s2",
        title="b",
    )
    write(
        spool_root, now=WHEN + timedelta(minutes=2), session_id="s1",
        title="c",
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    assert [e["title"] for e in store.events(session_id="s2")] == ["b"]
    assert [e["title"] for e in store.events(session_id="s1")] == ["c", "a"]
    assert [e["title"] for e in store.events(limit=2)] == ["c", "b"]
    store.close()


# -- threads --


def test_permissionrequest_with_actions_creates_thread(tmp_path):
    spool_root = tmp_path / "spool"
    nonce = "5f" * 16
    rec = write(
        spool_root,
        now=WHEN,
        event="permissionrequest",
        session_id="s1",
        title="allow bash?",
        body="rm -rf",
        actions=approve_actions(nonce),
    )
    store = open_store(tmp_path)
    counts = store.ingest_spool(str(spool_root))
    assert counts == {"events": 1, "sessions": 1, "threads": 1}
    thread = thread_for(store, nonce)
    assert thread["kind"] == "approve"
    assert thread["session_id"] == "s1"
    assert thread["title"] == "allow bash?"
    assert thread["body"] == "rm -rf"
    assert thread["created_ts"] == rec["ts"]
    assert thread["status"] == "open"
    store.ingest_spool(str(spool_root))
    assert len(store.threads()) == 1
    store.close()


def test_permissionrequest_without_actions_makes_no_thread(tmp_path):
    spool_root = tmp_path / "spool"
    write(spool_root, now=WHEN, event="permissionrequest")
    store = open_store(tmp_path)
    assert store.ingest_spool(str(spool_root))["threads"] == 0
    assert store.threads() == []
    store.close()


# -- delivery --


def test_refresh_delivery_flips_stored_rows(tmp_path):
    spool_root = tmp_path / "spool"
    r1 = write(spool_root, now=WHEN, title="one")
    write(spool_root, now=LATER, title="two")
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    spool.mark_delivered(str(spool_root), r1["id"])
    assert store.refresh_delivery(str(spool_root)) == 1
    by_title = {e["title"]: e for e in store.events()}
    assert by_title["one"]["delivered"] == 1
    assert by_title["one"]["delivered_ts"]
    assert by_title["two"]["delivered"] == 0
    assert by_title["two"]["delivered_ts"] is None
    assert store.refresh_delivery(str(spool_root)) == 0
    store.close()


# -- thread refresh --


def test_refresh_threads_open_answered_expired(tmp_path):
    spool_root = tmp_path / "spool"
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    n_open, n_done, n_gone = "a" * 32, "b" * 32, "c" * 32
    (approvals / (n_open + ".pending")).write_text("{}\n")
    decision_ts = LATER.isoformat()
    (approvals / (n_done + ".json")).write_text(
        json.dumps({"decision": "allow", "ts": decision_ts}) + "\n"
    )
    for nonce in (n_open, n_done, n_gone):
        write(
            spool_root,
            now=WHEN,
            event="permissionrequest",
            session_id="s1",
            actions=approve_actions(nonce),
        )
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    assert all(t["status"] == "open" for t in store.threads())

    counts = store.refresh_threads(str(approvals))
    # one thread answered, one held open by its pending marker, one
    # expired with neither file; only changed rows count
    assert counts == {"answered": 1, "open": 0, "expired": 1}
    by_nonce = {t["nonce"]: t for t in store.threads()}
    assert by_nonce[n_open]["status"] == "open"
    assert by_nonce[n_done]["status"] == "answered"
    assert by_nonce[n_done]["answer"] == "allow"
    assert by_nonce[n_done]["answer_ts"] == decision_ts
    assert by_nonce[n_gone]["status"] == "expired"
    assert len(store.threads(status="answered")) == 1

    assert store.refresh_threads(str(approvals)) == {
        "answered": 0,
        "open": 0,
        "expired": 0,
    }
    store.close()


# -- replies --


def test_add_reply_to_thread_inherits_session(tmp_path):
    spool_root = tmp_path / "spool"
    nonce = "e" * 32
    write(
        spool_root,
        now=WHEN,
        event="permissionrequest",
        session_id="s9",
        actions=approve_actions(nonce),
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    reply = store.add_reply("approved here", thread_nonce=nonce)
    assert reply["session_id"] == "s9"
    assert reply["thread_nonce"] == nonce
    assert reply["source"] == "console"
    assert reply["body"] == "approved here"
    assert reply["delivered"] == 0
    assert reply["ts"]
    assert store.replies() == [reply]
    store.close()


def test_add_reply_session_only(tmp_path):
    spool_root = tmp_path / "spool"
    write(spool_root, now=WHEN, session_id="s1")
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    reply = store.add_reply("noted", session_id="s1")
    assert reply["session_id"] == "s1"
    assert reply["thread_nonce"] is None
    store.close()


def test_add_reply_requires_exactly_one_target(tmp_path):
    store = open_store(tmp_path)
    with pytest.raises(ValueError):
        store.add_reply("both", thread_nonce="x", session_id="s1")
    with pytest.raises(ValueError):
        store.add_reply("neither")
    store.close()


def test_add_reply_unknown_nonce_raises(tmp_path):
    store = open_store(tmp_path)
    with pytest.raises(KeyError):
        store.add_reply("hi", thread_nonce="f" * 32)
    store.close()


def test_record_answer_marks_thread_and_raises_on_unknown(tmp_path):
    spool_root = tmp_path / "spool"
    nonce = "ab" * 16
    write(
        spool_root,
        now=WHEN,
        event="permissionrequest",
        session_id="s1",
        actions=approve_actions(nonce),
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    store.record_answer(nonce, "allow", answer_ts=LATER.isoformat())
    thread = thread_for(store, nonce)
    assert thread["status"] == "answered"
    assert thread["answer"] == "allow"
    assert thread["answer_ts"] == LATER.isoformat()
    with pytest.raises(KeyError):
        store.record_answer("cd" * 16, "allow")
    store.close()


# -- snapshot --


def test_snapshot_shape(tmp_path):
    spool_root = tmp_path / "spool"
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    nonce = "d" * 32
    (approvals / (nonce + ".pending")).write_text("{}\n")
    r1 = write(
        spool_root,
        now=WHEN,
        event="permissionrequest",
        session_id="s1",
        actions=approve_actions(nonce),
    )
    write(spool_root, now=LATER, event="stop", title="bye")
    store = open_store(tmp_path)
    store.ingest_spool(str(spool_root))
    spool.mark_delivered(str(spool_root), r1["id"])
    store.refresh_delivery(str(spool_root))
    store.add_reply("go ahead", thread_nonce=nonce)

    snap = store.snapshot()
    assert set(snap) == {"sessions", "events", "threads", "replies",
                         "counts"}
    assert snap["counts"] == {
        "events_total": 2,
        "events_pending": 1,
        "threads_open": 1,
    }
    assert [s["session_id"] for s in snap["sessions"]] == ["s1"]
    assert [e["title"] for e in snap["events"]] == ["bye", "hi"]
    (thread,) = snap["threads"]
    assert thread["nonce"] == nonce
    assert thread["status"] == "open"
    assert len(snap["replies"]) == 1
    assert snap["replies"][0]["body"] == "go ahead"
    store.close()
