"""Filesystem layout for the Claude Code adapter.

Everything lives under the tugline home (~/.tugline by default). The
TUGLINE_HOME environment variable moves the whole tree at once; that
is the knob the tests use, and it is the only knob. Nothing here
does any IO, so callers can compose paths cheaply on the hot path.
"""

from __future__ import annotations

import os

DEFAULT_HOME = "~/.tugline"
LOG_FILE = "adapter-claude.log"


def home_root() -> str:
    return os.path.expanduser(os.environ.get("TUGLINE_HOME") or DEFAULT_HOME)


def spool_root() -> str:
    return os.path.join(home_root(), "spool")


def state_root() -> str:
    return os.path.join(home_root(), "state")


def approvals_root() -> str:
    return os.path.join(state_root(), "approvals")


def log_path() -> str:
    return os.path.join(home_root(), "logs", LOG_FILE)
