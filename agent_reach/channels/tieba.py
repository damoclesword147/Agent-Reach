# -*- coding: utf-8 -*-
"""Read-only Tieba search and thread reading through an isolated aiotieba tool."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    NEED_SETUP,
    NOT_FOUND,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    TIMEOUT,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
    unix_to_iso,
)
from agent_reach.runtime.run import CommandFailed, run_cmd
from agent_reach.runtime.tools import python_for_tool

from .base import Action, Channel, Param

_TID_RE = re.compile(r"^/p/([0-9]{1,19})/?$")
_MAX_PAGE = 1000
_MAX_COMMENT_LIMIT = 5
_COMMENT_PARTIAL_NOTE = "Only up to the requested number of high-ranking nested replies per floor are returned."


def _parse_target(target: str) -> int:
    value = str(target).strip()
    if re.fullmatch(r"[0-9]{1,19}", value) and 0 < int(value) < 2**63:
        return int(value)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        parsed = None
        port = None
    if (
        parsed
        and parsed.scheme == "https"
        and (parsed.hostname or "").lower() in {"tieba.baidu.com", "tieba.com"}
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
    ):
        match = _TID_RE.fullmatch(parsed.path)
        if match and 0 < int(match.group(1)) < 2**63:
            return int(match.group(1))
    raise ReachError(BAD_INPUT, "target must be a Tieba thread id or https://tieba.baidu.com/p/<id>")


def _validate_window(limit: int, page: int, *, maximum: int) -> None:
    if not 1 <= limit <= maximum:
        raise ReachError(BAD_INPUT, f"limit must be between 1 and {maximum}")
    if not 1 <= page <= _MAX_PAGE:
        raise ReachError(BAD_INPUT, f"page must be between 1 and {_MAX_PAGE}")


def _worker(action: str, params: dict, *, timeout: float) -> dict:
    python = python_for_tool("aiotieba")
    if not python:
        raise ReachError(NEED_SETUP, "aiotieba is not installed; run `agent-reach add tieba`")
    payload = {"action": action, **params, "timeout": timeout}
    argv = [python, "-I", "-X", "utf8", str(Path(__file__).with_name("_tieba_worker.py"))]
    try:
        output = run_cmd(argv, timeout=timeout, label="aiotieba", input_data=json.dumps(payload, ensure_ascii=False))
    except CommandFailed as exc:
        output = exc.stdout.strip()
        if not output:
            raise ReachError(UPSTREAM_BROKEN, "Tieba worker exited without a JSON response") from None
    try:
        result = json.loads(output)
    except (TypeError, ValueError):
        raise ReachError(UPSTREAM_BROKEN, "Tieba worker returned invalid JSON") from None
    if not isinstance(result, dict):
        raise ReachError(UPSTREAM_BROKEN, "Tieba worker returned an unexpected result shape")
    if not result.get("ok"):
        error = result.get("error") or {}
        code = str(error.get("code") or UPSTREAM_BROKEN)
        message = str(error.get("message") or "Tieba request failed")[:400]
        if code not in {NEED_SETUP, NEED_LOGIN, NOT_FOUND, PLATFORM_BLOCKED, RATE_LIMITED, TIMEOUT, UPSTREAM_BROKEN, BAD_INPUT}:
            code = UPSTREAM_BROKEN
        if code == NEED_LOGIN:
            raise ReachError(code, message, hint="This read-only channel does not import account credentials; view the content on the official site.")
        raise ReachError(code, message)
    return result


def _url(tid: int, pid: int | None = None) -> str:
    base = f"https://tieba.baidu.com/p/{tid}"
    return f"{base}?pid={pid}#pid{pid}" if pid else base


def _raw(row: dict, *, kind: str) -> dict:
    return {"kind": kind, **row}


def _paging(result: dict, scope: str) -> dict:
    value = result.get("meta")
    meta = value if isinstance(value, dict) else {}
    return {
        "page": meta.get("page"),
        "has_more": meta.get("has_more"),
        "total_pages": meta.get("total_pages"),
        "content_scope": scope,
        "completeness_verified": False,
    }


def _search_items(rows: list, paging: dict | None = None) -> list:
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tid = int(row.get("tid") or 0)
        pid = int(row.get("pid") or 0)
        if not tid:
            continue
        title = str(row.get("title") or "").strip()
        text = str(row.get("text") or "").strip()
        result.append(
            make_item(
                title=title or None,
                url=_url(tid, pid),
                author=row.get("author") or None,
                published_at=unix_to_iso(row.get("created")),
                text=text[:30_000] or None,
                raw=_raw({**{k: row.get(k) for k in ("forum", "tid", "pid", "ppid", "is_comment", "text_truncated")},
                          **(paging or {})}, kind="search_result"),
            )
        )
    return result


def _thread_items(result: dict) -> list:
    items = []
    for row in result.get("items") or []:
        if not isinstance(row, dict):
            continue
        tid = int(row.get("tid") or 0)
        if not tid:
            continue
        items.append(
            make_item(
                title=row.get("title"),
                url=_url(tid),
                author=row.get("author"),
                published_at=unix_to_iso(row.get("created")),
                text=str(row.get("text") or "")[:30_000] or None,
                raw=_raw({**{k: row.get(k) for k in ("forum", "tid", "reply_count", "agree", "is_good", "is_top", "text_truncated")},
                          **_paging(result, "thread_list")}, kind="thread"),
            )
        )
    return items


def _read_items(result: dict, tid: int) -> list:
    items = []
    thread = result.get("thread") or {}
    if isinstance(thread, dict) and int(thread.get("tid") or 0) == tid:
        items.append(
            make_item(
                title=thread.get("title"),
                url=_url(tid),
                author=thread.get("author"),
                published_at=unix_to_iso(thread.get("created")),
                text=str(thread.get("text") or "")[:30_000] or None,
                raw=_raw({**{k: thread.get(k) for k in ("forum", "tid", "reply_count", "agree", "is_good", "is_top", "text_truncated")},
                          **_paging(result, "thread_page")}, kind="thread"),
            )
        )
    value = result.get("meta")
    meta = value if isinstance(value, dict) else {}
    comment_limit = int(meta.get("comment_limit_per_floor") or 0)
    if not items:
        raise ReachError(NOT_FOUND, f"Tieba thread not found: {tid}")
    for row in result.get("items") or []:
        if not isinstance(row, dict):
            continue
        floor = int(row.get("floor") or 0)
        pid = int(row.get("pid") or 0)
        value = row.get("comments")
        comments = value if isinstance(value, list) else []
        comments_returned = len(comments)
        total_comments = int(row.get("reply_count") or 0)
        raw = {
            "kind": "floor",
            "thread_id": tid,
            "floor": floor,
            "pid": pid,
            "agree": row.get("agree"),
            "is_thread_author": row.get("is_thread_author"),
            "comment_limit_per_floor": comment_limit,
            "comments_returned": comments_returned,
            "comments_reported": total_comments,
            "comments_may_be_incomplete": bool(total_comments > comments_returned),
            "comments": comments[:comment_limit] if comment_limit else [],
            **_paging(result, "floor_with_sampled_replies" if comment_limit else "floor"),
        }
        text = str(row.get("text") or "").strip()
        if comments:
            text += "\n" + "\n".join(
                f"  @{str(comment.get('author') or 'unknown')}: {str(comment.get('text') or '')}"
                for comment in comments[:comment_limit]
                if isinstance(comment, dict)
            )
        if comment_limit:
            raw["comment_scope_note"] = _COMMENT_PARTIAL_NOTE
        raw["text_truncated"] = bool(row.get("text_truncated") or len(text) > 30_000)
        items.append(
            make_item(
                title=f"Floor {floor}" if floor else f"Reply on thread {tid}",
                url=_url(tid, pid),
                author=row.get("author"),
                published_at=unix_to_iso(row.get("created")),
                text=text[:30_000] or None,
                raw=raw,
            )
        )
    return items


class TiebaChannel(Channel):
    name = "tieba"
    description = "百度贴吧公开主题、楼层与吧内搜索"
    level = "optional"
    tools = ("aiotieba",)
    actions = (
        Action(
            "search",
            "Search public Tieba threads (global search covers threads only)",
            (
                Param("query", "search terms"),
                Param("forum", "optional forum name", required=False, default=None),
                Param("limit", "maximum results (1-20)", required=False, type=int, default=10),
                Param("page", "result page (1-1000)", required=False, type=int, default=1),
            ),
            (("aiotieba", "_api_search"),),
            live_test={"query": "python", "limit": 1, "page": 1},
        ),
        Action(
            "threads",
            "Browse public threads in a forum",
            (
                Param("forum", "forum name"),
                Param("limit", "maximum results (1-100)", required=False, type=int, default=10),
                Param("page", "result page (1-1000)", required=False, type=int, default=1),
                Param("sort", "reply, create, or hot", required=False, default="reply"),
            ),
            (("aiotieba", "_api_threads"),),
            live_test={"forum": "python", "limit": 1, "page": 1, "sort": "reply"},
        ),
        Action(
            "read",
            "Read one page of a public thread; nested replies are an optional bounded sample",
            (
                Param("target", "thread id or Tieba thread URL"),
                Param("limit", "floors per page (2-30)", required=False, type=int, default=20),
                Param("page", "thread page (1-1000)", required=False, type=int, default=1),
                Param("comment_limit", "nested replies per floor (0-5; sampled)", required=False, type=int, default=0),
            ),
            (("aiotieba", "_api_read"),),
        ),
    )

    def can_handle(self, url: str) -> bool:
        try:
            return bool(_parse_target(url)) and url.startswith("https://")
        except ReachError:
            return False

    def validate_params(self, action: str, params: dict) -> None:
        if action == "search":
            _validate_window(int(params["limit"]), int(params["page"]), maximum=20)
            if params.get("forum") is not None and not str(params["forum"]).strip():
                raise ReachError(BAD_INPUT, "forum must not be empty")
        elif action == "threads":
            _validate_window(int(params["limit"]), int(params["page"]), maximum=100)
            if str(params["sort"]).lower() not in {"reply", "create", "hot"}:
                raise ReachError(BAD_INPUT, "sort must be reply, create, or hot")
        elif action == "read":
            _parse_target(str(params["target"]))
            _validate_window(int(params["limit"]), int(params["page"]), maximum=30)
            if int(params["limit"]) < 2:
                raise ReachError(BAD_INPUT, "limit must be between 2 and 30 for thread reads")
            if not 0 <= int(params["comment_limit"]) <= _MAX_COMMENT_LIMIT:
                raise ReachError(BAD_INPUT, f"comment_limit must be between 0 and {_MAX_COMMENT_LIMIT}")

    def _call(self, action: str, params: dict, timeout: float) -> dict:
        return _worker(action, params, timeout=timeout)

    def _api_search(self, *, query: str, forum: str | None, limit: int, page: int, timeout: float) -> list:
        result = self._call("search", {"query": query, "forum": forum, "limit": limit, "page": page}, timeout)
        return _search_items(result.get("items") or [], _paging(result, "search_results"))

    def _api_threads(self, *, forum: str, limit: int, page: int, sort: str, timeout: float) -> list:
        result = self._call("threads", {"forum": forum, "limit": limit, "page": page, "sort": sort.lower()}, timeout)
        return _thread_items(result)

    def _api_read(self, *, target: str, limit: int, page: int, comment_limit: int, timeout: float) -> list:
        tid = _parse_target(target)
        result = self._call(
            "read",
            {"tid": tid, "limit": limit, "page": page, "comment_limit": comment_limit},
            timeout,
        )
        return _read_items(result, tid)
