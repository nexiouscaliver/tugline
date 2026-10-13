"""The approve receiver: the loopback HTTP listener behind the buttons.

The adapters publish Approve and Deny http action buttons whose URLs
point at /approvals/<nonce>. When the phone presses one, the request
lands here: directly on 127.0.0.1 when the press comes from the
machine, or through the operator's reverse proxy path when the
buttons are pointed at an external URL. A valid press carries the
same bearer token as every other tugline request, names a nonce the
adapter registered as a <nonce>.pending file, and a decision of
allow or deny. The receiver writes <state root>/approvals/<nonce>.json
atomically, removes the pending marker, and the approve module
polling that file picks the decision up.

Anything else is refused without ceremony: 403 for a bad token, 404
for an unknown nonce or path, 400 for a decision that is neither
allow nor deny. A request never crashes the listener; the worst case
is a one-line 500.

The bind is loopback only, the same posture as a self-hosted ntfy on
this machine. A token file with no token in it means open access
(again the same posture as ntfy); configure the token before turning
the approve toggle on for real.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from tugline.core import policy

DEFAULT_APPROVE_PORT = 2587
_NONCE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class _ReceiverServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, receiver):
        super().__init__(address, _Handler)
        self.receiver = receiver


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self._route()

    def do_GET(self):
        self._route()

    def _route(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, text = self.server.receiver.handle_request(
                self.command, self.path, self.headers, body
            )
        except Exception:  # a broken request must never kill the thread
            status, text = 500, "receiver error"
        payload = (text + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ApproveReceiver:
    def __init__(self, config, state_root=None, read_token=None, log=None):
        server_cfg = config.server
        self.host = "127.0.0.1"
        port = getattr(server_cfg, "approve_port", 0) or 0
        self.requested_port = int(port)
        self.port = None  # the bound port, once started
        self.state_root = os.path.expanduser(
            state_root or policy.DEFAULT_STATE_ROOT
        )
        self.approvals_dir = os.path.join(self.state_root, "approvals")
        if read_token is None:
            from tugline.core.publisher import Publisher

            self.read_token = Publisher.from_config(config).read_token
        else:
            self.read_token = read_token
        self._log = log or (lambda line: None)
        self._server = None
        self._thread = None

    # -- lifecycle --

    def start(self):
        if self._server is not None:
            return True
        try:
            self._server = _ReceiverServer(
                (self.host, self.requested_port), self
            )
        except OSError as exc:
            self._log("approve receiver not started: %s" % exc)
            self._server = None
            return False
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="tugline-approve-receiver",
            daemon=True,
        )
        self._thread.start()
        self._log(
            "approve receiver listening on 127.0.0.1:%d" % self.port
        )
        return True

    def stop(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        self.port = None

    # -- request handling --

    def handle_request(self, method, path, headers, body=b""):
        parts = urlsplit(path)
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) != 2 or segments[0] != "approvals":
            return 404, "unknown endpoint"
        nonce = segments[1]
        if not _NONCE.match(nonce):
            return 404, "unknown nonce"

        expected = self.read_token()
        if expected:
            provided = headers.get("Authorization") or ""
            if provided != "Bearer " + expected:
                return 403, "bad token"

        decision = self._decision_value(parts, body)
        if decision not in ("allow", "deny"):
            return 400, "decision must be allow or deny"

        pending = os.path.join(self.approvals_dir, nonce + ".pending")
        target = os.path.join(self.approvals_dir, nonce + ".json")
        if not os.path.exists(pending):
            if os.path.exists(target):
                return 200, "already answered"
            return 404, "unknown nonce"

        self._write_decision(target, decision)
        try:
            os.remove(pending)
        except OSError:
            pass  # the decision is already recorded; the marker is stale
        return 200, "decision recorded: %s" % decision

    def _decision_value(self, parts, body):
        query = parse_qs(parts.query)
        if query.get("decision"):
            return str(query["decision"][0]).strip().lower()
        try:
            form = parse_qs(body.decode("utf-8", "replace"))
        except ValueError:
            return ""
        if form.get("decision"):
            return str(form["decision"][0]).strip().lower()
        return ""

    def _write_decision(self, target, decision):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        payload = json.dumps(
            {
                "decision": decision,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            separators=(",", ":"),
        )
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
