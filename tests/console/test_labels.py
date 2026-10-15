"""Operator labels, honest liveness, collision-proof ids (W7-L16).

The operator finding, live on the console: several zcode sessions
looked near-identical, one short suffix collided across three of
them, and rows showed running hours after their last event. These
tests pin the three-part fix: a token-gated label write on the
sessions table (schema v3 migration), display state derived from
recency in the API layer (the store keeps raw facts), and display
ids long enough to be unique within each response.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from console.test_server import BASE_CONFIG, Bench, wait_for
from tugline.console import view
from tugline.core import paths, spool
from tugline.core.store import MIGRATIONS, Store

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)

# the live finding: three sessions whose 6 char tails all render
# "or_1_1" plus one whose tail is the only thing that identifies it
COLLIDING_IDS = [
    "sess_dwf-dwfrun-b5c5ce13-a3c0-4863-a561-1c1aad4da86f-actor_1_1",
    "sess_dwf-dwfrun-9f2c7741-52ab-4c31-8d20-7c88a3e01142-actor_1_1",
    "sess_dwf-dwfrun-3d9b0a55-77de-4f02-9a1c-5b6d8e9f0a13-actor_1_1",
    "sess_517ee9c0-bdc0-49a9-9938-1c2e4de851df",
]


def seen(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def session_row(session_id, state, last_seen, label=None):
    return {
        "session_id": session_id,
        "state": state,
        "last_seen": last_seen,
        "label": label,
    }


def seed_session(home, session_id, event="notify", minutes_ago=0):
    """One spool record for one session at a fixed age."""
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return spool.append(
        paths.spool_root(),
        event=event,
        session_id=session_id,
        topic="main",
        priority="default",
        title="seed",
        body="seed body",
        now=when,
    )


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


def put_label(bench, session_id, label):
    """PUT one label; returns (status, parsed body)."""
    status, raw = bench.request(
        "PUT", "/api/sessions/%s/label" % session_id, body={"label": label}
    )
    return status, json.loads(raw)


# -- PUT /api/sessions/<id>/label: set, clear, validate --


def test_put_label_sets_and_shows(bench):
    seed_session(bench.home, "sess-1")
    assert wait_for(lambda: bench.store.sessions())
    status, data = put_label(bench, "sess-1", "controller")
    assert status == 200
    assert data["ok"] is True
    assert data["session"]["label"] == "controller"
    status, data = bench.get_json("/api/sessions")
    row = [s for s in data["sessions"] if s["session_id"] == "sess-1"][0]
    assert row["label"] == "controller"


def test_put_empty_label_clears(bench):
    seed_session(bench.home, "sess-1")
    assert wait_for(lambda: bench.store.sessions())
    status, _ = put_label(bench, "sess-1", "name")
    assert status == 200
    status, data = put_label(bench, "sess-1", "   ")
    assert status == 200
    assert data["session"]["label"] is None
    row = [s for s in bench.store.sessions() if s["session_id"] == "sess-1"][0]
    assert row["label"] is None


def test_put_label_strips_surrounding_space(bench):
    seed_session(bench.home, "sess-1")
    assert wait_for(lambda: bench.store.sessions())
    status, data = put_label(bench, "sess-1", "  spaced  ")
    assert status == 200
    assert data["session"]["label"] == "spaced"


def test_put_label_length_limit_is_sixty(bench):
    seed_session(bench.home, "sess-1")
    assert wait_for(lambda: bench.store.sessions())
    status, _ = put_label(bench, "sess-1", "x" * 60)
    assert status == 200
    status, data = put_label(bench, "sess-1", "x" * 61)
    assert status == 400
    assert "label" in data["error"]


def test_put_label_rejects_non_string(bench):
    seed_session(bench.home, "sess-1")
    assert wait_for(lambda: bench.store.sessions())
    for bad in (123, None, ["x"], {"a": 1}):
        status, data = bench.request(
            "PUT", "/api/sessions/sess-1/label", body={"label": bad}
        )
        data = json.loads(data)
        assert status == 400, bad
        assert "label" in data["error"]


def test_put_label_rejects_control_characters(bench):
    seed_session(bench.home, "sess-1")
    assert wait_for(lambda: bench.store.sessions())
    for bad in ("two\nlines", "tab\there", "bell\x07"):
        status, data = bench.request(
            "PUT", "/api/sessions/sess-1/label", body={"label": bad}
        )
        data = json.loads(data)
        assert status == 400, repr(bad)
        assert "plain" in data["error"]


def test_put_label_bad_body_is_400(bench):
    seed_session(bench.home, "sess-1")
    assert wait_for(lambda: bench.store.sessions())
    status, raw = bench.request(
        "PUT", "/api/sessions/sess-1/label", body=b"{not json"
    )
    assert status == 400
    assert json.loads(raw)["error"]
    status, data = bench.request(
        "PUT", "/api/sessions/sess-1/label", body=["not", "an", "object"]
    )
    assert status == 400
    status, _ = bench.request(
        "PUT", "/api/sessions/sess-1/label", body={}
    )
    assert status == 400


def test_put_label_unknown_session_is_404(bench):
    status, data = put_label(bench, "ghost", "x")
    assert status == 404
    assert data["error"]


def test_label_survives_later_ingest(bench):
    seed_session(bench.home, "sess-1")
    assert wait_for(lambda: bench.store.sessions())
    status, _ = put_label(bench, "sess-1", "keeper")
    assert status == 200
    seed_session(bench.home, "sess-1", event="stop")
    assert wait_for(
        lambda: bench.store.sessions()[0]["last_event"] == "stop"
    )
    row = [s for s in bench.store.sessions() if s["session_id"] == "sess-1"][0]
    assert row["label"] == "keeper"
    assert row["state"] == "done"


# -- auth on the PUT: token-gated like every other write --


@pytest.fixture
def token_bench(tmp_path, monkeypatch):
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    (tmp_path / "credentials").write_text("sekret-token\n")
    bench = Bench(tmp_path, token_file=tmp_path / "credentials")
    yield bench
    bench.stop()


def test_put_label_denied_without_token(token_bench):
    status, raw = token_bench.request(
        "PUT", "/api/sessions/ghost/label", body={"label": "x"}
    )
    assert status == 403
    assert json.loads(raw)["error"] == "bad token"


def test_put_label_denied_with_wrong_token(token_bench):
    status, _ = token_bench.request(
        "PUT",
        "/api/sessions/ghost/label",
        body={"label": "x"},
        headers={"Authorization": "Bearer nope"},
    )
    assert status == 403
    status, _ = token_bench.request(
        "PUT", "/api/sessions/ghost/label?token=nope", body={"label": "x"}
    )
    assert status == 403


def test_put_label_accepts_bearer_and_query_token(token_bench):
    # the gate passes and routing answers: ghost session -> 404, not 403
    status, _ = token_bench.request(
        "PUT",
        "/api/sessions/ghost/label",
        body={"label": "x"},
        headers={"Authorization": "Bearer sekret-token"},
    )
    assert status == 404
    status, _ = token_bench.request(
        "PUT",
        "/api/sessions/ghost/label?token=sekret-token",
        body={"label": "x"},
    )
    assert status == 404


# -- schema migration: existing dbs gain the label column in place --


def test_fresh_store_is_schema_version_five(tmp_path):
    store = Store(tmp_path / "store" / "tugline.db")
    version = store.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "7"
    cols = {
        row[1] for row in store.conn.execute("PRAGMA table_info(sessions)")
    }
    assert "label" in cols
    store.close()


def test_version_two_store_migrates_label_in_place(tmp_path):
    path = tmp_path / "store" / "tugline.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0][1])
    conn.executescript(MIGRATIONS[1][1])
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    conn.execute(
        "INSERT INTO sessions (session_id, adapter, project, cwd,"
        " first_seen, last_seen, state, last_event, last_event_ts)"
        " VALUES ('live-1', 'zcode', 'mission-control', '/w',"
        " '2026-10-13T22:30:00+00:00', '2026-10-13T23:36:00+00:00',"
        " 'running', 'sessionstart', '2026-10-13T23:36:00+00:00')"
    )
    conn.execute(
        "INSERT INTO events (spool_id, ts, session_id, adapter, event,"
        " topic, priority, title, body)"
        " VALUES ('sp-1', '2026-10-13T23:36:00+00:00', 'live-1', 'zcode',"
        " 'sessionstart', 'main', 'default', 't', 'b')"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    version = store.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "7"
    # the live row rides over untouched, label starts NULL
    row = store.sessions()[0]
    assert row["session_id"] == "live-1"
    assert row["state"] == "running"
    assert row["last_event"] == "sessionstart"
    assert row["label"] is None
    assert len(store.events()) == 1
    # and the label write works on the migrated table
    updated = store.set_session_label("live-1", "stalled sibling")
    assert updated["label"] == "stalled sibling"
    assert updated["state"] == "running"  # facts did not move
    store.close()


def test_set_session_label_unknown_session_raises(tmp_path):
    store = Store(tmp_path / "store" / "tugline.db")
    with pytest.raises(KeyError):
        store.set_session_label("ghost", "x")
    store.close()


# -- liveness derivation: clock-injected, boundary exact --


def test_done_stays_done_at_any_age():
    assert view.derive_display_state("done", seen(0), NOW) == "done"
    assert view.derive_display_state("done", seen(600), NOW) == "done"


def test_waiting_keeps_waiting_even_hours_old():
    assert view.derive_display_state("waiting", seen(240), NOW) == "waiting"


def test_running_within_five_minutes_is_active():
    assert view.derive_display_state("running", seen(1), NOW) == "active"
    assert view.derive_display_state("running", seen(5), NOW) == "active"


def test_running_between_windows_still_running():
    assert view.derive_display_state("running", seen(6), NOW) == "running"
    assert view.derive_display_state("running", seen(30), NOW) == "running"


def test_running_older_than_thirty_minutes_is_idle():
    # the live finding: sessionstart at 22:36 read as running at
    # 06:00; six hours of silence must render idle
    assert view.derive_display_state("running", seen(31), NOW) == "idle"
    assert view.derive_display_state("running", seen(360), NOW) == "idle"


def test_unparseable_last_seen_is_never_active():
    for bad in ("not-a-date", "", None):
        assert view.derive_display_state("running", bad, NOW) == "idle", bad
    assert view.derive_display_state("done", "not-a-date", NOW) == "done"
    assert view.derive_display_state("waiting", "", NOW) == "waiting"


# -- decorate: display fields, active-first ordering --


def test_decorate_attaches_display_fields():
    rows = [session_row("sess_abc123", "running", seen(1))]
    out = view.decorate_sessions(rows, now=NOW)
    assert out[0]["display_state"] == "active"
    assert out[0]["display_id"] == "abc123"
    assert out[0]["state"] == "running"  # raw fact kept
    assert rows[0].get("display_state") is None  # input not mutated


def test_decorate_sorts_active_first_then_last_seen():
    # store order is newest first: a session that just stopped, an
    # active one two minutes old, then a long-silent one; the active
    # row floats to the top, the rest keep recency order
    rows = [
        session_row("just-stopped", "done", seen(1)),
        session_row("act-two", "running", seen(2)),
        session_row("idle-old", "running", seen(40)),
    ]
    out = view.decorate_sessions(rows, now=NOW)
    assert [r["session_id"] for r in out] == [
        "act-two",
        "just-stopped",
        "idle-old",
    ]
    assert [r["display_state"] for r in out] == [
        "active",
        "done",
        "idle",
    ]


def test_decorate_missing_last_seen_sorts_after_active():
    rows = [
        session_row("no-time", "running", ""),
        session_row("act", "running", seen(1)),
    ]
    out = view.decorate_sessions(rows, now=NOW)
    assert out[0]["session_id"] == "act"
    assert out[1]["display_state"] == "idle"


# -- display uniqueness: the colliding fixture --


def test_short_tail_when_no_collision():
    display = view.unique_display_ids(COLLIDING_IDS)
    assert display[COLLIDING_IDS[3]] == "e851df"


def test_colliding_tails_lengthen_until_unique():
    display = view.unique_display_ids(COLLIDING_IDS)
    actor_shown = [display[i] for i in COLLIDING_IDS[:3]]
    # distinct, each a suffix of its own id, each longer than 6
    assert len(set(actor_shown)) == 3
    for shown, full in zip(actor_shown, COLLIDING_IDS[:3]):
        assert full.endswith(shown)
        assert len(shown) > 6
    # every value in the map is unique across the response
    assert len(set(display.values())) == len(COLLIDING_IDS)


def test_identical_ids_fall_back_to_the_full_id():
    twin = "sess_same"
    display = view.unique_display_ids([twin, twin])
    assert display[twin] == twin


def test_sessions_endpoint_disambiguates_colliding_ids(bench):
    when = datetime.now(timezone.utc) - timedelta(minutes=10)
    for offset, session_id in enumerate(COLLIDING_IDS):
        spool.append(
            paths.spool_root(),
            event="sessionstart",
            session_id=session_id,
            topic="main",
            priority="default",
            title="seed",
            body="seed body",
            now=when + timedelta(seconds=offset),
        )
    assert wait_for(lambda: len(bench.store.sessions()) == 4)
    status, data = bench.get_json("/api/sessions")
    assert status == 200
    shown = {s["session_id"]: s["display_id"] for s in data["sessions"]}
    assert len(set(shown.values())) == 4
    assert shown[COLLIDING_IDS[3]] == "e851df"
    # a label on one of the colliding rows rides along, id stays unique
    status, _ = put_label(bench, COLLIDING_IDS[0], "the live controller")
    assert status == 200
    status, data = bench.get_json("/api/sessions")
    rows = {s["session_id"]: s for s in data["sessions"]}
    assert rows[COLLIDING_IDS[0]]["label"] == "the live controller"
    assert len(set(s["display_id"] for s in data["sessions"])) == 4


def test_state_snapshot_carries_the_same_decoration(bench):
    seed_session(bench.home, "sess-1", minutes_ago=1)
    assert wait_for(lambda: bench.store.sessions())
    status, state = bench.get_json("/api/state")
    assert status == 200
    row = state["sessions"][0]
    assert row["display_state"] == "active"
    assert row["display_id"]


def test_sessions_derive_states_over_http(bench):
    seed_session(bench.home, "fresh-running", minutes_ago=0)
    seed_session(bench.home, "stale-running", event="notify", minutes_ago=90)
    seed_session(bench.home, "stale-stop", event="stop", minutes_ago=60)
    seed_session(bench.home, "stale-wait", event="permissionrequest",
                 minutes_ago=45)
    assert wait_for(lambda: len(bench.store.sessions()) == 4)
    status, data = bench.get_json("/api/sessions")
    states = {s["session_id"]: s["display_state"] for s in data["sessions"]}
    assert states["fresh-running"] == "active"
    assert states["stale-running"] == "idle"
    assert states["stale-stop"] == "done"
    assert states["stale-wait"] == "waiting"
    # active sorts first no matter how recent the stale rows were
    assert data["sessions"][0]["session_id"] == "fresh-running"
