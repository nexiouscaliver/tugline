"""Session tags and name-based reply targeting (W7-L18, store half).

Labels (schema v3) give a session one operator name. Tags give it
many, set by the operator OR by the agent itself, which is what makes
reply targeting by name practical: the operator can aim a reply at
"the session tagged build-night" without ever copying a raw session
id. These tests pin the schema v4 tags table (v5 with W9-L21), the normalization rules
([a-z0-9-], trimmed, lowercase, max 24 chars, max 8 per session), the
in-place migration from a v3 store, and the resolution order the
reply endpoint builds on: exact session id, then unique label, then
unique tag, with ambiguity reported instead of guessed.
"""

import sqlite3

import pytest

from tugline.core.store import (
    MIGRATIONS,
    TAG_MAX_CHARS,
    TAG_MAX_PER_SESSION,
    Store,
    normalize_tag,
)


def open_store(tmp_path):
    return Store(tmp_path / "store" / "tugline.db")


def seed(store, session_id, label=None, tags=()):
    """Insert one session row directly (ingest is covered elsewhere)."""
    if label is not None:
        store.set_session_label(session_id, label)
    for tag in tags:
        store.add_session_tag(session_id, tag)


def make_sessions(store, *ids):
    with store._lock:
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            for sid in ids:
                store.conn.execute(
                    "INSERT INTO sessions (session_id, first_seen,"
                    " last_seen, state, last_event, last_event_ts)"
                    " VALUES (?, '2026-10-14T06:00:00+00:00',"
                    " '2026-10-14T06:00:00+00:00', 'running', 'notify',"
                    " '2026-10-14T06:00:00+00:00')",
                    (sid,),
                )
            store.conn.execute("COMMIT")
        except BaseException:
            store.conn.execute("ROLLBACK")
            raise


# -- normalization --


def test_normalize_tag_lowercases_and_trims():
    assert normalize_tag("  Build-Night ") == "build-night"
    assert normalize_tag("Controller") == "controller"


def test_normalize_tag_strips_characters_outside_the_alphabet():
    # spaces, underscores, punctuation: stripped, not replaced
    assert normalize_tag("mission control") == "missioncontrol"
    assert normalize_tag("w7_lane") == "w7lane"
    assert normalize_tag("tag!") == "tag"


def test_normalize_tag_garbage_is_empty():
    for bad in ("", "   ", "!!!", "***"):
        assert normalize_tag(bad) == ""
    assert normalize_tag(None) == ""


def test_tag_limits_are_exported():
    assert TAG_MAX_CHARS == 24
    assert TAG_MAX_PER_SESSION == 8


# -- schema v4 --


def test_fresh_store_is_schema_version_five_with_tags_table(tmp_path):
    store = open_store(tmp_path)
    version = store.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "5"
    names = {
        row[0]
        for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "tags" in names
    cols = {
        row[1] for row in store.conn.execute("PRAGMA table_info(tags)")
    }
    assert {"session_id", "tag"} <= cols
    pk = [
        row[1]
        for row in store.conn.execute("PRAGMA table_info(tags)")
        if row[5]
    ]
    assert sorted(pk) == ["session_id", "tag"]
    store.close()


def test_version_three_store_migrates_tags_in_place(tmp_path):
    """The migration-on-v3-fixture receipt.

    A store exactly as W7-L16 left it (schema 3, sessions with labels,
    events, threads, replies) opens, gains the tags table in place,
    keeps every existing fact byte-for-byte, and accepts tag writes.
    """
    path = tmp_path / "store" / "tugline.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0][1])
    conn.executescript(MIGRATIONS[1][1])
    conn.executescript(MIGRATIONS[2][1])
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    conn.execute(
        "INSERT INTO sessions (session_id, adapter, project, cwd,"
        " first_seen, last_seen, state, last_event, last_event_ts, label)"
        " VALUES ('controller', 'zcode', 'mission-control', '/w',"
        " '2026-10-14T05:00:00+00:00', '2026-10-14T05:30:00+00:00',"
        " 'running', 'sessionstart', '2026-10-14T05:30:00+00:00',"
        " 'controller')"
    )
    conn.execute(
        "INSERT INTO events (spool_id, ts, session_id, adapter, event,"
        " topic, priority, title, body) VALUES ('sp-1',"
        " '2026-10-14T05:30:00+00:00', 'controller', 'zcode',"
        " 'sessionstart', 'main', 'default', 't', 'b')"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    version = store.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "5"
    # every v3 fact rides over untouched
    row = store.sessions()[0]
    assert row["session_id"] == "controller"
    assert row["label"] == "controller"
    assert row["state"] == "running"
    assert row["adapter"] == "zcode"
    assert len(store.events()) == 1
    # new tags start empty, then work on the migrated table
    assert row["tags"] == []
    updated = store.add_session_tag("controller", "Build-Night")
    assert updated["tags"] == ["build-night"]
    assert updated["label"] == "controller"  # facts did not move
    store.close()


# -- add / remove --


def test_add_session_tag_normalizes_and_returns_row(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1")
    row = store.add_session_tag("sess-1", "  Build-Night ")
    assert row["tags"] == ["build-night"]
    assert row["session_id"] == "sess-1"
    store.close()


def test_add_session_tag_is_idempotent(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1")
    store.add_session_tag("sess-1", "one")
    row = store.add_session_tag("sess-1", "one")
    assert row["tags"] == ["one"]
    store.close()


def test_tags_list_sorted_for_stable_rendering(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1")
    for tag in ("zeta", "alpha", "mid"):
        store.add_session_tag("sess-1", tag)
    assert store.session_tags("sess-1") == ["alpha", "mid", "zeta"]
    store.close()


def test_add_session_tag_unknown_session_raises(tmp_path):
    store = open_store(tmp_path)
    with pytest.raises(KeyError):
        store.add_session_tag("ghost", "one")
    store.close()


def test_add_session_tag_rejects_empty_after_normalization(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1")
    for bad in ("", "   ", "!!!"):
        with pytest.raises(ValueError):
            store.add_session_tag("sess-1", bad)
    store.close()


def test_add_session_tag_rejects_over_length(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1")
    ok = "a" * TAG_MAX_CHARS
    assert store.add_session_tag("sess-1", ok)["tags"] == [ok]
    with pytest.raises(ValueError):
        store.add_session_tag("sess-1", "a" * (TAG_MAX_CHARS + 1))
    store.close()


def test_add_session_tag_caps_at_eight_per_session(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1")
    for i in range(TAG_MAX_PER_SESSION):
        store.add_session_tag("sess-1", "tag-%d" % i)
    # a ninth DISTINCT tag is refused; an existing one stays fine
    with pytest.raises(ValueError):
        store.add_session_tag("sess-1", "one-too-many")
    row = store.add_session_tag("sess-1", "tag-0")
    assert len(row["tags"]) == TAG_MAX_PER_SESSION
    store.close()


def test_remove_session_tag_detaches(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1")
    store.add_session_tag("sess-1", "one")
    store.add_session_tag("sess-1", "two")
    row = store.remove_session_tag("sess-1", "ONE")  # normalized match
    assert row["tags"] == ["two"]
    store.close()


def test_remove_absent_tag_is_a_no_op(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1")
    row = store.remove_session_tag("sess-1", "never-there")
    assert row["tags"] == []
    store.close()


def test_remove_session_tag_unknown_session_raises(tmp_path):
    store = open_store(tmp_path)
    with pytest.raises(KeyError):
        store.remove_session_tag("ghost", "one")
    store.close()


def test_tags_do_not_leak_across_sessions(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1", "sess-2")
    store.add_session_tag("sess-1", "shared")
    assert store.session_tags("sess-2") == []
    store.close()


# -- sessions listing carries tags --


def test_sessions_rows_carry_tags(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1", "sess-2")
    store.add_session_tag("sess-1", "alpha")
    rows = {r["session_id"]: r for r in store.sessions()}
    assert rows["sess-1"]["tags"] == ["alpha"]
    assert rows["sess-2"]["tags"] == []
    # snapshot rides the same rows
    assert store.snapshot()["sessions"][0]["tags"] is not None
    store.close()


def test_set_session_label_row_carries_tags(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1")
    store.add_session_tag("sess-1", "alpha")
    row = store.set_session_label("sess-1", "named")
    assert row["label"] == "named"
    assert row["tags"] == ["alpha"]
    store.close()


# -- lookups for filtering --


def test_sessions_with_tag_matches_normalized(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1", "sess-2", "sess-3")
    store.add_session_tag("sess-1", "build")
    store.add_session_tag("sess-2", "build")
    store.add_session_tag("sess-3", "other")
    assert sorted(store.sessions_with_tag("BUILD")) == ["sess-1", "sess-2"]
    assert store.sessions_with_tag("nope") == []
    store.close()


def test_sessions_with_label_matches_exactly(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1", "sess-2")
    store.set_session_label("sess-1", "controller")
    assert store.sessions_with_label("controller") == ["sess-1"]
    assert store.sessions_with_label("Controller") == []
    assert store.sessions_with_label("nope") == []
    store.close()


# -- reply-target resolution: id, then label, then tag --


def test_resolve_exact_session_id_wins(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "alpha", "sess-1")
    store.set_session_label("sess-1", "alpha")  # collides with the id
    store.add_session_tag("sess-1", "alpha")
    out = store.resolve_session_ref("alpha")
    assert out["match"] == "session_id"
    assert out["session_id"] == "alpha"
    store.close()


def test_resolve_unique_label(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1", "sess-2")
    store.set_session_label("sess-1", "controller")
    out = store.resolve_session_ref("controller")
    assert out["match"] == "label"
    assert out["session_id"] == "sess-1"
    store.close()


def test_resolve_unique_tag(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1", "sess-2")
    store.add_session_tag("sess-1", "build-night")
    out = store.resolve_session_ref("build-night")
    assert out["match"] == "tag"
    assert out["session_id"] == "sess-1"
    store.close()


def test_resolve_label_beats_tag(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1", "sess-2")
    store.set_session_label("sess-1", "night")
    store.add_session_tag("sess-2", "night")
    out = store.resolve_session_ref("night")
    assert out["match"] == "label"
    assert out["session_id"] == "sess-1"
    store.close()


def test_resolve_ambiguous_tag_reports_candidates(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1", "sess-2", "sess-3")
    store.add_session_tag("sess-1", "build")
    store.add_session_tag("sess-2", "build")
    store.add_session_tag("sess-3", "other")
    out = store.resolve_session_ref("build")
    assert out["match"] is None
    assert out["ambiguous"] is True
    assert sorted(out["candidates"]) == ["sess-1", "sess-2"]
    store.close()


def test_resolve_ambiguous_label_reports_candidates(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1", "sess-2")
    store.set_session_label("sess-1", "twin")
    store.set_session_label("sess-2", "twin")
    out = store.resolve_session_ref("twin")
    assert out["match"] is None
    assert out["ambiguous"] is True
    assert sorted(out["candidates"]) == ["sess-1", "sess-2"]
    store.close()


def test_resolve_unknown_ref_is_missing_not_ambiguous(tmp_path):
    store = open_store(tmp_path)
    make_sessions(store, "sess-1")
    out = store.resolve_session_ref("ghost")
    assert out["match"] is None
    assert out["ambiguous"] is False
    assert out["candidates"] == []
    store.close()
