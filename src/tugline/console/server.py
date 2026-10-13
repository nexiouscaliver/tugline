"""The console HTTP API: loopback only, store-backed, SSE-pushed.

ConsoleServer owns two threads: an ingest thread folding the spool
into the store on a fixed interval, and a ThreadingHTTPServer serving
127.0.0.1 only. The bind address is the security boundary for reads;
write endpoints (POST) additionally require the same bearer token the
rest of tugline uses when the configured token file holds one, the
same posture as the approve receiver.

Replies written here are recorded twice with one meaning: a store row
with source=console for the console itself, and a line appended to
<home>/replies.jsonl in the exact daemon entry format (plus a source
key) so the existing reply consumers see console replies as first
class. Each line is truncated to stay under 4096 bytes so one huge
paste cannot wedge a consumer.

A request never kills a thread: every handler path funnels into one
try block whose worst case is a one-line JSON 500.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tugline.console.ingest import IngestThread, daemon_health, ntfy_health
from tugline.core import approval_files, spool

DEFAULT_PORT = 2588
SSE_PING_SECONDS = 15.0
REPLY_LINE_LIMIT = 4096
REPLY_TITLE = "console reply"
_NONCE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_INDEX_HTML = Path(__file__).resolve().parent / "web" / "index.html"


class _Raw:
    """A pre-encoded response body with its own content type."""

    def __init__(self, body, content_type):
        self.body = body
        self.content_type = content_type


class _ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, console):
        super().__init__(address, _Handler)
        self.console = console


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        console = self.server.console
        if method == "GET" and urlsplit(self.path).path == "/api/events/stream":
            try:
                console.stream_state(self)
            except Exception:
                pass  # the stream already ended; nothing left to send
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload = console.handle_request(
                method, self.path, self.headers, body
            )
        except Exception:  # a broken request must never kill the thread
            status, payload = 500, {"error": "console error"}
        if isinstance(payload, _Raw):
            data, content_type = payload.body, payload.content_type
        else:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            content_type = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ConsoleServer:
    def __init__(self, config, store, home=None, *, port=None,
                 ingest_interval=2.0, read_token=None):
        self.config = config
        self.store = store
        self.home = str(home) if home is not None else _home_root()
        self.spool_root = os.path.join(self.home, "spool")
        self.approvals_root = os.path.join(self.home, "state", "approvals")
        self.replies_path = os.path.join(self.home, "replies.jsonl")
        requested = port if port is not None else getattr(
            self.config.server, "console_port", DEFAULT_PORT
        )
        self.requested_port = int(requested)
        self.port = None  # the bound port, once started
        self.ingest_interval = float(ingest_interval)
        if read_token is None:
            from tugline.core.publisher import Publisher

            read_token = Publisher.from_config(self.config).read_token
        self.read_token = read_token
        self._httpd = None
        self._thread = None
        self._ingest = None
        self._stopping = False
        self._delta_lock = threading.Lock()
        self._delta_event = threading.Event()
        self._latest_delta = None
        self._last_ingest_ts = None

    # -- lifecycle --

    def start(self):
        """Bind loopback and start serving plus ingesting."""
        if self._httpd is not None:
            return True
        self._stopping = False
        try:
            self._httpd = _ConsoleHTTPServer(("127.0.0.1", self.requested_port), self)
        except OSError:
            self._httpd = None
            return False
        self.port = self._httpd.server_address[1]
        self._ingest = IngestThread(
            self.store,
            self.spool_root,
            self.approvals_root,
            interval=self.ingest_interval,
            on_tick=self._on_tick,
        )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="tugline-console-http",
            daemon=True,
        )
        self._ingest.start()
        self._thread.start()
        return True

    def stop(self):
        """Shut the ingest thread and the HTTP server down cleanly."""
        self._stopping = True
        self._delta_event.set()  # wake any stream loop so it can exit
        if self._ingest is not None:
            self._ingest.stop()
            self._ingest.join(timeout=5)
            self._ingest = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self.port = None

    def _on_tick(self, delta):
        with self._delta_lock:
            self._latest_delta = delta
            self._last_ingest_ts = delta.get("ts")
        self._delta_event.set()

    # -- request handling --

    def handle_request(self, method, path, headers, body=b""):
        parts = urlsplit(path)
        segments = [s for s in parts.path.split("/") if s]
        query = parse_qs(parts.query)

        if not segments:
            if method != "GET":
                return 404, {"error": "unknown endpoint"}
            return self._index()
        if segments[0] != "api" or len(segments) < 2:
            return 404, {"error": "unknown endpoint"}

        name = segments[1]
        if method == "GET":
            return self._get(name, segments, query)
        if method == "POST":
            expected = self.read_token()
            if expected:
                provided = headers.get("Authorization") or ""
                if provided != "Bearer " + expected:
                    return 403, {"error": "bad token"}
            return self._post(name, segments, query, body)
        return 404, {"error": "unknown endpoint"}

    def _index(self):
        try:
            page = _INDEX_HTML.read_bytes()
        except OSError:
            return 500, {"error": "console page missing"}
        return 200, _Raw(page, "text/html; charset=utf-8")

    def _get(self, name, segments, query):
        if name == "state" and len(segments) == 2:
            return 200, self.state_payload()
        if name == "sessions" and len(segments) == 2:
            return 200, {"sessions": self.store.sessions()}
        if name == "events" and len(segments) == 2:
            limit = _int_param(query, "limit", 50)
            if limit is None:
                return 400, {"error": "limit must be an integer"}
            session = (query.get("session") or [None])[0]
            return 200, {"events": self.store.events(limit=limit, session_id=session)}
        if name == "threads" and len(segments) == 2:
            status = (query.get("status") or [None])[0]
            return 200, {"threads": self.store.threads(status=status)}
        if name == "replies" and len(segments) == 2:
            limit = _int_param(query, "limit", 50)
            if limit is None:
                return 400, {"error": "limit must be an integer"}
            return 200, {"replies": self.store.replies(limit=limit)}
        return 404, {"error": "unknown endpoint"}

    def _post(self, name, segments, query, body):
        if name == "replies" and len(segments) == 2:
            return self._post_reply(body)
        if name == "approvals" and len(segments) == 3:
            return self._post_approval(segments[2], query)
        return 404, {"error": "unknown endpoint"}

    def _post_reply(self, body):
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return 400, {"error": "body must be json"}
        if not isinstance(data, dict):
            return 400, {"error": "body must be a json object"}
        thread_nonce = data.get("thread_nonce")
        session_id = data.get("session_id")
        if (bool(thread_nonce)) == (bool(session_id)):
            return 400, {"error": "exactly one of thread_nonce or session_id"}
        if not isinstance(data.get("body"), str):
            return 400, {"error": "body must be a string"}
        try:
            row = self.store.add_reply(
                data["body"],
                thread_nonce=thread_nonce,
                session_id=session_id,
                source="console",
            )
        except KeyError:
            return 404, {"error": "unknown thread nonce"}
        self._append_reply_line(row)
        return 200, {"ok": True, "reply": row}

    def _post_approval(self, nonce, query):
        if not _NONCE.match(nonce):
            return 404, {"error": "unknown nonce"}
        values = query.get("decision") or []
        decision = str(values[0]).strip().lower() if values else ""
        if decision not in approval_files.VALID_DECISIONS:
            return 400, {"error": "decision must be allow or deny"}
        root = self.approvals_root
        pending = approval_files.pending_path(root, nonce)
        target = approval_files.decision_path(root, nonce)
        existing = approval_files.read_decision(root, nonce)
        if existing is not None and not os.path.exists(pending):
            return 200, {
                "ok": True,
                "status": "already answered",
                "decision": existing["decision"],
            }
        if not os.path.exists(pending) and existing is None:
            return 404, {"error": "unknown nonce"}
        approval_files.write_decision(root, nonce, decision)
        approval_files.remove_pending(root, nonce)
        saved = approval_files.read_decision(root, nonce) or {}
        try:
            self.store.record_answer(nonce, decision, saved.get("ts"))
        except KeyError:
            pass  # the thread row may not be ingested yet; the file is truth
        return 200, {"ok": True, "status": "answered", "decision": decision}

    # -- state and replies.jsonl --

    def state_payload(self):
        """One read for a console render: the store snapshot plus
        health, spool depth, and when the ingest last ran."""
        payload = self.store.snapshot()
        health = daemon_health(self.spool_root)
        health["ntfy"] = ntfy_health(self.config)
        payload["health"] = health
        payload["spool_depth"] = sum(1 for _ in spool.undelivered(self.spool_root))
        payload["last_ingest_ts"] = self._last_ingest_ts
        return payload

    def _append_reply_line(self, row):
        entry = {
            "id": uuid.uuid4().hex[:16],
            "ts": row.get("ts"),
            "topic": self.config.topics.reply,
            "title": REPLY_TITLE,
            "body": str(row.get("body") or ""),
            "consumed": False,
            "source": "console",
        }
        line = _fit_line(entry)
        directory = os.path.dirname(self.replies_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.replies_path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()

    # -- SSE --

    def stream_state(self, handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        snapshot = self.state_payload()
        self._write_event(handler, "snapshot", snapshot)
        while not self._stopping:
            triggered = self._delta_event.wait(timeout=SSE_PING_SECONDS)
            if self._stopping:
                break
            if not triggered:
                self._write_event(handler, None, None)  # ": ping" keepalive
                continue
            self._delta_event.clear()
            with self._delta_lock:
                delta = self._latest_delta
            if delta is not None:
                self._write_event(handler, "delta", delta)

    def _write_event(self, handler, event, data):
        """One SSE frame; a dead client ends the stream quietly."""
        try:
            if event is None:
                handler.wfile.write(b": ping\n\n")
            else:
                payload = json.dumps(data, separators=(",", ":"))
                handler.wfile.write(
                    ("event: %s\ndata: %s\n\n" % (event, payload)).encode("utf-8")
                )
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise _ClientGone from None


class _ClientGone(Exception):
    """The SSE peer disconnected; stop streaming to it."""


def _home_root():
    from tugline.core import paths

    return paths.home_root()


def _int_param(query, key, default):
    values = query.get(key)
    if not values:
        return default
    try:
        return int(str(values[0]).strip())
    except ValueError:
        return None


def _fit_line(entry):
    """Serialize one replies.jsonl entry as a line under the byte cap.

    The body is the only field worth cutting: everything else is short
    by construction. A coarse byte slice lands under the cap, then a
    small trim loop covers json escaping at the seam.
    """
    def line_of(item):
        return json.dumps(item, separators=(",", ":")) + "\n"

    line = line_of(entry)
    if len(line.encode("utf-8")) < REPLY_LINE_LIMIT:
        return line
    body = str(entry.get("body") or "")
    base = line_of(dict(entry, body=""))
    budget = max(0, REPLY_LINE_LIMIT - 1 - len(base.encode("utf-8")))
    entry["body"] = body.encode("utf-8")[:budget].decode("utf-8", "ignore")
    while entry["body"]:
        line = line_of(entry)
        if len(line.encode("utf-8")) < REPLY_LINE_LIMIT:
            return line
        cut = max(1, len(entry["body"]) // 4)
        entry["body"] = entry["body"][: len(entry["body"]) - cut]
    return line_of(entry)
