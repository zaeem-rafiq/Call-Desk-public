"""Golden eval: run the full pipeline on data/call.mp3 and check the PepsiCo flags.

Checks (see tests/golden.json):
  1. Flags heard in the audio: the EPS guide-down (direction cut), margin pressure, 2027 deferral.
  2. The five FY2026 guidance lines (flags or the agent's guidance table) with value and direction.
Also reports latency from the spoken quote to the first flag and to the checked flag.

A run with Gemini mocked is labelled MOCK: it exercises the plumbing on canned data and is
not an evaluation of the model.

Usage: .venv/bin/python tests/eval.py [--speed 0] [--save]
  --speed 0  process chunks as fast as possible (default); 1 = real time
  --save     promote this run to data/replay/latest if every check passes on live Gemini
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config, observability, pipeline  # noqa: E402
from app.claims import parse_range  # noqa: E402
from app.hub import EventHub  # noqa: E402

GOLDEN: dict = {}


def _label_ok(label: str, spec: dict) -> bool:
    words = set(label.lower().replace("-", " ").split())
    text = label.lower()
    if not all(w in text for w in spec.get("label_all", [])):
        return False
    if spec.get("label_any") and not any(w in text for w in spec["label_any"]):
        return False
    return not any(w in words or f" {w} " in f" {text} " for w in spec.get("label_none", []))


def _value_ok(value: str | None, expected: list[float]) -> bool:
    rng = parse_range(value)
    return bool(rng) and abs(rng[0] - expected[0]) < 0.26 and abs(rng[1] - expected[1]) < 0.26


def check(flags: list[dict]) -> list[tuple[str, bool, str]]:
    """Degraded flags (live agent failed) never count toward a pass."""
    flags = [f for f in flags if not f.get("degraded")]
    results = []
    for spec in GOLDEN["spoken_flags"]:
        metrics = spec.get("metric_any") or [spec["metric"]]
        hits = [f for f in flags if f["metric"] in metrics]
        if "direction" in spec:
            hits = [f for f in hits if f["direction"] == spec["direction"]]
        if "quote_contains" in spec:
            hits = [f for f in hits if spec["quote_contains"] in f["quote"].lower()] or hits
        ok = bool(hits)
        detail = f"{hits[0]['metric']} {hits[0]['direction']} \"{hits[0]['quote'][:60]}\"" if hits else "no matching flag"
        results.append((spec["name"], ok, detail))

    rows = [{"label": f["metric_label"], "prior": f.get("prior_value"), "new": f.get("new_value"), "direction": f["direction"], "from": f"flag {f['id']}"}
            for f in flags]
    rows += [{"label": r["metric_label"], "prior": r.get("prior_value"), "new": r["new_value"], "direction": r["direction"], "from": f"table in {f['id']}"}
             for f in flags for r in f.get("related_changes", [])]
    for spec in GOLDEN["guidance_lines"]:
        cands = [r for r in rows if _label_ok(r["label"], spec)]
        good = [r for r in cands if r["direction"] == spec.get("direction", r["direction"]) and _value_ok(r["new"], spec["new"])
                and all(x in (r["prior"] or "") for x in spec["prior_contains"])]
        if good:
            r = good[0]
            results.append((spec["name"], True, f"{r['prior']} → {r['new']} ({r['direction']}, {r['from']})"))
        else:
            seen = "; ".join(f"{r['prior']} → {r['new']} ({r['direction']})" for r in cands[:2]) or "not found"
            results.append((spec["name"], False, f"expected {spec.get('direction', 'any')} to {spec['new']}; saw {seen}"))
    return results


async def run(speed: float) -> tuple[list[dict], dict, list[dict]]:
    hub = EventHub()
    hub.start_run(f"eval-{time.strftime('%H%M%S')}", record=True)
    summary = await pipeline.run_live(hub, speed=speed)
    flags: dict[str, dict] = {}
    first_seen: dict[str, float] = {}
    for item in hub.history:
        if item["event"] == "flag":
            d = item["data"]
            first_seen.setdefault(d["id"], item["t"])
            flags[d["id"]] = {**d, "_first_t": first_seen[d["id"]], "_final_t": item["t"]}
    return list(flags.values()), summary, hub.history


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--speed", type=float, default=0.0)
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--call", default=None, help="call id from data/calls/ (default: the default call)")
    args = parser.parse_args()
    if args.call:
        config.set_active_call(args.call)
    GOLDEN.update(json.loads(config.call_info().golden_path.read_text()))
    observability.init_sentry()
    modes = config.integration_modes()
    started = time.monotonic()
    flags, summary, _history = asyncio.run(run(args.speed))
    elapsed = time.monotonic() - started
    mocked = [k for k, v in modes.items() if v == "mock"]
    label = "MOCK" if modes["gemini"] == "mock" else "LIVE"

    print(f"\nCall Desk golden eval · {label} run · transcriber={summary.get('transcriber')} · speed={args.speed} · {elapsed:.0f}s")
    print(f"integrations: " + ", ".join(f"{k}={v}" for k, v in modes.items()))
    print(f"\nflags ({len(flags)}):")
    for f in sorted(flags, key=lambda f: f["t_offset"]):
        lat = ""
        if args.speed == 1.0:
            lat = f"  +{f['_first_t'] - f['t_offset']:.0f}s flag / +{f['_final_t'] - f['t_offset']:.0f}s checked"
        print(f"  {f['t_offset']:6.1f}s {f['direction']:8} {f['metric_label']:<34} {str(f.get('new_value'))[:28]:<28} conf={f.get('confidence')}{lat}")

    degraded = [f["id"] for f in flags if f.get("degraded")]
    if degraded:
        print(f"\ndegraded flags (agent failed, excluded from checks): {', '.join(degraded)}")
    results = check(flags)
    print("\nchecks:")
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<38} {detail}")
    passed = sum(ok for _, ok, _ in results)
    verdict = "PASS" if passed == len(results) else "FAIL"
    print(f"\n{label} {verdict}: {passed}/{len(results)} checks" + (f"  (mocked: {', '.join(mocked)})" if mocked else ""))
    if label == "MOCK":
        print("Gemini is mocked, so this run checks plumbing on canned data. It is not an evaluation of the model.")
    if args.save and label == "LIVE" and verdict == "PASS":
        print("use the UI run (server) to refresh the replay cache; eval runs are kept in data/replay/runs/")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
