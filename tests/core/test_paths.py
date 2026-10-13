"""Tests for home tree path resolution (tugline.core.paths)."""

import os

from tugline.core import paths


def test_default_home_is_dot_tugline(monkeypatch):
    monkeypatch.delenv("TUGLINE_HOME", raising=False)
    assert paths.home_root() == os.path.expanduser(
        os.path.join("~", ".tugline")
    )


def test_tugline_home_moves_the_home_root(monkeypatch, tmp_path):
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    assert paths.home_root() == str(tmp_path)


def test_tugline_home_value_is_expanded(monkeypatch):
    monkeypatch.setenv("TUGLINE_HOME", os.path.join("~", "tugline-alt"))
    assert paths.home_root() == os.path.expanduser(
        os.path.join("~", "tugline-alt")
    )


def test_one_env_var_moves_every_derived_root(monkeypatch, tmp_path):
    monkeypatch.setenv("TUGLINE_HOME", str(tmp_path))
    home = str(tmp_path)
    assert paths.spool_root() == os.path.join(home, "spool")
    assert paths.state_root() == os.path.join(home, "state")
    assert paths.logs_root() == os.path.join(home, "logs")
    assert paths.store_root() == os.path.join(home, "store")
    assert paths.store_path() == os.path.join(home, "store", "tugline.db")
    assert paths.approvals_root() == os.path.join(
        home, "state", "approvals"
    )


def test_default_roots_stay_under_one_home(monkeypatch):
    monkeypatch.delenv("TUGLINE_HOME", raising=False)
    home = paths.home_root()
    for root in (
        paths.spool_root(),
        paths.state_root(),
        paths.logs_root(),
        paths.store_root(),
    ):
        assert root.startswith(home + os.sep)
    assert paths.approvals_root() == os.path.join(
        paths.state_root(), "approvals"
    )
    assert paths.store_path() == os.path.join(
        paths.store_root(), "tugline.db"
    )
