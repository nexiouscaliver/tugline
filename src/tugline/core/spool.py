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
    when = now or datetime.now(timezone.utc)
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
    raise NotImplementedError("spool append not implemented yet")


def records(root, on_error=None):
    raise NotImplementedError("records not implemented yet")


def undelivered(root, on_error=None):
    raise NotImplementedError("undelivered not implemented yet")


def mark_delivered(root, record_id) -> None:
    raise NotImplementedError("mark_delivered not implemented yet")
