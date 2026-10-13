"""CLI tests: subcommand registration, end-to-end run, speed ceiling.

The speed budget is the hook contract: Claude Code kills hooks at
their configured timeout, and a slow hook would stall the agent, so
the fast path (everything except a permission wait) has to answer in
well under 100 ms. The subprocess runs measure the real command end
to end; the in-process measurements time the hook body alone.
"""

import io
import json
import os
import subprocess
import sys
import time

from tugline.adapters.claude import hook as hook_module


def cli_env(home):
    env = dict(os.environ)
    env["TUGLINE_HOME"] = str(home)
    return env


def test_cli_lists_the_hook_claude_command(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "tugline.cli", "--help"],
        capture_output=True,
        text=True,
        env=cli_env(tmp_path / "home"),
        timeout=60,
    )
    assert result.returncode == 0
    assert "hook-claude" in result.stdout


def test_cli_hook_run_end_to_end(tmp_path):
    home = tmp_path / "home"
    payload = json.dumps(
        {"session_id": "sess-sub", "hook_event_name": "Stop", "cwd": "/tmp"}
    )
    result = subprocess.run(
        [sys.executable, "-m", "tugline.cli", "hook-claude", "Stop"],
        input=payload,
        capture_output=True,
        text=True,
        env=cli_env(home),
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    day_files = sorted((home / "spool").glob("*.jsonl"))
    assert len(day_files) == 1
    record = json.loads(day_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert record["event"] == "stop"
    assert record["session_id"] == "sess-sub"


def test_cli_hook_survives_garbage_stdin(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "tugline.cli", "hook-claude", "Stop"],
        input="}{ not json",
        capture_output=True,
        text=True,
        env=cli_env(tmp_path / "home"),
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_hook_fast_path_runs_under_100_ms(tug_home, hook_payload, capsys):
    payload = json.dumps(hook_payload("Stop"))
    assert hook_module.run_event("Stop", io.StringIO(payload)) == 0  # warm the dirs
    start = time.perf_counter()
    assert hook_module.run_event("Stop", io.StringIO(payload)) == 0
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert elapsed_ms < 100.0
    capsys.readouterr()


def test_hook_fast_path_stays_under_100_ms_across_runs(tug_home, hook_payload):
    payload = json.dumps(hook_payload("Stop"))
    hook_module.run_event("Stop", io.StringIO(payload))
    worst = 0.0
    for _ in range(3):
        start = time.perf_counter()
        hook_module.run_event("Stop", io.StringIO(payload))
        worst = max(worst, time.perf_counter() - start)
    assert worst < 0.100
