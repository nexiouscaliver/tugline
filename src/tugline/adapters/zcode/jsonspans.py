"""Span-mapped JSON scanning and exact-text splicing.

The installer must preserve every existing byte of the operator's
config, so instead of rewriting JSON it splices text at exact offsets.
This module scans already-valid JSON (json.loads ran first) into a tree
of nodes that remember where every value, object member, and array item
sits in the source text, and offers the few splice primitives the
installer and uninstaller need.
"""

from __future__ import annotations

import json

WS = " \t\r\n"


class ScanError(Exception):
    """The text does not match what json.loads accepted. Never written."""


class Node:
    __slots__ = ("value", "start", "end", "members", "key_spans", "items")

    def __init__(self, value, start, end):
        self.value = value
        self.start = start
        self.end = end  # one past the last character of the value
        self.members = {}  # key -> Node, in document order
        self.key_spans = {}  # key -> (start, end) of the quoted key
        self.items = []  # Node list, in document order


def _skip_ws(text, pos):
    while pos < len(text) and text[pos] in WS:
        pos += 1
    return pos


_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    '"': '"',
    "\\": "\\",
    "/": "/",
}


def _scan_string(text, pos):
    """pos at the opening quote; returns (value, end past close quote)."""
    pos += 1
    out = []
    while pos < len(text):
        ch = text[pos]
        if ch == '"':
            return "".join(out), pos + 1
        if ch == "\\":
            pos += 1
            if pos >= len(text):
                raise ScanError("unterminated string escape")
            esc = text[pos]
            if esc == "u":
                if pos + 4 >= len(text):
                    raise ScanError("short unicode escape")
                try:
                    out.append(chr(int(text[pos + 1 : pos + 5], 16)))
                except ValueError:
                    raise ScanError("bad unicode escape") from None
                pos += 4
            else:
                out.append(_ESCAPES.get(esc, esc))
        else:
            out.append(ch)
        pos += 1
    raise ScanError("unterminated string")


def _scan_value(text, pos):
    pos = _skip_ws(text, pos)
    if pos >= len(text):
        raise ScanError("unexpected end of input")
    ch = text[pos]
    if ch == "{":
        return _scan_object(text, pos)
    if ch == "[":
        return _scan_array(text, pos)
    if ch == '"':
        start = pos
        value, pos = _scan_string(text, pos)
        return Node(value, start, pos), pos
    start = pos
    while pos < len(text) and text[pos] not in ",}] \t\r\n":
        pos += 1
    literal = text[start:pos]
    if literal == "true":
        return Node(True, start, pos), pos
    if literal == "false":
        return Node(False, start, pos), pos
    if literal == "null":
        return Node(None, start, pos), pos
    try:
        return Node(int(literal), start, pos), pos
    except ValueError:
        pass
    try:
        return Node(float(literal), start, pos), pos
    except ValueError:
        raise ScanError("cannot scan literal %r" % literal) from None


def _scan_object(text, pos):
    start = pos
    pos += 1
    node = Node({}, start, 0)
    pos = _skip_ws(text, pos)
    if pos < len(text) and text[pos] == "}":
        node.end = pos + 1
        return node, node.end
    while True:
        pos = _skip_ws(text, pos)
        if pos >= len(text) or text[pos] != '"':
            raise ScanError("expected object key")
        key_start = pos
        key, pos = _scan_string(text, pos)
        if key in node.members:
            raise ScanError("duplicate key %r" % key)
        pos = _skip_ws(text, pos)
        if pos >= len(text) or text[pos] != ":":
            raise ScanError("expected colon")
        pos += 1
        value_node, pos = _scan_value(text, pos)
        node.members[key] = value_node
        node.key_spans[key] = (key_start, _string_end(text, key_start))
        node.value[key] = value_node.value
        pos = _skip_ws(text, pos)
        if pos < len(text) and text[pos] == ",":
            pos += 1
            continue
        if pos < len(text) and text[pos] == "}":
            node.end = pos + 1
            return node, node.end
        raise ScanError("expected , or } in object")


def _string_end(text, key_start):
    """End offset (exclusive) of the quoted string starting at key_start."""
    pos = key_start + 1
    while pos < len(text):
        if text[pos] == "\\":
            pos += 2
            continue
        if text[pos] == '"':
            return pos + 1
        pos += 1
    raise ScanError("unterminated key")


def _scan_array(text, pos):
    start = pos
    pos += 1
    node = Node([], start, 0)
    pos = _skip_ws(text, pos)
    if pos < len(text) and text[pos] == "]":
        node.end = pos + 1
        return node, node.end
    while True:
        item, pos = _scan_value(text, pos)
        node.items.append(item)
        node.value.append(item.value)
        pos = _skip_ws(text, pos)
        if pos < len(text) and text[pos] == ",":
            pos += 1
            continue
        if pos < len(text) and text[pos] == "]":
            node.end = pos + 1
            return node, node.end
        raise ScanError("expected , or ] in array")


def scan(text):
    """Scan a whole JSON document (must be an object) into a Node tree."""
    node, pos = _scan_value(text, 0)
    pos = _skip_ws(text, pos)
    if pos != len(text):
        raise ScanError("trailing content after document")
    if not node.members and not isinstance(node.value, dict):
        raise ScanError("root is not an object")
    return node


def line_indent(text, offset):
    """Leading whitespace of the line that contains offset."""
    newline = text.rfind("\n", 0, offset)
    if newline < 0:
        return ""
    return text[newline + 1 : offset]


def append_item_edit(text, array_node, item_text):
    """Edit that appends one item to an array, matching its style."""
    close = array_node.end - 1
    if array_node.items:
        last = array_node.items[-1]
        indent = line_indent(text, last.start)
        return (close, close, "," + "\n" + indent + item_text)
    return (close, close, item_text)


def insert_member_edit(text, obj_node, key, value_text):
    """Edit that adds one member to an object, matching its style."""
    return insert_members_edit(text, obj_node, [(key, value_text)])[0]


def insert_members_edit(text, obj_node, pairs):
    """Edit that adds several members to an object in one splice."""
    close = obj_node.end - 1
    joined = ", ".join('"%s": %s' % (key, value) for key, value in pairs)
    if obj_node.members:
        last_key = next(reversed(obj_node.members))
        key_start = obj_node.key_spans[last_key][0]
        indent = line_indent(text, key_start)
        return [(close, close, "," + "\n" + indent + joined)]
    return [(close, close, joined)]


def remove_item_edit(text, array_node, index):
    """Edit deleting array item index, handling its comma."""
    return remove_items_edit(text, array_node, [index])[0]


def remove_items_edit(text, array_node, indexes):
    """Edits deleting several array items, grouped into runs.

    Items deleted as one contiguous run take exactly one boundary comma
    with them, so edits can never overlap.
    """
    wanted = sorted(set(indexes))
    edits = []
    i = 0
    while i < len(wanted):
        j = i
        while j + 1 < len(wanted) and wanted[j + 1] == wanted[j] + 1:
            j += 1
        first = array_node.items[wanted[i]]
        last = array_node.items[wanted[j]]
        start, end = first.start, last.end
        start, end = _widen_run(text, array_node, start, end)
        edits.append((start, end, ""))
        i = j + 1
    return edits


def remove_member_edit(text, obj_node, key):
    """Edit deleting an object member, handling its comma."""
    return remove_members_edit(text, obj_node, [key])[0]


def remove_members_edit(text, obj_node, keys):
    """Edits deleting several object members, grouped into runs."""
    order = list(obj_node.members)
    edits = []
    i = 0
    while i < len(order):
        if order[i] not in keys:
            i += 1
            continue
        j = i
        while j + 1 < len(order) and order[j + 1] in keys:
            j += 1
        first_key = order[i]
        last_key = order[j]
        start = obj_node.key_spans[first_key][0]
        end = obj_node.members[last_key].end
        start, end = _widen_run(text, obj_node, start, end)
        edits.append((start, end, ""))
        i = j + 1
    return edits


def _widen_run(text, container, start, end):
    """Extend a deletion run over one boundary comma, if there is one."""
    pos = end
    while pos < container.end and text[pos] in WS:
        pos += 1
    if pos < container.end and text[pos] == ",":
        return start, pos + 1
    pos = start
    while pos > container.start and text[pos - 1] in WS:
        pos -= 1
    if pos > container.start and text[pos - 1] == ",":
        return pos - 1, end
    return start, end


def apply_edits(text, edits):
    """Apply (start, end, replacement) edits, highest offset first."""
    result = text
    for start, end, replacement in sorted(edits, key=lambda e: -e[0]):
        result = result[:start] + replacement + result[end:]
    return result


def parse_and_scan(text):
    """Validate with json.loads, then scan. Any mismatch raises."""
    json.loads(text)
    return scan(text)
