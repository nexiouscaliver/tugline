"""Wake-on-reply dispatcher (W8-L19).

The operator's ask: a reply must reach its idle session immediately,
not at whatever turn boundary comes next. W7-L17 already injects
replies into a RUNNING session at its next prompt; this module covers
the other half. The daemon calls WakeDispatcher.poll() once per cycle
after the spool flush:

1. Reap and timeout running wakes (one log line and journal entry
   each: dispatched, finished, timed_out).
2. Read the tail of replies.jsonl for entries that are unconsumed and
   bound to a session (only the console writes session_id on a reply
   line; free-form phone replies stay on the injection path).
3. For each new bound reply, exactly one decision, deduped on the
   reply id and relearned from the wake journal on restart:
   - the session's adapter has no driver or the adapter
     is not enabled: log "wake unsupported for adapter X, reply
     queued for next-boundary injection" and leave the reply
   unconsumed for the W7-L17 path;
   - the session is busy (last spooled event is not a stop or a
     session start, or a wake is already in flight for it): same
     fallback, no wake;
   - otherwise dispatch: spawn the adapter's driver with the reply
     body as the prompt, resuming that session, cwd from the store's
     session row, mark the reply consumed so the injection path never
     double-delivers it, and journal the exact command.

Safety rails, all mandatory: one wake per reply id, one wake in
flight per session, a global concurrent cap (default 1), a per-wake
timeout that kills the process (default 1800s), dry_run mode that
records the exact command without spawning anything (the ship
default; the controller arms it after live verification), and no
failure path may ever raise into the daemon.

Driver templates are config-driven (wake.claude_command,
wake.zcode_command). The claude flag order is verified against
claude --help v2.1.299, where the resume-with-prompt parse error
itself prints the usage: "claude -p --resume <session-id|title>".
The zcode flags are verified against the app-bundled CLI's own help
(v0.16.9: -p, --resume, --cwd; --mode left at its documented
--prompt default, yolo).

Bare driver names are resolved against PATH plus a few extra roots
(see EXTRA_EXEC_PATHS) before the journal or the spawn sees them:
the daemon runs under launchd, whose PATH=/usr/bin:/bin:/usr/sbin:
/sbin has no node, and both live wake dispatches died with
"[Errno 2] No such file or directory: 'node'" while the same
command worked from every interactive shell.

The store is probed read-only (the console's ingest thread keeps it
current within its interval); a missing, busy, or broken store means
no wakes, never a crash.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

# The verified claude resume-with-prompt form. Flag order checked
# against claude --help (v2.1.299): -r, --resume [value] resumes a
# conversation by session ID, -p, --print runs it non-interactively
# with the positional prompt.
DEFAULT_CLAUDE_COMMAND = [
    "/Users/shahil/.local/bin/claude",
    "--resume",
    "{session}",
    "-p",
    "{body}",
]

# The verified zcode resume-with-prompt form, run through node
# because the app ships a .cjs bundle, not an executable. Flags
# checked against the bundled CLI's own help (v0.16.9): -p, --prompt
# <text> runs a single prompt without opening the TUI, --resume
# <sessionId> resumes a persisted session, --cwd <path> sets the
# working directory. --mode is deliberately absent: help documents
# yolo as the default permission mode for --prompt. The path is the
# ZCode.app-bundled CLI, so it is version-locked to the installed
# app and moves with it; wake.zcode_command is the override seam.
DEFAULT_ZCODE_COMMAND = [
    "node",
    "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
    "--resume",
    "{session}",
    "-p",
    "{body}",
    "--cwd",
    "{cwd}",
]

# argv templates per adapter, from the wake config's command keys.
# Adapters without an entry take the "unsupported" path.
DRIVER_COMMAND_KEYS = {
    "claude": "claude_command",
    "zcode": "zcode_command",
}


def driver_command(adapter, wake_config):
    key = DRIVER_COMMAND_KEYS.get(adapter)
    if key is None:
        return None
    return list(getattr(wake_config, key))


REPLY_TAIL_BYTES = 262144  # the journal tail scanned for bound replies
JOURNAL_TAIL_BYTES = 262144  # the wake journal tail relearned at startup
STORE_TIMEOUT_S = 0.2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# The launchd PATH gap: daemon sessions get /usr/bin:/bin:/usr/sbin:
# /sbin and none of the shell's fragments, while Homebrew installs
# node under /opt/homebrew/bin (Apple Silicon) or /usr/local/bin
# (Intel). A bare "node" argv[0] (exactly what the zcode driver
# template spawns) is therefore unfindable under the daemon and fine
# in every interactive shell, which is why tests and manual runs
# never saw the production deaths. These roots are appended to PATH
# when resolving and spawning driver commands; the module-level
# list is the seam tests patch with fixture dirs.
EXTRA_EXEC_PATHS = ["/opt/homebrew/bin", "/usr/local/bin"]


def _augmented_path(env=None):
    """PATH from env (falling back to the process environment) plus
    the extra roots, empty components dropped: the os.pathsep-joined
    search string shutil.which consumes."""
    source = os.environ if env is None else env
    parts = [source.get("PATH") or ""]
    parts.extend(EXTRA_EXEC_PATHS)
    return os.pathsep.join([p for p in parts if p])


def resolve_executable(argv, env=None):
    """Resolve a bare argv[0] against the augmented PATH.

    A name carrying a path separator is already an explicit choice
    by the config author and passes through untouched; so does
    anything the augmented PATH cannot find, because the spawn then
    fails with the OS error and the existing error journaling owns
    that failure better than a guess here could. Only a bare name
    that resolves comes back rewritten as [absolute path] + rest."""
    if not argv or os.path.sep in argv[0]:
        return argv
    found = shutil.which(argv[0], path=_augmented_path(env))
    if not found:
        return argv
    return [found] + list(argv[1:])


def _default_spawn(argv, cwd=None):
    # the child inherits the augmented PATH, not just the resolved
    # argv[0]: node's own children (git shims, package-manager
    # helpers) would hit the same launchd gap one process later
    env = dict(os.environ)
    env["PATH"] = _augmented_path()
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


class WakeDispatcher:
    def __init__(
        self,
        config,
        replies_path,
        state_root,
        store_path,
        spawn=None,
        log=None,
        now=None,
    ):
        self.config = config
        self.replies_path = str(replies_path)
        self.state_root = str(state_root)
        self.store_path = str(store_path)
        self._spawn = spawn or _default_spawn
        self._log_fn = log or (lambda line: None)
        self._now = now or time.monotonic
        self.journal_path = os.path.join(self.state_root, "wake.jsonl")
        # reply ids already decided: seeded from the journal tail so a
        # daemon restart never re-wakes an old reply
        self._seen = self._seed_seen()
        # reply ids deferred by the global cap: retried every poll
        self._deferred = set()
        # session_id -> {"proc", "reply_id", "adapter", "started",
        #                "argv", "log"}
        self._inflight = {}

    # -- public surface --

    def poll(self) -> int:
        """One bounded pass. Returns the wake decisions dispatched
        this pass (real spawns plus dry runs). Never raises."""
        if not getattr(self.config.toggles, "wake", False):
            return 0
        try:
            return self._poll()
        except Exception as exc:  # the daemon must survive anything
            self.log("wake poll failed: %s" % (exc,))
            return 0

    def inflight(self) -> int:
        return len(self._inflight)

    def log(self, line):
        self._log_fn(line)

    # -- one pass --

    def _poll(self) -> int:
        self._reap()
        self._expire()
        dispatched = 0
        for entry in self._candidates():
            rid = entry["id"]
            if rid in self._seen:
                continue
            session_id = str(entry.get("session_id") or "")
            row = self._session_row(session_id)
            if row is None:
                self._decide(
                    entry, "skipped",
                    "wake skipped: session %s not in store,"
                    " reply queued for next-boundary injection" % session_id,
                    adapter="",
                )
                continue
            adapter = str(row.get("adapter") or "")
            command = driver_command(adapter, self.config.wake)
            if adapter not in self.config.wake.adapters:
                command = None
            if command is None:
                self._decide(
                    entry, "unsupported",
                    "wake unsupported for adapter %s,"
                    " reply queued for next-boundary injection"
                    % (adapter or "unknown"),
                    adapter=adapter,
                )
                continue
            if session_id in self._inflight:
                self._decide(
                    entry, "skipped",
                    "wake skipped: session %s already has a wake in flight,"
                    " reply queued for next-boundary injection" % session_id,
                    adapter=adapter,
                )
                continue
            if not self._idle(row):
                self._decide(
                    entry, "skipped",
                    "wake skipped: session %s is busy (%s),"
                    " reply queued for next-boundary injection"
                    % (session_id, row.get("state") or "running"),
                    adapter=adapter,
                )
                continue
            if len(self._inflight) >= max(1, self.config.wake.max_concurrent):
                # transient: the cap frees up when a wake finishes, so
                # this reply stays a candidate instead of being decided
                self._deferred.add(rid)
                continue
            if self._dispatch(entry, session_id, adapter, row, command):
                dispatched += 1
        return dispatched

    def _dispatch(self, entry, session_id, adapter, row, command) -> bool:
        rid = entry["id"]
        cwd = str(row.get("cwd") or "") or None
        # {cwd} feeds the zcode --cwd flag; a row from before the cwd
        # enrichment gets the user's home rather than an empty string
        # (the daemon's own cwd would be meaningless to a resumed
        # session). Templates without {cwd} never see it.
        cwd_value = cwd or os.path.expanduser("~")
        argv = [
            part.replace("{session}", session_id)
            .replace("{body}", str(entry.get("body") or ""))
            .replace("{cwd}", cwd_value)
            for part in command
        ]
        # resolved before the dry/real fork so the journal and the
        # spawn agree on one command: under launchd a bare argv[0]
        # like "node" is unfindable on the daemon's minimal PATH, so
        # it becomes an absolute path when the augmented PATH knows
        # it and stays bare (spawn error, existing error journaling)
        # when nothing does
        argv = resolve_executable(argv)
        dry = bool(self.config.wake.dry_run)
        if dry:
            self._seen.add(rid)
            record = {
                "event": "dispatched",
                "reply_id": rid,
                "reply_ts": str(entry.get("ts") or ""),
                "session_id": session_id,
                "adapter": adapter,
                "command": argv,
                "dry_run": True,
            }
            if os.path.sep not in argv[0]:
                # a dry run is a preflight: it previews the dispatch
                # that would really happen, so it must say when that
                # dispatch would die on spawn
                record["argv0_resolved"] = False
                self.log(
                    "wake dry run: driver %s unresolvable on PATH for"
                    " session %s reply %s; a real dispatch would fail"
                    " to spawn" % (argv[0], session_id, rid)
                )
            self._journal(record)
            self.log(
                "wake dispatched: session %s adapter %s reply %s (dry run)"
                % (session_id, adapter, rid)
            )
            return True
        try:
            proc = self._spawn(argv, cwd=cwd)
        except Exception as exc:
            self._seen.add(rid)
            self._journal(
                {
                    "event": "error",
                    "reply_id": rid,
                    "reply_ts": str(entry.get("ts") or ""),
                    "session_id": session_id,
                    "adapter": adapter,
                    "command": argv,
                    "error": str(exc),
                }
            )
            self.log("wake dispatch failed: session %s reply %s: %s"
                     % (session_id, rid, exc))
            return False
        self._seen.add(rid)
        self._deferred.discard(rid)
        self._inflight[session_id] = {
            "proc": proc,
            "reply_id": rid,
            "adapter": adapter,
            "started": self._now(),
            "argv": argv,
        }
        self._mark_reply_consumed(rid)
        self._journal(
            {
                "event": "dispatched",
                "reply_id": rid,
                "reply_ts": str(entry.get("ts") or ""),
                "session_id": session_id,
                "adapter": adapter,
                "command": argv,
            }
        )
        self.log(
            "wake dispatched: session %s adapter %s reply %s"
            % (session_id, adapter, rid)
        )
        return True

    # -- reaping, timeouts --

    def _reap(self):
        for session_id in list(self._inflight):
            wake = self._inflight[session_id]
            try:
                code = wake["proc"].poll()
            except Exception:
                code = -1
            if code is None:
                continue
            self._finish(session_id, wake, code)

    def _expire(self):
        timeout = float(self.config.wake.timeout_seconds)
        for session_id in list(self._inflight):
            wake = self._inflight[session_id]
            elapsed = self._now() - wake["started"]
            if elapsed < timeout:
                continue
            try:
                wake["proc"].kill()
            except Exception:
                pass
            self.log(
                "wake timed out: session %s reply %s after %ds, killed"
                % (session_id, wake["reply_id"], int(elapsed))
            )
            self._journal(
                {
                    "event": "timed_out",
                    "reply_id": wake["reply_id"],
                    "session_id": session_id,
                    "adapter": wake["adapter"],
                    "duration_s": round(elapsed, 3),
                }
            )
            del self._inflight[session_id]

    def _finish(self, session_id, wake, code):
        duration = self._now() - wake["started"]
        del self._inflight[session_id]
        self.log(
            "wake finished: exit %d after %ds"
            % (code, int(duration))
        )
        self._journal(
            {
                "event": "finished",
                "reply_id": wake["reply_id"],
                "session_id": session_id,
                "adapter": wake["adapter"],
                "exit_code": code,
                "duration_s": round(duration, 3),
            }
        )

    # -- decisions --

    def _decide(self, entry, event, message, adapter=""):
        """One terminal decision for a reply: journal it, log it, and
        mark the reply id decided. The reply itself stays unconsumed
        so the injection path still owns delivery."""
        rid = entry["id"]
        self._seen.add(rid)
        self._deferred.discard(rid)
        self._journal(
            {
                "event": event,
                "reply_id": rid,
                "reply_ts": str(entry.get("ts") or ""),
                "session_id": str(entry.get("session_id") or ""),
                "adapter": adapter,
            }
        )
        self.log(message)

    # -- inputs --

    def _candidates(self):
        """Unconsumed reply journal entries bound to a session, in
        file order, from a bounded tail."""
        try:
            size = os.path.getsize(self.replies_path)
            with open(self.replies_path, encoding="utf-8") as fh:
                if size > REPLY_TAIL_BYTES:
                    fh.seek(size - REPLY_TAIL_BYTES)
                    fh.readline()  # drop the possibly partial line
                text = fh.read()
        except OSError:
            return []
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("consumed"):
                continue
            if not entry.get("session_id"):
                continue
            if not entry.get("id"):
                continue
            out.append(entry)
        return out

    def _session_row(self, session_id):
        """One sessions row from the store, read-only; None when the
        store is missing, busy, or has never seen the session."""
        if not session_id:
            return None
        if not os.path.exists(self.store_path):
            return None
        try:
            uri = "file:%s?mode=ro" % urllib.request.pathname2url(self.store_path)
            conn = sqlite3.connect(uri, uri=True, timeout=STORE_TIMEOUT_S)
            try:
                row = conn.execute(
                    "SELECT session_id, adapter, cwd, state, last_event"
                    " FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            finally:
                conn.close()
        except (OSError, ValueError, sqlite3.Error):
            return None
        if row is None:
            return None
        return {
            "session_id": row[0],
            "adapter": row[1],
            "cwd": row[2],
            "state": row[3],
            "last_event": row[4],
        }

    @staticmethod
    def _idle(row) -> bool:
        """A session looks idle when its last spooled event was a stop
        or a session start. Anything else (a prompt mid-turn, a
        permission request) means the session will consume the reply
        at its own next boundary through the injection path."""
        state = str(row.get("state") or "").strip().lower()
        last_event = str(row.get("last_event") or "").strip().lower()
        if state == "done":
            return True
        return last_event == "sessionstart"

    # -- outputs --

    def _mark_reply_consumed(self, rid):
        """Flip consumed=true for the woken reply, atomically.

        Same contract as the injection path's marker: the journal has
        unsynchronized appenders, so the rewrite re-checks the size and
        gives up quietly (worst case: the reply injects again next
        prompt) rather than fighting a moving file."""
        path = self.replies_path
        for _ in range(2):
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
                size = len(raw)
                out_lines = []
                for line in raw.decode("utf-8", "replace").splitlines(
                    keepends=True
                ):
                    stripped = line.strip()
                    if stripped:
                        try:
                            entry = json.loads(stripped)
                        except ValueError:
                            entry = None
                        if (
                            isinstance(entry, dict)
                            and entry.get("id") == rid
                            and not entry.get("consumed")
                        ):
                            entry["consumed"] = True
                            line = (
                                json.dumps(entry, separators=(",", ":")) + "\n"
                            )
                    out_lines.append(line)
                if os.path.getsize(path) != size:
                    continue  # an appender moved the journal; re-read
                tmp = path + ".wake.tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write("".join(out_lines))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
                return
            except OSError:
                return

    def _journal(self, entry):
        """Append one wake journal line; best effort, never raises."""
        entry = dict(entry)
        entry.setdefault("ts", _now_iso())
        try:
            os.makedirs(self.state_root, exist_ok=True)
            with open(self.journal_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
                fh.flush()
        except OSError:
            pass

    def _seed_seen(self) -> set:
        """Reply ids already decided, from the wake journal tail.

        Keeps a restart from re-waking old replies; the bounded tail
        covers any journal the dispatcher could reasonably catch up
        on (older entries' replies are long consumed)."""
        try:
            size = os.path.getsize(self.journal_path)
            with open(self.journal_path, encoding="utf-8") as fh:
                if size > JOURNAL_TAIL_BYTES:
                    fh.seek(size - JOURNAL_TAIL_BYTES)
                    fh.readline()
                text = fh.read()
        except OSError:
            return set()
        seen = set()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("reply_id"):
                seen.add(str(entry["reply_id"]))
        return seen
