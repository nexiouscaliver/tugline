"""The tug ask primitive (W9-L21): one multiple choice question an
operator answers from tugline itself.

The flow, all inside AskRunner.run and never raising:

1. Create the ask thread in the store (kind "ask", options JSON) so
   the console can render the question with tappable choices and the
   answer state from its normal threads read.
2. Push one HIGH priority notification whose title reuses the
   enriched helpers ("[<harness>] <project> - asks: <question>") and
   whose body lists the lettered options with "reply <letter>"
   guidance. A publish failure is logged, never fatal: the ask stays
   open and answerable from the console.
3. Block polling for an answer until the timeout (default 300s, max
   1800s), then mark the thread expired.

Answers arrive three ways, checked every poll:

- a store reply row bound to the thread nonce (a console option tap
  or a reply aimed at the ask), or bound to the session after the
  console resolved id > label > tag (W7-L18 targeting);
- a reply journal line (replies.jsonl) whose body is exactly one
  option key and whose thread/session binding resolves to this ask
  thread: a binding is required, so a bare unbound ntfy reply never
  answers an ask, and a session-bound reply lands on the session's
  NEWEST open ask so two concurrent asks never steal answers;
- the thread row itself, flipped by tug answer --thread <nonce>
  <key> from another process.

A reply whose body is not exactly one option key is rejected (the
ask keeps waiting); so is an answer attempt for a key the ask does
not carry. On timeout the runner expires the thread and reports
"no answer".
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from tugline.core import payload, topics

DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 1800
MIN_OPTIONS = 2
MAX_OPTIONS = 8
OPTION_LETTERS = "ABCDEFGH"
POLL_INTERVAL_S = 1.0
ASK_PRIORITY = "high"
REPLY_TAIL_BYTES = 262144

# option strings may carry their own letter ("A: ship it"); anything
# else is lettered automatically by position.


def parse_options(raw) -> list:
    """--option strings into [{"key", "text"}]; ValueError on misuse.

    An explicit letter wins ("A: ship it", "b) roll back"); a bare
    string gets the first unused letter. 2 to 8 options, unique keys,
    nothing empty.
    """
    items = [str(item or "") for item in (raw or [])]
    if not (MIN_OPTIONS <= len(items) <= MAX_OPTIONS):
        raise ValueError(
            "an ask carries %d to %d options (got %d)"
            % (MIN_OPTIONS, MAX_OPTIONS, len(items))
        )
    out = []
    used = set()
    for item in items:
        text = item.strip()
        if not text:
            raise ValueError("an option cannot be empty")
        key = ""
        desc = text
        if (
            len(text) >= 2
            and text[0].isalpha()
            and text[0].isascii()
            and text[1] in ":)"
        ):
            key = text[0].upper()
            desc = text[2:].strip()
        if not key:
            key = next(
                (c for c in OPTION_LETTERS if c not in used), None
            )
            if key is None:
                raise ValueError("too many options to letter")
        if key in used:
            raise ValueError("duplicate option key %r" % key)
        used.add(key)
        out.append({"key": key, "text": desc})
    return out


def match_option(options, body):
    """The canonical option key when body is exactly one option key
    (case-insensitive, whitespace-trimmed); None otherwise. "A: ship"
    is a message, not a key: only the bare key answers."""
    text = str(body or "").strip()
    if not text:
        return None
    for option in options:
        if text.upper() == str(option.get("key") or "").upper():
            return str(option["key"])
    return None


def ask_title(session_row, question) -> str:
    """The ask notification title, reusing the enriched helpers:
    "[<harness>] <project> - asks: <question>"."""
    harness = str((session_row or {}).get("adapter") or "").strip()
    if not harness:
        harness = "tugline"
    one_line = " ".join(str(question or "").split())
    return payload.context_title(
        harness,
        (session_row or {}).get("cwd"),
        "asks: %s" % one_line[:80],
    )


def ask_body(session_id, options) -> str:
    """The ask notification body: the session line from the enriched
    helper, the lettered options, and the reply guidance."""
    lines = [payload.context_body(session_id)]
    for option in options:
        text = str(option.get("text") or "").strip()
        if text:
            lines.append("%s: %s" % (option["key"], text))
        else:
            lines.append(str(option["key"]))
    keys = ", ".join(str(o["key"]) for o in options)
    lines.append("reply with a letter: %s" % keys)
    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AskRunner:
    """One ask lifecycle against one store. run() never raises."""

    def __init__(
        self,
        store,
        publisher=None,
        config=None,
        replies_path=None,
        poll_interval=POLL_INTERVAL_S,
        now=time.monotonic,
        sleep=time.sleep,
        log=None,
    ):
        self.store = store
        self.config = config
        self.replies_path = str(replies_path) if replies_path else None
        self.poll_interval = max(0.05, float(poll_interval))
        self._now = now
        self._sleep = sleep
        self._log_fn = log or (lambda line: None)
        if publisher is not None:
            self.publisher = publisher
        elif config is not None:
            from tugline.core.publisher import Publisher

            self.publisher = Publisher.from_config(config)
        else:
            self.publisher = None

    def log(self, line):
        self._log_fn(str(line))

    # -- public surface --

    def run(self, session_id, question, options, timeout_s=DEFAULT_TIMEOUT_S,
            nonce=None) -> dict:
        """Ask, notify, block, and answer. Returns a result dict:

        {"ok": True, "key", "text", "nonce", "via"}
        {"ok": False, "reason", "nonce"} with reason one of
        "unknown session", "timeout", "expired", or "error: ...".

        Every failure path lands in the result; nothing raises.
        """
        try:
            return self._run(
                session_id, question, options, timeout_s, nonce
            )
        except Exception as exc:  # never raises out of run()
            self.log("ask failed: %s" % (exc,))
            return {
                "ok": False,
                "reason": "error: %s" % exc,
                "nonce": str(nonce or ""),
            }

    # -- the lifecycle --

    def _resolve_session(self, ref):
        """(session row or None, failure dict or None) for one --session
        reference.

        The ask path accepts what the console's reply targeting
        accepts (W7-L18): an exact session id, then a label exactly
        one session carries, then a tag exactly one session carries.
        An ambiguous name fails with the candidates listed, mirroring
        the console's 400; nothing matched fails as unknown session.
        """
        try:
            resolved = self.store.resolve_session_ref(ref)
        except Exception:
            resolved = None
        if resolved and resolved.get("match"):
            row = self.store.session(resolved["session_id"])
            if row is not None:
                return row, None
        if resolved and resolved.get("ambiguous"):
            return None, {
                "ok": False,
                "reason": "ambiguous session",
                "kind": resolved.get("kind") or "",
                "candidates": list(resolved.get("candidates") or []),
            }
        return None, {"ok": False, "reason": "unknown session"}

    def _run(self, session_id, question, options, timeout_s, nonce):
        session_id = str(session_id or "")
        timeout_s = min(max(float(timeout_s or 0), 0.0), MAX_TIMEOUT_S)
        row, failure = self._resolve_session(session_id)
        if row is None:
            failure["nonce"] = str(nonce or "")
            return failure
        # everything downstream (thread creation, binding checks, the
        # notification body) carries the true id, never the reference
        session_id = str(row.get("session_id") or session_id)
        thread = self.store.create_ask_thread(
            session_id, question, options, nonce=nonce
        )
        nonce = thread["nonce"]
        self.log(
            "ask %s open for %s (timeout %ds)"
            % (nonce, session_id, int(timeout_s))
        )
        self._notify(row, question, thread["options"])

        baseline_replies = self._reply_ids()
        baseline_journal = self._journal_ids()
        deadline = self._now() + timeout_s
        while True:
            key = self._check_store_replies(
                nonce, session_id, thread["options"], baseline_replies
            )
            via = "reply"
            if key is None:
                key = self._check_journal(
                    nonce, session_id, thread["options"], baseline_journal
                )
                via = "ntfy reply"
            if key is None:
                key, via = self._check_thread(nonce, thread["options"])
            if key is not None:
                text = self._option_text(thread["options"], key)
                self.log("ask %s answered %s via %s" % (nonce, key, via))
                return {
                    "ok": True,
                    "key": key,
                    "text": text,
                    "nonce": nonce,
                    "via": via,
                }
            if self._now() >= deadline:
                break
            remaining = deadline - self._now()
            self._sleep(min(self.poll_interval, max(remaining, 0.0)))
        self.store.expire_ask_thread(nonce)
        self.log("ask %s expired after %ds" % (nonce, int(timeout_s)))
        return {"ok": False, "reason": "timeout", "nonce": nonce}

    def _notify(self, session_row, question, options) -> None:
        if self.publisher is None:
            return
        if self.config is not None and not getattr(
            self.config.toggles, "notify", True
        ):
            self.log("ask notification skipped: notify is off in config")
            return
        topic = "main"
        if self.config is not None:
            topic = topics.resolve("main", self.config)
        try:
            result = self.publisher.publish(
                topic,
                ask_body(session_row["session_id"], options),
                title=ask_title(session_row, question),
                priority=ASK_PRIORITY,
            )
        except Exception as exc:  # a dead publisher never kills the ask
            self.log("ask notification failed: %s" % (exc,))
            return
        if result.ok:
            self.log("ask notification published to %s" % topic)
        else:
            self.log(
                "ask notification failed: %s"
                % (result.error or ("HTTP %s" % result.status))
            )

    # -- answer sources --

    def _check_store_replies(self, nonce, session_id, options,
                             baseline) -> str | None:
        """Console replies: rows bound to the thread nonce directly,
        or to the session (already resolved to an exact id by the
        console's id > label > tag targeting). Wrong keys reject."""
        try:
            rows = self.store.replies(limit=100)
        except Exception:
            return None
        for row in reversed(rows):  # oldest first: first match wins
            if row.get("id") in baseline:
                continue
            bound = row.get("thread_nonce") or ""
            target = str(row.get("session_id") or "")
            if bound:
                if str(bound) != str(nonce):
                    continue
            elif target and target == session_id:
                if self.store.newest_open_ask(session_id) != nonce:
                    continue
            else:
                continue
            key = match_option(options, row.get("body"))
            if key is not None:
                try:
                    self.store.answer_ask_thread(nonce, key)
                except ValueError:
                    # the console answer endpoint closes the thread
                    # first and records the reply row second; a poll
                    # that sees the row after the close reads it as
                    # the answer, not as a race error
                    current = self.store._thread_row(nonce)
                    if (
                        not current
                        or current.get("status") != "answered"
                        or str(current.get("answer") or "").upper()
                        != key.upper()
                    ):
                        raise
                return key
        return None

    def _check_journal(self, nonce, session_id, options, baseline):
        """Reply journal lines (ntfy app replies and anything else
        appended to replies.jsonl): the body must be exactly one
        option key and the line's thread/session binding must resolve
        to this ask thread. Unbound lines never answer."""
        for entry in self._journal_tail():
            if entry.get("id") in baseline:
                continue
            bound = str(entry.get("thread_nonce") or "")
            ref = str(entry.get("session_id") or "")
            if bound:
                if bound != str(nonce):
                    continue
            elif ref:
                try:
                    resolved = self.store.resolve_session_ref(ref)
                except Exception:
                    continue
                if not resolved.get("match"):
                    continue
                if resolved.get("session_id") != session_id:
                    continue
                if self.store.newest_open_ask(session_id) != nonce:
                    continue
            else:
                continue  # no binding: never maps
            key = match_option(options, entry.get("body"))
            if key is not None:
                try:
                    self.store.answer_ask_thread(nonce, key)
                except Exception:
                    continue
                return key
        return None

    def _check_thread(self, nonce, options):
        """The thread row itself, flipped by tug answer or a console
        tap writing the answer directly."""
        try:
            row = self.store._thread_row(nonce)
        except Exception:
            return None, ""
        if not row or row.get("status") != "answered":
            return None, ""
        answer = str(row.get("answer") or "")
        key = match_option(options, answer)
        if key is None:
            return None, ""
        return key, "tug answer"

    # -- inputs --

    def _reply_ids(self) -> set:
        try:
            return {
                row.get("id") for row in self.store.replies(limit=100)
            }
        except Exception:
            return set()

    def _journal_tail(self) -> list:
        if not self.replies_path:
            return []
        try:
            size = os.path.getsize(self.replies_path)
            with open(self.replies_path, encoding="utf-8") as fh:
                if size > REPLY_TAIL_BYTES:
                    fh.seek(size - REPLY_TAIL_BYTES)
                    fh.readline()  # drop the possibly partial line
                text = fh.read()
        except (OSError, ValueError):
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
            if isinstance(entry, dict):
                out.append(entry)
        return out

    def _journal_ids(self) -> set:
        return {entry.get("id") for entry in self._journal_tail()}

    @staticmethod
    def _option_text(options, key) -> str:
        for option in options:
            if str(option.get("key")) == str(key):
                return str(option.get("text") or "")
        return ""
