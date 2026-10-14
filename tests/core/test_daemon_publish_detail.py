"""Publish-time detail parity between the phone buzz and the console.

The naming parity audit (2026-10-14) confirmed the gaps this file
pins: the only labeled session showed its label nowhere on the phone,
hook fragments matched console display ids for some sessions and not
others, stop records carried a response preview the console rendered
but the phone never saw, and no buzz linked back to the console.

Pinned behaviors (W9 detail parity):

- NAMING: at publish time the daemon resolves the record's session to
  the same display name the console shows (the label when set, else
  the collision-lengthened unique suffix), rewrites the "session
  <frag>" token the hook wrote, and prefixes the title with
  "[<name>] " unless the title already carries it. Sessions with no
  store row publish exactly what the hook spooled. The lookup is one
  read-only SELECT per flush pass, never per record.
- PREVIEW: a stop record carrying response_preview publishes the body
  plus a blank line plus up to 300 characters of the preview,
  newlines kept, nothing collapsed.
- TAGS: the record's adapter and project ride along as ntfy tags
  (lowercase, hyphens for spaces) after the per-event emoji tags.
- CLICK: stop, permissionrequest, and resumed sessionstart records
  carry X-Click pointing at [server] console_url plus a
  "#session/<id>" fragment; empty console_url means no click at all,
  and notify records never carry one. The token never rides in the
  URL.
"""

from datetime import datetime, timezone

from tugline.core import config as cfg
from tugline.core import policy, spool
from tugline.core.daemon import Daemon
from tugline.core.store import Store

WHEN = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)

SID_LABELED = "sess-517ee9c0-bdc0-49a9-9938-1c2e4de851df"
SHORT6_LABELED = "e851df"
LABEL = "controller"

# Two sessions sharing their last 6 characters: the hook prints the
# shared suffix (it cannot know better without store access) and the
# publish-time pass lengthens it to the same distinct name the console
# shows.
SID_A = "sess-run1-actor_1_1"
SID_B = "sess-run2-actor_1_1"
SHARED6 = "or_1_1"
DISPLAY_A = "1-actor_1_1"
DISPLAY_B = "2-actor_1_1"

SID_P = "sess-abc123def456"
SHORT6_P = "def456"

PREVIEW = "a" * 50 + "\n" + "b" * 50 + "\n" + "c" * 400
PREVIEW_CAPPED = PREVIEW[:300]

CONSOLE_URL = "https://tug.tailnet.example"


def make_config(url, console_url=""):
    return cfg.Config(
        server=cfg.ServerConfig(url, None, console_url=console_url),
        topics=cfg.TopicsConfig("tug", "tug-warn", "tug-reply"),
        policy=cfg.PolicyConfig(600),
        toggles=cfg.TogglesConfig(True, True, False),
        path="/none",
    )


def make_daemon(fake_ntfy, tmp_path, console_url=""):
    return Daemon(
        make_config(fake_ntfy.url, console_url),
        spool_root=str(tmp_path / "spool"),
        replies_path=str(tmp_path / "replies.jsonl"),
        state_root=str(tmp_path / "state"),
        poll_timeout=2.0,
        backoff_base=0.01,
        cycle_idle=0.0,
    )


def seed_store(tmp_path, session_ids, labels=None):
    """Create the store the daemon reads (<tmp>/store/tugline.db).

    One session row per id, ingested through the real spool fold so
    the daemon sees the same shape the console writes; notify seeds
    never trigger auto-labeling, so an unlabeled id stays unlabeled.
    """
    seed_root = str(tmp_path / "seed-spool")
    for sid in session_ids:
        spool.append(
            seed_root,
            event="notify",
            session_id=sid,
            topic="tug",
            priority="default",
            title="seed",
            body="seed %s" % sid,
            now=WHEN,
        )
    store = Store(tmp_path / "store" / "tugline.db")
    store.ingest_spool(seed_root)
    for sid, label in (labels or {}).items():
        store.set_session_label(sid, label)
    store.close()


def spool_record(root, event, session_id, *, title, body, **extra):
    return spool.append(
        str(root),
        event=event,
        session_id=session_id,
        topic="tug-warn" if event == "permissionrequest" else "tug",
        priority="high" if event == "permissionrequest" else "default",
        title=title,
        body=body,
        extra=extra or None,
        now=WHEN,
    )


def spool_stop(root, session_id, *, title, body, **extra):
    return spool_record(root, "stop", session_id, title=title, body=body, **extra)


def arm(tmp_path, *session_ids):
    state_root = str(tmp_path / "state")
    for sid in session_ids:
        policy.arm(sid, state_root=state_root)


# -- publish-time naming --


def test_labeled_session_publishes_the_label_in_title_and_body(
    fake_ntfy, tmp_path
):
    seed_store(tmp_path, [SID_LABELED], labels={SID_LABELED: LABEL})
    daemon = make_daemon(fake_ntfy, tmp_path)
    arm(tmp_path, SID_LABELED)
    spool_stop(
        str(tmp_path / "spool"),
        SID_LABELED,
        title="[zcode] mission-control - turn finished",
        body="session %s, turn finished" % SHORT6_LABELED,
    )

    daemon.flush()

    post = fake_ntfy.posts()[0]
    assert post["body"] == "session %s, turn finished" % LABEL
    assert (
        post["headers"].get("x-title")
        == "[%s] [zcode] mission-control - turn finished" % LABEL
    )


def test_unlabeled_collision_publishes_the_lengthened_id(fake_ntfy, tmp_path):
    seed_store(tmp_path, [SID_A, SID_B])
    daemon = make_daemon(fake_ntfy, tmp_path)
    arm(tmp_path, SID_A, SID_B)
    root = str(tmp_path / "spool")
    spool_stop(root, SID_A, title="t-a", body="session %s, turn finished" % SHARED6)
    spool_stop(root, SID_B, title="t-b", body="session %s, turn finished" % SHARED6)

    daemon.flush()

    posts = fake_ntfy.posts()
    assert [p["body"] for p in posts] == [
        "session %s, turn finished" % DISPLAY_A,
        "session %s, turn finished" % DISPLAY_B,
    ]
    assert posts[0]["headers"].get("x-title") == "[%s] t-a" % DISPLAY_A
    assert posts[1]["headers"].get("x-title") == "[%s] t-b" % DISPLAY_B


def test_records_without_a_store_row_publish_unchanged(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    arm(tmp_path, SID_P)
    spool_stop(
        str(tmp_path / "spool"),
        SID_P,
        title="session stopped",
        body="session %s stopped: turn finished" % SHORT6_P,
    )

    daemon.flush()

    post = fake_ntfy.posts()[0]
    assert post["body"] == "session %s stopped: turn finished" % SHORT6_P
    assert post["headers"].get("x-title") == "session stopped"


def test_notify_body_keeps_operator_text_except_the_naming_rewrite(
    fake_ntfy, tmp_path
):
    seed_store(tmp_path, [SID_LABELED], labels={SID_LABELED: LABEL})
    daemon = make_daemon(fake_ntfy, tmp_path)
    spool_record(
        str(tmp_path / "spool"),
        "notify",
        SID_LABELED,
        title="ops note",
        body="session %s, ship it" % SHORT6_LABELED,
    )

    daemon.flush()

    post = fake_ntfy.posts()[0]
    assert post["body"] == "session %s, ship it" % LABEL
    assert post["headers"].get("x-title") == "[%s] ops note" % LABEL


def test_naming_lookup_runs_once_per_flush_pass(fake_ntfy, tmp_path, monkeypatch):
    seed_store(tmp_path, [SID_A, SID_B])
    daemon = make_daemon(fake_ntfy, tmp_path)
    arm(tmp_path, SID_A)
    root = str(tmp_path / "spool")
    for sid in (SID_A, SID_B, SID_A):
        spool_stop(root, sid, title="t", body="session %s, done" % SHARED6)
    calls = []
    original = daemon._session_display_names

    def counting(records):
        calls.append(len(records))
        return original(records)

    monkeypatch.setattr(daemon, "_session_display_names", counting)

    daemon.flush()

    assert calls == [3]


# -- richer bodies: the stop response preview --


def test_stop_response_preview_appends_multiline_capped_at_300(
    fake_ntfy, tmp_path
):
    daemon = make_daemon(fake_ntfy, tmp_path)
    arm(tmp_path, SID_P)
    spool_stop(
        str(tmp_path / "spool"),
        SID_P,
        title="session stopped",
        body="session %s, turn finished" % SHORT6_P,
        response_preview=PREVIEW,
    )

    daemon.flush()

    post = fake_ntfy.posts()[0]
    assert post["body"] == (
        "session %s, turn finished\n\n%s" % (SHORT6_P, PREVIEW_CAPPED)
    )
    # newlines kept, nothing collapsed: the phone renders multi-line
    assert "\n" + "a" * 50 + "\n" in post["body"]


def test_stop_without_preview_keeps_the_hook_body(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    arm(tmp_path, SID_P)
    spool_stop(
        str(tmp_path / "spool"),
        SID_P,
        title="session stopped",
        body="session %s, turn finished" % SHORT6_P,
        response_preview="",
    )

    daemon.flush()

    assert fake_ntfy.posts()[0]["body"] == (
        "session %s, turn finished" % SHORT6_P
    )


# -- adapter and project tags --


def test_adapter_and_project_ride_as_tags(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    arm(tmp_path, SID_P)
    spool_stop(
        str(tmp_path / "spool"),
        SID_P,
        title="session stopped",
        body="session %s, turn finished" % SHORT6_P,
        adapter="zcode",
        project="mission control",
    )

    daemon.flush()

    post = fake_ntfy.posts()[0]
    assert post["headers"].get("x-tags") == "white_check_mark,zcode,mission-control"


def test_tags_absent_when_record_carries_no_adapter_or_project(
    fake_ntfy, tmp_path
):
    daemon = make_daemon(fake_ntfy, tmp_path)
    arm(tmp_path, SID_P)
    spool_stop(
        str(tmp_path / "spool"),
        SID_P,
        title="session stopped",
        body="session %s, turn finished" % SHORT6_P,
    )

    daemon.flush()

    assert fake_ntfy.posts()[0]["headers"].get("x-tags") == "white_check_mark"


# -- click-through to the console --


def test_click_url_set_from_config_for_session_scoped_events(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path, console_url=CONSOLE_URL)
    root = str(tmp_path / "spool")
    arm(tmp_path, SID_P)
    spool_stop(
        root,
        SID_P,
        title="session stopped",
        body="session %s, turn finished" % SHORT6_P,
    )
    spool_record(
        root,
        "permissionrequest",
        SID_P,
        title="permission request",
        body="session %s, permission: Bash" % SHORT6_P,
    )
    spool_record(
        root,
        "sessionstart",
        SID_P,
        title="session resumed",
        body="session %s, resumed" % SHORT6_P,
        source="resume",
    )

    daemon.flush()

    wanted = "%s#session/%s" % (CONSOLE_URL, SID_P)
    posts = fake_ntfy.posts()
    assert len(posts) == 3
    assert all(p["headers"].get("x-click") == wanted for p in posts)


def test_click_absent_for_notify_records_even_when_console_url_set(
    fake_ntfy, tmp_path
):
    daemon = make_daemon(fake_ntfy, tmp_path, console_url=CONSOLE_URL)
    spool_record(
        str(tmp_path / "spool"),
        "notify",
        SID_P,
        title="ops note",
        body="session %s, ship it" % SHORT6_P,
    )

    daemon.flush()

    assert "x-click" not in fake_ntfy.posts()[0]["headers"]


def test_click_absent_when_console_url_unset(fake_ntfy, tmp_path):
    daemon = make_daemon(fake_ntfy, tmp_path)
    arm(tmp_path, SID_P)
    spool_stop(
        str(tmp_path / "spool"),
        SID_P,
        title="session stopped",
        body="session %s, turn finished" % SHORT6_P,
    )

    daemon.flush()

    assert "x-click" not in fake_ntfy.posts()[0]["headers"]
