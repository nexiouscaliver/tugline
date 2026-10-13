"""The live flush-path failure (W5-L11, 2026-10-13) and its pins.

Live reality that motivated this file: the daemon process started at
07:50 IST with an empty pending spool, then held its reply stream open
(ntfy sends keepalive events every 45 seconds, and every arriving line
resets the read timeout, so the 120-second stream timeout never fired).
The serve loop never completed another iteration, so 120 records queued
over the following twelve hours -- notify, sessionstart, stop,
permissionrequest -- were never flushed: zero publishes, zero marks in
delivered.json, and daemon.err.log showed nothing past "daemon
started". The operator's repro (tug notify --topic tug/tug-reply/
tug-warn) returned spool ids and delivered nothing, while "tug test"
(the direct publish path) buzzed the phone.

The fixture mirrors the live spool shape: event records carry topic
KEYS ("main", "warn"), direct tug notify records carry LITERAL topic
names ("tug", "tug-warn"), and a stale delivered.json sidecar holds
ids from earlier days.
"""

import json
import threading
import time

from tugline.core import config as cfg
from tugline.core.daemon import Daemon
from tugline.core.spool import append, delivered_ids, undelivered


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
    kw.setdefault("stream_timeout", 6.0)
    kw.setdefault("backoff_base", 0.01)
    kw.setdefault("cycle_idle", 0.0)
    return Daemon(
        make_config(fake_ntfy.url),
        spool_root=str(tmp_path / "spool"),
        replies_path=str(tmp_path / "replies.jsonl"),
        state_root=str(tmp_path / "state"),
        **kw,
    )


def write_stale_sidecar(root):
    """The live delivered.json: frozen at W3-era ids, none of today's."""
    import os

    os.makedirs(str(root), exist_ok=True)
    with open(str(root) + "/delivered.json", "w", encoding="utf-8") as fh:
        json.dump({"ids": ["04c13ea10e5544d9"]}, fh)


def queue_live_shape_records(root):
    """Queue records in both topic spellings the system produces:
    event-style key topics and tug-notify literal topics."""
    queued = {}
    for title, topic in (
        ("key topic from an event record", "main"),
        ("literal topic from tug notify", "tug"),
        ("literal warn topic from tug notify", "tug-warn"),
    ):
        record = append(
            str(root),
            event="notify",
            session_id="manual",
            topic=topic,
            priority="default",
            title=title,
            body="queued mid-run, like the operator repro",
        )
        queued[record["id"]] = topic
    return queued


def start_keepalive_pump(fake_ntfy, interval):
    """Feed keepalive events like the real server (45s live; fast here).

    Each line resets the daemon's per-read timeout clock, which is what
    starved the live flush path: the read never timed out, so the serve
    loop never came back around to flush.
    """
    stop = threading.Event()

    def pump():
        while not stop.wait(interval):
            with fake_ntfy.cond:
                fake_ntfy.replies.append(
                    {"id": "k%d" % time.time(), "time": int(time.time()),
                     "event": "keepalive"}
                )
                fake_ntfy.cond.notify_all()

    threading.Thread(target=pump, daemon=True).start()
    return stop


def test_records_queued_while_the_stream_is_held_still_deliver(
    fake_ntfy, tmp_path
):
    """The operator repro end to end: daemon running, reply stream held
    open by keepalives, notifications queued mid-run. They must deliver
    through the serve loop itself, get marked in the sidecar, and leave
    tug-status-equivalent counting at zero."""
    fake_ntfy.stream_mode = "hold"  # the server never closes the stream
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = str(tmp_path / "spool")
    write_stale_sidecar(root)
    keepalives = start_keepalive_pump(fake_ntfy, 0.5)

    serve_thread = threading.Thread(target=daemon.serve, daemon=True)
    serve_thread.start()
    time.sleep(1.0)  # daemon started; the stream is now held open

    queued = queue_live_shape_records(root)

    deadline = time.time() + 12  # the flush bound is 10 seconds
    while time.time() < deadline:
        if len(fake_ntfy.posts()) == len(queued):
            break
        time.sleep(0.1)

    daemon.request_stop()
    serve_thread.join(timeout=15)
    keepalives.set()

    posts = fake_ntfy.posts()
    assert len(posts) == len(queued), (
        "records queued while the stream is held were not flushed "
        "(live bug): posts=%r" % [p["path"] for p in posts]
    )
    assert sorted(p["path"] for p in posts) == ["/tug", "/tug", "/tug-warn"]
    marked = delivered_ids(root)
    assert all(rid in marked for rid in queued)
    assert list(undelivered(root)) == []  # what tug status counts


def test_held_stream_pass_is_bounded_even_under_keepalive_firehose(
    fake_ntfy, tmp_path
):
    """The pass itself must end: keepalives arriving faster than the
    read timeout reset it line by line, which on main held the stream
    open forever. stream_replies must return within the bound even so."""
    fake_ntfy.stream_mode = "hold"
    daemon = make_daemon(
        fake_ntfy, tmp_path, stream_timeout=6.0, flush_interval=1.0
    )
    keepalives = start_keepalive_pump(fake_ntfy, 0.2)

    returned = []
    thread = threading.Thread(
        target=lambda: returned.append(daemon.stream_replies()), daemon=True
    )
    thread.start()
    thread.join(timeout=5)

    keepalives.set()
    assert thread.is_alive() is False, "stream pass never ended (live bug)"
    assert returned == [0]  # keepalives are not messages
    # the pass returned while the stream was still open and healthy
    assert daemon.stream_status == "held"


# -- the summary line the live failure hid behind --


def capture_log(daemon, monkeypatch):
    lines = []
    monkeypatch.setattr(daemon, "_log_fn", lines.append)
    return lines


def test_flush_pass_logs_one_summary_line(fake_ntfy, tmp_path, monkeypatch):
    """Every flush pass emits exactly one summary line with truthful
    counts: this is what was missing while 120 records sat silent."""
    daemon = make_daemon(fake_ntfy, tmp_path)
    lines = capture_log(daemon, monkeypatch)
    root = str(tmp_path / "spool")
    write_stale_sidecar(root)
    queue_live_shape_records(root)  # 3 notify records, all published
    append(  # a prompt record: consumed as state, no publish
        root,
        event="userpromptsubmit",
        session_id="s1",
        topic="main",
        priority="default",
        title="prompt",
        body="",
    )

    assert daemon.flush() == 4

    summary = [line for line in lines if line.startswith("flush pass:")]
    assert len(summary) == 1
    assert summary[0] == (
        "flush pass: scanned=4 handled=4 published=3 marked=4 suppressed=1"
    )


def test_empty_flush_pass_still_logs_the_summary_line(
    fake_ntfy, tmp_path, monkeypatch
):
    """The live 07:50 start: an empty pending spool logged nothing at
    all, so a later starvation was invisible. An empty pass must still
    say so."""
    daemon = make_daemon(fake_ntfy, tmp_path)
    lines = capture_log(daemon, monkeypatch)
    root = str(tmp_path / "spool")
    write_stale_sidecar(root)

    assert daemon.flush() == 0

    summary = [line for line in lines if line.startswith("flush pass:")]
    assert summary == ["flush pass: scanned=0 handled=0 published=0 "
                       "marked=0 suppressed=0"]


def test_suppressed_records_are_marked_once_and_never_republished(
    fake_ntfy, tmp_path
):
    """Fix contract: a policy suppression is final for that record.
    With flush now running every few seconds, suppressed records must
    not be re-evaluated (and could never be re-published) forever —
    one pass decides, marks in the sidecar, and every later pass skips
    them: stale records never re-publish after a policy decision."""
    daemon = make_daemon(fake_ntfy, tmp_path)
    root = str(tmp_path / "spool")
    write_stale_sidecar(root)
    suppressed_id = append(
        root,
        event="sessionstart",  # not a resume: policy suppresses it
        session_id="s1",
        topic="main",
        priority="default",
        title="session started",
        body="",
    )["id"]
    stopped_id = append(
        root,
        event="stop",  # short turn, not armed: policy suppresses it
        session_id="s1",
        topic="main",
        priority="default",
        title="session stopped",
        body="",
    )["id"]

    assert daemon.flush() == 2
    assert fake_ntfy.posts() == []
    marked = delivered_ids(root)
    assert suppressed_id in marked and stopped_id in marked

    # repeated passes over the same spool: nothing re-evaluated, nothing
    # published, the sidecar does not churn
    for _ in range(3):
        assert daemon.flush() == 0
    assert fake_ntfy.posts() == []
    assert len(delivered_ids(root)) == 3  # stale id + the two records
    assert list(undelivered(root)) == []


def test_serve_flush_cadence_stays_bounded_while_the_stream_is_held(
    fake_ntfy, tmp_path
):
    """The stream-starvation receipt: while a healthy stream is held
    open by keepalives, the serve loop still comes back to flush at
    least every 10 seconds (daemon defaults)."""
    fake_ntfy.stream_mode = "hold"
    daemon = make_daemon(fake_ntfy, tmp_path)  # flush_interval=5 default
    stamps = []
    original = daemon.flush

    def stamping():
        stamps.append(time.monotonic())
        if len(stamps) >= 3:
            daemon.request_stop()
        return original()

    daemon.flush = stamping
    keepalives = start_keepalive_pump(fake_ntfy, 0.5)
    serve_thread = threading.Thread(target=daemon.serve, daemon=True)
    serve_thread.start()
    serve_thread.join(timeout=30)
    keepalives.set()

    assert not serve_thread.is_alive(), "serve never came back to flush"
    assert len(stamps) >= 3
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert max(gaps) <= 10.0, "flush starved behind the held stream: %r" % gaps
