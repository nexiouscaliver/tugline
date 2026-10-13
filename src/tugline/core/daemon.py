"""The tugline daemon.

Single-threaded loop:

1. Flush the spool: every undelivered record is decided by the policy
   and, when it should notify, published over ntfy with retry and
   exponential backoff (attempts capped, backoff doubling up to a
   ceiling). Handled records are marked delivered and the delivery
   state file is fsynced.
2. Hold a streaming subscribe on the reply topic
   ({server}/{topic}/json?since={cursor}) and append each message to
   ~/.tugline/replies.jsonl with consumed=false and the ntfy message
   id as the reply id, saving the cursor after every message. When the
   stream ends (server close or read timeout, default 120s) the daemon
   reconnects with the same cursor. The cursor lives in
   <state_root>/reply_cursor.json as {"last_id": "..."}.
3. Maintain session state from UserPromptSubmit and Stop records in
   the spool (see tugline.core.policy for the file formats). Consuming
   a Stop clears that session's armed marker.

Rate posture: ntfy's default visitor limits are a burst of 60 requests
replenishing one token every 5 seconds, so the daemon makes at most
one reply request per poll_floor seconds (default 6, which keeps the
bucket full). A held stream satisfies that trivially; a failed stream
open falls back to a single bounded poll on the same floor. A tight
poll loop would 429 within a minute.

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
import urllib.parse
import urllib.request
import uuid
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
        stream_timeout=120.0,
        poll_floor=6.0,
        cycle_idle=0.5,
        backoff_base=1.0,
        backoff_cap=30.0,
        max_attempts=5,
        now=time.monotonic,
        log=None,
    ):
        self.config = config
        self.spool_root = os.path.expanduser(spool_root or DEFAULT_SPOOL_ROOT)
        self.replies_path = os.path.expanduser(
            replies_path or DEFAULT_REPLIES_PATH
        )
        self.state_root = os.path.expanduser(
            state_root or policy.DEFAULT_STATE_ROOT
        )
        self.publisher = publisher or Publisher.from_config(config)
        self.sleep = sleep
        self.poll_timeout = poll_timeout
        self.stream_timeout = stream_timeout
        self.poll_floor = poll_floor
        self.cycle_idle = cycle_idle
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_attempts = max_attempts
        self._now = now
        self._log_fn = log or _stderr_log
        self._stop = False
        self.stream_status = None
        self._next_reply_request_at = None

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
        if not record.get("id"):
            raise ValueError("record without id")
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

    # -- reply streaming --

    def stream_replies(self) -> int:
        """Hold the reply stream open and capture messages as they arrive.

        Opens {server}/{reply-topic}/json?since={cursor} as a streaming
        GET and reads it line by line, saving the cursor after every
        message, until the server closes it or the read timeout (120s
        by default) passes; the caller then reconnects with the same
        cursor. Returns the number of messages captured. stream_status
        says "held" once the stream was established and "error" when it
        could not be opened at all (the poll fallback path).
        """
        self.stream_status = "held"
        if not self.config.toggles.replies:
            return 0
        base = self.config.server.url.rstrip("/")
        topic = topics.resolve(topics.REPLY, self.config)
        since = self._load_cursor()
        count = self._open_stream(base, topic, since)
        return count or 0

    def _open_stream(self, base, topic, since):
        url = "%s/%s/json?since=%s" % (
            base,
            urllib.parse.quote(topic),
            urllib.parse.quote(str(since)),
        )
        headers = {}
        token = self.publisher.read_token()
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=self.stream_timeout
            ) as resp:
                return self._read_stream_lines(resp, topic)
        except urllib.error.HTTPError as exc:
            self.stream_status = "error"
            self.log("reply stream failed: HTTP %d" % exc.code)
            return None
        except (urllib.error.URLError, OSError) as exc:
            self.stream_status = "error"
            self.log("reply stream failed: %s" % (getattr(exc, "reason", exc),))
            return None

    def _read_stream_lines(self, resp, topic) -> int:
        count = 0
        try:
            for raw_line in resp:
                if self._stop:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("event") not in (None, "message"):
                    continue
                self._append_reply(message, topic)
                count += 1
                if message.get("id"):
                    self._save_cursor(str(message["id"]))
            return count
        except OSError:
            # the held stream broke mid-read (timeout or reset). The
            # cursor already covers everything captured, so the caller
            # simply reconnects with it.
            return count

    # -- reply polling (the error-path fallback) --

    def poll_replies(self) -> int:
        """One bounded poll of the reply topic; fallback when the
        streaming subscribe cannot be established. Pacing between
        requests is the caller's job (see _pace_reply_request)."""
        if not self.config.toggles.replies:
            return 0
        base = self.config.server.url.rstrip("/")
        topic = topics.resolve(topics.REPLY, self.config)
        since = self._load_cursor()
        url = "%s/%s/json?poll=1&since=%s" % (
            base,
            urllib.parse.quote(topic),
            urllib.parse.quote(str(since)),
        )
        headers = {}
        token = self.publisher.read_token()
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.poll_timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            self.log("reply poll failed: HTTP %d" % exc.code)
            return 0
        except (urllib.error.URLError, OSError) as exc:
            self.log("reply poll failed: %s" % (getattr(exc, "reason", exc),))
            return 0

        count = 0
        last_id = None
        for line in body.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("event") not in (None, "message"):
                continue
            self._append_reply(message, topic)
            count += 1
            if message.get("id"):
                last_id = str(message["id"])
        if last_id:
            self._save_cursor(last_id)
        return count

    def _append_reply(self, message, topic):
        entry = {
            "id": str(message.get("id") or uuid.uuid4().hex[:16]),
            "ts": datetime.now(timezone.utc).isoformat(),
            "topic": message.get("topic") or topic,
            "title": message.get("title") or "",
            "body": message.get("message") or "",
            "consumed": False,
        }
        directory = os.path.dirname(self.replies_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.replies_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _cursor_path(self):
        return os.path.join(self.state_root, "reply_cursor.json")

    def _load_cursor(self):
        try:
            with open(self._cursor_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return data["last_id"]
        except (OSError, ValueError, KeyError, TypeError):
            return str(int(time.time()))

    def _save_cursor(self, last_id):
        os.makedirs(self.state_root, exist_ok=True)
        path = self._cursor_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"last_id": str(last_id)}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    # -- cycle and serve loop --

    def run_once(self):
        """One daemon cycle: flush the spool, then poll replies once."""
        return {"handled": self.flush(), "replies": self.poll_replies()}

    def _pace_reply_request(self):
        """Keep at least poll_floor seconds between reply requests.

        ntfy replenishes one request token every 5 seconds; a 6 second
        floor keeps the visitor bucket full no matter what the server
        does. A held stream blows through the floor naturally (one
        request outlives it), so the sleep only happens after short or
        failed streams.
        """
        now = self._now()
        if self._next_reply_request_at is not None and now < self._next_reply_request_at:
            self.sleep(self._next_reply_request_at - now)
        self._next_reply_request_at = self._now() + self.poll_floor

    def _replies_cycle(self):
        """One reply pass: hold the stream, poll only if it errors."""
        self._pace_reply_request()
        self.stream_replies()
        if self.stream_status == "error":
            self._pace_reply_request()
            self.poll_replies()

    def serve(self):
        """Run cycles until SIGTERM (or a stop request) lands."""
        self._install_signal_handlers()
        self.log("daemon started")
        try:
            while not self._stop:
                self.flush()
                if self._stop:
                    break
                if self.config.toggles.replies:
                    self._replies_cycle()
                elif self.cycle_idle:
                    self.sleep(self.cycle_idle)
        except KeyboardInterrupt:
            pass
        self.log("daemon stopped")

    def request_stop(self):
        self._stop = True

    def _install_signal_handlers(self):
        try:
            signal.signal(signal.SIGTERM, self._on_sigterm)
        except ValueError:
            # Not the main thread (embedded use); stopping goes through
            # request_stop() instead.
            pass

    def _on_sigterm(self, signum, frame):
        self._stop = True


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
