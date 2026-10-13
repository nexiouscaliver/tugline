"""Tests for the console HTTP API (tugline.console.server).

One real ConsoleServer on port 0 against a temp TUGLINE_HOME with a
real Store and a synthetic spool; requests go over HTTP like the
browser will.
"""

import json
import os
import time
import urllib.error
import urllib.request

import pytest

from tugline.console.server import ConsoleServer
from tugline.core import approval_files, paths, spool
from tugline.core.config import load_config
from tugline.core.store import Store

BASE_CONFIG = """\
[server]
url = "http://127.0.0.1:2599"
console_port = 0
token_file = "__TOKEN_FILE__"

[topics]
main = "tug"
warn = "tug-warn"
reply = "tug-reply"

[policy]
idle_seconds = 600

[toggles]
console = true
"""


class Bench:
    """A running console plus small HTTP helpers."""

    def __init__(self, home, text=None, token_file=None, interval=0.2):
        if text is None:
            # a token file that does not exist keeps POSTs open; the
            # machine's real ~/.tugline must never leak into a test
            target = token_file or (home / "credentials-absent")
            text = BASE_CONFIG.replace("__TOKEN_FILE__", str(target))
        (home / "config.toml").write_text(text)
        self.home = str(home)
        self.config = load_config()
        self.store = Store(paths.store_path())
        self.server = ConsoleServer(
            self.config, self.store, home=self.home, ingest_interval=interval
        )
        assert self.server.start() is True
        self.base = "http://127.0.0.1:%d" % self.server.port

    def request(self, method, path, body=None, headers=None, timeout=15):
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get_json(self, path):
        status, raw = self.request("GET", path)
        return status, json.loads(raw)

    def post_json(self, path, body=None, headers=None):
        status, raw = self.request("POST", path, body=body, headers=headers)
        return status, json.loads(raw)

    def stop(self):
        self.server.stop()
        self.store.close()


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


def approve_actions(nonce):
    return [
        {
            "action": "http",
            "label": "Approve",
            "url": "http://127.0.0.1:2586/approvals/%s?decision=allow" % nonce,
            "method": "POST",
        }
    ]


def seed_permission_request(nonce, session_id="sess-1"):
    """Pending marker plus one spool permissionrequest with buttons."""
    approval_files.register_pending(paths.approvals_root(), nonce)
    return spool.append(
        paths.spool_root(),
        event="permissionrequest",
        session_id=session_id,
        topic="warn",
        priority="urgent",
        title="permission request",
        body="session %s asks permission" % session_id,
        actions=approve_actions(nonce),
    )


def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def wait_for_thread(bench, nonce):
    assert wait_for(
        lambda: any(t["nonce"] == nonce for t in bench.store.threads())
    ), "thread %s never appeared in the store" % nonce


# -- config wiring --


def test_console_config_defaults_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    conf = load_config()
    assert conf.path == str(tmp_path / "config.toml")
    assert conf.server.console_port == 2588
    assert conf.toggles.console is True
    text = (tmp_path / "config.toml").read_text()
    assert str(tmp_path / "credentials") in text


# -- lifecycle and static pages --


def test_port_zero_records_the_bound_port(bench):
    assert isinstance(bench.server.port, int)
    assert bench.server.port > 0


def test_root_serves_the_html_page(bench):
    status, raw = bench.request("GET", "/")
    assert status == 200
    text = raw.decode()
    assert text.lstrip().startswith("<!doctype html>")
    assert "/api/state" in text


# -- GET endpoints --


def test_state_shape_and_health(bench, home):
    seed_permission_request("nonce-1")
    assert wait_for(lambda: bench.store.sessions())
    status, state = bench.get_json("/api/state")
    assert status == 200
    for key in ("sessions", "events", "threads", "replies", "counts"):
        assert key in state
    assert {"daemon", "ntfy"} <= set(state["health"])
    assert state["spool_depth"] == 1
    assert state["last_ingest_ts"]
    assert state["sessions"][0]["session_id"] == "sess-1"


def test_sessions_endpoint(bench, home):
    seed_permission_request("nonce-1")
    assert wait_for(lambda: bench.store.sessions())
    status, data = bench.get_json("/api/sessions")
    assert status == 200
    assert [s["session_id"] for s in data["sessions"]] == ["sess-1"]


def test_events_endpoint_limit_and_session(bench, home):
    seed_permission_request("nonce-1", session_id="sess-1")
    seed_permission_request("nonce-2", session_id="sess-2")
    assert wait_for(lambda: len(bench.store.events()) >= 2)
    status, data = bench.get_json("/api/events?limit=1")
    assert status == 200
    assert len(data["events"]) == 1
    status, data = bench.get_json("/api/events?session=sess-2")
    assert status == 200
    assert data["events"]
    assert all(e["session_id"] == "sess-2" for e in data["events"])


def test_events_bad_limit_is_a_400(bench):
    status, data = bench.get_json("/api/events?limit=many")
    assert status == 400
    assert "error" in data


def test_threads_endpoint_status_filter(bench, home):
    seed_permission_request("nonce-1")
    wait_for_thread(bench, "nonce-1")
    status, data = bench.get_json("/api/threads")
    assert status == 200
    assert [t["nonce"] for t in data["threads"]] == ["nonce-1"]
    status, data = bench.get_json("/api/threads?status=open")
    assert [t["nonce"] for t in data["threads"]] == ["nonce-1"]
    status, data = bench.get_json("/api/threads?status=answered")
    assert data["threads"] == []


def test_replies_endpoint_empty(bench):
    status, data = bench.get_json("/api/replies")
    assert status == 200
    assert data["replies"] == []


def test_unknown_endpoint_is_404(bench):
    status, data = bench.get_json("/api/nope")
    assert status == 404
    assert data["error"]


# -- POST /api/replies --


def test_post_reply_to_thread_records_and_appends(bench, home):
    seed_permission_request("nonce-1")
    wait_for_thread(bench, "nonce-1")
    status, data = bench.post_json(
        "/api/replies", {"thread_nonce": "nonce-1", "body": "go ahead"}
    )
    assert status == 200
    assert data["ok"] is True
    row = bench.store.replies()[0]
    assert row["source"] == "console"
    assert row["thread_nonce"] == "nonce-1"
    assert row["session_id"] == "sess-1"
    assert row["body"] == "go ahead"
    line = (home / "replies.jsonl").read_text().splitlines()[-1]
    entry = json.loads(line)
    for key in ("id", "ts", "topic", "title", "body", "consumed"):
        assert key in entry
    assert entry["consumed"] is False
    assert entry["source"] == "console"
    assert entry["body"] == "go ahead"


def test_post_reply_truncates_huge_body(bench, home):
    seed_permission_request("nonce-1")
    wait_for_thread(bench, "nonce-1")
    status, _ = bench.post_json(
        "/api/replies", {"thread_nonce": "nonce-1", "body": "x" * 20000}
    )
    assert status == 200
    line = (home / "replies.jsonl").read_text().splitlines()[-1]
    assert len(line.encode("utf-8")) < 4096
    entry = json.loads(line)
    assert entry["body"]
    assert bench.store.replies()[0]["body"] == "x" * 20000


def test_post_reply_session_target(bench, home):
    seed_permission_request("nonce-1")
    wait_for_thread(bench, "nonce-1")
    status, _ = bench.post_json(
        "/api/replies", {"session_id": "sess-1", "body": "hi"}
    )
    assert status == 200
    assert bench.store.replies()[0]["thread_nonce"] is None


def test_post_reply_needs_exactly_one_target(bench, home):
    seed_permission_request("nonce-1")
    wait_for_thread(bench, "nonce-1")
    both = {"thread_nonce": "nonce-1", "session_id": "sess-1", "body": "x"}
    status, data = bench.post_json("/api/replies", both)
    assert status == 400
    assert data["error"]
    status, data = bench.post_json("/api/replies", {"body": "x"})
    assert status == 400
    assert data["error"]


def test_post_reply_unknown_nonce_is_404(bench):
    status, data = bench.post_json(
        "/api/replies", {"thread_nonce": "ghost", "body": "x"}
    )
    assert status == 404
    assert data["error"]


def test_post_reply_bad_json_is_400(bench):
    status, raw = bench.request(
        "POST", "/api/replies", body=b"{not json", headers={}
    )
    assert status == 400
    assert json.loads(raw)["error"]


# -- POST /api/approvals/<nonce> --


def test_approve_allow_writes_files_and_updates_thread(bench, home):
    seed_permission_request("nonce-1")
    wait_for_thread(bench, "nonce-1")
    status, data = bench.post_json("/api/approvals/nonce-1?decision=allow")
    assert status == 200
    assert data["ok"] is True
    root = paths.approvals_root()
    assert not os.path.exists(os.path.join(root, "nonce-1.pending"))
    saved = approval_files.read_decision(root, "nonce-1")
    assert saved["decision"] == "allow"
    thread = [t for t in bench.store.threads() if t["nonce"] == "nonce-1"][0]
    assert thread["status"] == "answered"
    assert thread["answer"] == "allow"


def test_approve_deny_writes_files_and_updates_thread(bench, home):
    seed_permission_request("nonce-2")
    wait_for_thread(bench, "nonce-2")
    status, data = bench.post_json("/api/approvals/nonce-2?decision=deny")
    assert status == 200
    root = paths.approvals_root()
    saved = approval_files.read_decision(root, "nonce-2")
    assert saved["decision"] == "deny"
    thread = [t for t in bench.store.threads() if t["nonce"] == "nonce-2"][0]
    assert thread["answer"] == "deny"


def test_approve_unknown_nonce_is_404(bench):
    status, data = bench.post_json("/api/approvals/ghost?decision=allow")
    assert status == 404
    assert data["error"]


def test_approve_bad_decision_is_400(bench, home):
    seed_permission_request("nonce-1")
    wait_for_thread(bench, "nonce-1")
    status, data = bench.post_json("/api/approvals/nonce-1?decision=banana")
    assert status == 400
    assert data["error"]
    assert os.path.exists(
        os.path.join(paths.approvals_root(), "nonce-1.pending")
    )


def test_approve_missing_decision_is_400(bench, home):
    seed_permission_request("nonce-1")
    wait_for_thread(bench, "nonce-1")
    status, data = bench.post_json("/api/approvals/nonce-1")
    assert status == 400


def test_approve_already_answered_is_200(bench, home):
    seed_permission_request("nonce-1")
    wait_for_thread(bench, "nonce-1")
    root = paths.approvals_root()
    approval_files.write_decision(root, "nonce-1", "deny")
    approval_files.remove_pending(root, "nonce-1")
    status, data = bench.post_json("/api/approvals/nonce-1?decision=allow")
    assert status == 200
    assert data["status"] == "already answered"
    assert approval_files.read_decision(root, "nonce-1")["decision"] == "deny"


# -- auth posture --


def test_token_gates_post_but_not_get(tmp_path, monkeypatch):
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    (tmp_path / "credentials").write_text("sekret-token\n")
    bench = Bench(tmp_path, token_file=tmp_path / "credentials")
    try:
        status, _ = bench.get_json("/api/state")
        assert status == 200
        status, data = bench.post_json(
            "/api/replies", {"session_id": "s", "body": "x"}
        )
        assert status == 403
        assert data["error"]
        status, data = bench.post_json(
            "/api/replies",
            {"session_id": "s", "body": "x"},
            headers={"Authorization": "Bearer sekret-token"},
        )
        assert status == 200
    finally:
        bench.stop()


# -- SSE --


def test_stream_snapshot_then_delta_after_ingest(bench, home):
    req = urllib.request.Request(bench.base + "/api/events/stream")
    seen = {}
    current = None
    got_snapshot = False
    seeded = None
    expected_id = None
    with urllib.request.urlopen(req, timeout=20) as resp:
        assert resp.headers.get("Content-Type", "").startswith(
            "text/event-stream"
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8").strip()
            if line.startswith("event:"):
                current = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current:
                seen[current] = json.loads(line.split(":", 1)[1].strip())
                if current == "snapshot":
                    got_snapshot = True
            if got_snapshot and seeded is None:
                seeded = seed_permission_request("nonce-sse")
            if seeded is not None and expected_id is None:
                rows = [
                    e
                    for e in bench.store.events(limit=50)
                    if e["spool_id"] == seeded["id"]
                ]
                if rows:
                    expected_id = rows[0]["id"]
            if expected_id is not None and seen.get("delta", {}).get(
                "last_event_id"
            ) == expected_id:
                break
    assert got_snapshot
    assert "counts" in seen["snapshot"]
    assert expected_id is not None
    assert seen["delta"]["last_event_id"] == expected_id
    assert "counts" in seen["delta"]
