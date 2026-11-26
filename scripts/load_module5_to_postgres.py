"""
load_module5_to_postgres.py

Loads Module 5 bond-extraction results into the reporting schema in PostgreSQL.

What it does, in plain English:
  1. Reads the two bond CSV files:
       - module5_build_tranches.csv   (106 bonds extracted from Apple, Microsoft,
                                       Alphabet, Eli Lilly, and J&J FWPs)
       - module5_test_run2_tranches.csv (36 bonds from Meta and Broadcom FWPs,
                                        scored in test run 2)
  2. Looks up the EDGAR source URL (the FWP filing URL) for each bond by
     matching each bond's trade date to the deal table (module5_documents.csv).
  3. Creates reporting.bond_terms — one row per bond (142 bonds total),
     with company, set, deal date, all extracted fields, and the source URL.
  4. Reads both validation files (build and test run 2) and creates
     reporting.bond_checks — one row per bond per check, V1 through V8
     (8 checks × 142 bonds = 1136 rows total).
  5. Reads the official run-1 test files (module5_test_tranches.csv,
     module5_test_validation.csv, module5_test_openfigi.csv) alongside the
     run-2 equivalents, then builds reporting.module5_scorecard — a small
     table with one row per measure comparing run 1 against run 2.
  6. Grants SELECT on all three tables to the powerbi_reader role.
  7. Prints a reconciliation table that checks CSV row counts against DB row
     counts, by company and set.
  8. Confirms powerbi_reader can query each table.

Re-runnable: each of the three tables is dropped and recreated on every run.
"""

import os
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import psycopg2
import sqlalchemy
from dotenv import load_dotenv

load_dotenv()

PGHOST     = os.getenv("PGHOST",     "localhost")
PGPORT     = os.getenv("PGPORT",     "5432")
PGDATABASE = os.getenv("PGDATABASE", "finance_platform")
PGUSER     = os.getenv("PGUSER",     "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

REPO_ROOT = Path(__file__).resolve().parent.parent
PROC_DIR  = REPO_ROOT / "data" / "processed"

BUILD_CSV       = PROC_DIR / "module5_build_tranches.csv"
TEST_RUN2_CSV   = PROC_DIR / "module5_test_run2_tranches.csv"
BUILD_VAL_CSV   = PROC_DIR / "module5_build_validation.csv"
TEST_RUN2_VAL   = PROC_DIR / "module5_test_run2_validation.csv"
DOCS_CSV        = PROC_DIR / "module5_documents.csv"
# Run-1 files are the official (immutable) test score
TEST_RUN1_CSV   = PROC_DIR / "module5_test_tranches.csv"
TEST_RUN1_VAL   = PROC_DIR / "module5_test_validation.csv"
TEST_RUN1_FIGI  = PROC_DIR / "module5_test_openfigi.csv"
TEST_RUN2_FIGI  = PROC_DIR / "module5_test_run2_openfigi.csv"

# Short company names shown in the DB (issuer name → display name)
COMPANY_MAP = {
    "Apple":                  "Apple",
    "Microsoft Corporation":  "Microsoft",
    "Alphabet Inc.":          "Alphabet",
    "Eli Lilly and Company":  "Eli Lilly",
    "Johnson & Johnson":      "Johnson & Johnson",
    "Meta Platforms, Inc.":   "Meta",
    "Broadcom Inc.":          "Broadcom",
}

# Documents CSV uses short names; map them back for joining
DOCS_ISSUER_MAP = {
    "Apple":               "Apple",
    "Microsoft":           "Microsoft Corporation",
    "Alphabet":            "Alphabet Inc.",
    "Eli Lilly":           "Eli Lilly and Company",
    "Johnson & Johnson":   "Johnson & Johnson",
    "Meta":                "Meta Platforms, Inc.",
    "Broadcom":            "Broadcom Inc.",
}

# Plain-English names for each check code
CHECK_NAMES = {
    "V1": "CUSIP format (9-char, check digit)",
    "V2": "ISIN format (12-char, Luhn check)",
    "V3": "CUSIP prefix matches issuer",
    "V4": "Yield = benchmark + spread",
    "V5": "Coupon / yield / price consistency",
    "V6": "Trade date, settlement date, maturity present",
    "V7": "Day count convention valid for currency",
    "V8": "CUSIP unique within deal",
}

_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _parse_trade_date(s):
    """Convert "May 8, 2023" or "November 5, 2026" to a date object. Returns None on failure."""
    m = re.match(r"(?i)([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", str(s).strip())
    if not m:
        return None
    mon = _MONTH_MAP.get(m.group(1).lower())
    if not mon:
        return None
    try:
        return date(int(m.group(3)), int(mon), int(m.group(2)))
    except ValueError:
        return None


def _build_doc_records(docs_df):
    """
    Return a list of (full_issuer, deal_date, url) tuples, one per row in
    module5_documents.csv.  Used by _add_url_and_deal_date to find the best
    match for each bond's trade date.
    """
    records = []
    for _, row in docs_df.iterrows():
        docs_issuer = str(row["issuer"]).strip()
        full_issuer = DOCS_ISSUER_MAP.get(docs_issuer, docs_issuer)
        deal_dt     = pd.to_datetime(row["deal_date"]).date()
        url         = row["fwp_url"] if pd.notna(row.get("fwp_url")) else row.get("document_url", "")
        records.append((full_issuer, deal_dt, str(url) if pd.notna(url) else ""))
    return records


def _add_url_and_deal_date(df, docs_df):
    """
    Attach source_doc_url and deal_date to each bond row.

    Strategy: for each bond, find the document record for the same issuer whose
    deal_date is closest to the bond's trade_date and within a 5-day forward
    window (trade → 424B2 filing is typically 1–4 business days).
    """
    records = _build_doc_records(docs_df)

    urls       = []
    deal_dates = []
    for _, bond in df.iterrows():
        issuer   = str(bond["issuer"]).strip()
        trade_dt = _parse_trade_date(bond.get("trade_date", ""))
        if trade_dt is None:
            urls.append("")
            deal_dates.append("")
            continue

        best_url  = ""
        best_dd   = ""
        best_gap  = 999
        for rec_issuer, deal_dt, url in records:
            if rec_issuer != issuer:
                continue
            gap = (deal_dt - trade_dt).days
            if 0 <= gap <= 5 and gap < best_gap:
                best_gap = gap
                best_url = url
                best_dd  = str(deal_dt)

        urls.append(best_url)
        deal_dates.append(best_dd)

    df = df.copy()
    df["source_doc_url"] = urls
    df["deal_date"]      = deal_dates
    return df


def build_bond_terms(docs_df):
    """
    Load both tranches CSVs, tag each row with set/company/deal_date/source_doc_url,
    and return a single DataFrame ready for the database.
    """
    build = pd.read_csv(BUILD_CSV, dtype=str)
    build["set"] = "build"

    test = pd.read_csv(TEST_RUN2_CSV, dtype=str)
    test["set"] = "test"

    df = pd.concat([build, test], ignore_index=True)
    df["company"] = df["issuer"].map(COMPANY_MAP).fillna(df["issuer"])

    df = _add_url_and_deal_date(df, docs_df)

    # Choose the columns to store (and their order in the table)
    keep = [
        "company", "set", "deal_date", "issuer",
        "bond_name", "currency", "principal_amount",
        "rate_type", "coupon_pct", "coupon_frequency",
        "floating_benchmark", "floating_spread_pct",
        "maturity_date", "price_to_public_pct",
        "benchmark_treasury_yield_pct", "spread_to_treasury_bps",
        "yield_pct", "yield_semiannual", "yield_annual",
        "trade_date", "settlement_date",
        "day_count", "day_count_source",
        "rating_moodys", "rating_sp",
        "cusip", "isin",
        "source_doc_url",
    ]
    return df[keep]


def build_bond_checks():
    """
    Load both validation CSVs (build + test run 2), normalise result values,
    and add human-readable check names.  Returns a single DataFrame.
    """
    build_val = pd.read_csv(BUILD_VAL_CSV, dtype=str)
    build_val["set"] = "build"

    test_val = pd.read_csv(TEST_RUN2_VAL, dtype=str)
    test_val["set"] = "test"

    df = pd.concat([build_val, test_val], ignore_index=True)

    df["company"]    = df["issuer"].map(COMPANY_MAP).fillna(df["issuer"])
    df["check_name"] = df["check"].map(CHECK_NAMES)

    # Normalise result spellings: "not applicable" → "not_applicable"
    df["result"] = (
        df["result"]
        .str.strip()
        .str.lower()
        .str.replace(" ", "_", regex=False)
    )

    # Rename "detail" to "reason" to match what the user asked for
    df = df.rename(columns={"detail": "reason"})

    keep = ["company", "set", "issuer", "cusip", "bond_name",
            "check", "check_name", "result", "reason"]
    return df[keep]


def _pct_fields_filled(df):
    """
    Fraction of non-null, non-empty cells across all data columns
    (excluding the helper column 'missing_fields').
    """
    data_cols = [c for c in df.columns if c != "missing_fields"]
    total = len(df) * len(data_cols)
    filled = sum(
        df[c].notna().sum() - (df[c].astype(str).str.strip().str.lower() == "nan").sum()
        for c in data_cols
    )
    return round(100 * filled / total) if total else 0


def _pass_all_rate(val_df, tranches_df):
    """
    Fraction of bonds in tranches_df where every check in val_df is not 'fail'.
    Returns (n_pass, n_total).
    """
    fail_cusips = set(val_df[val_df["result"].str.strip().str.lower() == "fail"]["cusip"])
    all_cusips  = set(tranches_df["cusip"].astype(str).str.strip())
    n_pass      = len(all_cusips - fail_cusips)
    return n_pass, len(all_cusips)


def build_scorecard():
    """
    Compare run-1 (Broadcom only, extractor unchanged) against run-2
    (Meta + Broadcom, improved extractor).  Returns a DataFrame with
    one row per measure.
    """
    t1  = pd.read_csv(TEST_RUN1_CSV,  dtype=str)
    v1  = pd.read_csv(TEST_RUN1_VAL,  dtype=str)
    f1  = pd.read_csv(TEST_RUN1_FIGI, dtype=str)
    t2  = pd.read_csv(TEST_RUN2_CSV,  dtype=str)
    v2  = pd.read_csv(TEST_RUN2_VAL,  dtype=str)
    f2  = pd.read_csv(TEST_RUN2_FIGI, dtype=str)

    pass1, total1   = _pass_all_rate(v1, t1)
    pass2, total2   = _pass_all_rate(v2, t2)

    coupon1_pass    = (f1["coupon_match"]   == "pass").sum()
    maturity1_pass  = (f1["maturity_match"] == "pass").sum()
    coupon2_pass    = (f2["coupon_match"]   == "pass").sum()
    maturity2_pass  = (f2["maturity_match"] == "pass").sum()

    pct1 = _pct_fields_filled(t1)
    pct2 = _pct_fields_filled(t2)

    rows = [
        ("bonds_found",          total1, total2, "count",
         "Total bonds extracted from test-set FWPs"),
        ("meta_bonds",           0,      (t2["issuer"] == "Meta Platforms, Inc.").sum(),  "count",
         "Bonds extracted for Meta"),
        ("broadcom_bonds",       (t1["issuer"] == "Broadcom Inc.").sum(),
                                 (t2["issuer"] == "Broadcom Inc.").sum(),  "count",
         "Bonds extracted for Broadcom"),
        ("pct_fields_filled",    pct1,  pct2,  "pct",
         "Non-null cells as % of total data cells"),
        ("bonds_pass_all_checks", pass1, pass2, "count",
         "Bonds with zero failures across V1-V8"),
        ("bonds_total",          total1, total2, "count",
         "Total bonds scored (denominator for pass rate)"),
        ("figi_coupon_pass",     int(coupon1_pass),   int(coupon2_pass),   "count",
         "Bonds whose coupon matches OpenFIGI"),
        ("figi_maturity_pass",   int(maturity1_pass), int(maturity2_pass), "count",
         "Bonds whose maturity date matches OpenFIGI"),
        ("figi_total",           len(f1), len(f2), "count",
         "Total bonds checked against OpenFIGI (denominator)"),
    ]
    return pd.DataFrame(rows, columns=["measure", "run1_value", "run2_value", "unit", "description"])


def engine_url():
    return (
        f"postgresql+psycopg2://{PGUSER}:{PGPASSWORD}"
        f"@{PGHOST}:{PGPORT}/{PGDATABASE}"
    )


def write_tables(bond_terms, bond_checks, scorecard):
    engine = sqlalchemy.create_engine(engine_url())

    with engine.connect() as conn:
        conn.execute(sqlalchemy.text("CREATE SCHEMA IF NOT EXISTS reporting"))
        conn.commit()

    # Drop in reverse dependency order, then recreate
    for tbl in ("bond_terms", "bond_checks", "module5_scorecard"):
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text(f"DROP TABLE IF EXISTS reporting.{tbl}"))
            conn.commit()

    bond_terms.to_sql("bond_terms", engine, schema="reporting",
                      if_exists="replace", index=False)
    print(f"  reporting.bond_terms        : {len(bond_terms)} rows written")

    bond_checks.to_sql("bond_checks", engine, schema="reporting",
                       if_exists="replace", index=False)
    print(f"  reporting.bond_checks       : {len(bond_checks)} rows written")

    scorecard.to_sql("module5_scorecard", engine, schema="reporting",
                     if_exists="replace", index=False)
    print(f"  reporting.module5_scorecard : {len(scorecard)} rows written")

    # Grant SELECT to powerbi_reader on all three tables
    with engine.connect() as conn:
        for tbl in ("bond_terms", "bond_checks", "module5_scorecard"):
            conn.execute(sqlalchemy.text(
                f"GRANT SELECT ON reporting.{tbl} TO powerbi_reader"
            ))
        conn.commit()
    print("  GRANT SELECT → powerbi_reader on all 3 tables  [done]")


def reconcile(bond_terms):
    """
    Print CSV counts vs DB counts side by side, broken down by company and set.
    Both must match for the script to succeed.
    """
    engine = sqlalchemy.create_engine(engine_url())

    # CSV counts
    csv_counts = (
        bond_terms.groupby(["company", "set"])
        .size()
        .reset_index(name="csv_rows")
    )

    # DB counts
    with engine.connect() as conn:
        db_df = pd.read_sql(
            "SELECT company, set, COUNT(*) AS db_rows "
            "FROM reporting.bond_terms GROUP BY company, set ORDER BY company, set",
            conn,
        )

    merged = csv_counts.merge(db_df, on=["company", "set"], how="outer").fillna(0)
    merged["match"] = merged["csv_rows"].astype(int) == merged["db_rows"].astype(int)

    print()
    print("=" * 70)
    print("Reconciliation: CSV rows vs DB rows")
    print("=" * 70)
    print(f"  {'Company':<22}  {'Set':<6}  {'CSV':>5}  {'DB':>5}  {'Match':>6}")
    print("  " + "-" * 54)
    for _, r in merged.sort_values(["set", "company"]).iterrows():
        flag = "[PASS]" if r["match"] else "[FAIL]"
        print(f"  {r['company']:<22}  {r['set']:<6}  {int(r['csv_rows']):>5}  "
              f"{int(r['db_rows']):>5}  {flag}")

    csv_total = int(csv_counts["csv_rows"].sum())
    db_total  = int(merged["db_rows"].sum())
    print("  " + "-" * 54)
    total_flag = "[PASS]" if csv_total == db_total else "[FAIL]"
    print(f"  {'TOTAL':<22}  {'':6}  {csv_total:>5}  {db_total:>5}  {total_flag}")

    if not merged["match"].all() or csv_total != db_total:
        raise AssertionError(
            f"Row count mismatch: CSV={csv_total}, DB={db_total}. "
            "Check output above for which companies differ."
        )
    print(f"\n  142 bonds expected (106 build + 36 test). Found: {db_total}.")
    if db_total != 142:
        raise AssertionError(f"Expected 142 bonds total, got {db_total}.")


def confirm_powerbi_reader(tables):
    """Connect as powerbi_reader and run a one-row SELECT on each table."""
    pbi_pw = os.getenv("PBI_READER_PASSWORD", "")
    if not pbi_pw:
        print("\n(PBI_READER_PASSWORD not set — skipping powerbi_reader test)")
        return

    pbi_engine = sqlalchemy.create_engine(
        f"postgresql+psycopg2://powerbi_reader:{pbi_pw}"
        f"@{PGHOST}:{PGPORT}/{PGDATABASE}"
    )
    print()
    print("=" * 70)
    print("powerbi_reader access check")
    print("=" * 70)
    with pbi_engine.connect() as conn:
        for tbl in tables:
            df = pd.read_sql(
                f"SELECT * FROM reporting.{tbl} LIMIT 1", conn
            )
            print(f"  reporting.{tbl:<26}  {len(df)} row returned  [PASS]")


def main():
    print("Loading module5_documents.csv ...")
    docs_df = pd.read_csv(DOCS_CSV)

    print("Building bond_terms (142 bonds = 106 build + 36 test) ...")
    bond_terms = build_bond_terms(docs_df)

    print("Building bond_checks (8 checks × 142 bonds = 1136 rows) ...")
    bond_checks = build_bond_checks()

    print("Building module5_scorecard (run 1 vs run 2) ...")
    scorecard = build_scorecard()

    print()
    print("Writing to reporting schema ...")
    write_tables(bond_terms, bond_checks, scorecard)

    reconcile(bond_terms)

    print()
    print("=" * 70)
    print("module5_scorecard — run 1 vs run 2")
    print("=" * 70)
    print(scorecard.to_string(index=False))

    confirm_powerbi_reader(["bond_terms", "bond_checks", "module5_scorecard"])

    print()
    print("Done.")


if __name__ == "__main__":
    main()
