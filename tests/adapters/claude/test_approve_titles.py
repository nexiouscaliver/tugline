"""Context-enriched title and body for the claude approve record.

The flagged record the approve module publishes when the approve
toggle is on carries Approve and Deny buttons, so it is the record the
operator acts on from the phone; it must answer the same at-a-glance
questions the hook records answer: which harness, which project, which
session, waiting. The enriched shapes match the hook format exactly:

- title: "[claude] <project> - waiting for your input"
- body:  "session <last 6 of the session id>, permission: <tool>
  <summarized tool input>"

The existing tool-input summary survives after the prefix and the
payload's cwd (or project_dir) names the project; a missing cwd
degrades to "unknown" exactly as the hook does. With
[toggles] context_titles = false the record must be byte-identical to
the strings the module emitted before the enrichment.
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

SID = "sess-4f2a"
SHORT6 = "s-4f2a"
CWD = "/Users/shahil/work/tugline"


def make_config(approve=True, context_titles=True):
    return Config(
        server=ServerConfig(url="http://127.0.0.1:2586", token_file="/dev/null"),
        topics=TopicsConfig(main="tug", warn="tug-warn", reply="tug-reply"),
        policy=PolicyConfig(idle_seconds=600),
        toggles=TogglesConfig(
            notify=True, replies=True, approve=approve, context_titles=context_titles
        ),
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


def request(payload, config, tmp_path, timeout=0, approve_id="tit0"):
    return approve_module.handle(
        payload,
        session_id=SID,
        config=config,
        spool_root=str(tmp_path / "spool"),
        state_root=str(tmp_path / "state"),
        timeout=timeout,
        approve_id=approve_id,
        sleep=FakeClock().sleep,
        monotonic=FakeClock().monotonic,
    )


def spooled(tmp_path):
    return list(spool_module.records(str(tmp_path / "spool")))


# Enriched shapes, default config (context_titles defaults to true)


def test_approve_on_record_title_and_body_carry_context(tmp_path):
    result = request(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git push"},
            "cwd": CWD,
        },
        make_config(),
        tmp_path,
        approve_id="tit1",
    )
    assert result is None  # timeout=0: record spooled, no wait
    rec = spooled(tmp_path)[0]
    assert rec["title"] == "[claude] tugline - waiting for your input"
    assert rec["body"] == (
        'session %s, permission: Bash {"command":"git push"}' % SHORT6
    )
    assert rec["event"] == "permissionrequest"
    assert rec["topic"] == "warn"
    assert rec["priority"] == "high"
    assert rec["session_id"] == SID
    labels = [action["label"] for action in rec["actions"]]
    assert labels == ["Approve", "Deny"]


def test_project_dir_used_when_cwd_is_missing(tmp_path):
    request(
        {"tool_name": "Bash", "cwd": "", "project_dir": "/Users/shahil/work/other"},
        make_config(),
        tmp_path,
        approve_id="tit2",
    )
    rec = spooled(tmp_path)[0]
    assert rec["title"] == "[claude] other - waiting for your input"


def test_missing_cwd_falls_back_to_unknown_project(tmp_path):
    request({"tool_name": "Bash"}, make_config(), tmp_path, approve_id="tit3")
    rec = spooled(tmp_path)[0]
    assert rec["title"] == "[claude] unknown - waiting for your input"
    assert rec["body"] == "session %s, permission: Bash" % SHORT6


def test_no_config_still_enriches_like_the_hook_default(tmp_path):
    """Without a config the hook's own plain path enriches (the probe
    defaults on); publish_request must not regress to anonymous."""
    approve_module.publish_request(
        {"tool_name": "Bash", "cwd": CWD},
        session_id=SID,
        spool_root=str(tmp_path / "spool"),
    )
    rec = spooled(tmp_path)[0]
    assert rec["title"] == "[claude] tugline - waiting for your input"
    assert rec["body"] == "session %s, permission: Bash" % SHORT6


def test_long_tool_input_summary_stays_bounded(tmp_path):
    request(
        {"tool_name": "Bash", "tool_input": {"command": "x" * 5000}, "cwd": CWD},
        make_config(),
        tmp_path,
        approve_id="tit4",
    )
    rec = spooled(tmp_path)[0]
    prefix = "session %s, permission: Bash " % SHORT6
    assert rec["body"].startswith(prefix)
    assert len(rec["body"]) - len(prefix) <= 200


# Toggle off: byte-identical legacy shapes


def test_off_record_is_byte_identical_legacy(tmp_path):
    request(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git push"},
            "cwd": CWD,
        },
        make_config(context_titles=False),
        tmp_path,
        approve_id="off1",
    )
    rec = spooled(tmp_path)[0]
    assert rec["title"] == "claude permission request"
    assert rec["body"] == 'Bash {"command":"git push"}'


def test_off_record_without_tool_input_is_byte_identical_legacy(tmp_path):
    request(
        {"tool_name": "Bash", "cwd": CWD},
        make_config(context_titles=False),
        tmp_path,
        approve_id="off2",
    )
    rec = spooled(tmp_path)[0]
    assert rec["title"] == "claude permission request"
    assert rec["body"] == "Bash"


def test_off_record_keeps_its_buttons(tmp_path):
    request(
        {"tool_name": "Bash", "cwd": CWD},
        make_config(context_titles=False),
        tmp_path,
        approve_id="off3",
    )
    rec = spooled(tmp_path)[0]
    assert [action["label"] for action in rec["actions"]] == ["Approve", "Deny"]
