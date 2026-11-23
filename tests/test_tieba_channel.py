import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_reach.channels._tieba_worker import _post_row, _search_row, _thread_row
from agent_reach.channels.tieba import TiebaChannel, _parse_target, _read_items, _search_items
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import BAD_INPUT, PLATFORM_BLOCKED, RATE_LIMITED, ReachError
from agent_reach.runtime.run import CommandFailed


def test_tieba_url_routing_and_strict_target_parsing():
    channel = TiebaChannel()
    assert channel.can_handle("https://tieba.baidu.com/p/123456?see_lz=1")
    assert channel.can_handle("https://tieba.com/p/123456")
    assert not channel.can_handle("https://tieba.baidu.com.evil.example/p/123456")
    assert _parse_target("123456") == 123456
    assert _parse_target("https://tieba.baidu.com/p/123456?see_lz=1") == 123456
    for target in (
        "http://tieba.baidu.com/p/123456",
        "https://tieba.baidu.com.evil.example/p/123456",
        "https://user:pass@tieba.baidu.com/p/123456",
        "https://tieba.baidu.com/p/0",
        "https://tieba.baidu.com/f?kw=python",
    ):
        with pytest.raises(ReachError):
            _parse_target(target)


def test_tieba_search_rows_normalize_and_encode_pids():
    items = _search_items([{
        "title": "A thread",
        "text": "A reply",
        "tid": 123,
        "pid": 456,
        "ppid": 400,
        "forum": "python",
        "author": "Reader",
        "created": 1700000000,
        "is_comment": True,
    }])
    assert len(items) == 1
    assert items[0]["url"] == "https://tieba.baidu.com/p/123?pid=456#pid456"
    assert items[0]["author"] == "Reader"
    assert items[0]["raw"]["is_comment"] is True


def test_tieba_read_marks_nested_reply_sample_as_incomplete():
    result = {
        "thread": {"title": "A thread", "text": "Opening post", "tid": 123, "author": "OP", "created": 1700000000},
        "items": [{
            "floor": 2, "pid": 456, "author": "User", "text": "A reply", "created": 1700000001,
            "reply_count": 9,
            "comments": [{"pid": 789, "author": "Nested", "text": "A nested reply", "created": 1700000002}],
        }],
        "meta": {"page": 1, "has_more": True, "comment_limit_per_floor": 1},
    }
    items = _read_items(result, 123)
    assert len(items) == 2
    floor = items[1]
    assert "Nested" in floor["text"]
    assert floor["raw"]["comments_reported"] == 9
    assert floor["raw"]["comments_may_be_incomplete"] is True
    assert floor["raw"]["comment_scope_note"]
    assert floor["raw"]["page"] == 1
    assert floor["raw"]["has_more"] is True


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ok": False, "error": {"code": "RATE_LIMITED", "message": "too many"}}, RATE_LIMITED),
        ({"ok": False, "error": {"code": "PLATFORM_BLOCKED", "message": "verification"}}, PLATFORM_BLOCKED),
    ],
)
def test_tieba_worker_errors_are_final(payload, expected):
    with patch("agent_reach.channels.tieba.python_for_tool", return_value="C:/private/venv/python.exe"), patch(
        "agent_reach.channels.tieba.run_cmd", side_effect=CommandFailed("UPSTREAM_BROKEN", "failed", stdout=json.dumps(payload))
    ) as run:
        result = run_action(TiebaChannel(), "search", {"query": "x", "limit": 1})
    assert result["ok"] is False
    assert result["error"]["code"] == expected
    run.assert_called_once()


def test_tieba_worker_uses_private_python_and_json_input():
    payload = {"ok": True, "items": [], "meta": {"scope": "global"}}
    with patch("agent_reach.channels.tieba.python_for_tool", return_value="C:/private/venv/python.exe"), patch(
        "agent_reach.channels.tieba.run_cmd", return_value=json.dumps(payload)
    ) as run:
        result = TiebaChannel()._api_search(query="some topic", forum=None, limit=2, page=1, timeout=7)

    assert result == []
    argv = run.call_args.args[0]
    assert argv[0] == "C:/private/venv/python.exe"
    assert argv[1:4] == ["-I", "-X", "utf8"]
    assert Path(argv[4]).is_absolute()
    assert Path(argv[4]).name == "_tieba_worker.py"
    sent = json.loads(run.call_args.kwargs["input_data"])
    assert sent == {"action": "search", "query": "some topic", "forum": None, "limit": 2, "page": 1, "timeout": 7}
    assert run.call_args.kwargs["timeout"] == 7


def test_tieba_enforces_api_page_and_comment_bounds():
    channel = TiebaChannel()
    assert run_action(channel, "search", {"query": "x", "limit": 21})["error"]["code"] == BAD_INPUT
    assert run_action(channel, "read", {"target": "https://evil.example/p/123"})["error"]["code"] == BAD_INPUT
    assert run_action(channel, "read", {"target": "123", "comment_limit": 6})["error"]["code"] == BAD_INPUT
    assert run_action(channel, "threads", {"forum": "python", "sort": "random"})["error"]["code"] == BAD_INPUT


def test_tieba_worker_serializers_keep_bounded_fields():
    class User:
        show_name = "author"

    class Thread:
        tid = 123
        title = "topic"
        text = "body"
        fname = "python"
        user = User()
        create_time = 1700000000
        last_time = 1700000001
        reply_num = 12
        agree = 4
        is_good = False
        is_top = False

    assert _thread_row(Thread())["tid"] == 123

    class SearchRow:
        tid = 123
        pid = 456
        ppid = 0
        title = "topic"
        text = "body"
        fname = "python"
        show_name = "author"
        create_time = 1700000000
        content_type = type("Kind", (), {"value": 2})()

    assert _search_row(SearchRow())["pid"] == 456

    class Post:
        floor = 1
        pid = 2
        user = User()
        text = "body"
        create_time = 1700000000
        agree = 3
        reply_num = 8
        is_thread_author = False
        comments = [type("Comment", (), {"pid": 9, "user": User(), "text": "nested", "create_time": 1700000001})()]

    row = _post_row(Post(), 1)
    assert row["comments"][0]["pid"] == 9

