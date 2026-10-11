"""Watch .env and seed Tiger / Snowflake as soon as their keys appear.

Usage: .venv/bin/python scripts/autoseed.py [--watch] [--interval 15]
Records results in data/seed_state.json (git-ignored). Re-seeds when the target changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config  # noqa: E402

STATE = config.DATA / "seed_state.json"
SEEDS = {"tiger": "scripts/seed_tiger.py", "snowflake": "scripts/seed_snowflake.py"}


def target_fingerprint(name: str) -> str:
    """Hash of the connection target so a new key triggers a re-seed. Never stored in clear."""
    e = config.env()
    raw = e.get("TIGER_DATABASE_URL", "") if name == "tiger" else f"{e.get('SNOWFLAKE_ACCOUNT')}|{e.get('SNOWFLAKE_USER')}|{e.get('SNOWFLAKE_DATABASE')}|{e.get('SNOWFLAKE_SCHEMA')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def run_once() -> dict:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    for name, script in SEEDS.items():
        if config.is_mock(name):
            continue
        fp = target_fingerprint(name)
        prev = state.get(name, {})
        if prev.get("fingerprint") == fp and (prev.get("ok") or prev.get("attempts", 0) >= 3):
            continue
        print(f"[{datetime.now():%H:%M:%S}] seeding {name} ...", flush=True)
        proc = subprocess.run([sys.executable, str(ROOT / script)], capture_output=True, text=True, timeout=900, cwd=ROOT)
        out = (proc.stdout + proc.stderr).strip()
        print(out[-2000:], flush=True)
        attempts = prev.get("attempts", 0) + 1 if prev.get("fingerprint") == fp else 1
        state[name] = {"ok": proc.returncode == 0, "fingerprint": fp, "attempts": attempts, "at": datetime.now().isoformat(timespec="seconds"),
                       "tail": out[-500:]}
        STATE.write_text(json.dumps(state, indent=2))
        print(f"[{datetime.now():%H:%M:%S}] {name}: {'OK' if proc.returncode == 0 else f'FAILED (exit {proc.returncode})'}", flush=True)
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=15)
    args = parser.parse_args()
    while True:
        run_once()
        if not args.watch:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
