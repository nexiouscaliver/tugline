"""Sqlite session store: routing truth for the console.

The spool is an append-only log shaped for the daemon, not for
questions like "what is this session waiting on". The store folds
that log into tables a console can query in one place:

- sessions: one row per session id seen in the spool, with the
  earliest and latest event timestamps, the newest event name, a
  derived state (waiting after a permissionrequest, done after a
  stop, running otherwise), and an operator-settable display label
  (NULL when unset; ingest never touches it).
- events: every spool record, one row per record id. spool_id is
  UNIQUE, which with a SELECT check is what makes ingest idempotent.
  Rows carry the adapter/project/cwd session fields when the record
  has them, "" otherwise.
- threads: one row per approval nonce parsed out of permissionrequest
  action urls, carried from open to answered or expired by the files
  under the approvals root.
- replies: rows the console writes back toward sessions and threads;
  delivering them is a later concern, so delivered stays 0.

Thread safety: one connection shared by the whole process
(check_same_thread=False), WAL journal so readers do not block the
writer, and every write path serialized through one threading.Lock.
The intended shape is a single process with multiple reader threads
and one writer at a time; reads never take the lock. Transactions are
managed explicitly (isolation_level=None) so each write path is one
BEGIN IMMEDIATE ... COMMIT that rolls back whole on error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from tugline.core import spool

SCHEMA_VERSION_KEY = "schema_version"

MIGRATIONS = [
    (
        1,
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            adapter TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            first_seen TEXT,
            last_seen TEXT,
            state TEXT NOT NULL DEFAULT 'running',
            last_event TEXT,
            last_event_ts TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spool_id TEXT UNIQUE,
            ts TEXT,
            session_id TEXT NOT NULL DEFAULT '',
            adapter TEXT NOT NULL DEFAULT '',
            event TEXT,
            topic TEXT,
            priority TEXT,
            title TEXT,
            body TEXT,
            delivered INTEGER NOT NULL DEFAULT 0,
            delivered_ts TEXT
        );
        CREATE TABLE threads (
            nonce TEXT PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            kind TEXT,
            title TEXT,
            body TEXT,
            created_ts TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            answer TEXT,
            answer_ts TEXT
        );
        CREATE TABLE replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            thread_nonce TEXT,
            session_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'console',
            body TEXT,
            delivered INTEGER NOT NULL DEFAULT 0
        );
        """,
    ),
    (
        2,
        """
        ALTER TABLE events ADD COLUMN project TEXT NOT NULL DEFAULT '';
        ALTER TABLE events ADD COLUMN cwd TEXT NOT NULL DEFAULT '';
        """,
    ),
    (
        3,
        """
        ALTER TABLE sessions ADD COLUMN label TEXT;
        """,
    ),
]


def connect(path) -> Store:
    """Open (creating if needed) the store at path."""
    return Store(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _carried(rec, key) -> str:
    """One enrichment field as a string; "" when the record omits it."""
    return str(rec.get(key) or "") if key in rec else ""


def _statements(sql):
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if stmt:
            yield stmt


def _session_state(event) -> str:
    """Map the newest event name onto a session state.

    Names are matched case-insensitively, the same leniency
    core.policy applies to hook payloads.
    """
    name = str(event or "").strip().lower()
    if name == "permissionrequest":
        return "waiting"
    if name == "stop":
        return "done"
    return "running"


def _approval_nonce(record) -> str:
    """Nonce from the first action url shaped .../approvals/<nonce>.

    The query (and anything after a further slash) is stripped; ""
    when the record carries no such url.
    """
    actions = record.get("actions")
    if not isinstance(actions, list):
        return ""
    for action in actions:
        if not isinstance(action, dict):
            continue
        url = action.get("url")
        if not isinstance(url, str) or "/approvals/" not in url:
            continue
        tail = url.split("/approvals/", 1)[1]
        nonce = tail.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0]
        nonce = nonce.strip()
        if nonce:
            return nonce
    return ""


def _read_decision(path):
    """{"decision", "ts"} from a receiver decision file, else None."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision") or "").strip()
    if not decision:
        return None
    return {"decision": decision, "ts": str(data.get("ts") or "")}


class Store:
    def __init__(self, path):
        path = os.fspath(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # -- schema --

    def _migrate(self):
        """Apply pending migrations in order, one transaction each."""
        with self._lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                "key TEXT PRIMARY KEY, value TEXT)"
            )
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (SCHEMA_VERSION_KEY,),
            ).fetchone()
            current = int(row[0]) if row is not None else 0
            for version, sql in MIGRATIONS:
                if version <= current:
                    continue
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _statements(sql):
                        self.conn.execute(stmt)
                    self.conn.execute(
                        "INSERT OR REPLACE INTO meta (key, value)"
                        " VALUES (?, ?)",
                        (SCHEMA_VERSION_KEY, str(version)),
                    )
                    self.conn.execute("COMMIT")
                except BaseException:
                    self.conn.execute("ROLLBACK")
                    raise

    # -- ingest --

    def ingest_spool(self, spool_root) -> dict:
        """Fold spool records into events, sessions, and threads.

        Walks day files oldest first (spool.records, read-only) and
        skips ids already stored, so re-running over the same spool
        returns zeros and changes nothing. Returns counts of new rows
        only: {"events", "sessions", "threads"}.
        """
        counts = {"events": 0, "sessions": 0, "threads": 0}
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                seen_here = set()
                for rec in spool.records(str(spool_root)):
                    rid = str(rec.get("id") or "")
                    if not rid or rid in seen_here:
                        continue
                    row = self.conn.execute(
                        "SELECT 1 FROM events WHERE spool_id = ?", (rid,)
                    ).fetchone()
                    if row is not None:
                        continue
                    seen_here.add(rid)
                    self._insert_event(rec, rid)
                    counts["events"] += 1
                    ts = str(rec.get("ts") or "")
                    session_id = str(rec.get("session_id") or "")
                    event = str(rec.get("event") or "")
                    if session_id and self._upsert_session(
                        session_id, event, ts, rec
                    ):
                        counts["sessions"] += 1
                    nonce = _approval_nonce(rec)
                    if (
                        event.strip().lower() == "permissionrequest"
                        and nonce
                        and self._insert_thread(nonce, session_id, rec, ts)
                    ):
                        counts["threads"] += 1
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return counts

    def _insert_event(self, rec, rid):
        self.conn.execute(
            "INSERT INTO events (spool_id, ts, session_id, adapter, event,"
            " topic, priority, title, body, project, cwd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rid,
                str(rec.get("ts") or ""),
                str(rec.get("session_id") or ""),
                _carried(rec, "adapter"),
                str(rec.get("event") or ""),
                str(rec.get("topic") or ""),
                str(rec.get("priority") or ""),
                str(rec.get("title") or ""),
                str(rec.get("body") or ""),
                _carried(rec, "project"),
                _carried(rec, "cwd"),
            ),
        )

    def _upsert_session(self, session_id, event, ts, rec) -> bool:
        """Insert or widen the session row; True when it is new.

        The spool is walked oldest first, so the record in hand is
        usually the newest one yet; the timestamp comparisons keep the
        result right even when it is not. The enrichment fields
        (adapter/project/cwd) are upserted last writer wins, and only
        from records that carry them, so a plain record from the
        approve path never wipes what an enriched hook record set.
        """
        state = _session_state(event)
        carried = {
            key: str(rec.get(key) or "")
            for key in ("adapter", "project", "cwd")
            if key in rec
        }
        row = self.conn.execute(
            "SELECT first_seen, last_seen, last_event_ts FROM sessions"
            " WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO sessions (session_id, adapter, project, cwd,"
                " first_seen, last_seen, state, last_event, last_event_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    carried.get("adapter", ""),
                    carried.get("project", ""),
                    carried.get("cwd", ""),
                    ts,
                    ts,
                    state,
                    event,
                    ts,
                ),
            )
            return True
        first_seen = row["first_seen"] or ts
        if ts < first_seen:
            first_seen = ts
        last_seen = row["last_seen"] or ts
        if ts > last_seen:
            last_seen = ts
        extra_cols = ""
        extra_vals = []
        for key in ("adapter", "project", "cwd"):
            if key in carried:
                extra_cols += ", %s = ?" % key
                extra_vals.append(carried[key])
        if ts >= (row["last_event_ts"] or ""):
            self.conn.execute(
                "UPDATE sessions SET first_seen = ?, last_seen = ?,"
                " state = ?, last_event = ?, last_event_ts = ?" + extra_cols
                + " WHERE session_id = ?",
                [first_seen, last_seen, state, event, ts] + extra_vals
                + [session_id],
            )
        else:
            self.conn.execute(
                "UPDATE sessions SET first_seen = ?, last_seen = ?"
                + extra_cols + " WHERE session_id = ?",
                [first_seen, last_seen] + extra_vals + [session_id],
            )
        return False

    def _insert_thread(self, nonce, session_id, rec, ts) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM threads WHERE nonce = ?", (nonce,)
        ).fetchone()
        if row is not None:
            return False
        self.conn.execute(
            "INSERT INTO threads (nonce, session_id, kind, title, body,"
            " created_ts) VALUES (?, ?, 'approve', ?, ?, ?)",
            (
                nonce,
                session_id,
                str(rec.get("title") or ""),
                str(rec.get("body") or ""),
                ts,
            ),
        )
        return True

    # -- refresh --

    def refresh_delivery(self, spool_root) -> int:
        """Flip events to delivered per delivered.json.

        Rows already at 1 are left alone. Returns how many rows
        flipped.
        """
        ids = spool.delivered_ids(str(spool_root))
        if not ids:
            return 0
        now = _now_iso()
        flipped = 0
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for rid in sorted(ids):
                    cur = self.conn.execute(
                        "UPDATE events SET delivered = 1, delivered_ts = ?"
                        " WHERE spool_id = ? AND delivered = 0",
                        (now, rid),
                    )
                    flipped += cur.rowcount
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return flipped

    def refresh_threads(self, approvals_root) -> dict:
        """Reconcile threads against the approvals directory.

        For every thread not yet answered: a parseable decision file
        marks it answered (answer and answer_ts from the file), a
        pending marker keeps it open, and neither file means the ask
        went away unanswered (expired). Returns counts of rows whose
        status changed, so a refresh that changes nothing returns
        zeros.
        """
        root = str(approvals_root)
        counts = {"answered": 0, "open": 0, "expired": 0}
        rows = self.conn.execute(
            "SELECT nonce, status FROM threads WHERE status != 'answered'"
        ).fetchall()
        updates = []
        for row in rows:
            nonce = row["nonce"]
            decision = _read_decision(os.path.join(root, nonce + ".json"))
            if decision is not None:
                updates.append(
                    ("answered", nonce, decision["decision"],
                     decision["ts"])
                )
                continue
            pending = os.path.exists(
                os.path.join(root, nonce + ".pending")
            )
            target = "open" if pending else "expired"
            if target != row["status"]:
                updates.append((target, nonce, None, None))
        if not updates:
            return counts
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for status, nonce, answer, answer_ts in updates:
                    if status == "answered":
                        self.conn.execute(
                            "UPDATE threads SET status = 'answered',"
                            " answer = ?, answer_ts = ? WHERE nonce = ?",
                            (answer, answer_ts, nonce),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE threads SET status = ? WHERE nonce = ?",
                            (status, nonce),
                        )
                    counts[status] += 1
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return counts

    # -- writes from the console --

    def set_session_label(self, session_id, label) -> dict:
        """Set or clear one session's operator label.

        The label is stored exactly as given: validation and trimming
        belong to the caller (the console API). An empty value clears
        the label (NULL). Raises KeyError for a session id the store
        has never seen. Returns the updated row.
        """
        session_id = str(session_id)
        value = str(label) if label else None
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise KeyError("unknown session: %r" % session_id)
                self.conn.execute(
                    "UPDATE sessions SET label = ? WHERE session_id = ?",
                    (value, session_id),
                )
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return dict(
            self.conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        )

    def add_reply(self, body, thread_nonce=None, session_id=None,
                  source="console") -> dict:
        """Insert one reply aimed at a thread or a session.

        Exactly one of thread_nonce or session_id must be given. A
        thread target must exist and the reply inherits that thread's
        session_id. Returns the inserted row with ts = now.
        """
        thread_nonce = str(thread_nonce) if thread_nonce else None
        session_id = str(session_id) if session_id else None
        if (thread_nonce is None) == (session_id is None):
            raise ValueError(
                "exactly one of thread_nonce or session_id is required"
            )
        if thread_nonce is not None:
            row = self.conn.execute(
                "SELECT session_id FROM threads WHERE nonce = ?",
                (thread_nonce,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown thread nonce: %r" % thread_nonce)
            session_id = row["session_id"]
        ts = _now_iso()
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.conn.execute(
                    "INSERT INTO replies (ts, thread_nonce, session_id,"
                    " source, body) VALUES (?, ?, ?, ?, ?)",
                    (ts, thread_nonce, session_id, str(source),
                     str(body or "")),
                )
                rid = cur.lastrowid
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return dict(
            self.conn.execute(
                "SELECT * FROM replies WHERE id = ?", (rid,)
            ).fetchone()
        )

    def record_answer(self, nonce, decision, answer_ts=None) -> None:
        """Mark a thread answered outside the decision-file flow.

        The console approve endpoint calls this directly; refresh_threads
        reading the decision files is the other path to the same state.
        Raises KeyError for an unknown nonce.
        """
        nonce = str(nonce)
        row = self.conn.execute(
            "SELECT 1 FROM threads WHERE nonce = ?", (nonce,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown thread nonce: %r" % nonce)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "UPDATE threads SET status = 'answered', answer = ?,"
                    " answer_ts = ? WHERE nonce = ?",
                    (str(decision), answer_ts or _now_iso(), nonce),
                )
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    # -- queries --

    def sessions(self) -> list:
        """All sessions, most recently seen first."""
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def events(self, limit=50, session_id=None) -> list:
        """Events newest first; one session when session_id is given.

        Ordered by ts, not by insertion id: spool records can arrive
        with out-of-order timestamps, so id order is not chronological.
        The ISO-8601 strings from datetime.isoformat sort correctly as
        plain text; id DESC tiebreaks identical timestamps.
        """
        if session_id is None:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE session_id = ?"
                " ORDER BY ts DESC, id DESC LIMIT ?",
                (str(session_id), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def threads(self, status=None) -> list:
        """Threads newest first; one status when given."""
        if status is None:
            rows = self.conn.execute(
                "SELECT * FROM threads ORDER BY created_ts DESC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM threads WHERE status = ?"
                " ORDER BY created_ts DESC",
                (str(status),),
            ).fetchall()
        return [dict(row) for row in rows]

    def replies(self, limit=50) -> list:
        """Replies newest first."""
        rows = self.conn.execute(
            "SELECT * FROM replies ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self) -> dict:
        """One read for a console render: sessions, the last 50
        events, threads, the last 20 replies, and headline counts."""
        return {
            "sessions": self.sessions(),
            "events": self.events(limit=50),
            "threads": self.threads(),
            "replies": self.replies(limit=20),
            "counts": {
                "events_total": self._count("SELECT COUNT(*) FROM events"),
                "events_pending": self._count(
                    "SELECT COUNT(*) FROM events WHERE delivered = 0"
                ),
                "threads_open": self._count(
                    "SELECT COUNT(*) FROM threads WHERE status = 'open'"
                ),
            },
        }

    def _count(self, sql) -> int:
        return int(self.conn.execute(sql).fetchone()[0])

    def close(self) -> None:
        self.conn.close()
