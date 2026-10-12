"""In-process fake ntfy server for tests.

Records every request (method, path, headers, body), can be told to
fail the next N POSTs with 500, to delay responses, and serves queued
reply messages on GET <topic>/json poll calls. Binds 127.0.0.1 on an
ephemeral port; no real network involved.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _record(self, method, body=b""):
        rec = {
            "method": method,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body.decode("utf-8", "replace"),
        }
        with self.server.lock:
            self.server.requests.append(rec)
        return rec

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._record("POST", body)
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
        self._record("GET")
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


class FakeNtfy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.requests = []
        self.fail_posts = 0
        self.fail_gets = 0
        self.delay = 0.0
        self.replies = []

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]

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

    def queue_reply(self, message, title=None, msg_id=None, topic="tug-reply"):
        with self.lock:
            self.replies.append(
                {
                    "id": msg_id or ("r%d" % (len(self.requests) + 1)),
                    "time": int(time.time()),
                    "event": "message",
                    "topic": topic,
                    "title": title or "",
                    "message": message,
                }
            )


def start_fake_ntfy():
    server = FakeNtfy()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
