"""Tests for the zcode wake driver (tugline.core.wake).

The zcode driver resumes an idle zcode session through the
app-bundled CLI: node <ZCode.app>/Contents/Resources/glm/zcode.cjs
--resume <session> -p "<reply body>" --cwd <project dir>. Every flag
here was verified against `zcode.cjs --help` (v0.16.9): -p runs a
single prompt without opening the TUI, --resume takes a sessionId,
--cwd sets the working directory, and --mode is deliberately NOT
passed because the documented default for --prompt is yolo. The real
zcode.cjs (and the real node) is never spawned by a test: unit tests
fake the spawn, the one real-Popen test points both the interpreter
and the CLI slot at fake shell scripts.
"""

import json
import os
import stat

from tugline.core import config as cfg
from tugline.core import spool
from tugline.core.store import Store
from tugline.core.wake import DEFAULT_ZCODE_COMMAND, WakeDispatcher

ZCODE_CJS = "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"


def make_config(wake=None, wake_enabled=True):
    if wake is None:
        # real dispatch semantics for unit tests: spawns are faked
        # everywhere they run; the ship default dry_run = true lives
        # in test_wake_config.
        wake = cfg.WakeConfig(dry_run=False)
    return cfg.Config(
        server=cfg.ServerConfig("http://127.0.0.1:2599", None),
        topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
        policy=cfg.PolicyConfig(600),
        toggles=cfg.TogglesConfig(True, True, False, True, True, wake_enabled),
        path="/none",
        wake=wake,
    )


def seed_store(tmp_path, event="stop", adapter="zcode", session_id="sess-z1",
               cwd="/tmp/zcode-proj"):
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
        extra={"adapter": adapter, "cwd": cwd, "project": "zcode-proj"},
    )
    store = Store(str(tmp_path / "store" / "tugline.db"))
    store.ingest_spool(root)
    return store


def append_reply(tmp_path, rid="rep1", session_id="sess-z1",
                 body="do the zcode thing",
                 consumed=False, ts="2026-10-14T08:00:00+00:00"):
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


def test_default_zcode_command_is_the_verified_flag_order():
    assert DEFAULT_ZCODE_COMMAND == [
        "node",
        ZCODE_CJS,
        "--resume",
        "{session}",
        "-p",
        "{body}",
        "--cwd",
        "{cwd}",
    ]
    # --mode is deliberately absent: the documented default for
    # --prompt is yolo, so the driver never pins a mode
    assert "--mode" not in DEFAULT_ZCODE_COMMAND


# -- dispatch on an idle zcode session --


def test_wake_dispatches_idle_zcode_session(tmp_path):
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
        "node",
        ZCODE_CJS,
        "--resume",
        "sess-z1",
        "-p",
        "do the zcode thing",
        "--cwd",
        "/tmp/zcode-proj",
    ]
    assert call["cwd"] == "/tmp/zcode-proj"
    assert "wake dispatched: session sess-z1 adapter zcode reply rep1" in logs


def test_zcode_wake_marks_reply_consumed_on_dispatch(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    dispatcher = make_dispatcher(tmp_path, spawn=FakeSpawn())

    dispatcher.poll()

    assert read_reply_lines(tmp_path)[0]["consumed"] is True


def test_zcode_cwd_flag_falls_back_to_home_when_row_lacks_it(tmp_path):
    # a store row from before the cwd enrichment has no cwd: the
    # --cwd flag still gets a real directory instead of an empty
    # string (the daemon's own cwd would be meaningless to a resumed
    # session)
    seed_store(tmp_path, cwd="")
    append_reply(tmp_path)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 1
    argv = spawn.calls[0]["argv"]
    assert argv[argv.index("--cwd") + 1] == os.path.expanduser("~")


# -- rails: dedupe, idle-only, toggle-off --


def test_zcode_wake_dedupes_on_reply_id(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 1
    assert dispatcher.poll() == 0
    assert len(spawn.calls) == 1


def test_busy_zcode_session_is_not_woken(tmp_path):
    seed_store(tmp_path, event="userpromptsubmit")
    append_reply(tmp_path)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 0
    assert spawn.calls == []
    # the reply stays consumable by the injection path
    assert read_reply_lines(tmp_path)[0]["consumed"] is False


def test_zcode_wake_off_disables_dispatch(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(
        tmp_path, config=make_config(wake_enabled=False), spawn=spawn
    )

    assert dispatcher.poll() == 0
    assert spawn.calls == []
    assert read_journal(tmp_path) == []


def test_zcode_adapter_disabled_queues_for_injection(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    config = make_config(wake=cfg.WakeConfig(adapters=["claude"],
                                             dry_run=False))
    spawn = FakeSpawn()
    logs = []
    dispatcher = make_dispatcher(tmp_path, config=config, spawn=spawn,
                                 log=logs.append)

    assert dispatcher.poll() == 0
    assert spawn.calls == []
    assert (
        "wake unsupported for adapter zcode,"
        " reply queued for next-boundary injection" in logs
    )
    events = [e["event"] for e in read_journal(tmp_path)]
    assert events == ["unsupported"]


# -- the unsupported fallback still covers unknown adapters --


def test_unknown_adapter_still_queues_for_injection(tmp_path):
    # with zcode now a driver, the fallback rail needs an adapter no
    # build will ever know: one with no driver entry at all
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
    assert read_reply_lines(tmp_path)[0]["consumed"] is False
    events = [e["event"] for e in read_journal(tmp_path)]
    assert events == ["unsupported"]


# -- dry run records the exact command --


def test_zcode_dry_run_records_the_command_without_spawning(tmp_path):
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    config = make_config(wake=cfg.WakeConfig(dry_run=True))
    dispatcher = make_dispatcher(tmp_path, config=config, spawn=spawn)

    assert dispatcher.poll() == 1
    assert spawn.calls == []
    assert read_reply_lines(tmp_path)[0]["consumed"] is False
    events = read_journal(tmp_path)
    assert events[0]["event"] == "dispatched"
    assert events[0]["dry_run"] is True
    assert events[0]["adapter"] == "zcode"
    assert events[0]["command"] == [
        "node",
        ZCODE_CJS,
        "--resume",
        "sess-z1",
        "-p",
        "do the zcode thing",
        "--cwd",
        "/tmp/zcode-proj",
    ]


# -- the real spawn path with a fake node and a fake CLI script --


def test_real_spawn_runs_a_fake_node_and_fake_cli_script(tmp_path):
    seed_store(tmp_path, cwd=str(tmp_path))
    append_reply(tmp_path, body="hello from the zcode test")
    out = tmp_path / "driver-out.txt"
    fake_cli = tmp_path / "fake-zcode.cjs"
    fake_cli.write_text("# not the real CLI, never executed\n",
                        encoding="utf-8")
    # the fake interpreter stands in for node: it records its own argv
    # so the test proves the exact flag shape zcode.cjs would receive
    fake_node = tmp_path / "fake-node"
    fake_node.write_text(
        "#!/bin/sh\n" 'printf "%s\\n" "$@" > ' + f'"{out}"' + "\n",
        encoding="utf-8",
    )
    os.chmod(fake_node, os.stat(fake_node).st_mode | stat.S_IXUSR)
    # the command template points at fakes only: neither the real
    # node nor the real zcode.cjs is ever spawned by a test
    config = make_config(
        wake=cfg.WakeConfig(
            zcode_command=[
                str(fake_node),
                str(fake_cli),
                "--resume",
                "{session}",
                "-p",
                "{body}",
                "--cwd",
                "{cwd}",
            ],
            dry_run=False,
        )
    )
    dispatcher = make_dispatcher(tmp_path, config=config)  # real Popen

    assert dispatcher.poll() == 1
    import time

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        dispatcher.poll()
        if out.exists() and not dispatcher.inflight():
            break
        time.sleep(0.05)
    assert not dispatcher.inflight()
    argv = out.read_text().strip().split("\n")
    assert argv == [
        str(fake_cli),
        "--resume",
        "sess-z1",
        "-p",
        "hello from the zcode test",
        "--cwd",
        str(tmp_path),
    ]
    events = [e["event"] for e in read_journal(tmp_path)]
    assert events[0] == "dispatched"
    assert events[-1] == "finished"
