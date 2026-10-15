"""Installer for the tugline hook entries in the Claude Code settings.

Claude Code keeps hook wiring in its settings JSON (by default
~/.claude/settings.json) under "hooks", keyed by event name. Each
event maps to a list of matcher groups and each group holds a "hooks"
list of {type: command, command: ...} entries.

The operator already routes every event to a status tool, so this
installer only APPENDS one tugline group per wired event. It never
rewrites, reorders, or drops anything it does not own, and it never
touches anything outside the "hooks" key (permissions, model, and
the rest pass through untouched). Ownership is the literal marker
token tugline:claude inside the command string; the uninstaller
removes exactly the entries carrying it.

The write path is: parse the file, append our groups, back up the
previous bytes under a timestamped sibling, then swap the new file in
with one atomic rename. Install is idempotent per event: a second run
finds its own markers and appends only events still missing (so an
upgrade that wires a new event composes with an existing install).

The dump format is fixed (indent 2, UTF-8, one trailing newline), so
a settings file already in that format survives an install plus
uninstall round trip byte for byte.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime

MARKER = "tugline:claude"
SETTINGS_DEFAULT = os.path.join("~", ".claude", "settings.json")
BACKUP_INFIX = ".tugline-"

# Wired events and their hook timeouts in seconds. PermissionRequest
# waits on a phone decision bounded by the approve hard cap (300 s)
# plus margin; everything else answers in well under a second.
# UserPromptSubmit (W7-L17) delivers unread operator replies into the
# running session on every prompt.
HOOK_EVENTS = {
    "SessionStart": 20,
    "UserPromptSubmit": 20,
    "Stop": 20,
    "Notification": 20,
    "PermissionRequest": 320,
}

RESTART_WARNING = (
    "warning: running Claude Code sessions keep their old hook set; "
    "restart them to pick up tugline"
)


class SettingsError(Exception):
    """The settings file is not a shape the installer can safely edit."""


def resolve_path(settings_path=None) -> str:
    return os.path.expanduser(str(settings_path or SETTINGS_DEFAULT))


def hook_command(event, command="tug") -> str:
    return "%s hook-claude %s # %s" % (command, event, MARKER)


def absolute_command(command="tug", which=shutil.which) -> str:
    """Resolve a bare command name to an absolute path for the hook
    entries.

    The daemon (launchd) and the sessions it wakes run on
    PATH=/usr/bin:/bin:/usr/sbin:/sbin, where a bare "tug" cannot
    resolve, so a hook written as a bare name silently fails in
    exactly the sessions tugline exists to serve. Resolution tries
    the installer's own PATH, then the directory of the running
    interpreter (a venv install puts tug next to its python), then
    ~/.local/bin (the documented install location). A name nothing
    finds stays bare, degrading to the old behavior rather than
    breaking the install; a name that already carries a path
    separator is an explicit choice and passes through.
    """
    name = str(command)
    if os.path.sep in name:
        return name
    found = which(name)
    if not found:
        sibling = os.path.join(
            os.path.dirname(os.path.abspath(sys.executable)), name
        )
        if os.path.isfile(sibling) and os.access(sibling, os.X_OK):
            found = sibling
    if not found:
        local = os.path.join(
            os.path.expanduser("~"), ".local", "bin", name
        )
        if os.path.isfile(local) and os.access(local, os.X_OK):
            found = local
    return found or name


def is_marked(hook) -> bool:
    """True when a single hook entry belongs to tugline."""
    if not isinstance(hook, dict):
        return False
    return hook.get("type") == "command" and MARKER in str(hook.get("command") or "")


def group_has_marker(group) -> bool:
    """True when any hook entry inside a matcher group is tugline's."""
    if not isinstance(group, dict):
        return False
    inner = group.get("hooks")
    if not isinstance(inner, list):
        return False
    return any(is_marked(hook) for hook in inner)


def marked_events(data) -> set:
    """Event names that already carry tugline entries."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return set()
    return {
        event
        for event, groups in hooks.items()
        if isinstance(groups, list) and any(group_has_marker(g) for g in groups)
    }


def load_settings(path):
    """Read and parse the settings file. Returns (original text, data)."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise SettingsError("cannot read %s: %s" % (path, exc)) from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SettingsError("settings is not valid JSON: %s" % (path,)) from None
    if not isinstance(data, dict):
        raise SettingsError("settings must be a JSON object: %s" % (path,))
    return text, data


def dump_settings(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_backup(path, text, now=None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    backup = "%s%s%s" % (path, BACKUP_INFIX, stamp)
    with open(backup, "w", encoding="utf-8") as fh:
        fh.write(text)
    return backup


def write_settings(path, data) -> None:
    """Write the settings file atomically (temp file, one rename)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tugline-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(dump_settings(data))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _upgrade_bare_commands(data) -> int:
    """Rewrite bare tug executables in tugline's own hook entries to
    absolute paths; returns how many entries changed.

    Installs from before the absolute-command fix carry bare
    "tug hook-claude <Event>" commands, which silently fail under the
    daemon's launchd PATH. Entries whose executable already carries a
    path (an explicit install command or a prior upgrade) pass through
    untouched, as does anything the resolver cannot find.
    """
    count = 0
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                continue
            for hook in inner:
                if not (isinstance(hook, dict) and is_marked(hook)):
                    continue
                command = str(hook.get("command") or "")
                parts = command.split(None, 1)
                if len(parts) < 2 or os.path.sep in parts[0]:
                    continue
                resolved = absolute_command(parts[0])
                if resolved == parts[0]:
                    continue
                hook["command"] = "%s %s" % (resolved, parts[1])
                count += 1
    return count


def install(settings_path=None, command="tug", now=None, warn_stream=None) -> dict:
    """Append tugline hook entries to the Claude Code settings.

    Idempotent per event (append-only installer discipline): events
    that already carry a tugline entry are left alone, missing ones
    are appended, and bare tug commands in existing entries are
    upgraded in place to the absolute tug path. Nothing is written
    when every event is already wired and no bare command remains.
    Warns once that already-running sessions keep their old hooks.
    """
    path = resolve_path(settings_path)
    existed = os.path.exists(path)
    if existed:
        text, data = load_settings(path)
    else:
        text, data = None, {}

    hooks = data.get("hooks")
    if hooks is None:
        hooks = {}
        data["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise SettingsError('settings "hooks" must be an object: %s' % (path,))

    already = marked_events(data)
    upgraded = _upgrade_bare_commands(data)
    needed = [event for event in HOOK_EVENTS if event not in already]
    if not needed and not upgraded:
        return {
            "status": "already-installed",
            "changed": False,
            "path": path,
            "backup": None,
            "events": sorted(already),
        }

    embed = absolute_command(command)
    for event in needed:
        timeout = HOOK_EVENTS[event]
        groups = hooks.get(event)
        if groups is None:
            groups = []
            hooks[event] = groups
        if not isinstance(groups, list):
            raise SettingsError(
                'settings hooks.%s must be a list: %s' % (event, path)
            )
        groups.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": hook_command(event, embed),
                        "timeout": timeout,
                    }
                ]
            }
        )

    backup = write_backup(path, text, now) if existed else None
    write_settings(path, data)
    _warn_restart(warn_stream)
    return {
        "status": "installed",
        "changed": True,
        "path": path,
        "backup": backup,
        "upgraded": upgraded,
        "events": sorted(set(already) | set(needed)),
    }


def _warn_restart(stream) -> None:
    if stream is None:
        stream = sys.stderr
    try:
        print(RESTART_WARNING, file=stream)
    except OSError:
        pass
