"""Tests for driver executable resolution in the wake dispatcher
(tugline.core.wake).

The daemon runs under launchd, where PATH=/usr/bin:/bin:/usr/sbin:
/sbin and a bare "node" argv[0] (the zcode driver template) is
unfindable: the first live wake dispatches died with "[Errno 2] No
such file or directory: 'node'" while the same command worked from
every interactive shell, because interactive PATHs carry Homebrew.
resolve_executable bridges that gap by searching PATH plus
EXTRA_EXEC_PATHS before the journal or the spawn sees the command.

Every test here pins the machine out of the picture: the env PATH
and tugline.core.wake.EXTRA_EXEC_PATHS are pointed at fixture dirs
holding a tiny fake node script, never the real /opt/homebrew and
never whatever node the host happens to have.
"""

import json
import os
import stat
import subprocess

from tugline.core import config as cfg
from tugline.core import spool
from tugline.core import wake as wake_mod
from tugline.core.store import Store
from tugline.core.wake import WakeDispatcher, resolve_executable

ZCODE_CJS = "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"


def make_config(wake=None, wake_enabled=True):
    if wake is None:
        wake = cfg.WakeConfig(dry_run=False)
    return cfg.Config(
        server=cfg.ServerConfig("http://127.0.0.1:2599", None),
        topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
        policy=cfg.PolicyConfig(600),
        toggles=cfg.TogglesConfig(True, True, False, True, True, wake_enabled),
        path="/none",
        wake=wake,
    )


def seed_store(tmp_path, event="stop", adapter="zcode", session_id="sess-x1",
               cwd="/tmp/exec-proj"):
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
        extra={"adapter": adapter, "cwd": cwd, "project": "exec-proj"},
    )
    store = Store(str(tmp_path / "store" / "tugline.db"))
    store.ingest_spool(root)
    return store


def append_reply(tmp_path, rid="rep1", session_id="sess-x1",
                 body="do the exec thing",
                 consumed=False, ts="2026-10-15T07:00:00+00:00"):
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


def make_node_dir(parent, sub="node-bin", name="node"):
    """A fixture dir holding a tiny executable script standing in for
    the driver binary. Never the real node."""
    bindir = parent / sub
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
    return bindir


def empty_dir(parent, sub="empty"):
    d = parent / sub
    d.mkdir(exist_ok=True)
    return d


# -- resolve_executable: the pure function --


def test_resolve_finds_a_bare_name_via_env_path(tmp_path, monkeypatch):
    bindir = make_node_dir(tmp_path)
    monkeypatch.setattr(wake_mod, "EXTRA_EXEC_PATHS", [])

    resolved = resolve_executable(
        ["node", "-p", "hi"], env={"PATH": str(bindir)}
    )

    assert resolved == [str(bindir / "node"), "-p", "hi"]


def test_resolve_finds_a_bare_name_via_extra_roots_when_path_is_empty(
    tmp_path, monkeypatch
):
    # launchd-like: the env PATH knows nothing about the driver, the
    # extra roots carry it (the W13 production fix)
    bindir = make_node_dir(tmp_path)
    monkeypatch.setattr(wake_mod, "EXTRA_EXEC_PATHS", [str(bindir)])

    resolved = resolve_executable(["node", "-v"], env={"PATH": ""})

    assert resolved == [str(bindir / "node"), "-v"]


def test_absolute_argv0_passes_through_untouched(tmp_path, monkeypatch):
    # an explicit path is the config author's choice, existence or
    # not; the spawn error path owns a wrong one
    bindir = make_node_dir(tmp_path)
    monkeypatch.setattr(wake_mod, "EXTRA_EXEC_PATHS", [str(bindir)])
    argv = ["/definitely/not/installed/driver", "-p", "x"]

    assert resolve_executable(argv, env={"PATH": str(bindir)}) == argv


def test_unresolvable_bare_name_returns_argv_unchanged(
    tmp_path, monkeypatch
):
    nowhere = empty_dir(tmp_path)
    monkeypatch.setattr(wake_mod, "EXTRA_EXEC_PATHS", [str(nowhere)])
    argv = ["node", "-v"]

    assert resolve_executable(argv, env={"PATH": str(nowhere)}) == argv


def test_empty_argv_returns_unchanged():
    assert resolve_executable([]) == []


# -- the dispatcher resolves before journalling or spawning --


def test_real_dispatch_spawns_the_resolved_absolute_path(
    tmp_path, monkeypatch
):
    # the production shape: launchd PATH has no node, the extra roots
    # do, and the spawn must receive the absolute path or die with
    # "[Errno 2] No such file or directory: 'node'"
    extra = make_node_dir(tmp_path, sub="extra-roots")
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(wake_mod, "EXTRA_EXEC_PATHS", [str(extra)])
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    dispatcher = make_dispatcher(tmp_path, spawn=spawn)

    assert dispatcher.poll() == 1
    assert spawn.calls[0]["argv"][0] == str(extra / "node")
    # the journal records the same resolved command the spawn got
    events = read_journal(tmp_path)
    assert events[0]["event"] == "dispatched"
    assert events[0]["command"][0] == str(extra / "node")
    assert "argv0_resolved" not in events[0]


def test_dry_run_journals_the_resolved_absolute_path(tmp_path, monkeypatch):
    bindir = make_node_dir(tmp_path)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(wake_mod, "EXTRA_EXEC_PATHS", [])
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    logs = []
    config = make_config(wake=cfg.WakeConfig(dry_run=True))
    dispatcher = make_dispatcher(
        tmp_path, config=config, spawn=spawn, log=logs.append
    )

    assert dispatcher.poll() == 1
    assert spawn.calls == []
    events = read_journal(tmp_path)
    assert events[0]["event"] == "dispatched"
    assert events[0]["dry_run"] is True
    assert events[0]["command"][0] == str(bindir / "node")
    assert events[0]["command"][1] == ZCODE_CJS
    assert "argv0_resolved" not in events[0]
    assert not any("unresolvable" in line for line in logs)


def test_dry_run_flags_and_warns_when_driver_is_unresolvable(
    tmp_path, monkeypatch
):
    # a dry run is a preflight: it must say when the dispatch it
    # previews would die on spawn
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(wake_mod, "EXTRA_EXEC_PATHS", [])
    seed_store(tmp_path)
    append_reply(tmp_path)
    spawn = FakeSpawn()
    logs = []
    config = make_config(wake=cfg.WakeConfig(dry_run=True))
    dispatcher = make_dispatcher(
        tmp_path, config=config, spawn=spawn, log=logs.append
    )

    assert dispatcher.poll() == 1
    events = read_journal(tmp_path)
    # unresolved stays bare; the real spawn's OS error and its error
    # journaling own that failure, not the resolver
    assert events[0]["command"][0] == "node"
    assert events[0]["argv0_resolved"] is False
    assert any(
        "unresolvable" in line and "node" in line for line in logs
    )


# -- the real spawn path hands the child a usable PATH --


def test_default_spawn_passes_the_augmented_path_to_the_child(
    tmp_path, monkeypatch
):
    # fixing argv[0] alone would push the launchd gap one process
    # later: node's own children need a PATH that can find things
    # (the tug bin dirs carry their own coverage below)
    on_path = make_node_dir(tmp_path, sub="on-path")
    extra = make_node_dir(tmp_path, sub="extra-roots")
    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.setattr(wake_mod, "EXTRA_EXEC_PATHS", [str(extra)])
    monkeypatch.setattr(wake_mod, "tug_bin_dirs", lambda: [])
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        captured["argv"] = list(argv)
        return FakeProc()

    monkeypatch.setattr(wake_mod.subprocess, "Popen", fake_popen)

    wake_mod._default_spawn(["/usr/bin/true"])

    env = captured["env"]
    assert env is not os.environ  # a copy, not a mutation of the daemon's
    assert env["PATH"] == os.pathsep.join([str(on_path), str(extra)])
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    assert captured["stdin"] is subprocess.DEVNULL


# -- the tug bin dirs ride along for the child's hooks (W10-L23) --
#
# The hooks a woken session executes are "tug hook-claude <Event>"
# commands; under the daemon's launchd PATH a bare tug cannot resolve,
# so every hook in a wake-spawned session silently failed. The
# installer embeds absolute paths, and the spawn env carries the tug
# bin dirs as defense in depth.


def make_tug_dir(tmp_path, sub="tug-bin"):
    bindir = tmp_path / sub
    bindir.mkdir()
    tug = bindir / "tug"
    tug.write_text("#!/bin/sh\n", encoding="utf-8")
    tug.chmod(0o755)
    return bindir


def test_tug_bin_dirs_find_the_interpreter_sibling(tmp_path,
                                                   monkeypatch):
    bindir = make_tug_dir(tmp_path, sub="venv-bin")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(wake_mod.sys, "executable",
                        str(bindir / "python"))
    monkeypatch.setenv("HOME", str(fake_home))

    assert wake_mod.tug_bin_dirs() == [str(bindir)]


def test_augmented_path_carries_the_tug_bin_dirs(tmp_path,
                                                 monkeypatch):
    tugdir = make_tug_dir(tmp_path)
    monkeypatch.setattr(wake_mod, "EXTRA_EXEC_PATHS", [])
    monkeypatch.setattr(wake_mod, "tug_bin_dirs", lambda: [str(tugdir)])

    path = wake_mod._augmented_path({"PATH": "/usr/bin:/bin"})

    assert path == "/usr/bin:/bin" + os.pathsep + str(tugdir)


def test_default_spawn_env_includes_the_tug_bin_dir(tmp_path,
                                                    monkeypatch):
    tugdir = make_tug_dir(tmp_path)
    monkeypatch.setattr(wake_mod, "EXTRA_EXEC_PATHS", [])
    monkeypatch.setattr(wake_mod, "tug_bin_dirs", lambda: [str(tugdir)])
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(wake_mod.subprocess, "Popen", fake_popen)

    wake_mod._default_spawn(["/usr/bin/true"])

    assert captured["env"]["PATH"].endswith(os.pathsep + str(tugdir))
