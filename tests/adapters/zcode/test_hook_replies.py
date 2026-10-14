"""UserPromptSubmit reply injection tests (the W7-L17 delivery gap).

The operator replied Approve from the web console on 2026-10-14; the
reply landed in replies.jsonl and the store but never reached the
running session, because the zcode adapter installed no
UserPromptSubmit hook: unread replies surfaced only when a session
resumed. These tests pin the fix: on every prompt the hook reads
unconsumed replies (the journal plus the store's session binding),
emits them on stdout the way the established SessionStart injection
does (one JSON object with additionalContext), marks what it injected
consumed in the journal, and still spools the prompt record. A reply
read that fails in any way degrades to today's behavior: exit 0, no
stdout, record still spooled.
"""

import json
import sqlite3

import pytest

from tugline.adapters.zcode import hook
from tugline.core import spool
from tugline.core.store import Store

CONFIG_TEXT = """
[server]
url = "http://127.0.0.1:2586"
token_file = "~/.tugline/credentials"

[topics]
main = "tug"
warn = "tug-warn"
reply = "tug-reply"

[policy]
idle_seconds = 600

[toggles]
notify = true
replies = true
approve = false
"""

PROMPT_PAYLOAD = json.dumps(
    {"hookEventName": "UserPromptSubmit", "sessionId": "s-live", "prompt": "go on"}
)


@pytest.fixture
def home(tmp_path):
    return tmp_path / "tugline-home"


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    return str(path)


def journal_path(home):
    return home / "replies.jsonl"


def write_journal(home, entries):
    lines = [json.dumps(e, separators=(",", ":")) for e in entries]
    journal_path(home).parent.mkdir(parents=True, exist_ok=True)
    journal_path(home).write_text("\n".join(lines) + "\n", encoding="utf-8")


def journal(home):
    text = journal_path(home).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def bind_reply(home, body, session_id, *, consumed=False, entry_id=None):
    """One console-shaped reply: a store row plus its journal twin.

    The console writes the store row first and copies its ts into the
    journal line, so (ts, body) is the join between the two.
    """
    store = Store(str(home / "store" / "tugline.db"))
    try:
        row = store.add_reply(body, session_id=session_id)
    finally:
        store.close()
    entry = {
        "id": entry_id or "c1",
        "ts": row["ts"],
        "topic": "tug-reply",
        "title": "console reply",
        "body": body,
        "consumed": consumed,
        "source": "console",
    }
    return entry


def run_prompt(home, config_path, payload=PROMPT_PAYLOAD, env=None):
    out = []
    code = hook.run(
        "UserPromptSubmit",
        payload,
        config_path=config_path,
        home=str(home),
        env=env if env is not None else {},
        _stdout=_ListIO(out),
    )
    return code, "".join(out)


class _ListIO:
    """A stdout stand-in the tests can read without capsys races."""

    def __init__(self, sink):
        self._sink = sink

    def write(self, text):
        self._sink.append(text)

    def flush(self):
        pass


def stdout_payload(stdout):
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])


def spool_events(home):
    return [r["event"] for r in spool.records(str(home / "spool"))]


# the injection itself


def test_prompt_injects_a_session_bound_reply(home, config_path):
    entry = bind_reply(home, "Approve", "s-live")
    write_journal(home, [entry])

    code, stdout = run_prompt(home, config_path)

    assert code == 0
    payload = stdout_payload(stdout)
    assert "Approve" in payload["additionalContext"]
    assert spool_events(home) == ["userpromptsubmit"]
    assert journal(home)[0]["consumed"] is True


def test_prompt_falls_back_to_all_unread_when_none_bound(home, config_path):
    write_journal(
        home,
        [
            {
                "id": "p1",
                "ts": "2026-10-14T05:00:00+00:00",
                "topic": "tug-reply",
                "title": "phone reply",
                "body": "ship it",
                "consumed": False,
            }
        ],
    )

    code, stdout = run_prompt(home, config_path)

    assert code == 0
    assert "ship it" in stdout_payload(stdout)["additionalContext"]


def test_prompt_prefers_replies_bound_to_this_session(home, config_path):
    mine = bind_reply(home, "do the merge", "s-live", entry_id="mine")
    other = bind_reply(home, "not for you", "s-other", entry_id="other")
    phone = dict(mine)
    phone.update({"id": "phone", "ts": "2026-10-14T06:00:00+00:00",
                  "body": "any session", "source": None})
    phone.pop("source")
    write_journal(home, [other, phone, mine])

    code, stdout = run_prompt(home, config_path)

    assert code == 0
    context = stdout_payload(stdout)["additionalContext"]
    assert "do the merge" in context
    assert "not for you" not in context
    assert "any session" not in context
    by_id = {e["id"]: e["consumed"] for e in journal(home)}
    assert by_id == {"mine": True, "other": False, "phone": False}


def test_consumed_replies_are_not_reinjected(home, config_path):
    entry = bind_reply(home, "Approve", "s-live")
    entry["consumed"] = True
    write_journal(home, [entry])

    code, stdout = run_prompt(home, config_path)

    assert code == 0
    assert stdout == ""
    assert spool_events(home) == ["userpromptsubmit"]
    assert journal(home)[0]["consumed"] is True


def test_injection_caps_at_the_five_newest_replies(home, config_path):
    entries = []
    for i in range(7):
        entry = {
            "id": "r%d" % i,
            "ts": "2026-10-14T05:0%d:00+00:00" % i,
            "topic": "tug-reply",
            "title": "phone reply",
            "body": "reply number %d" % i,
            "consumed": False,
        }
        entries.append(entry)
    write_journal(home, entries)

    code, stdout = run_prompt(home, config_path)

    assert code == 0
    context = stdout_payload(stdout)["additionalContext"]
    for i in range(2, 7):
        assert "reply number %d" % i in context
    assert "reply number 0" not in context
    assert "reply number 1" not in context
    by_id = {e["id"]: e["consumed"] for e in journal(home)}
    assert by_id["r0"] is False
    assert by_id["r1"] is False
    for i in range(2, 7):
        assert by_id["r%d" % i] is True


# fail-open: a reply read that fails never blocks the prompt


def test_broken_store_degrades_to_the_journal(home, config_path):
    db = home / "store" / "tugline.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not sqlite" * 64)
    write_journal(
        home,
        [
            {
                "id": "x1",
                "ts": "2026-10-14T05:00:00+00:00",
                "topic": "tug-reply",
                "title": "phone reply",
                "body": "still readable",
                "consumed": False,
            }
        ],
    )

    code, stdout = run_prompt(home, config_path)

    assert code == 0
    assert "still readable" in stdout_payload(stdout)["additionalContext"]


def test_unreadable_journal_never_fails_the_hook(home, config_path, tmp_path):
    blocker = tmp_path / "blocked-replies.jsonl"
    blocker.mkdir()
    real = journal_path(home)
    real.parent.mkdir(parents=True, exist_ok=True)
    real.symlink_to(blocker)

    code, stdout = run_prompt(home, config_path)

    assert code == 0
    assert stdout == ""
    assert spool_events(home) == ["userpromptsubmit"]


def test_missing_journal_and_store_is_a_quiet_prompt(home, config_path):
    code, stdout = run_prompt(home, config_path)

    assert code == 0
    assert stdout == ""
    assert spool_events(home) == ["userpromptsubmit"]


# the store probe is bounded and read-only


def test_store_probe_opens_read_only(home, config_path, tmp_path):
    entry = bind_reply(home, "Approve", "s-live")
    write_journal(home, [entry])

    code, stdout = run_prompt(home, config_path)

    assert code == 0
    assert stdout  # the bound reply was found through the store
    db = home / "store" / "tugline.db"
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        row = conn.execute(
            "SELECT delivered FROM replies WHERE session_id = 's-live'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == 0  # the adapter never writes the store
