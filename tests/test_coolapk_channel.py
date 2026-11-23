import json
from unittest.mock import patch

import pytest

from agent_reach.channels.coolapk import CoolapkChannel, _feed_id
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_SETUP,
    NOT_FOUND,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    ReachError,
)
from agent_reach.runtime.run import CommandFailed


def test_coolapk_url_routing_and_target_validation():
    channel = CoolapkChannel()
    assert channel.can_handle("https://www.coolapk.com/feed/123")
    assert not channel.can_handle("https://coolapk.com")
    assert not channel.can_handle("https://coolapk.com.evil.example/feed/123")
    assert _feed_id("123") == 123
    assert _feed_id("https://www.coolapk.com/feed/123?from=share") == 123
    for target in (
        "http://www.coolapk.com/feed/123",
        "https://coolapk.com.evil.example/feed/123",
        "https://user:pass@coolapk.com/feed/123",
        "https://coolapk.com/feed/0",
        "https://coolapk.com/user/123",
    ):
        with pytest.raises(ReachError):
            _feed_id(target)


def test_coolapk_normalizes_search_rows_and_caps_output():
    payload = [{
        "id": 123,
        "messageTitle": "A phone",
        "content": "body",
        "username": "author",
        "dateline": "2026-11-20 10:00",
        "like_num": 3,
        "reply_num": 4,
        "images": ["https://img.example/a.jpg"],
    }]
    with patch("agent_reach.channels.coolapk._run", return_value=payload) as call:
        result = CoolapkChannel()._cli_search(query="phone", limit=1, page=1, timeout=5)

    assert result[0]["title"] == "A phone"
    assert result[0]["text"] == "body"
    assert result[0]["url"] == "https://www.coolapk.com/feed/123"
    assert result[0]["raw"]["reply_num"] == 4
    assert call.call_args.args[0][:3] == ["search", "--type", "feed"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": "当前访问需要验证码", "code": -1}, PLATFORM_BLOCKED),
        ({"error": "too many requests", "code": "rate_limited"}, RATE_LIMITED),
        ({"error": "no such feed", "code": "not_found"}, NOT_FOUND),
    ],
)
def test_coolapk_maps_upstream_errors_without_fallback(payload, expected):
    channel = CoolapkChannel()
    with patch("agent_reach.channels.coolapk.run_cmd", side_effect=CommandFailed("UPSTREAM_BROKEN", "failed", stdout=json.dumps(payload))) as command:
        result = run_action(channel, "read", {"target": "123"})
    assert result["ok"] is False
    assert result["error"]["code"] == expected
    command.assert_called_once()
    environment = command.call_args.kwargs["env"]
    assert environment["HOME"] == environment["USERPROFILE"]
    assert environment["HOME"].endswith("coolapk-mcp")


def test_coolapk_rejects_bad_json_and_result_shapes():
    channel = CoolapkChannel()
    with patch("agent_reach.channels.coolapk.run_cmd", return_value="not json"):
        result = run_action(channel, "hot", {"limit": 1})
    assert result["error"]["code"] == "UPSTREAM_BROKEN"

    with patch("agent_reach.channels.coolapk._run", return_value={"unexpected": True}):
        result = run_action(channel, "hot", {"limit": 1})
    assert result["error"]["code"] == "UPSTREAM_BROKEN"


@pytest.mark.parametrize("raw_traceback", [True, False])
def test_coolapk_reports_missing_app_prerequisite_without_treating_it_as_login(raw_traceback):
    message = "libauth blob 未缓存。 upstream bootstrap instructions"
    failed = CommandFailed(
        "UPSTREAM_BROKEN", "failed",
        stdout="startup output" if raw_traceback else json.dumps({"error": message}),
        stderr=f"RuntimeError: {message}" if raw_traceback else "",
    )
    with patch("agent_reach.channels.coolapk.run_cmd", side_effect=failed) as command:
        result = run_action(CoolapkChannel(), "search", {"query": "phone"})
    assert result["error"]["code"] == NEED_SETUP
    assert result["items"] == []
    assert "official Coolapk app" in result["error"]["hint"]
    assert "bootstrap instructions" not in json.dumps(result)
    command.assert_called_once()


def test_coolapk_enforces_bounded_pages_and_searches_read_only():
    channel = CoolapkChannel()
    result = run_action(channel, "hot", {"limit": 21})
    assert result["ok"] is False
    assert result["error"]["code"] == BAD_INPUT
    with patch("agent_reach.channels.coolapk._run", return_value=[]) as run:
        result = run_action(channel, "secondhand", {"brand": 1005, "limit": 2, "page": 1})
    assert result["ok"] is True
    run.assert_called_once()
    assert run.call_args.args[0] == ["secondhand", "--page", "1", "--brand", "1005"]
    assert 0 < run.call_args.kwargs["timeout"] <= 30.0



def test_coolapk_dateline_is_unix_seconds():
    from agent_reach.channels.coolapk import _item

    item = _item({"id": 1, "message": "x", "dateline": 1700000000}, scope="feed_list")
    assert item["published_at"] == "2023-11-14T22:13:20Z"
    assert _item({"id": 1, "dateline": "1700000000000"}, scope="feed_list")["published_at"] == "2023-11-14T22:13:20Z"
    assert _item({"id": 1, "dateline": "2024-01-02T03:04:05"}, scope="feed_list")["published_at"].startswith("2024-01-02T03:04:05")


def test_coolapk_author_message_is_not_login():
    from agent_reach.channels.coolapk import _upstream_error

    assert _upstream_error({"error": "missing author"}).code != "NEED_LOGIN"
    assert _upstream_error({"error": "login required"}).code == "NEED_LOGIN"
