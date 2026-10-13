"""Tests for the shared approval decision files (core.approval_files)."""

import json
import os
from datetime import datetime

import pytest

from tugline.core import approval_files


def test_register_pending_writes_marker(tmp_path):
    root = str(tmp_path / "approvals")
    approval_files.register_pending(root, "n1")
    path = os.path.join(root, "n1.pending")
    assert os.path.exists(path)
    data = json.loads(open(path, encoding="utf-8").read())
    assert isinstance(data["ts"], float)


def test_register_pending_creates_the_root(tmp_path):
    root = str(tmp_path / "deep" / "approvals")
    approval_files.register_pending(root, "n1")
    assert os.path.exists(os.path.join(root, "n1.pending"))


def test_write_decision_allow_and_deny(tmp_path):
    root = str(tmp_path / "approvals")
    for nonce, decision in (("n1", "allow"), ("n2", "deny")):
        path = approval_files.write_decision(root, nonce, decision)
        assert path == os.path.join(root, nonce + ".json")
        data = json.loads(open(path, encoding="utf-8").read())
        assert data["decision"] == decision
        datetime.fromisoformat(data["ts"])  # a real ISO timestamp


def test_write_decision_rejects_unknown_value(tmp_path):
    with pytest.raises(ValueError):
        approval_files.write_decision(str(tmp_path), "n1", "maybe")


def test_read_decision_round_trip(tmp_path):
    root = str(tmp_path)
    approval_files.write_decision(root, "n1", "allow")
    data = approval_files.read_decision(root, "n1")
    assert data["decision"] == "allow"
    assert data["ts"]


def test_read_decision_missing_returns_none(tmp_path):
    assert approval_files.read_decision(str(tmp_path), "gone") is None


def test_read_decision_malformed_returns_none(tmp_path):
    root = str(tmp_path)
    bad = (
        "not json at all\n",
        "[1, 2, 3]\n",
        '{"ts": "2026-10-13T00:00:00+00:00"}\n',
        '{"decision": ""}\n',
    )
    for i, text in enumerate(bad):
        with open(os.path.join(root, "n%d.json" % i), "w") as fh:
            fh.write(text)
        assert approval_files.read_decision(root, "n%d" % i) is None


def test_read_decision_keeps_extra_keys(tmp_path):
    root = str(tmp_path)
    path = os.path.join(root, "n1.json")
    with open(path, "w") as fh:
        fh.write('{"decision": "deny", "ts": "t", "message": "why"}\n')
    data = approval_files.read_decision(root, "n1")
    assert data["decision"] == "deny"
    assert data["message"] == "why"


def test_remove_pending_deletes_marker(tmp_path):
    root = str(tmp_path)
    approval_files.register_pending(root, "n1")
    approval_files.remove_pending(root, "n1")
    assert not os.path.exists(os.path.join(root, "n1.pending"))


def test_remove_pending_missing_is_fine(tmp_path):
    approval_files.remove_pending(str(tmp_path), "never-there")
