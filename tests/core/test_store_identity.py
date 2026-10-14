"""Session auto-identity (W9-L21): auto-label, auto-tags, one
display rule, and ask threads in the store.

The operator's complaints this pins: a session is unrecognizable in
its first moments (no name, no tags), the id shows up under two
conventions in the wild (leading "sess_..." characters here, unique
suffix there), and events say WHAT happened but never WHO did it.

Pinned behaviors:

- AUTO-LABEL: the first suitable UserPromptSubmit record names the
  session (first 5-7 words of the prompt, sanitized); set once,
  never overwritten by later prompts; an operator label always wins
  and pauses auto-labeling, including clearing it.
- AUTO-TAGS: on first sight (session row creation) the store tags
  the session with its adapter and its project label from cwd;
  operator tag changes are never fought afterwards.
- DISPLAY: one policy everywhere - the label when set, else the
  W7-L16 collision-proof unique suffix, never the leading id
  characters - exported as one helper and attached to every session
  row and every event row (with label and tags) so any stream row
  answers who did what where.
- ASK THREADS: kind=ask rows carry their options as parsed JSON,
  answer with wrong-key rejection, expire, and the newest open ask
  per session is queryable (the binding rule for session-bound
  replies).
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from tugline.core import spool
from tugline.core.store import (
    MIGRATIONS,
    Store,
    auto_label_from_prompt,
    prompt_text_from_body,
    session_display_id,
)

WHEN = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
LATER = WHEN + timedelta(minutes=2)


def write(root, **kw):
    base = dict(
        event="notify",
        session_id="sess-1",
        topic="main",
        priority="default",
        title="hi",
        body="hello there",
        now=WHEN,
    )
    base.update(kw)
    return spool.append(str(root), **base)


def prompt_record(root, session_id, prompt, ts=WHEN, **extra):
    body = "session %s, prompt: %s" % (str(session_id)[-6:], prompt)
    return write(
        root,
        event="userpromptsubmit",
        session_id=session_id,
        body=body,
        now=ts,
        **extra,
    )


def open_store(tmp_path):
    return Store(tmp_path / "store" / "tugline.db")


def session_row(store, session_id):
    matches = [s for s in store.sessions() if s["session_id"] == session_id]
    assert len(matches) == 1
    return matches[0]


# -- auto-label: the sanitizer --


def test_auto_label_takes_five_to_seven_words():
    # the cap is seven words: an eighth word drops off
    assert auto_label_from_prompt(
        "fix the login bug on the settings page now please"
    ) == "fix the login bug on the settings"


def test_auto_label_short_prompt_keeps_all_words():
    assert auto_label_from_prompt("roll the release") == "roll the release"


def test_auto_label_collapses_whitespace_and_caps_length():
    # 7 long words overflow 40 chars: words drop off the end until it
    # fits, and the whitespace collapses before that
    label = auto_label_from_prompt(
        "wordword   wordword wordword wordword wordword wordword wordword"
    )
    assert label == "wordword wordword wordword wordword"


def test_auto_label_hard_slice_when_one_word_overflows():
    label = auto_label_from_prompt("x" * 60)
    assert label == "x" * 40


def test_auto_label_strips_trailing_punctuation_artifacts():
    assert auto_label_from_prompt("fix the build now...") == "fix the build now"
    assert auto_label_from_prompt("which deploy,") == "which deploy"


def test_auto_label_drops_non_ascii():
    assert auto_label_from_prompt("caf\xe9 racer build") == "caf racer build"


def test_auto_label_empty_prompt_is_empty():
    assert auto_label_from_prompt("   ") == ""


def test_prompt_text_from_body_reads_the_enriched_marker():
    assert (
        prompt_text_from_body("session c0abcd, prompt: fix the login")
        == "fix the login"
    )


def test_prompt_text_from_body_plain_body_is_the_prompt():
    assert prompt_text_from_body("roll the release forward") == (
        "roll the release forward"
    )


def test_prompt_text_from_body_session_fallback_is_not_a_prompt():
    assert prompt_text_from_body("session c0abcd") == ""


# -- auto-label: the ingest rule --


def test_first_prompt_labels_the_session(tmp_path):
    prompt_record(tmp_path, "sess-1", "fix the login bug in settings")
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    row = session_row(store, "sess-1")
    assert row["label"] == "fix the login bug in settings"
    assert row["label_source"] == "auto"
    store.close()


def test_auto_label_set_once_later_prompts_do_not_overwrite(tmp_path):
    prompt_record(tmp_path, "sess-1", "first prompt wins the name")
    prompt_record(
        tmp_path,
        "sess-1",
        "second prompt must not replace the label",
        ts=LATER,
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    row = session_row(store, "sess-1")
    assert row["label"] == "first prompt wins the name"
    store.close()


def test_operator_label_wins_over_auto(tmp_path):
    write(tmp_path, session_id="sess-1")
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.set_session_label("sess-1", "controller")
    store.close()

    prompt_record(tmp_path, "sess-1", "operator named me already", ts=LATER)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    row = session_row(store, "sess-1")
    assert row["label"] == "controller"
    assert row["label_source"] == "operator"
    store.close()


def test_operator_can_replace_an_auto_label(tmp_path):
    prompt_record(tmp_path, "sess-1", "auto name from the prompt")
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    assert session_row(store, "sess-1")["label_source"] == "auto"
    store.set_session_label("sess-1", "renamed by operator")
    store.close()

    prompt_record(tmp_path, "sess-1", "another prompt arrives", ts=LATER)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    row = session_row(store, "sess-1")
    assert row["label"] == "renamed by operator"
    assert row["label_source"] == "operator"
    store.close()


def test_operator_clear_pauses_auto_labeling(tmp_path):
    prompt_record(tmp_path, "sess-1", "auto name from the prompt")
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.set_session_label("sess-1", "")
    assert session_row(store, "sess-1")["label"] is None
    store.close()

    prompt_record(tmp_path, "sess-1", "prompt after the clear", ts=LATER)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    row = session_row(store, "sess-1")
    assert row["label"] is None
    assert row["label_source"] == "operator"
    store.close()


def test_unsuitable_prompt_does_not_block_a_later_one(tmp_path):
    # a prompt record whose body carries no prompt text (the fallback
    # body) is not suitable; the next real prompt still labels
    write(
        tmp_path,
        event="userpromptsubmit",
        session_id="sess-1",
        body="session c0abcd",
    )
    prompt_record(tmp_path, "sess-1", "the next prompt is suitable", ts=LATER)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    row = session_row(store, "sess-1")
    assert row["label"] == "the next prompt is suitable"
    store.close()


def test_non_prompt_events_never_auto_label(tmp_path):
    write(
        tmp_path,
        event="stop",
        session_id="sess-1",
        body="session c0abcd, prompt: not a userpromptsubmit record",
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    row = session_row(store, "sess-1")
    assert row["label"] is None
    assert row["label_source"] is None
    store.close()


def test_manual_notify_records_do_not_label(tmp_path):
    write(tmp_path, session_id="sess-1", body="manual notification")
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    assert session_row(store, "sess-1")["label"] is None
    store.close()


# -- auto-tags: first sight --


def test_first_sight_tags_adapter_and_project(tmp_path):
    write(
        tmp_path,
        session_id="sess-1",
        extra={"adapter": "zcode", "cwd": "/Users/op/work/mission-control"},
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    assert session_row(store, "sess-1")["tags"] == [
        "mission-control",
        "zcode",
    ]
    assert store.sessions_with_tag("zcode") == ["sess-1"]
    assert store.sessions_with_tag("mission-control") == ["sess-1"]
    store.close()


def test_project_tag_follows_tag_normalization(tmp_path):
    write(
        tmp_path,
        session_id="sess-1",
        extra={"adapter": "claude", "cwd": "/Users/op/work/My Project"},
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    assert session_row(store, "sess-1")["tags"] == ["claude", "myproject"]
    store.close()


def test_auto_tags_never_fight_the_operator(tmp_path):
    write(
        tmp_path,
        session_id="sess-1",
        extra={"adapter": "zcode", "cwd": "/Users/op/work/mission-control"},
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.remove_session_tag("sess-1", "zcode")
    store.add_session_tag("sess-1", "operator-tag")
    store.close()

    write(
        tmp_path,
        session_id="sess-1",
        event="stop",
        extra={"adapter": "zcode", "cwd": "/Users/op/work/mission-control"},
        now=LATER,
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    assert session_row(store, "sess-1")["tags"] == [
        "mission-control",
        "operator-tag",
    ]
    store.close()


def test_bare_first_record_gets_no_auto_tags(tmp_path):
    write(tmp_path, session_id="sess-1")  # a manual notify, no enrichment
    write(
        tmp_path,
        session_id="sess-1",
        event="stop",
        extra={"adapter": "zcode", "cwd": "/Users/op/work/mission-control"},
        now=LATER,
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    # first sight carried no adapter/cwd: the operator's tags are the
    # only tags, and later records never force the auto pair back in
    assert session_row(store, "sess-1")["tags"] == []
    store.close()


def test_auto_tags_idempotent_across_reingest(tmp_path):
    write(
        tmp_path,
        session_id="sess-1",
        extra={"adapter": "zcode", "cwd": "/Users/op/work/mission-control"},
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.ingest_spool(str(tmp_path))
    assert session_row(store, "sess-1")["tags"] == [
        "mission-control",
        "zcode",
    ]
    store.close()


# -- the one display rule --


def test_display_id_label_wins():
    assert (
        session_display_id("sess_abcdef123456", "controller", ["other"])
        == "controller"
    )


def test_display_id_falls_back_to_unique_suffix():
    ids = ["sess_517ee9c0aaaa", "sess_517ee9c1bbbb"]
    shown = session_display_id(ids[0], None, ids)
    assert shown == "c0aaaa"
    assert ids[0].endswith(shown)


def test_display_suffix_lengthens_on_collision():
    ids = ["ccaaaaX1", "ddaaaaX1"]
    first = session_display_id(ids[0], None, ids)
    second = session_display_id(ids[1], None, ids)
    assert first != second
    assert ids[0].endswith(first)
    assert ids[1].endswith(second)


def test_display_id_never_renders_leading_id_characters():
    sid = "sess_517ee9c0abcd"
    shown = session_display_id(sid, None, [sid])
    assert sid.endswith(shown)
    assert not shown.startswith("sess")
    assert len(shown) <= len(sid)


def test_session_rows_carry_display_id(tmp_path):
    write(tmp_path, session_id="sess_517ee9c0aaaa")
    write(tmp_path, session_id="sess_517ee9c1bbbb")
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.set_session_label("sess_517ee9c0aaaa", "controller")
    rows = {r["session_id"]: r for r in store.sessions()}
    assert rows["sess_517ee9c0aaaa"]["display_id"] == "controller"
    assert rows["sess_517ee9c1bbbb"]["display_id"] == "c1bbbb"
    store.close()


def test_event_rows_carry_who_did_what_where(tmp_path):
    prompt_record(
        tmp_path,
        "sess-1",
        "fix the login bug",
        extra={"adapter": "zcode", "cwd": "/Users/op/work/mission-control"},
    )
    write(tmp_path, session_id="sess-2")
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.add_session_tag("sess-1", "build")
    events = {e["session_id"]: e for e in store.events()}
    assert events["sess-1"]["label"] == "fix the login bug"
    assert events["sess-1"]["tags"] == ["build", "mission-control", "zcode"]
    assert events["sess-1"]["display_id"] == "fix the login bug"
    assert events["sess-2"]["label"] is None
    assert events["sess-2"]["tags"] == []
    assert events["sess-2"]["display_id"] == "sess-2"[-6:]
    store.close()


def test_display_id_survives_a_label_unset(tmp_path):
    write(tmp_path, session_id="sess_517ee9c0aaaa")
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.set_session_label("sess_517ee9c0aaaa", "temp")
    assert session_row(store, "sess_517ee9c0aaaa")["display_id"] == "temp"
    store.set_session_label("sess_517ee9c0aaaa", "")
    assert session_row(store, "sess_517ee9c0aaaa")["display_id"] == "c0aaaa"
    store.close()


# -- ask threads in the store --


OPTIONS = [
    {"key": "A", "text": "ship the release"},
    {"key": "B", "text": "roll back"},
]


def seed_session(tmp_path, session_id="sess-1"):
    write(
        tmp_path,
        session_id=session_id,
        extra={"adapter": "zcode", "cwd": "/Users/op/work/mission-control"},
    )


def ask_thread(store, nonce):
    matches = [t for t in store.threads() if t["nonce"] == nonce]
    assert len(matches) == 1
    return matches[0]


def test_create_ask_thread_carries_options_json(tmp_path):
    seed_session(tmp_path)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    row = store.create_ask_thread(
        "sess-1", "which deploy?", OPTIONS, nonce="ask-nonce-1"
    )
    assert row["kind"] == "ask"
    assert row["status"] == "open"
    assert row["options"] == OPTIONS
    assert row["title"] == "which deploy?"
    assert row["session_id"] == "sess-1"
    stored = ask_thread(store, "ask-nonce-1")
    assert stored["options"] == OPTIONS
    # the raw column is JSON text, the read parses it back
    raw = store.conn.execute(
        "SELECT options FROM threads WHERE nonce = 'ask-nonce-1'"
    ).fetchone()[0]
    assert json.loads(raw) == OPTIONS
    store.close()


def test_create_ask_thread_unknown_session(tmp_path):
    seed_session(tmp_path)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    try:
        store.create_ask_thread("ghost", "q", OPTIONS, nonce="n")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    store.close()


def test_create_ask_thread_validates_options(tmp_path):
    seed_session(tmp_path)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    for bad in ([], [{}], [{"key": "A"}], [{"text": "x"}], [{"key": "", "text": "x"}]):
        try:
            store.create_ask_thread("sess-1", "q", bad, nonce="n")
            raise AssertionError("expected ValueError for %r" % (bad,))
        except ValueError:
            pass
    store.close()


def test_answer_ask_thread_happy_path(tmp_path):
    seed_session(tmp_path)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.create_ask_thread("sess-1", "q", OPTIONS, nonce="n1")
    row = store.answer_ask_thread("n1", "b")  # case-insensitive key
    assert row["status"] == "answered"
    assert row["answer"] == "B"
    assert row["answer_ts"]
    store.close()


def test_answer_ask_thread_wrong_key_rejected(tmp_path):
    seed_session(tmp_path)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.create_ask_thread("sess-1", "q", OPTIONS, nonce="n1")
    try:
        store.answer_ask_thread("n1", "Z")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "not an option" in str(exc)
    assert ask_thread(store, "n1")["status"] == "open"
    store.close()


def test_answer_ask_thread_requires_an_open_ask(tmp_path):
    seed_session(tmp_path)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.create_ask_thread("sess-1", "q", OPTIONS, nonce="n1")
    store.answer_ask_thread("n1", "A")
    try:
        store.answer_ask_thread("n1", "B")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    store.create_ask_thread("sess-1", "q2", OPTIONS, nonce="n2")
    store.expire_ask_thread("n2")
    try:
        store.answer_ask_thread("n2", "A")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        store.answer_ask_thread("missing", "A")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    store.close()


def test_answer_path_refuses_approve_threads(tmp_path):
    nonce = "appr-nonce"
    write(
        tmp_path,
        event="permissionrequest",
        session_id="sess-1",
        actions=[
            {
                "action": "http",
                "label": "Approve",
                "url": "http://127.0.0.1:2586/approvals/%s?decision=allow"
                % nonce,
                "method": "POST",
            }
        ],
    )
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    try:
        store.answer_ask_thread(nonce, "A")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    store.close()


def test_expire_ask_thread_flips_only_open_asks(tmp_path):
    seed_session(tmp_path)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.create_ask_thread("sess-1", "q", OPTIONS, nonce="n1")
    assert store.expire_ask_thread("n1") is True
    assert ask_thread(store, "n1")["status"] == "expired"
    assert store.expire_ask_thread("n1") is False
    assert ask_thread(store, "n1")["status"] == "expired"
    store.close()


def test_newest_open_ask_per_session(tmp_path):
    seed_session(tmp_path)
    store = open_store(tmp_path)
    store.ingest_spool(str(tmp_path))
    store.create_ask_thread(
        "sess-1", "older", OPTIONS, nonce="old", ts="2026-10-14T12:00:00+00:00"
    )
    store.create_ask_thread(
        "sess-1", "newer", OPTIONS, nonce="new", ts="2026-10-14T12:01:00+00:00"
    )
    assert store.newest_open_ask("sess-1") == "new"
    store.answer_ask_thread("new", "A")
    assert store.newest_open_ask("sess-1") == "old"
    store.close()


# -- schema v5 migration --


def build_version_four_store(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    for version, sql in MIGRATIONS:
        if version > 4:
            continue
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
    conn.execute("INSERT INTO meta VALUES ('schema_version', '4')")
    conn.execute(
        "INSERT INTO sessions (session_id, adapter, project, cwd,"
        " first_seen, last_seen, state, last_event, last_event_ts, label)"
        " VALUES ('sess-old', 'zcode', 'mission-control', '/w',"
        " 't1', 't2', 'running', 'stop', 't2', 'old label')"
    )
    conn.execute(
        "INSERT INTO tags (session_id, tag) VALUES ('sess-old', 'build')"
    )
    conn.execute(
        "INSERT INTO threads (nonce, session_id, kind, title, body,"
        " created_ts, status) VALUES ('old-nonce', 'sess-old', 'approve',"
        " 't', 'b', 't1', 'open')"
    )
    conn.commit()
    conn.close()


def test_version_four_store_migrates_identity_in_place(tmp_path):
    path = tmp_path / "store" / "tugline.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    build_version_four_store(path)

    store = Store(path)
    version = store.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "5"
    row = session_row(store, "sess-old")
    assert row["label"] == "old label"  # v4 labels ride over untouched
    assert row["label_source"] is None  # and read as not operator-set
    assert row["tags"] == ["build"]
    thread_cols = {
        r[1] for r in store.conn.execute("PRAGMA table_info(threads)")
    }
    assert "options" in thread_cols
    assert ask_thread(store, "old-nonce")["options"] is None
    # the migrated store accepts ask threads and answers them
    created = store.create_ask_thread("sess-old", "q", OPTIONS, nonce="n9")
    assert created["options"] == OPTIONS
    assert store.answer_ask_thread("n9", "A")["answer"] == "A"
    store.close()


def test_fresh_store_is_schema_version_five(tmp_path):
    store = open_store(tmp_path)
    version = store.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "5"
    session_cols = {
        r[1] for r in store.conn.execute("PRAGMA table_info(sessions)")
    }
    assert "label_source" in session_cols
    store.close()
