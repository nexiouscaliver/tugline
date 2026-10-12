"""Command line entry point for tugline (the ``tug`` command)."""

import argparse
import sys

from tugline import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tug",
        description="Push notifications and phone replies for coding-agent sessions, over ntfy.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"tugline {__version__}",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["hello"],
        help="command to run (currently only: hello)",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return 2

    if args.command == "hello":
        print("hello from tugline")
        return 0

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
