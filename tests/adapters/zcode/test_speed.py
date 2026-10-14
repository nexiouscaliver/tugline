"""End-to-end speed check for the installed hook command.

The zcode harness runs hooks inline: latency adds 1:1 to turn time, so
the installed command must finish well inside 100 ms with a real stdin
pipe. This measures the actual console binary from the project venv
(the same entry an installed hook config runs), one warmup run for the
bytecode cache, then three measured runs.

A busy machine can starve every sample in one window (a full-suite run
on a loaded laptop measured 622/662/399 ms here while the same command
holds ~47 ms steady-state), so a round that misses the budget gets one
settle-and-retry; the verdict itself never moves: best of the final
round must stay under 100 ms.
"""

import os
import subprocess
import sys
import time

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


def test_installed_hook_command_under_100ms(tmp_path):
    tug = os.path.join(sys.prefix, "bin", "tug")
    assert os.path.exists(tug), "expected the tug console script in %s" % tug

    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(CONFIG_TEXT, encoding="utf-8")

    env = dict(os.environ)
    env["TUGLINE_ZCODE_HOME"] = str(home)
    env["TUGLINE_CONFIG"] = str(config)

    payload = '{"hookEventName":"Stop","sessionId":"s-speed"}'
    argv = [tug, "hook-zcode", "Stop"]

    def once():
        start = time.perf_counter()
        result = subprocess.run(
            argv, input=payload, capture_output=True, text=True, env=env
        )
        elapsed = (time.perf_counter() - start) * 1000.0
        return elapsed, result

    warm, warm_result = once()
    assert warm_result.returncode == 0, warm_result.stderr
    assert warm_result.stdout == ""

    def measure():
        timings = []
        for _ in range(3):
            elapsed, result = once()
            assert result.returncode == 0, result.stderr
            timings.append(elapsed)
        return timings

    timings = measure()
    if min(timings) >= 100.0:
        # let a transient machine-load spike pass, then try once more;
        # a real regression misses the budget in both rounds
        time.sleep(1.0)
        timings = measure()

    print(
        "hook-zcode wall time: warmup %.1f ms, measured %s ms"
        % (warm, ", ".join("%.1f" % t for t in timings))
    )
    assert min(timings) < 100.0, "best of 3 was %.1f ms" % min(timings)
