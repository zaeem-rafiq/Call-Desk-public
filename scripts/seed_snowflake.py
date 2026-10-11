"""Seed Snowflake: prior-quarter transcripts and guidance, plus a Cortex Search service.

Creates <DB>.<SCHEMA>.PRIOR_DOCS (searchable chunks), <DB>.<SCHEMA>.PRIOR_GUIDANCE
(one row per guidance line), and the Cortex Search service PRIOR_GUIDANCE_SEARCH,
then runs a test query. Only pre-call documents (data/prior/) are loaded.

Usage: .venv/bin/python scripts/seed_snowflake.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402
from app.tools import prior_guidance  # noqa: E402

TEST_QUERY = "What was PepsiCo's core EPS guidance last quarter?"


def main() -> int:
    if config.is_mock("snowflake"):
        print(f"Snowflake stays in mock mode: {config.mock_reason('snowflake')}")
        return 1
    e = config.env()
    db, schema, wh = e["SNOWFLAKE_DATABASE"], e["SNOWFLAKE_SCHEMA"], e["SNOWFLAKE_WAREHOUSE"]
    chunks = prior_guidance.load_prior_chunks()
    guidance_rows = [c for c in chunks if "#row-" in c["DOC_ID"]]
    print(f"{len(chunks)} chunks ({len(guidance_rows)} guidance-table rows) from data/prior/")

    with prior_guidance.connect(database=None, schema=None) as conn:
        cur = conn.cursor()
        cur.execute(f"CREATE WAREHOUSE IF NOT EXISTS {wh} WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE")
        cur.execute(f"USE WAREHOUSE {wh}")
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {db}")
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {db}.{schema}")
        cur.execute(f"USE SCHEMA {db}.{schema}")
        cur.execute("""CREATE OR REPLACE TABLE PRIOR_DOCS (
            DOC_ID STRING, CHUNK STRING, DOC_TITLE STRING, QUARTER STRING, DOC_TYPE STRING,
            SOURCE_URL STRING, COMPANY STRING, TICKER STRING)""")
        cur.executemany(
            "INSERT INTO PRIOR_DOCS (DOC_ID, CHUNK, DOC_TITLE, QUARTER, DOC_TYPE, SOURCE_URL, COMPANY, TICKER) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'PepsiCo', 'PEP')",
            [(c["DOC_ID"], c["CHUNK"], c["DOC_TITLE"], c["QUARTER"], c["DOC_TYPE"], c["SOURCE_URL"]) for c in chunks],
        )
        cur.execute("""CREATE OR REPLACE TABLE PRIOR_GUIDANCE (
            TICKER STRING, AS_OF STRING, FISCAL_YEAR NUMBER, LINE STRING, SOURCE_DOC STRING)""")
        cur.executemany(
            "INSERT INTO PRIOR_GUIDANCE (TICKER, AS_OF, FISCAL_YEAR, LINE, SOURCE_DOC) VALUES ('PEP', %s, 2026, %s, %s)",
            [(c["QUARTER"], c["CHUNK"], c["DOC_ID"]) for c in guidance_rows],
        )
        started = time.monotonic()
        cur.execute(f"""CREATE OR REPLACE CORTEX SEARCH SERVICE {prior_guidance.SERVICE}
            ON CHUNK
            ATTRIBUTES QUARTER, DOC_TYPE, TICKER
            WAREHOUSE = {wh}
            TARGET_LAG = '1 day'
            AS (SELECT CHUNK, DOC_TITLE, QUARTER, DOC_TYPE, SOURCE_URL, TICKER, DOC_ID FROM PRIOR_DOCS)""")
        print(f"Cortex Search service {db}.{schema}.{prior_guidance.SERVICE} built in {time.monotonic() - started:.0f}s")

    rows = prior_guidance._cortex_query_fresh(TEST_QUERY, 3)
    print(f"test query: {TEST_QUERY}")
    for r in rows:
        print("  ->", json.dumps(r.get("CHUNK", ""))[:160])
    ok = bool(rows) and all(r.get("CHUNK") for r in rows)
    if not ok:
        print("self-test FAILED: no rows, or rows without CHUNK text")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
