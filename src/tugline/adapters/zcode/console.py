"""Console surfaces for the zcode adapter.

Three commands dispatch here before argparse sees argv, so the core
command set and its dispatch are untouched:

    tug hook-zcode <event>        read one JSON payload on stdin
    tug install zcode [--config PATH]
    tug uninstall zcode [--config PATH]

shim() raises SystemExit with the surface's exit code when argv belongs
to this adapter and returns None otherwise (the caller then proceeds
with the normal parser path). The hook surface always exits 0; the
installer surfaces use 2 for usage and refusal errors because they are
run by a human, not by the harness.
"""

from __future__ import annotations

import os
import sys

HOOK_COMMAND = "hook-zcode"
HOME_ENV = "TUGLINE_ZCODE_HOME"

USAGE = (
    "usage: tug hook-zcode <event>\n"
    "       tug install zcode [--config PATH]\n"
    "       tug uninstall zcode [--config PATH]"
)


def _home():
    return os.environ.get(HOME_ENV) or None


def shim(argv=None) -> None:
    tokens = list(sys.argv[1:] if argv is None else argv)
    if not tokens:
        return
    if tokens[0] == HOOK_COMMAND:
        raise SystemExit(_hook(tokens[1:]))
    if (
        len(tokens) >= 2
        and tokens[0] in ("install", "uninstall")
        and tokens[1] == "zcode"
    ):
        raise SystemExit(_installer(tokens[0], tokens[2:]))
    return None


def _hook(args) -> int:
    from tugline.adapters.zcode import hook

    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    return hook.run(args[0], hook.read_stdin(), home=_home())


def _installer(kind, args) -> int:
    from tugline.adapters.zcode import install, uninstall

    config = os.path.expanduser(install.DEFAULT_CONFIG)
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        if flag == "--config" and rest:
            config = os.path.expanduser(rest.pop(0))
        else:
            print(USAGE, file=sys.stderr)
            return 2
    if kind == "install":
        return install.install(config, home=_home())
    return uninstall.uninstall(config, home=_home())
