"""Tests for the wake-on-reply dispatcher (tugline.core.wake).

The dispatcher watches replies.jsonl for entries bound to a session
(only the console writes those, carrying session_id) and, when the
target session's adapter has a wake driver and the session looks idle,
spawns the driver with the reply body as the prompt, resuming that
session. Everything here runs against a fake spawn or a fake driver
script: the real claude binary is never spawned by a test.
"""

import json
import os
import stat
import subprocess

import pytest

from tugline.core import config as cfg
from tugline.core import spool
from tugline.core.store import Store
from tugline.core.wake import DEFAULT_CLAUDE_COMMAND, WakeDispatcher


def make_config(wake=None, wake_enabled=True):
    if wake is None:
        # real dispatch semantics for unit tests: spawns are faked
        # (or pointed at a fake driver script) everywhere they run.
        # The ship default dry_run = true lives in test_wake_config.
        wake = cfg.WakeConfig(dry_run=False)
    return cfg.Config(
        server=cfg.ServerConfig("http://127.0.0.1:2599", None),
        topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
        policy=cfg.PolicyConfig(600),
        toggles=cfg.TogglesConfig(True, True, False, True, True, wake_enabled),
        path="/none",
        wake=wake,
    )


def seed_store(tmp_path, event="stop", adapter="claude", session_id="sess-1",
               cwd="/tmp/wake-proj"):
    """A real store holding one session with the given last event."""
    root = str(tmp_path / "spool")
    spool.append(
        root,
        event=event,
        session_id=session_id,
        topic="tug",
        priority="default",
        title="t",
        body="b",
        extra={"adapter": adapter, "cwd": cwd, "project": "wake-proj"},
    )
    store = Store(str(tmp_path / "store" / "tugline.db"))
    store.ingest_spool(root)
    return store


def append_reply(tmp_path, rid="rep1", session_id="sess-1", body="do the thing",
                 consumed=False, ts="2026-10-14T07:00:00+00:00"):
    entry = {
        "id": rid,
        "ts": ts,
        "topic": "tug-reply",
        "title": "console reply",
        "body": body,
        "consumed": consumed,
        "source": "console",
    }
    if session_id is not None:
        entry["session_id"] = session_id
    path = tmp_path / "replies.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    return entry


class FakeProc:
    """Popen-shaped fake: silent until released, then an exit code."""

    def __init__(self, exit_code=0):
        self.exit_code = exit_code
        self.released = False
        self.killed = False

    def release(self):
        self.released = True

    def poll(self):
        if self.released or self.killed:
            return self.exit_code
        return None

    def kill(self):
        self.killed = True
        self.exit_code = -9
        self.released = True

    def wait(self, timeout=None):
        return self.poll()


class FakeSpawn:
    def __init__(self, exit_code=0):
        self.calls = []
        self.procs = []
        self.exit_code = exit_code

    def __call__(self, argv, cwd=None):
        self.calls.append({"argv": list(argv), "cwd": cwd})
        proc = FakeProc(self.exit_code)
        self.procs.append(proc)
        return proc


def make_dispatcher(tmp_path, config=None, spawn=None, log=None, now=None):
    return WakeDispatcher(
        config or make_config(),
        replies_path=str(tmp_path / "replies.jsonl"),
        state_root=str(tmp_path / "state"),
        store_path=str(tmp_path / "store" / "tugline.db"),
        spawn=spawn,
        log=log if log is not None else (lambda line: None),
        now=now,
    )


def read_journal(tmp_path):
    path = tmp_path / "state" / "wake.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def read_reply_lines(tmp_path):
    path = tmp_path / "replies.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# -- command template --


def test_default_claude_command_is_the_verified_flag_order():
    assert DEFAULT_CLAUDE_COMMAND == [
        "/Users/shahil/.local/bin/claude",
        "--resume",
        "{session}",
        "-p",
        "{body}",
    ]


# -- dispatch on an idle session --


def test_wake_dispatches_idle_claude_session(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    logs = []
    dispatcher = make_dispatcher(tmp_path, spawn=spawn, log=logs.append)

    dispatched = dispatcher.poll()

    assert dispatched == 1
    assert len(spawn.calls) == 1
    call = spawn.calls[0]
    assert call["argv"] == [
        "/Users/shahil/.local/bin/claude",
        "--resume",
        "sess-1",
        "-p",
        "do the thing",
    ]
    assert call["cwd"] == "/tmp/wake-proj"
    assert "wake dispatched: session sess-1 adapter claude reply rep1" in logs


def test_wake_marks_reply_consumed_on_dispatch(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    dispatcher = make_dispatcher(tmp_path, spawn=FakeSpawn())

    dispatcher.poll()

    lines = read_reply_lines(tmp_path)
    assert lines[0]["consumed"] is True


def test_wake_ignores_unbound_replies(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path, rid="free1", session_id=None)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 0
    assert spawn.calls == []
    assert read_journal(tmp_path) == []


def test_wake_ignores_already_consumed_replies(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path, consumed=True)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 0
    assert spawn.calls == []


def test_wake_unknown_session_skips_without_crashing(tmp_path):
    # no store seeded at all: every bound reply is a skip, never a crash
    append_reply(tmp_path)
    spawn = FakeSpawn()
    logs = []
    dispatcher = make_dispatcher(tmp_path, spawn=spawn, log=logs.append)

    assert dispatcher.poll() == 0
    assert spawn.calls == []
    assert any("wake skipped" in line for line in logs)


# -- adapter support --


def test_wake_unsupported_adapter_queues_for_injection(tmp_path):
    # zcode gained its own driver (test_wake_zcode), so the
    # unsupported rail runs on an adapter no build will know
    seed_store(tmp_path, adapter="cursor")
    append_reply(tmp_path)
    spawn = FakeSpawn()
    logs = []
    dispatcher = make_dispatcher(tmp_path, spawn=spawn, log=logs.append)

    assert dispatcher.poll() == 0
    assert spawn.calls == []
    assert (
        "wake unsupported for adapter cursor,"
        " reply queued for next-boundary injection" in logs
    )
    # the reply stays consumable by the injection path
    assert read_reply_lines(tmp_path)[0]["consumed"] is False
    events = [e["event"] for e in read_journal(tmp_path)]
    assert events == ["unsupported"]


def test_wake_adapter_not_enabled_queues_for_injection(tmp_path):
    seed_store(tmp_path, adapter="claude")
    append_reply(tmp_path)
    config = make_config(wake=cfg.WakeConfig(adapters=[]))
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, config=config, spawn=spawn)

    assert dispatcher.poll() == 0
    assert spawn.calls == []
    events = [e["event"] for e in read_journal(tmp_path)]
    assert events == ["unsupported"]


# -- idleness --


def test_busy_session_is_not_woken(tmp_path):
    seed_store(tmp_path, event="userpromptsubmit")
    append_reply(tmp_path)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 0
    assert spawn.calls == []
    assert read_reply_lines(tmp_path)[0]["consumed"] is False


def test_waiting_session_is_not_woken(tmp_path):
    seed_store(tmp_path, event="permissionrequest")
    append_reply(tmp_path)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 0
    assert spawn.calls == []


def test_sessionstart_counts_as_idle(tmp_path):
    seed_store(tmp_path, event="sessionstart")
    append_reply(tmp_path)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 1
    assert len(spawn.calls) == 1


# -- rails: dedupe, one per session, cap, timeout --


def test_wake_dedupes_on_reply_id(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 1
    assert dispatcher.poll() == 0
    assert len(spawn.calls) == 1


def test_one_wake_in_flight_per_session(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path, rid="rep1")
    append_reply(tmp_path, rid="rep2", ts="2026-10-14T07:00:01+00:00")
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 1
    assert len(spawn.calls) == 1
    # the second reply for the same session waits for the first wake
    assert dispatcher.poll() == 0
    assert len(spawn.calls) == 1
    events = [e["event"] for e in read_journal(tmp_path)]
    assert "skipped" in events


def release_all(spawn):
    for proc in spawn.procs:
        proc.release()


def test_wake_finish_logs_exit_code_and_duration(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn(exit_code=3)
    logs = []
    clock = {"t": 100.0}
    dispatcher = make_dispatcher(
        tmp_path, spawn=spawn, log=logs.append, now=lambda: clock["t"]
    )

    dispatcher.poll()
    clock["t"] += 42.0
    release_all(spawn)
    dispatcher.poll()

    assert "wake finished: exit 3 after 42s" in logs
    events = read_journal(tmp_path)
    assert events[-1]["event"] == "finished"
    assert events[-1]["exit_code"] == 3
    assert events[-1]["duration_s"] == 42.0


def test_global_cap_defers_second_session_wake(tmp_path):
    seed_store(tmp_path, session_id="sess-1")
    seed_store(tmp_path, session_id="sess-2")
    append_reply(tmp_path, rid="rep1", session_id="sess-1")
    append_reply(
        tmp_path, rid="rep2", session_id="sess-2",
        ts="2026-10-14T07:00:01+00:00",
    )
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    # default cap is 1: the first session wakes, the second defers
    assert dispatcher.poll() == 1
    assert len(spawn.calls) == 1
    assert dispatcher.poll() == 0
    assert len(spawn.calls) == 1

    # once the first wake finishes, the deferred reply dispatches in
    # the same pass that reaps the finished one
    release_all(spawn)
    assert dispatcher.poll() == 1
    assert len(spawn.calls) == 2
    assert spawn.calls[1]["argv"][2] == "sess-2"


def test_timeout_kills_and_logs(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    logs = []
    clock = {"t": 1000.0}
    config = make_config(
        wake=cfg.WakeConfig(timeout_seconds=30.0, dry_run=False)
    )
    dispatcher = make_dispatcher(
        tmp_path, config=config, spawn=spawn, log=logs.append,
        now=lambda: clock["t"],
    )

    dispatcher.poll()
    assert len(spawn.calls) == 1
    clock["t"] += 31.0
    dispatcher.poll()

    assert any("wake timed out" in line for line in logs)
    events = read_journal(tmp_path)
    assert events[-1]["event"] == "timed_out"


# -- dry run and the master toggle --


def test_dry_run_records_the_command_without_spawning(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    logs = []
    config = make_config(wake=cfg.WakeConfig(dry_run=True))
    dispatcher = make_dispatcher(tmp_path, config=config, spawn=spawn,
                                 log=logs.append)

    assert dispatcher.poll() == 1
    assert spawn.calls == []
    assert any(
        "wake dispatched: session sess-1 adapter claude reply rep1" in line
        for line in logs
    )
    # nothing was really delivered, so the injection path stays armed
    assert read_reply_lines(tmp_path)[0]["consumed"] is False
    events = read_journal(tmp_path)
    assert events[0]["event"] == "dispatched"
    assert events[0]["dry_run"] is True
    assert events[0]["command"] == [
        "/Users/shahil/.local/bin/claude",
        "--resume",
        "sess-1",
        "-p",
        "do the thing",
    ]


def test_dry_run_dedupes_so_it_does_not_relog_every_poll(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    dispatcher = make_dispatcher(
        tmp_path, config=make_config(wake=cfg.WakeConfig(dry_run=True)),
        spawn=FakeSpawn(),
    )

    assert dispatcher.poll() == 1
    assert dispatcher.poll() == 0
    assert len(read_journal(tmp_path)) == 1


def test_wake_off_disables_the_dispatcher(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(
        tmp_path, config=make_config(wake_enabled=False), spawn=spawn
    )

    assert dispatcher.poll() == 0
    assert spawn.calls == []
    assert read_journal(tmp_path) == []


# -- failure posture --


def test_spawn_failure_never_crashes(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    logs = []

    def exploding_spawn(argv, cwd=None):
        raise OSError("no such binary")

    dispatcher = make_dispatcher(tmp_path, spawn=exploding_spawn,
                                 log=logs.append)

    assert dispatcher.poll() == 0
    assert any("wake dispatch failed" in line for line in logs)
    # and the next poll still works (nothing wedged)
    assert dispatcher.poll() == 0


def test_corrupt_journal_lines_are_ignored(tmp_path):
    seed_store(tmp_path)
    path = tmp_path / "replies.jsonl"
    path.write_text("not json at all\n", encoding="utf-8")
    append_reply(tmp_path)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 1
    assert len(spawn.calls) == 1


# -- the journal survives a restart --


def test_seen_replies_are_relearned_from_the_journal(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    make_dispatcher(tmp_path, spawn=spawn).poll()
    assert len(spawn.calls) == 1

    # a fresh dispatcher (daemon restart) must not wake the same reply again
    second = FakeSpawn()
    make_dispatcher(tmp_path, spawn=second).poll()
    assert second.calls == []


# -- the real spawn path with a fake driver script --


def test_real_spawn_runs_a_fake_driver_script(tmp_path):
    seed_store(tmp_path, cwd=str(tmp_path))
    append_reply(tmp_path, body="hello from the test")
    out = tmp_path / "driver-out.txt"
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\n" 'printf "%s\\n" "$@" > ' + f'"{out}"' + "\n",
        encoding="utf-8",
    )
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
    # the command template points at the fake driver, never the real
    # claude binary (standing rail: no real-binary spawns in tests)
    config = make_config(
        wake=cfg.WakeConfig(
            claude_command=[str(script), "--resume", "{session}", "-p", "{body}"],
            dry_run=False,
        )
    )
    dispatcher = make_dispatcher(tmp_path, config=config)  # real Popen spawn

    assert dispatcher.poll() == 1
    # the driver finished on its own; wait it out (a real process takes
    # a few milliseconds), then reap so the journal closes out
    import time

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        dispatcher.poll()
        if out.exists() and not dispatcher.inflight():
            break
        time.sleep(0.05)
    assert not dispatcher.inflight()
    argv = out.read_text().strip().split("\n")
    assert argv == ["--resume", "sess-1", "-p", "hello from the test"]
    events = [e["event"] for e in read_journal(tmp_path)]
    assert events[0] == "dispatched"
    assert events[-1] == "finished"
    assert all(
        e.get("command", [str(script)])[0] == str(script)
        for e in read_journal(tmp_path)
        if "command" in e
    )


def test_real_spawn_missing_binary_is_a_logged_failure(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    config = make_config(
        wake=cfg.WakeConfig(
            claude_command=["/nonexistent/wake-binary", "{session}", "{body}"],
            dry_run=False,
        )
    )
    logs = []
    dispatcher = make_dispatcher(tmp_path, config=config, log=logs.append)

    assert dispatcher.poll() == 0
    assert any("wake dispatch failed" in line for line in logs)
    events = [e["event"] for e in read_journal(tmp_path)]
    assert events == ["error"]


# -- daemon wiring --


def test_daemon_runs_the_wake_poll_each_cycle(fake_ntfy, tmp_path):
    from tugline.core.daemon import Daemon

    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    config = make_config()
    daemon = Daemon(
        config,
        spool_root=str(tmp_path / "spool"),
        replies_path=str(tmp_path / "replies.jsonl"),
        state_root=str(tmp_path / "state"),
        poll_timeout=2.0,
        backoff_base=0.01,
        wake=WakeDispatcher(
            config,
            replies_path=str(tmp_path / "replies.jsonl"),
            state_root=str(tmp_path / "state"),
            store_path=str(tmp_path / "store" / "tugline.db"),
            spawn=spawn,
        ),
    )

    result = daemon.run_once()

    # the seed stop record flushes (suppressed, nothing published) and
    # the wake dispatcher ran its pass in the same cycle
    assert result["handled"] == 1
    assert result["wakes"] == 1
    assert len(spawn.calls) == 1


def test_daemon_builds_a_dispatcher_when_wake_is_on(fake_ntfy, tmp_path):
    from tugline.core.daemon import Daemon

    daemon = Daemon(
        make_config(),
        spool_root=str(tmp_path / "spool"),
        replies_path=str(tmp_path / "replies.jsonl"),
        state_root=str(tmp_path / "state"),
    )
    assert daemon._wake is not None


def test_daemon_has_no_dispatcher_when_wake_is_off(fake_ntfy, tmp_path):
    from tugline.core.daemon import Daemon

    daemon = Daemon(
        make_config(wake_enabled=False),
        spool_root=str(tmp_path / "spool"),
        replies_path=str(tmp_path / "replies.jsonl"),
        state_root=str(tmp_path / "state"),
    )
    assert daemon._wake is None


def test_daemon_serve_cycle_polls_the_dispatcher(fake_ntfy, tmp_path):
    from tugline.core.daemon import Daemon

    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    config = make_config()
    config.toggles.replies = False  # keep the cycle to flush and wake
    dispatcher = WakeDispatcher(
        config,
        replies_path=str(tmp_path / "replies.jsonl"),
        state_root=str(tmp_path / "state"),
        store_path=str(tmp_path / "store" / "tugline.db"),
        spawn=spawn,
    )
    daemon = Daemon(
        config,
        spool_root=str(tmp_path / "spool"),
        replies_path=str(tmp_path / "replies.jsonl"),
        state_root=str(tmp_path / "state"),
        poll_timeout=2.0,
        backoff_base=0.01,
        cycle_idle=0.0,
        wake=dispatcher,
    )
    original = dispatcher.poll
    polls = []

    def counting():
        result = original()
        polls.append(result)
        if spawn.calls or len(polls) >= 20:  # never spin forever
            daemon.request_stop()
        return result

    dispatcher.poll = counting
    daemon.serve()
    assert polls  # the serve loop reached the wake poll
    assert len(spawn.calls) == 1
