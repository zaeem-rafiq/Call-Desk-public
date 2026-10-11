"""Make a copy of this repository whose whole history is safe to publish.

The working repository's early commits contain third-party files: Motley Fool call transcripts,
the webcast audio and yfinance price bars. This clones the repository into a new folder, removes
those files from every commit with git-filter-repo (keeping every commit, author and timestamp),
and checks the result. It never pushes: the copy has no remote. Pushing it is a separate step that
needs the owner's approval.

Usage: .venv/bin/python scripts/make_public_copy.py [--out folder] [--branch build/hackathon]
       (needs uv on PATH; git-filter-repo runs through uvx, pinned below)
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILTER_REPO = ["uvx", "--from", "git-filter-repo==2.47.0", "git-filter-repo"]

# Removed from every commit. Globs match across folders, so per-call folders under data/calls/ are covered.
REMOVE_PATHS = ["data/raw/", "data/replay/latest-prev/", "data/call.mp3"]
REMOVE_GLOBS = [
    "data/*_transcript.txt",  # Motley Fool transcripts (our own data/replay/latest/transcript.txt is kept)
    "data/calls/*.mp3",  # webcast audio, rebuilt by scripts/fetch_audio.py
    "data/*prices.csv", "data/*day_close.csv", "data/*prior_close.csv",  # yfinance bars, rebuilt by scripts/fetch_prices.py
]
# Added to the copy's .gitignore in one last commit, so files the fetch scripts rebuild are never committed back.
# The working repository still tracks them, so its own .gitignore must not list them.
PUBLIC_IGNORES = """
# third-party audio and vendor price bars: rebuilt by scripts/fetch_audio.py and scripts/fetch_prices.py
data/call.mp3
data/calls/**/*.mp3
data/**/prices.csv
data/**/day_close.csv
data/**/prior_close.csv
"""
REMOVED = re.compile(r"^data/raw/|^data/replay/latest-prev/|^data/call\.mp3$|^data/calls/.*\.mp3$"
                     r"|^data/.*_transcript\.txt$|^data/.*(prices|day_close|prior_close)\.csv$")

# Real credentials only; the test fixtures (https://public@o0.ingest.sentry.io/0) and .env.example placeholders do not match.
SECRETS = re.compile(r"AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9]{20,}|[0-9a-f]{32}@o\d+\.ingest|sntrys_[A-Za-z0-9_=]{20,}"
                     r"|ghp_[A-Za-z0-9]{36}|-----BEGIN [A-Z ]*PRIVATE KEY|tsdb" r"admin:(?!<)[^@\s]{6,}@"  # split so this line never matches itself
                     r"|^\+\s*(GEMINI_API_KEY|NIMBLE_API_KEY|SNOWFLAKE_PASSWORD|SENTRY_DSN|TIGER_DATABASE_URL)=\S{8,}", re.M)


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def main() -> int:
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else Path(tempfile.mkdtemp(prefix="call-desk-public-")) / "Call-Desk"
    branch = sys.argv[sys.argv.index("--branch") + 1] if "--branch" in sys.argv else "build/hackathon"
    if out.exists():
        print(f"{out} already exists; pass a new --out folder")
        return 1

    subprocess.run(["git", "clone", "-q", "--no-local", "--branch", branch, str(ROOT), str(out)], check=True)
    source_commits = int(git("rev-list", "--count", "HEAD", cwd=out))
    source_head = git("rev-parse", "--short", "HEAD", cwd=out).strip()
    args = ["--invert-paths", "--quiet"]
    for p in REMOVE_PATHS:
        args += ["--path", p]
    for g in REMOVE_GLOBS:
        args += ["--path-glob", g]
    subprocess.run([*FILTER_REPO, *args], cwd=out, check=True)  # also drops the origin remote

    problems = []
    if git("remote", cwd=out).strip():
        problems.append("the copy still has a remote")
    left = sorted({p for p in git("log", "--all", "--format=", "--name-only", cwd=out).splitlines() if REMOVED.search(p)})
    if left:
        problems.append(f"removed paths still in history: {left}")
    commits = int(git("rev-list", "--count", "HEAD", cwd=out))
    if commits != source_commits:
        problems.append(f"commit count changed: {source_commits} -> {commits}")
    # This script holds the patterns themselves (an early version matched its own line), so its diffs are not scanned.
    history = git("log", "-p", "--all", "--no-color", "--", ".", ":(exclude)scripts/make_public_copy.py", cwd=out)
    secrets = sorted({m.group(0)[:6] + "…" for m in SECRETS.finditer(history)})
    if secrets:
        problems.append(f"possible credentials in history (masked): {secrets}")

    if not problems:
        with (out / ".gitignore").open("a") as f:
            f.write(PUBLIC_IGNORES)
        git("add", ".gitignore", cwd=out)
        git("commit", "-q", "-m", "Public copy: ignore the third-party audio and price files that the fetch scripts rebuild\n\n"
            "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", cwd=out)

    data_files = [p for p in git("ls-tree", "-r", "--name-only", "HEAD", cwd=out).splitlines()
                  if p.startswith("data/") and p.endswith((".txt", ".csv", ".mp3", ".wav", ".json", ".jsonl", ".md"))]
    print(f"source {branch} at {source_head}: {source_commits} commits")
    print(f"copy at {out}: {commits} rewritten commits{'' if problems else ' + 1 .gitignore commit'}, HEAD {git('rev-parse', '--short', 'HEAD', cwd=out).strip()}, no remote")
    print("data files kept at HEAD (check none is third-party):")
    for p in data_files:
        print(f"  {p}")
    for p in problems:
        print(f"PROBLEM: {p}")
    print("OK: safe-to-publish copy ready (not pushed)" if not problems else "NOT OK: fix the problems above before pushing")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
