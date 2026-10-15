"""The tug ask primitive (W9-L21): one MCQ question, answerable
from tugline itself.

The runner creates an ask thread, pushes one HIGH priority
notification with the enriched title helpers, then blocks polling
for an answer up to the timeout. Answers arrive three ways:

(a) a console option tap or a reply bound to the thread nonce (a
    store reply row carrying thread_nonce, or session_id after the
    console resolved id > label > tag);
(b) a reply journal line whose body is exactly one option key and
    whose thread/session binding resolves to the ask thread (a
    bare unbound ntfy reply never maps);
(c) tug answer --thread <nonce> <key>, which flips the thread row
    directly.

Wrong keys are rejected (the ask stays open). On timeout the thread
is marked expired. No failure path ever raises out of run().
"""

import json
import os

import pytest

from tugline.core import spool
from tugline.core.ask import (
    AskRunner,
    match_option,
    parse_options,
    ask_body,
    ask_title,
)
from tugline.core.publisher import PublishResult
from tugline.core.store import Store

WHEN_TEXT = "2026-10-14T12:00:00+00:00"

OPTIONS = [
    {"key": "A", "text": "ship the release"},
    {"key": "B", "text": "roll back"},
]


class FakePublisher:
    def __init__(self, fail=False, raise_on_publish=False):
        self.calls = []
        self.fail = fail
        self.raise_on_publish = raise_on_publish

    def publish(self, topic, body, title=None, priority=None,
                tags=None, click=None, actions=None):
        if self.raise_on_publish:
            raise RuntimeError("publisher exploded")
        self.calls.append(
            {
                "topic": topic,
                "body": body,
                "title": title,
                "priority": priority,
                "tags": tags,
                "actions": actions,
            }
        )
        if self.fail:
            return PublishResult(ok=False, status=500, error="HTTP 500")
        return PublishResult(ok=True, status=200, message_id="m1")


class FakeClock:
    """monotonic time that only moves when sleep is called."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = None  # optional hook: fn(seconds) runs first

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if self.on_sleep is not None:
            hook = self.on_sleep
            self.on_sleep = None  # fire once, like an operator typing
            hook(seconds)
        self.sleeps.append(seconds)
        self.now += seconds


def seed(tmp_path, session_id="sess_517ee9c0aaaa", **extra):
    base = {"adapter": "zcode", "cwd": "/Users/op/work/mission-control"}
    base.update(extra)
    return spool.append(
        str(tmp_path / "spool"),
        event="sessionstart",
        session_id=session_id,
        topic="main",
        priority="default",
        title="session started",
        body="session c0aaaa, started",
        extra=base,
    )


def open_store(tmp_path):
    store = Store(tmp_path / "store" / "tugline.db")
    store.ingest_spool(str(tmp_path / "spool"))
    return store


def make_runner(tmp_path, publisher=None, clock=None, **kw):
    store = open_store(tmp_path)
    clock = clock or FakeClock()
    runner = AskRunner(
        store,
        publisher=publisher if publisher is not None else FakePublisher(),
        replies_path=str(tmp_path / "replies.jsonl"),
        poll_interval=1.0,
        now=clock.monotonic,
        sleep=clock.sleep,
        **kw,
    )
    runner._test_store = store
    runner._test_clock = clock
    return runner


def journal_append(path, entry):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


# -- option parsing --


def test_parse_options_explicit_letters_win():
    assert parse_options(["A: ship", "B: roll back"]) == [
        {"key": "A", "text": "ship"},
        {"key": "B", "text": "roll back"},
    ]


def test_parse_options_auto_lettering():
    assert parse_options(["ship", "B: roll back", "ask the operator"]) == [
        {"key": "A", "text": "ship"},
        {"key": "B", "text": "roll back"},
        {"key": "C", "text": "ask the operator"},
    ]


def test_parse_options_rejects_counts_out_of_range():
    with pytest.raises(ValueError):
        parse_options(["only one"])
    with pytest.raises(ValueError):
        parse_options(["o%d" % i for i in range(9)])


def test_parse_options_rejects_duplicates_and_empties():
    with pytest.raises(ValueError):
        parse_options(["A: x", "A: y"])
    with pytest.raises(ValueError):
        parse_options(["A: x", " "])


def test_match_option_exact_key_case_insensitive():
    assert match_option(OPTIONS, "b") == "B"
    assert match_option(OPTIONS, "A") == "A"
    assert match_option(OPTIONS, " A ") == "A"


def test_match_option_rejects_non_keys():
    assert match_option(OPTIONS, "Z") is None
    assert match_option(OPTIONS, "A: ship the release") is None
    assert match_option(OPTIONS, "") is None


# -- the notification --


def test_ask_title_reuses_the_enriched_helpers(tmp_path):
    seed(tmp_path)
    store = open_store(tmp_path)
    title = ask_title(store.session("sess_517ee9c0aaaa"), "which deploy?")
    assert title == "[zcode] mission-control - asks: which deploy?"
    store.close()


def test_ask_title_falls_back_when_the_adapter_is_unknown(tmp_path):
    assert (
        ask_title({"adapter": "", "cwd": ""}, "q")
        == "[tugline] unknown - asks: q"
    )


def test_ask_body_lists_lettered_options_and_reply_guidance():
    body = ask_body("sess_517ee9c0aaaa", OPTIONS)
    lines = body.splitlines()
    assert lines[0] == "session c0aaaa"
    assert "A: ship the release" in lines
    assert "B: roll back" in lines
    assert lines[-1] == "reply with a letter: A, B"


# -- the lifecycle --


def _answer_later(store):
    """The operator answers while the ask blocks (fires on first sleep)."""
    store.answer_ask_thread("ask-1", "B")


def test_full_lifecycle_answered_while_blocking(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    clock.on_sleep = lambda seconds: _answer_later(store)
    result = runner.run(
        "sess_517ee9c0aaaa",
        "which deploy?",
        OPTIONS,
        timeout_s=300,
        nonce="ask-1",
    )
    assert result["ok"] is True
    assert result["key"] == "B"
    assert result["text"] == "roll back"
    thread = [t for t in store.threads() if t["nonce"] == "ask-1"][0]
    assert thread["status"] == "answered"
    assert thread["answer"] == "B"
    assert thread["kind"] == "ask"
    assert thread["options"] == OPTIONS
    store.close()


def test_notification_is_high_priority_and_enriched(tmp_path):
    seed(tmp_path)
    publisher = FakePublisher()
    runner = make_runner(tmp_path, publisher=publisher)
    store, clock = runner._test_store, runner._test_clock
    clock.on_sleep = lambda seconds: _answer_later(store)
    result = runner.run(
        "sess_517ee9c0aaaa", "which deploy?", OPTIONS, timeout_s=300,
        nonce="ask-1",
    )
    assert result["ok"] is True
    assert len(publisher.calls) == 1
    call = publisher.calls[0]
    assert call["title"] == "[zcode] mission-control - asks: which deploy?"
    assert call["priority"] == "high"
    assert "A: ship the release" in call["body"]
    assert "reply with a letter" in call["body"]
    runner._test_store.close()


def test_answer_via_console_reply_bound_to_the_nonce(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock

    def reply_later(seconds):
        store.add_reply("b", thread_nonce="ask-1", source="console")

    clock.on_sleep = reply_later
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=300, nonce="ask-1"
    )
    assert result["ok"] is True
    assert result["key"] == "B"
    runner._test_store.close()


def test_answer_via_console_reply_bound_to_the_session(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock

    def reply_later(seconds):
        store.add_reply("a", session_id="sess_517ee9c0aaaa", source="console")

    clock.on_sleep = reply_later
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=300, nonce="ask-1"
    )
    assert result["ok"] is True
    assert result["key"] == "A"
    runner._test_store.close()


def test_answer_via_journal_reply_bound_by_label(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    store.set_session_label("sess_517ee9c0aaaa", "night build")
    journal = str(tmp_path / "replies.jsonl")
    journal_append(
        journal,
        {"id": "pre-existing", "body": "noise", "session_id": "other"},
    )

    def reply_later(seconds):
        journal_append(
            journal,
            {
                "id": "ntfy-1",
                "body": "B",
                "consumed": False,
                "session_id": "night build",  # resolved id > label > tag
            },
        )

    clock.on_sleep = reply_later
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=300, nonce="ask-1"
    )
    assert result["ok"] is True
    assert result["key"] == "B"
    runner._test_store.close()


def test_answer_via_journal_reply_bound_by_tag(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    store.add_session_tag("sess_517ee9c0aaaa", "build")
    journal = str(tmp_path / "replies.jsonl")

    def reply_later(seconds):
        journal_append(
            journal,
            {"id": "ntfy-2", "body": "A", "consumed": False,
             "session_id": "build"},
        )

    clock.on_sleep = reply_later
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=300, nonce="ask-1"
    )
    assert result["ok"] is True
    runner._test_store.close()


def test_session_bound_reply_lands_on_the_newest_open_ask(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    older = store.create_ask_thread(
        "sess_517ee9c0aaaa", "older q", OPTIONS, nonce="older",
        ts="2026-10-13T11:00:00+00:00",
    )
    assert older["status"] == "open"

    def reply_later(seconds):
        store.add_reply(
            "a", session_id="sess_517ee9c0aaaa", source="console"
        )

    clock.on_sleep = reply_later
    result = runner.run(
        "sess_517ee9c0aaaa", "newer q", OPTIONS, timeout_s=300,
        nonce="ask-1",
    )
    assert result["ok"] is True
    threads = {t["nonce"]: t for t in store.threads()}
    assert threads["ask-1"]["status"] == "answered"
    assert threads["older"]["status"] == "open"
    runner._test_store.close()


def test_wrong_key_reply_is_rejected_and_the_ask_keeps_waiting(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    journal = str(tmp_path / "replies.jsonl")

    def wrong_then_right(seconds):
        store.add_reply("Z", thread_nonce="ask-1", source="console")
        journal_append(
            journal,
            {"id": "z-1", "body": "B: roll back please", "consumed": False,
             "session_id": "sess_517ee9c0aaaa"},
        )

    clock.on_sleep = wrong_then_right
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=300, nonce="ask-1"
    )
    # neither wrong-key shape answered the ask: it times out
    assert result["ok"] is False
    assert result["reason"] == "timeout"
    thread = [t for t in store.threads() if t["nonce"] == "ask-1"][0]
    assert thread["status"] == "expired"
    runner._test_store.close()


def test_unbound_ntfy_reply_never_maps(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    clock = runner._test_clock
    journal = str(tmp_path / "replies.jsonl")

    def reply_later(seconds):
        journal_append(
            journal,
            {"id": "free-1", "body": "A", "consumed": False},
        )

    clock.on_sleep = reply_later
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=5, nonce="ask-1"
    )
    assert result["ok"] is False
    assert result["reason"] == "timeout"
    runner._test_store.close()


def test_journal_reply_for_another_session_never_maps(tmp_path):
    seed(tmp_path, session_id="sess_517ee9c0aaaa")
    seed(tmp_path, session_id="sess_517ee9c1bbbb")
    runner = make_runner(tmp_path)
    clock = runner._test_clock
    journal = str(tmp_path / "replies.jsonl")

    def reply_later(seconds):
        journal_append(
            journal,
            {"id": "other-1", "body": "A", "consumed": False,
             "session_id": "sess_517ee9c1bbbb"},
        )

    clock.on_sleep = reply_later
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=5, nonce="ask-1"
    )
    assert result["ok"] is False
    runner._test_store.close()


def test_timeout_marks_the_thread_expired(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=7, nonce="ask-1"
    )
    assert result["ok"] is False
    assert result["reason"] == "timeout"
    thread = [t for t in store.threads() if t["nonce"] == "ask-1"][0]
    assert thread["status"] == "expired"
    # the wait was bounded: sleeps cover the timeout, never exceed it
    assert sum(clock.sleeps) <= 7 + 1.0
    store.close()


def test_zero_timeout_expires_immediately(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=0, nonce="ask-1"
    )
    assert result["ok"] is False
    assert result["reason"] == "timeout"
    runner._test_store.close()


def test_answered_by_tug_answer_while_blocking(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    clock.on_sleep = lambda seconds: _answer_later(store)
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=300, nonce="ask-1"
    )
    assert result["ok"] is True
    assert result["key"] == "B"
    runner._test_store.close()


def test_console_tap_flip_then_reply_row_still_answers(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock

    def tap_like_the_console_endpoint(seconds):
        # the console answer endpoint closes the thread first, then
        # records the reply row; a poll that sees the row after the
        # close must read it as the answer, not as a race error
        store.answer_ask_thread("ask-1", "B")
        store.add_reply("B", thread_nonce="ask-1", source="console")

    clock.on_sleep = tap_like_the_console_endpoint
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=300, nonce="ask-1"
    )
    assert result["ok"] is True
    assert result["key"] == "B"
    runner._test_store.close()


# -- never raises --


def test_unknown_session_returns_cleanly(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    result = runner.run(
        "ghost-session", "q?", OPTIONS, timeout_s=1, nonce="ask-1"
    )
    assert result["ok"] is False
    assert result["reason"] == "unknown session"
    runner._test_store.close()


def test_exploding_publisher_never_raises(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path, publisher=FakePublisher(
        raise_on_publish=True
    ))
    store, clock = runner._test_store, runner._test_clock
    clock.on_sleep = lambda seconds: _answer_later(store)
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=300, nonce="ask-1"
    )
    assert result["ok"] is True  # the ask still answers via the store
    runner._test_store.close()


def test_failed_publish_still_waits_and_times_out(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path, publisher=FakePublisher(fail=True))
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=2, nonce="ask-1"
    )
    assert result["ok"] is False
    assert result["reason"] == "timeout"
    runner._test_store.close()


def test_unreadable_journal_never_raises(tmp_path):
    seed(tmp_path)
    # a directory where the journal should be: every read fails
    os.makedirs(tmp_path / "replies.jsonl")
    runner = make_runner(tmp_path)
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=1, nonce="ask-1"
    )
    assert result["ok"] is False
    assert result["reason"] == "timeout"
    runner._test_store.close()


def test_ask_thread_survives_approval_reconciliation(tmp_path):
    """An open ask thread must stay open when refresh_threads runs:
    ask threads have no approval pending files, so reconciling them
    against the approvals directory (the console ingest does, every
    couple of seconds) would expire every live ask instantly."""
    import os

    from tugline.core.store import Store

    store = Store(tmp_path / "store" / "tugline.db")
    store._upsert_session(
        "sess_ask1", "sessionstart", "2026-10-14T00:00:00+00:00",
        {"adapter": "zcode", "project": "p", "cwd": "/tmp"},
    )
    thread = store.create_ask_thread(
        "sess_ask1", "pick one",
        [{"key": "A", "text": "go"}, {"key": "B", "text": "stop"}],
    )
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    counts = store.refresh_threads(approvals)
    row = next(
        t for t in store.threads() if t["nonce"] == thread["nonce"]
    )
    assert row["status"] == "open", (
        "ask thread expired by approval reconciliation: %r (counts %r)"
        % (row["status"], counts)
    )


# -- name resolution on the ask path (W10-L23) --
#
# tug ask used to accept only the exact bare session id: a label or a
# tag (both valid reply targets on the console, which resolves id >
# unique label > unique tag) failed with "unknown session". The ask
# path now runs the same resolution; ambiguous names fail with the
# candidates listed, exactly like the console's 400.


def test_ask_by_unique_label_resolves(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    store.set_session_label("sess_517ee9c0aaaa", "night build")
    journal = str(tmp_path / "replies.jsonl")

    def reply_later(seconds):
        journal_append(
            journal,
            {"id": "ntfy-l", "body": "A", "consumed": False,
             "session_id": "sess_517ee9c0aaaa"},
        )

    clock.on_sleep = reply_later
    result = runner.run(
        "night build", "q?", OPTIONS, timeout_s=300, nonce="ask-1"
    )
    assert result["ok"] is True
    assert result["key"] == "A"
    runner._test_store.close()


def test_ask_by_unique_tag_resolves(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    store.add_session_tag("sess_517ee9c0aaaa", "deploy")
    journal = str(tmp_path / "replies.jsonl")

    def reply_later(seconds):
        journal_append(
            journal,
            {"id": "ntfy-t", "body": "B", "consumed": False,
             "session_id": "sess_517ee9c0aaaa"},
        )

    clock.on_sleep = reply_later
    result = runner.run(
        "deploy", "q?", OPTIONS, timeout_s=300, nonce="ask-2"
    )
    assert result["ok"] is True
    assert result["key"] == "B"
    runner._test_store.close()


def test_ask_by_ambiguous_tag_fails_with_candidates(tmp_path):
    seed(tmp_path, session_id="sess_one")
    seed(tmp_path, session_id="sess_two")
    runner = make_runner(tmp_path)
    store = runner._test_store
    store.add_session_tag("sess_one", "build")
    store.add_session_tag("sess_two", "build")

    result = runner.run(
        "build", "q?", OPTIONS, timeout_s=5, nonce="ask-3"
    )

    assert result["ok"] is False
    assert result["reason"] == "ambiguous session"
    assert result["kind"] == "tag"
    assert sorted(result["candidates"]) == ["sess_one", "sess_two"]
    assert store.threads() == []  # nothing was created for the name
    store.close()


def test_ask_by_ambiguous_label_fails_with_candidates(tmp_path):
    seed(tmp_path, session_id="sess_one")
    seed(tmp_path, session_id="sess_two")
    runner = make_runner(tmp_path)
    store = runner._test_store
    store.set_session_label("sess_one", "twin")
    store.set_session_label("sess_two", "twin")

    result = runner.run("twin", "q?", OPTIONS, timeout_s=5, nonce="ask-4")

    assert result["ok"] is False
    assert result["reason"] == "ambiguous session"
    assert result["kind"] == "label"
    assert sorted(result["candidates"]) == ["sess_one", "sess_two"]
    store.close()


def test_ask_by_exact_id_still_resolves(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    clock = runner._test_clock
    journal = str(tmp_path / "replies.jsonl")

    def reply_later(seconds):
        journal_append(
            journal,
            {"id": "ntfy-i", "body": "A", "consumed": False,
             "session_id": "sess_517ee9c0aaaa"},
        )

    clock.on_sleep = reply_later
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=300, nonce="ask-5"
    )
    assert result["ok"] is True
    runner._test_store.close()


# -- an answered ask consumes its reply journal line (W10-L23) --
#
# A console reply that answers an ask used to leave its replies.jsonl
# line unconsumed, so the wake dispatcher still spawned a redundant
# claude --resume -p "B" turn for the very reply the ask had just
# delivered. When the runner claims an answer it now marks the line
# consumed, exactly like the injection path and the dispatcher do on
# delivery.


def journal_lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def console_dual_write(store, journal, nonce, body, line_id, session_id):
    """Mirror the console's dual write: one store reply row bound to
    the thread plus one replies.jsonl line carrying the row's ts and
    body (the console mints a fresh journal id per line)."""
    row = store.add_reply(body, thread_nonce=nonce, source="console")
    journal_append(
        journal,
        {
            "id": line_id,
            "ts": row["ts"],
            "topic": "tug-reply",
            "title": "console reply",
            "body": body,
            "consumed": False,
            "source": "console",
            "session_id": session_id,
        },
    )
    return row


def test_console_reply_answer_consumes_the_journal_line(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    journal = str(tmp_path / "replies.jsonl")

    def reply_later(seconds):
        console_dual_write(
            store, journal, "ask-c1", "B", "line-1", "sess_517ee9c0aaaa"
        )

    clock.on_sleep = reply_later
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=300, nonce="ask-c1"
    )
    assert result["ok"] is True
    assert result["via"] == "reply"
    lines = journal_lines(journal)
    assert [line["consumed"] for line in lines] == [True]
    store.close()


def test_journal_answer_consumes_its_line(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    journal = str(tmp_path / "replies.jsonl")
    journal_append(
        journal,
        {"id": "noise", "body": "not for this ask",
         "session_id": "sess_517ee9c0aaaa", "consumed": False},
    )

    def reply_later(seconds):
        journal_append(
            journal,
            {"id": "ntfy-9", "body": "A", "consumed": False,
             "session_id": "sess_517ee9c0aaaa"},
        )

    clock.on_sleep = reply_later
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=300, nonce="ask-j1"
    )
    assert result["ok"] is True
    assert result["via"] == "ntfy reply"
    consumed = {
        line["id"]: line["consumed"] for line in journal_lines(journal)
    }
    assert consumed == {"noise": False, "ntfy-9": True}
    store.close()


def test_non_key_reply_never_consumes_its_line(tmp_path):
    seed(tmp_path)
    runner = make_runner(tmp_path)
    store, clock = runner._test_store, runner._test_clock
    journal = str(tmp_path / "replies.jsonl")

    def reply_later(seconds):
        console_dual_write(
            store, journal, "ask-n1", "maybe", "line-m",
            "sess_517ee9c0aaaa",
        )

    clock.on_sleep = reply_later
    result = runner.run(
        "sess_517ee9c0aaaa", "q?", OPTIONS, timeout_s=5, nonce="ask-n1"
    )
    assert result["ok"] is False  # a non-key body never answers
    lines = journal_lines(journal)
    assert [line["consumed"] for line in lines] == [False]
    store.close()
