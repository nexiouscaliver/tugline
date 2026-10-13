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
