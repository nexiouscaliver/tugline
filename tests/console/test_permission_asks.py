"""Permission threads and the asks section truth (W10-L24).

Harness PermissionRequest events must reach the console asks section
even when the approve toggle is off (it is off on live per PD4): the
store creates one kind=permission thread per session, refreshed by
each new permissionrequest and retired to "gone" by any later event
for the session, so read-side visibility never depends on the
decision-button flow. The threads read carries identity fields (the
one source of truth the section renders from) and honest decidability:
a permission thread is not answerable from the console, and the note
says so instead of drawing dead buttons.
"""

import json

import pytest

from console.test_server import (
    Bench,
    seed_permission_request,
    wait_for,
    wait_for_thread,
)
from tugline.core import paths, spool
from tugline.core.store import Store


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


def seed_plain_permission(home, session_id="sess-perm", tool="CreateWorkflow",
                          prompt="run the release checklist"):
    """A real no-toggle session shape: a prompt record for identity,
    then a PermissionRequest record with no action urls (approve off).

    This is the exact byte shape hook-zcode spools with [toggles]
    approve=false: no actions key at all.
    """
    spool.append(
        paths.spool_root(),
        event="userpromptsubmit",
        session_id=session_id,
        topic="main",
        priority="default",
        title="[zcode] demo - prompt submitted",
        body="session %s, prompt: %s" % (session_id[-6:], prompt),
        extra={"adapter": "zcode", "cwd": "/w/demo", "project": "demo"},
    )
    return spool.append(
        paths.spool_root(),
        event="permissionrequest",
        session_id=session_id,
        topic="warn",
        priority="high",
        title="[zcode] demo - waiting for your input",
        body="session %s, permission: %s" % (session_id[-6:], tool),
        extra={"adapter": "zcode", "cwd": "/w/demo", "project": "demo"},
    )


def permission_thread(store):
    rows = [t for t in store.threads() if t["kind"] == "permission"]
    assert len(rows) == 1, "expected exactly one permission thread"
    return rows[0]


# -- the store: the thread exists regardless of the approve toggle --


def test_plain_permissionrequest_creates_permission_thread(home):
    seed_plain_permission(home)
    store = Store(paths.store_path())
    counts = store.ingest_spool(paths.spool_root())
    assert counts["threads"] == 1
    thread = permission_thread(store)
    assert thread["session_id"] == "sess-perm"
    assert thread["status"] == "open"
    assert thread["title"] == "[zcode] demo - waiting for your input"
    assert "CreateWorkflow" in thread["body"]
    assert thread["options"] is None  # no ask options, honest reading
    # idempotent: a second fold over the same spool changes nothing
    assert store.ingest_spool(paths.spool_root())["threads"] == 0
    assert len(store.threads()) == 1
    store.close()


def test_later_event_retires_the_permission_thread_to_gone(home):
    seed_plain_permission(home)
    spool.append(
        paths.spool_root(),
        event="stop",
        session_id="sess-perm",
        topic="main",
        priority="default",
        title="[zcode] demo - turn finished",
        body="session on-perm, done",
        extra={"adapter": "zcode", "cwd": "/w/demo", "project": "demo"},
    )
    store = Store(paths.store_path())
    counts = store.ingest_spool(paths.spool_root())
    assert counts["threads"] == 1
    assert counts["gone"] == 1
    assert permission_thread(store)["status"] == "gone"
    store.close()


def test_refired_permission_refreshes_the_open_thread_in_place(home):
    rec1 = seed_plain_permission(home)
    spool.append(
        paths.spool_root(),
        event="permissionrequest",
        session_id="sess-perm",
        topic="warn",
        priority="high",
        title="[zcode] demo - waiting for your input",
        body="session on-perm, permission: Bash",
        extra={"adapter": "zcode", "cwd": "/w/demo", "project": "demo"},
    )
    store = Store(paths.store_path())
    store.ingest_spool(paths.spool_root())
    thread = permission_thread(store)
    assert thread["status"] == "open"
    assert "Bash" in thread["body"]
    assert thread["created_ts"] > rec1["ts"]  # the newest question wins
    assert len([t for t in store.threads() if t["kind"] == "permission"]) == 1
    store.close()


def test_new_permission_after_gone_reopens_the_thread(home):
    seed_plain_permission(home)
    spool.append(
        paths.spool_root(),
        event="stop",
        session_id="sess-perm",
        topic="main",
        priority="default",
        title="t",
        body="b",
    )
    spool.append(
        paths.spool_root(),
        event="permissionrequest",
        session_id="sess-perm",
        topic="warn",
        priority="high",
        title="[zcode] demo - waiting for your input",
        body="session on-perm, permission: WebFetch",
        extra={"adapter": "zcode", "cwd": "/w/demo", "project": "demo"},
    )
    store = Store(paths.store_path())
    store.ingest_spool(paths.spool_root())
    thread = permission_thread(store)
    assert thread["status"] == "open"
    assert "WebFetch" in thread["body"]
    store.close()


def test_refresh_threads_never_touches_permission_threads(home):
    seed_plain_permission(home)
    store = Store(paths.store_path())
    store.ingest_spool(paths.spool_root())
    # no pending marker, no decision file: the approve reconcile pass
    # would call this expired; permission threads derive from events
    counts = store.refresh_threads(paths.approvals_root())
    assert counts == {"answered": 0, "open": 0, "expired": 0}
    assert permission_thread(store)["status"] == "open"
    store.close()


def test_nonce_permissionrequest_still_makes_an_approve_thread(home):
    # the approve-gated flow keeps its own thread kind untouched
    seed_permission_request("nonce-9", session_id="sess-approve")
    store = Store(paths.store_path())
    store.ingest_spool(paths.spool_root())
    rows = store.threads()
    assert [t["kind"] for t in rows] == ["approve"]
    assert rows[0]["nonce"] == "nonce-9"
    store.close()


# -- the v7 backfill: stores that folded the spool before the fix --


def seed_pre_fix_store(home):
    """Rewind a freshly folded store to its pre-W10-L24 shape.

    The live consoles on redeploy hold exactly this state: every
    event row ingested, schema v6, and no permission thread (the old
    ingest created none). The rewind reproduces it byte for byte: the
    permission thread the new ingest created is deleted and the
    version pin drops back to 6, so the next open runs the v7
    backfill migration the way a redeploy would.
    """
    store = Store(paths.store_path())
    store.ingest_spool(paths.spool_root())
    store.conn.execute("BEGIN IMMEDIATE")
    store.conn.execute("DELETE FROM threads WHERE kind = 'permission'")
    store.conn.execute(
        "UPDATE meta SET value = '6' WHERE key = 'schema_version'"
    )
    store.conn.execute("COMMIT")
    assert store.threads() == []
    store.close()


def test_migration_backfills_a_waiting_permission_thread(home):
    seed_plain_permission(home)
    seed_pre_fix_store(home)
    store = Store(paths.store_path())  # reopening applies the backfill
    thread = permission_thread(store)
    assert thread["status"] == "open"  # still waiting: newest event
    assert "CreateWorkflow" in thread["body"]
    assert thread["session_id"] == "sess-perm"
    # re-ingest stays idempotent: same one row, still open
    store.ingest_spool(paths.spool_root())
    assert len(store.threads()) == 1
    assert permission_thread(store)["status"] == "open"
    store.close()


def test_migration_backfills_gone_for_moved_on_sessions(home):
    seed_plain_permission(home)
    spool.append(
        paths.spool_root(),
        event="stop",
        session_id="sess-perm",
        topic="main",
        priority="default",
        title="[zcode] demo - turn finished",
        body="session on-perm, done",
    )
    seed_pre_fix_store(home)

    store = Store(paths.store_path())
    thread = permission_thread(store)
    assert thread["status"] == "gone"  # the harness moved on
    store.close()


def test_migration_skips_sessions_the_approve_flow_threaded(home):
    seed_permission_request("nonce-old", session_id="sess-approve")
    store = Store(paths.store_path())
    store.ingest_spool(paths.spool_root())
    assert [t["kind"] for t in store.threads()] == ["approve"]
    store.close()

    store = Store(paths.store_path())  # reopen: backfill must not add
    assert [t["kind"] for t in store.threads()] == ["approve"]
    store.close()


# -- the read side: identity fields and honest decidability --


def test_threads_read_carries_identity_and_decidability(bench, home):
    seed_plain_permission(home)
    assert wait_for(lambda: bench.store.threads())

    status, data = bench.get_json("/api/threads")

    assert status == 200
    assert data["approve_on"] is False  # the default config posture
    row = [t for t in data["threads"] if t["kind"] == "permission"][0]
    # identity fields ride the row: the section renders from this read
    # alone, never from the sessions panel's in-memory map
    assert row["session_id"] == "sess-perm"
    assert row["display_id"]
    assert row["label"] == "run the release checklist"  # the auto label
    assert row["tags"] == ["demo", "zcode"]  # alphabetical, the store contract
    assert row["adapter"] == "zcode"
    assert row["project"] == "demo"
    # honest decidability: no decision-file target exists, so the row
    # says so instead of drawing dead buttons
    assert row["decidable"] is False
    assert "approve is off" in row["decidable_note"]
    assert "harness" in row["decidable_note"]


def test_state_snapshot_threads_carry_the_same_decoration(bench, home):
    seed_plain_permission(home)
    wait_for(lambda: bench.store.threads())
    status, state = bench.get_json("/api/state")
    assert status == 200
    row = [t for t in state["threads"] if t["kind"] == "permission"][0]
    assert row["display_id"]
    assert row["label"]
    assert row["tags"] == ["demo", "zcode"]  # alphabetical, the store contract
    assert row["decidable"] is False


def test_approve_thread_rows_stay_decidable(bench, home):
    seed_permission_request("nonce-1", session_id="sess-approve")
    wait_for_thread(bench, "nonce-1")
    status, data = bench.get_json("/api/threads")
    row = [t for t in data["threads"] if t["nonce"] == "nonce-1"][0]
    assert row["kind"] == "approve"
    assert row["decidable"] is True
    assert row["display_id"]


def test_permission_nonce_cannot_be_decided_over_http(bench, home):
    # no fake decidability: the approvals endpoint answers from the
    # decision files, and a permission thread has none
    seed_plain_permission(home)
    wait_for(lambda: bench.store.threads())
    nonce = [t for t in bench.store.threads()][0]["nonce"]
    status, data = bench.post_json(
        "/api/approvals/%s?decision=allow" % nonce
    )
    assert status == 404
    assert data["error"]


def test_permission_thread_supports_bound_replies(bench, home):
    # the one honest console action on a permission ask: a free-form
    # reply to the waiting session through the thread binding
    seed_plain_permission(home)
    wait_for(lambda: bench.store.threads())
    nonce = [t for t in bench.store.threads()][0]["nonce"]
    status, data = bench.post_json(
        "/api/replies", {"thread_nonce": nonce, "body": "allow it"}
    )
    assert status == 200
    row = bench.store.replies()[0]
    assert row["session_id"] == "sess-perm"
    assert row["thread_nonce"] == nonce


# -- the web assets render the permission cards --


def test_web_assets_render_permission_cards(bench):
    status, raw = bench.request("GET", "/app.js")
    assert status == 200
    text = raw.decode()
    # permission cards render under the approvals, from the decorated
    # thread row itself (one source of truth), with the honest note
    assert "kind === \"permission\"" in text
    assert "decidable" in text
    assert "permcard" in text
    assert "answer in the harness" in text
    # SSE (re)open repaints the section instead of leaving it stale
    assert "onopen" in text
    # asks fetch failures show an inline error, never a blank panel
    assert "asks-error" in text

    status, raw = bench.request("GET", "/style.css")
    assert status == 200
    css = raw.decode()
    assert "permcard" in css
    assert "min-height: 48px" in css


def test_ask_cards_still_render_after_the_section_rework(bench, home):
    from console.test_asks import OPTIONS as ASK_OPTIONS, open_ask

    open_ask(bench)
    status, data = bench.get_json("/api/threads")
    row = [t for t in data["threads"] if t["nonce"] == "ask0001"][0]
    assert row["kind"] == "ask"
    assert row["options"] == ASK_OPTIONS
    assert row["decidable"] is True
