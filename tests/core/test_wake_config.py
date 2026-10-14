"""Tests for the wake config keys (tugline.core.config).

The [wake] section is optional: a config from before the feature (the
live machine's file) must keep loading with the defaults. The claude
command template is an array of strings, which the Python 3.10 mini
parser must read too (CI runs a 3.10 leg).
"""

import sys

import pytest

from tugline.core import config as cfg


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TUGLINE_CONFIG", raising=False)
    return tmp_path


def write_config(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


WAKE_SECTION = """\
[server]
url = "http://127.0.0.1:2599"
token_file = "none"

[topics]
main = "tug"
warn = "tug-warn"
reply = "tug-reply"

[policy]
idle_seconds = 600

[toggles]
notify = true
replies = true
approve = false
wake = false

[wake]
adapters = ["claude", "zcode"]
dry_run = false
timeout_seconds = 90
max_concurrent = 2
claude_command = [
  "/opt/claude",
  "--resume",
  "{session}",
  "-p",
  "{body}",
]
"""

OLD_CONFIG = """\
[server]
url = "http://127.0.0.1:2599"
token_file = "none"

[topics]
main = "tug"
warn = "tug-warn"
reply = "tug-reply"

[policy]
idle_seconds = 600

[toggles]
notify = true
replies = true
approve = false
"""


# -- defaults --


def test_wake_defaults_on_fresh_config(home):
    conf = cfg.load_config()
    assert conf.toggles.wake is True
    wake = conf.wake
    assert wake.adapters == ["claude"]
    assert wake.dry_run is True
    assert wake.timeout_seconds == 1800.0
    assert wake.max_concurrent == 1
    assert wake.claude_command == [
        "/Users/shahil/.local/bin/claude",
        "--resume",
        "{session}",
        "-p",
        "{body}",
    ]


def test_config_without_wake_section_uses_defaults(home, tmp_path):
    # the live machine's config predates the feature: it must load
    write_config(tmp_path / "config.toml", OLD_CONFIG)
    conf = cfg.load_config(tmp_path / "config.toml")
    assert conf.wake.dry_run is True
    assert conf.wake.adapters == ["claude"]
    assert conf.toggles.wake is True


def test_default_template_documents_the_wake_section(home):
    conf = cfg.load_config()
    text = (home / ".tugline" / "config.toml").read_text()
    assert "[wake]" in text
    assert "dry_run" in text
    assert "{session}" in text


# -- parsing and validation --


def test_wake_section_values_parse(home, tmp_path):
    write_config(tmp_path / "config.toml", WAKE_SECTION)
    conf = cfg.load_config(tmp_path / "config.toml")
    assert conf.toggles.wake is False
    assert conf.wake.adapters == ["claude", "zcode"]
    assert conf.wake.dry_run is False
    assert conf.wake.timeout_seconds == 90.0
    assert conf.wake.max_concurrent == 2
    assert conf.wake.claude_command == [
        "/opt/claude", "--resume", "{session}", "-p", "{body}",
    ]


def test_wake_unknown_key_is_rejected(home, tmp_path):
    text = WAKE_SECTION.replace("dry_run = false", "dry_runn = false")
    write_config(tmp_path / "config.toml", text)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(tmp_path / "config.toml")


def test_claude_command_needs_both_placeholders(home, tmp_path):
    text = WAKE_SECTION.replace('"{body}",', '"body",')
    write_config(tmp_path / "config.toml", text)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(tmp_path / "config.toml")


def test_claude_command_must_be_a_list_of_strings(home, tmp_path):
    text = WAKE_SECTION.replace("dry_run = false", 'claude_command = 7')
    # drop the original multi-line array
    text = text.split("claude_command = [", 1)[0]
    write_config(tmp_path / "config.toml", text)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(tmp_path / "config.toml")


def test_adapters_must_be_strings(home, tmp_path):
    text = WAKE_SECTION.replace(
        'adapters = ["claude", "zcode"]', 'adapters = [1, 2]'
    )
    write_config(tmp_path / "config.toml", text)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(tmp_path / "config.toml")


def test_max_concurrent_must_be_positive(home, tmp_path):
    text = WAKE_SECTION.replace("max_concurrent = 2", "max_concurrent = 0")
    write_config(tmp_path / "config.toml", text)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(tmp_path / "config.toml")


def test_timeout_must_be_positive(home, tmp_path):
    text = WAKE_SECTION.replace("timeout_seconds = 90", "timeout_seconds = 0")
    write_config(tmp_path / "config.toml", text)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(tmp_path / "config.toml")


def test_wake_section_must_be_a_table(home, tmp_path):
    text = OLD_CONFIG + '\n[wake]\nbroken line without equals\n'
    write_config(tmp_path / "config.toml", text)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(tmp_path / "config.toml")


# -- the 3.10 mini parser reads string arrays --


def test_mini_parser_reads_wake_string_arrays():
    parsed = cfg._mini_toml_loads(WAKE_SECTION)
    assert parsed["wake"]["adapters"] == ["claude", "zcode"]
    assert parsed["wake"]["claude_command"] == [
        "/opt/claude", "--resume", "{session}", "-p", "{body}",
    ]
    assert parsed["wake"]["dry_run"] is False
    assert parsed["toggles"]["wake"] is False


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="tomllib only ships in the stdlib from 3.11"
)
def test_mini_parser_matches_tomllib_on_wake_section():
    import tomllib

    assert cfg._mini_toml_loads(WAKE_SECTION) == tomllib.loads(WAKE_SECTION)


def test_mini_parser_rejects_a_broken_array():
    with pytest.raises(cfg.ConfigError):
        cfg._mini_toml_loads('[wake]\nclaude_command = ["unterminated]\n')


def test_mini_parser_array_of_non_strings_parses_but_loader_rejects(tmp_path):
    # the parser stays value-neutral (tomllib parses [1, 2] too); the
    # loud rejection belongs to the loader's type check
    parsed = cfg._mini_toml_loads("[wake]\nadapters = [1, 2]\n")
    assert parsed["wake"]["adapters"] == [1, 2]
    text = WAKE_SECTION.replace(
        'adapters = ["claude", "zcode"]', "adapters = [1, 2]"
    )
    write_config(tmp_path / "config.toml", text)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(tmp_path / "config.toml")


def test_mini_parser_array_allows_comments_after_entries():
    parsed = cfg._mini_toml_loads(
        '[wake]\nadapters = ["claude"]  # the only driver today\n'
    )
    assert parsed["wake"]["adapters"] == ["claude"]
