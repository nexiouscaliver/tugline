"""Tests for config loading (tugline.core.config)."""

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


CUSTOM = """\
# custom test config
[server]
url = "http://10.0.0.5:2586"
token_file = "~/.tugline/my-creds"

[topics]
main = "custom-main"
warn = "custom-warn"
reply = "custom-reply"

[policy]
idle_seconds = 45

[toggles]
notify = false
replies = false
approve = true
"""


def test_first_run_writes_default_template(home):
    conf = cfg.load_config()
    path = home / ".tugline" / "config.toml"
    assert path.exists()
    text = path.read_text()
    for header in ("[server]", "[topics]", "[policy]", "[toggles]"):
        assert header in text


def test_first_run_template_is_documented(home):
    conf = cfg.load_config()
    text = (home / ".tugline" / "config.toml").read_text()
    assert "#" in text
    assert len(text.splitlines()) > 10


def test_defaults(home):
    conf = cfg.load_config()
    assert conf.server.url == "http://127.0.0.1:2586"
    assert conf.server.token_file == str(home / ".tugline" / "credentials")
    assert conf.topics.main == "tug"
    assert conf.topics.warn == "tug-warn"
    assert conf.topics.reply == "tug-reply"
    assert conf.policy.idle_seconds == 600
    assert conf.toggles.notify is True
    assert conf.toggles.replies is True
    assert conf.toggles.approve is False


def test_custom_values_loaded(home):
    path = write_config(home / ".tugline" / "config.toml", CUSTOM)
    conf = cfg.load_config()
    assert conf.server.url == "http://10.0.0.5:2586"
    assert conf.server.token_file == str(home / ".tugline" / "my-creds")
    assert conf.topics.main == "custom-main"
    assert conf.policy.idle_seconds == 45
    assert conf.toggles.notify is False
    assert conf.toggles.approve is True


def test_env_override_points_at_custom_file(home, tmp_path, monkeypatch):
    path = write_config(tmp_path / "alt.toml", CUSTOM)
    monkeypatch.setenv("TUGLINE_CONFIG", str(path))
    conf = cfg.load_config()
    assert conf.server.url == "http://10.0.0.5:2586"
    assert conf.path == str(path)


def test_env_override_missing_file_gets_template(home, tmp_path, monkeypatch):
    monkeypatch.setenv("TUGLINE_CONFIG", str(tmp_path / "fresh.toml"))
    cfg.load_config()
    assert (tmp_path / "fresh.toml").exists()


def test_explicit_path_wins_over_env(home, tmp_path, monkeypatch):
    env_path = write_config(tmp_path / "env.toml", CUSTOM)
    arg_path = write_config(tmp_path / "arg.toml", CUSTOM.replace("10.0.0.5", "10.9.9.9"))
    monkeypatch.setenv("TUGLINE_CONFIG", str(env_path))
    conf = cfg.load_config(str(arg_path))
    assert conf.server.url == "http://10.9.9.9:2586"


def test_bad_url_type_fails_loud(home):
    write_config(
        home / ".tugline" / "config.toml",
        CUSTOM.replace('url = "http://10.0.0.5:2586"', "url = 123"),
    )
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config()
    assert "[server] url" in str(exc.value)


def test_non_http_url_rejected(home):
    write_config(
        home / ".tugline" / "config.toml",
        CUSTOM.replace('url = "http://10.0.0.5:2586"', 'url = "ftp://x"'),
    )
    with pytest.raises(cfg.ConfigError):
        cfg.load_config()


def test_bad_idle_seconds_rejected(home):
    write_config(
        home / ".tugline" / "config.toml",
        CUSTOM.replace("idle_seconds = 45", "idle_seconds = 0"),
    )
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config()
    assert "idle_seconds" in str(exc.value)


def test_non_bool_toggle_rejected(home):
    write_config(
        home / ".tugline" / "config.toml",
        CUSTOM.replace("approve = true", 'approve = "yes"'),
    )
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config()
    assert "[toggles] approve" in str(exc.value)


def test_missing_section_rejected(home):
    text = CUSTOM.replace("[policy]\nidle_seconds = 45\n\n", "")
    write_config(home / ".tugline" / "config.toml", text)
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config()
    assert "policy" in str(exc.value)


def test_unknown_key_rejected(home):
    write_config(
        home / ".tugline" / "config.toml",
        CUSTOM.replace('reply = "custom-reply"', 'reply = "custom-reply"\nbogus = 1'),
    )
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config()
    assert "bogus" in str(exc.value)


def test_unknown_section_rejected(home):
    write_config(home / ".tugline" / "config.toml", CUSTOM + "\n[nope]\nx = 1\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config()
    assert "nope" in str(exc.value)


def test_empty_topic_rejected(home):
    write_config(
        home / ".tugline" / "config.toml",
        CUSTOM.replace('main = "custom-main"', 'main = ""'),
    )
    with pytest.raises(cfg.ConfigError):
        cfg.load_config()


# the 3.10 fallback parser


def test_mini_parser_reads_the_default_template(home):
    conf = cfg.load_config()
    text = open(conf.path, encoding="utf-8").read()
    parsed = cfg._mini_toml_loads(text)
    assert parsed["server"]["url"] == "http://127.0.0.1:2586"
    assert parsed["topics"]["warn"] == "tug-warn"
    assert parsed["policy"]["idle_seconds"] == 600
    assert parsed["toggles"]["approve"] is False


def test_mini_parser_matches_pinned_structure_on_custom():
    pinned = {
        "server": {
            "url": "http://10.0.0.5:2586",
            "token_file": "~/.tugline/my-creds",
        },
        "topics": {
            "main": "custom-main",
            "warn": "custom-warn",
            "reply": "custom-reply",
        },
        "policy": {"idle_seconds": 45},
        "toggles": {"notify": False, "replies": False, "approve": True},
    }
    assert cfg._mini_toml_loads(CUSTOM) == pinned


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="tomllib only ships in the stdlib from 3.11"
)
def test_mini_parser_matches_tomllib_on_custom(home):
    import tomllib

    parsed_mini = cfg._mini_toml_loads(CUSTOM)
    parsed_real = tomllib.loads(CUSTOM)
    assert parsed_mini == parsed_real


def test_mini_parser_comments_and_values():
    text = '# leading comment\n[server]  # inline\nurl = "a b c" # tail\nn = 12\nf = 1.5\nflag = true\n'
    parsed = cfg._mini_toml_loads(text)
    assert parsed == {
        "server": {"url": "a b c", "n": 12, "f": 1.5, "flag": True}
    }


def test_mini_parser_rejects_garbage_line():
    with pytest.raises(cfg.ConfigError):
        cfg._mini_toml_loads('[server]\nwhat is this\n')


def test_load_config_via_mini_parser_when_tomllib_absent(home, monkeypatch):
    write_config(home / ".tugline" / "config.toml", CUSTOM)
    monkeypatch.setattr(cfg, "tomllib", None)
    conf = cfg.load_config()
    assert conf.server.url == "http://10.0.0.5:2586"
    assert conf.toggles.approve is True


# approve receiver and external button base


def test_approve_port_and_external_url_defaults(home):
    conf = cfg.load_config()
    assert conf.server.approve_port == 2587
    assert conf.server.external_url == ""


def test_approve_port_loaded_from_config(home):
    write_config(
        home / ".tugline" / "config.toml",
        CUSTOM.replace('token_file = "~/.tugline/my-creds"',
                       'token_file = "~/.tugline/my-creds"\napprove_port = 2599'),
    )
    conf = cfg.load_config()
    assert conf.server.approve_port == 2599


def test_approve_port_out_of_range_rejected(home):
    write_config(
        home / ".tugline" / "config.toml",
        CUSTOM.replace('token_file = "~/.tugline/my-creds"',
                       'token_file = "~/.tugline/my-creds"\napprove_port = 70000'),
    )
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config()
    assert "approve_port" in str(exc.value)


def test_approve_port_wrong_type_rejected(home):
    write_config(
        home / ".tugline" / "config.toml",
        CUSTOM.replace('token_file = "~/.tugline/my-creds"',
                       'token_file = "~/.tugline/my-creds"\napprove_port = "x"'),
    )
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config()
    assert "[server] approve_port" in str(exc.value)


def test_external_url_loaded_when_http(home):
    write_config(
        home / ".tugline" / "config.toml",
        CUSTOM.replace(
            'token_file = "~/.tugline/my-creds"',
            'token_file = "~/.tugline/my-creds"\nexternal_url = "https://tail.example.com"',
        ),
    )
    conf = cfg.load_config()
    assert conf.server.external_url == "https://tail.example.com"


def test_external_url_must_be_http_or_empty(home):
    write_config(
        home / ".tugline" / "config.toml",
        CUSTOM.replace(
            'token_file = "~/.tugline/my-creds"',
            'token_file = "~/.tugline/my-creds"\nexternal_url = "ftp://tail.example.com"',
        ),
    )
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config()
    assert "external_url" in str(exc.value)
