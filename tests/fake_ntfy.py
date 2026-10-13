"""In-process fake ntfy server for tests.

Records every request (method, path, headers, body), can be told to
fail the next N POSTs or GETs with 500, to delay responses, and
serves the reply topic two ways, mirroring the real server:

- poll requests (poll=1 in the query) get the whole backlog in one
  body and the connection closes;
- subscribe requests (no poll parameter) get a held ndjson stream:
  the backlog flows line by line, late arrivals flow as they are
  queued, and the stream closes after a quiet grace period (or holds
  open in "hold" mode until the server shuts down). A numeric since
  value filters the backlog by message time, like the real cache.

Every request passes a rate limiter carrying the real ntfy default
visitor numbers: burst 60, one token replenished every 5 seconds. A
request with an empty bucket gets a 429 and bumps the rate_limited
counter. The clock is injectable so long runs can be simulated.
"""

import json
import select
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_BURST = 60
DEFAULT_REPLENISH_SECONDS = 5.0


class RateLimiter:
    """Token bucket with ntfy's default visitor request limits."""

    def __init__(
        self,
        burst=DEFAULT_BURST,
        replenish_seconds=DEFAULT_REPLENISH_SECONDS,
        clock=time.time,
    ):
        self.burst = float(burst)
        self.replenish_seconds = float(replenish_seconds)
        self.clock = clock
        self.tokens = float(burst)
        self.last = clock()

    def allow(self, now=None):
        now = self.clock() if now is None else now
        elapsed = max(0.0, float(now) - self.last)
        self.last = float(now)
        self.tokens = min(self.burst, self.tokens + elapsed / self.replenish_seconds)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


def _passes_since(since, message):
    """Backlog visibility under a since value, like the real cache."""
    if not since or since == "all":
        return True
    if since.isdigit():
        return int(message.get("time") or 0) >= int(since)
    return True  # an id cursor: everything still queued came after it


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _record(self, method, body=b"", status=None):
        rec = {
            "method": method,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body.decode("utf-8", "replace"),
        }
        if status is not None:
            rec["status"] = status
        with self.server.lock:
            self.server.requests.append(rec)
        return rec

    def _gate(self, method, body=b""):
        """Count the request against the visitor bucket; 429 on empty."""
        allowed = self.server.limiter.allow(self.server.clock())
        self._record(method, body, status=200 if allowed else 429)
        if allowed:
            return True
        with self.server.lock:
            self.server.rate_limited += 1
        payload = b"429: too many requests\n"
        self.send_response(429)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        return False

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if not self._gate("POST", body):
            return
        if self.server.delay:
            time.sleep(self.server.delay)
        with self.server.lock:
            fail = self.server.fail_posts > 0
            if fail:
                self.server.fail_posts -= 1
        if fail:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        with self.server.lock:
            served = len(self.server.requests)
        payload = json.dumps({"id": "srv-%d" % served, "time": int(time.time())})
        data = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._gate("GET"):
            return
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path.endswith("/json") and "poll" not in query:
            self._serve_stream(query)
            return
        if self.server.delay:
            time.sleep(self.server.delay)
        with self.server.lock:
            fail = self.server.fail_gets > 0
            if fail:
                self.server.fail_gets -= 1
            replies = list(self.server.replies)
            self.server.replies = []
        if fail:
            self.send_response(500)
            self.end_headers()
            return
        body = "".join(json.dumps(m) + "\n" for m in replies).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- held streaming subscribe --

    def _serve_stream(self, query):
        since = (query.get("since") or [""])[0]
        with self.server.lock:
            fail = self.server.fail_gets > 0
            if fail:
                self.server.fail_gets -= 1
            reject = (
                self.server.reject_since_all > 0 and since == "all"
            )
            if reject:
                self.server.reject_since_all -= 1
        if reject:
            payload = b"413: request too large\n"
            self.send_response(413)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if fail:
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self._stream_lines(since)
        except OSError:
            return  # the client hung up (its read timeout fired)

    def _client_gone(self):
        """True when the subscriber closed its side (read timeout)."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
        except (OSError, ValueError):
            return True
        if not ready:
            return False
        try:
            data = self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True
        return data == b""

    def _stream_lines(self, since):
        delivered = 0
        quiet_deadline = time.time() + self.server.stream_grace
        while True:
            if self._client_gone():
                return
            batch = self._take_replies(since)
            if batch:
                try:
                    for message in batch:
                        self.wfile.write((json.dumps(message) + "\n").encode("utf-8"))
                    self.wfile.flush()
                except OSError:
                    # the client hung up between take and write; put the
                    # batch back so the next connection gets it
                    with self.server.lock:
                        self.server.replies = batch + self.server.replies
                        self.server.cond.notify_all()
                    return
                delivered += len(batch)
                quiet_deadline = time.time() + self.server.stream_grace
                close_after = self.server.stream_close_after
                if close_after is not None and delivered >= close_after:
                    return
                continue
            if self.server.stream_mode == "hold":
                if not self._wait_for_replies(0.05):
                    return  # server shutting down
                continue
            if time.time() >= quiet_deadline:
                return
            self._wait_for_replies(0.02)

    def _take_replies(self, since):
        with self.server.lock:
            send = []
            keep = []
            for message in self.server.replies:
                if _passes_since(since, message):
                    send.append(message)
                else:
                    keep.append(message)
            self.server.replies = keep
        return send

    def _wait_for_replies(self, timeout):
        with self.server.cond:
            if not self.server.replies and not self.server._closing:
                self.server.cond.wait(timeout)
            return not self.server._closing


class FakeNtfy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, clock=None):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.cond = threading.Condition()
        # one lock everywhere: the condition's own lock, so queue_reply
        # can wake held streams without a second lock to juggle
        self.lock = self.cond
        self.requests = []
        self.fail_posts = 0
        self.fail_gets = 0
        self.delay = 0.0
        self.replies = []
        self.clock = clock or time.time
        self.limiter = RateLimiter(clock=self.clock)
        self.rate_limited = 0
        self.reject_since_all = 0
        self.stream_mode = "backlog"  # backlog: close when quiet; hold: stay open
        self.stream_grace = 0.1  # quiet seconds before a backlog stream closes
        self.stream_close_after = None  # hold mode: close after N delivered lines
        self._closing = False

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]

    def shutdown(self):
        with self.cond:
            self._closing = True
            self.cond.notify_all()
        super().shutdown()

    def posts(self, path=None):
        with self.lock:
            return [
                r
                for r in self.requests
                if r["method"] == "POST"
                and (path is None or r["path"] == path)
            ]

    def gets(self):
        with self.lock:
            return [r for r in self.requests if r["method"] == "GET"]

    def queue_reply(
        self, message, title=None, msg_id=None, topic="tug-reply", t=None
    ):
        with self.cond:
            self.replies.append(
                {
                    "id": msg_id or ("r%d" % (len(self.requests) + 1)),
                    "time": int(t) if t is not None else int(time.time()),
                    "event": "message",
                    "topic": topic,
                    "title": title or "",
                    "message": message,
                }
            )
            self.cond.notify_all()


def start_fake_ntfy(clock=None):
    server = FakeNtfy(clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
