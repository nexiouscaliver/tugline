"""Tests for the spool writer (tugline.core.spool), including write speed."""

import json
import time
from datetime import datetime, timedelta, timezone

from tugline.core.spool import (
    append,
    mark_delivered,
    records,
    today_filename,
    undelivered,
)

WHEN = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)


def write(root, **kw):
    base = dict(
        event="notify",
        session_id="s1",
        topic="tug",
        priority="default",
        title="hi",
        body="hello there",
        now=WHEN,
    )
    base.update(kw)
    return append(str(root), **base)


def test_append_creates_day_file_named_for_utc_date(tmp_path):
    write(tmp_path)
    expected = tmp_path / "2026-10-13.jsonl"
    assert expected.exists()
    assert today_filename(WHEN) == "2026-10-13.jsonl"


def test_record_has_contract_fields(tmp_path):
    rec = write(tmp_path, actions=[{"action": "view", "label": "Open"}])
    for key in (
        "id",
        "ts",
        "event",
        "session_id",
        "topic",
        "priority",
        "title",
        "body",
        "actions",
        "delivered",
    ):
        assert key in rec, "missing field %s" % key
    assert rec["delivered"] is False
    assert rec["event"] == "notify"
    assert rec["actions"] == [{"action": "view", "label": "Open"}]
    line = (tmp_path / "2026-10-13.jsonl").read_text().splitlines()[0]
    assert json.loads(line) == rec


def test_actions_omitted_when_not_given(tmp_path):
    rec = write(tmp_path)
    assert "actions" not in rec


# Session enrichment extras (adapter, cwd, project)


def test_extra_fields_merge_into_the_record(tmp_path):
    rec = write(
        tmp_path,
        extra={"adapter": "zcode", "cwd": "/w/proj", "project": "proj"},
    )
    assert rec["adapter"] == "zcode"
    assert rec["cwd"] == "/w/proj"
    assert rec["project"] == "proj"


def test_extra_values_are_coerced_to_strings(tmp_path):
    rec = write(tmp_path, extra={"count": 5, "flag": True})
    assert rec["count"] == "5"
    assert rec["flag"] == "True"


def test_extra_lands_after_fixed_fields_and_before_delivered(tmp_path):
    rec = write(tmp_path, extra={"adapter": "zcode"})
    keys = list(rec)
    assert keys.index("adapter") > keys.index("body")
    assert keys.index("delivered") == len(keys) - 1
    line = (tmp_path / "2026-10-13.jsonl").read_text().splitlines()[0]
    assert json.loads(line) == rec


def test_extra_fields_round_trip_through_the_day_file(tmp_path):
    write(tmp_path, extra={"adapter": "claude", "project": "tugline"})
    rec = records(str(tmp_path))[0]
    assert rec["adapter"] == "claude"
    assert rec["project"] == "tugline"


def test_record_without_extra_is_byte_identical_to_before(tmp_path):
    """Exact line pin: same keys, same order, same separators, no extras."""
    rec = write(tmp_path)
    line = (tmp_path / "2026-10-13.jsonl").read_text(encoding="utf-8")
    assert line == (
        '{"id":"%s","ts":"2026-10-13T12:00:00+00:00","event":"notify",'
        '"session_id":"s1","topic":"tug","priority":"default",'
        '"title":"hi","body":"hello there","delivered":false}\n' % rec["id"]
    )


def test_ids_are_unique(tmp_path):
    ids = {write(tmp_path)["id"] for _ in range(10)}
    assert len(ids) == 10


def test_rotation_one_file_per_day(tmp_path):
    write(tmp_path, now=WHEN)
    write(tmp_path, now=WHEN + timedelta(days=1))
    write(tmp_path, now=WHEN + timedelta(days=1))
    files = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert files == ["2026-10-13.jsonl", "2026-10-14.jsonl"]
    counts = {
        name: len((tmp_path / name).read_text().splitlines()) for name in files
    }
    assert counts == {"2026-10-13.jsonl": 1, "2026-10-14.jsonl": 2}


def test_records_iterates_chronologically_across_days(tmp_path):
    write(tmp_path, now=WHEN, title="day1")
    write(tmp_path, now=WHEN + timedelta(days=1), title="day2a")
    write(tmp_path, now=WHEN + timedelta(days=1), title="day2b")
    titles = [r["title"] for r in records(str(tmp_path))]
    assert titles == ["day1", "day2a", "day2b"]


def test_mark_delivered_removes_from_undelivered(tmp_path):
    r1 = write(tmp_path, title="one")
    write(tmp_path, title="two")
    pending = [r["title"] for r in undelivered(str(tmp_path))]
    assert pending == ["one", "two"]
    mark_delivered(str(tmp_path), r1["id"])
    pending = [r["title"] for r in undelivered(str(tmp_path))]
    assert pending == ["two"]


def test_mark_delivered_is_idempotent(tmp_path):
    rec = write(tmp_path)
    mark_delivered(str(tmp_path), rec["id"])
    mark_delivered(str(tmp_path), rec["id"])
    assert list(undelivered(str(tmp_path))) == []


def test_bad_lines_skipped_and_reported(tmp_path):
    r1 = write(tmp_path, title="good")
    day = tmp_path / "2026-10-13.jsonl"
    text = day.read_text().splitlines()
    day.write_text(text[0] + "\n{broken json\n")
    errors = []
    got = list(undelivered(str(tmp_path), on_error=errors.append))
    assert [r["title"] for r in got] == ["good"]
    assert r1["id"]
    assert len(errors) == 1


def test_event_files_stay_append_only_after_delivery(tmp_path):
    rec = write(tmp_path)
    mark_delivered(str(tmp_path), rec["id"])
    raw = (tmp_path / "2026-10-13.jsonl").read_text().splitlines()
    assert json.loads(raw[0])["delivered"] is False


def test_append_write_path_under_100ms(tmp_path):
    """Wall-clock budget for one spool append is 100 ms, with margin."""
    root = str(tmp_path)
    append(root, event="notify", session_id="s1", topic="tug",
           priority="default", title="warm", body="warmup")
    slowest = 0.0
    for i in range(25):
        t0 = time.perf_counter()
        append(root, event="notify", session_id="s1", topic="tug",
               priority="default", title="t%d" % i, body="speed check")
        elapsed = time.perf_counter() - t0
        slowest = max(slowest, elapsed)
    print("SPOOL-SPEED: slowest single append of 25: %.3f ms" % (slowest * 1000))
    assert slowest < 0.100, "spool append took %.1f ms" % (slowest * 1000)
