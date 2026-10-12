"""One-shot ntfy publisher over urllib.

One publish call, one HTTP request, one result object. Retries and
backoff belong to the daemon, not here.

The bearer token is read from the token file on each call and sent in
the Authorization header only. It is never included in any error
message, log line, or result field. A missing or empty token file
means the request goes out anonymous, which ntfy accepts when access
is open.

Action buttons (X-Actions, ntfy format "key=value" pairs joined by
commas, actions joined by semicolons) are capped at three per message,
matching what a notification shade renders reliably.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

MAX_ACTIONS = 3


@dataclass
class PublishResult:
    ok: bool
    status: int | None
    message_id: str | None = None
    error: str | None = None


class Publisher:
    def __init__(self, url, token_file=None, timeout=5.0):
        self.url = url.rstrip("/")
        self.token_file = token_file
        self.timeout = timeout

    @classmethod
    def from_config(cls, config, timeout=5.0):
        return cls(config.server.url, config.server.token_file, timeout)

    def read_token(self):
        if not self.token_file:
            return None
        try:
            with open(os.path.expanduser(self.token_file), encoding="utf-8") as fh:
                for line in fh:
                    token = line.strip()
                    if token:
                        return token
        except OSError:
            return None
        return None

    def publish(
        self,
        topic,
        body,
        title=None,
        priority=None,
        tags=None,
        click=None,
        actions=None,
    ):
        headers = {}
        token = self.read_token()
        if token:
            headers["Authorization"] = "Bearer " + token
        if title:
            headers["X-Title"] = str(title)
        if priority:
            headers["X-Priority"] = str(priority)
        if tags:
            headers["X-Tags"] = ",".join(str(t) for t in tags)
        if click:
            headers["X-Click"] = str(click)
        action_header = _format_actions(actions)
        if action_header:
            headers["X-Actions"] = action_header

        request = urllib.request.Request(
            self.url + "/" + str(topic).strip("/"),
            data=str(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read(4096)
        except urllib.error.HTTPError as exc:
            return PublishResult(
                ok=False, status=exc.code, error="HTTP %d posting to %s" % (exc.code, topic)
            )
        except urllib.error.URLError as exc:
            return PublishResult(
                ok=False, status=None, error="request failed: %s" % (exc.reason,)
            )
        except OSError as exc:
            return PublishResult(False, None, error="request failed: %s" % (exc,))

        message_id = None
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                message_id = parsed.get("id")
        except (ValueError, UnicodeDecodeError):
            pass
        return PublishResult(ok=200 <= status < 300, status=status, message_id=message_id)


def _format_actions(actions):
    if not actions:
        return None
    parts = []
    for action in list(actions)[:MAX_ACTIONS]:
        if not isinstance(action, dict):
            continue
        pieces = []
        for key in ("action", "label", "url", "method", "body", "headers"):
            value = action.get(key)
            if value is not None:
                pieces.append("%s=%s" % (key, value))
        if pieces:
            parts.append(", ".join(pieces))
    return "; ".join(parts) if parts else None
