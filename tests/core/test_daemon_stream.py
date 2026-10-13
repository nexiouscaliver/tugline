"""Streaming subscribe tests: rate behavior and connection handling.

The live dogfood run showed the daemon 429-looping: a poll=1 request
every half second burns ntfy's default visitor budget (burst 60, one
token per 5 seconds) inside a minute, and daemon.err.log grew about
1.4 lines per second. These tests pin the fix: one held streaming GET
per window, reconnect with the same cursor when the stream ends, and
a 6 second floor between reply requests when the stream cannot be
established at all (replenish is 1 per 5 seconds; 6 keeps the bucket
full).
"""

import json
import threading
import time
import urllib.error
import urllib.request

from fake_ntfy import RateLimiter
from tugline.core import config as cfg
from tugline.core.daemon import Daemon


class FakeClock:
    """Clock the fake server and the test agree on; only the test
    advances it, so a simulated 10 minutes runs in milliseconds."""

    def __init__(self, start=1000000.0):
        self.now = float(start)

    def __call__(self):
        return self.now


def make_config(url):
    return cfg.Config(
        server=cfg.ServerConfig(url, None),
        topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
        policy=cfg.PolicyConfig(600),
        toggles=cfg.TogglesConfig(True, True, False),
        path="/none",
    )


def make_daemon(fake_ntfy, tmp_path, **kw):
    kw.setdefault("poll_timeout", 2.0)
    kw.setdefault("stream_timeout", 10.0)
    kw.setdefault("backoff_base", 0.01)
    kw.setdefault("cycle_idle", 0.0)
    return Daemon(
        make_config(fake_ntfy.url),
        spool_root=str(tmp_path / "spool"),
        replies_path=str(tmp_path / "replies.jsonl"),
        state_root=str(tmp_path / "state"),
        **kw,
    )


def future():
    """A message time no since value computed in this test can top."""
    return int(time.time()) + 3600


def read_replies(tmp_path):
    path = tmp_path / "replies.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# the fake enforces the real numbers


def test_rate_limiter_enforces_the_real_visitor_numbers():
    clock = FakeClock(1000.0)
    limiter = RateLimiter(clock=clock)
    assert [limiter.allow() for _ in range(60)] == [True] * 60
    assert limiter.allow() is False  # burst spent
    clock.now += 5.0
    assert limiter.allow() is True  # exactly one token per 5 seconds
    assert limiter.allow() is False


def test_fake_server_429s_when_the_bucket_is_empty(fake_ntfy):
    fake_ntfy.limiter.tokens = 0.5  # not enough for the next request
    try:
        urllib.request.urlopen(fake_ntfy.url + "/tug-reply/json?poll=1", timeout=5)
        status = 200
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 429
    assert fake_ntfy.rate_limited == 1


# the daemon subscribes instead of polling


def test_stream_replies_opens_a_subscribe_not_a_poll(fake_ntfy, tmp_path):
    fake_ntfy.queue_reply("looks good", msg_id="r1", t=future())
    daemon = make_daemon(fake_ntfy, tmp_path)

    assert daemon.stream_replies() == 1

    paths = [r["path"] for r in fake_ntfy.gets()]
    assert len(paths) == 1
    assert paths[0].startswith("/tug-reply/json?since=")
    assert "poll=1" not in paths[0]


def test_stream_ignores_open_and_keepalive_events(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    with fake_ntfy.lock:
        fake_ntfy.replies.append({"id": "keep1", "time": future(), "event": "open"})
        fake_ntfy.replies.append(
            {"id": "keep2", "time": future(), "event": "keepalive"}
        )
        fake_ntfy.replies.append(
            {
                "id": "r1",
                "time": future(),
                "event": "message",
                "topic": "tug-reply",
                "message": "real one",
            }
        )
        fake_ntfy.cond.notify_all()

    assert daemon.stream_replies() == 1
    assert [e["id"] for e in read_replies(tmp_path)] == ["r1"]


# the success criteria: zero 429s over a simulated 10 minutes


def test_zero_429_over_ten_minutes_with_no_traffic(fake_ntfy, tmp_path):
    clock = FakeClock()
    fake_ntfy.clock = clock
    fake_ntfy.limiter.clock = clock
    daemon = make_daemon(fake_ntfy, tmp_path)

    start = clock.now
    while clock.now < start + 600:
        daemon.stream_replies()
        clock.now += 120.0  # one full read-timeout hold per request

    assert fake_ntfy.rate_limited == 0
    gets = fake_ntfy.gets()
    assert len(gets) <= 6  # about one request per two minutes
    assert all("poll=1" not in r["path"] for r in gets)


def test_zero_429_over_ten_minutes_at_one_reply_every_6s(fake_ntfy, tmp_path):
    clock = FakeClock()
    fake_ntfy.clock = clock
    fake_ntfy.limiter.clock = clock
    daemon = make_daemon(fake_ntfy, tmp_path)

    captured = 0
    seq = 0
    start = clock.now
    while clock.now < start + 600:
        for _ in range(20):  # 120 second window at one reply per 6 seconds
            seq += 1
            fake_ntfy.queue_reply("m%d" % seq, msg_id="r%d" % seq, t=future())
        captured += daemon.stream_replies()
        clock.now += 120.0

    assert fake_ntfy.rate_limited == 0
    assert captured == 100  # every message rode a held stream
    assert len(fake_ntfy.gets()) <= 6
    entries = read_replies(tmp_path)
    assert len(entries) == 100
    assert entries[0]["id"] == "r1"
    assert entries[-1]["id"] == "r100"


# reconnect behavior


def test_stream_reconnects_with_the_cursor_on_read_timeout(fake_ntfy, tmp_path):
    fake_ntfy.stream_mode = "hold"  # never closes on its own
    fake_ntfy.queue_reply("one", msg_id="r1", t=future())
    daemon = make_daemon(fake_ntfy, tmp_path, stream_timeout=0.3)

    assert daemon.stream_replies() == 1  # held until the read timeout

    time.sleep(0.15)  # let the stale handler reap the closed connection
    fake_ntfy.queue_reply("two", msg_id="r2", t=future())
    assert daemon.stream_replies() == 1  # reconnect picks it up

    paths = [r["path"] for r in fake_ntfy.gets()]
    assert "since=r1" in paths[1]
    assert [e["id"] for e in read_replies(tmp_path)] == ["r1", "r2"]


def test_stream_captures_a_line_arriving_mid_hold(fake_ntfy, tmp_path):
    fake_ntfy.stream_mode = "hold"
    fake_ntfy.stream_close_after = 1
    daemon = make_daemon(fake_ntfy, tmp_path, stream_timeout=5.0)

    def late_reply():
        time.sleep(0.15)
        fake_ntfy.queue_reply("late", msg_id="r9", t=future())

    threading.Thread(target=late_reply).start()

    assert daemon.stream_replies() == 1
    entries = read_replies(tmp_path)
    assert entries[-1]["body"] == "late"


def test_stream_429_is_an_error_not_a_tight_loop(fake_ntfy, tmp_path, monkeypatch):
    daemon = make_daemon(fake_ntfy, tmp_path)
    fake_ntfy.limiter.tokens = 0.0  # the very next request 429s
    lines = []
    monkeypatch.setattr(daemon, "_log_fn", lines.append)

    assert daemon.stream_replies() == 0
    assert daemon.stream_status == "error"
    assert any("429" in line for line in lines)
    assert fake_ntfy.rate_limited == 1


# the poll fallback only exists on the error path, floored at 6 seconds


def test_reply_requests_keep_a_six_second_floor_when_stream_errors(
    fake_ntfy, tmp_path
):
    fake_ntfy.fail_gets = 999  # every stream open fails
    daemon = make_daemon(fake_ntfy, tmp_path, poll_floor=6.0)
    clock = FakeClock(500.0)
    sleeps = []

    def bump_sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    daemon.sleep = bump_sleep
    daemon._now = clock

    daemon._replies_cycle()
    daemon._replies_cycle()
    daemon._replies_cycle()

    assert sleeps == [6.0, 6.0, 6.0, 6.0, 6.0]
    # each cycle: one failed stream open plus one fallback poll, never
    # closer than 6 seconds to the previous reply request
    gets = fake_ntfy.gets()
    assert len(gets) == 6
    assert sum(1 for r in gets if "poll=1" in r["path"]) == 3
    assert fake_ntfy.rate_limited == 0


def test_serve_holds_one_stream_per_flush_cycle(fake_ntfy, tmp_path):
    fake_ntfy.stream_grace = 0.02
    daemon = make_daemon(fake_ntfy, tmp_path, poll_floor=6.0)
    clock = FakeClock(500.0)

    def bump_sleep(seconds):
        clock.now += seconds

    daemon.sleep = bump_sleep
    daemon._now = clock
    calls = []
    original = daemon.flush

    def counting():
        calls.append(1)
        if len(calls) >= 3:
            daemon.request_stop()
        return original()

    daemon.flush = counting
    daemon.serve()

    assert len(calls) == 3
    gets = fake_ntfy.gets()
    assert len(gets) == 2  # one held stream per completed cycle
    assert all("poll=1" not in r["path"] for r in gets)
    assert fake_ntfy.rate_limited == 0


# cursor bootstrap: replies published before the first capture

# The live bug: with no cursor file yet, every request used
# since=<int(now)>, so a reply landed only when a request started in
# the same integer second (5 of 5 pre-cursor test replies missed on
# the operator machine). A fresh daemon must bootstrap from since=all
# and record the first seen id.


def test_first_stream_bootstraps_from_since_all(fake_ntfy, tmp_path):
    fake_ntfy.queue_reply("early", msg_id="r0", t=int(time.time()) - 3600)
    daemon = make_daemon(fake_ntfy, tmp_path)  # no cursor file yet

    assert daemon.stream_replies() == 1

    first_path = fake_ntfy.gets()[0]["path"]
    assert "since=all" in first_path


def test_precursor_replies_captured_on_first_stream_open(fake_ntfy, tmp_path):
    for i in range(3):
        fake_ntfy.queue_reply(
            "pre%d" % i, msg_id="p%d" % i, t=int(time.time()) - 60
        )
    daemon = make_daemon(fake_ntfy, tmp_path)

    assert daemon.stream_replies() == 3

    entries = read_replies(tmp_path)
    assert [e["id"] for e in entries] == ["p0", "p1", "p2"]
    with open(tmp_path / "state" / "reply_cursor.json", encoding="utf-8") as fh:
        assert json.load(fh) == {"last_id": "p2"}


def test_since_all_refused_falls_back_to_a_one_hour_window(fake_ntfy, tmp_path):
    fake_ntfy.reject_since_all = 1  # one 413 for since=all, then behave
    fake_ntfy.queue_reply("recent", msg_id="r1", t=int(time.time()))
    daemon = make_daemon(fake_ntfy, tmp_path)

    assert daemon.stream_replies() == 1

    paths = [r["path"] for r in fake_ntfy.gets()]
    assert "since=all" in paths[0]
    fallback_since = paths[1].split("since=")[1]
    expected = int(time.time()) - 3600
    assert abs(int(fallback_since) - expected) <= 2
    assert [e["id"] for e in read_replies(tmp_path)] == ["r1"]


def test_first_poll_bootstraps_from_since_all_too(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)

    daemon.poll_replies()

    assert "since=all" in fake_ntfy.gets()[0]["path"]
