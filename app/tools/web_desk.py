"""Nimble web desk: a team of web specialists, each built on a different Nimble capability.

Verdict phase (feeds the fact-check, hard-capped at VERDICT_BUDGET_S):
  primary   POST /v2/search depth=standard    the company's own release and prepared remarks on the IR site and
                                              SEC; the guidance table (metric | current | previous) ranks first
  broad     POST /v2/search x2                the model's query plus a fixed recall query, merged
  serp      POST /v2/fast-serp                what Google shows right now (p50 < 700 ms); /v2/serp on a 5xx
  sitemap   POST /v2/map                      maps the IR site once per process; lists the Q3 documents

Enrich phase (runs after the verdict and fills in the card; never gates it):
  verify    POST /v2/agents/runs effort=low   an autonomous Web Search Agent re-checks the claim from the document
                                              `primary` found and returns a trust report (confidence, cited claims)
  wall      GET  /v2/domain-knowledge/driver  scans the news sites the desk found for antibot systems, then reads the
            + POST /v2/extract driver=...     protected article with the driver Nimble recommends
  social    POST /v2/extract/templates/run    Reddit threads on the guidance (pre-built reddit_answers template)
  newswire  POST /v2/search focus=news        wires, analyst and market reaction since the release

Mock mode replays app/mocks/web_desk.json (labelled MOCK) so the desk works without a key.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

import httpx
from functools import lru_cache
from pathlib import Path
from typing import Awaitable, Callable

from .. import config, observability
from ..claims import parse_range
from ..models import TrustClaim, WebDesk, WebItem, WebStep, WebVerify
from . import nimble
from .live_web import IR_DOMAINS, _passages, guidance_table, interleave, ir_query, mock_search_live_web, recall_query

MOCK_FILE = Path(__file__).resolve().parent.parent / "mocks" / "web_desk.json"
VERDICT_BUDGET_S = 6.5  # the tool's own timeout is 8 s; return what landed by then
ENRICH_BUDGET_S = 45.0


def _info() -> config.CallInfo:
    return config.call_info()  # the active call (set per run)


def _call_date() -> str:
    return str(_info().session_day)  # search filters: coverage from the call day onward


def _ir_domains() -> list[str]:
    return list(_info().ir_domains) or IR_DOMAINS


def _ir_site() -> str:
    """The investor-relations host to map: an 'investor(s).' or 'ir.' domain if the call lists one."""
    doms = _ir_domains()
    return "https://" + next((d for d in doms if d.startswith(("investor", "ir."))), doms[0])


def _spoken_date(d) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _prior_report_date(ticker: str) -> str:
    """Date of the last guidance before the call, from the guidance history (e.g. 'July 9, 2026'); '' if unknown."""
    try:
        from datetime import date

        from .guidance_history import _csv_rows
        df = _csv_rows()
        last = df[df["TICKER"] == ticker]["AS_OF_DATE"].max()
        return _spoken_date(date.fromisoformat(last)) if isinstance(last, str) and last else ""
    except Exception:  # noqa: BLE001 - a prompt detail, never a failure
        return ""
WIRE_DOMAINS = ["prnewswire.com", "businesswire.com", "reuters.com", "cnbc.com", "wsj.com", "bloomberg.com",
                "marketwatch.com", "barrons.com", "ft.com", "apnews.com"]
# Transcripts of the call itself would make the check circular.
TRANSCRIPT_HOSTS = ["fool.com", "seekingalpha.com", "investing.com", "gurufocus.com", "marketbeat.com",
                    "finance.yahoo.com", "insidermonkey.com", "rev.com", "quartr.com", "roic.ai"]
VIDEO_HOSTS = ["youtube.com", "m.youtube.com", "tiktok.com", "vimeo.com"]
HARD_DOMAINS = ["reuters.com", "wsj.com", "bloomberg.com", "barrons.com", "marketwatch.com", "cnbc.com", "ft.com",
                "investors.com", "foxbusiness.com"]

STEP_META = {
    "primary": ("Primary-source reader", "POST /v2/search · depth=standard · IR + SEC", "verdict"),
    "broad": ("Broad web search", "POST /v2/search · 2 queries", "verdict"),
    "newswire": ("Newswire & street", "POST /v2/search · focus=news", "enrich"),
    "serp": ("Google right now", "POST /v2/fast-serp · google_search", "verdict"),
    "sitemap": ("IR site mapper", "POST /v2/map · investor relations site", "verdict"),
    "verify": ("Verification agent", "POST /v2/agents/runs · effort=low", "enrich"),
    "wall": ("Wall-breaker", "GET /v2/domain-knowledge → POST /v2/extract driver=…", "enrich"),
    "social": ("Reddit pulse", "POST /v2/extract/templates/run · reddit_answers", "enrich"),
}


def _step(sid: str, feeds: list[str] | None = None, mock: bool = False) -> WebStep:
    label, endpoint, phase = STEP_META[sid]
    return WebStep(id=sid, label=label, endpoint=endpoint, phase=phase, feeds=feeds or [], mock=mock)


async def _timed(step: WebStep, coro: Awaitable, on_change: Callable[[], None] | None = None):
    """Run one specialist inside a Sentry tool span; record status, timing and failure on its step."""
    started = time.monotonic()
    with observability.span("gen_ai.execute_tool", f"execute_tool nimble.{step.id}",
                            {"gen_ai.tool.name": f"nimble.{step.id}", "gen_ai.tool.type": "function",
                             "gen_ai.agent.name": "call-desk-web-desk", "calldesk.endpoint": step.endpoint}) as span:
        try:
            out = await coro
            if step.status == "running":  # a specialist that marked itself skipped keeps that status
                step.status = "ok"
            span.set_status("ok")
            return out
        except asyncio.CancelledError:
            step.status, step.summary = "skipped", step.summary or "dropped: over the time budget"
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException):  # a slow source is dropped, not an error in our code
            step.status, step.summary = "skipped", f"dropped: no answer within {time.monotonic() - started:.0f} s"
            span.set_status("deadline_exceeded")
            return None
        except Exception as exc:  # noqa: BLE001 - a failed specialist is shown, never fatal
            step.status, step.summary = "failed", f"{type(exc).__name__}: {exc}"[:160]
            span.set_status("internal_error")
            return None
        finally:
            step.ms = int((time.monotonic() - started) * 1000)
            if on_change:
                on_change()


async def _run_bounded(tasks: list[asyncio.Task], budget_s: float) -> None:
    """Wait up to budget_s, then cancel and drain whatever is left.

    The finally block also runs when this coroutine itself is cancelled (a stopped run), so no specialist outlives
    its fact-check and publishes into the next run.
    """
    try:
        await asyncio.wait(tasks, timeout=budget_s)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _about_this_call(hit: dict, company: str, ticker: str) -> bool:
    """The wall-breaker opens only coverage of this call: it names the company and is not dated before the call day."""
    text = f"{hit.get('title', '')} {hit.get('snippet', '')}"
    name = company.split()[0]
    if name.lower() not in text.lower() and not re.search(rf"\b{re.escape(ticker)}\b", text):
        return False
    from datetime import date

    call_day = _info().session_day
    published = str(hit.get("published") or "")[:10]
    try:
        if published and date.fromisoformat(published) < call_day:
            return False
    except ValueError:
        pass
    m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}), (20\d\d)\b", text)
    if m:
        try:
            if date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))) < call_day:
                return False
        except ValueError:
            pass
    return True


def _on(domain: str, hosts: list[str]) -> bool:
    """True when domain is one of hosts or a subdomain of one (investor.pepsico.com is pepsico.com)."""
    return any(domain == h or domain.endswith("." + h) for h in hosts)


# ------------------------------------------------------------------ IR site map (cached per process)
_ir_links_by_call: dict[str, list[dict]] = {}
_ir_tasks: dict[str, asyncio.Task] = {}


def _release_urls(links: list[dict]) -> list[str]:
    """Q3 2026 release and prepared remarks from the IR site map, best first."""
    keys = ("q3-2026", "q3_2026", "third-quarter-2026", "2026-q3")
    hits = [x["url"] for x in links if any(k in x["url"].lower() for k in keys)]
    return sorted(hits, key=lambda u: (0 if "earnings-release" in u else 1 if "prepared" in u else 2, len(u)))


async def _map_ir(call_id: str, site: str) -> list[dict]:
    async with nimble.client(timeout=20.0) as http:
        _ir_links_by_call[call_id] = await nimble.site_map(http, site, limit=1000)
    return _ir_links_by_call[call_id]


# ------------------------------------------------------------------ verdict phase
async def live_verdict(query: str, metric_label: str = "", new_value: str | None = None, company: str = "PepsiCo") -> dict:
    """Five specialists in parallel; whatever lands inside VERDICT_BUDGET_S feeds the verdict, the rest is dropped."""
    call_id = _info().call_id
    steps = {sid: _step(sid) for sid in ("primary", "broad", "serp", "sitemap")}
    hits: dict[str, list[dict]] = {}

    ir_task = _ir_tasks.get(call_id)
    stale = ir_task is None or ir_task.get_loop() is not asyncio.get_running_loop() or (
        ir_task.done() and (ir_task.cancelled() or ir_task.exception() is not None))
    if call_id not in _ir_links_by_call and stale:
        # cached for every later flag of this call; shared, so not cancelled with this one
        ir_task = _ir_tasks[call_id] = asyncio.create_task(_map_ir(call_id, _ir_site()))

    async with nimble.client(timeout=5.0) as http:
        async def primary():
            r = await nimble.search(http, ir_query(company, metric_label), depth="standard", max_results=4,
                                    include_domains=_ir_domains(), start_date=_call_date())
            rows = []
            for h in r:
                table = guidance_table(h["snippet"])
                if table:
                    rows.append({**h, "snippet": f"Fiscal 2026 guidance table (metric | current | previous): {table}", "primary": True})
            hits["primary"] = rows[:2]
            n_rows = len(rows[0]["snippet"].split("; ")) if rows else 0
            steps["primary"].summary = (f"guidance table read from {rows[0]['source']} ({n_rows} lines, current vs previous)" if rows
                                        else f"{len(r)} company documents, no guidance table")

        async def broad():
            batches = await asyncio.gather(
                nimble.search(http, query, max_results=5),
                nimble.search(http, recall_query(company, metric_label), max_results=5), return_exceptions=True)
            ok = [b for b in batches if not isinstance(b, BaseException)]
            if not ok:
                raise batches[0]
            hits["broad"] = interleave(ok, 8)
            steps["broad"].summary = f"{len(hits['broad'])} results from 2 queries (model's + recall)"

        async def serp():
            q = f"{company} {metric_label} guidance {new_value or ''}".strip()
            try:
                r, how = await nimble.fast_serp(http, q, n=6), "fast"
            except nimble.NimbleError as exc:  # Nimble-side 5xx: fall back to the standard SERP endpoint; the budget still caps it
                if "-> 5" not in str(exc):
                    raise
                r, how = await nimble.serp(http, q, n=6), "standard SERP fallback"
            hits["serp"] = r
            steps["serp"].summary = f"top {len(r)} Google results" + (f" · #1 {r[0]['source']}" if r else "") + (f" · {how}" if how != "fast" else "")

        async def sitemap():
            if call_id not in _ir_links_by_call:
                try:
                    await asyncio.wait_for(asyncio.shield(ir_task), timeout=VERDICT_BUDGET_S - 1)
                except asyncio.TimeoutError:
                    steps["sitemap"].status, steps["sitemap"].summary = "skipped", "map still running; later flags use it"
                    return
                except nimble.NimbleError as exc:
                    if "-> 5" not in str(exc):
                        raise
                    steps["sitemap"].status, steps["sitemap"].summary = "skipped", "unavailable: Nimble map returned a server error"
                    return
            links = _ir_links_by_call.get(call_id) or []
            urls = _release_urls(links)
            hits["sitemap"] = [{"title": u.rstrip("/").rsplit("/", 1)[-1], "url": u, "source": nimble.domain(u),
                                "snippet": "Q3 2026 document found on the IR site map"} for u in urls[:2]]
            releases = sorted({m.group(1) for x in links if (m := re.search(r"q\d-(20\d\d)-earnings-release", x["url"].lower()))})
            archive = f"{sum(1 for x in links if 'earnings-release' in x['url'].lower())} earnings releases ({releases[0]}–{releases[-1]})" if releases else "no earnings releases"
            steps["sitemap"].summary = (f"{len(links)} IR pages mapped · {archive} · "
                                        + (f"{len(urls)} Q3 2026 documents" if urls else "Q3 2026 not in the site map yet; search found it"))

        fns = {"primary": primary, "broad": broad, "serp": serp, "sitemap": sitemap}
        tasks = [asyncio.create_task(_timed(steps[sid], fn())) for sid, fn in fns.items()]
        await _run_bounded(tasks, VERDICT_BUDGET_S)
        if steps["sitemap"].status == "running":
            steps["sitemap"].status, steps["sitemap"].summary = "skipped", "map still running; later flags use it"

    # The company's own guidance table first, then everything else round-robin, without transcripts or repeats.
    rest = interleave([hits.get("broad", []), hits.get("serp", []), hits.get("sitemap", [])], 10)
    merged, seen = [], set()
    for h in hits.get("primary", []) + rest:
        if h.get("url") and h["url"] not in seen and not _on(nimble.domain(h["url"]), TRANSCRIPT_HOSTS):
            seen.add(h["url"])
            merged.append(h)
    if not merged and all(s.status == "failed" for s in steps.values()):
        raise RuntimeError("every web specialist failed: " + "; ".join(s.summary for s in steps.values())[:300])
    primary_url = (hits.get("primary") or hits.get("sitemap") or [{}])[0].get("url", "")
    return {"hits": merged[:8], "steps": [s.model_dump() for s in steps.values()],
            "broad": hits.get("broad", []), "primary_url": primary_url, "mock": False}


async def mock_verdict(query: str, metric_label: str = "", new_value: str | None = None, company: str = "PepsiCo") -> dict:
    hits = await mock_search_live_web(query)
    m = _mock()
    steps = [_step(sid, mock=True) for sid in ("primary", "broad", "serp", "sitemap")]
    for st in steps:
        st.status, st.ms, st.summary = "ok", m["verdict_ms"].get(st.id), m["verdict_summary"].get(st.id, "")
    return {"hits": hits, "steps": [st.model_dump() for st in steps], "broad": [],
            "primary_url": m["primary_url"], "mock": True}


# ------------------------------------------------------------------ enrich phase
_agents_unbillable_until = 0.0  # set for 10 minutes after a 402 from /v2/agents/runs
VERIFY_CACHE = config.DATA / "cache" / "nimble_verify.json"  # one stored live result per company+metric; NIMBLE_VERIFY_FRESH=1 re-runs


def _verify_key(company: str, metric_label: str) -> str:
    return f"{company}|{metric_label}".lower()


def load_verify_cache() -> dict:
    try:
        return json.loads(VERIFY_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _store(key: str, entry: dict | None) -> None:
    """Write one cache entry (a stored result, or {"pending": {...}} for a billed run still working)."""
    cache = load_verify_cache()
    if entry is None:
        cache.pop(key, None)
    else:
        cache[key] = entry
    try:
        VERIFY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        VERIFY_CACHE.write_text(json.dumps(cache, indent=1))
    except OSError:
        pass


def _now_et() -> str:
    from datetime import datetime

    return datetime.now(config.ET).strftime("%b %d %H:%M ET")


def save_verify(company: str, metric_label: str, v: WebVerify) -> None:
    """Store every completed result, whatever its confidence: a paid run is never thrown away."""
    _store(_verify_key(company, metric_label), {**v.model_dump(), "cached_at": _now_et()})


async def _start_or_collect(key: str, pending: dict | None, company: str, ticker: str, metric_label: str, primary_url: str,
                            step: WebStep) -> WebVerify | None:
    """Collect an already-billed run, or start exactly one new run (never retried). Returns None when skipped."""
    global _agents_unbillable_until
    async with nimble.client(timeout=15.0) as agent_http:
        if pending:  # billed earlier: collect it, never start a second run for the same metric
            agent_id, run_id = pending["agent_id"], pending["run_id"]
        else:
            if time.monotonic() < _agents_unbillable_until:
                step.status, step.summary = "skipped", "unavailable: Nimble account cannot be billed for agent runs (402)"
                return None
            try:
                agent_id, run_id = await nimble.agent_start(agent_http, _verify_prompt(company, ticker, metric_label, primary_url),
                                                            output_schema=_verify_schema(), sources=_verify_sources(primary_url), effort="low")
            except nimble.NimbleError as exc:
                if "-> 402" not in str(exc):
                    raise
                _agents_unbillable_until = time.monotonic() + 600
                step.status, step.summary = "skipped", "unavailable: Nimble account cannot be billed for agent runs (402)"
                return None
            _store(key, {"pending": {"agent_id": agent_id, "run_id": run_id, "started": _now_et()}})
        try:
            out = await nimble.agent_wait(agent_http, agent_id, run_id, deadline_s=ENRICH_BUDGET_S - 3)
        except nimble.AgentPending:
            task = asyncio.get_running_loop().create_task(_finish_pending(agent_id, run_id, company, metric_label))
            _finishers.add(task)
            task.add_done_callback(_finishers.discard)
            since = pending["started"] if pending else _now_et()
            step.status, step.summary = "skipped", f"agent still working (run started {since}); its result will be stored"
            return None
        except nimble.NimbleError:
            _store(key, None)  # the run failed: a later flag may start a new one
            raise
    result = to_verify(out["output"], out["run_s"])
    save_verify(company, metric_label, result)
    return result


_finishers: set[asyncio.Task] = set()
_inflight: dict[str, asyncio.Future] = {}  # metric key -> the running agent run's result, shared by concurrent flags


async def _finish_pending(agent_id: str, run_id: str, company: str, metric_label: str) -> None:
    """Collect a run that missed the flag's deadline (status polls are free) and store its result for later flags."""
    try:
        async with nimble.client(timeout=15.0) as http:
            out = await nimble.agent_wait(http, agent_id, run_id, deadline_s=360)
        save_verify(company, metric_label, to_verify(out["output"], out["run_s"]))
    except nimble.AgentPending:
        pass  # still pending: the stored ids let the next flag collect it
    except Exception as exc:  # noqa: BLE001 - a failed run frees the slot; nothing else to do
        if not isinstance(exc, asyncio.CancelledError):
            _store(_verify_key(company, metric_label), None)


def _verify_schema() -> dict:
    s = {"type": "string"}
    when, before = _spoken_date(_info().session_day), _prior_report_date(_info().ticker)
    return {"type": "object", "properties": {
        "metric": s,
        "guidance_in_this_release": {"type": "string", "description": f"the fiscal 2026 range as written in the Q3 2026 release of {when}"},
        "guidance_before_this_release": {"type": "string", "description": "the range in effect just before"
                                                                          + (f", from the report of {before}" if before else "")},
        "change": {"type": "string", "description": "one word: cut, raise, reaffirm, or new"},
        "source_url": s,
    }, "required": ["metric", "guidance_in_this_release", "guidance_before_this_release", "change", "source_url"]}


def _verify_prompt(company: str, ticker: str, metric_label: str, primary_url: str) -> str:
    where = f" The company's own document is at {primary_url}." if primary_url else ""
    when, before = _spoken_date(_info().session_day), _prior_report_date(ticker)
    return (f"{company} ({ticker}) reported third-quarter 2026 results on {when} and updated its fiscal 2026 guidance. "
            f"From that {when} release, what is the fiscal 2026 {metric_label} guidance, and what was it before"
            + (f" ({before})?" if before else " (in its previous quarterly report)?")
            + f"{where} Report exactly this metric, not a similar one (core EPS and core constant-currency EPS are different lines). "
            f"Ignore releases from 2025 or earlier.")


def _verify_sources(primary_url: str) -> dict:
    # Nimble requires each group's `order` to match its position in the list.
    out = {"allow": [{"title": "Company investor relations and SEC filings", "domains": _ir_domains(), "order": 0},
                     {"title": "Wires and financial press", "domains": WIRE_DOMAINS, "order": 1}]}
    if primary_url:
        out["prioritize"] = (f"The company's Q3 2026 earnings release and prepared remarks dated {_spoken_date(_info().session_day)}, "
                             f"e.g. {primary_url}")
    return out


def to_verify(output: dict, run_s: float | None) -> WebVerify:
    trust = output.get("trust") or {}
    content = output.get("content")
    claims = []
    for c in trust.get("claims") or []:
        cites = [WebItem(title=x.get("title") or "", url=x.get("url") or "", source=nimble.domain(x.get("url") or ""),
                         snippet=" … ".join(x.get("excerpts") or [])[:300]) for x in (c.get("citations") or [])[:3]]
        name = str(c.get("path") or c.get("callout") or "").removeprefix("$.")
        claims.append(TrustClaim(claim=VERIFY_LABELS.get(name, name), confidence=c.get("confidence") or "", citations=cites))
    sources = trust.get("sources") or []
    items = [WebItem(title=x.get("title") or "", url=x.get("url") or "", source=nimble.domain(x.get("url") or ""),
                     snippet=f"{x.get('type') or ''} · {x.get('source_category') or ''}".strip(" ·")) for x in sources]
    official = sum(1 for x in sources if x.get("source_category") == "official" or x.get("type") == "primary")
    if isinstance(content, dict):
        answer = {VERIFY_LABELS.get(k, k): v for k, v in content.items() if k != "metric" and v}
    else:
        answer = {"answer": str(content or "")[:600]}
    return WebVerify(confidence=trust.get("confidence") or "", reasoning=(trust.get("reasoning") or "")[:400],
                     answer=answer, claims=[c for c in claims if c.claim != "metric"], sources=items[:6], official_sources=official, run_s=run_s)


VERIFY_LABELS = {"guidance_in_this_release": "now", "guidance_before_this_release": "before",
                 "guidance_in_oct_8_2026_release": "now", "guidance_before_oct_8_2026": "before", "change": "change", "source_url": "source"}


def _as_items(rows: list[dict], n: int = 4) -> list[WebItem]:
    return [WebItem(title=r.get("title", "")[:160], url=r.get("url", ""), source=r.get("source", ""),
                    snippet=(r.get("snippet") or "")[:280], published=r.get("published", "")) for r in rows[:n]]


def _reddit_items(parsed) -> list[WebItem]:
    posts = (parsed or {}).get("posts") if isinstance(parsed, dict) else None
    out = []
    for x in (posts or [])[:5]:
        sub = str(x.get("post_subreddit") or "").strip("/")
        title = x.get("post_title") or x.get("title") or ""
        out.append(WebItem(title=title or f"r/{sub.removeprefix('r/')}", url=x.get("post_link") or "", source="reddit.com",
                           snippet=f"r/{sub.removeprefix('r/')} · {x.get('upvotes', 0)} upvotes · {x.get('comments', 0)} comments"))
    return out


NO_VERIFY_METRICS = {"outlook_next_year", "other", "macro"}  # commentary, not a guided number
_UNIT_NUMBER = re.compile(r"(?i)(\$\s?\d|\d(\.\d+)?\s?(%|percent|pp|bps|basis points|points?\b|x\b|times\b|billion|million|cents?\b))")


def verifiable(metric: str, value: str | None) -> bool:
    """Worth a billed agent run: a guided number with a unit ("+2.5% to +3.5%", "$5.10 to $5.60", "2.2x").

    A bare year ("detailed expectations to 2027") or words never qualify, nor do commentary metrics.
    """
    if metric in NO_VERIFY_METRICS or not value:
        return False
    text = re.sub(r"\b(19|20)\d\d\b", " ", value)  # a year is not a guidance number
    return bool(_UNIT_NUMBER.search(text)) and parse_range(text) is not None


async def enrich(desk: WebDesk, *, company: str, ticker: str, metric_label: str, new_value: str | None, metric: str = "",
                 found: dict, mock: bool, on_change: Callable[[], None]) -> None:
    """Run the enrich specialists in parallel; each one updates `desk` and calls on_change as it lands.

    `found` is the verdict phase's output: the primary document URL feeds `verify`, its news and broad hits feed `wall`.
    """
    if mock:
        return await _mock_enrich(desk, metric_label=metric_label, new_value=new_value, metric=metric, on_change=on_change)
    steps = {sid: _step(sid) for sid in ("verify", "wall", "social", "newswire")}
    primary_url = found.get("primary_url") or ""
    steps["verify"].feeds = ["primary"] if primary_url else []
    steps["wall"].feeds = ["broad"]
    desk.steps += list(steps.values())
    on_change()

    async with nimble.client(timeout=15.0) as http:
        async def verify():
            if not verifiable(metric, new_value):  # the agent is reliable on numeric guidance lines only, and each run is billed
                steps["verify"].status, steps["verify"].summary = "skipped", "not a numeric guidance line; agent not run"
                return
            key = _verify_key(company, metric_label)
            joined = _inflight.get(key)
            if joined is not None and not joined.done():  # another flag is already paying for this metric's run: share it
                try:
                    desk.verify = WebVerify(**(await asyncio.shield(joined)))
                except Exception:  # noqa: BLE001 - that flag reports the failure; this one just has no result
                    steps["verify"].status, steps["verify"].summary = "skipped", "shared run did not finish in time"
                    return
                steps["verify"].summary = f"confidence {desk.verify.confidence or '?'} · shared the run started by an earlier flag"
                return
            entry = load_verify_cache().get(key)
            pending = (entry or {}).get("pending")
            fresh = (os.environ.get("NIMBLE_VERIFY_FRESH") or config.get("NIMBLE_VERIFY_FRESH") or "0") == "1"
            if entry and not pending and not fresh:
                desk.verify = WebVerify(**entry)
                v = desk.verify
                steps["verify"].summary = (f"confidence {v.confidence or '?'} · {v.official_sources} official sources · "
                                           f"cached result of the {v.cached_at} live run")
                return
            shared = asyncio.get_running_loop().create_future()  # registered before the billed call, so concurrent flags join it
            _inflight[key] = shared
            try:
                result = await _start_or_collect(key, pending, company, ticker, metric_label, primary_url, steps["verify"])
            except BaseException as exc:
                if not shared.done():
                    shared.set_exception(RuntimeError(f"{type(exc).__name__}"))
                    shared.exception()  # retrieved: nobody may be waiting
                raise
            finally:
                _inflight.pop(key, None)
            if not shared.done():
                if result is None:
                    shared.set_exception(RuntimeError("no result"))
                    shared.exception()
                else:
                    shared.set_result(result.model_dump())
            if result is not None:
                desk.verify = result
                v = desk.verify
                steps["verify"].summary = (f"confidence {v.confidence or '?'} · {v.official_sources} official sources · "
                                           f"{len(v.claims)} cited claims · {v.run_s:.0f}s")

        async def wall():
            candidates, seen = [], set()
            for h in found.get("broad", []):
                d = nimble.domain(h.get("url", ""))
                old = re.search(r"/(20(?:[01]\d|2[0-5]))/", h.get("url", ""))  # a dated path before 2026 is last year's story
                if (d and d not in seen and not _on(d, TRANSCRIPT_HOSTS + _ir_domains() + VIDEO_HOSTS) and not old
                        and _about_this_call(h, company, ticker)):
                    seen.add(d)
                    candidates.append(h)
            candidates.sort(key=lambda h: 0 if _on(nimble.domain(h["url"]), HARD_DOMAINS) else 1)
            candidates = candidates[:4]
            if not candidates:
                steps["wall"].status, steps["wall"].summary = "skipped", "no news article about this call to open"
                return
            knowledge = await asyncio.gather(*(nimble.domain_knowledge(http, nimble.domain(h["url"])) for h in candidates), return_exceptions=True)
            scanned = [(h, k) for h, k in zip(candidates, knowledge) if isinstance(k, dict)]
            if not scanned:
                raise knowledge[0]
            pick, dk = next(((h, k) for h, k in scanned if k.get("antibots")), scanned[0])
            driver, antibots = dk.get("driver") or "", dk.get("antibots") or []
            async with nimble.client(timeout=28.0) as wall_http:  # stealth drivers are slow under load; the enrich budget allows it
                page = await nimble.extract(wall_http, pick["url"], driver=driver or None, render=bool(dk.get("need_to_render")) or "auto",
                                            timeout_ms=25000)
            text = _passages(page["markdown"], f"{company} {metric_label} guidance outlook cut")
            desk.wall = {"domain": nimble.domain(pick["url"]), "url": pick["url"], "title": pick.get("title", ""), "antibots": antibots,
                         "driver": page["driver"] or driver, "snippet": text[:420],
                         "scanned": [{"domain": nimble.domain(h["url"]), "antibots": k.get("antibots") or []} for h, k in scanned]}
            protected = sum(1 for _, k in scanned if k.get("antibots"))
            steps["wall"].summary = (f"scanned {len(scanned)} site{'s' if len(scanned) != 1 else ''}, {protected} protected · {desk.wall['domain']}: "
                                     f"{', '.join(dict.fromkeys(antibots)) or 'no antibot'} → driver {desk.wall['driver']} · "
                                     f"{'read' if text else 'opened'}")

        async def social():
            parsed = await nimble.template_run(http_slow, "reddit_answers",
                                               {"prompt": f"What are investors saying about {company} ({ticker}) {metric_label.lower()} guidance after its Q3 2026 results?"})
            desk.social = _reddit_items(parsed)
            subs = sorted({x.snippet.split(" · ")[0] for x in desk.social})
            steps["social"].summary = f"{len(desk.social)} Reddit threads" + (f" · {', '.join(subs[:3])}" if subs else "")

        async def newswire():
            q = f"{company} {ticker} {metric_label} guidance analysts react"
            async with nimble.client(timeout=20.0) as news_http:  # focus=news runs subagents: slow under load
                try:
                    r = await nimble.search(news_http, q, focus="news", max_results=6, start_date=_call_date(), exclude_domains=TRANSCRIPT_HOSTS)
                except (nimble.NimbleError, httpx.TimeoutException) as exc:
                    if isinstance(exc, nimble.NimbleError) and "-> 5" not in str(exc):
                        raise
                    # focus=news often returns 500 "can't download the query response": fall back to plain search once.
                    try:
                        r = await nimble.search(news_http, f"{q} news", max_results=6, start_date=_call_date(), exclude_domains=TRANSCRIPT_HOSTS)
                    except (nimble.NimbleError, httpx.TimeoutException):
                        steps["newswire"].status, steps["newswire"].summary = "skipped", "unavailable: Nimble news search returned errors"
                        return
            desk.street = _as_items(r, 5)
            steps["newswire"].summary = f"{len(r)} stories since Oct 8" + (f" · {', '.join(sorted({x.source for x in desk.street})[:3])}" if r else "")

        async with nimble.client(timeout=ENRICH_BUDGET_S) as http_slow:
            tasks = [asyncio.create_task(_timed(steps[sid], fn(), on_change))
                     for sid, fn in (("verify", verify), ("wall", wall), ("social", social), ("newswire", newswire))]
            await _run_bounded(tasks, ENRICH_BUDGET_S)
    on_change()


# ------------------------------------------------------------------ mock
@lru_cache(maxsize=1)
def _mock() -> dict:
    return json.loads(MOCK_FILE.read_text())


async def _mock_enrich(desk: WebDesk, *, metric_label: str, new_value: str | None, metric: str = "",
                       on_change: Callable[[], None]) -> None:
    m = _mock()
    order = ("newswire", "wall", "social", "verify")
    steps = {sid: _step(sid, mock=True) for sid in order}
    steps["verify"].feeds, steps["wall"].feeds = ["primary"], ["broad"]
    desk.steps += list(steps.values())
    on_change()
    for sid in order:
        await asyncio.sleep(m["enrich_delay_s"].get(sid, 0.5))
        s = steps[sid]
        s.status, s.ms, s.summary = "ok", m["enrich_ms"].get(sid), m["enrich_summary"].get(sid, "")
        if sid == "newswire":
            desk.street = [WebItem(**x) for x in m["street"]]
        elif sid == "social":
            desk.social = [WebItem(**x) for x in m["social"]]
        elif sid == "wall":
            desk.wall = m["wall"]
        elif sid == "verify" and not verifiable(metric, new_value):  # same rule as live: numeric guidance lines only
            s.status, s.summary = "skipped", "not a numeric guidance line; agent not run"
        elif sid == "verify":
            v = m["verify"].get(metric_label) or m["verify"]["default"]
            desk.verify = WebVerify(**v)
            s.summary = (f"confidence {desk.verify.confidence} · {desk.verify.official_sources} official sources · "
                         f"{len(desk.verify.claims)} cited claims · {desk.verify.run_s:.0f}s")
        on_change()
