"""CLI surface for the ask primitive (W9-L21): tug ask and tug answer.

tug ask creates the ask thread, pushes the notification, blocks for
the answer, prints "key: text" on stdout and exits 0; on timeout it
prints "no answer", exits 1, and the thread is expired. tug answer
flips an ask thread from the agent or controller side. Usage errors
exit 2; no path ever tracebacks.
"""

import threading
import time

import pytest

from tugline.cli import main
from tugline.core import paths, spool
from tugline.core.store import Store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    monkeypatch.delenv("TUGLINE_SESSION", raising=False)
    return tmp_path


def seed_session(home, session_id="sess_517ee9c0aaaa"):
    return spool.append(
        paths.spool_root(),
        event="sessionstart",
        session_id=session_id,
        topic="main",
        priority="default",
        title="session started",
        body="session c0aaaa, started",
        extra={"adapter": "zcode", "cwd": "/Users/op/work/mission-control"},
    )


def store(home):
    """Open the store and fold the spool once, exactly like the CLI
    commands do, so seeded sessions are visible."""
    opened = Store(paths.store_path())
    opened.ingest_spool(paths.spool_root())
    return opened


def write_config(home, url):
    """Point the tmp home at a test ntfy URL (never the live one).
    TUGLINE_HOME *is* the home root, so config.toml sits directly
    under it, not inside a .tugline subdir."""
    config_path = home / "config.toml"
    config_path.write_text(
        '[server]\nurl = "%s"\ntoken_file = "none"\n\n[topics]\nmain = "tug"'
        "\nwarn = \"tug-warn\"\nreply = \"tug-reply\"\n\n[policy]\n"
        "idle_seconds = 600\n\n[toggles]\nnotify = true\nreplies = true\n"
        "approve = false\n" % url
    )


def open_threads(store):
    return [t for t in store.threads() if t["status"] == "open"]


# -- argument validation --


def test_ask_needs_two_options(home, capsys):
    seed_session(home)
    assert (
        main(["ask", "--session", "sess_517ee9c0aaaa",
              "--option", "A: only one", "q?"]) == 2
    )
    assert "2 to 8" in capsys.readouterr().err


def test_ask_rejects_bad_timeouts(home, capsys):
    seed_session(home)
    for bad in ("0", "-5", "1801"):
        assert (
            main(["ask", "--session", "sess_517ee9c0aaaa",
                  "--option", "A: x", "--option", "B: y",
                  "--timeout", bad, "q?"]) == 2
        )
        assert "timeout" in capsys.readouterr().err.lower()


def test_ask_rejects_empty_question(home, capsys):
    seed_session(home)
    assert (
        main(["ask", "--session", "sess_517ee9c0aaaa",
              "--option", "A: x", "--option", "B: y", "   "]) == 2
    )


def test_ask_unknown_session_is_a_clean_exit_2(home, capsys):
    seed_session(home)
    assert (
        main(["ask", "--session", "ghost",
              "--option", "A: x", "--option", "B: y", "q?"]) == 2
    )
    assert "unknown session" in capsys.readouterr().err.lower()
    # nothing was created for the ghost
    assert store(home).threads() == []


# -- the blocking lifecycle --


def test_ask_times_out_with_no_answer(home, fake_ntfy, capsys):
    seed_session(home)
    write_config(home, fake_ntfy.url)
    code = main(
        ["ask", "--session", "sess_517ee9c0aaaa",
         "--option", "A: ship", "--option", "B: roll back",
         "--timeout", "1", "which deploy?"]
    )
    out = capsys.readouterr()
    assert code == 1
    assert out.out.strip() == "no answer"
    threads = store(home).threads()
    assert len(threads) == 1
    assert threads[0]["status"] == "expired"
    assert threads[0]["kind"] == "ask"
    # one HIGH priority enriched notification went out, then nothing
    posts = fake_ntfy.posts("/tug")
    assert len(posts) == 1
    assert posts[0]["headers"]["x-priority"] == "high"
    assert (
        posts[0]["headers"]["x-title"]
        == "[zcode] mission-control - asks: which deploy?"
    )
    assert "reply with a letter" in posts[0]["body"]


def test_ask_answers_from_a_console_reply_while_blocking(home, fake_ntfy,
                                                          capsys):
    seed_session(home)
    write_config(home, fake_ntfy.url)
    result = {}

    def run_ask():
        result["code"] = main(
            ["ask", "--session", "sess_517ee9c0aaaa",
             "--option", "A: ship", "--option", "B: roll back",
             "--timeout", "30", "which deploy?"]
        )

    thread = threading.Thread(target=run_ask)
    thread.start()
    deadline = time.monotonic() + 10
    nonce = None
    while time.monotonic() < deadline:
        with store(home) as opened:  # context manager closes the conn
            asks = [t for t in opened.threads() if t["kind"] == "ask"]
            if asks:
                nonce = asks[0]["nonce"]
                break
        time.sleep(0.05)
    assert nonce, "the ask thread never appeared in the store"
    with store(home) as opened:
        opened.add_reply("b", thread_nonce=nonce, source="console")
    thread.join(timeout=15)
    assert not thread.is_alive()
    out = capsys.readouterr()
    assert result["code"] == 0
    assert out.out.strip() == "B: roll back"


# -- tug answer --


def test_answer_flips_the_thread(home, capsys):
    seed_session(home)
    with store(home) as opened:
        opened.create_ask_thread(
            "sess_517ee9c0aaaa",
            "which deploy?",
            [{"key": "A", "text": "ship"}, {"key": "B", "text": "roll back"}],
            nonce="ask-n1",
        )
    assert main(["answer", "--thread", "ask-n1", "b"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "B: roll back"
    threads = store(home).threads()
    assert threads[0]["status"] == "answered"
    assert threads[0]["answer"] == "B"


def test_answer_wrong_key_is_rejected(home, capsys):
    seed_session(home)
    with store(home) as opened:
        opened.create_ask_thread(
            "sess_517ee9c0aaaa",
            "q",
            [{"key": "A", "text": "ship"}, {"key": "B", "text": "roll"}],
            nonce="ask-n1",
        )
    assert main(["answer", "--thread", "ask-n1", "Z"]) == 2
    assert "not an option" in capsys.readouterr().err
    assert store(home).threads()[0]["status"] == "open"


def test_answer_unknown_thread(home, capsys):
    seed_session(home)
    assert main(["answer", "--thread", "ghost", "A"]) == 2
    assert "unknown thread" in capsys.readouterr().err.lower()
