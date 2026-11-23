"""Exercise the real isolated Tieba worker with API-shaped, offline clients."""

import asyncio
import io
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from agent_reach.channels import _tieba_worker as worker


class Rows(list):
    def __init__(self, rows=(), *, err=None, thread=None, has_more=False, page=1):
        super().__init__(rows)
        self.err = err
        self.thread = thread
        self.has_more = has_more
        self.page = SimpleNamespace(current_page=page, total_page=4)


@pytest.fixture
def api(monkeypatch):
    calls = []
    responses = {}
    clients = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.closed = False
            clients.append(self)
            calls.append(("client", args, kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def _request(self, method, args, kwargs):
            calls.append((method, args, kwargs))
            result = responses[method]
            if isinstance(result, BaseException):
                raise result
            return result

        async def search_global(self, *args, **kwargs):
            return await self._request("global", args, kwargs)

        async def search_in_forum(self, *args, **kwargs):
            return await self._request("forum", args, kwargs)

        async def get_posts(self, *args, **kwargs):
            return await self._request("posts", args, kwargs)

        async def get_threads(self, *args, **kwargs):
            return await self._request("threads", args, kwargs)

    fake = ModuleType("aiotieba")
    fake.Client = Client
    enums = ModuleType("aiotieba.enums")
    enums.ThreadSortType = SimpleNamespace(HOT=1, CREATE=2, REPLY=3)
    monkeypatch.setitem(sys.modules, "aiotieba", fake)
    monkeypatch.setitem(sys.modules, "aiotieba.enums", enums)
    return SimpleNamespace(calls=calls, responses=responses, clients=clients)


def search_payload(**overrides):
    return {"action": "search", "query": "test", "forum": None, "page": 2, "limit": 3, **overrides}


def test_global_search_preserves_actual_upstream_fields_and_closes_client(api):
    api.responses["global"] = Rows([
        SimpleNamespace(tid=12, pid=34, title="Topic", content="Actual excerpt",
                        forum_name="Python", user=SimpleNamespace(show_name="Author"),
                        create_time=1700000000),
    ], has_more=True, page=2)
    result = asyncio.run(worker._execute(search_payload()))
    assert result["ok"] is True
    row = result["items"][0]
    assert (row["text"], row["forum"], row["author"]) == ("Actual excerpt", "Python", "Author")
    assert result["meta"]["page"] == 2
    assert result["meta"]["has_more"] is True
    assert api.calls[1] == ("global", ("test",), {"pn": 2, "rn": 3})
    assert api.calls[0] == ("client", (), {"proxy": True})
    assert api.clients[0].closed


def test_forum_search_keeps_the_forum_specific_response_shape(api):
    api.responses["forum"] = Rows([
        SimpleNamespace(tid=12, pid=34, ppid=30, title="Topic", text="A reply",
                        fname="Python", show_name="Poster", create_time=1700000000,
                        content_type=SimpleNamespace(value=3)),
    ])
    result = asyncio.run(worker._execute(search_payload(forum="Python")))
    assert result["ok"] is True
    assert result["items"][0]["text"] == "A reply"
    assert result["items"][0]["author"] == "Poster"
    assert result["meta"]["scope"] == "forum"
    assert api.calls[1] == ("forum", ("Python", "test"), {"pn": 2, "rn": 3})
    assert api.clients[0].closed


@pytest.mark.parametrize("message,code", [
    ("HTTP 429 too many requests", "RATE_LIMITED"),
    ("captcha required", "PLATFORM_BLOCKED"),
    ("login required", "NEED_LOGIN"),
])
def test_wrapped_api_error_is_not_successful_empty_search(api, message, code):
    api.responses["global"] = Rows(err=RuntimeError(message))
    result = asyncio.run(worker._execute(search_payload()))
    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert api.clients[0].closed


def test_client_closes_even_if_the_request_raises(api):
    api.responses["global"] = TimeoutError("upstream timed out")
    with pytest.raises(TimeoutError):
        asyncio.run(worker._execute(search_payload()))
    assert api.clients[0].closed


def test_worker_timeout_does_not_print_request_url(api, monkeypatch, capsys):
    api.responses["global"] = Rows(err=TimeoutError("timed out: https://example.test/request?sign=test"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(search_payload())))
    assert worker.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == {"code": "TIMEOUT", "message": "Tieba request timed out"}


@pytest.mark.parametrize("comment_limit", [0, 2])
def test_read_passes_page_and_bounded_comment_request_without_claiming_all(api, comment_limit):
    thread = SimpleNamespace(tid=12, title="Topic", text="Opening post")
    nested = [SimpleNamespace(pid=i, text=f"Reply {i}") for i in range(4)]
    api.responses["posts"] = Rows([
        SimpleNamespace(pid=34, floor=2, text="Floor body", reply_num=8, comments=nested),
    ], thread=thread, has_more=True, page=3)
    result = asyncio.run(worker._execute({
        "action": "read", "tid": 12, "page": 3, "limit": 20, "comment_limit": comment_limit,
    }))
    assert result["ok"] is True
    method, args, kwargs = api.calls[1]
    assert (method, args, kwargs["pn"], kwargs["rn"]) == ("posts", (12,), 3, 20)
    assert kwargs["with_comments"] is (comment_limit > 0)
    if comment_limit:
        assert kwargs["comment_rn"] == comment_limit
    else:
        assert "comment_rn" not in kwargs
    assert len(result["items"][0]["comments"]) == comment_limit
    assert result["items"][0]["reply_count"] == 8
    assert result["meta"]["page"] == 3
    assert result["meta"]["has_more"] is True
    assert result["meta"]["comment_limit_per_floor"] == comment_limit
    assert api.clients[0].closed


def test_thread_without_replies_remains_a_successful_known_thread(api):
    api.responses["posts"] = Rows(thread=SimpleNamespace(tid=12, title="Empty topic", text="Body"))
    result = asyncio.run(worker._execute({
        "action": "read", "tid": 12, "page": 1, "limit": 20, "comment_limit": 0,
    }))
    assert result["ok"] is True
    assert result["thread"]["tid"] == 12
    assert result["items"] == []
    assert api.clients[0].closed


def test_worker_runs_with_only_stdlib_and_serializes_missing_dependency(tmp_path):
    # -S excludes all site packages and -I ignores caller PYTHONPATH. A direct
    # script must reach its own JSON boundary without importing channel registry.
    script = Path(worker.__file__).resolve()
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(script)],
        input=json.dumps(search_payload()), capture_output=True,
        encoding="utf-8", timeout=10, cwd=tmp_path,
    )
    assert proc.returncode == 1
    result = json.loads(proc.stdout)
    assert result["ok"] is False
    assert "aiotieba" in result["error"]["message"]
    assert "yaml" not in proc.stderr
    assert "Traceback" not in proc.stderr


def test_upstream_logging_does_not_corrupt_worker_json(api, monkeypatch, capsys):
    async def noisy_request(*args, **kwargs):
        print("upstream log")
        return Rows()

    monkeypatch.setattr(sys.modules["aiotieba"].Client, "search_global", noisy_request)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(search_payload())))
    assert worker.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["items"] == []
    assert "upstream log" in output.err


def test_author_in_message_is_not_a_login_error():
    assert worker._error(RuntimeError("bad author field"))["error"]["code"] == "UPSTREAM_BROKEN"
    assert worker._error(RuntimeError("authentication required"))["error"]["code"] == "NEED_LOGIN"


def test_certificate_failure_is_not_a_platform_block():
    exc = RuntimeError("SSLCertVerificationError: certificate verify failed: unable to get local issuer")
    assert worker._error(exc)["error"]["code"] == "UPSTREAM_BROKEN"
    assert worker._error(RuntimeError("captcha required"))["error"]["code"] == "PLATFORM_BLOCKED"


def test_timeout_budget_is_passed_to_the_client(api):
    class TimeoutConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    sys.modules["aiotieba"].TimeoutConfig = TimeoutConfig
    api.responses["global"] = Rows([])
    asyncio.run(worker._execute(search_payload(), 12.0))
    config = api.calls[0][2]["timeout"]
    assert config.kwargs == {"http_acquire_conn": 10.0, "http_connect": 10.0, "http_read": 12.0}


def test_timeout_is_retried_once_within_budget(api, monkeypatch):
    results = iter([{"ok": False, "error": {"code": "TIMEOUT", "message": "t"}}, {"ok": True, "items": []}])
    budgets = []

    clock = [100.0]

    async def fake_execute(payload, budget=None):
        budgets.append(budget)
        clock[0] += budget  # a timed-out attempt uses its whole budget
        return next(results)

    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "_execute", fake_execute)
    assert worker._run(search_payload(timeout=30))["ok"] is True
    assert len(budgets) == 2
    assert budgets[0] == pytest.approx(13.5)
    assert sum(budgets) == pytest.approx(27.0)  # never past timeout minus start-up margin


def test_non_timeout_errors_are_not_retried(monkeypatch):
    calls = []

    async def fake_execute(payload, budget=None):
        calls.append(budget)
        return {"ok": False, "error": {"code": "RATE_LIMITED", "message": "slow down"}}

    monkeypatch.setattr(worker, "_execute", fake_execute)
    assert worker._run(search_payload(timeout=30))["error"]["code"] == "RATE_LIMITED"
    assert len(calls) == 1


def test_https_requests_get_an_explicit_tls_context(monkeypatch):
    import ssl

    seen = {}

    class Base:
        def __init__(self, *args, ssl=True, **kwargs):
            seen["ssl"] = ssl

    aiohttp = ModuleType("aiohttp")
    aiohttp.ClientRequest = Base
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    worker._use_connector_tls()
    aiohttp.ClientRequest("GET", "https://example.test/", ssl=False)
    assert isinstance(seen["ssl"], ssl.SSLContext)
    assert seen["ssl"].verify_mode == ssl.CERT_NONE
    aiohttp.ClientRequest("GET", "https://example.test/", ssl=None)
    assert seen["ssl"] is None
