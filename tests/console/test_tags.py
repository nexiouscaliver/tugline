"""Session tags and name-based reply targeting (W7-L18, console half).

The operator ask: tag any session for reference (operator and agent
both), and reply by name instead of raw session id. These tests pin
the two tag endpoints (PUT/DELETE, token-gated like the label PUT),
tags riding on every session listing, and the reply resolution order
the UI and the operator depend on: exact session id, then unique
label, then unique tag, with the ambiguous case a 400 that names the
matching sessions so the console can offer the choice.
"""

import json
from datetime import datetime, timedelta, timezone

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


@pytest.fixture
def token_bench(tmp_path, monkeypatch):
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    (tmp_path / "credentials").write_text("sekret-token\n")
    bench = Bench(tmp_path, token_file=tmp_path / "credentials")
    yield bench
    bench.stop()


def seed_session(home, session_id, minutes_ago=0, event="notify"):
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


def seeded(bench, *ids):
    for offset, sid in enumerate(ids):
        seed_session(
            bench.home,
            sid,
            minutes_ago=10 + offset,
        )
    assert wait_for(
        lambda: {s["session_id"] for s in bench.store.sessions()} >= set(ids)
    )


def put_tag(bench, session_id, tag):
    status, raw = bench.request(
        "PUT", "/api/sessions/%s/tag/%s" % (session_id, tag)
    )
    return status, json.loads(raw)


def delete_tag(bench, session_id, tag):
    status, raw = bench.request(
        "DELETE", "/api/sessions/%s/tag/%s" % (session_id, tag)
    )
    return status, json.loads(raw)


def label(bench, session_id, text):
    status, raw = bench.request(
        "PUT", "/api/sessions/%s/label" % session_id, body={"label": text}
    )
    assert status == 200, raw
    return json.loads(raw)


def reply(bench, payload):
    status, raw = bench.request("POST", "/api/replies", body=payload)
    return status, json.loads(raw)


# -- PUT /api/sessions/<id>/tag/<tag> --


def test_put_tag_attaches_and_normalizes(bench):
    seeded(bench, "sess-1")
    status, data = put_tag(bench, "sess-1", "Build-Night")
    assert status == 200
    assert data["ok"] is True
    assert data["session"]["tags"] == ["build-night"]
    assert bench.store.session_tags("sess-1") == ["build-night"]


def test_put_tag_is_idempotent(bench):
    seeded(bench, "sess-1")
    put_tag(bench, "sess-1", "one")
    status, data = put_tag(bench, "sess-1", "one")
    assert status == 200
    assert data["session"]["tags"] == ["one"]


def test_put_tag_garbage_is_400(bench):
    seeded(bench, "sess-1")
    # spaces and punctuation decode to tags that normalize to nothing
    for bad in ("%20%20%20", "%21%21%21", "!!!"):
        status, data = put_tag(bench, "sess-1", bad)
        assert status == 400, bad
        assert "tag" in data["error"]


def test_put_tag_empty_segment_misses_the_route(bench):
    # a bare trailing slash never matches the 5-segment route
    seeded(bench, "sess-1")
    status, data = bench.request("PUT", "/api/sessions/sess-1/tag/")
    assert status == 404


def test_put_tag_over_length_is_400(bench):
    seeded(bench, "sess-1")
    status, data = put_tag(bench, "sess-1", "a" * 25)
    assert status == 400
    assert "24" in data["error"]
    status, _ = put_tag(bench, "sess-1", "a" * 24)
    assert status == 200


def test_put_tag_ninth_tag_is_400(bench):
    seeded(bench, "sess-1")
    for i in range(8):
        status, _ = put_tag(bench, "sess-1", "tag-%d" % i)
        assert status == 200
    status, data = put_tag(bench, "sess-1", "one-too-many")
    assert status == 400
    assert "8" in data["error"]


def test_put_tag_unknown_session_is_404(bench):
    status, data = put_tag(bench, "ghost", "one")
    assert status == 404
    assert data["error"] == "unknown session"


def test_put_tag_denied_without_token(token_bench):
    seeded(token_bench, "sess-1")
    status, data = put_tag(token_bench, "sess-1", "one")
    assert status == 403
    assert data["error"] == "bad token"


def test_put_tag_accepts_bearer_and_query_token(token_bench):
    seeded(token_bench, "sess-1")
    status, _ = token_bench.request(
        "PUT",
        "/api/sessions/sess-1/tag/one",
        headers={"Authorization": "Bearer sekret-token"},
    )
    assert status == 200
    status, _ = token_bench.request(
        "PUT", "/api/sessions/sess-1/tag/two?token=sekret-token"
    )
    assert status == 200
    assert token_bench.store.session_tags("sess-1") == ["one", "two"]


# -- DELETE /api/sessions/<id>/tag/<tag> --


def test_delete_tag_detaches(bench):
    seeded(bench, "sess-1")
    put_tag(bench, "sess-1", "one")
    put_tag(bench, "sess-1", "two")
    status, data = delete_tag(bench, "sess-1", "ONE")  # normalized match
    assert status == 200
    assert data["session"]["tags"] == ["two"]


def test_delete_absent_tag_stays_200(bench):
    seeded(bench, "sess-1")
    status, data = delete_tag(bench, "sess-1", "never-there")
    assert status == 200
    assert data["session"]["tags"] == []


def test_delete_tag_unknown_session_is_404(bench):
    status, data = delete_tag(bench, "ghost", "one")
    assert status == 404
    assert data["error"] == "unknown session"


def test_delete_tag_denied_without_token(token_bench):
    seeded(token_bench, "sess-1")
    put_tag(token_bench, "sess-1", "one")
    status, data = delete_tag(token_bench, "sess-1", "one")
    assert status == 403
    assert data["error"] == "bad token"


# -- tags ride every listing --


def test_sessions_listing_carries_tags(bench):
    seeded(bench, "sess-1", "sess-2")
    put_tag(bench, "sess-1", "alpha")
    status, data = bench.get_json("/api/sessions")
    rows = {s["session_id"]: s for s in data["sessions"]}
    assert rows["sess-1"]["tags"] == ["alpha"]
    assert rows["sess-2"]["tags"] == []


def test_state_snapshot_carries_tags(bench):
    seeded(bench, "sess-1")
    put_tag(bench, "sess-1", "alpha")
    status, state = bench.get_json("/api/state")
    assert status == 200
    row = [s for s in state["sessions"] if s["session_id"] == "sess-1"][0]
    assert row["tags"] == ["alpha"]


# -- POST /api/replies: name targeting --


def test_reply_by_label_resolves(bench):
    seeded(bench, "sess-1", "sess-2")
    label(bench, "sess-1", "controller")
    status, data = reply(bench, {"session_id": "controller", "body": "hi"})
    assert status == 200
    assert data["reply"]["session_id"] == "sess-1"
    assert bench.store.replies()[0]["session_id"] == "sess-1"


def test_reply_by_tag_resolves(bench):
    seeded(bench, "sess-1", "sess-2")
    put_tag(bench, "sess-1", "build-night")
    status, data = reply(bench, {"session_id": "build-night", "body": "hi"})
    assert status == 200
    assert data["reply"]["session_id"] == "sess-1"


def test_reply_resolution_order_id_then_label_then_tag(bench):
    # a session whose id IS what another session carries as label and
    # tag: the exact id must win
    seeded(bench, "alpha", "sess-1")
    label(bench, "sess-1", "alpha")
    put_tag(bench, "sess-1", "alpha")
    status, data = reply(bench, {"session_id": "alpha", "body": "hi"})
    assert status == 200
    assert data["reply"]["session_id"] == "alpha"

    # with no id collision, a unique label beats a tag
    seeded(bench, "sess-2", "sess-3")
    label(bench, "sess-2", "night")
    put_tag(bench, "sess-3", "night")
    status, data = reply(bench, {"session_id": "night", "body": "hi"})
    assert status == 200
    assert data["reply"]["session_id"] == "sess-2"


def test_reply_ambiguous_tag_is_400_with_session_list(bench):
    seeded(bench, "sess-1", "sess-2", "sess-3")
    put_tag(bench, "sess-1", "build")
    put_tag(bench, "sess-2", "build")
    put_tag(bench, "sess-3", "other")
    status, data = reply(bench, {"session_id": "build", "body": "hi"})
    assert status == 400
    assert "ambiguous" in data["error"]
    assert sorted(data["sessions"]) == ["sess-1", "sess-2"]
    # and nothing was recorded
    assert bench.store.replies() == []


def test_reply_ambiguous_label_is_400_with_session_list(bench):
    seeded(bench, "sess-1", "sess-2")
    label(bench, "sess-1", "twin")
    label(bench, "sess-2", "twin")
    status, data = reply(bench, {"session_id": "twin", "body": "hi"})
    assert status == 400
    assert sorted(data["sessions"]) == ["sess-1", "sess-2"]


def test_reply_unknown_name_is_404(bench):
    seeded(bench, "sess-1")
    status, data = reply(bench, {"session_id": "ghost", "body": "hi"})
    assert status == 404
    assert data["error"] == "unknown session"


def test_reply_by_thread_still_works_alongside_names(bench, home):
    from console.test_server import seed_permission_request, wait_for_thread

    seed_permission_request("nonce-1", session_id="sess-1")
    wait_for_thread(bench, "nonce-1")
    status, data = reply(bench, {"thread_nonce": "nonce-1", "body": "hi"})
    assert status == 200
    assert data["reply"]["session_id"] == "sess-1"
    assert data["reply"]["thread_nonce"] == "nonce-1"
    # both targets at once stays the unchanged 400
    status, data = reply(
        bench,
        {"thread_nonce": "nonce-1", "session_id": "sess-1", "body": "hi"},
    )
    assert status == 400
