"""Agent self-naming commands: label, tag, untag, sessions (W7-L18).

Agents know their own session id from context but CLAUDE_SESSION_ID
is not exported to agent shells, so every naming command takes the
session as an explicit --session flag (never from the environment).
The commands work whether or not a console is running: each opens the
store under TUGLINE_HOME and folds the spool once, the same idempotent
pass the console performs, so a fresh home with spooled sessions is
addressable immediately.
"""

import json

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


def seed_spool(home, session_id, event="notify"):
    return spool.append(
        paths.spool_root(),
        event=event,
        session_id=session_id,
        topic="main",
        priority="default",
        title="seed",
        body="seed body",
    )


def store(home):
    return Store(paths.store_path())


# -- tug label --


def test_label_sets_the_name(home, capsys):
    seed_spool(home, "sess-1")
    assert main(["label", "--session", "sess-1", "controller"]) == 0
    out = capsys.readouterr().out
    assert "sess-1" in out
    assert "controller" in out
    assert store(home).sessions()[0]["label"] == "controller"


def test_label_clears_with_flag(home, capsys):
    seed_spool(home, "sess-1")
    main(["label", "--session", "sess-1", "controller"])
    capsys.readouterr()
    assert main(["label", "--session", "sess-1", "--clear"]) == 0
    assert store(home).sessions()[0]["label"] is None


def test_label_trims_the_value(home):
    seed_spool(home, "sess-1")
    assert main(["label", "--session", "sess-1", "  spaced  "]) == 0
    assert store(home).sessions()[0]["label"] == "spaced"


def test_label_unknown_session_fails(home, capsys):
    seed_spool(home, "sess-1")
    assert main(["label", "--session", "ghost", "x"]) == 2
    assert "unknown session" in capsys.readouterr().err.lower()


def test_label_needs_explicit_session(home, capsys, monkeypatch):
    # the id is always an explicit flag, even with TUGLINE_SESSION set
    monkeypatch.setenv("TUGLINE_SESSION", "sess-env")
    seed_spool(home, "sess-env")
    with pytest.raises(SystemExit) as excinfo:
        main(["label", "name"])
    assert excinfo.value.code == 2


# -- tug tag / tug untag --


def test_tag_adds_one_or_more(home, capsys):
    seed_spool(home, "sess-1")
    assert main(["tag", "--session", "sess-1", "build"]) == 0
    assert "build" in capsys.readouterr().out
    assert main(["tag", "--session", "sess-1", "night", "w7"]) == 0
    assert store(home).session_tags("sess-1") == ["build", "night", "w7"]


def test_tag_normalizes_like_the_api(home):
    seed_spool(home, "sess-1")
    assert main(["tag", "--session", "sess-1", "Build-Night"]) == 0
    assert store(home).session_tags("sess-1") == ["build-night"]


def test_tag_invalid_fails_without_partial_writes(home, capsys):
    seed_spool(home, "sess-1")
    assert main(["tag", "--session", "sess-1", "good", "!!!"]) == 2
    assert store(home).session_tags("sess-1") == []


def test_tag_over_length_fails(home, capsys):
    seed_spool(home, "sess-1")
    assert main(["tag", "--session", "sess-1", "a" * 25]) == 2
    assert "24" in capsys.readouterr().err


def test_tag_ninth_tag_fails(home, capsys):
    seed_spool(home, "sess-1")
    args = ["tag", "--session", "sess-1"] + ["t%d" % i for i in range(8)]
    assert main(args) == 0
    capsys.readouterr()
    assert main(["tag", "--session", "sess-1", "one-more"]) == 2
    assert len(store(home).session_tags("sess-1")) == 8


def test_untag_removes(home, capsys):
    seed_spool(home, "sess-1")
    main(["tag", "--session", "sess-1", "build", "night"])
    capsys.readouterr()
    assert main(["untag", "--session", "sess-1", "build"]) == 0
    assert store(home).session_tags("sess-1") == ["night"]


def test_untag_absent_tag_is_fine(home):
    seed_spool(home, "sess-1")
    assert main(["untag", "--session", "sess-1", "nope"]) == 0
    assert store(home).session_tags("sess-1") == []


def test_tag_unknown_session_fails(home, capsys):
    seed_spool(home, "sess-1")
    assert main(["tag", "--session", "ghost", "x"]) == 2
    assert "unknown session" in capsys.readouterr().err.lower()


# -- tug sessions --


def test_sessions_lists_ids_labels_and_tags(home, capsys):
    seed_spool(home, "sess-1")
    seed_spool(home, "sess-2")
    main(["label", "--session", "sess-1", "controller"])
    main(["tag", "--session", "sess-1", "build"])
    capsys.readouterr()

    assert main(["sessions"]) == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[0].split("\t") == [
        "session", "label", "tags", "state", "last_seen"
    ]
    rows = {}
    for line in lines[1:]:
        parts = line.split("\t")
        assert len(parts) == 5, line
        rows[parts[0]] = parts[1:4]
    assert rows["sess-1"][:2] == ["controller", "build"]
    assert rows["sess-2"][:2] == ["-", "-"]


def test_sessions_filters_by_label(home, capsys):
    seed_spool(home, "sess-1")
    seed_spool(home, "sess-2")
    main(["label", "--session", "sess-1", "controller"])
    capsys.readouterr()

    assert main(["sessions", "--label", "controller"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2  # header plus one row
    assert "sess-1" in out[1]
    assert "sess-2" not in out


def test_sessions_filters_by_tag(home, capsys):
    seed_spool(home, "sess-1")
    seed_spool(home, "sess-2")
    assert main(["tag", "--session", "sess-2", "build"]) == 0
    capsys.readouterr()

    assert main(["sessions", "--tag", "build"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2
    assert "sess-2" in out[1]
    assert "sess-1" not in out


def test_sessions_empty_home(home, capsys):
    assert main(["sessions"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["session\tlabel\ttags\tstate\tlast_seen"]


def test_sessions_is_a_read_over_the_store(home, capsys):
    # the list reflects store facts, never mutating them: two runs
    # over the same home answer identically
    seed_spool(home, "sess-1")
    assert main(["sessions"]) == 0
    before = store(home).sessions()
    assert main(["sessions"]) == 0
    assert store(home).sessions() == before


def test_sessions_json_flag(home, capsys):
    seed_spool(home, "sess-1")
    main(["tag", "--session", "sess-1", "build"])
    capsys.readouterr()
    assert main(["sessions", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert data[0]["session_id"] == "sess-1"
    assert data[0]["tags"] == ["build"]
