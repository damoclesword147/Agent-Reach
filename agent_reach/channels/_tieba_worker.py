"""Isolated aiotieba worker. Input/output are one bounded JSON object each."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from contextlib import redirect_stdout
from typing import Any

_TEXT_LIMIT = 12_000
# Whole words only: "author" must not be read as an auth failure.
_LOGIN_RE = re.compile(r"\b(?:login|log in|sign in|unauthori[sz]ed|auth(?:entication|orization)?)\b")
# "certificate verify failed" is a network/TLS problem, not a platform challenge.
_BLOCKED_RE = re.compile(r"captcha|(?<!certificate )verif(?:y|ication) (?:required|needed|code)|\bverify your\b")
# Seconds reserved for interpreter start-up and JSON exchange inside the caller's budget.
_STARTUP_MARGIN = 3.0
_MAX_ATTEMPTS = 2
_MIN_ATTEMPT = 5.0


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default) if obj is not None else default


def _user_name(user: Any) -> str:
    return str(_value(user, "show_name") or _value(user, "nick_name") or _value(user, "user_name") or "")


def _thread_row(thread: Any) -> dict:
    text = str(_value(thread, "text", "") or "")
    return {
        "title": str(_value(thread, "title", "") or "")[:500],
        "text": text[:_TEXT_LIMIT],
        "text_truncated": len(text) > _TEXT_LIMIT,
        "tid": int(_value(thread, "tid", 0) or 0),
        "forum": str(_value(thread, "fname", "") or ""),
        "author": _user_name(_value(thread, "user")),
        "created": int(_value(thread, "create_time", 0) or 0),
        "last_reply": int(_value(thread, "last_time", 0) or 0),
        "reply_count": int(_value(thread, "reply_num", 0) or 0),
        "agree": int(_value(thread, "agree", 0) or 0),
        "is_good": bool(_value(thread, "is_good", False)),
        "is_top": bool(_value(thread, "is_top", False)),
    }


def _search_row(row: Any) -> dict:
    tid = int(_value(row, "tid", 0) or 0)
    pid = int(_value(row, "pid", 0) or 0)
    text = str(_value(row, "text") or _value(row, "content", "") or "")
    return {
        "title": str(_value(row, "title", "") or "")[:500],
        "text": text[:_TEXT_LIMIT],
        "text_truncated": len(text) > _TEXT_LIMIT,
        "tid": tid,
        "pid": pid,
        "ppid": int(_value(row, "ppid", 0) or 0),
        "forum": str(_value(row, "fname") or _value(row, "forum_name", "") or ""),
        "author": str(_value(row, "show_name") or _user_name(_value(row, "user"))),
        "created": int(_value(row, "create_time", 0) or 0),
        "is_comment": int(_value(_value(row, "content_type"), "value", 0) or 0) == 3,
    }


def _post_row(row: Any, comment_limit: int) -> dict:
    comments = list(_value(row, "comments", []) or [])[:comment_limit]
    text = str(_value(row, "text", "") or "")
    return {
        "floor": int(_value(row, "floor", 0) or 0),
        "pid": int(_value(row, "pid", 0) or 0),
        "author": _user_name(_value(row, "user")),
        "text": text[:_TEXT_LIMIT],
        "text_truncated": len(text) > _TEXT_LIMIT,
        "created": int(_value(row, "create_time", 0) or 0),
        "agree": int(_value(row, "agree", 0) or 0),
        "reply_count": int(_value(row, "reply_num", 0) or 0),
        "is_thread_author": bool(_value(row, "is_thread_author", False)),
        "comments": [
            {
                "pid": int(_value(comment, "pid", 0) or 0),
                "author": _user_name(_value(comment, "user")),
                "text": str(_value(comment, "text", "") or "")[:4000],
                "text_truncated": len(str(_value(comment, "text", "") or "")) > 4000,
                "created": int(_value(comment, "create_time", 0) or 0),
            }
            for comment in comments
        ],
    }


def _error(exc: BaseException) -> dict:
    code = str(getattr(exc, "code", "") or "")
    message = re.sub(r"https?://[^\s'\"<>]+", "[URL]", str(getattr(exc, "msg", "") or exc), flags=re.I)[:400]
    lowered = f"{code} {message}".casefold()
    if isinstance(exc, ModuleNotFoundError) and exc.name == "aiotieba":
        code = "NEED_SETUP"
    elif isinstance(exc, TimeoutError):
        code = "TIMEOUT"
        message = "Tieba request timed out"
    elif re.search(r"rate[ _-]?limit|too many requests|\b429\b", lowered) or "频繁" in message or "限流" in message:
        code = "RATE_LIMITED"
    elif _BLOCKED_RE.search(lowered) or "验证码" in message or "安全验证" in message:
        code = "PLATFORM_BLOCKED"
    elif _LOGIN_RE.search(lowered) or "登录" in message:
        code = "NEED_LOGIN"
    elif "not found" in lowered or "不存在" in message:
        code = "NOT_FOUND"
    else:
        code = "UPSTREAM_BROKEN"
    return {"ok": False, "error": {"code": code, "message": message}}


def _result_error(result: Any) -> BaseException | None:
    err = _value(result, "err")
    return err if isinstance(err, BaseException) else None


def _total_budget(payload: dict) -> float | None:
    """Seconds this worker may spend on requests inside the caller's timeout."""
    try:
        total = float(payload.get("timeout") or 0)
    except (TypeError, ValueError):
        return None
    return max(_MIN_ATTEMPT, total - _STARTUP_MARGIN) if total > 0 else None


def _tls_context() -> Any:
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Same trust policy aiotieba uses (it never verifies), but as an explicit context.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _use_connector_tls() -> None:
    """Give aiotieba's HTTPS requests an explicit TLS context instead of ``ssl=False``.

    aiotieba builds each request with ``ssl=False``. With current aiohttp the HTTPS endpoints
    (global search, thread posts) then hang during connect on some networks while the HTTP
    ones keep working. An explicit unverified context connects normally. Called only from
    ``main``: this worker is a separate process, so the patch never reaches Agent Reach.
    """
    try:
        import aiohttp
    except ImportError:
        return
    original = aiohttp.ClientRequest
    if getattr(original, "_reach_connector_tls", False):
        return
    context = _tls_context()

    class _ConnectorTLSRequest(original):  # type: ignore[misc, valid-type]
        _reach_connector_tls = True

        def __init__(self, *args: Any, ssl: Any = True, **kwargs: Any) -> None:
            super().__init__(*args, ssl=context if ssl is False else ssl, **kwargs)

    aiohttp.ClientRequest = _ConnectorTLSRequest


def _client_options(tb: Any, budget: float | None) -> dict:
    options: dict = {"proxy": True}
    config = getattr(tb, "TimeoutConfig", None)
    if budget and config is not None:
        # aiotieba defaults to 3s connect / 4s pool acquire / 12s read.
        connect = min(10.0, budget)
        options["timeout"] = config(http_acquire_conn=connect, http_connect=connect, http_read=budget)
    return options


async def _execute(payload: dict, budget: float | None = None) -> dict:
    import aiotieba as tb
    from aiotieba.enums import ThreadSortType

    action = payload.get("action")
    async with tb.Client(**_client_options(tb, budget)) as client:
        if action == "search":
            query = str(payload["query"])
            forum = payload.get("forum")
            page = int(payload["page"])
            limit = int(payload["limit"])
            if forum:
                result = await client.search_in_forum(str(forum), query, pn=page, rn=limit)
            else:
                result = await client.search_global(query, pn=page, rn=limit)
            if error := _result_error(result):
                return _error(error)
            return {
                "ok": True,
                "items": [_search_row(row) for row in list(result)[:limit]],
                "meta": {"page": page, "has_more": bool(_value(result, "has_more", False)),
                         "total_pages": int(_value(_value(result, "page"), "total_page", 0) or 0),
                         "scope": "forum" if forum else "global"},
            }

        if action == "threads":
            page = int(payload["page"])
            sort = {
                "hot": ThreadSortType.HOT,
                "create": ThreadSortType.CREATE,
                "reply": ThreadSortType.REPLY,
            }[str(payload["sort"])]
            result = await client.get_threads(str(payload["forum"]), pn=page, rn=int(payload["limit"]), sort=sort)
            if error := _result_error(result):
                return _error(error)
            return {
                "ok": True,
                "items": [_thread_row(row) for row in list(result)[:int(payload["limit"])]],
                "meta": {"page": page, "has_more": bool(_value(result, "has_more", False)),
                         "total_pages": int(_value(_value(result, "page"), "total_page", 0) or 0)},
            }

        if action == "read":
            page = int(payload["page"])
            comment_limit = int(payload["comment_limit"])
            comment_options = {"comment_rn": comment_limit} if comment_limit else {}
            result = await client.get_posts(
                int(payload["tid"]), pn=page, rn=int(payload["limit"]),
                with_comments=comment_limit > 0,
                **comment_options,
            )
            if error := _result_error(result):
                return _error(error)
            thread = _value(result, "thread")
            if not thread or not int(_value(thread, "tid", 0) or 0):
                return {"ok": True, "items": [], "meta": {"page": page, "has_more": False}}
            return {
                "ok": True,
                "thread": _thread_row(thread),
                "items": [_post_row(row, comment_limit) for row in list(result)[:int(payload["limit"])]],
                "meta": {"page": page, "has_more": bool(_value(result, "has_more", False)),
                         "total_pages": int(_value(_value(result, "page"), "total_page", 0) or 0),
                         "comment_limit_per_floor": comment_limit},
            }
        return {"ok": False, "error": {"code": "BAD_INPUT", "message": "unknown action"}}


def _run(payload: dict) -> dict:
    """Run one action; retry a timeout once if enough of the caller's budget remains."""
    total = _total_budget(payload)
    deadline = time.monotonic() + total if total else None
    result: dict = {}
    for attempt in range(_MAX_ATTEMPTS):
        budget = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if attempt and remaining < _MIN_ATTEMPT:
                break
            # First attempt keeps room for a retry; the retry uses whatever is left.
            budget = remaining / (_MAX_ATTEMPTS - attempt) if attempt == 0 else remaining
            budget = max(_MIN_ATTEMPT, budget)
        try:
            # aiotieba creates a logging handler on stdout during requests.
            with redirect_stdout(sys.stderr):
                result = asyncio.run(_execute(payload, budget))
        except Exception as exc:
            result = _error(exc)
        if result.get("ok") or (result.get("error") or {}).get("code") != "TIMEOUT":
            break
    return result


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        _use_connector_tls()
        result = _run(payload)
    except Exception as exc:
        result = _error(exc)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
