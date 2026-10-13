"""Background ingest and health probes for the console.

IngestThread folds the spool into the store on a fixed interval:
ingest new records, refresh delivery state, reconcile threads against
the approvals directory, then hand a small delta to the on_tick
listener (which the server uses to push SSE events). The store owns
all write serialization; this thread only decides when to call.

The health helpers are separate because /api/state needs them on
demand: ntfy_health does one bounded GET against the server, and
daemon_health looks for a running `tug daemon` plus the age of
delivered.json as the last flush hint. Both degrade to a string
("unreachable", "unknown") instead of raising.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone

from tugline.core import paths, spool


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class IngestThread(threading.Thread):
    def __init__(self, store, spool_root, approvals_root, interval=2.0,
                 on_tick=None):
        super().__init__(name="tugline-console-ingest", daemon=True)
        self.store = store
        self.spool_root = str(spool_root)
        self.approvals_root = str(approvals_root)
        self.interval = float(interval)
        self.on_tick = on_tick
        self.last_tick_ts = None
        self._stop_event = threading.Event()
        self._listener_lock = threading.Lock()

    def run(self):
        while not self._stop_event.is_set():
            delta = self.tick()
            if delta is not None and self.on_tick is not None:
                with self._listener_lock:
                    self.on_tick(delta)
            if self._stop_event.wait(self.interval):
                break

    def tick(self):
        """One fold pass; returns the delta dict for the listener."""
        counts = {}
        counts.update(self.store.ingest_spool(self.spool_root))
        counts["delivered"] = self.store.refresh_delivery(self.spool_root)
        counts.update(self.store.refresh_threads(self.approvals_root))
        self.last_tick_ts = _now_iso()
        rows = self.store.events(limit=1)
        return {
            "last_event_id": rows[0]["id"] if rows else None,
            "counts": counts,
            "ts": self.last_tick_ts,
        }

    def stop(self):
        self._stop_event.set()


def ntfy_health(config) -> str:
    """One bounded GET on <server url>/v1/health: ok or unreachable."""
    url = config.server.url.rstrip("/") + "/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
        if 200 <= int(status) < 300:
            return "ok"
    except Exception:  # any network shape reads the same from outside
        pass
    return "unreachable"


def daemon_health(spool_root=None) -> dict:
    """Is a `tug daemon` process alive, and how old is the last flush.

    pgrep -f answers running (a match) or stopped (no match); any
    failure, including no pgrep on the box, reads unknown. The age of
    <spool root>/delivered.json is the last_flush hint, None when the
    daemon has never flushed.
    """
    status = "unknown"
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "tug daemon"], capture_output=True
        )
        if proc.returncode == 0:
            status = "running"
        elif proc.returncode == 1:
            status = "stopped"
    except Exception:
        status = "unknown"
    root = str(spool_root) if spool_root is not None else paths.spool_root()
    age = None
    try:
        mtime = os.path.getmtime(os.path.join(root, spool.DELIVERED_FILE))
        age = max(0.0, time.time() - mtime)
    except OSError:
        age = None
    return {"daemon": status, "last_flush_age_s": age}
