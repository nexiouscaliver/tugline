"""The GET auth posture of the console API (tailnet exposure gate).

Two postures, one mechanism: with a token on file every /api read
(state, sessions, events, threads, replies, and the SSE stream)
requires the token as an Authorization: Bearer header or a ?token=
query parameter, exactly the receiver's 403; with no token on file
the console is the open loopback default, byte-identical to before
the gate existed. The page itself and its assets stay open in both
postures: the shell carries no data, so there is nothing to leak.
"""

import json
import urllib.error
import urllib.request

import pytest

from console.test_server import BASE_CONFIG, Bench

TOKEN = "sekret-token"
GATED_GETS = (
    "/api/state",
    "/api/sessions",
    "/api/events",
    "/api/threads",
    "/api/replies",
)


def stream_status(bench, path, headers=None):
    """The status of one stream request, without reading the body.

    A denial must come back as a plain response; a granted stream
    stays open, so the caller closes it the moment the status is
    known instead of draining it.
    """
    req = urllib.request.Request(bench.base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture
def token_home(tmp_path, monkeypatch):
    """A home whose credentials file holds one token."""
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    (tmp_path / "credentials").write_text(TOKEN + "\n")
    return tmp_path


@pytest.fixture
def token_bench(token_home):
    bench = Bench(token_home, token_file=token_home / "credentials")
    yield bench
    bench.stop()


@pytest.fixture
def open_bench(tmp_path, monkeypatch):
    """The loopback default: the token file does not exist."""
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    bench = Bench(tmp_path)
    yield bench
    bench.stop()


# -- token configured: every read is gated --


def test_token_gates_every_api_read(token_bench):
    for path in GATED_GETS:
        status, raw = token_bench.request("GET", path)
        assert status == 403, path
        assert json.loads(raw)["error"] == "bad token"


def test_bearer_header_unlocks_every_read(token_bench):
    headers = {"Authorization": "Bearer " + TOKEN}
    for path in GATED_GETS:
        status, _ = token_bench.request("GET", path, headers=headers)
        assert status == 200, path


def test_query_param_unlocks_every_read(token_bench):
    # the phone path: browsers cannot set headers on EventSource
    for path in GATED_GETS:
        status, _ = token_bench.request(
            "GET", path + "?token=" + TOKEN
        )
        assert status == 200, path


def test_wrong_token_is_403_by_header_and_query(token_bench):
    status, raw = token_bench.request(
        "GET", "/api/state", headers={"Authorization": "Bearer nope"}
    )
    assert status == 403
    assert json.loads(raw)["error"] == "bad token"
    status, raw = token_bench.request("GET", "/api/state?token=nope")
    assert status == 403
    assert json.loads(raw)["error"] == "bad token"


def test_empty_query_token_is_403(token_bench):
    # a blank ?token= is not a token; the gate stays shut
    status, _ = token_bench.request("GET", "/api/state?token=")
    assert status == 403


def test_unknown_api_path_is_403_without_token(token_bench):
    # the gate answers before endpoint routing, so an unauthenticated
    # caller cannot even learn which endpoints exist
    status, _ = token_bench.request("GET", "/api/nope")
    assert status == 403


def test_post_accepts_query_param_token(token_bench):
    # the page appends ?token= to every call, writes included; auth
    # passes and routing takes over (ghost nonce -> 404, not 403)
    status, _ = token_bench.post_json(
        "/api/replies?token=" + TOKEN, {"thread_nonce": "ghost", "body": "x"}
    )
    assert status == 404
    status, _ = token_bench.post_json(
        "/api/replies?token=nope", {"thread_nonce": "ghost", "body": "x"}
    )
    assert status == 403


def test_page_and_assets_stay_open_with_token_configured(token_bench):
    # the shell is static and carries no data; it stays reachable so
    # the paste-token fallback can appear
    for path in ("/", "/app.js", "/style.css"):
        status, _ = token_bench.request("GET", path)
        assert status == 200, path


# -- token configured: the SSE endpoint --


def test_stream_requires_token(token_bench):
    assert stream_status(token_bench, "/api/events/stream") == 403


def test_stream_rejects_wrong_token(token_bench):
    assert (
        stream_status(token_bench, "/api/events/stream?token=nope") == 403
    )
    assert (
        stream_status(
            token_bench,
            "/api/events/stream",
            headers={"Authorization": "Bearer nope"},
        )
        == 403
    )


def test_stream_accepts_query_param_token(token_bench):
    req = urllib.request.Request(
        token_bench.base + "/api/events/stream?token=" + TOKEN
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.headers.get("Content-Type", "").startswith(
            "text/event-stream"
        )
        assert resp.readline().decode("utf-8").strip() == "event: snapshot"


def test_stream_accepts_bearer_header(token_bench):
    req = urllib.request.Request(token_bench.base + "/api/events/stream")
    req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.headers.get("Content-Type", "").startswith(
            "text/event-stream"
        )
        assert resp.readline().decode("utf-8").strip() == "event: snapshot"


# -- no token file: the loopback default is byte-identical --


def test_reads_open_without_token_file(open_bench):
    for path in GATED_GETS:
        status, _ = open_bench.request("GET", path)
        assert status == 200, path


def test_stream_open_without_token_file(open_bench):
    req = urllib.request.Request(open_bench.base + "/api/events/stream")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.headers.get("Content-Type", "").startswith(
            "text/event-stream"
        )
        assert resp.readline().decode("utf-8").strip() == "event: snapshot"


def test_writes_open_without_token_file(open_bench):
    # routing answers, not the gate: a ghost nonce is a 404
    status, _ = open_bench.post_json(
        "/api/replies", {"thread_nonce": "ghost", "body": "x"}
    )
    assert status == 404


def test_empty_token_file_still_means_open(tmp_path, monkeypatch):
    # a configured file with no token in it is the receiver posture:
    # open access, not a lockout
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    (tmp_path / "credentials").write_text("\n")
    text = BASE_CONFIG.replace("__TOKEN_FILE__", str(tmp_path / "credentials"))
    (tmp_path / "config.toml").write_text(text)
    bench = Bench(tmp_path, text=text)
    try:
        status, _ = bench.request("GET", "/api/state")
        assert status == 200
    finally:
        bench.stop()
