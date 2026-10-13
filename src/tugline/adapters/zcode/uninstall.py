"""Exact-match uninstaller for the zcode harness config.

Removes exactly the entries whose command carries the tugline:zcode
marker, nothing else, by splicing text at byte offsets so every other
byte survives untouched.

Containers the installer recorded creating (sidecar under
<home>/state/zcode-install.json) are taken back out when uninstalling
empties them, which is what makes install followed by uninstall restore
the original file byte for byte. Without the sidecar the uninstaller
still removes every marker entry and any event key it empties, but
leaves outer containers alone (conservative fallback).

Edit levels are exclusive: when a whole container is dropped, the
entries inside it are not edited separately, so spans never overlap.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import time

from tugline.adapters.zcode import install as _install
from tugline.adapters.zcode import jsonspans

MARKER = _install.MARKER


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _entry_has_marker(entry):
    return _install._entry_has_marker(entry)


def _load_sidecar(home):
    path = _install.sidecar_path(home)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    configs = data.get("configs") if isinstance(data, dict) else None
    return configs if isinstance(configs, dict) else {}


def _save_sidecar(home, configs):
    path = _install.sidecar_path(home)
    try:
        if not configs:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "configs": configs}, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        pass


def _marker_map(events_node):
    """event -> (marker item indexes, all_items_are_markers)."""
    result = {}
    for event, array_node in events_node.members.items():
        indexes = [
            i
            for i, item in enumerate(array_node.items)
            if _entry_has_marker(item.value)
        ]
        result[event] = (indexes, bool(indexes) and len(indexes) == len(array_node.items))
    return result


def uninstall(config_path, *, home=None, out=None):
    out = out if out is not None else sys.stdout
    config_path = os.path.abspath(os.path.expanduser(config_path))

    if not os.path.exists(config_path):
        print("nothing installed: %s does not exist" % config_path, file=out)
        return 0
    try:
        text = _read(config_path)
    except OSError as exc:
        print("cannot read %s: %s" % (config_path, exc), file=out)
        return 2
    try:
        data = json.loads(text)
    except ValueError:
        print("refusing to touch %s: not valid JSON" % config_path, file=out)
        return 2

    try:
        root = jsonspans.parse_and_scan(text)
    except (jsonspans.ScanError, ValueError):
        print(
            "refusing to touch %s: structure did not scan cleanly"
            % config_path,
            file=out,
        )
        return 2

    sidecar = _load_sidecar(home)
    config_key = os.path.abspath(config_path)
    entry = sidecar.get(config_key)
    created = set(entry.get("created", [])) if isinstance(entry, dict) else set()

    hooks_node = root.members.get("hooks")
    events_node = hooks_node.members.get("events") if hooks_node else None
    markers = _marker_map(events_node) if events_node else {}

    emptied = [event for event, (_, all_m) in markers.items() if all_m]
    partial = [
        event for event, (indexes, all_m) in markers.items() if indexes and not all_m
    ]
    removed_entries = sum(len(indexes) for indexes, _ in markers.values())

    drop_events = (
        "hooks.events" in created
        and events_node is not None
        and events_node.members
        and set(events_node.members) == set(emptied)
    )
    remaining_members = (
        set(hooks_node.members) - {"events"} if hooks_node else set()
    )
    drop_hooks = (
        "hooks" in created
        and hooks_node is not None
        and (drop_events or not events_node)
        and remaining_members <= {"enabled"}
        and (
            "enabled" not in hooks_node.members
            or hooks_node.members["enabled"].value is True
        )
    )

    edits = []
    if drop_hooks:
        edits.append(jsonspans.remove_member_edit(text, root, "hooks"))
    elif drop_events:
        edits.append(jsonspans.remove_member_edit(text, hooks_node, "events"))
    else:
        for event in partial:
            indexes = markers[event][0]
            edits.extend(
                jsonspans.remove_items_edit(
                    text, events_node.members[event], indexes
                )
            )
        if emptied:
            edits.extend(
                jsonspans.remove_members_edit(text, events_node, emptied)
            )

    if not edits:
        print("nothing to remove in %s" % config_path, file=out)
        if config_key in sidecar:
            del sidecar[config_key]
            _save_sidecar(home, sidecar)
        return 0

    new_text = jsonspans.apply_edits(text, edits)

    expected = copy.deepcopy(data)
    if drop_hooks:
        expected.pop("hooks", None)
    elif isinstance(expected.get("hooks"), dict):
        hooks_expected = expected["hooks"]
        events_expected = hooks_expected.get("events")
        if drop_events and isinstance(events_expected, dict):
            hooks_expected.pop("events", None)
        elif isinstance(events_expected, dict):
            for event in emptied:
                events_expected.pop(event, None)
            for event in partial:
                events_expected[event] = [
                    e
                    for e in events_expected[event]
                    if not _entry_has_marker(e)
                ]

    try:
        if json.loads(new_text) != expected:
            raise ValueError("semantic mismatch")
    except ValueError:
        print(
            "refusing to touch %s: edit did not verify cleanly"
            % config_path,
            file=out,
        )
        return 2

    backup = "%s%s%s" % (
        config_path,
        _install.BACKUP_GLOB_SUFFIX,
        time.strftime("%Y%m%d-%H%M%S"),
    )
    try:
        shutil.copyfile(config_path, backup)
    except OSError as exc:
        print("cannot write backup, aborting: %s" % exc, file=out)
        return 2

    try:
        tmp = config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, config_path)
    except OSError as exc:
        print("cannot write %s: %s" % (config_path, exc), file=out)
        return 2

    if config_key in sidecar:
        del sidecar[config_key]
        _save_sidecar(home, sidecar)

    print(
        "removed %d hook entries from %s" % (removed_entries, config_path),
        file=out,
    )
    print("backup written to %s" % backup, file=out)
    return 0
