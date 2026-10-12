"""Append-only notification spool.

Layout under the spool root (~/.tugline/spool by default):

- YYYY-MM-DD.jsonl, one file per UTC day. Each line is one JSON
  record: {"id", "ts", "event", "session_id", "topic", "priority",
  "title", "body", "actions"?, "delivered": false}. Day files are
  append-only and never rewritten; the stored "delivered" flag stays
  false forever and delivery state lives in delivered.json.
- delivered.json: {"ids": ["..."]} list of record ids the daemon has
  finished with (published, or consumed as state-only). Rewritten
  atomically with fsync on every mark.

The append path is deliberately light: serialize one dict, open the
day file in append mode, write the line, flush. No per-record fsync.
The write path has a 100 ms budget, enforced by a test.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

DELIVERED_FILE = "delivered.json"


def today_filename(now: datetime | None = None) -> str:
    when = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    return when.strftime("%Y-%m-%d") + ".jsonl"


def append(
    root,
    *,
    event,
    session_id,
    topic,
    priority,
    title,
    body,
    actions=None,
    now=None,
) -> dict:
    """Append one record to today's file and return it."""
    root = str(root)
    when = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)

    record = {
        "id": uuid.uuid4().hex[:16],
        "ts": when.isoformat(),
        "event": event,
        "session_id": session_id,
        "topic": topic,
        "priority": priority,
        "title": title,
        "body": body,
    }
    if actions:
        record["actions"] = actions
    record["delivered"] = False

    os.makedirs(root, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(os.path.join(root, today_filename(when)), "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
    return record


def _day_files(root):
    try:
        names = sorted(
            name for name in os.listdir(root) if name.endswith(".jsonl")
        )
    except FileNotFoundError:
        return
    for name in names:
        yield os.path.join(root, name)


def records(root, on_error=None):
    """Yield every parseable record, oldest day first."""
    for path in _day_files(root):
        try:
            with open(path, encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        if on_error is not None:
                            on_error("%s:%d" % (path, line_no))
                        continue
                    if isinstance(rec, dict):
                        yield rec
        except OSError as exc:
            if on_error is not None:
                on_error("%s: %s" % (path, exc))


def delivered_ids(root) -> set:
    path = os.path.join(str(root), DELIVERED_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    ids = data.get("ids", []) if isinstance(data, dict) else []
    return {str(i) for i in ids}


def undelivered(root, on_error=None):
    """Yield records not yet marked delivered, oldest first."""
    done = delivered_ids(root)
    for rec in records(root, on_error=on_error):
        if rec.get("id") not in done:
            yield rec


def mark_delivered(root, record_id) -> None:
    root = str(root)
    os.makedirs(root, exist_ok=True)
    ids = delivered_ids(root)
    ids.add(str(record_id))
    path = os.path.join(root, DELIVERED_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"ids": sorted(ids)}, fh)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
