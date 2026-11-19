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

REPO_ROOT  = Path(__file__).resolve().parent.parent
OUTPUT_CSV = REPO_ROOT / "data" / "processed" / "qc_trend_by_quarter.csv"


# ── helpers ────────────────────────────────────────────────────────────────────

def is_blank(s: pd.Series) -> pd.Series:
    nulls  = s.isna()
    blanks = s.fillna("").astype(str).str.strip().isin(["", "N/A", "n/a", "NA"])
    return nulls | blanks


def apply_rules_to_group(grp: pd.DataFrame) -> dict:
    """
    Run all rules on a single filing_date partition.
    Returns a dict of {rule_key: count_flagged}.
    D1 rows are counted separately and excluded from total_unique_exceptions.
    """
    # Derivatives: exchange-traded futures have no CUSIP/ISIN and their valUSD
    # is unrealised P&L (can be negative) — R1/R2/R3 are false positives for them.
    is_derivative = grp["assetcat"].fillna("").str.upper().str.startswith("D")

    r1 = is_blank(grp["cusip"]) & is_blank(grp["isin"]) & ~is_derivative
    r2 = grp["pctval"].notna() & (
        (grp["pctval"] < 0) | (grp["pctval"] > 100)
    ) & ~is_derivative
    r3 = grp["valusd"].notna() & (grp["valusd"] < 0) & ~is_derivative

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

    # D1: derivative rows — informational, not counted as errors
    d1 = is_derivative

    any_error = r1 | r2 | r3 | r4 | r5 | r6   # D1 excluded from error total

    return {
        "r1_count": int(r1.sum()),
        "r2_count": int(r2.sum()),
        "r3_count": int(r3.sum()),
        "r4_count": int(r4.sum()),
        "r5_count": int(r5.sum()),
        "r6_count": int(r6.sum()),
        "d1_count": int(d1.sum()),
        "total_unique_exceptions": int(any_error.sum()),
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
        "d1_count",
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
           f"  {'R4':>4}  {'R5':>4}  {'R6':>4}  {'D1':>4}  {'Total errors':>13}")
    print(hdr)
    print("  " + "-" * 72)
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
            f"  {row['total_unique_exceptions']:>13}"
        )
    print("=" * 78)

    # ── before/after comparison ───────────────────────────────────────────────
    if old_df is not None and "total_unique_exceptions" in old_df.columns:
        print()
        print("BEFORE vs AFTER (total_unique_exceptions):")
        print(f"  {'Filing date':<14}  {'Before':>8}  {'After':>8}  {'Delta':>8}")
        print("  " + "-" * 42)
        for _, row in trend_df.iterrows():
            fd = row["filing_date"]
            new_total = row["total_unique_exceptions"]
            old_row = old_df[old_df["filing_date"].astype(str).str[:10] == fd]
            if not old_row.empty:
                old_total = int(old_row["total_unique_exceptions"].iloc[0])
                delta = new_total - old_total
                delta_str = f"{delta:+d}" if delta != 0 else "  —"
            else:
                old_total = "n/a"
                delta_str = ""
            print(f"  {fd:<14}  {str(old_total):>8}  {new_total:>8}  {delta_str:>8}")

    print()
    print("Rule key:")
    print("  R1 Both cusip AND isin blank/null           (equity/debt only)")
    print("  R2 pctVal out of range [0, 100]             (equity/debt only)")
    print("  R3 valUSD negative                          (equity/debt only)")
    print("  R4 Duplicate non-blank cusip (within same quarter)")
    print("  R5 assetCat or issuerCat blank/null")
    print("  R6 balance ~= valUSD (possible $1-per-unit money-market instrument)")
    print("  D1 Derivative row — expected, review only (NOT counted in total errors)")

    # ── save ──────────────────────────────────────────────────────────────────
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    trend_df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nSaved to {OUTPUT_CSV.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
