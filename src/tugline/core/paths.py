"""Path resolution for the tugline home tree.

Everything tugline writes lives under one home root so a single
TUGLINE_HOME moves the whole tree: tests point it at a temp dir, and
an operator can keep a second instance side by side.

- spool: notification day files and delivered.json (core.spool)
- state: arm markers and session state (core.policy), with approval
  nonces under state/approvals
- logs: runtime logs
- store: the sqlite session store (core.store), one file tugline.db

TUGLINE_CONFIG still overrides the config file location alone; that
wiring lands in a later change, and this module only resolves paths.
Pure functions, no IO beyond expanduser.
"""

from __future__ import annotations

import os

HOME_ENV = "TUGLINE_HOME"
DEFAULT_HOME = os.path.join("~", ".tugline")


def home_root() -> str:
    """expanduser(TUGLINE_HOME) when set, ~/.tugline otherwise."""
    return os.path.expanduser(os.environ.get(HOME_ENV) or DEFAULT_HOME)


def spool_root() -> str:
    """Day files and delivered.json live here."""
    return os.path.join(home_root(), "spool")


def state_root() -> str:
    """Arm markers, session state, and approvals live here."""
    return os.path.join(home_root(), "state")


def logs_root() -> str:
    """Runtime logs live here."""
    return os.path.join(home_root(), "logs")


def store_root() -> str:
    """The sqlite store directory."""
    return os.path.join(home_root(), "store")


def store_path() -> str:
    """The sqlite database file, <home>/store/tugline.db."""
    return os.path.join(store_root(), "tugline.db")


def approvals_root() -> str:
    """Approval nonce markers, <home>/state/approvals."""
    return os.path.join(state_root(), "approvals")
