"""
export_reporting_for_app.py

Exports every table/view in the reporting schema plus selected Module 2 and
Module 3 result files to app/data/ as Parquet files.

All data is public SEC-derived information.  No credentials are written to
app/data/ — the export reads from PostgreSQL using the local .env but the
exported files themselves contain only the processed data.

Run order: run after all reporting-schema loaders have been run.
"""

import os
import shutil
from pathlib import Path

import pandas as pd
import sqlalchemy
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

PGHOST     = os.getenv("PGHOST",     "localhost")
PGPORT     = os.getenv("PGPORT",     "5432")
PGDATABASE = os.getenv("PGDATABASE", "finance_platform")
PGUSER     = os.getenv("PGUSER",     "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

REPO_ROOT = Path(__file__).resolve().parent.parent
PROC      = REPO_ROOT / "data" / "processed"
OUT       = REPO_ROOT / "app" / "data"
OUT.mkdir(parents=True, exist_ok=True)


def engine():
    return sqlalchemy.create_engine(
        f"postgresql+psycopg2://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}"
    )


# ── Reporting schema tables / views ──────────────────────────────────────────

REPORTING_QUERIES = {
    "qc_trend":            "SELECT * FROM reporting.qc_trend ORDER BY period_end_date",
    "qc_exceptions":       "SELECT * FROM reporting.qc_exceptions ORDER BY period_end_date, name",
    "v_fund_size":         "SELECT * FROM reporting.v_fund_size ORDER BY period_end_date",
    "dim_rule":            "SELECT * FROM reporting.dim_rule ORDER BY rule_sort",
    "bond_terms":          "SELECT * FROM reporting.bond_terms ORDER BY issuer, maturity_date",
    "module5_scorecard":   "SELECT * FROM reporting.module5_scorecard",
    "cash_recon_scorecard":"SELECT * FROM reporting.cash_recon_scorecard ORDER BY round_number, reconciler_version",
    "cash_recon_rules":    "SELECT * FROM reporting.cash_recon_rules",
    "cash_expected":       "SELECT * FROM reporting.cash_expected ORDER BY payment_date",
    "cash_recon_results":  "SELECT * FROM reporting.cash_recon_results ORDER BY round, expected_payment_id",
    "bond_checks":         "SELECT * FROM reporting.bond_checks ORDER BY cusip, check_name",
}

# ── Processed CSV files (Module 2 and Module 3) ───────────────────────────────

FILE_COPIES = {
    "reconciliation":       PROC / "reconciliation_2026-11-17.csv",
    "internal_consistency": PROC / "internal_consistency_2026-08-25.csv",
    "ml_anomalies":         PROC / "ml_anomalies_2026-08-25.csv",
}


def _get_db_count(conn, table_or_query: str, is_query: bool = False) -> int:
    if is_query:
        subq = f"SELECT COUNT(*) FROM ({table_or_query}) _sub"
    else:
        subq = f"SELECT COUNT(*) FROM reporting.{table_or_query}"
    return conn.execute(text(subq)).scalar()


def main():
    eng = engine()

    print(f"\nExporting reporting schema tables to {OUT}/")
    print(f"{'Source':<30}  {'DB rows':>8}  {'Written':>8}  {'Match':>6}")
    print("-" * 60)

    all_pass = True
    with eng.connect() as conn:
        for name, query in REPORTING_QUERIES.items():
            df = pd.read_sql(text(query), conn)
            out_path = OUT / f"{name}.parquet"
            df.to_parquet(out_path, index=False)
            db_n  = _get_db_count(conn, query, is_query=True)
            match = len(df) == db_n
            if not match:
                all_pass = False
            flag = "PASS" if match else "FAIL"
            print(f"  {name:<28}  {db_n:>8}  {len(df):>8}  [{flag}]")

    print()
    print(f"Copying processed files to {OUT}/")
    print(f"{'Source file':<40}  {'Rows':>8}")
    print("-" * 55)

    for name, src_path in FILE_COPIES.items():
        df = pd.read_csv(src_path)
        out_path = OUT / f"{name}.parquet"
        df.to_parquet(out_path, index=False)
        print(f"  {src_path.name:<38}  {len(df):>8}")

    print()
    if all_pass:
        print("All DB row counts match.  app/data/ is ready.")
    else:
        print("WARNING: some row counts did not match — see [FAIL] rows above.")

    # List final contents
    print()
    print(f"Files in {OUT}/:")
    for f in sorted(OUT.iterdir()):
        size_kb = f.stat().st_size / 1024
        print(f"  {f.name:<40}  {size_kb:>7.1f} KB")


if __name__ == "__main__":
    main()
