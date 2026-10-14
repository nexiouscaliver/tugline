"""Inline hook entry for the zcode harness.

Invoked as: tug hook-zcode <event>

The harness pipes one JSON line on stdin and waits. Exit code 2 would
block the session, so this entry always exits 0, even on garbage input,
and logs one line to <home>/logs/adapter-zcode.log instead of raising.
stdout must stay empty (strict schema) unless the approve path prints a
permission decision or UserPromptSubmit injects unread replies.

Flow: read stdin, normalize through the core payload module, apply core
policy, append one record to the core spool, exit 0. On
UserPromptSubmit, before the record is spooled, unconsumed replies are
read (journal plus store binding) and emitted on stdout as one JSON
object with additionalContext, the same channel the established
SessionStart injection uses.
"""

from __future__ import annotations

import json
import os
import re
import select
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone

from tugline.core import payload, policy, spool
from tugline.core.config import (
    Config,
    ConfigError,
    PolicyConfig,
    ServerConfig,
    TopicsConfig,
    TogglesConfig,
    load_config,
)

STDIN_CEILING = 2.0
LOG_NAME = "adapter-zcode.log"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

# Reply injection bounds: these hooks run inline on every prompt, so
# every read is bounded and best effort. The journal is read from its
# tail only, the store is opened read-only with a tiny busy timeout,
# and no reply failure may ever block or slow the prompt.
REPLY_TAIL_BYTES = 65536
REPLY_MAX_INJECTED = 5
REPLY_BODY_CHARS = 200
REPLY_STORE_TIMEOUT_S = 0.05


def read_stdin(stream=None, ceiling=STDIN_CEILING):
    """Read stdin with a hard ceiling. Returns whatever arrived in time."""
    if stream is None:
        stream = sys.stdin
    try:
        stream.fileno()
    except (OSError, ValueError, AttributeError):
        # in-memory stream (tests, embedders): cannot block, read it all
        return stream.read()
    chunks = []
    deadline = time.monotonic() + ceiling
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([stream], [], [], remaining)
        except (OSError, ValueError):
            break
        if not ready:
            break
        chunk = stream.read(4096)
        if chunk == "":
            break
        chunks.append(chunk)
    return "".join(chunks)


def log_line(home, message):
    directory = os.path.join(str(home), "logs")
    try:
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        clean = " ".join(str(message).split())[:300]
        with open(
            os.path.join(directory, LOG_NAME), "a", encoding="utf-8"
        ) as fh:
            fh.write("%s hook-zcode: %s\n" % (stamp, clean))
    except OSError:
        pass


def _fallback_config():
    return Config(
        server=ServerConfig(
            url="http://127.0.0.1:2586",
            token_file=os.path.expanduser("~/.tugline/credentials"),
        ),
        topics=TopicsConfig(main="tug", warn="tug-warn", reply="tug-reply"),
        policy=PolicyConfig(idle_seconds=600),
        toggles=TogglesConfig(notify=True, replies=True, approve=False),
        path="<defaults>",
    )


def _load_config(path):
    if path:
        try:
            return load_config(path)
        except ConfigError:
            return None
    try:
        return load_config()
    except ConfigError:
        return None


def _event_name(normalized, event_arg):
    name = normalized.get("hook_event_name") or event_arg or ""
    return str(name).strip()


def _session_id(normalized, env):
    sid = normalized.get("session_id") or env.get("CLAUDE_SESSION_ID") or ""
    return str(sid) or "unknown"


def _short(session_id):
    return str(session_id)[:8]


def _adapter_extra(normalized, env):
    """Adapter, cwd, and project extra fields for the spool record.

    cwd prefers the payload cwd field, then CLAUDE_PROJECT_DIR from
    the environment, then the process cwd; project is the final path
    segment of that cwd (or ""). String operations only, derived once
    per run.
    """
    cwd = str(
        normalized.get("cwd") or env.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    )
    project = cwd.rstrip("/").rsplit("/", 1)[-1]
    return {"adapter": "zcode", "cwd": cwd, "project": project}


# -- reply injection (UserPromptSubmit) --


def _unread_replies(home):
    """Unconsumed journal entries, in file order, from a bounded tail.

    Both reply writers (the daemon's ntfy capture and the console)
    append one line each to <home>/replies.jsonl with consumed=false.
    Reading is capped at the last REPLY_TAIL_BYTES so a long-lived
    journal cannot slow the prompt; anything older is long consumed.
    """
    path = os.path.join(str(home), "replies.jsonl")
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8") as fh:
            if size > REPLY_TAIL_BYTES:
                fh.seek(size - REPLY_TAIL_BYTES)
                fh.readline()  # drop the possibly partial line
            text = fh.read()
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and not entry.get("consumed"):
            entries.append(entry)
    return entries


def _bound_reply_pairs(home, session_id):
    """(ts, body) pairs of store replies aimed at this session.

    The console binds every reply it writes to a thread's session; the
    store is the only place that binding lives. This probe is
    read-only with a tiny busy timeout, and any problem (no store,
    busy store, broken store) simply yields no binding so the caller
    falls back to all unread replies.
    """
    if not session_id:
        return set()
    path = os.path.join(str(home), "store", "tugline.db")
    if not os.path.exists(path):
        return set()
    try:
        uri = "file:%s?mode=ro" % urllib.request.pathname2url(path)
        conn = sqlite3.connect(uri, uri=True, timeout=REPLY_STORE_TIMEOUT_S)
        try:
            rows = conn.execute(
                "SELECT ts, body FROM replies"
                " WHERE session_id = ? AND delivered = 0",
                (str(session_id),),
            ).fetchall()
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error):
        return set()
    return {(str(row[0]), str(row[1])) for row in rows}


def _reply_context(entries):
    """The additionalContext text for the chosen replies."""
    lines = [
        "tugline: %d unread repl%s from the operator (oldest first):"
        % (len(entries), "y" if len(entries) == 1 else "ies")
    ]
    for entry in entries:
        body = str(entry.get("body") or "").strip() or "(empty reply)"
        if len(body) > REPLY_BODY_CHARS:
            body = body[:REPLY_BODY_CHARS] + "..."
        label = str(entry.get("source") or entry.get("title") or "reply")
        lines.append("- [%s] %s" % (label, body))
    return "\n".join(lines)


def _mark_replies_consumed(home, chosen):
    """Flip consumed=true for the injected entries, atomically.

    The journal has two unsynchronized appenders (daemon and console),
    so the rewrite re-checks the file size and retries once when an
    append landed mid-read; if the journal keeps moving the marking is
    skipped (worst case: the reply injects again next prompt). Every
    other byte of the journal passes through unchanged.
    """
    path = os.path.join(str(home), "replies.jsonl")
    ids = {entry.get("id") for entry in chosen if entry.get("id")}
    if not ids:
        return
    for _ in range(2):
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            size = len(raw)
            out_lines = []
            for line in raw.decode("utf-8", "replace").splitlines(keepends=True):
                stripped = line.strip()
                if stripped:
                    try:
                        entry = json.loads(stripped)
                    except ValueError:
                        entry = None
                    if (
                        isinstance(entry, dict)
                        and entry.get("id") in ids
                        and not entry.get("consumed")
                    ):
                        entry["consumed"] = True
                        line = json.dumps(entry, separators=(",", ":")) + "\n"
                out_lines.append(line)
            if os.path.getsize(path) != size:
                continue  # an appender moved the journal; re-read
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("".join(out_lines))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return
        except OSError:
            return


def _emit_unread_replies(home, session_id, out):
    """Inject unread replies into the session. Returns the count.

    Best effort by contract: replies bound to this session (through
    the store) are preferred; with none bound, all unread replies are
    injected. The established stdout channel carries them (one JSON
    object with additionalContext, like the SessionStart injection),
    the injected entries are marked consumed, and any failure along
    the way degrades to injecting nothing rather than blocking the
    prompt.
    """
    try:
        unread = _unread_replies(home)
        if not unread:
            return 0
        bound = _bound_reply_pairs(home, session_id)
        chosen = [
            entry
            for entry in unread
            if (str(entry.get("ts") or ""), str(entry.get("body") or ""))
            in bound
        ]
        if not chosen:
            chosen = unread
        chosen = chosen[-REPLY_MAX_INJECTED:]
        if not chosen:
            return 0
        out.write(
            json.dumps({"additionalContext": _reply_context(chosen)}) + "\n"
        )
        out.flush()
        _mark_replies_consumed(home, chosen)
        return len(chosen)
    except Exception:  # never let a reply failure block the prompt
        return 0


def _legacy_record_fields(event, normalized, decision, session_id):
    """The exact title and body strings from before context enrichment."""
    if event == "permissionrequest":
        tool = str(normalized.get("tool_name") or "tool use")
        title = "permission request"
        body = "session %s: %s" % (_short(session_id), tool)
    elif event == "sessionstart":
        title = "session resumed"
        body = "session %s resumed" % _short(session_id)
    elif event == "stop":
        title = "session stopped"
        body = "session %s stopped: %s" % (
            _short(session_id),
            decision.reason or "turn finished",
        )
    elif event == "userpromptsubmit":
        title = "prompt submitted"
        prompt = str(normalized.get("prompt") or "")
        body = prompt[:60] or "session %s" % _short(session_id)
    else:
        title = event or "hook event"
        body = "session %s" % _short(session_id)
    topic = decision.topic if decision.action == "publish" else "main"
    priority = decision.priority if decision.action == "publish" else "default"
    return topic, priority, title, body


def _record_fields(event, normalized, decision, session_id, context_on, cwd):
    """Topic, priority, title, body for one record.

    With context_on the title answers, at a glance, which harness,
    which project, and finished versus waiting; the body leads with
    "session <short6>" plus a comma separated detail. With context off
    the legacy strings come back byte for byte.
    """
    if not context_on:
        return _legacy_record_fields(event, normalized, decision, session_id)

    if event == "permissionrequest":
        tool = str(normalized.get("tool_name") or "tool use")
        summary = "waiting for your input"
        detail = "permission: %s" % tool
    elif event == "sessionstart":
        source = str(normalized.get("source") or "").strip().lower()
        if source == "resume":
            summary, detail = "session resumed", "resumed"
        else:
            summary, detail = "session started", "started"
    elif event == "stop":
        summary = "turn finished"
        detail = payload.one_line_preview(normalized) or "turn finished"
    elif event == "userpromptsubmit":
        summary = "prompt submitted"
        prompt = str(normalized.get("prompt") or "")
        detail = ("prompt: %s" % prompt[:60]).rstrip() if prompt else ""
    else:
        summary, detail = event or "hook event", ""
    title = payload.context_title("zcode", cwd, summary)
    body = payload.context_body(session_id, detail)
    topic = decision.topic if decision.action == "publish" else "main"
    priority = decision.priority if decision.action == "publish" else "default"
    return topic, priority, title, body


def run(
    event_arg,
    stdin_text,
    *,
    config_path=None,
    home=None,
    env=None,
    _stdout=None,
):
    """Handle one hook event. Always returns an exit code of 0."""
    home = os.path.expanduser(home) if home else os.path.expanduser("~/.tugline")
    env = env if env is not None else dict(os.environ)
    out = _stdout if _stdout is not None else sys.stdout
    try:
        return _run(
            event_arg,
            stdin_text,
            home=home,
            env=env,
            config_path=config_path,
            out=out,
        )
    except Exception as exc:  # never traceback to the harness
        log_line(home, "unexpected error: %s" % exc)
        return 0


def _run(event_arg, stdin_text, *, home, env, config_path, out):
    raw = stdin_text
    if not raw.strip():
        log_line(home, "empty stdin, nothing to do")
        return 0
    try:
        parsed = json.loads(raw)
    except ValueError:
        log_line(home, "stdin is not json")
        return 0
    if not isinstance(parsed, dict):
        log_line(home, "stdin payload is not an object")
        return 0

    normalized = payload.normalize(parsed)
    event = _event_name(normalized, event_arg).lower()
    if not event:
        log_line(home, "no event name in payload or argv")
        return 0
    session_id = _session_id(normalized, env)
    extra = _adapter_extra(normalized, env)
    if event == "stop":
        # the console's session detail shows the last thing this
        # session said; 600 characters of one-line preview is the
        # budget (title, body, and priority stay exactly as they were)
        extra["response_preview"] = payload.one_line_preview(normalized, 600)

    config = _load_config(config_path)
    if config is None:
        log_line(home, "config unreadable, using defaults")
        config = _fallback_config()

    state_root = os.path.join(home, "state")
    policy_sid = session_id if _SAFE_ID.match(session_id) else "unknown"
    state = policy.SessionState(
        session_id=policy_sid,
        armed=policy.is_armed(policy_sid, state_root=state_root),
        turn_seconds=policy.load_session(policy_sid, state_root=state_root).get(
            "last_turn_seconds"
        ),
        idle_seconds=config.policy.idle_seconds,
    )
    decision = policy.decide(normalized, state)

    spool_root = os.path.join(home, "spool")

    if event == "permissionrequest" and config.toggles.approve:
        # the approve module publishes its own urgent record with
        # buttons, so the plain notify record is skipped here
        from tugline.adapters.zcode import approve

        outcome = approve.run(
            session_id,
            config=config,
            spool_root=spool_root,
            approvals_root=os.path.join(state_root, "approvals"),
        )
        if outcome is not None:
            out.write(approve.format_decision(outcome) + "\n")
            out.flush()
        return 0

    topic, priority, title, body = _record_fields(
        event,
        normalized,
        decision,
        session_id,
        config.toggles.context_titles,
        normalized.get("cwd") or normalized.get("project_dir"),
    )
    if event == "userpromptsubmit":
        injected = _emit_unread_replies(home, session_id, out)
        if injected:
            log_line(home, "injected %d unread replies" % injected)
    spool.append(
        spool_root,
        event=event,
        session_id=session_id,
        topic=topic,
        priority=priority,
        title=title,
        body=body,
        extra=extra,
    )
    return 0


def main(argv=None):
    """Console path: pull the event off argv, read stdin, run."""
    args = list(sys.argv[1:] if argv is None else argv)
    event = args[0] if args else ""
    return run(event, read_stdin())


if __name__ == "__main__":
    sys.exit(main())
