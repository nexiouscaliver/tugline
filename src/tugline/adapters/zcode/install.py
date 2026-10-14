"""Idempotent installer for the zcode harness config.

Target file: ~/.zcode/cli/config.json (any path can be passed; tests use
fixtures and never touch the live config).

What it writes, per event: one entry under hooks.events with the command

    tug hook-zcode <Event> # tugline:zcode

The "# tugline:zcode" tail rides in the command string as a shell
comment and doubles as the marker the uninstaller matches exactly.

Edit strategy: the file is spliced at exact byte offsets (see
jsonspans), so every byte that was there before is still there after.
A semantic check (json.loads comparison) must pass before anything is
written. Writes are atomic (tmp + rename) and preceded by a timestamped
backup copy of the original.

Already-installed events are detected and skipped, so running it twice
changes nothing the second time. Hooks are snapshotted at session start
by the harness, so the output warns once: running sessions must be
restarted to pick the new hooks up.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import time

from tugline.adapters.zcode import jsonspans

MARKER = "tugline:zcode"
EVENTS = ("SessionStart", "Stop", "PermissionRequest", "UserPromptSubmit")
TIMEOUTS = {
    "SessionStart": 10,
    "Stop": 10,
    "PermissionRequest": 310,
    "UserPromptSubmit": 10,
}
DEFAULT_CONFIG = os.path.join("~", ".zcode", "cli", "config.json")
BACKUP_GLOB_SUFFIX = ".tugline-bak-"

RESTART_WARNING = (
    "note: already-running sessions will not see the new hooks until "
    "they are restarted"
)


def entry_for(event):
    return {
        "hooks": [
            {
                "type": "command",
                "command": "tug hook-zcode %s # %s" % (event, MARKER),
                "timeout": TIMEOUTS[event],
                "statusMessage": "tugline",
            }
        ]
    }


def _home_root(home):
    return os.path.expanduser(home) if home else os.path.expanduser("~/.tugline")


def sidecar_path(home):
    return os.path.join(_home_root(home), "state", "zcode-install.json")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _entry_has_marker(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
        return False
    for hook in entry["hooks"]:
        if isinstance(hook, dict) and MARKER in str(hook.get("command", "")):
            return True
    return False


def _validate(data):
    if not isinstance(data, dict):
        return "config root is not an object"
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        return "hooks is not an object"
    events = hooks.get("events") if isinstance(hooks, dict) else None
    if events is not None and not isinstance(events, dict):
        return "hooks.events is not an object"
    if isinstance(events, dict):
        for name, entries in events.items():
            if not isinstance(entries, list):
                return "hooks.events.%s is not an array" % name
    return None


def _subtree_text(needed, base_indent):
    """Serialize created event entries, one compact entry per line."""
    lines = []
    for event in EVENTS:
        if event not in needed:
            continue
        entry = json.dumps(entry_for(event))
        lines.append(
            '%s"%s": [%s]' % (" " * (base_indent + 2), event, entry)
        )
    inner = ",\n".join(lines)
    return "{\n%s\n%s}" % (inner, " " * base_indent)


def install(config_path, *, home=None, out=None):
    out = out if out is not None else sys.stdout
    config_path = os.path.abspath(os.path.expanduser(config_path))
    existed = os.path.exists(config_path)

    try:
        text = _read(config_path) if existed else "{}"
    except OSError as exc:
        print("cannot read %s: %s" % (config_path, exc), file=out)
        return 2

    try:
        data = json.loads(text)
    except ValueError:
        print("refusing to touch %s: not valid JSON" % config_path, file=out)
        return 2
    problem = _validate(data)
    if problem:
        print("refusing to touch %s: %s" % (config_path, problem), file=out)
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

    hooks_node = root.members.get("hooks")
    events_node = hooks_node.members.get("events") if hooks_node else None
    existing_events = data.get("hooks", {}).get("events", {}) or {}
    needed = [e for e in EVENTS if not any(
        _entry_has_marker(x) for x in existing_events.get(e, []) or []
    )]
    already = [e for e in EVENTS if e not in needed]

    if not needed:
        for event in already:
            print("hooks for %s already installed" % event, file=out)
        print(RESTART_WARNING, file=out)
        return 0

    edits = []
    created = []
    if events_node is not None:
        missing_pairs = []
        for event in needed:
            array_node = events_node.members.get(event)
            if array_node is not None:
                edits.append(
                    jsonspans.append_item_edit(
                        text, array_node, json.dumps(entry_for(event))
                    )
                )
            else:
                missing_pairs.append(
                    (event, "[%s]" % json.dumps(entry_for(event)))
                )
                created.append("hooks.events." + event)
        if missing_pairs:
            edits.extend(
                jsonspans.insert_members_edit(text, events_node, missing_pairs)
            )
    elif hooks_node is not None:
        value = _subtree_text(needed, 2)
        edits.append(
            jsonspans.insert_member_edit(text, hooks_node, "events", value)
        )
        created.append("hooks.events")
        created.extend("hooks.events." + e for e in needed)
    else:
        value = _subtree_text(needed, 2)
        hooks_value = '{\n  "enabled": true,\n  "events": %s\n}' % value
        edits.append(
            jsonspans.insert_member_edit(text, root, "hooks", hooks_value)
        )
        created.append("hooks")
        created.append("hooks.events")
        created.extend("hooks.events." + e for e in needed)

    new_text = jsonspans.apply_edits(text, edits)

    expected = copy.deepcopy(data)
    hooks_expected = expected.setdefault("hooks", {})
    if "enabled" not in hooks_expected and "hooks" not in data:
        hooks_expected["enabled"] = True
    events_expected = hooks_expected.setdefault("events", {})
    for event in needed:
        events_expected.setdefault(event, []).append(entry_for(event))
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

    if existed:
        backup = "%s%s%s" % (
            config_path,
            BACKUP_GLOB_SUFFIX,
            time.strftime("%Y%m%d-%H%M%S"),
        )
        try:
            shutil.copyfile(config_path, backup)
        except OSError as exc:
            print("cannot write backup, aborting: %s" % exc, file=out)
            return 2
        print("backup written to %s" % backup, file=out)

    try:
        directory = os.path.dirname(config_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, config_path)
    except OSError as exc:
        print("cannot write %s: %s" % (config_path, exc), file=out)
        return 2

    _record_created(config_path, home, created)
    for event in needed:
        print("installed hooks for %s" % event, file=out)
    for event in already:
        print("hooks for %s already installed" % event, file=out)
    if isinstance(data.get("hooks", {}), dict) and data.get("hooks", {}).get(
        "enabled"
    ) is False:
        print(
            "warning: hooks are disabled in the zcode config "
            "(hooks.enabled is false); the entries will not run "
            "until hooks are enabled",
            file=out,
        )
    print(RESTART_WARNING, file=out)
    return 0


def _record_created(config_path, home, created):
    if not created:
        return
    path = sidecar_path(home)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {"version": 1, "configs": {}}
        entry = data.setdefault("configs", {}).setdefault(
            os.path.abspath(config_path), {"created": []}
        )
        entry["created"] = sorted(set(entry["created"]) | set(created))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        pass
