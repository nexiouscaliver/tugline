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
