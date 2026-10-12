"""Tests for the tug CLI (tugline.cli.main).

main(argv) follows the argparse convention: argv is the argument list WITHOUT
the program name (i.e. sys.argv[1:]).
"""

import pytest

from tugline import __version__
from tugline.cli import main


def test_version_prints_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out


def test_hello_prints_one_line(capsys):
    code = main(["hello"])
    assert code == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert lines[0] == "hello from tugline"


def test_hello_mentions_project(capsys):
    code = main(["hello"])
    assert code == 0
    assert "tugline" in capsys.readouterr().out


def test_no_command_prints_usage_and_fails(capsys):
    code = main([])
    assert code == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower() or "usage" in captured.out.lower()


def test_unknown_command_fails(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["bogus"])
    assert excinfo.value.code == 2
