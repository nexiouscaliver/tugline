"""UserPromptSubmit reply injection tests for the Claude Code adapter.

The live settings already route UserPromptSubmit, but only to the
status tool: the tugline handler had no branch for the event, so
replies never reached a running session on this harness either. These
tests pin the fix: the handler reads unconsumed replies (journal plus
store session binding), emits one JSON object on stdout in Claude
Code's documented shape (hookSpecificOutput.additionalContext), marks
what it injected consumed in the journal, and never spools a record
for the prompt (unchanged adapter behavior). Failures degrade
silently: exit 0, no stdout.
"""

import json

from tugline.adapters.claude import hook as hook_module
from tugline.core.store import Store


def journal_path(tug_home):
    return tug_home / "replies.jsonl"


def write_journal(tug_home, entries):
    lines = [json.dumps(e, separators=(",", ":")) for e in entries]
    journal_path(tug_home).write_text("\n".join(lines) + "\n", encoding="utf-8")


def journal(tug_home):
    text = journal_path(tug_home).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def bind_reply(tug_home, body, session_id, entry_id="c1"):
    store = Store(str(tug_home / "store" / "tugline.db"))
    try:
        row = store.add_reply(body, session_id=session_id)
    finally:
        store.close()
    return {
        "id": entry_id,
        "ts": row["ts"],
        "topic": "tug-reply",
        "title": "console reply",
        "body": body,
        "consumed": False,
        "source": "console",
    }


def run_prompt(tug_home, hook_payload, stdin=None):
    import io

    payload = hook_payload("UserPromptSubmit", prompt="continue")
    if stdin is None:
        stdin = io.StringIO(json.dumps(payload))
    return hook_module.run_event("UserPromptSubmit", stdin)


def stdout_payload(captured):
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])


def test_prompt_injects_an_unread_reply(
    tug_home, hook_payload, spool_records, capsys
):
    write_journal(tug_home, [bind_reply(tug_home, "Approve", "sess-4f2a")])

    rc = run_prompt(tug_home, hook_payload)

    assert rc == 0
    payload = stdout_payload(capsys.readouterr())
    block = payload["hookSpecificOutput"]
    assert block["hookEventName"] == "UserPromptSubmit"
    assert "Approve" in block["additionalContext"]
    assert journal(tug_home)[0]["consumed"] is True
    assert spool_records() == []  # the claude adapter never spooled prompts


def test_prompt_without_replies_prints_nothing(tug_home, hook_payload, capsys):
    rc = run_prompt(tug_home, hook_payload)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_reply_read_failure_degrades_silently(
    tug_home, hook_payload, capsys, tmp_path
):
    blocker = tmp_path / "blocked.jsonl"
    blocker.mkdir()
    tug_home.mkdir(parents=True, exist_ok=True)
    journal_path(tug_home).symlink_to(blocker)

    rc = run_prompt(tug_home, hook_payload)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_prompt_prefers_replies_bound_to_this_session(
    tug_home, hook_payload, capsys
):
    mine = bind_reply(tug_home, "run the tests", "sess-4f2a", entry_id="mine")
    other = bind_reply(tug_home, "other session", "sess-zzz", entry_id="other")
    write_journal(tug_home, [other, mine])

    rc = run_prompt(tug_home, hook_payload)

    assert rc == 0
    context = stdout_payload(capsys.readouterr())["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "run the tests" in context
    assert "other session" not in context
    by_id = {e["id"]: e["consumed"] for e in journal(tug_home)}
    assert by_id == {"mine": True, "other": False}
