"""Hook payload normalization.

Hooks emit the same field under two spellings depending on the caller:
camelCase (hookEventName) and snake_case (hook_event_name). This module
maps both onto one canonical snake_case dict so the rest of the code
only ever sees one spelling.
"""


def normalize(raw):
    raise NotImplementedError("payload normalization not implemented yet")
