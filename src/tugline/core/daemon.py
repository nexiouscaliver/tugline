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

import time


class Daemon:
    def __init__(self, config, **kw):
        self.config = config

    def flush(self) -> int:
        raise NotImplementedError("daemon flush not implemented yet")
