"""The tugline daemon.

Single-threaded loop:

1. Flush the spool: every undelivered record is decided by the policy
   and, when it should notify, published over ntfy with retry and
   exponential backoff (attempts capped, backoff doubling up to a
   ceiling). Handled records are marked delivered and the delivery
   state file is fsynced.
2. Long-poll the reply topic and append each message found to
   ~/.tugline/replies.jsonl with consumed=false and the ntfy message
   id as the reply id. The poll cursor lives in
   <state_root>/reply_cursor.json as {"last_id": "..."}.
3. Maintain session state from UserPromptSubmit and Stop records in
   the spool (see tugline.core.policy for the file formats). Consuming
   a Stop clears that session's armed marker.

A bad record never takes the daemon down: one log line, keep going.
SIGTERM exits the loop cleanly. --once runs a single cycle.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from tugline.core import policy, spool, topics
from tugline.core.publisher import Publisher

DEFAULT_SPOOL_ROOT = os.path.join("~", ".tugline", "spool")
DEFAULT_REPLIES_PATH = os.path.join("~", ".tugline", "replies.jsonl")


class Daemon:
    def __init__(
        self,
        config,
        spool_root=None,
        replies_path=None,
        state_root=None,
        publisher=None,
        sleep=time.sleep,
        poll_timeout=45.0,
        cycle_idle=0.5,
        backoff_base=1.0,
        backoff_cap=30.0,
        max_attempts=5,
        log=None,
    ):
        self.config = config
        self.spool_root = os.path.expanduser(spool_root or DEFAULT_SPOOL_ROOT)
        self.replies_path = os.path.expanduser(
            replies_path or DEFAULT_REPLIES_PATH
        )
        self.state_root = state_root or policy.DEFAULT_STATE_ROOT
        self.publisher = publisher or Publisher.from_config(config)
        self.sleep = sleep
        self.poll_timeout = poll_timeout
        self.cycle_idle = cycle_idle
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_attempts = max_attempts
        self._log_fn = log or _stderr_log
        self._stop = False

    def log(self, line):
        self._log_fn(line)

    # -- spool flush --

    def flush(self) -> int:
        """Handle every undelivered record once. Returns handled count."""
        handled = 0
        misses = 0
        for record in spool.undelivered(self.spool_root, on_error=self.log):
            try:
                ok = self._handle(record)
            except Exception as exc:  # one bad record, one line, move on
                self.log("record %s skipped: %s" % (record.get("id"), exc))
                continue
            if ok:
                handled += 1
                misses = 0
            else:
                misses += 1
                if misses >= 3:
                    self.log("flush pausing after repeated publish failures")
                    break
        return handled

    def _handle(self, record) -> bool:
        name = _record_event(record)
        session_id = str(record.get("session_id") or "")
        ts_epoch = _record_epoch(record)

        if name == "userpromptsubmit":
            policy.record_prompt(session_id, ts_epoch, self.state_root)
            self._mark(record)
            return True

        turn_seconds = None
        if name == "stop":
            policy.record_stop(session_id, ts_epoch, self.state_root)
            state = policy.load_session(session_id, self.state_root)
            turn_seconds = state.get("last_turn_seconds")

        decision = policy.decide(
            record,
            policy.SessionState(
                session_id=session_id or "none",
                armed=policy.is_armed(session_id, self.state_root)
                if session_id
                else False,
                turn_seconds=turn_seconds,
                idle_seconds=self.config.policy.idle_seconds,
            ),
        )

        if decision.action == "publish" and self.config.toggles.notify:
            topic = topics.resolve(decision.topic, self.config)
            if not self._publish_with_retry(topic, record, decision, name):
                return False

        self._mark(record)
        if name == "stop" and session_id:
            policy.disarm(session_id, self.state_root)
        return True

    def _publish_with_retry(self, topic, record, decision, event_name) -> bool:
        delay = self.backoff_base
        last_error = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            result = self.publisher.publish(
                topic,
                record.get("body") or record.get("title") or "",
                title=record.get("title"),
                priority=decision.priority or record.get("priority"),
                tags=topics.tags_for(event_name),
                actions=record.get("actions"),
            )
            if result.ok:
                return True
            last_error = result.error or ("HTTP %s" % result.status)
            self.log(
                "publish %s attempt %d/%d failed: %s"
                % (record.get("id"), attempt, self.max_attempts, last_error)
            )
            if attempt < self.max_attempts:
                self.sleep(delay)
                delay = min(delay * 2, self.backoff_cap)
        self.log("publish %s gave up: %s" % (record.get("id"), last_error))
        return False

    def _mark(self, record):
        spool.mark_delivered(self.spool_root, record.get("id"))


def _record_event(record) -> str:
    name = record.get("event") or record.get("hook_event_name") or ""
    return str(name).strip().lower()


def _record_epoch(record) -> float:
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        return time.time()


def _stderr_log(line):
    print("tugline: %s" % line, file=sys.stderr, flush=True)
