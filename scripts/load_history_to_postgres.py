"""
load_history_to_postgres.py

Creates security_master_history in the finance_platform database and
loads all 6 quarterly IVV N-PORT holdings CSVs into it.

Schema: identical to security_master (surrogate SERIAL pk + 17 data
columns) plus filing_date DATE (SEC filing date) and period_end_date
DATE (quarter-end date from repPdDate in the N-PORT XML).  The original
per-CSV 'id' column is NOT carried over — the SERIAL here is an
independent sequence across all 6 loads.
"""

import csv
import os
from io import StringIO
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv()

PGHOST     = os.getenv("PGHOST", "localhost")
PGPORT     = os.getenv("PGPORT", "5432")
PGDATABASE = os.getenv("PGDATABASE", "finance_platform")
PGUSER     = os.getenv("PGUSER", "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

REPO_ROOT = Path(__file__).resolve().parent.parent
PROC_DIR  = REPO_ROOT / "data" / "processed"

# repPdDate extracted from each XML: maps SEC filing_date -> quarter-end period_end_date
PERIOD_END_MAP = {
    "2025-05-27": "2025-03-31",
    "2025-08-28": "2025-06-30",
    "2025-11-26": "2025-09-30",
    "2026-02-25": "2025-12-31",
    "2026-05-28": "2026-03-31",
    "2026-08-25": "2026-06-30",
}

# 6 quarterly filings in chronological order
FILINGS = [
    ("2025-05-27", "ivv_holdings_2025-05-27.csv"),
    ("2025-08-28", "ivv_holdings_2025-08-28.csv"),
    ("2025-11-26", "ivv_holdings_2025-11-26.csv"),
    ("2026-02-25", "ivv_holdings_2026-02-25.csv"),
    ("2026-05-28", "ivv_holdings_2026-05-28.csv"),
    ("2026-08-25", "ivv_holdings_2026-08-25.csv"),
]

# Data columns (matches parse_holdings.py output; PostgreSQL lowercases them)
DATA_COLS = [
    "name", "lei", "title", "cusip", "isin",
    "balance", "units", "curCd", "valUSD", "pctVal",
    "payoffProfile", "assetCat", "issuerCat", "invCountry",
    "isRestrictedSec", "fairValLevel", "isCashCollateral",
]
NUMERIC_COLS = {"balance", "valUSD", "pctVal"}

CREATE_TABLE_SQL = """
CREATE TABLE security_master_history (
    id               SERIAL PRIMARY KEY,
    filing_date      DATE        NOT NULL,
    period_end_date  DATE        NOT NULL,
    name             TEXT,
    lei              TEXT,
    title            TEXT,
    cusip            TEXT,
    isin             TEXT,
    balance          NUMERIC,
    units            TEXT,
    curcd            TEXT,
    valusd           NUMERIC,
    pctval           NUMERIC,
    payoffprofile    TEXT,
    assetcat         TEXT,
    issuercat        TEXT,
    invcountry       TEXT,
    isrestrictedsec  TEXT,
    fairvallevel     TEXT,
    iscashcollateral TEXT
);
"""

# COPY target columns (must match CREATE TABLE order, excluding SERIAL id)
COPY_COLS = [
    "filing_date",
    "period_end_date",
    "name", "lei", "title", "cusip", "isin",
    "balance", "units", "curcd", "valusd", "pctval",
    "payoffprofile", "assetcat", "issuercat", "invcountry",
    "isrestrictedsec", "fairvallevel", "iscashcollateral",
]


def connect():
    return psycopg2.connect(
        host=PGHOST, port=int(PGPORT), dbname=PGDATABASE,
        user=PGUSER, password=PGPASSWORD,
    )


def load_filing(cur, filing_date: str, csv_path: Path) -> int:
    """Read one CSV, add filing_date + period_end_date, bulk-load via COPY. Returns row count."""
    df = pd.read_csv(csv_path, dtype=str)
    df_load = df[DATA_COLS].copy()

    # Normalise column names to lowercase to match COPY_COLS
    df_load.columns = [c.lower() for c in df_load.columns]

    for col in NUMERIC_COLS:
        col_lc = col.lower()
        df_load[col_lc] = pd.to_numeric(df_load[col_lc], errors="coerce")

    # Insert filing_date and period_end_date as the first two columns
    period_end_date = PERIOD_END_MAP[filing_date]
    df_load.insert(0, "filing_date", filing_date)
    df_load.insert(1, "period_end_date", period_end_date)

    buf = StringIO()
    df_load.to_csv(buf, index=False, header=False, quoting=csv.QUOTE_MINIMAL)
    buf.seek(0)

    cols_sql = ", ".join(COPY_COLS)
    cur.copy_expert(
        f"COPY security_master_history ({cols_sql}) FROM STDIN WITH (FORMAT CSV, NULL '')",
        buf,
    )
    return len(df_load)


def main():
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()

    # ── Create table (drop first to allow re-runs) ────────────────────────────
    cur.execute("DROP TABLE IF EXISTS security_master_history;")
    cur.execute(CREATE_TABLE_SQL)
    print("Table 'security_master_history' created.")

    # ── Load each filing ──────────────────────────────────────────────────────
    print()
    total_rows = 0
    for filing_date, filename in FILINGS:
        csv_path = PROC_DIR / filename
        n = load_filing(cur, filing_date, csv_path)
        total_rows += n
        print(f"  Loaded {n:>4} rows  |  filing_date = {filing_date}  |  {filename}")

    # ── Verification ─────────────────────────────────────────────────────────
    print()
    cur.execute("SELECT COUNT(*) FROM security_master_history;")
    count = cur.fetchone()[0]
    print(f"Total rows in security_master_history: {count}")
    assert count == total_rows, f"Row count mismatch: expected {total_rows}, got {count}"

    print()
    print("Rows per filing_date:")
    cur.execute(
        "SELECT filing_date, COUNT(*) AS rows, "
        "       SUM(valusd) AS total_valusd "
        "FROM security_master_history "
        "GROUP BY filing_date "
        "ORDER BY filing_date;"
    )
    rows = cur.fetchall()
    print(f"  {'Filing date':<14}  {'Rows':>5}  {'Sum valUSD':>24}")
    print("  " + "-" * 48)
    for d, n, v in rows:
        print(f"  {str(d):<14}  {n:>5}  ${float(v):>22,.2f}")

    cur.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
