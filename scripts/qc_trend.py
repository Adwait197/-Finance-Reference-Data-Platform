"""
Module 4: QC exception trend across 6 quarterly N-PORT filings.

Applies the same rules as qc_rules.py to security_master_history, grouped
by filing_date.  R4 (duplicate cusip) is evaluated within each partition.

Derivative scoping (matches qc_rules.py):
  Rows with assetCat starting with 'D' (exchange-traded derivatives) are
  excluded from R1/R2/R3 — futures have no CUSIP/ISIN and their valUSD is
  unrealised P&L which can be negative.  They are counted separately as D1.
  D1 rows are NOT added to total_unique_exceptions (not data errors).

Output: data/processed/qc_trend_by_quarter.csv
  Columns: filing_date, r1_count, r2_count, r3_count, r4_count,
           r5_count, r6_count, d1_count, total_unique_exceptions
"""

import os
from pathlib import Path

import pandas as pd
import sqlalchemy
from dotenv import load_dotenv

load_dotenv()

PGHOST     = os.getenv("PGHOST", "localhost")
PGPORT     = int(os.getenv("PGPORT", "5432"))
PGDATABASE = os.getenv("PGDATABASE", "finance_platform")
PGUSER     = os.getenv("PGUSER", "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

REPO_ROOT     = Path(__file__).resolve().parent.parent
OUTPUT_CSV    = REPO_ROOT / "data" / "processed" / "qc_trend_by_quarter.csv"
EXPECTED_CSV  = REPO_ROOT / "data" / "reference" / "expected_exceptions.csv"


# ── helpers ────────────────────────────────────────────────────────────────────

def is_blank(s: pd.Series) -> pd.Series:
    nulls  = s.isna()
    blanks = s.fillna("").astype(str).str.strip().isin(["", "N/A", "n/a", "NA"])
    return nulls | blanks


def load_reviewed_lookup(csv_path: Path = EXPECTED_CSV) -> dict:
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path, keep_default_na=False)
    for col in ("cusip", "isCashCollateral", "assetcat"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].str.strip()
    lookup = {}
    for _, row in df.iterrows():
        cusip    = row["cusip"]
        iscash   = row["isCashCollateral"]
        assetcat = row.get("assetcat", "")
        if cusip:
            key = ("cusip", cusip, iscash)
        elif assetcat:
            key = ("assetcat", assetcat)
        else:
            continue
        lookup[key] = {"label": row["label"], "reason": row["reason"]}
    return lookup


def find_reviewed(lookup: dict, cusip_val, iscash_val, assetcat_val):
    c = str(cusip_val    or "").strip()
    i = str(iscash_val   or "").strip()
    a = str(assetcat_val or "").strip()
    if c and c.upper() not in ("N/A", "NA"):
        m = lookup.get(("cusip", c, i))
        if m:
            return m
    if a:
        m = lookup.get(("assetcat", a))
        if m:
            return m
    return None


_reviewed_lookup_cache = None

def apply_rules_to_group(grp: pd.DataFrame) -> dict:
    """
    Run all rules on a single filing_date partition.
    Returns a dict of {rule_key: count_flagged}.

    D1 (reviewed derivatives) and E1 (reviewed STIV) are counted separately and
    excluded from total_unique_exceptions.  N1 (unreviewed candidates) counts as Open.
    """
    global _reviewed_lookup_cache
    if _reviewed_lookup_cache is None:
        _reviewed_lookup_cache = load_reviewed_lookup()
    reviewed_lookup = _reviewed_lookup_cache

    is_derivative = grp["assetcat"].fillna("").str.upper().str.startswith("D")
    is_stiv       = grp["assetcat"].fillna("") == "STIV"
    is_candidate  = is_derivative | is_stiv

    # Per-row reviewed check
    review_matches = [
        find_reviewed(reviewed_lookup, row.get("cusip"), row.get("iscashcollateral"), row.get("assetcat"))
        for _, row in grp.iterrows()
    ]
    is_reviewed = pd.Series([m is not None for m in review_matches], index=grp.index)

    d1 = is_derivative & is_reviewed   # reviewed derivatives
    e1 = is_stiv       & is_reviewed   # reviewed STIV
    n1 = is_candidate  & ~is_reviewed  # unreviewed — counts as Open

    r1 = is_blank(grp["cusip"]) & is_blank(grp["isin"]) & ~is_derivative
    r2 = grp["pctval"].notna() & (
        (grp["pctval"] < 0) | (grp["pctval"] > 100)
    ) & ~is_derivative
    r3 = grp["valusd"].notna() & (grp["valusd"] < 0) & ~is_derivative

    # R4: same isCashCollateral bucket only
    bucket   = grp["iscashcollateral"].fillna("unknown")
    comp_key = grp["cusip"].fillna("") + "||" + bucket
    nonblank_key = comp_key[~is_blank(grp["cusip"])]
    dup_keys = nonblank_key[nonblank_key.duplicated(keep=False)].unique()
    r4 = comp_key.isin(dup_keys) & ~is_blank(grp["cusip"])

    r5 = is_blank(grp["assetcat"]) | is_blank(grp["issuercat"])

    both_pos = (
        grp["balance"].notna() & grp["valusd"].notna()
        & (grp["valusd"] > 0) & (grp["balance"] > 0)
    )
    ratio = grp["balance"] / grp["valusd"].replace(0, float("nan"))
    r6 = both_pos & (ratio >= 0.99) & (ratio <= 1.01) & ~is_stiv

    any_open = r1 | r2 | r3 | r4 | r5 | r6 | n1

    return {
        "r1_count": int(r1.sum()),
        "r2_count": int(r2.sum()),
        "r3_count": int(r3.sum()),
        "r4_count": int(r4.sum()),
        "r5_count": int(r5.sum()),
        "r6_count": int(r6.sum()),
        "d1_count": int(d1.sum()),
        "e1_count": int(e1.sum()),
        "n1_count": int(n1.sum()),
        "total_unique_exceptions": int(any_open.sum()),
    }


def main():
    # ── load ──────────────────────────────────────────────────────────────────
    print("Loading security_master_history from PostgreSQL ...")
    engine = sqlalchemy.create_engine(
        f"postgresql+psycopg2://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}"
    )
    with engine.connect() as conn:
        df = pd.read_sql("SELECT * FROM security_master_history", conn)

    for col in ("valusd", "pctval", "balance"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    print(f"  {len(df)} rows loaded across {df['filing_date'].nunique()} filing dates.")

    # ── apply rules per filing_date ───────────────────────────────────────────
    records = []
    for filing_date, grp in df.groupby("filing_date"):
        result = apply_rules_to_group(grp)
        result["filing_date"] = str(filing_date)[:10]   # keep only the date part
        records.append(result)

    out_cols = [
        "filing_date",
        "r1_count", "r2_count", "r3_count",
        "r4_count", "r5_count", "r6_count",
        "d1_count", "e1_count", "n1_count",
        "total_unique_exceptions",
    ]
    trend_df = pd.DataFrame(records)[out_cols].sort_values("filing_date").reset_index(drop=True)

    # ── load old CSV for before/after comparison ──────────────────────────────
    old_df = None
    if OUTPUT_CSV.exists():
        try:
            old_df = pd.read_csv(OUTPUT_CSV)
        except Exception:
            pass

    # ── print new table ───────────────────────────────────────────────────────
    print()
    print("=" * 78)
    print("QC EXCEPTION TREND — security_master_history (6 quarters)  [AFTER FIX]")
    print("=" * 78)
    hdr = (f"  {'Filing date':<14}  {'R1':>4}  {'R2':>4}  {'R3':>4}"
           f"  {'R4':>4}  {'R5':>4}  {'R6':>4}  {'D1':>4}  {'E1':>4}  {'N1':>4}  {'Open total':>11}")
    print(hdr)
    print("  " + "-" * 85)
    for _, row in trend_df.iterrows():
        print(
            f"  {row['filing_date']:<14}"
            f"  {row['r1_count']:>4}"
            f"  {row['r2_count']:>4}"
            f"  {row['r3_count']:>4}"
            f"  {row['r4_count']:>4}"
            f"  {row['r5_count']:>4}"
            f"  {row['r6_count']:>4}"
            f"  {row['d1_count']:>4}"
            f"  {row['e1_count']:>4}"
            f"  {row['n1_count']:>4}"
            f"  {row['total_unique_exceptions']:>11}"
        )
    print("=" * 91)

    # ── before/after comparison ───────────────────────────────────────────────
    if old_df is not None and "total_unique_exceptions" in old_df.columns:
        print()
        print("BEFORE vs AFTER  (R4/R6 scope fix + E1 classification):")
        print(f"  {'Filing date':<14}  {'Old open':>10}  {'New open':>10}  {'Expected':>10}")
        print("  " + "-" * 50)
        for _, row in trend_df.iterrows():
            fd = row["filing_date"]
            new_open     = row["total_unique_exceptions"]
            new_expected = row["d1_count"] + row["e1_count"]
            old_row = old_df[old_df["filing_date"].astype(str).str[:10] == fd]
            if not old_row.empty:
                old_total = int(old_row["total_unique_exceptions"].iloc[0])
                delta_str = f"{old_total}"
            else:
                old_total = "n/a"
                delta_str = ""
            print(f"  {fd:<14}  {str(old_total):>10}  {new_open:>10}  {new_expected:>10}")

    print()
    print("Rule key:")
    print("  R1 Both cusip AND isin blank/null           (equity/debt only)")
    print("  R2 pctVal out of range [0, 100]             (equity/debt only)")
    print("  R3 valUSD negative                          (equity/debt only)")
    print("  R4 Duplicate cusip within same isCashCollateral bucket")
    print("  R5 assetCat or issuerCat blank/null")
    print("  R6 balance ~= valUSD for non-STIV rows      (excludes $1-NAV money-market funds)")
    print("  D1 Derivative — Expected, review only       (NOT counted in total_unique_exceptions)")
    print("  E1 STIV / cash collateral — Expected        (NOT counted in total_unique_exceptions)")
    print("  N1 Unreviewed STIV or derivative            (IS counted in total_unique_exceptions)")

    # ── save ──────────────────────────────────────────────────────────────────
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    trend_df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nSaved to {OUTPUT_CSV.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
