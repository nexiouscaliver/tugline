"""Context-enriched title and body for the zcode approve record.

The urgent button record the approve module spools is the one
notification the operator acts on from the phone, so it must answer the
same at-a-glance questions the hook records answer: which harness,
which project, which session, waiting. The enriched shapes match the
hook format exactly:

- title: "[zcode] <project> - waiting for your input"
- body:  "session <last 6 of the session id>, permission: <tool>,
  approve or deny from the phone"

The phone guidance stays: it is the action instruction for the Approve
and Deny buttons. The hook call site passes only the session id today,
so a run without cwd and tool_name degrades exactly as the hook does
for a payload without those fields: project "unknown", tool
"tool use". With [toggles] context_titles = false the record must be
byte-identical to the strings the module emitted before the
enrichment, except the session fragment: since the W9 naming parity
wave it derives from the shared display rule (the last 6 characters),
never the pre-enrichment first 8.
"""

import io
import json

import pytest

from tugline.adapters.zcode import approve, hook
from tugline.core import spool

CONFIG_APPROVE_OFF = """
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

CONFIG_APPROVE_ON = CONFIG_APPROVE_OFF.replace("approve = false", "approve = true")

CONFIG_CONTEXT_OFF = CONFIG_APPROVE_ON + "context_titles = false\n"

SID = "abc123def456"
SHORT6 = "def456"
CWD = "/Users/shahil/work/mission-control"


@pytest.fixture
def config_on(tmp_path):
    from tugline.core.config import load_config

    path = tmp_path / "config-on.toml"
    path.write_text(CONFIG_APPROVE_ON, encoding="utf-8")
    return load_config(str(path))


@pytest.fixture
def approvals(tmp_path):
    root = tmp_path / "approvals"
    root.mkdir()
    return str(root)


def records_for(tmp_path):
    return list(spool.records(str(tmp_path / "spool")))


# Enriched shapes, default config (context_titles defaults to true)


def test_approve_record_title_and_body_carry_context(
    config_on, tmp_path, approvals
):
    approve.run(
        SID,
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=0,
        nonce="n-ctx",
        cwd=CWD,
        tool_name="Bash",
    )
    rec = records_for(tmp_path)[0]
    assert rec["title"] == "[zcode] mission-control - waiting for your input"
    assert rec["body"] == (
        "session %s, permission: Bash, approve or deny from the phone" % SHORT6
    )
    assert rec["event"] == "permissionrequest"
    assert rec["topic"] == "warn"
    assert rec["priority"] == "urgent"
    assert rec["session_id"] == SID


def test_project_label_is_the_last_cwd_component(config_on, tmp_path, approvals):
    approve.run(
        SID,
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=0,
        nonce="n-deep",
        cwd="/srv/deep/nested/project-x",
        tool_name="Bash",
    )
    rec = records_for(tmp_path)[0]
    assert rec["title"] == "[zcode] project-x - waiting for your input"


def test_missing_cwd_and_tool_degrade_like_the_hook(
    config_on, tmp_path, approvals
):
    approve.run(
        SID,
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=0,
        nonce="n-bare",
    )
    rec = records_for(tmp_path)[0]
    assert rec["title"] == "[zcode] unknown - waiting for your input"
    assert rec["body"] == (
        "session %s, permission: tool use, approve or deny from the phone"
        % SHORT6
    )


def test_buttons_survive_the_enrichment(config_on, tmp_path, approvals):
    approve.run(
        SID,
        config=config_on,
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=0,
        nonce="n-btn",
        cwd=CWD,
        tool_name="Bash",
    )
    rec = records_for(tmp_path)[0]
    labels = [a["label"] for a in rec["actions"]]
    assert labels == ["Approve", "Deny"]
    assert rec["actions"][0]["url"] == (
        "http://127.0.0.1:2586/approvals/n-btn?decision=allow"
    )
    assert rec["actions"][1]["url"] == (
        "http://127.0.0.1:2586/approvals/n-btn?decision=deny"
    )


def test_hook_approve_path_record_is_enriched(tmp_path, monkeypatch):
    """The hook call site passes only the session id today, so the
    record through the real hook degrades to the unknown project while
    still carrying harness, short6, and the phone guidance."""
    cfg = tmp_path / "config-on.toml"
    cfg.write_text(CONFIG_APPROVE_ON, encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setattr(approve, "new_nonce", lambda: "fxt")
    decisions = home / "state" / "approvals"
    decisions.mkdir(parents=True)
    (decisions / "fxt.json").write_text(
        json.dumps({"decision": "allow"}), encoding="utf-8"
    )
    out = io.StringIO()
    code = hook.run(
        "PermissionRequest",
        '{"hookEventName":"PermissionRequest","sessionId":"%s",'
        '"cwd":"%s","toolName":"Bash"}' % (SID, CWD),
        config_path=str(cfg),
        home=str(home),
        _stdout=out,
    )
    assert code == 0
    rec = records_for(home)[0]
    assert rec["title"] == "[zcode] unknown - waiting for your input"
    assert rec["body"] == (
        "session %s, permission: tool use, approve or deny from the phone"
        % SHORT6
    )
    assert json.loads(out.getvalue())["hookSpecificOutput"][
        "permissionDecision"
    ] == "allow"


# Toggle off: byte-identical legacy shape


def test_off_record_is_byte_identical_legacy(tmp_path, approvals):
    from tugline.core.config import load_config

    path = tmp_path / "config-off.toml"
    path.write_text(CONFIG_CONTEXT_OFF, encoding="utf-8")
    approve.run(
        SID,
        config=load_config(str(path)),
        spool_root=str(tmp_path / "spool"),
        approvals_root=approvals,
        wait=0,
        nonce="n-off",
        cwd=CWD,
        tool_name="Bash",
    )
    rec = records_for(tmp_path)[0]
    assert rec["title"] == "permission request"
    assert rec["body"] == (
        "session %s asks permission, approve or deny from the phone" % SHORT6
    )
