"""Morning note: the current run's flags as a Markdown note an IR or FP&A team can paste and send."""

from __future__ import annotations

from datetime import datetime

from . import config

_RANK = {"cut": 0, "raise": 0, "reaffirm": 1, "new": 2, "n/a": 3}
# Same plain words as the screen (static/app.js BADGE), for readers outside finance.
_LABEL = {"cut": "LOWERED", "raise": "RAISED", "reaffirm": "UNCHANGED", "new": "NEW", "n/a": "NOTE"}
_DIRECTION_WORD = {"cut": "lowered", "raise": "raised", "reaffirm": "unchanged", "new": "new", "n/a": "note"}


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def _pp(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}pp"


def _time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(config.ET).strftime("%H:%M:%S ET")
    except (TypeError, ValueError):
        return iso or ""


def morning_note(history: list[dict]) -> str:
    """Build the note from published events: the last state of each flag, the brief, and the run status."""
    flags: dict[str, dict] = {}
    brief: dict = {}
    status: dict = {}
    for item in history:
        data = item.get("data", {})
        if item["event"] == "flag":
            flags[data["id"]] = data
        elif item["event"] == "brief":
            brief = data
        elif item["event"] == "status":
            status = {**status, **data}

    info = config.call_info()
    mode = status.get("mode", "live")
    lines = [
        f"# {info.company} ({info.ticker}): {info.title}, {info.session_day:%b %-d, %Y}",
        "",
        f"_Call Desk note, generated {datetime.now(config.ET):%b %-d, %Y %H:%M ET} from a {mode} run. "
        "Numbers that were not said on the call are marked \"not spoken\", with their source._",
        "",
    ]
    if not flags:
        return "\n".join(lines + ["No flags yet. Press Play call, then export again."]) + "\n"

    if brief.get("status") == "done" and brief.get("headline"):
        lines += [f"**Brief:** {brief['headline']}", ""] + [f"- {b}" for b in brief.get("bullets", [])] + [""]

    ordered = sorted(flags.values(), key=lambda f: (_RANK.get(f.get("direction"), 3), -(f.get("materiality") or 0)))
    lines.append(f"## Flags ({len(ordered)})")
    for f in ordered:
        if f.get("status") != "checked":
            continue
        label = _LABEL.get(f["direction"], f["direction"].upper())
        lines += ["", f"### {label} · {f['metric_label']}", f"{_time(f.get('call_ts', ''))} · {f.get('speaker', '')}", ""]
        change = f"{f.get('prior_value') or 'no prior guidance'} → {f.get('new_value') or 'not stated'}"
        source = f.get("new_value_source") or ""
        if f.get("new_value") and source and source != "spoken":
            change += f" (not spoken · {source})"
        lines.append(f"- **Change:** {change}")
        if f.get("why_it_matters"):
            lines.append(f"- **Why it matters:** {f['why_it_matters']}")
        lines.append(f"- **Quote:** \"{f.get('quote', '')}\"")
        pr = f.get("price_reaction") or {}
        if pr:
            market = (f"- **Market:** {pr.get('ticker')} {_pct(pr.get('pct_move'))} vs {pr.get('benchmark')} "
                      f"{_pct(pr.get('benchmark_pct_move'))} ({pr.get('window', '')} after the quote)")
            if pr.get("close_pct_move") is not None:
                market += f"; by the close {pr.get('ticker')} {_pct(pr['close_pct_move'])}, {_pp(pr.get('close_vs_benchmark'))} vs {pr.get('benchmark')}"
            lines.append(market)
        if f.get("confidence") is not None:
            lines.append(f"- **Confidence:** {f['confidence']:.2f}" + (" (agent failed; evidence unreviewed)" if f.get("degraded") else ""))
        rows = f.get("related_changes") or []
        if rows:
            lines += ["", "| Guidance line | Prior | Now | Change |", "|---|---|---|---|"]
            lines += [f"| {r['metric_label']} | {r.get('prior_value') or 'n/a'} | {r['new_value']} | {_DIRECTION_WORD.get(r['direction'], r['direction'])} |" for r in rows]
        sources = [e for e in f.get("evidence", []) if e.get("stance") in ("supports", "contradicts")][:4]
        if sources:
            lines += ["", "Sources:"] + [f"- {e['source']}: {e.get('ref', '')} ({e['stance']})" for e in sources]
    return "\n".join(lines) + "\n"
