"""Topic names and small notification decorations.

Logical names ("main", "warn", "reply") are used everywhere in the code
and mapped to the real ntfy topic strings from config here. Anything
that is not a logical name is a literal topic and passes through
untouched, so "tug notify --topic my-topic" just works.
"""

from __future__ import annotations

MAIN = "main"
WARN = "warn"
REPLY = "reply"

_TAGS = {
    "permissionrequest": ["warning"],
    "stop": ["white_check_mark"],
    "sessionstart": ["arrow_forward"],
    "notify": ["mailbox"],
}


def resolve(name, config):
    """Map a logical topic name to its configured ntfy topic."""
    topics = config.topics
    if name == MAIN:
        return topics.main
    if name == WARN:
        return topics.warn
    if name == REPLY:
        return topics.reply
    return name


def names(config):
    return {
        MAIN: config.topics.main,
        WARN: config.topics.warn,
        REPLY: config.topics.reply,
    }


def tags_for(event_name):
    """Short emoji tags per event type, empty for unknown events."""
    return list(_TAGS.get(str(event_name or "").strip().lower(), []))
