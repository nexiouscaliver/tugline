"""Tests for the one-shot ntfy publisher (tugline.core.publisher)."""

import pytest

from tugline.core.publisher import PublishResult, Publisher


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "credentials"
    path.write_text("tok-secret-value-123\n")
    return str(path)


def make_publisher(fake_ntfy, token_file=None, timeout=5.0):
    return Publisher(fake_ntfy.url, token_file=token_file, timeout=timeout)


def test_publish_posts_to_topic_with_auth(fake_ntfy, token_file):
    pub = make_publisher(fake_ntfy, token_file)
    result = pub.publish("tug", "build finished", title="tug", priority="default")
    assert result.ok is True
    assert result.status == 200
    assert result.message_id
    posts = fake_ntfy.posts("/tug")
    assert len(posts) == 1
    assert posts[0]["headers"].get("authorization") == "Bearer tok-secret-value-123"
    assert posts[0]["body"] == "build finished"
    assert posts[0]["headers"].get("x-title") == "tug"
    assert posts[0]["headers"].get("x-priority") == "default"


def test_publish_without_token_file_sends_no_auth(fake_ntfy, tmp_path):
    pub = make_publisher(fake_ntfy, str(tmp_path / "missing-creds"))
    result = pub.publish("tug", "anon")
    assert result.ok is True
    assert "authorization" not in fake_ntfy.posts("/tug")[0]["headers"]


def test_publish_tags_joined(fake_ntfy, token_file):
    pub = make_publisher(fake_ntfy, token_file)
    pub.publish("tug", "x", tags=["warning", "skull"])
    assert fake_ntfy.posts("/tug")[0]["headers"].get("x-tags") == "warning,skull"


def test_publish_click_url_header(fake_ntfy, token_file):
    pub = make_publisher(fake_ntfy, token_file)
    pub.publish("tug", "x", click="https://example.com/log")
    headers = fake_ntfy.posts("/tug")[0]["headers"]
    assert headers.get("x-click") == "https://example.com/log"


def test_publish_actions_header(fake_ntfy, token_file):
    pub = make_publisher(fake_ntfy, token_file)
    pub.publish(
        "tug",
        "x",
        actions=[
            {"action": "view", "label": "Open", "url": "https://example.com"},
            {
                "action": "http",
                "label": "Approve",
                "url": "https://example.com/approve",
                "method": "POST",
            },
        ],
    )
    header = fake_ntfy.posts("/tug")[0]["headers"].get("x-actions")
    parts = header.split("; ")
    assert len(parts) == 2
    assert parts[0].startswith("action=view, label=Open, url=")
    assert "method=POST" in parts[1]


def test_publish_actions_capped_at_three(fake_ntfy, token_file):
    pub = make_publisher(fake_ntfy, token_file)
    actions = [
        {"action": "view", "label": "n%d" % i, "url": "https://e/%d" % i}
        for i in range(5)
    ]
    pub.publish("tug", "x", actions=actions)
    header = fake_ntfy.posts("/tug")[0]["headers"].get("x-actions")
    assert header.count("; ") == 2
    assert "n0" in header and "n2" in header and "n3" not in header


def test_server_error_500_reported(fake_ntfy, token_file):
    fake_ntfy.fail_posts = 1
    pub = make_publisher(fake_ntfy, token_file)
    result = pub.publish("tug", "x")
    assert result.ok is False
    assert result.status == 500
    assert "500" in result.error


def test_timeout_yields_error_result_without_status(fake_ntfy, token_file):
    fake_ntfy.delay = 1.0
    pub = make_publisher(fake_ntfy, token_file, timeout=0.2)
    result = pub.publish("tug", "x")
    assert result.ok is False
    assert result.status is None
    assert result.error
    fake_ntfy.delay = 0.0


def test_token_never_appears_in_error_text(fake_ntfy, token_file):
    fake_ntfy.fail_posts = 3
    pub = make_publisher(fake_ntfy, token_file)
    result = pub.publish("tug", "x")
    assert result.ok is False
    assert "tok-secret-value-123" not in str(result)
    assert "tok-secret-value-123" not in repr(result)
    assert "tok-secret-value-123" not in (result.error or "")


def test_empty_token_file_means_anonymous(fake_ntfy, tmp_path):
    path = tmp_path / "credentials"
    path.write_text("\n")
    pub = make_publisher(fake_ntfy, str(path))
    pub.publish("tug", "x")
    assert "authorization" not in fake_ntfy.posts("/tug")[0]["headers"]


def test_result_object_is_plain_data():
    r = PublishResult(ok=True, status=200, message_id="m1", error=None)
    assert r.ok and r.status == 200


def test_token_file_with_key_value_lines(fake_ntfy, tmp_path):
    path = tmp_path / "credentials"
    path.write_text("ntfy_user=shahil\nntfy_token=tk_abcd1234\n")
    pub = make_publisher(fake_ntfy, str(path))
    pub.publish("tug", "x")
    header = fake_ntfy.posts("/tug")[0]["headers"].get("authorization")
    assert header == "Bearer tk_abcd1234"


def test_token_file_bare_line_still_works(fake_ntfy, tmp_path):
    path = tmp_path / "credentials"
    path.write_text("tk_plain999\n")
    pub = make_publisher(fake_ntfy, str(path))
    pub.publish("tug", "x")
    header = fake_ntfy.posts("/tug")[0]["headers"].get("authorization")
    assert header == "Bearer tk_plain999"


def test_publish_actions_header_encodes_headers_as_pairs(fake_ntfy, token_file):
    pub = make_publisher(fake_ntfy, token_file)
    pub.publish(
        "tug",
        "x",
        actions=[
            {
                "action": "http",
                "label": "Approve",
                "url": "http://127.0.0.1:2587/approvals/n1?decision=allow",
                "method": "POST",
                "headers": {"Authorization": "Bearer tk_pair_test"},
            }
        ],
    )
    header = fake_ntfy.posts("/tug")[0]["headers"].get("x-actions")
    assert "headers.Authorization=Bearer tk_pair_test" in header
    assert "method=POST" in header
