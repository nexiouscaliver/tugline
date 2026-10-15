"""Wake-on-reply dispatcher (W8-L19).

The operator's ask: a reply must reach its idle session immediately,
not at whatever turn boundary comes next. W7-L17 already injects
replies into a RUNNING session at its next prompt; this module covers
the other half. The daemon calls WakeDispatcher.poll() once per cycle
after the spool flush:

1. Reap and timeout running wakes (one log line and journal entry
   each: dispatched, finished, timed_out, reopened).
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
     double-delivers it, and journal the exact command. A wake whose
     turn then fails (nonzero exit) or times out re-opens the reply
     for the injection path instead of swallowing it.

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
The zcode driver continues the LATEST session in the session's cwd
(-c) rather than resuming by id: -c is verified by a live resume
proof against the bundled CLI (a second turn inside a pre-existing
session, exit 0), while every --resume <id> -p run in the current
app bundle (v0.16.9) exits 1 at model creation with "Select a model
before continuing" because the rehydrated session loses its model
selection. Since -c reaches whatever session is latest in --cwd,
the dispatcher guards it: a reply bound to a session that is not
the store's latest for its cwd skips the wake entirely (the reply
stays queued for the injection path); a fixed bundle can restore
--resume through wake.zcode_command, whose by-id template skips
the guard.

Bare driver names are resolved against PATH plus the running tug
executable's own directory plus a few extra roots (see
EXTRA_EXEC_PATHS) before the journal or the spawn sees them:
the daemon runs under launchd, whose PATH=/usr/bin:/bin:/usr/sbin:
/sbin has no node, and both live wake dispatches died with
"[Errno 2] No such file or directory: 'node'" while the same
command worked from every interactive shell.

The provider config bridge: the zcode app injects
ZCODE_BUILTIN_PROVIDER_CONFIG_FILE (and the personal variant) into
its child processes, and the zcode hook records those values to
state/zcode-runtime.json on every invocation; the wake spawn merges
them back into the child environment (see zcode_runtime_env and
_default_spawn) because the launchd daemon never inherits them and
the CLI's fallback paths do not exist on disk, so a daemon-spawned
zcode wake otherwise dies on "cannot locate CLI ZCode Built-in
Provider Config".

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
import sys
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

# The verified zcode continue-with-prompt form, run through node
# because the app ships a .cjs bundle, not an executable. -c
# continues the LATEST session in --cwd, verified by a live resume
# proof against the bundled CLI (v0.16.9): a second turn inside a
# pre-existing session, exit 0. The by-id --resume <sessionId> form
# is broken in the current app bundle (the rehydrated session loses
# its model selection and every --resume <id> -p run exits 1 with
# "Model creation failed" caused by "Select a model before
# continuing"), so the default never uses it; a future fixed bundle
# can restore it through the wake.zcode_command override seam.
# {session} is no longer substituted by the default template: -c
# picks the session itself, and the wake journal already records
# session_id as its own event field.
DEFAULT_ZCODE_COMMAND = [
    "node",
    "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
    "-c",
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


def _is_continue_command(command) -> bool:
    """A -c-shaped template: one whose literal elements include both
    "-c" and "{cwd}". Only such a template reaches whatever session
    is latest in --cwd, so only it needs the latest-in-cwd guard; a
    --resume override addresses one session by id and skips it."""
    return "-c" in command and "{cwd}" in command


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
    the running executable's directory plus the extra roots, empty
    components dropped and every entry collapsed to its first
    occurrence: the os.pathsep-joined search string shutil.which
    consumes."""
    source = os.environ if env is None else env
    # the env PATH is split, not joined whole: dedup has to see the
    # components to collapse a dir that both the inherited PATH and
    # the executable's own contribution carry
    parts = (source.get("PATH") or "").split(os.pathsep)
    # WHY the executable's own directory: the session a wake spawns
    # runs tugline hooks shaped "tug hook-zcode <Event>", and that
    # tug must be the same binary the daemon runs. tugline installs
    # as a uv tool whose venv bin dir (e.g. ~/.local/share/uv/tools/
    # tugline/bin) is on no launchd PATH and in no extra root, so a
    # woken turn could fully succeed while every hook of its own died
    # (core.hooks hook.run.failed) and the turn stayed store-silent:
    # no session events, no response preview, no stop buzz. The dir
    # sits before the extra roots so the daemon's own tug outranks
    # any stale tug a generic root happens to carry.
    exe_dir = os.path.dirname(sys.executable)
    if exe_dir:
        parts.append(exe_dir)
    parts.extend(EXTRA_EXEC_PATHS)
    seen = set()
    unique = []
    for part in parts:
        if not part or part in seen:
            continue  # empty or already present: first occurrence wins
        seen.add(part)
        unique.append(part)
    return os.pathsep.join(unique)


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


def zcode_runtime_env(state_root):
    """The provider config variables the zcode hook recorded, as a
    child-env dict; {} when there is nothing usable.

    WHY: the zcode app injects ZCODE_BUILTIN_PROVIDER_CONFIG_FILE
    (and the personal variant) into its children, and the hook
    snapshots them to <state_root>/zcode-runtime.json; the daemon
    runs under launchd and can never inherit them, and the CLI's
    own fallback paths do not exist on disk, so without these vars
    a daemon-spawned zcode wake dies on "cannot locate CLI ZCode
    Built-in Provider Config". A missing file, broken json, or any
    OSError simply means no vars: the spawn then behaves exactly
    as it did before the bridge existed. Only present, non-null,
    string values survive; the spawn applies them with setdefault
    semantics so an inherited os.environ value always wins.
    """
    path = os.path.join(str(state_root), "zcode-runtime.json")
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    out = {}
    builtin = record.get("builtin_provider_config")
    if isinstance(builtin, str) and builtin:
        out["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = builtin
    personal = record.get("personal_provider_config")
    if isinstance(personal, str) and personal:
        out["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = personal
    return out


def _default_spawn(argv, cwd=None, runtime_env=None):
    # the child inherits the augmented PATH, not just the resolved
    # argv[0]: node's own children (git shims, package-manager
    # helpers) would hit the same launchd gap one process later.
    # runtime_env carries the provider config vars the hook recorded
    # (see zcode_runtime_env); setdefault semantics so a value the
    # daemon's own environment already carries always wins over the
    # record.
    env = dict(os.environ)
    env["PATH"] = _augmented_path()
    for key, value in (runtime_env or {}).items():
        env.setdefault(key, value)
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
        if spawn is None:
            # the default spawn reads the hook's runtime env record
            # at CALL time, not a snapshot taken here: a long-lived
            # daemon outlives app upgrades, and the versioned
            # provider config paths inside the record change with
            # every one of them
            def _spawn_with_runtime_env(argv, cwd=None):
                return _default_spawn(
                    argv,
                    cwd=cwd,
                    runtime_env=zcode_runtime_env(self.state_root),
                )

            self._spawn = _spawn_with_runtime_env
        else:
            self._spawn = spawn
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
            if _is_continue_command(command):
                # -c continues whatever session is latest in --cwd:
                # waking a non-latest session (or one whose latest
                # cannot be verified: missing store row cwd, busy
                # store) would deliver the reply to the wrong
                # conversation, so the wake stands down and the
                # injection path still owns delivery
                stored_cwd = str(row.get("cwd") or "")
                latest = self._latest_session_in_cwd(stored_cwd)
                if latest != session_id:
                    self._decide(
                        entry, "skipped",
                        "wake skipped: session %s is not the latest"
                        " in %s (latest %s), reply queued for"
                        " next-boundary injection"
                        % (session_id, stored_cwd or "(no cwd)",
                           latest or "unknown"),
                        adapter=adapter,
                        reason="not the latest session in %s:"
                        " latest is %s"
                        % (stored_cwd or "(no cwd)", latest or "unknown"),
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
            # the killed turn never consumed the reply: hand it back
            # to the injection path instead of swallowing it
            self._reopen_reply(wake["reply_id"])
            self._journal(
                {
                    "event": "reopened",
                    "reply_id": wake["reply_id"],
                    "session_id": session_id,
                    "adapter": wake["adapter"],
                    "duration_s": round(elapsed, 3),
                }
            )
            self.log(
                "wake reply %s re-opened for next-boundary injection"
                % wake["reply_id"]
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
        if code != 0:
            # the failed turn never consumed the reply: hand it back
            # to the injection path instead of swallowing it (code 0
            # consumed it legitimately; that wake stands)
            self._reopen_reply(wake["reply_id"])
            self._journal(
                {
                    "event": "reopened",
                    "reply_id": wake["reply_id"],
                    "session_id": session_id,
                    "adapter": wake["adapter"],
                    "exit_code": code,
                }
            )
            self.log(
                "wake reply %s re-opened for next-boundary injection"
                % wake["reply_id"]
            )

    # -- decisions --

    def _decide(self, entry, event, message, adapter="", reason=None):
        """One terminal decision for a reply: journal it, log it, and
        mark the reply id decided. The reply itself stays unconsumed
        so the injection path still owns delivery. reason, when set,
        lands in the journal event so a skip is explainable from the
        journal alone."""
        rid = entry["id"]
        self._seen.add(rid)
        self._deferred.discard(rid)
        record = {
            "event": event,
            "reply_id": rid,
            "reply_ts": str(entry.get("ts") or ""),
            "session_id": str(entry.get("session_id") or ""),
            "adapter": adapter,
        }
        if reason is not None:
            record["reason"] = reason
        self._journal(record)
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

    def _latest_session_in_cwd(self, cwd):
        """The store's most recently seen session for a cwd, read-only;
        None when the cwd is empty, the store is missing or busy, or
        no session is known there (same probe posture as _session_row:
        unverifiable means no wake)."""
        if not cwd:
            return None
        if not os.path.exists(self.store_path):
            return None
        try:
            uri = "file:%s?mode=ro" % urllib.request.pathname2url(self.store_path)
            conn = sqlite3.connect(uri, uri=True, timeout=STORE_TIMEOUT_S)
            try:
                row = conn.execute(
                    "SELECT session_id FROM sessions WHERE cwd = ?"
                    " ORDER BY last_seen DESC, session_id DESC LIMIT 1",
                    (cwd,),
                ).fetchone()
            finally:
                conn.close()
        except (OSError, ValueError, sqlite3.Error):
            return None
        if row is None:
            return None
        return str(row[0] or "")

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

    def _reopen_reply(self, rid):
        """Flip consumed back to false for a woken reply whose turn
        then failed or timed out, atomically.

        Mirrors _mark_reply_consumed (the reply was consumed at
        dispatch, so a dead turn must hand it back to the injection
        path rather than silently swallowing it): same size re-check,
        same two attempts, and a quiet give-up when the line moves
        (worst case: the reply injects again next prompt)."""
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
                            and entry.get("consumed")
                        ):
                            entry["consumed"] = False
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
