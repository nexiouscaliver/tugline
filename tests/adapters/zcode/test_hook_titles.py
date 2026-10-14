"""Context-enriched notification titles and bodies for the zcode hook.

Operator complaint (2026-10-13): phone notifications carry no harness,
project, session, or finished-vs-waiting clarity. The enriched shapes:

- title: "[zcode] <project> - <plain summary>"
- body:  "session <last 6 of the session id>" plus a comma separated
  detail when one exists ("permission: Bash", an 80 character turn
  preview, "prompt: ...").

With [toggles] context_titles = false every shape below must be
byte-identical to the strings the hook emitted before the enrichment,
except the session fragment: since the W9 naming parity wave it
derives from the shared display rule (the last 6 characters, the
population-blind fallback of the console's collision-lengthened
suffix), never the pre-enrichment first 8, so a hook body always
carries the token the publish-time naming pass knows how to rewrite.
"""

import json

import pytest

from tugline.adapters.zcode import hook
from tugline.core import spool

CONFIG_DEFAULTS = """
[server]
url = "http://127.0.0.1:2586"
token_file = "~/.tugline/credentials"

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

CONFIG_CONTEXT_OFF = CONFIG_DEFAULTS + "context_titles = false\n"

CWD = "/Users/shahil/work/mission-control"
SID = "abc123def456"
SHORT6 = "def456"


@pytest.fixture
def home(tmp_path):
    return tmp_path / "tugline-home"


def run_hook(event, payload_obj, *, config_text, home, env=None):
    config = home.parent / "config.toml"
    config.write_text(config_text, encoding="utf-8")
    return hook.run(
        event,
        json.dumps(payload_obj),
        config_path=str(config),
        home=str(home),
        env=env if env is not None else {},
    )


def spool_records(home):
    return list(spool.records(str(home / "spool")))


def base_event(event, **extra):
    payload = {
        "hookEventName": event,
        "sessionId": SID,
        "cwd": CWD,
    }
    payload.update(extra)
    return payload


# Enriched shapes, default config (context_titles defaults to true)


def test_permission_request_title_and_body_carry_context(home):
    code = run_hook(
        "PermissionRequest",
        base_event("PermissionRequest", toolName="Bash"),
        config_text=CONFIG_DEFAULTS,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "[zcode] mission-control - waiting for your input"
    assert rec["body"] == "session %s, permission: Bash" % SHORT6
    assert rec["topic"] == "warn"
    assert rec["priority"] == "high"


def test_stop_title_and_body_carry_the_turn_preview(home):
    code = run_hook(
        "Stop",
        base_event(
            "Stop",
            lastAssistantMessage="Fixed the flush bug\nand added tests\n",
        ),
        config_text=CONFIG_DEFAULTS,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "[zcode] mission-control - turn finished"
    assert rec["body"] == "session %s, Fixed the flush bug and added tests" % SHORT6


def test_stop_preview_falls_back_to_response_preview(home):
    code = run_hook(
        "Stop",
        base_event("Stop", responsePreview="done via preview"),
        config_text=CONFIG_DEFAULTS,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["body"] == "session %s, done via preview" % SHORT6


def test_stop_preview_is_truncated_to_80_characters(home):
    code = run_hook(
        "Stop",
        base_event("Stop", lastAssistantMessage="x" * 100),
        config_text=CONFIG_DEFAULTS,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["body"] == "session %s, %s" % (SHORT6, "x" * 80)


def test_stop_without_preview_keeps_a_plain_detail(home):
    code = run_hook(
        "Stop", base_event("Stop"), config_text=CONFIG_DEFAULTS, home=home
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "[zcode] mission-control - turn finished"
    assert rec["body"] == "session %s, turn finished" % SHORT6


def test_sessionstart_resume_and_startup_summaries(home):
    run_hook(
        "SessionStart",
        base_event("SessionStart", source="resume"),
        config_text=CONFIG_DEFAULTS,
        home=home,
    )
    rec = spool_records(home)[0]
    assert rec["title"] == "[zcode] mission-control - session resumed"
    assert rec["body"] == "session %s, resumed" % SHORT6

    other = home.parent / "tugline-home-startup"
    run_hook(
        "SessionStart",
        base_event("SessionStart", source="startup"),
        config_text=CONFIG_DEFAULTS,
        home=other,
    )
    rec = spool_records(other)[0]
    assert rec["title"] == "[zcode] mission-control - session started"
    assert rec["body"] == "session %s, started" % SHORT6


def test_userpromptsubmit_summary_and_prompt_detail(home):
    code = run_hook(
        "UserPromptSubmit",
        base_event("UserPromptSubmit", prompt="fix the bug"),
        config_text=CONFIG_DEFAULTS,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "[zcode] mission-control - prompt submitted"
    assert rec["body"] == "session %s, prompt: fix the bug" % SHORT6


def test_project_label_is_the_last_cwd_component(home):
    code = run_hook(
        "Stop",
        base_event("Stop", cwd="/srv/deep/nested/project-x"),
        config_text=CONFIG_DEFAULTS,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "[zcode] project-x - turn finished"


def test_missing_cwd_falls_back_to_unknown_project(home):
    payload = {"hookEventName": "Stop", "sessionId": SID}
    code = run_hook(
        "Stop", payload, config_text=CONFIG_DEFAULTS, home=home
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "[zcode] unknown - turn finished"


def test_unmapped_event_keeps_a_plain_summary(home):
    code = run_hook(
        "SubagentStop", base_event("SubagentStop"), config_text=CONFIG_DEFAULTS, home=home
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "[zcode] mission-control - subagentstop"
    assert rec["body"] == "session %s" % SHORT6


# Toggle off: byte-identical legacy shapes


def test_off_permission_request_is_byte_identical_legacy(home):
    code = run_hook(
        "PermissionRequest",
        base_event("PermissionRequest", toolName="Bash"),
        config_text=CONFIG_CONTEXT_OFF,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "permission request"
    assert rec["body"] == "session %s: Bash" % SHORT6


def test_off_stop_is_byte_identical_legacy(home):
    code = run_hook(
        "Stop",
        base_event("Stop", lastAssistantMessage="preview must not appear"),
        config_text=CONFIG_CONTEXT_OFF,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "session stopped"
    assert rec["body"] == "session %s stopped: stop below threshold and not armed" % SHORT6


def test_off_sessionstart_is_byte_identical_legacy(home):
    code = run_hook(
        "SessionStart",
        base_event("SessionStart", source="resume"),
        config_text=CONFIG_CONTEXT_OFF,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "session resumed"
    assert rec["body"] == "session %s resumed" % SHORT6


def test_off_userpromptsubmit_is_byte_identical_legacy(home):
    code = run_hook(
        "UserPromptSubmit",
        base_event("UserPromptSubmit", prompt="fix the bug"),
        config_text=CONFIG_CONTEXT_OFF,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "prompt submitted"
    assert rec["body"] == "fix the bug"


def test_off_unmapped_event_is_byte_identical_legacy(home):
    code = run_hook(
        "SubagentStop", base_event("SubagentStop"), config_text=CONFIG_CONTEXT_OFF, home=home
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["title"] == "subagentstop"
    assert rec["body"] == "session %s" % SHORT6


# -- fragment rule alignment (W9 naming parity) --


def test_session_fragment_is_the_suffix_rule_even_on_collision(home):
    # two sessions can share their last 6 characters; the hook cannot
    # know that (pure string work, no store access), so it prints the
    # shared suffix and the publish-time naming pass lengthens it to
    # the distinct console name
    code = run_hook(
        "Stop",
        {
            "hookEventName": "Stop",
            "sessionId": "sess-run1-actor_1_1",
            "cwd": CWD,
            "lastAssistantMessage": "done",
        },
        config_text=CONFIG_DEFAULTS,
        home=home,
    )
    assert code == 0
    rec = spool_records(home)[0]
    assert rec["body"] == "session %s, done" % "or_1_1"
