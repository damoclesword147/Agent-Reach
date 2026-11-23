# -*- coding: utf-8 -*-
"""Read-only Coolapk community access through the upstream Coolapk CLI."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    NEED_SETUP,
    NOT_FOUND,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
    unix_to_iso,
)
from agent_reach.runtime.run import CommandFailed, run_cmd
from agent_reach.runtime.sessions import clear_session, session_env

from .base import Action, Channel, Param

_FEED_URL_RE = re.compile(r"^/feed/([0-9]{1,19})/?$")
_MAX_LIMIT = 20
_MAX_PAGE = 100
_MAX_TEXT_CHARS = 30_000
# Whole words only: "author" must not be read as an auth failure.
_LOGIN_RE = re.compile(r"\b(?:login|log in|sign in|unauthori[sz]ed|auth(?:entication|orization)?)\b")


def _missing_app_prerequisite(message: str) -> ReachError | None:
    if "未找到 libauth blob 缓存" in message or "libauth blob 未缓存" in message:
        return ReachError(
            NEED_SETUP,
            "Coolapk CLI lacks the app authentication data required by this backend",
            hint="Agent Reach does not provision this data. Use the official Coolapk app for community access.",
        )
    return None


def _feed_id(target: str) -> int:
    value = str(target).strip()
    if re.fullmatch(r"[0-9]{1,19}", value) and 0 < int(value) < 2**63:
        return int(value)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        parsed = None
        port = None
    if parsed and parsed.scheme == "https" and (parsed.hostname or "").lower() in {
        "coolapk.com", "www.coolapk.com"
    }:
        match = _FEED_URL_RE.fullmatch(parsed.path)
        if match and 0 < int(match.group(1)) < 2**63 and parsed.username is None and parsed.password is None and port in {None, 443}:
            return int(match.group(1))
    raise ReachError(BAD_INPUT, "target must be a Coolapk feed id or https://www.coolapk.com/feed/<id>")


def _upstream_error(payload: Any) -> ReachError | None:
    if not isinstance(payload, dict) or not payload.get("error"):
        return None
    code = str(payload.get("code") or "").casefold()
    message = str(payload["error"])
    if missing := _missing_app_prerequisite(message):
        return missing
    message = message[:500]
    lowered = f"{code} {message}".casefold()
    if "captcha" in lowered or "verification" in lowered or "验证码" in message or "安全验证" in message:
        return ReachError(PLATFORM_BLOCKED, message)
    if re.search(r"rate[ _-]?limit|too many requests|\b429\b", lowered) or "频繁" in message or "限流" in message:
        return ReachError(RATE_LIMITED, message)
    if _LOGIN_RE.search(lowered) or "登录" in message:
        return ReachError(NEED_LOGIN, message, hint="This read-only channel does not import account credentials; view the content in the official app.")
    if "not_found" in code or "不存在" in message:
        return ReachError(NOT_FOUND, message)
    return ReachError(UPSTREAM_BROKEN, message)


def _decode_output(output: str) -> Any:
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        raise ReachError(UPSTREAM_BROKEN, "Coolapk CLI returned invalid JSON") from None
    error = _upstream_error(payload)
    if error:
        raise error
    return payload


def _run(argv: list[str], *, timeout: float) -> Any:
    try:
        output = run_cmd(["coolapk", *argv], timeout=timeout, env=session_env("coolapk-mcp"))
    except CommandFailed as exc:
        if missing := _missing_app_prerequisite(exc.stderr):
            raise missing from None
        for output in (exc.stdout, exc.stderr):
            try:
                payload = json.loads(output)
            except ValueError:
                continue
            error = _upstream_error(payload)
            if error:
                raise error from None
        raise
    return _decode_output(output)


def _as_feed_rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = payload["items"]
    elif isinstance(payload, dict) and payload.get("id") is not None:
        rows = [payload]
    elif payload is None:
        rows = []
    else:
        raise ReachError(UPSTREAM_BROKEN, "Coolapk CLI returned an unexpected result shape")
    feeds = []
    for row in rows:
        if not isinstance(row, dict):
            raise ReachError(UPSTREAM_BROKEN, "Coolapk CLI returned a malformed feed")
        value = str(row.get("id") or "")
        if re.fullmatch(r"[0-9]{1,19}", value) and 0 < int(value) < 2**63:
            if row.get("entityType", "feed") == "feed":
                feeds.append(row)
    return feeds


def _item(row: dict, *, scope: str, page: int | None = None) -> dict:
    feed_id = row.get("id")
    title = str(row.get("title") or row.get("messageTitle") or "").strip()
    text = str(row.get("content") or row.get("message") or "").strip()
    user = str(row.get("username") or row.get("user_name") or "").strip()
    published = row.get("dateline") or row.get("published_at")
    raw = {
        key: row.get(key)
        for key in ("id", "uid", "feed_type", "images", "device", "ip_location")
        if row.get(key) not in (None, "", [], {})
    }
    for field, alias in (("like_num", "likenum"), ("reply_num", "replynum"), ("forward_num", "forwardnum")):
        if field in row or alias in row:
            raw[field] = row.get(field, row.get(alias))
    raw.update(content_scope=scope, completeness_verified=False, text_truncated=len(text) > _MAX_TEXT_CHARS)
    if page is not None:
        raw.update(page=page, has_more=None)
    stamp = _published_at(published)
    return make_item(
        title=title or None,
        url=f"https://www.coolapk.com/feed/{feed_id}" if feed_id is not None else row.get("url"),
        author=user or (str(row.get("uid")) if row.get("uid") else None),
        published_at=stamp,
        text=text[:_MAX_TEXT_CHARS] if text else None,
        raw=raw,
    )


def _published_at(value: Any) -> str | None:
    """Coolapk `dateline` is Unix seconds; tolerate ISO strings from other shapes."""
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)) or re.fullmatch(r"[0-9]{9,13}", str(value).strip()):
        seconds = int(float(value))
        if seconds > 10**11:  # milliseconds
            seconds //= 1000
        return unix_to_iso(seconds)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()


def _validate_window(limit: int, page: int) -> None:
    if not 1 <= limit <= _MAX_LIMIT:
        raise ReachError(BAD_INPUT, f"limit must be between 1 and {_MAX_LIMIT}")
    if not 1 <= page <= _MAX_PAGE:
        raise ReachError(BAD_INPUT, f"page must be between 1 and {_MAX_PAGE}")


class CoolapkChannel(Channel):
    name = "coolapk"
    description = "酷安公开动态、热榜与二手内容（实验性：需自备 App 认证数据）"
    level = "optional"
    tools = ("coolapk-mcp",)
    actions = (
        Action(
            "search",
            "Search public Coolapk feeds",
            (
                Param("query", "search terms"),
                Param("limit", "maximum items (1-20)", required=False, type=int, default=10),
                Param("page", "result page (1-100)", required=False, type=int, default=1),
            ),
            (("coolapk-cli", "_cli_search"),),
            live_test={"query": "手机", "limit": 1, "page": 1},
        ),
        Action(
            "hot",
            "Read public Coolapk hot feeds",
            (
                Param("limit", "maximum items (1-20)", required=False, type=int, default=10),
                Param("page", "result page (1-100)", required=False, type=int, default=1),
            ),
            (("coolapk-cli", "_cli_hot"),),
            live_test={"limit": 1, "page": 1},
        ),
        Action(
            "secondhand",
            "Read public Coolapk second-hand listings",
            (
                Param("brand", "optional brand id", required=False, type=int),
                Param("limit", "maximum items (1-20)", required=False, type=int, default=10),
                Param("page", "result page (1-100)", required=False, type=int, default=1),
            ),
            (("coolapk-cli", "_cli_secondhand"),),
            live_test={"limit": 1, "page": 1},
        ),
        Action(
            "read",
            "Read a public Coolapk feed detail (details may be blocked or incomplete)",
            (Param("target", "feed id or Coolapk feed URL"),),
            (("coolapk-cli", "_cli_read"),),
        ),
    )

    def can_handle(self, url: str) -> bool:
        try:
            return bool(_feed_id(url)) and url.startswith("https://")
        except ReachError:
            return False

    def validate_params(self, action: str, params: dict) -> None:
        if action == "read":
            _feed_id(str(params["target"]))
        if action in {"search", "hot", "secondhand"}:
            _validate_window(int(params["limit"]), int(params["page"]))
            if action == "secondhand" and params.get("brand") is not None and int(params["brand"]) < 0:
                raise ReachError(BAD_INPUT, "brand must be a non-negative numeric id")

    def on_remove(self) -> None:
        clear_session("coolapk-mcp")

    def _cli_search(self, *, query: str, limit: int, page: int, timeout: float) -> list:
        payload = _run(["search", "--type", "feed", "--page", str(page), "--", query], timeout=timeout)
        return [_item(row, scope="search_results", page=page) for row in _as_feed_rows(payload)[:limit]]

    def _cli_hot(self, *, limit: int, page: int, timeout: float) -> list:
        payload = _run(["hot", "--page", str(page)], timeout=timeout)
        return [_item(row, scope="feed_list", page=page) for row in _as_feed_rows(payload)[:limit]]

    def _cli_secondhand(self, *, brand: int | None, limit: int, page: int, timeout: float) -> list:
        argv = ["secondhand", "--page", str(page)]
        if brand is not None:
            argv.extend(["--brand", str(brand)])
        payload = _run(argv, timeout=timeout)
        return [_item(row, scope="feed_list", page=page) for row in _as_feed_rows(payload)[:limit]]

    def _cli_read(self, *, target: str, timeout: float) -> list:
        feed_id = _feed_id(target)
        payload = _run(["feed", str(feed_id), "--no-replies"], timeout=timeout)
        rows = _as_feed_rows(payload)
        if not rows:
            raise ReachError(NOT_FOUND, f"Coolapk feed not found: {feed_id}")
        if int(rows[0]["id"]) != feed_id:
            raise ReachError(UPSTREAM_BROKEN, "Coolapk CLI returned a different feed id")
        return [_item(rows[0], scope="feed_detail_without_replies")]
