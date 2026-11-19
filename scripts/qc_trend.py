"""
Module 4: QC exception trend across 6 quarterly N-PORT filings.

Applies the same 6 deterministic rules as qc_rules.py to
security_master_history, grouped by filing_date.  R4 (duplicate cusip)
is evaluated within each filing_date partition, not across the full table,
because a cusip can legitimately appear in every quarter without being a
data-quality error — only same-quarter duplicates are real breaks.

Output: data/processed/qc_trend_by_quarter.csv
  Columns: filing_date, r1_count, r2_count, r3_count, r4_count,
           r5_count, r6_count, total_unique_exceptions
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

REPO_ROOT  = Path(__file__).resolve().parent.parent
OUTPUT_CSV = REPO_ROOT / "data" / "processed" / "qc_trend_by_quarter.csv"


# ── helpers ────────────────────────────────────────────────────────────────────

def is_blank(s: pd.Series) -> pd.Series:
    nulls  = s.isna()
    blanks = s.fillna("").astype(str).str.strip().isin(["", "N/A", "n/a", "NA"])
    return nulls | blanks


def apply_rules_to_group(grp: pd.DataFrame) -> dict:
    """
    Run all 6 rules on a single filing_date partition.
    Returns a dict of {rule_key: count_flagged}.
    """
    r1 = is_blank(grp["cusip"]) & is_blank(grp["isin"])

    r2 = grp["pctval"].notna() & (
        (grp["pctval"] < 0) | (grp["pctval"] > 100)
    )

    r3 = grp["valusd"].notna() & (grp["valusd"] < 0)

    # R4: duplicate non-blank cusip within this filing_date only
    non_blank = grp["cusip"][~is_blank(grp["cusip"])]
    dup_cusips = non_blank[non_blank.duplicated(keep=False)].unique()
    r4 = grp["cusip"].isin(dup_cusips)

    r5 = is_blank(grp["assetcat"]) | is_blank(grp["issuercat"])

    both_pos = (
        grp["balance"].notna() & grp["valusd"].notna()
        & (grp["valusd"] > 0) & (grp["balance"] > 0)
    )
    ratio = grp["balance"] / grp["valusd"]
    r6 = both_pos & (ratio >= 0.99) & (ratio <= 1.01)

    any_flag = r1 | r2 | r3 | r4 | r5 | r6

    return {
        "r1_count": int(r1.sum()),
        "r2_count": int(r2.sum()),
        "r3_count": int(r3.sum()),
        "r4_count": int(r4.sum()),
        "r5_count": int(r5.sum()),
        "r6_count": int(r6.sum()),
        "total_unique_exceptions": int(any_flag.sum()),
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
        "total_unique_exceptions",
    ]
    trend_df = pd.DataFrame(records)[out_cols].sort_values("filing_date").reset_index(drop=True)

    # ── print ─────────────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("QC EXCEPTION TREND — security_master_history (6 quarters)")
    print("=" * 70)
    hdr = (f"  {'Filing date':<14}  {'R1':>4}  {'R2':>4}  {'R3':>4}"
           f"  {'R4':>4}  {'R5':>4}  {'R6':>4}  {'Total exceptions':>18}")
    print(hdr)
    print("  " + "-" * 64)
    for _, row in trend_df.iterrows():
        print(
            f"  {row['filing_date']:<14}"
            f"  {row['r1_count']:>4}"
            f"  {row['r2_count']:>4}"
            f"  {row['r3_count']:>4}"
            f"  {row['r4_count']:>4}"
            f"  {row['r5_count']:>4}"
            f"  {row['r6_count']:>4}"
            f"  {row['total_unique_exceptions']:>18}"
        )
    print("=" * 70)
    print()
    print("Rule key:")
    print("  R1 Both cusip AND isin blank/null")
    print("  R2 pctVal out of range [0, 100]")
    print("  R3 valUSD negative")
    print("  R4 Duplicate non-blank cusip (within same quarter)")
    print("  R5 assetCat or issuerCat blank/null")
    print("  R6 balance ~= valUSD (possible $1-per-unit money-market instrument)")

    # ── save ──────────────────────────────────────────────────────────────────
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    trend_df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nSaved to {OUTPUT_CSV.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
