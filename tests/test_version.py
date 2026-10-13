"""Version truth: tugline.__version__ must agree with the project version.

The 0.2.0 cutover shipped with src/tugline/__init__.py still pinning
0.1.0, so `tug version` misreported on the released install (found live
by the controller during the cutover). These tests pin the agreement
that keeps the reported version truthful.
"""

import os
import sys

import pytest

import tugline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pyproject_version():
    import tomllib

    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="tomllib only ships in the stdlib from 3.11",
)
def test_dunder_version_matches_pyproject():
    assert tugline.__version__ == _pyproject_version()


def test_dunder_version_matches_installed_metadata():
    import importlib.metadata

    try:
        installed = importlib.metadata.version("tugline")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("tugline is not installed in this environment")
    assert tugline.__version__ == installed
