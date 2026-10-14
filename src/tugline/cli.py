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

    label = sub.add_parser(
        "label", help="set or clear one session's display name"
    )
    label.add_argument(
        "--session",
        required=True,
        help="session id to label (always explicit; no env fallback)",
    )
    label.add_argument("name", nargs="?", default=None, help="the label")
    label.add_argument(
        "--clear", action="store_true", help="clear the label instead"
    )

    tag = sub.add_parser(
        "tag", help="attach one or more tags to a session"
    )
    tag.add_argument(
        "--session",
        required=True,
        help="session id to tag (always explicit; no env fallback)",
    )
    tag.add_argument("tags", nargs="+", help="tags to attach")

    untag = sub.add_parser(
        "untag", help="detach one or more tags from a session"
    )
    untag.add_argument(
        "--session",
        required=True,
        help="session id (always explicit; no env fallback)",
    )
    untag.add_argument("tags", nargs="+", help="tags to detach")

    sessions = sub.add_parser(
        "sessions", help="list sessions with label and tags (read-only)"
    )
    sessions.add_argument("--label", default=None, help="exact label filter")
    sessions.add_argument("--tag", default=None, help="tag filter")
    sessions.add_argument(
        "--json", action="store_true", help="one JSON array instead of rows"
    )

    ask = sub.add_parser(
        "ask",
        help="ask the operator one multiple choice question and wait",
    )
    ask.add_argument(
        "--session",
        required=True,
        help="session id asking (always explicit; no env fallback)",
    )
    ask.add_argument(
        "--option",
        action="append",
        dest="options",
        default=None,
        metavar="OPTION",
        help='lettered choice such as "A: description" (2-8, repeatable;'
        " bare options are lettered automatically)",
    )
    ask.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="seconds to wait for an answer (default 300, max 1800)",
    )
    ask.add_argument("question", help="the question text")

    answer = sub.add_parser(
        "answer", help="answer an ask thread (agent or controller side)"
    )
    answer.add_argument(
        "--thread",
        required=True,
        help="the ask thread nonce",
    )
    answer.add_argument("key", help="the chosen option key")

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
        "label": cmd_label,
        "tag": cmd_tag,
        "untag": cmd_untag,
        "sessions": cmd_sessions,
        "ask": cmd_ask,
        "answer": cmd_answer,
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


# -- session naming (labels and tags) --
#
# Agents know their own session id from context, but CLAUDE_SESSION_ID
# is not exported to agent shells, so these commands never fall back
# to the environment: the id is always an explicit --session flag.


def _named_store():
    """Open the store and fold the spool once.

    The fold is the same idempotent pass the console runs on start,
    so the commands work on a fresh home with no console running.
    """
    from tugline.core.store import Store

    store = Store(paths.store_path())
    store.ingest_spool(paths.spool_root())
    return store


def cmd_label(args) -> int:
    if args.clear and args.name is not None:
        print("give a label or --clear, not both", file=sys.stderr)
        return 2
    if not args.clear and not (args.name or "").strip():
        print("give a label or --clear", file=sys.stderr)
        return 2
    store = _named_store()
    try:
        row = store.set_session_label(
            args.session, args.name.strip() if args.name else ""
        )
    except KeyError:
        print(
            "unknown session: %s (sessions come from spooled hook"
            " events)" % args.session,
            file=sys.stderr,
        )
        return 2
    finally:
        store.close()
    if args.clear or row["label"] is None:
        print("label cleared: %s" % args.session)
    else:
        print("labeled %s: %s" % (args.session, row["label"]))
    return 0


def _tag_words(args) -> list:
    """All requested tags normalized, validated up front so a bad one
    never leaves a partial write behind."""
    from tugline.core.store import (
        TAG_MAX_CHARS,
        normalize_tag,
    )

    words = []
    for raw in args.tags:
        tag = normalize_tag(raw)
        if not tag:
            print(
                "not a tag: %r (lowercase letters, digits, hyphens)" % raw,
                file=sys.stderr,
            )
            return []
        if len(tag) > TAG_MAX_CHARS:
            print(
                "tag too long: %r (max %d characters)"
                % (tag, TAG_MAX_CHARS),
                file=sys.stderr,
            )
            return []
        words.append(tag)
    return words


def cmd_tag(args) -> int:
    words = _tag_words(args)
    if not words:
        return 2
    store = _named_store()
    try:
        for word in words:
            store.add_session_tag(args.session, word)
    except KeyError:
        print(
            "unknown session: %s (sessions come from spooled hook"
            " events)" % args.session,
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        store.close()
    for word in words:
        print("tagged %s: %s" % (args.session, word))
    return 0


def cmd_untag(args) -> int:
    words = _tag_words(args)
    if not words:
        return 2
    store = _named_store()
    try:
        for word in words:
            store.remove_session_tag(args.session, word)
    except KeyError:
        print(
            "unknown session: %s (sessions come from spooled hook"
            " events)" % args.session,
            file=sys.stderr,
        )
        return 2
    finally:
        store.close()
    for word in words:
        print("untagged %s: %s" % (args.session, word))
    return 0


def cmd_sessions(args) -> int:
    from tugline.core.store import normalize_tag

    store = _named_store()
    try:
        rows = store.sessions()
        if args.label is not None:
            rows = [r for r in rows if r.get("label") == args.label]
        if args.tag is not None:
            want = normalize_tag(args.tag)
            rows = [r for r in rows if want in (r.get("tags") or [])]
    finally:
        store.close()
    if args.json:
        print(json.dumps(rows))
        return 0
    print("session\tlabel\ttags\tstate\tlast_seen")
    for row in rows:
        label = row.get("label") or "-"
        tags = ",".join(row.get("tags") or []) or "-"
        print(
            "%s\t%s\t%s\t%s\t%s"
            % (
                row.get("session_id", ""),
                label,
                tags,
                row.get("state", ""),
                row.get("last_seen") or "",
            )
        )
    return 0


# -- the ask primitive (W9-L21) --
#
# tug ask blocks for up to the timeout, so it must never traceback:
# every failure lands on a clean exit code (2 usage, 1 no answer).


def _ask_runner(store, config):
    from tugline.core.ask import AskRunner

    return AskRunner(
        store,
        config=config,
        replies_path=tugline_path("replies.jsonl"),
        log=lambda line: print("tug: %s" % line, file=sys.stderr),
    )


def cmd_ask(args) -> int:
    from tugline.core.ask import MAX_TIMEOUT_S, parse_options

    question = " ".join(str(args.question or "").split())
    if not question:
        print("give a question", file=sys.stderr)
        return 2
    if not 1 <= args.timeout <= MAX_TIMEOUT_S:
        print(
            "timeout must be 1 to %d seconds" % MAX_TIMEOUT_S,
            file=sys.stderr,
        )
        return 2
    try:
        options = parse_options(args.options or [])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        config = load_config()
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    try:
        store = _named_store()
    except Exception as exc:
        print("store error: %s" % exc, file=sys.stderr)
        return 2
    try:
        result = _ask_runner(store, config).run(
            args.session, question, options, timeout_s=args.timeout
        )
    except Exception as exc:  # the ask flow never tracebacks
        print("ask error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        store.close()
    if result["ok"]:
        text = result.get("text") or ""
        print(
            "%s: %s" % (result["key"], text) if text else result["key"]
        )
        return 0
    if result.get("reason") == "unknown session":
        print(
            "unknown session: %s (sessions come from spooled hook"
            " events)" % args.session,
            file=sys.stderr,
        )
        return 2
    if result.get("reason") == "timeout":
        print("no answer")
        return 1
    print("ask %s" % result.get("reason"), file=sys.stderr)
    return 1


def cmd_answer(args) -> int:
    store = _named_store()
    try:
        row = store.answer_ask_thread(args.thread, args.key)
    except KeyError:
        print("unknown thread: %s" % args.thread, file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        store.close()
    text = ""
    for option in row.get("options") or []:
        if str(option.get("key")) == str(row.get("answer")):
            text = str(option.get("text") or "")
            break
    print("%s: %s" % (row["answer"], text) if text else row["answer"])
    return 0


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
