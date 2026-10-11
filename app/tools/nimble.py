"""Thin async client for the Nimble web-data API (https://docs.nimbleway.com).

One function per Nimble capability the web desk uses. Shapes follow the v2
OpenAPI spec (see docs/sdk_notes.md, section 2). Every function raises on a
non-success response; callers time-box and turn failures into skipped steps.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .. import config

BASE = "https://sdk.nimbleway.com"


class NimbleError(RuntimeError):
    pass


def client(timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE,
        headers={"Authorization": f"Bearer {config.get('NIMBLE_API_KEY')}", "Content-Type": "application/json"},
        timeout=httpx.Timeout(timeout, connect=5.0),
    )


def _check(r: httpx.Response) -> Any:
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = r.text[:200]
        # 402 = quota or trial finished; 429 has two documented body shapes
        raise NimbleError(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {str(body)[:200]}")
    return r.json()


async def search(http: httpx.AsyncClient, query: str, *, focus: str | list[str] = "general", depth: str = "lite",
                 max_results: int = 5, time_range: str | None = None, start_date: str | None = None,
                 end_date: str | None = None, include_domains: list[str] | None = None,
                 exclude_domains: list[str] | None = None) -> list[dict]:
    """POST /v2/search. Focus modes other than general only work with depth=lite."""
    body: dict[str, Any] = {"query": query, "max_results": max_results, "search_depth": depth, "focus": focus,
                            "country": "US", "locale": "en"}
    if time_range:
        body["time_range"] = time_range
    if start_date:
        body["start_date"] = start_date
    if end_date:
        body["end_date"] = end_date
    if include_domains:
        body["include_domains"] = include_domains
    if exclude_domains:
        body["exclude_domains"] = exclude_domains
    data = _check(await http.post("/v2/search", json=body))
    out = []
    for x in data.get("results") or []:
        url = x.get("url") or ""
        extra = x.get("additional_data") or {}
        out.append({
            "title": x.get("title") or "",
            "url": url,
            "source": domain(url),
            "snippet": (x.get("content") or x.get("description") or "").strip(),
            "published": str(extra.get("publish_date") or extra.get("published_at") or extra.get("date") or ""),
            "agent": (x.get("metadata") or {}).get("agent_name") or "",
        })
    return out


async def fast_serp(http: httpx.AsyncClient, query: str, *, engine: str = "google_search", n: int = 8) -> list[dict]:
    """POST /v2/fast-serp: realtime Google results (p50 under 700 ms)."""
    data = _check(await http.post("/v2/fast-serp", json={"search_engine": engine, "query": query, "num_results": n, "parse": True}))
    return _organic(data, n)


async def serp(http: httpx.AsyncClient, query: str, *, engine: str = "google_search", n: int = 8) -> list[dict]:
    """POST /v2/serp: the standard (non-realtime) SERP endpoint; fallback when fast-serp fails."""
    data = _check(await http.post("/v2/serp", json={"search_engine": engine, "query": query, "num_results": n, "parse": True}))
    return _organic(data, n)


def _organic(data: dict, n: int) -> list[dict]:
    parsing = data.get("parsing") or (data.get("data") or {}).get("parsing") or {}
    entities = parsing.get("entities") or {}
    return [
        {"title": o.get("title") or "", "url": o.get("url") or "", "source": o.get("cleaned_domain") or domain(o.get("url") or ""),
         "snippet": o.get("snippet") or "", "position": o.get("position")}
        for o in (entities.get("OrganicResult") or [])[:n]
    ]


async def extract(http: httpx.AsyncClient, url: str, *, render: bool | str = "auto", driver: str | None = None,
                  timeout_ms: int = 6000) -> dict:
    """POST /v2/extract: the page as main-content markdown. Returns {markdown, driver, ms, status_code}."""
    body: dict[str, Any] = {"url": url, "formats": ["markdown"], "markdown_backend": "main_content", "render": render,
                            "request_timeout": timeout_ms}
    if driver:
        body["driver"] = driver
    data = _check(await http.post("/v2/extract", json=body))
    if data.get("status") != "success":
        raise NimbleError(f"extract {data.get('status')} ({data.get('status_code')}) for {domain(url)}")
    meta = data.get("metadata") or {}
    return {"markdown": (data.get("data") or {}).get("markdown") or "", "driver": meta.get("driver") or driver or "",
            "ms": meta.get("query_duration"), "status_code": data.get("status_code")}


async def site_map(http: httpx.AsyncClient, url: str, *, limit: int = 400, sitemap: str = "include") -> list[dict]:
    """POST /v2/map: discover a site's URLs."""
    data = _check(await http.post("/v2/map", json={"url": url, "limit": limit, "sitemap": sitemap, "domain_filter": "domain"}))
    return [{"url": x.get("url") or "", "title": x.get("title") or ""} for x in data.get("links") or []]


async def domain_knowledge(http: httpx.AsyncClient, url: str) -> dict:
    """GET /v2/domain-knowledge/driver: detected antibot systems and the recommended driver."""
    return _check(await http.get("/v2/domain-knowledge/driver", params={"url": url}))


async def domain_health(http: httpx.AsyncClient, domains: list[str]) -> list[dict]:
    """POST /v1/domain-health/check: live success rate per domain across Nimble's traffic."""
    data = _check(await http.post("/v1/domain-health/check", json={"domains": domains}))
    return [{"domain": d.get("domain"), "status": d.get("status"), "success_rate": d.get("success_rate")} for d in data.get("domains") or []]


async def template_run(http: httpx.AsyncClient, template: str, params: dict) -> Any:
    """POST /v2/extract/templates/run: a pre-built extractor's structured records (data.parsing)."""
    data = _check(await http.post("/v2/extract/templates/run", json={"template": template, "params": params}))
    return (data.get("data") or {}).get("parsing")


class AgentPending(NimbleError):
    """The run is still working at the deadline. It is already billed: keep its ids and collect the result later."""

    def __init__(self, agent_id: str, run_id: str, status: str) -> None:
        super().__init__(f"agent run still {status}")
        self.agent_id, self.run_id = agent_id, run_id


async def agent_start(http: httpx.AsyncClient, prompt: str, *, output_schema: dict | None = None, sources: dict | None = None,
                      effort: str = "low") -> tuple[str, str]:
    """POST /v2/agents/runs: the one billed call (per task). Never retried or hedged. Returns (agent_id, run_id)."""
    body: dict[str, Any] = {"input": prompt, "effort": effort}
    if output_schema:
        body["output_schema"] = output_schema
    if sources:
        body["sources"] = sources
    run = _check(await http.post("/v2/agents/runs", json=body))
    agent_id, run_id = run.get("web_search_agent_id"), run.get("id")
    if not (agent_id and run_id):
        raise NimbleError(f"agent run not created: {str(run)[:200]}")
    return agent_id, run_id


async def agent_wait(http: httpx.AsyncClient, agent_id: str, run_id: str, *, deadline_s: float = 40.0, poll_s: float = 2.0) -> dict:
    """Poll a started run (free status calls) and GET its result. Raises AgentPending at the deadline.

    Returns {"output": {type, content, trust}, "run_s": seconds spent waiting here}.
    """
    started = time.monotonic()
    path = f"/v2/agents/{agent_id}/runs/{run_id}"
    while True:
        run = _check(await http.get(path))
        if not run.get("is_active", True):
            break
        if time.monotonic() - started > deadline_s:
            raise AgentPending(agent_id, run_id, str(run.get("status")))
        await asyncio.sleep(poll_s)
    if run.get("status") != "completed":
        raise NimbleError(f"agent run {run.get('status')}: {str(run.get('error'))[:200]}")
    result = _check(await http.get(f"{path}/result"))
    return {"output": result.get("output") or {}, "run_s": round(time.monotonic() - started, 1)}


def domain(url: str) -> str:
    try:
        host = httpx.URL(url).host
    except Exception:  # noqa: BLE001 - malformed URL from a result
        return ""
    return host.removeprefix("www.")
