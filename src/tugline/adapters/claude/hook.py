"""The Claude Code hook entry point (tug hook-claude <event>).

Claude Code runs each hook as a command and hands it one JSON payload
on stdin. This module is the tugline side of that contract:

- read stdin under a hard 2 second ceiling,
- normalize the payload through the core (Claude's snake_case field
  spellings are taken directly, camelCase maps onto them),
- map the event onto the spool:
    SessionStart -> low priority notify, only when the source says
                   resume (a fresh startup is not worth a ping)
    Stop         -> a stop record; the core armed/idle policy decides
                   downstream whether it notifies
    Notification -> a notify; the classified type maps onto a plain
                   summary ("waiting for your input" for idle prompts,
                   "turn finished" for completions) and the raw type
                   string never reaches the phone
    PermissionRequest -> a high priority warn record, plus the bounded
                   remote approve handoff when the toggle is on
- titles and bodies carry session context ("[claude] project - turn
  finished", body leading "session <short6>") unless the
  [toggles] context_titles switch is off, which restores the exact
  strings from before the enrichment except the session fragment:
  since the W9 naming parity wave it is always the display rule's
  last 6 characters, so a hook body carries the token the
  publish-time naming pass knows how to rewrite,
- UserPromptSubmit injects unread operator replies into the session
  through Claude Code's documented context channel (one JSON object
  with hookSpecificOutput.additionalContext on stdout), so a running
  session sees replies without waiting for a resume,
- keep stdout EMPTY unless a permission decision is being returned or
  replies are being injected,
- log exactly one line per run under ~/.tugline/logs/.

Every failure path is fail closed: log one line, exit 0, print
nothing. A hook that prints no decision falls through to Claude
Code's local permission prompt, which is exactly the behavior wanted
when anything is broken. A reply read that fails in any way degrades
to today's behavior (no injection, exit 0). Nothing here ever raises
to the caller.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone

from tugline.adapters.claude import approve, paths
from tugline.core import payload as payload_module
from tugline.core import spool
from tugline.core.config import ConfigError, load_config

STDIN_CEILING = 2.0

# Reply injection bounds: the hook runs inline on every prompt, so
# every read is bounded and best effort (journal tail only, read-only
# store probe with a tiny busy timeout, nothing may block).
REPLY_TAIL_BYTES = 65536
REPLY_MAX_INJECTED = 5
REPLY_BODY_CHARS = 200
REPLY_STORE_TIMEOUT_S = 0.05


class _HookEventAction(argparse.Action):
    """Run the hook while the arguments are being parsed, then exit.

    Same pattern as argparse's own version action: the hook is the
    whole command, so once the event name is in hand there is nothing
    left for the CLI dispatcher to do.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        raise SystemExit(run_event(str(values)))


def register_hook_command(subparsers):
    """Wire the hook-claude subcommand onto the tug CLI."""
    parser = subparsers.add_parser(
        "hook-claude",
        help="run one Claude Code hook event (payload on stdin)",
    )
    parser.add_argument(
        "event",
        action=_HookEventAction,
        help="hook event name, for example Stop or PermissionRequest",
    )


def read_stdin(stream=None, timeout=STDIN_CEILING) -> str:
    """Read the hook payload under a hard time ceiling.

    A terminal or a pipe that stays silent past the ceiling reads as
    an empty payload; the caller treats that as "no input" and exits
    cleanly rather than hanging the hook.
    """
    if stream is None:
        stream = sys.stdin
    try:
        if stream.isatty():
            return ""
    except Exception:
        return ""
    try:
        stream.fileno()
    except Exception:
        fileno = None
    else:
        fileno = True
    if fileno is not None:
        try:
            ready, _, _ = select.select([stream], [], [], timeout)
        except Exception:
            ready = []
        if not ready:
            return ""
    try:
        return stream.read()
    except Exception:
        return ""


def parse_payload(raw):
    """Return the payload dict, or None for empty/garbage/non-object input."""
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def classify_notification(message) -> str:
    text = str(message or "").lower()
    if "waiting for your input" in text:
        return "idle_prompt"
    if "finished" in text or "completed" in text:
        return "agent_completed"
    return "generic"


# Plain notification summaries for the enriched titles. The classified
# ntype strings (idle_prompt, agent_completed, generic) stay internal:
# they never reach the phone raw again.
_NOTIFICATION_SUMMARIES = {
    "idle_prompt": "waiting for your input",
    "agent_completed": "turn finished",
    "generic": "notification",
}


def _context_titles_enabled() -> bool:
    """Read [toggles] context_titles without ever writing a config.

    A tolerant one-file probe: TUGLINE_CONFIG wins, otherwise the
    config next to the adapter home (which is ~/.tugline in
    production). Any missing file, unparsable line, or unknown value
    leaves the toggle at its default, and nothing is ever created.
    """
    path = os.environ.get("TUGLINE_CONFIG") or os.path.join(
        paths.home_root(), "config.toml"
    )
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return True
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "toggles":
            continue
        key, sep, rest = line.partition("=")
        if not sep or key.strip() != "context_titles":
            continue
        value = rest.split("#", 1)[0].strip().strip('"').lower()
        return value != "false"
    return True


def _session_cwd(normalized):
    return normalized.get("cwd") or normalized.get("project_dir")


def _adapter_extra(normalized):
    """Adapter, cwd, and project extra fields for the spool record.

    cwd prefers the payload cwd field, then CLAUDE_PROJECT_DIR from
    the environment, then the process cwd; project is the final path
    segment of that cwd (or ""). String operations only, derived once
    per run.
    """
    cwd = str(
        normalized.get("cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    project = cwd.rstrip("/").rsplit("/", 1)[-1]
    return {"adapter": "claude", "cwd": cwd, "project": project}


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


def _emit_unread_replies(session) -> int:
    """Inject unread replies into the session. Returns the count.

    Best effort by contract: replies bound to this session (through
    the store) are preferred; with none bound, all unread replies are
    injected. The output is Claude Code's documented UserPromptSubmit
    context shape, the injected entries are marked consumed in the
    journal, and any failure along the way degrades to injecting
    nothing rather than blocking the prompt.
    """
    home = paths.home_root()
    try:
        unread = _unread_replies(home)
        if not unread:
            return 0
        bound = _bound_reply_pairs(home, session)
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
        payload_out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _reply_context(chosen),
            }
        }
        sys.stdout.write(json.dumps(payload_out) + "\n")
        sys.stdout.flush()
        _mark_replies_consumed(home, chosen)
        return len(chosen)
    except Exception:  # never let a reply failure block the prompt
        return 0


def run_event(event_name, stdin=None) -> int:
    """Run one hook event. Never raises; always returns an exit code."""
    try:
        return _run(event_name, stdin)
    except Exception as exc:  # one bad payload, one line, exit clean
        _log(event_name, "error: %s" % (exc,))
        return 0


def _run(event_name, stdin) -> int:
    data = parse_payload(read_stdin(stdin))
    if data is None:
        _log(event_name, "no payload")
        return 0

    normalized = payload_module.normalize(data)
    event = str(event_name or normalized.get("hook_event_name") or "").strip()
    session = str(normalized.get("session_id") or "claude")
    key = event.lower()
    context_on = _context_titles_enabled()
    extra = _adapter_extra(normalized)

    if key == "sessionstart":
        source = str(normalized.get("source") or "").strip().lower()
        if source != "resume":
            _log(event, "session start is not a resume")
            return 0
        if context_on:
            title = payload_module.context_title(
                "claude", _session_cwd(normalized), "session resumed"
            )
            body = payload_module.context_body(session, "resumed")
        else:
            title = "claude session resumed"
            body = "session %s resumed" % payload_module.short_session_id(
                session
            )
        spool.append(
            paths.spool_root(),
            event="notify",
            session_id=session,
            topic="main",
            priority="low",
            title=title,
            body=body,
            extra=extra,
        )
        _log(event, "spooled resume")
        return 0

    if key == "stop":
        if context_on:
            title = payload_module.context_title(
                "claude", _session_cwd(normalized), "turn finished"
            )
            detail = payload_module.one_line_preview(normalized) or "turn finished"
            body = payload_module.context_body(session, detail)
        else:
            title = "claude turn finished"
            body = "session %s turn finished" % payload_module.short_session_id(
                session
            )
        # the console's session detail shows the last thing this
        # session said and renders it as markdown, so the preview keeps
        # newlines (code blocks and lists must survive) inside a 600
        # character budget (title, body, and priority stay exactly as
        # they were)
        extra = dict(extra)
        extra["response_preview"] = payload_module.multiline_preview(
            normalized, 600
        )
        spool.append(
            paths.spool_root(),
            event="stop",
            session_id=session,
            topic="main",
            priority="default",
            title=title,
            body=body,
            extra=extra,
        )
        _log(event, "spooled stop")
        return 0

    if key == "notification":
        message = normalized.get("message") or normalized.get("title") or ""
        ntype = classify_notification(message)
        if context_on:
            title = payload_module.context_title(
                "claude", _session_cwd(normalized), _NOTIFICATION_SUMMARIES[ntype]
            )
            body = payload_module.context_body(session, message)
        else:
            title = "claude %s" % ntype
            body = ("%s: %s" % (ntype, message)).rstrip(": ")
        spool.append(
            paths.spool_root(),
            event="notify",
            session_id=session,
            topic="main",
            priority="default",
            title=title,
            body=body,
            extra=extra,
        )
        _log(event, "spooled notification %s" % ntype)
        return 0

    if key == "permissionrequest":
        decision = _permission_request(normalized, session, context_on, extra)
        if decision is None:
            _log(event, "permission request spooled")
        else:
            sys.stdout.write(decision + "\n")
            sys.stdout.flush()
            _log(event, "decision returned")
        return 0

    if key == "userpromptsubmit":
        injected = _emit_unread_replies(session)
        if injected:
            _log(event, "injected %d unread replies" % injected)
        else:
            _log(event, "prompt submitted, no unread replies")
        return 0

    _log(event, "event not wired")
    return 0


def _spool_plain_permission(normalized, session, context_on, extra):
    """The permission record for when the approve flow is not active.

    Byte-identical to approve.publish_request's plain record when the
    context toggle is off; enriched with session context when on.
    """
    tool = str(normalized.get("tool_name") or "tool")
    summary = approve.summarize_input(normalized.get("tool_input"))
    if context_on:
        title = payload_module.context_title(
            "claude", _session_cwd(normalized), "waiting for your input"
        )
        body = payload_module.context_body(session, "permission: %s" % tool)
    else:
        title = "claude permission request"
        body = (tool + " " + summary).rstrip()
    spool.append(
        paths.spool_root(),
        event="permissionrequest",
        session_id=session,
        topic="warn",
        priority="high",
        title=title,
        body=body,
        extra=extra,
    )


def _permission_request(normalized, session, context_on, extra):
    try:
        config = load_config()
    except ConfigError:
        config = None
    if config is None or not config.toggles.approve:
        # No usable config means no buttons and no wait: publish the
        # plain record and let the local prompt decide.
        _spool_plain_permission(normalized, session, context_on, extra)
        return None
    return approve.handle(
        normalized,
        session_id=session,
        config=config,
        spool_root=paths.spool_root(),
        state_root=paths.state_root(),
    )


def _log(event, outcome) -> None:
    try:
        path = paths.log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                "%s %s %s\n"
                % (datetime.now(timezone.utc).isoformat(), event, outcome)
            )
    except OSError:
        pass
