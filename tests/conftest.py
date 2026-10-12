"""Shared test setup: the fake ntfy server fixture."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from fake_ntfy import start_fake_ntfy  # noqa: E402


@pytest.fixture
def fake_ntfy():
    server = start_fake_ntfy()
    yield server
    server.shutdown()
    server.server_close()
