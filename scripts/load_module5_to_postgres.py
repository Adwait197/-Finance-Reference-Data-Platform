"""
load_module5_to_postgres.py

Loads Module 5 bond-extraction results into the reporting schema in PostgreSQL.

What it does, in plain English:
  1. Reads the two bond CSV files:
       - module5_build_tranches.csv   (106 bonds extracted from Apple, Microsoft,
                                       Alphabet, Eli Lilly, and J&J FWPs)
       - module5_test_run2_tranches.csv (36 bonds from Meta and Broadcom FWPs,
                                        scored in test run 2)
  2. Each bond row now carries its exact fwp_accession (the EDGAR accession number
     of the FWP it was extracted from).  The script looks up two URLs from
     module5_documents.csv:
       - source_doc_url   = the FWP (free-writing prospectus) the bond came from
       - prospectus_url   = the matching 424B2 final prospectus for that deal
  3. Creates reporting.bond_terms  — one row per bond (142 bonds total),
     with company, set, deal date, all extracted fields, and both document URLs.
  4. Reads both validation files (build and test run 2) and creates
     reporting.bond_checks — one row per bond per check, V1 through V9
     (9 checks × 142 bonds = 1278 rows total).
  5. Reads the official run-1 test files alongside the run-2 equivalents and
     builds reporting.module5_scorecard — one row per measure.
  6. Grants SELECT on all three tables to the powerbi_reader role.
  7. Prints a reconciliation table (CSV counts vs DB row counts per company/set).
  8. Confirms powerbi_reader can query each table.

Fields-filled definition (used in the scorecard):
  Numerator   = cells that are non-null and non-empty for applicable fields.
  Denominator = total applicable cells, where "applicable" means the field is
                meaningful for that specific bond's type and currency:
                  - floating_benchmark, floating_spread_pct  → applicable only
                    for floating-rate bonds (always null for fixed-rate bonds)
                  - yield_annual                             → applicable only for
                    EUR bonds (annual yield is only reported for EUR/GBP deals)
                All other fields are applicable to every bond.
                This correctly removes the "not applicable" cells from the
                denominator so the percentage reflects genuine completeness.

Re-runnable: each of the three tables is dropped and recreated on every run.
"""

import os
import re
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
TEST_RUN1_CSV   = PROC_DIR / "module5_test_tranches.csv"
TEST_RUN1_VAL   = PROC_DIR / "module5_test_validation.csv"
TEST_RUN1_FIGI  = PROC_DIR / "module5_test_openfigi.csv"
TEST_RUN2_FIGI  = PROC_DIR / "module5_test_run2_openfigi.csv"

COMPANY_MAP = {
    "Apple":                  "Apple",
    "Microsoft Corporation":  "Microsoft",
    "Alphabet Inc.":          "Alphabet",
    "Eli Lilly and Company":  "Eli Lilly",
    "Johnson & Johnson":      "Johnson & Johnson",
    "Meta Platforms, Inc.":   "Meta",
    "Broadcom Inc.":          "Broadcom",
}

CHECK_NAMES = {
    "V1": "CUSIP format (9-char, check digit)",
    "V2": "ISIN format (12-char, Luhn check)",
    "V3": "CUSIP prefix matches issuer",
    "V4": "Yield = benchmark + spread",
    "V5": "Coupon / yield / price consistency",
    "V6": "Trade date, settlement date, maturity present",
    "V7": "Day count convention valid for currency",
    "V8": "CUSIP unique within deal",
    "V9": "Moody's and S&P ratings are recognized grades",
}

# Fields that are only applicable for floating-rate bonds
_FLOAT_ONLY = {"floating_benchmark", "floating_spread_pct"}
# yield_annual is only reported for EUR bonds (annual yield convention)
_EUR_ONLY = {"yield_annual"}


def _loud_numeric(df, col):
    """
    Convert df[col] to float.  Print every value that cannot be converted
    (company, cusip, column, original text) so nothing turns into NULL silently.
    Returns a float Series; raises RuntimeError if any conversions fail.
    """
    failures = []
    out = pd.to_numeric(df[col], errors="coerce")
    # Find rows where the input was non-null/non-empty but the output is NaN
    mask_input_present = df[col].notna() & (df[col].astype(str).str.strip().str.lower() != "") & (df[col].astype(str).str.strip().str.lower() != "nan")
    mask_coerced_null  = out.isna()
    bad = df[mask_input_present & mask_coerced_null]
    for _, row in bad.iterrows():
        failures.append(
            f"  CANNOT CONVERT: company={row.get('company','?')}  "
            f"cusip={row.get('cusip','?')}  col={col}  "
            f"value={repr(row[col])}"
        )
    if failures:
        print(f"\nERROR — {len(failures)} non-convertible value(s) in '{col}':")
        for f in failures:
            print(f)
        raise RuntimeError(
            f"{len(failures)} value(s) in '{col}' could not be converted to float — "
            "fix the extractor or expected_exceptions before reloading."
        )
    return out


def _build_doc_lookups(docs_df):
    """
    Build two dicts from module5_documents.csv, keyed by fwp_accession:
      fwp_url_map        → fwp_url (the FWP itself)
      prospectus_url_map → document_url (the matching 424B2)
      deal_date_map      → deal_date string
    One FWP accession may appear in multiple deal rows (Alphabet had two 424B2
    filings sharing one FWP); we deduplicate on first occurrence.
    """
    fwp_url_map        = {}
    prospectus_url_map = {}
    deal_date_map      = {}
    for _, row in docs_df.iterrows():
        acc = str(row.get("fwp_accession", "")).strip()
        if not acc or acc == "nan":
            continue
        if acc in fwp_url_map:
            continue   # keep first occurrence
        fwp_url_map[acc]        = str(row["fwp_url"])        if pd.notna(row.get("fwp_url"))        else ""
        prospectus_url_map[acc] = str(row["document_url"])   if pd.notna(row.get("document_url"))   else ""
        deal_date_map[acc]      = str(row["deal_date"])       if pd.notna(row.get("deal_date"))      else ""
    return fwp_url_map, prospectus_url_map, deal_date_map


def build_bond_terms(docs_df):
    """
    Load both tranches CSVs, tag each row with set/company/deal_date/URLs.
    URLs are resolved from the bond's own fwp_accession column, not by date matching.
    """
    fwp_url_map, prospectus_url_map, deal_date_map = _build_doc_lookups(docs_df)

    build = pd.read_csv(BUILD_CSV, dtype=str)
    build["set"] = "build"

    test = pd.read_csv(TEST_RUN2_CSV, dtype=str)
    test["set"] = "test"

    df = pd.concat([build, test], ignore_index=True)
    df["company"] = df["issuer"].map(COMPANY_MAP).fillna(df["issuer"])

    # Resolve URLs directly from fwp_accession — no date approximation needed
    df["source_doc_url"]  = df["fwp_accession"].map(fwp_url_map).fillna("")
    df["prospectus_url"]  = df["fwp_accession"].map(prospectus_url_map).fillna("")
    df["deal_date"]       = df["fwp_accession"].map(deal_date_map).fillna("")

    # principal_amount: preserve original text, then parse to plain number.
    # Formats seen: '$1,000,000,000', '€700,000,000', '£750,000,000',
    #               'C$2,250,000,000' — strip everything except digits and '.'.
    df["principal_amount_text"] = (
        df["principal_amount"].astype(str).str.strip()
        .where(df["principal_amount"].notna() &
               (df["principal_amount"].astype(str).str.strip() != "nan"), "")
    )
    def _parse_principal(val):
        if pd.isna(val) or str(val).strip().lower() in ("", "nan"):
            return None
        cleaned = re.sub(r"[^\d.]", "", str(val))
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    df["principal_amount"] = df["principal_amount"].apply(_parse_principal)

    # Cast all numeric fields to float so PostgreSQL stores double precision.
    # _loud_numeric raises if any non-null, non-empty value cannot be converted,
    # so nothing is silently coerced to NULL.
    _NUMERIC_COLS = [
        "coupon_pct", "price_to_public_pct", "yield_pct",
        "spread_to_treasury_bps", "benchmark_treasury_yield_pct",
        "yield_semiannual", "yield_annual", "floating_spread_pct",
    ]
    for col in _NUMERIC_COLS:
        if col in df.columns:
            df[col] = _loud_numeric(df, col)

    keep = [
        "company", "set", "deal_date", "issuer",
        "bond_name", "currency",
        "principal_amount", "principal_amount_text", "principal_source",
        "rate_type", "coupon_pct", "coupon_frequency",
        "floating_benchmark", "floating_spread_pct",
        "maturity_date", "price_to_public_pct",
        "benchmark_treasury_yield_pct", "spread_to_treasury_bps",
        "yield_pct", "yield_semiannual", "yield_annual",
        "trade_date", "settlement_date",
        "day_count", "day_count_source",
        "rating_moodys", "rating_sp",
        "cusip", "isin",
        "fwp_accession",
        "source_doc_url",
        "prospectus_url",
    ]
    return df[keep]


def _url_comparison(df, docs_df):
    """
    Compare the exact accession-based URLs against the old nearest-date approach.
    Returns the number of bonds whose source_doc_url changed.
    """
    import re
    from datetime import date, timedelta

    _MONTH_MAP = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
    }

    DOCS_ISSUER_MAP = {
        "Apple": "Apple", "Microsoft": "Microsoft Corporation",
        "Alphabet": "Alphabet Inc.", "Eli Lilly": "Eli Lilly and Company",
        "Johnson & Johnson": "Johnson & Johnson",
        "Meta": "Meta Platforms, Inc.", "Broadcom": "Broadcom Inc.",
    }

    def _parse_dt(s):
        m = re.match(r"(?i)([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", str(s).strip())
        if not m:
            return None
        mon = _MONTH_MAP.get(m.group(1).lower())
        try:
            return date(int(m.group(3)), int(mon), int(m.group(2))) if mon else None
        except ValueError:
            return None

    # Build date-based records for the old approach
    records = []
    for _, row in docs_df.iterrows():
        docs_issuer = str(row["issuer"]).strip()
        full_issuer = DOCS_ISSUER_MAP.get(docs_issuer, docs_issuer)
        deal_dt     = pd.to_datetime(row["deal_date"]).date()
        url         = str(row["fwp_url"]) if pd.notna(row.get("fwp_url")) else ""
        records.append((full_issuer, deal_dt, url))

    old_urls = []
    for _, bond in df.iterrows():
        issuer   = str(bond["issuer"]).strip()
        trade_dt = _parse_dt(bond.get("trade_date", ""))
        if trade_dt is None:
            old_urls.append("")
            continue
        best_url, best_gap = "", 999
        for rec_issuer, deal_dt, url in records:
            if rec_issuer != issuer:
                continue
            gap = (deal_dt - trade_dt).days
            if 0 <= gap <= 5 and gap < best_gap:
                best_gap = gap
                best_url = url
        old_urls.append(best_url)

    changed = sum(
        1 for new, old in zip(df["source_doc_url"].fillna(""), old_urls)
        if new != old
    )
    return changed, len(df)


def build_bond_checks():
    """
    Load both validation CSVs, normalise result values, add check names.
    """
    build_val = pd.read_csv(BUILD_VAL_CSV, dtype=str)
    build_val["set"] = "build"

    test_val = pd.read_csv(TEST_RUN2_VAL, dtype=str)
    test_val["set"] = "test"

    df = pd.concat([build_val, test_val], ignore_index=True)
    df["company"]    = df["issuer"].map(COMPANY_MAP).fillna(df["issuer"])
    df["check_name"] = df["check"].map(CHECK_NAMES)

    df["result"] = (
        df["result"]
        .str.strip()
        .str.lower()
        .str.replace(" ", "_", regex=False)
    )
    df = df.rename(columns={"detail": "reason"})

    keep = ["company", "set", "issuer", "cusip", "bond_name",
            "check", "check_name", "result", "reason"]
    return df[keep]


def _pct_fields_filled(df):
    """
    Fields filled = non-null applicable cells / total applicable cells.

    Applicable fields per bond:
      - floating_benchmark, floating_spread_pct : only for floating-rate bonds
      - yield_annual                            : only for EUR bonds
      All other fields apply to every bond.

    This matches the definition written into README.md.  It excludes the
    three structurally-null columns that are always empty for fixed-rate USD
    bonds, so the percentage reflects genuine completeness rather than being
    deflated by planned-empty cells.
    """
    data_cols = [c for c in df.columns if c != "missing_fields"]
    total_applicable = 0
    total_filled     = 0
    for _, row in df.iterrows():
        rate = str(row.get("rate_type", "")).strip().lower()
        cur  = str(row.get("currency", "USD")).strip().upper()
        na_set = set()
        if rate == "fixed":
            na_set |= _FLOAT_ONLY
        elif rate == "floating":
            # For floating bonds, benchmark yield and spread are N/A (no fixed coupon)
            pass
        if cur not in ("EUR", "GBP"):
            na_set |= _EUR_ONLY
        for c in data_cols:
            if c in na_set:
                continue
            total_applicable += 1
            v = row[c]
            if pd.notna(v) and str(v).strip().lower() not in ("", "nan"):
                total_filled += 1
    return round(100 * total_filled / total_applicable) if total_applicable else 0


def _pass_all_rate(val_df, tranches_df):
    fail_cusips = set(val_df[val_df["result"].str.strip().str.lower() == "fail"]["cusip"])
    all_cusips  = set(tranches_df["cusip"].astype(str).str.strip())
    n_pass      = len(all_cusips - fail_cusips)
    return n_pass, len(all_cusips)


def build_scorecard():
    t1 = pd.read_csv(TEST_RUN1_CSV,  dtype=str)
    v1 = pd.read_csv(TEST_RUN1_VAL,  dtype=str)
    f1 = pd.read_csv(TEST_RUN1_FIGI, dtype=str)
    t2 = pd.read_csv(TEST_RUN2_CSV,  dtype=str)
    v2 = pd.read_csv(TEST_RUN2_VAL,  dtype=str)
    f2 = pd.read_csv(TEST_RUN2_FIGI, dtype=str)

    pass1, total1  = _pass_all_rate(v1, t1)
    pass2, total2  = _pass_all_rate(v2, t2)

    coupon1_pass   = (f1["coupon_match"]   == "pass").sum()
    maturity1_pass = (f1["maturity_match"] == "pass").sum()
    coupon2_pass   = (f2["coupon_match"]   == "pass").sum()
    maturity2_pass = (f2["maturity_match"] == "pass").sum()

    pct1 = _pct_fields_filled(t1)
    pct2 = _pct_fields_filled(t2)

    rows = [
        ("bonds_found",           total1, total2, "count",
         "Total bonds extracted from test-set FWPs"),
        ("meta_bonds",            0,      (t2["issuer"] == "Meta Platforms, Inc.").sum(),  "count",
         "Bonds extracted for Meta"),
        ("broadcom_bonds",        (t1["issuer"] == "Broadcom Inc.").sum(),
                                  (t2["issuer"] == "Broadcom Inc.").sum(),  "count",
         "Bonds extracted for Broadcom"),
        ("pct_fields_filled",     pct1,   pct2,   "pct",
         "Applicable fields filled (excludes floating cols for fixed bonds, "
         "yield_annual for non-EUR bonds)"),
        ("bonds_pass_all_checks", pass1,  pass2,  "count",
         "Bonds with zero failures across V1-V8"),
        ("bonds_total",           total1, total2, "count",
         "Total bonds scored (denominator for pass rate)"),
        ("figi_coupon_pass",      int(coupon1_pass),   int(coupon2_pass),   "count",
         "Bonds whose coupon matches OpenFIGI"),
        ("figi_maturity_pass",    int(maturity1_pass), int(maturity2_pass), "count",
         "Bonds whose maturity date matches OpenFIGI"),
        ("figi_total",            len(f1), len(f2), "count",
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

    with engine.connect() as conn:
        for tbl in ("bond_terms", "bond_checks", "module5_scorecard"):
            conn.execute(sqlalchemy.text(
                f"GRANT SELECT ON reporting.{tbl} TO powerbi_reader"
            ))
        conn.commit()
    print("  GRANT SELECT → powerbi_reader on all 3 tables  [done]")


def reconcile(bond_terms):
    engine = sqlalchemy.create_engine(engine_url())

    csv_counts = (
        bond_terms.groupby(["company", "set"])
        .size()
        .reset_index(name="csv_rows")
    )
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
            f"Row count mismatch: CSV={csv_total}, DB={db_total}."
        )
    print(f"\n  142 bonds expected (106 build + 36 test). Found: {db_total}.")
    if db_total != 142:
        raise AssertionError(f"Expected 142 bonds total, got {db_total}.")


def confirm_powerbi_reader(tables):
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
            df = pd.read_sql(f"SELECT * FROM reporting.{tbl} LIMIT 1", conn)
            print(f"  reporting.{tbl:<26}  {len(df)} row returned  [PASS]")


def main():
    print("Loading module5_documents.csv ...")
    docs_df = pd.read_csv(DOCS_CSV)

    print("Building bond_terms (142 bonds = 106 build + 36 test) ...")
    bond_terms = build_bond_terms(docs_df)

    # ── URL comparison: exact accession-based vs old nearest-date approach ──────
    changed, total = _url_comparison(bond_terms, docs_df)
    print(f"  source_doc_url: {changed}/{total} bonds changed vs nearest-date version"
          f"  ({'0 = exact match confirms no ambiguity' if changed == 0 else 'some URLs differ — see note'})")

    print("Building bond_checks (9 checks × 142 bonds = 1278 rows) ...")
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
