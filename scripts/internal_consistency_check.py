"""
Internal consistency check for security_master (N-PORT filing 2026-08-25).

Verifies that each holding's stored pctVal is consistent with the weight
implied by its own valUSD relative to the total portfolio value.

  implied_pct = (valUSD / sum(valUSD)) * 100

Both pctVal and implied_pct are in percentage units (e.g. 0.385 means 0.385%).
A discrepancy > 0.01 pp suggests a data-entry error, a stale pctVal carried
over from a different NAV date, or a rounding difference in the SEC filing.
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
OUTPUT_CSV = REPO_ROOT / "data" / "processed" / "internal_consistency_2026-08-25.csv"

THRESHOLD_PP = 0.01   # flag rows where |implied_pct - pctval| > this


def main():
    # ── load from PostgreSQL ─────────────────────────────────────────────────
    print("Loading security_master from PostgreSQL ...")
    engine = sqlalchemy.create_engine(
        f"postgresql+psycopg2://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}"
    )
    with engine.connect() as conn:
        df = pd.read_sql(
            "SELECT id, name, cusip, isin, valusd, pctval FROM security_master",
            conn,
        )
    for col in ("valusd", "pctval"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    print(f"  {len(df)} rows loaded.")

    # ── compute implied weight ────────────────────────────────────────────────
    total_valusd = df["valusd"].sum()
    print(f"  Total portfolio valUSD : ${total_valusd:,.2f}")

    df["implied_pct"] = (df["valusd"] / total_valusd) * 100

    # ── compare ──────────────────────────────────────────────────────────────
    df["pct_diff"]     = df["implied_pct"] - df["pctval"]
    df["pct_diff_abs"] = df["pct_diff"].abs()
    df["flag"]         = df["pct_diff_abs"] > THRESHOLD_PP

    flagged = df[df["flag"]]
    clean   = df[~df["flag"] & df["pctval"].notna() & df["implied_pct"].notna()]

    print()
    print("=" * 60)
    print("INTERNAL CONSISTENCY SUMMARY")
    print(f"  Filing date       : 2026-08-25")
    print(f"  Total rows        : {len(df)}")
    print(f"  Threshold         : {THRESHOLD_PP} pp")
    print(f"  Rows within tolerance (clean) : {len(clean)}")
    print(f"  Rows flagged (diff > {THRESHOLD_PP} pp)  : {len(flagged)}")
    print(f"  Rows with null pctVal or valUSD: {df['pctval'].isna().sum() + df['valusd'].isna().sum()}")
    print("=" * 60)

    if len(flagged):
        print(f"\nFlagged rows (sorted by |diff|):")
        print(f"  {'Name':<45}  {'pctVal':>8}  {'implied':>8}  {'diff':>8}")
        print("  " + "-" * 74)
        for _, row in flagged.sort_values("pct_diff_abs", ascending=False).iterrows():
            print(
                f"  {str(row['name']):<45}"
                f"  {row['pctval']:>8.4f}"
                f"  {row['implied_pct']:>8.4f}"
                f"  {row['pct_diff']:>+8.4f}"
            )
    else:
        print("\nAll rows are within tolerance — pctVal is internally consistent.")

    # ── save ─────────────────────────────────────────────────────────────────
    out_cols = ["id", "name", "cusip", "isin", "valusd", "pctval", "implied_pct", "pct_diff", "pct_diff_abs", "flag"]
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df[out_cols].sort_values("pct_diff_abs", ascending=False).to_csv(OUTPUT_CSV, index=False, float_format="%.6f")
    print(f"\nFull results saved to {OUTPUT_CSV.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
