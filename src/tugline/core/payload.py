"""Hook payload normalization.

Hooks emit the same field under two spellings depending on the caller:
camelCase (hookEventName) and snake_case (hook_event_name). This module
maps both onto one canonical snake_case dict so the rest of the code
only ever sees one spelling.

Rules, kept deliberately small:

- Known fields (below) come out under their snake_case name.
- If both spellings are present, snake_case wins. That order is fixed so
  callers can rely on one deterministic result.
- Unknown keys are copied through untouched, so nothing observed in a
  live payload is ever dropped.
- The input dict is never modified.

Pure functions only, no IO.
"""

from collections.abc import Mapping

# canonical name -> (snake_case spelling, camelCase spelling)
_FIELDS = {
    "hook_event_name": ("hook_event_name", "hookEventName"),
    "session_id": ("session_id", "sessionId"),
    "transcript_path": ("transcript_path", "transcriptPath"),
    "tool_name": ("tool_name", "toolName"),
    "tool_input": ("tool_input", "toolInput"),
    "response_preview": ("response_preview", "responsePreview"),
    "stop_hook_active": ("stop_hook_active", "stopHookActive"),
    "last_assistant_message": (
        "last_assistant_message",
        "lastAssistantMessage",
    ),
}

_KNOWN_SPELLINGS = {
    spelling for pair in _FIELDS.values() for spelling in pair
}


def normalize(raw):
    """Return a canonical dict for a hook payload.

    Raises TypeError when raw is not a mapping.
    """
    if not isinstance(raw, Mapping):
        raise TypeError("payload must be a mapping, got %s" % type(raw).__name__)

    out = {}
    for canonical, (snake, camel) in _FIELDS.items():
        if snake in raw:
            out[canonical] = raw[snake]
        elif camel in raw:
            out[canonical] = raw[camel]

    for key, value in raw.items():
        if key not in _KNOWN_SPELLINGS:
            out[key] = value
    return out


def event_name(normalized):
    """Return the hook event name (for example "Stop") or None."""
    return normalized.get("hook_event_name")


# Notification context enrichment (W6-L12). Every adapter notification
# must answer, at a glance: which harness, which project, finished or
# waiting, and which session. The helpers below are pure string
# formatting; everything they need already sits in the hook payload.

def short_session_id(session_id) -> str:
    """The last 6 characters of the session id (whole id when shorter)."""
    text = str(session_id or "")
    return text[-6:]


def project_label(cwd) -> str:
    """The last path component of the session cwd, or "unknown".

    The component is used as-is (no case folding, no full paths), so
    the label on the phone matches the directory the operator knows.
    """
    text = str(cwd or "").strip()
    if not text:
        return "unknown"
    label = text.rstrip("/").rsplit("/", 1)[-1].strip()
    return label or "unknown"


def context_title(harness, cwd, summary) -> str:
    """"[zcode] mission-control - waiting for your input"."""
    return "[%s] %s - %s" % (harness, project_label(cwd), summary)


def context_body(session_id, detail="") -> str:
    """Body first line "session <short6>", then a comma separated detail."""
    line = "session %s" % short_session_id(session_id)
    detail = " ".join(str(detail or "").split())
    return "%s, %s" % (line, detail) if detail else line


def one_line_preview(normalized, limit=80) -> str:
    """Single-line turn preview from the payload, truncated to limit.

    Prefers last_assistant_message, then response_preview. Newlines and
    runs of whitespace collapse to single spaces; empty when neither
    field carries anything.
    """
    text = normalized.get("last_assistant_message")
    if not text:
        text = normalized.get("response_preview")
    return " ".join(str(text or "").split())[:limit]
