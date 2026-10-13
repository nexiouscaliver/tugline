"""Uninstaller for the tugline hook entries in the Claude Code settings.

The mirror of install: walk the hooks object, drop the command hooks
whose command string carries the tugline:claude marker, keep every
foreign hook, group, key, and their order exactly as found. Groups
that only existed to hold tugline hooks disappear; event lists that
become empty are dropped, and a "hooks" object that becomes empty is
removed so a file that never had hooks comes back byte for byte.

A file with nothing to remove is left untouched: no backup, no
write, not even an open for writing.
"""

from __future__ import annotations

import os

from tugline.adapters.claude.install import (
    SettingsError,
    group_has_marker,
    is_marked,
    load_settings,
    resolve_path,
    write_backup,
    write_settings,
)


def uninstall(settings_path=None, now=None) -> dict:
    """Remove exactly the tugline marked hook entries. Never foreign ones."""
    path = resolve_path(settings_path)
    if not os.path.exists(path):
        return {"status": "missing", "changed": False, "removed": 0, "path": path}

    text, data = load_settings(path)
    hooks = data.get("hooks")
    removed = 0

    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            kept_groups = []
            removed_here = 0
            for group in groups:
                if isinstance(group, dict) and group_has_marker(group):
                    inner = group.get("hooks")
                    if isinstance(inner, list):
                        kept_hooks = [h for h in inner if not is_marked(h)]
                        removed_here += len(inner) - len(kept_hooks)
                        if kept_hooks:
                            group["hooks"] = kept_hooks
                            kept_groups.append(group)
                        continue
                kept_groups.append(group)
            if not removed_here:
                continue
            removed += removed_here
            if kept_groups:
                hooks[event] = kept_groups
            else:
                del hooks[event]
        if not hooks:
            data.pop("hooks", None)

    if not removed:
        return {"status": "clean", "changed": False, "removed": 0, "path": path}

    backup = write_backup(path, text, now)
    write_settings(path, data)
    return {
        "status": "uninstalled",
        "changed": True,
        "removed": removed,
        "path": path,
        "backup": backup,
    }
