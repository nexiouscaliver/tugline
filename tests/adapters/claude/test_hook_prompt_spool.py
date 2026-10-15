"""UserPromptSubmit spooling tests for the Claude Code adapter.

The claude adapter used to run the prompt event log-only (injection
and nothing else), unlike the zcode adapter which spools it. The store
learns a session only from spool records, so a fresh claude session
stayed invisible until its first Stop: tug ask from turn 1 answered
"unknown session" (the MCQ incident), auto-label never fired for
claude, and a mid-turn claude session read done/idle so the wake
dispatcher could spawn a concurrent driver on a live session.

These tests pin the fix: the prompt event injects replies exactly as
before AND spools one state-only record in the zcode shape (same
enriched title/body, same policy semantics: the daemon's policy
suppresses it from publishing, the store consumes it for identity,
auto-label, and busy detection).
"""

import json

from tugline.adapters.claude import hook as hook_module
from tugline.core.store import Store


def run_prompt(hook_payload, prompt="fix the login bug now"):
    import io

    payload = hook_payload("UserPromptSubmit", prompt=prompt)
    return hook_module.run_event(
        "UserPromptSubmit", io.StringIO(json.dumps(payload))
    )


def write_context_config(path, context_titles):
    text = (
        "[server]\nurl = \"http://127.0.0.1:2599\"\ntoken_file = \"none\"\n"
        "\n[topics]\nmain = \"tug\"\nwarn = \"tug-warn\"\n"
        "reply = \"tug-reply\"\n\n[policy]\nidle_seconds = 600\n"
        "\n[toggles]\nnotify = true\nreplies = true\napprove = false\n"
        "context_titles = %s\n" % ("true" if context_titles else "false")
    )
    path.write_text(text, encoding="utf-8")
    return path


def test_prompt_spools_a_state_only_record(
    tug_home, hook_payload, spool_records, capsys
):
    rc = run_prompt(hook_payload)

    assert rc == 0
    assert capsys.readouterr().out == ""  # nothing injected, no stdout
    records = spool_records()
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "userpromptsubmit"
    assert record["session_id"] == "sess-4f2a"
    assert record["topic"] == "main"
    assert record["priority"] == "default"
    assert record["title"] == "[claude] tugline - prompt submitted"
    assert record["body"] == (
        "session s-4f2a, prompt: fix the login bug now"
    )
    assert record["adapter"] == "claude"
    assert record["cwd"] == "/Users/shahil/work/tugline"
    assert record["project"] == "tugline"


def test_prompt_record_truncates_long_prompts_to_60_chars(
    tug_home, hook_payload, spool_records
):
    prompt = "word " * 30  # 150 chars, well past the cap

    run_prompt(hook_payload, prompt=prompt)

    body = spool_records()[0]["body"]
    prefix = "session s-4f2a, prompt: "
    assert body.startswith(prefix)
    carried = body[len(prefix):]
    # context_body collapses whitespace (the zcode shape), so the
    # carried prompt is the capped text with runs folded
    assert carried == " ".join(prompt[:60].split())
    assert len(carried) <= 60


def test_prompt_record_without_prompt_text_still_spools(
    tug_home, hook_payload, spool_records
):
    run_prompt(hook_payload, prompt="")

    record = spool_records()[0]
    assert record["event"] == "userpromptsubmit"
    assert record["title"] == "[claude] tugline - prompt submitted"
    assert record["body"] == "session s-4f2a"


def test_context_off_spools_the_legacy_shape(
    tug_home, hook_payload, spool_records, tmp_path, monkeypatch
):
    config = write_context_config(
        tmp_path / "config.toml", context_titles=False
    )
    monkeypatch.setenv("TUGLINE_CONFIG", str(config))

    run_prompt(hook_payload, prompt="fix the login bug now")

    record = spool_records()[0]
    assert record["title"] == "prompt submitted"
    assert record["body"] == "fix the login bug now"


def test_prompt_record_lands_the_session_in_the_store_at_turn_one(
    tug_home, hook_payload
):
    """The MCQ incident regression: a fresh session must be askable
    from its first turn, so the FIRST spooled record (the prompt) must
    create the store row, auto-label it, and mark it busy."""
    run_prompt(hook_payload, prompt="fix the login bug now")

    store = Store(str(tug_home / "store" / "tugline.db"))
    try:
        store.ingest_spool(str(tug_home / "spool"))
        row = store.session("sess-4f2a")
        assert row is not None  # ask works from turn 1
        assert row["adapter"] == "claude"
        assert row["project"] == "tugline"
        assert row["state"] == "running"  # mid-turn busy detection
        assert row["last_event"] == "userpromptsubmit"
        assert row["label"] == "fix the login bug now"
        assert row["label_source"] == "auto"
        assert "claude" in row["tags"]
        assert "tugline" in row["tags"]
    finally:
        store.close()


def test_prompt_record_is_suppressed_by_policy(
    tug_home, hook_payload, spool_records
):
    """State-only semantics: the daemon's publish policy must never
    buzz the phone for a prompt submission."""
    from tugline.core.policy import SessionState, decide

    run_prompt(hook_payload)
    record = spool_records()[0]

    decision = decide(record, SessionState(session_id="sess-4f2a"))
    assert decision.action == "suppress"
    assert "state" in decision.reason
