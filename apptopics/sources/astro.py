"""Client for the Astro ASO MCP server (streamable HTTP JSON-RPC).

Quirks learned from probing: store codes must be lowercase ('us'); tool results
arrive as JSON text inside result.content[0].text; errors come back as
isError=true with an 'Error: ...' text rather than a JSON-RPC error.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import settings
from ..registry import sources
from .base import RateLimiter

log = logging.getLogger("apptopics.astro")


class AstroError(RuntimeError):
    pass


@sources.register("astro")
@dataclass
class AstroClient:
    url: str = field(default_factory=lambda: settings.astro_mcp_url)
    timeout: float = 120.0
    # The server enforces 30 requests/min and answers "temporarily busy" beyond that.
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(26))
    max_retries: int = 5
    _session_id: str | None = None
    _id: int = 0
    _client: httpx.Client | None = None

    # -- transport -----------------------------------------------------------
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._id += 1
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        r = self._http().post(self.url, json=body, headers=headers)
        r.raise_for_status()
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        data = r.json()
        if "error" in data:
            raise AstroError(f"{method}: {data['error']}")
        return data.get("result")

    def connect(self) -> dict:
        info = self._rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "apptopics", "version": "0.1"},
        })
        return info.get("serverInfo", {})

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def call(self, tool: str, **arguments: Any) -> Any:
        if self._session_id is None:
            self.connect()
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            res = self._rpc("tools/call", {"name": tool, "arguments": arguments})
            text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            if res.get("isError"):
                if "temporarily busy" in text and attempt < self.max_retries:
                    wait = 20 * (attempt + 1)
                    log.warning("astro busy (%s); retry %d in %ds", tool, attempt + 1, wait)
                    time.sleep(wait)
                    continue
                raise AstroError(f"{tool}({arguments}): {text[:300]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        raise AstroError(f"{tool}: gave up after {self.max_retries} retries")

    def list_tools(self) -> list[dict]:
        if self._session_id is None:
            self.connect()
        return self._rpc("tools/list").get("tools", [])

    # -- typed helpers --------------------------------------------------------
    def most_searched_page(self, store: str, page: int = 1, limit: int = 100, category: str | None = None) -> dict:
        args: dict[str, Any] = {"store": store.lower(), "page": page, "limit": limit}
        if category:
            args["category"] = category
        return self.call("get_most_searched_keywords", **args)

    def iter_most_searched(self, store: str, limit: int = 100, max_pages: int | None = None):
        """Yield (rank, keyword_dict) across all pages. Stops on empty page."""
        page, rank = 1, 0
        total_pages = None
        while True:
            data = self.most_searched_page(store, page=page, limit=limit)
            kws = data.get("keywords", [])
            total_pages = data.get("totalPages", total_pages)
            if not kws:
                return
            for k in kws:
                rank += 1
                yield rank, k
            page += 1
            if (max_pages and page > max_pages) or (total_pages and page > total_pages):
                return

    def search(self, keyword: str, store: str, limit: int = 50, platform: str = "iphone") -> dict:
        return self.call("search_app_store", keyword=keyword, store=store.lower(), limit=limit, platform=platform)

    def list_apps(self) -> list[dict]:
        return self.call("list_apps")

    def ensure_placeholder_app(self, name: str, platform: str = "iphone", store: str = "us") -> dict:
        """Find or create a temporary placeholder app used to track research keywords
        (this is how Astro exposes difficulty for arbitrary keywords)."""
        for a in self.list_apps():
            if a.get("name") == name:
                return a
        return self.call("add_app", temporary=True, name=name, platform=platform, store=store.lower())

    def add_keywords(self, app_name: str, keywords: list[str], store: str = "us") -> Any:
        return self.call("add_keywords", appName=app_name, keywords=keywords[:100], store=store.lower())

    def app_keywords(self, app_name: str | None = None, app_id: str | None = None, platform: str | None = None) -> dict:
        args: dict[str, Any] = {}
        if app_id:
            args["appId"] = app_id
        elif app_name:
            args["appName"] = app_name
        if platform:
            args["platform"] = platform
        return self.call("get_app_keywords", **args)
