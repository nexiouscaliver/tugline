"""Sqlite session store: routing truth for the console.

The spool is an append-only log shaped for the daemon, not for
questions like "what is this session waiting on". The store folds
that log into tables a console can query in one place:

- sessions: one row per session id seen in the spool, with the
  earliest and latest event timestamps, the newest event name, a
  derived state (waiting after a permissionrequest, done after a
  stop, running otherwise), and an operator-settable display label
  (NULL when unset; ingest never touches it).
- tags: many short names per session (schema v4), set by the
  operator through the console or by the agent itself through the
  CLI. A tag is lowercase, trimmed, [a-z0-9-] only, at most 24
  characters, at most 8 per session; (session_id, tag) is the
  primary key. Tags give reply targeting a second name space
  alongside labels: a reply reference resolves as an exact session
  id first, then a label exactly one session carries, then a tag
  exactly one session carries, with ambiguity reported rather than
  guessed.
- events: every spool record, one row per record id. spool_id is
  UNIQUE, which with a SELECT check is what makes ingest idempotent.
  Rows carry the adapter/project/cwd session fields when the record
  has them, "" otherwise.
- threads: one row per approval nonce parsed out of permissionrequest
  action urls, carried from open to answered or expired by the files
  under the approvals root. Ask threads (W9-L21) live in the same
  table with kind "ask": created by the tug ask command with their
  options as JSON in the options column, answered by any of the ask
  paths, expired by the timeout.
- replies: rows the console writes back toward sessions and threads;
  delivering them is a later concern, so delivered stays 0.

Session identity (W9-L21) is assigned on ingest with zero operator
work: the first suitable UserPromptSubmit record auto-labels the
session (label_source "auto"), the session row's creation auto-tags
it with its adapter and project label, and one display rule answers
what to render: the label when set, else a collision-proof unique
suffix of the id (never the leading id characters). An operator
label always wins and pauses auto-labeling for that session,
including clearing it (label_source "operator").

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
import uuid
from datetime import datetime, timezone

from tugline.core import payload, spool

SCHEMA_VERSION_KEY = "schema_version"

TAG_MAX_CHARS = 24
TAG_MAX_PER_SESSION = 8
_TAG_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")

# Auto-label bounds (W9-L21): the first 5-7 words of the prompt,
# sanitized to ASCII, collapsed whitespace, at most 40 characters,
# no trailing punctuation artifacts.
AUTO_LABEL_MAX_WORDS = 7
AUTO_LABEL_MAX_CHARS = 40
_LABEL_TRAILING = ".,;:!?- \t"

# The display rule (one policy everywhere): the label when set, else
# a suffix of the session id lengthened until exactly one session in
# the population ends with it (the W7-L16 collision-proof rule); the
# leading "sess_..." characters are never a display id.
DISPLAY_SUFFIX_MIN = 6

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
    (
        4,
        """
        CREATE TABLE tags (
            session_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (session_id, tag)
        );
        """,
    ),
    (
        5,
        """
        ALTER TABLE sessions ADD COLUMN label_source TEXT;
        ALTER TABLE threads ADD COLUMN options TEXT;
        """,
    ),
]


def normalize_tag(value) -> str:
    """One tag in canonical form; "" when nothing of it is a tag.

    Lowercased and trimmed, every character outside [a-z0-9-]
    stripped (not replaced), no length truncation: callers decide
    whether an over-length result is refused (the store and the
    console both refuse it).
    """
    text = str(value or "").strip().lower()
    return "".join(ch for ch in text if ch in _TAG_ALPHABET)


# -- session identity (W9-L21) --


def _ascii_text(value) -> str:
    """The ASCII characters of value; non-ASCII drops, never maps."""
    return "".join(ch for ch in str(value or "") if ord(ch) < 128)


def prompt_text_from_body(body) -> str:
    """The prompt text out of a UserPromptSubmit body; "" when absent.

    The enriched bodies read "session <short6>, prompt: <first 60
    chars>" (W6-L12): everything after the first "prompt:" marker is
    the prompt. The legacy bodies carry the prompt as the whole body,
    except the no-prompt fallback "session <short6>".
    """
    text = str(body or "")
    marker = text.find("prompt:")
    if marker >= 0:
        return text[marker + len("prompt:"):].strip()
    if text.startswith("session "):
        return ""
    return text.strip()


def auto_label_from_prompt(prompt) -> str:
    """The auto label for one prompt; "" when nothing survives.

    The first 5-7 words of the prompt: at most AUTO_LABEL_MAX_WORDS
    words, then words drop off the end until it fits
    AUTO_LABEL_MAX_CHARS (a single overflowing word is sliced to the
    cap), whitespace collapsed, non-ASCII characters dropped, and
    trailing punctuation artifacts stripped so a label never ends on
    ".", ",", "-", and friends.
    """
    words = " ".join(_ascii_text(prompt).split()).split(" ")
    label = " ".join(words[:AUTO_LABEL_MAX_WORDS])
    while label and len(label) > AUTO_LABEL_MAX_CHARS and " " in label:
        label = label.rsplit(" ", 1)[0]
    if len(label) > AUTO_LABEL_MAX_CHARS:
        label = label[:AUTO_LABEL_MAX_CHARS]
    return label.rstrip(_LABEL_TRAILING)


def unique_display_suffix(session_id, population) -> str:
    """A suffix of the session id unique within population.

    Starts at the shortest useful tail and lengthens one character
    at a time until exactly one id in the list ends with it; the full
    id when even that cannot separate two rows (byte-identical ids
    cannot occur: session_id is the primary key). Mirrors the W7-L16
    console rule; the store exports it so every surface renders ids
    the same way.
    """
    ids = [str(i) for i in population]
    sid = str(session_id)
    shown = sid
    n = DISPLAY_SUFFIX_MIN
    while n < len(sid):
        tail = sid[-n:]
        if sum(1 for other in ids if other.endswith(tail)) == 1:
            shown = tail
            break
        n += 1
    return shown


def session_display_id(session_id, label=None, population=()) -> str:
    """THE display id for one session: the label when set, else the
    collision-proof unique suffix. The leading id characters are
    never the answer. Both the API rows and the console templates
    render this, nothing else."""
    text = str(label or "").strip()
    if text:
        return text
    ids = [str(i) for i in population] if population else [session_id]
    return unique_display_suffix(session_id, ids)


def _auto_tag_values(carried) -> list:
    """The auto tags for a first-sight session: its adapter and its
    project label from cwd, normalized like every other tag. A
    missing adapter or cwd contributes nothing ("unknown" is a
    project_label fallback for notifications, not a tag)."""
    out = []
    adapter = normalize_tag(carried.get("adapter", ""))
    if adapter:
        out.append(adapter)
    cwd = str(carried.get("cwd") or "").strip()
    if cwd:
        project = normalize_tag(payload.project_label(cwd))
        if project:
            out.append(project)
    return out


def _parse_options_json(raw):
    """Thread options from their JSON column; None when absent or
    unparseable (the honest no-options reading for approve rows)."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _validate_ask_options(options) -> list:
    """Ask options in canonical form, or ValueError.

    A non-empty list (at most 8) of objects carrying a non-empty
    string key and a string text; the count range 2-8 belongs to the
    CLI, the store keeps every ask answerable with at least one
    option and never stores more than it can render.
    """
    if not isinstance(options, (list, tuple)) or not (
        1 <= len(options) <= 8
    ):
        raise ValueError("an ask carries 1 to 8 options")
    clean = []
    for option in options:
        if not isinstance(option, dict):
            raise ValueError("each option is an object with key and text")
        key = option.get("key")
        text = option.get("text")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("each option key is a non-empty string")
        if not isinstance(text, str):
            raise ValueError("each option text is a string")
        clean.append({"key": key, "text": text})
    return clean


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
                    if session_id and event.strip().lower() == (
                        "userpromptsubmit"
                    ):
                        self._maybe_auto_label(session_id, rec)
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
            # first sight: the session tags itself with its adapter and
            # its project label, idempotently, never touching operator
            # tags (which cannot exist yet: the session is brand new)
            self._auto_tag_first_sight(session_id, carried)
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

    # -- session auto-identity (W9-L21), all inside the ingest
    # transaction --

    def _maybe_auto_label(self, session_id, rec) -> None:
        """Auto-label from the first suitable prompt record.

        Only when the session carries no label yet AND the operator
        has never touched the label (label_source IS NULL): an
        operator label always wins, and clearing the label pauses
        auto-labeling just as much as setting one. An unsuitable
        record (no prompt text survives) sets nothing, so a later
        prompt can still name the session.
        """
        row = self.conn.execute(
            "SELECT label, label_source FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None or row["label"] is not None:
            return
        if row["label_source"] is not None:
            return
        label = auto_label_from_prompt(
            prompt_text_from_body(rec.get("body"))
        )
        if not label:
            return
        self.conn.execute(
            "UPDATE sessions SET label = ?, label_source = 'auto'"
            " WHERE session_id = ?",
            (label, session_id),
        )

    def _auto_tag_first_sight(self, session_id, carried) -> None:
        """Tag a brand-new session with its adapter and project label.

        Runs once, at session row creation, from the enrichment the
        first record carries: a bare first record (no adapter/cwd)
        leaves the session untagged, and because auto-tags fire only
        at first sight they can never fight a later operator untag.
        Normal tag rules apply (lowercase [a-z0-9-]); a project label
        that normalizes to nothing is skipped.
        """
        for tag in _auto_tag_values(carried):
            self.conn.execute(
                "INSERT OR IGNORE INTO tags (session_id, tag)"
                " VALUES (?, ?)",
                (session_id, tag),
            )

    # -- ask threads (W9-L21) --

    def _thread_row(self, nonce) -> dict:
        """One thread row with its options parsed from JSON."""
        row = self.conn.execute(
            "SELECT * FROM threads WHERE nonce = ?", (str(nonce),)
        ).fetchone()
        if row is None:
            return {}
        item = dict(row)
        item["options"] = _parse_options_json(item.get("options"))
        return item

    def create_ask_thread(self, session_id, question, options,
                          nonce=None, ts=None) -> dict:
        """Open one kind=ask thread carrying the lettered options.

        The options are stored as JSON in the threads row so the
        console can render the choices and the answer state from the
        same read. KeyError for a session the store has never seen;
        ValueError for options that are not a list of 1-8
        {"key": str, "text": str} objects.
        """
        session_id = str(session_id)
        clean = _validate_ask_options(options)
        nonce_value = str(nonce) if nonce else uuid.uuid4().hex[:16]
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
                    "INSERT INTO threads (nonce, session_id, kind, title,"
                    " body, created_ts, options) VALUES (?, ?, 'ask', ?,"
                    " '', ?, ?)",
                    (
                        nonce_value,
                        session_id,
                        str(question or ""),
                        ts or _now_iso(),
                        json.dumps(clean, separators=(",", ":")),
                    ),
                )
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return self._thread_row(nonce_value)

    def answer_ask_thread(self, nonce, key, answer_ts=None) -> dict:
        """Answer an open ask thread with one of its option keys.

        The key matches case-insensitively and lands canonical
        (upper). ValueError when the thread is not an ask, is not
        open anymore, or the key is not one of its options (the
        wrong-key rejection); KeyError for an unknown nonce.
        """
        nonce = str(nonce)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT kind, status, options FROM threads"
                    " WHERE nonce = ?",
                    (nonce,),
                ).fetchone()
                if row is None:
                    raise KeyError("unknown thread nonce: %r" % nonce)
                if row["kind"] != "ask":
                    raise ValueError(
                        "thread %s is not an ask" % nonce
                    )
                if row["status"] != "open":
                    raise ValueError(
                        "thread %s is already %s" % (nonce, row["status"])
                    )
                options = _parse_options_json(row["options"])
                wanted = str(key or "").strip().upper()
                match = None
                for option in options:
                    if str(option.get("key") or "").upper() == wanted:
                        match = str(option.get("key"))
                        break
                if match is None:
                    raise ValueError(
                        "%r is not an option for thread %s (options: %s)"
                        % (
                            key,
                            nonce,
                            ", ".join(
                                str(o.get("key")) for o in options
                            ),
                        )
                    )
                self.conn.execute(
                    "UPDATE threads SET status = 'answered', answer = ?,"
                    " answer_ts = ? WHERE nonce = ?",
                    (match, answer_ts or _now_iso(), nonce),
                )
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return self._thread_row(nonce)

    def expire_ask_thread(self, nonce) -> bool:
        """Mark an open ask thread expired; True when it flipped.

        Answered threads and second attempts are no-ops (False).
        """
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.conn.execute(
                    "UPDATE threads SET status = 'expired'"
                    " WHERE nonce = ? AND status = 'open'",
                    (str(nonce),),
                )
                flipped = cur.rowcount > 0
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return flipped

    def newest_open_ask(self, session_id) -> str:
        """The nonce of the session's newest open ask; "" when none.

        This is the binding rule for session-bound replies: a reply
        aimed at a session (not a thread) resolves to that session's
        newest open ask, so two concurrent asks never steal each
        other's answers.
        """
        row = self.conn.execute(
            "SELECT nonce FROM threads WHERE session_id = ? AND kind = 'ask'"
            " AND status = 'open' ORDER BY created_ts DESC, rowid DESC",
            (str(session_id),),
        ).fetchone()
        return row[0] if row is not None else ""

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
        the label (NULL). Both paths record label_source "operator",
        which permanently pauses auto-labeling for the session: an
        operator label always wins, and so does an operator clear.
        Raises KeyError for a session id the store has never seen.
        Returns the updated row with its tags.
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
                    "UPDATE sessions SET label = ?, label_source ="
                    " 'operator' WHERE session_id = ?",
                    (value, session_id),
                )
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return self._session_row(session_id)

    def add_session_tag(self, session_id, tag) -> dict:
        """Attach one tag to one session; returns the updated row.

        The tag is normalized first (lowercase, trimmed, [a-z0-9-]);
        ValueError when nothing survives normalization, when it
        exceeds TAG_MAX_CHARS, or when it would be a ninth distinct
        tag on the session. Attaching a tag already present is a
        no-op. KeyError for a session the store has never seen.
        """
        return self._write_session_tag(session_id, tag, attach=True)

    def remove_session_tag(self, session_id, tag) -> dict:
        """Detach one tag from one session; returns the updated row.

        The tag is normalized first. Removing a tag that is not
        attached is a no-op. KeyError for an unknown session.
        """
        return self._write_session_tag(session_id, tag, attach=False)

    def _write_session_tag(self, session_id, tag, attach) -> dict:
        session_id = str(session_id)
        tag = normalize_tag(tag)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise KeyError("unknown session: %r" % session_id)
                if attach:
                    if not tag:
                        raise ValueError(
                            "tag must contain letters, digits, or hyphens"
                        )
                    if len(tag) > TAG_MAX_CHARS:
                        raise ValueError(
                            "tag must be at most %d characters"
                            % TAG_MAX_CHARS
                        )
                    have = self._tags_locked(session_id)
                    if tag not in have and len(have) >= TAG_MAX_PER_SESSION:
                        raise ValueError(
                            "a session carries at most %d tags"
                            % TAG_MAX_PER_SESSION
                        )
                    self.conn.execute(
                        "INSERT OR IGNORE INTO tags (session_id, tag)"
                        " VALUES (?, ?)",
                        (session_id, tag),
                    )
                else:
                    self.conn.execute(
                        "DELETE FROM tags WHERE session_id = ? AND tag = ?",
                        (session_id, tag),
                    )
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return self._session_row(session_id)

    def _tags_locked(self, session_id) -> list:
        """Tags for one session, alphabetical; caller holds a write
        transaction or is fine with a racing read."""
        rows = self.conn.execute(
            "SELECT tag FROM tags WHERE session_id = ? ORDER BY tag",
            (session_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def session_tags(self, session_id) -> list:
        """One session's tags, alphabetical."""
        return self._tags_locked(str(session_id))

    def sessions_with_tag(self, tag) -> list:
        """Session ids carrying one normalized tag, insertion-stable
        (ordered by when the store last saw each session)."""
        rows = self.conn.execute(
            "SELECT t.session_id FROM tags t JOIN sessions s"
            " ON s.session_id = t.session_id WHERE t.tag = ?"
            " ORDER BY s.last_seen DESC",
            (normalize_tag(tag),),
        ).fetchall()
        return [row[0] for row in rows]

    def sessions_with_label(self, label) -> list:
        """Session ids carrying one label exactly as given."""
        rows = self.conn.execute(
            "SELECT session_id FROM sessions WHERE label = ?"
            " ORDER BY last_seen DESC",
            (str(label),),
        ).fetchall()
        return [row[0] for row in rows]

    def resolve_session_ref(self, ref) -> dict:
        """Resolve one reply-target reference to a session.

        Order: an exact session id, then a label exactly one session
        carries, then a tag exactly one session carries. Returns a
        dict with match ("session_id", "label", or "tag") and
        session_id on success; on failure match is None with
        ambiguous True and candidates holding every matching session
        id when a label or tag matches more than one session, and
        ambiguous False when nothing matched at all. The caller
        decides the error shape (the console answers ambiguity with
        a 400 carrying the list).
        """
        ref = str(ref or "")
        row = self.conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (ref,)
        ).fetchone()
        if row is not None:
            return {"match": "session_id", "session_id": ref,
                    "ambiguous": False, "candidates": []}
        for match, ids in (
            ("label", self.sessions_with_label(ref)),
            ("tag", self.sessions_with_tag(ref)),
        ):
            if not ids:
                continue
            if len(ids) == 1:
                return {"match": match, "session_id": ids[0],
                        "ambiguous": False, "candidates": []}
            return {"match": None, "session_id": "", "kind": match,
                    "ambiguous": True, "candidates": sorted(ids)}
        return {"match": None, "session_id": "",
                "ambiguous": False, "candidates": []}

    def _identity_maps(self) -> tuple:
        """Label, tag, and display maps over every session, one read
        each, so row list endpoints can answer who did what where on
        every row without per-row queries."""
        rows = self.conn.execute(
            "SELECT session_id, label FROM sessions"
        ).fetchall()
        ids = [row["session_id"] for row in rows]
        labels = {}
        display = {}
        for row in rows:
            sid = row["session_id"]
            labels[sid] = row["label"]
            display[sid] = session_display_id(sid, row["label"], ids)
        tags = {}
        for tag_row in self.conn.execute(
            "SELECT session_id, tag FROM tags ORDER BY tag"
        ).fetchall():
            tags.setdefault(tag_row["session_id"], []).append(
                tag_row["tag"]
            )
        return labels, tags, display

    def _session_row(self, session_id) -> dict:
        """One session row with its tags and display id attached."""
        row = dict(
            self.conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        )
        row["tags"] = self._tags_locked(session_id)
        _, _, display = self._identity_maps()
        row["display_id"] = display.get(str(session_id), "")
        return row

    def session(self, session_id) -> dict:
        """One session row (with tags and display id) or None."""
        found = self.conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        if found is None:
            return None
        return self._session_row(session_id)

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
        """All sessions, most recently seen first, each with its tags
        and its display id (the one display rule: label when set,
        else the collision-proof unique suffix)."""
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY last_seen DESC"
        ).fetchall()
        _, tags, display = self._identity_maps()
        out = []
        for row in rows:
            item = dict(row)
            sid = item["session_id"]
            item["tags"] = tags.get(sid, [])
            item["display_id"] = display.get(sid, "")
            out.append(item)
        return out

    def events(self, limit=50, session_id=None) -> list:
        """Events newest first; one session when session_id is given.

        Ordered by ts, not by insertion id: spool records can arrive
        with out-of-order timestamps, so id order is not chronological.
        The ISO-8601 strings from datetime.isoformat sort correctly as
        plain text; id DESC tiebreaks identical timestamps.

        Every row also carries its session's identity (label, tags,
        display_id) so any stream view answers WHO did WHAT WHERE
        without a second request.
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
        labels, tags, display = self._identity_maps()
        out = []
        for row in rows:
            item = dict(row)
            sid = item.get("session_id") or ""
            item["label"] = labels.get(sid)
            item["tags"] = tags.get(sid, [])
            item["display_id"] = display.get(sid, "")
            out.append(item)
        return out

    def threads(self, status=None) -> list:
        """Threads newest first; one status when given. Ask rows come
        back with their options parsed from JSON (None for approve
        rows and anything unparseable), so one read carries both the
        choices and the answer state."""
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
        out = []
        for row in rows:
            item = dict(row)
            item["options"] = _parse_options_json(item.get("options"))
            out.append(item)
        return out

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

    def display_ids(self) -> dict:
        """The display id per session id over the whole store (the
        one display rule), for surfaces that render many rows."""
        _, _, display = self._identity_maps()
        return display

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
