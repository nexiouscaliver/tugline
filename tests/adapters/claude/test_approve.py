"""Approve tests: bounded wait, exact decision JSON, fail-closed timeout.

The matrix that matters: allow and deny decisions come back as the
exact JSON Claude Code expects, a timeout prints nothing (the local
permission prompt stays in charge, never an auto-allow), and the wait
never runs past its hard cap. All waiting is driven by an injected
clock, so the matrix runs instantly.
"""

import json

from tugline.adapters.claude import approve as approve_module
from tugline.core import spool as spool_module
from tugline.core.config import (
    Config,
    PolicyConfig,
    ServerConfig,
    TogglesConfig,
    TopicsConfig,
)


def make_config(approve=True, notify=True, url="http://127.0.0.1:2586"):
    return Config(
        server=ServerConfig(url=url, token_file="/dev/null"),
        topics=TopicsConfig(main="tug", warn="tug-warn", reply="tug-reply"),
        policy=PolicyConfig(idle_seconds=600),
        toggles=TogglesConfig(notify=notify, replies=True, approve=approve),
        path="memory",
    )


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def decide_file(tug_home, approve_id, content):
    path = tug_home / "state" / "approvals" / (approve_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def request(payload, config, tug_home, clock, timeout=None, approve_id="app0"):
    return approve_module.handle(
        payload,
        session_id="sess-4f2a",
        config=config,
        spool_root=str(tug_home / "spool"),
        state_root=str(tug_home / "state"),
        timeout=timeout,
        approve_id=approve_id,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


def spooled(tug_home):
    return list(spool_module.records(str(tug_home / "spool")))


# Decision matrix


def test_allow_decision_returns_the_exact_json(tug_home):
    clock = FakeClock()
    decide_file(tug_home, "app1", {"decision": "allow"})
    result = request({"tool_name": "Bash"}, make_config(), tug_home, clock, approve_id="app1")
    assert result == '{"decision":{"behavior":"allow"}}'
    assert clock.sleeps == []


def test_deny_decision_with_default_message_returns_the_exact_json(tug_home):
    clock = FakeClock()
    decide_file(tug_home, "app2", {"decision": "deny"})
    result = request({"tool_name": "Bash"}, make_config(), tug_home, clock, approve_id="app2")
    assert result == '{"decision":{"behavior":"deny","message":"denied from phone"}}'


def test_deny_decision_keeps_a_custom_message(tug_home):
    clock = FakeClock()
    decide_file(tug_home, "app3", {"decision": "deny", "message": "wrong branch"})
    result = request({"tool_name": "Bash"}, make_config(), tug_home, clock, approve_id="app3")
    assert result == '{"decision":{"behavior":"deny","message":"wrong branch"}}'


def test_timeout_returns_none_and_never_auto_allows(tug_home):
    clock = FakeClock()
    result = request(
        {"tool_name": "Bash"}, make_config(), tug_home, clock, timeout=1.0, approve_id="none1"
    )
    assert result is None
    assert clock.sleeps
    assert clock.now >= 1001.0


def test_timeout_bounds_default_to_120s_and_cap_at_300s():
    assert approve_module.effective_timeout(None) == 120.0
    assert approve_module.effective_timeout(999.0) == 300.0
    assert approve_module.effective_timeout(-3.0) == 0.0
    assert approve_module.effective_timeout(45.0) == 45.0


def test_zero_timeout_checks_once_without_sleeping(tug_home):
    clock = FakeClock()
    result = request(
        {"tool_name": "Bash"}, make_config(), tug_home, clock, timeout=0, approve_id="none2"
    )
    assert result is None
    assert clock.sleeps == []


def test_unknown_decision_value_keeps_waiting(tug_home):
    clock = FakeClock()
    decide_file(tug_home, "app4", {"decision": "maybe"})
    result = request(
        {"tool_name": "Bash"}, make_config(), tug_home, clock, timeout=1.0, approve_id="app4"
    )
    assert result is None


def test_corrupt_decision_file_keeps_waiting(tug_home):
    clock = FakeClock()
    path = tug_home / "state" / "approvals" / "app5.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json{", encoding="utf-8")
    result = request(
        {"tool_name": "Bash"}, make_config(), tug_home, clock, timeout=1.0, approve_id="app5"
    )
    assert result is None


def test_decision_arriving_mid_wait_is_returned(tug_home):
    clock = FakeClock()
    original_sleep = clock.sleep

    def sleep_and_decide(seconds):
        decide_file(tug_home, "app6", {"decision": "deny", "message": "wrong branch"})
        original_sleep(seconds)

    clock.sleep = sleep_and_decide
    result = request(
        {"tool_name": "Bash"}, make_config(), tug_home, clock, timeout=30.0, approve_id="app6"
    )
    assert result == '{"decision":{"behavior":"deny","message":"wrong branch"}}'


# The flagged spool record


def test_flagged_record_carries_approve_and_deny_buttons(tug_home):
    clock = FakeClock()
    decide_file(tug_home, "app7", {"decision": "allow"})
    request(
        {"tool_name": "Bash", "tool_input": {"command": "git push"}},
        make_config(),
        tug_home,
        clock,
        approve_id="app7",
    )
    records = spooled(tug_home)
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "permissionrequest"
    assert record["title"] == "claude permission request"
    labels = [action["label"] for action in record["actions"]]
    assert labels == ["Approve", "Deny"]
    assert all(action["method"] == "POST" for action in record["actions"])
    assert record["actions"][0]["url"].endswith("/approvals/app7?decision=allow")
    assert record["actions"][1]["url"].endswith("/approvals/app7?decision=deny")


def test_button_urls_use_the_configured_server_base(tug_home):
    clock = FakeClock()
    decide_file(tug_home, "app8", {"decision": "allow"})
    request(
        {"tool_name": "Bash"},
        make_config(url="https://ntfy.example.com"),
        tug_home,
        clock,
        approve_id="app8",
    )
    record = spooled(tug_home)[0]
    assert (
        record["actions"][0]["url"]
        == "https://ntfy.example.com/approvals/app8?decision=allow"
    )


def test_approve_toggle_off_spools_without_buttons_and_returns_none(tug_home):
    clock = FakeClock()
    result = request(
        {"tool_name": "Bash"}, make_config(approve=False), tug_home, clock, approve_id="app9"
    )
    assert result is None
    assert clock.sleeps == []
    record = spooled(tug_home)[0]
    assert "actions" not in record


def test_notify_toggle_off_skips_the_wait_but_keeps_buttons(tug_home):
    clock = FakeClock()
    result = request(
        {"tool_name": "Bash"}, make_config(notify=False), tug_home, clock, approve_id="app10"
    )
    assert result is None
    assert clock.sleeps == []
    record = spooled(tug_home)[0]
    assert record["actions"][0]["label"] == "Approve"


def test_tool_input_summary_is_compact_and_bounded(tug_home):
    clock = FakeClock()
    decide_file(tug_home, "app11", {"decision": "allow"})
    payload = {"tool_name": "Bash", "tool_input": {"command": "x" * 5000}}
    request(payload, make_config(), tug_home, clock, approve_id="app11")
    record = spooled(tug_home)[0]
    assert record["body"].startswith("Bash ")
    assert len(record["body"]) <= 205


def test_wait_stops_at_the_300s_hard_cap_in_the_loop(tug_home):
    clock = FakeClock()
    result = request(
        {"tool_name": "Bash"}, make_config(), tug_home, clock, timeout=999.0, approve_id="none3"
    )
    assert result is None
    assert clock.now <= 1000.0 + 300.0 + approve_module.POLL_INTERVAL


def test_uninstall_writes_its_own_backup_of_the_installed_state(settings_path):
    from datetime import datetime

    from tugline.adapters.claude import install as install_module
    from tugline.adapters.claude import uninstall as uninstall_module

    install_module.install(settings_path)
    installed_text = settings_path.read_text(encoding="utf-8")
    stamp = datetime(2026, 10, 13, 6, 6, 6)
    result = uninstall_module.uninstall(settings_path, now=stamp)
    assert result["backup"].endswith("settings.json.tugline-20261013T060606")
    with open(result["backup"], encoding="utf-8") as fh:
        assert fh.read() == installed_text


# button url formation with and without external_url


def make_config_ext(external_url="", url="http://127.0.0.1:2586", token_file="/dev/null"):
    from tugline.core.config import ServerConfig

    return Config(
        server=ServerConfig(
            url=url, token_file=token_file, external_url=external_url
        ),
        topics=TopicsConfig(main="tug", warn="tug-warn", reply="tug-reply"),
        policy=PolicyConfig(idle_seconds=600),
        toggles=TogglesConfig(notify=True, replies=True, approve=True),
        path="memory",
    )


def test_button_urls_use_external_url_when_set(tug_home):
    clock = FakeClock()
    decide_file(tug_home, "ext1", {"decision": "allow"})
    request(
        {"tool_name": "Bash"},
        make_config_ext(external_url="https://tail.example.com"),
        tug_home,
        clock,
        approve_id="ext1",
    )
    record = spooled(tug_home)[0]
    assert record["actions"][0]["url"] == (
        "https://tail.example.com/approvals/ext1?decision=allow"
    )
    assert record["actions"][1]["url"] == (
        "https://tail.example.com/approvals/ext1?decision=deny"
    )


def test_button_urls_keep_the_server_base_when_external_url_empty(tug_home):
    clock = FakeClock()
    decide_file(tug_home, "ext2", {"decision": "allow"})
    request(
        {"tool_name": "Bash"},
        make_config_ext(external_url=""),
        tug_home,
        clock,
        approve_id="ext2",
    )
    record = spooled(tug_home)[0]
    assert record["actions"][0]["url"] == (
        "http://127.0.0.1:2586/approvals/ext2?decision=allow"
    )


def test_buttons_carry_the_bearer_token_header(tug_home, tmp_path):
    creds = tmp_path / "creds"
    creds.write_text("tk_button_test\n", encoding="utf-8")
    clock = FakeClock()
    decide_file(tug_home, "ext3", {"decision": "allow"})
    request(
        {"tool_name": "Bash"},
        make_config_ext(token_file=str(creds)),
        tug_home,
        clock,
        approve_id="ext3",
    )
    record = spooled(tug_home)[0]
    for action in record["actions"]:
        assert action["headers"] == {"Authorization": "Bearer tk_button_test"}


def test_buttons_have_no_header_key_without_a_token(tug_home):
    clock = FakeClock()
    decide_file(tug_home, "ext4", {"decision": "allow"})
    request(
        {"tool_name": "Bash"},
        make_config_ext(token_file="/nonexistent/token-file"),
        tug_home,
        clock,
        approve_id="ext4",
    )
    record = spooled(tug_home)[0]
    for action in record["actions"]:
        assert "headers" not in action
