import json

import httpx
import pytest

from apptopics.sources.astro import AstroClient, AstroError


def make_client(handler):
    c = AstroClient(url="http://astro.test/mcp")
    c.limiter.interval = 0
    c._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    return c


def test_paging_and_session_header():
    pages = {1: [{"text": "A", "popularity": 90, "genre": "6005"}], 2: [{"text": "B", "popularity": 80, "genre": "6013"}], 3: []}
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(req.headers.get("Mcp-Session-Id"))
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "astro"}}}, headers={"Mcp-Session-Id": "S1"})
        p = body["params"]["arguments"]["page"]
        assert body["params"]["arguments"]["store"] == "us"
        text = json.dumps({"keywords": pages[p], "page": p, "totalPages": 2})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": text}], "isError": False}})

    c = make_client(handler)
    out = list(c.iter_most_searched("US"))
    assert [k["text"] for _, k in out] == ["A", "B"] and [r for r, _ in out] == [1, 2]
    assert seen[0] is None and all(s == "S1" for s in seen[1:])


def test_tool_error_raises():
    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "Error: Unsupported store 'XX'"}], "isError": True}})
    with pytest.raises(AstroError):
        make_client(handler).search("x", "xx")


def test_busy_retry(monkeypatch):
    import apptopics.sources.astro as mod
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": '{"error":"Server is temporarily busy (rate limit: 30 requests/min)."}'}], "isError": True}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": '{"apps": []}'}], "isError": False}})
    assert make_client(handler).search("x", "us") == {"apps": []} and calls["n"] == 2
