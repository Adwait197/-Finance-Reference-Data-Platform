"""
Load IVV holdings CSV into PostgreSQL security_master table.

Steps:
  1. Create the finance_platform database if it does not exist.
  2. Check the CSV for duplicate / missing CUSIP values.
  3. Create security_master with CUSIP as PK (if clean) or a surrogate SERIAL id.
  4. Truncate and load all rows from the CSV.
  5. Run two verification queries and print results.
"""

import os
import sys
import csv
from io import StringIO

import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv()

PGHOST     = os.getenv("PGHOST", "localhost")
PGPORT     = os.getenv("PGPORT", "5432")
PGDATABASE = os.getenv("PGDATABASE", "finance_platform")
PGUSER     = os.getenv("PGUSER", "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "processed", "ivv_holdings_2026-08-25.csv")

# Columns that should be NUMERIC in the database.
# NOTE: The N-PORT 'units' column contains unit-type codes (e.g. "NS" = not stated),
# not a numeric count — it is stored as TEXT. 'balance' is the actual share/unit count.
NUMERIC_COLS = {"balance", "valUSD", "pctVal"}

# All CSV columns in order.
CSV_COLS = [
    "name", "lei", "title", "cusip", "isin",
    "balance", "units", "curCd", "valUSD", "pctVal",
    "payoffProfile", "assetCat", "issuerCat", "invCountry",
    "isRestrictedSec", "fairValLevel", "isCashCollateral",
]


def create_database_if_not_exists():
    """Connect to the default 'postgres' DB and create finance_platform if absent."""
    conn = psycopg2.connect(
        host=PGHOST, port=int(PGPORT), dbname="postgres",
        user=PGUSER, password=PGPASSWORD,
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (PGDATABASE,))
    if cur.fetchone():
        print(f"Database '{PGDATABASE}' already exists.")
    else:
        cur.execute(f'CREATE DATABASE "{PGDATABASE}"')
        print(f"Created database '{PGDATABASE}'.")
    cur.close()
    conn.close()


def check_cusip(df):
    """
    Inspect the cusip column for nulls, blank strings, placeholder 'N/A' values,
    and duplicates. Returns True only if none of those problems exist.
    """
    print("\n--- CUSIP Quality Check ---")

    null_count  = df["cusip"].isnull().sum()
    blank_count = (df["cusip"].fillna("").str.strip() == "").sum()
    # Non-US securities (e.g. Irish-domiciled companies) carry literal 'N/A' in SEC filings.
    na_count    = (df["cusip"].fillna("").str.strip().str.upper() == "N/A").sum()
    dup_count   = df["cusip"].duplicated(keep=False).sum()

    print(f"  Total rows              : {len(df)}")
    print(f"  NULL cusip values       : {null_count}")
    print(f"  Blank cusip values      : {blank_count}")
    print(f"  'N/A' cusip values      : {na_count}  (non-US issuers have no CUSIP in SEC data)")
    print(f"  Duplicate cusip values  : {dup_count}")

    if na_count > 0:
        examples = df.loc[df["cusip"].str.strip().str.upper() == "N/A", ["name", "invCountry", "cusip"]].head(5)
        print("\n  Example N/A rows:")
        for _, row in examples.iterrows():
            print(f"    {row['name']!s:<45} country={row['invCountry']}  cusip={row['cusip']}")

    is_clean = (null_count == 0 and blank_count == 0 and na_count == 0 and dup_count == 0)
    return is_clean


def build_create_table_sql(use_surrogate):
    """Return the CREATE TABLE statement, with or without a surrogate PK."""
    col_defs = []
    if use_surrogate:
        col_defs.append("    id               SERIAL PRIMARY KEY")

    for col in CSV_COLS:
        pg_type = "NUMERIC" if col in NUMERIC_COLS else "TEXT"
        pk_clause = " PRIMARY KEY" if (col == "cusip" and not use_surrogate) else ""
        col_defs.append(f"    {col:<20} {pg_type}{pk_clause}")

    return "CREATE TABLE IF NOT EXISTS security_master (\n" + ",\n".join(col_defs) + "\n);"


def load_data(cur, df):
    """Bulk-load the dataframe into security_master using COPY."""
    df_load = df[CSV_COLS].copy()
    for col in NUMERIC_COLS:
        df_load[col] = pd.to_numeric(df_load[col], errors="coerce")

    buf = StringIO()
    df_load.to_csv(buf, index=False, header=False, quoting=csv.QUOTE_MINIMAL)
    buf.seek(0)

    cols_sql = ", ".join(CSV_COLS)
    cur.copy_expert(
        f"COPY security_master ({cols_sql}) FROM STDIN WITH (FORMAT CSV, NULL '')",
        buf,
    )


def main():
    # ── 1. Load CSV ──────────────────────────────────────────────────────────
    df = pd.read_csv(CSV_PATH, dtype=str)   # read everything as str for the quality check
    print(f"Loaded {len(df)} rows from {os.path.basename(CSV_PATH)}")

    # ── 2. Create database ───────────────────────────────────────────────────
    create_database_if_not_exists()

    # ── 3. CUSIP quality check ───────────────────────────────────────────────
    cusip_clean = check_cusip(df)

    if cusip_clean:
        use_surrogate = False
        print("\nCUSIP is clean — setting cusip as PRIMARY KEY.")
    else:
        use_surrogate = True
        print("\nCUSIP is NOT clean — using surrogate SERIAL id as PRIMARY KEY.")
        print("Reason: Some securities (non-US issuers) have no CUSIP; their field")
        print("        contains literal 'N/A', which violates the NOT NULL + UNIQUE")
        print("        requirement of a PRIMARY KEY constraint.")

    # ── 4. Connect to finance_platform ───────────────────────────────────────
    conn = psycopg2.connect(
        host=PGHOST, port=int(PGPORT), dbname=PGDATABASE,
        user=PGUSER, password=PGPASSWORD,
    )
    conn.autocommit = True
    cur = conn.cursor()

    # ── 5. Create table ──────────────────────────────────────────────────────
    create_sql = build_create_table_sql(use_surrogate)
    cur.execute(create_sql)
    print("\nTable 'security_master' ready.")

    # ── 6. Truncate ──────────────────────────────────────────────────────────
    if use_surrogate:
        cur.execute("TRUNCATE TABLE security_master RESTART IDENTITY;")
    else:
        cur.execute("TRUNCATE TABLE security_master;")
    print("Table truncated.")

    # ── 7. Load ──────────────────────────────────────────────────────────────
    load_data(cur, df)
    print(f"Bulk-loaded {len(df)} rows via COPY.")

    # ── 8. Verification queries ──────────────────────────────────────────────
    print("\n--- Verification ---")

    cur.execute("SELECT COUNT(*) FROM security_master;")
    count = cur.fetchone()[0]
    print(f"SELECT COUNT(*) FROM security_master  =>  {count} rows")

    cur.execute(
        "SELECT name, valUSD FROM security_master ORDER BY valUSD DESC LIMIT 5;"
    )
    rows = cur.fetchall()
    print("\nTop 5 holdings by valUSD:")
    print(f"  {'Name':<50} {'valUSD (USD)':>20}")
    print("  " + "-" * 72)
    for name, val in rows:
        print(f"  {name:<50} {float(val):>20,.2f}")

    cur.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
