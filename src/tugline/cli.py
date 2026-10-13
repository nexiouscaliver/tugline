"""Command line entry point for tugline (the ``tug`` command)."""

import argparse
import json
import os
import sys

from tugline import __version__
from tugline.adapters.zcode import console as zcode_console
from tugline.core import paths, policy, spool, topics
from tugline.core.config import ConfigError, load_config
from tugline.core.daemon import Daemon
from tugline.core.publisher import Publisher


def tugline_path(*parts):
    """One path under the tugline home (TUGLINE_HOME aware)."""
    return os.path.join(paths.home_root(), *parts)


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
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("hello", help="say hello (smoke test)")

    notify = sub.add_parser("notify", help="queue a notification in the spool")
    notify.add_argument("--title", required=True, help="notification title")
    notify.add_argument("--body", required=True, help="notification text")
    notify.add_argument(
        "--topic",
        default="main",
        help="logical topic (main, warn) or a raw ntfy topic (default: main)",
    )
    notify.add_argument(
        "--priority",
        default="default",
        help="min, low, default, high, urgent (default: default)",
    )
    notify.add_argument(
        "--session",
        default=None,
        help="session id to record (default: TUGLINE_SESSION env)",
    )

    arm = sub.add_parser("arm", help="arm a session so its next Stop notifies")
    arm.add_argument("session", nargs="?", default=None, help="session id")
    disarm = sub.add_parser("disarm", help="clear the armed marker for a session")
    disarm.add_argument("session", nargs="?", default=None, help="session id")

    test = sub.add_parser("test", help="send one test message and print the HTTP code")
    test.add_argument(
        "--topic", default="main", help="logical or raw topic (default: main)"
    )

    sub.add_parser("status", help="show service state, spool depth, last reply")

    daemon = sub.add_parser("daemon", help="flush the spool and poll for replies")
    daemon.add_argument(
        "--once", action="store_true", help="run a single cycle and exit"
    )

    console = sub.add_parser(
        "console", help="serve the local web console on loopback"
    )
    console.add_argument(
        "--port",
        type=int,
        default=None,
        help="port to serve on (default: [server] console_port from config)",
    )
    console.add_argument(
        "--open",
        action="store_true",
        help="open the console in the default browser",
    )

    sub.add_parser("version", help="print the tugline version")
    from tugline.adapters.claude.hook import register_hook_command; register_hook_command(sub)  # wiring: claude adapter hook command
    return parser


def main(argv=None) -> int:
    zcode_console.shim(argv)  # zcode adapter surfaces exit here
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return 2

    handlers = {
        "hello": cmd_hello,
        "notify": cmd_notify,
        "arm": cmd_arm,
        "disarm": cmd_disarm,
        "test": cmd_test,
        "status": cmd_status,
        "daemon": cmd_daemon,
        "console": cmd_console,
        "version": cmd_version,
    }
    return handlers[args.command](args)


def cmd_hello(args) -> int:
    print("hello from tugline")
    return 0


def cmd_version(args) -> int:
    print("tugline %s" % __version__)
    return 0


def cmd_notify(args) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    if not config.toggles.notify:
        print("notify is off in config, nothing sent")
        return 0
    session = args.session or os.environ.get("TUGLINE_SESSION") or "manual"
    record = spool.append(
        tugline_path("spool"),
        event="notify",
        session_id=session,
        topic=args.topic,
        priority=args.priority,
        title=args.title,
        body=args.body,
    )
    print(record["id"])
    return 0


def _session_arg(args):
    session = args.session or os.environ.get("TUGLINE_SESSION")
    if not session:
        print("give a session id or set TUGLINE_SESSION", file=sys.stderr)
        return None
    return session


def cmd_arm(args) -> int:
    session = _session_arg(args)
    if session is None:
        return 2
    try:
        policy.arm(session)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("armed %s" % session)
    return 0


def cmd_disarm(args) -> int:
    session = _session_arg(args)
    if session is None:
        return 2
    try:
        policy.disarm(session)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("disarmed %s" % session)
    return 0


def cmd_test(args) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    publisher = Publisher.from_config(config)
    result = publisher.publish(
        topics.resolve(args.topic, config),
        "tug test message",
        title="tug test",
        priority="default",
    )
    if result.status is not None:
        print("HTTP %s" % result.status)
    else:
        print("error: %s" % result.error)
    return 0 if result.ok else 1


def cmd_status(args) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2

    service = "unknown (no instance.json)"
    instance_path = tugline_path("instance.json")
    if os.path.isfile(instance_path):
        try:
            with open(instance_path, encoding="utf-8") as fh:
                instance = json.load(fh)
            topic_list = ", ".join(instance.get("topics", []))
            service = "port %s, topics %s" % (instance.get("port"), topic_list)
        except (OSError, ValueError):
            service = "unreadable instance.json"
    print("service: %s" % service)

    spool_root = tugline_path("spool")
    all_records = list(spool.records(spool_root))
    pending = list(spool.undelivered(spool_root))
    print(
        "spool: %d records on file, %d undelivered" % (len(all_records), len(pending))
    )

    reply = _last_reply()
    if reply is None:
        print("last reply: none")
    else:
        consumed = "yes" if reply.get("consumed") else "no"
        text = reply.get("body") or reply.get("title") or ""
        print("last reply: %s (consumed: %s)" % (text, consumed))
    return 0


def _last_reply():
    path = tugline_path("replies.jsonl")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except ValueError:
        return None


def cmd_daemon(args) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    daemon = Daemon(
        config,
        spool_root=paths.spool_root(),
        replies_path=tugline_path("replies.jsonl"),
        state_root=paths.state_root(),
    )
    if args.once:
        result = daemon.run_once()
        print("handled %d, replies %d" % (result["handled"], result["replies"]))
        return 0
    daemon.serve()
    return 0


def cmd_console(args) -> int:
    import signal
    import threading
    import webbrowser

    from tugline.console.server import ConsoleServer
    from tugline.core.store import Store

    try:
        config = load_config()
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    if not config.toggles.console:
        print("console is off in config, nothing to serve")
        return 0
    store = Store(paths.store_path())
    server = ConsoleServer(
        config, store, home=paths.home_root(), port=args.port
    )
    if not server.start():
        print(
            "console could not bind 127.0.0.1:%d" % (args.port or server.requested_port),
            file=sys.stderr,
        )
        store.close()
        return 1
    url = "http://127.0.0.1:%d/" % server.port
    print("tug console listening on %s" % url, flush=True)
    print(url, flush=True)
    if args.open:
        webbrowser.open(url)
    stopped = threading.Event()

    def _stop(signum, frame):
        stopped.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    while not stopped.is_set():
        stopped.wait(0.5)
    server.stop()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
