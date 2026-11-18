"""
Module 3: Automated QC rule engine — deterministic checks on security_master.

Rules applied
  R1  Both cusip AND isin are blank / null
  R2  pctval is negative or greater than 100
  R3  valusd is negative
  R4  Non-null cusip value appears on more than one row (duplicate)
  R5  assetcat OR issuercat is blank / null

A row that fails multiple rules is listed once with every failing rule noted
in the 'rules_failed' column (pipe-separated).

Output: data/processed/qc_exceptions_2026-08-25.csv — only flagged rows.
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
OUTPUT_CSV = REPO_ROOT / "data" / "processed" / "qc_exceptions_2026-08-25.csv"

EXAMPLE_ROWS = 3   # how many example rows to print per rule


# ── helpers ───────────────────────────────────────────────────────────────────

def is_blank(s: pd.Series) -> pd.Series:
    """True where a text column is null, empty string, whitespace-only, or 'N/A'."""
    nulls  = s.isna()
    blanks = s.fillna("").astype(str).str.strip().isin(["", "N/A", "n/a", "NA"])
    return nulls | blanks


# ── data loading ──────────────────────────────────────────────────────────────

def load_data() -> pd.DataFrame:
    print("Loading security_master from PostgreSQL ...")
    engine = sqlalchemy.create_engine(
        f"postgresql+psycopg2://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}"
    )
    with engine.connect() as conn:
        df = pd.read_sql("SELECT * FROM security_master", conn)
    for col in ("valusd", "pctval", "balance"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    print(f"  {len(df)} rows loaded.")
    return df


# ── rule definitions ──────────────────────────────────────────────────────────

def apply_rules(df: pd.DataFrame) -> pd.DataFrame:
    """
    Evaluate all rules and return df with a 'rules_failed' column.
    Rows that pass every rule have an empty string in 'rules_failed'.
    """
    masks = {}

    # R1 — both identifier fields blank
    masks["R1_no_identifier"] = is_blank(df["cusip"]) & is_blank(df["isin"])

    # R2 — pctval out of valid range [0, 100]
    masks["R2_pctval_range"] = df["pctval"].notna() & (
        (df["pctval"] < 0) | (df["pctval"] > 100)
    )

    # R3 — negative valUSD
    masks["R3_valusd_negative"] = df["valusd"].notna() & (df["valusd"] < 0)

    # R4 — duplicate non-blank cusip
    non_blank_cusip = df["cusip"][~is_blank(df["cusip"])]
    dup_cusips      = non_blank_cusip[non_blank_cusip.duplicated(keep=False)].unique()
    masks["R4_duplicate_cusip"] = df["cusip"].isin(dup_cusips)

    # R5 — blank assetcat or issuercat
    masks["R5_category_blank"] = is_blank(df["assetcat"]) | is_blank(df["issuercat"])

    # Build rules_failed column
    def _rules_for_row(idx):
        return " | ".join(rule for rule, mask in masks.items() if mask.iloc[idx])

    df = df.copy()
    df["rules_failed"] = [_rules_for_row(i) for i in range(len(df))]

    return df, masks


# ── reporting ─────────────────────────────────────────────────────────────────

def print_rule_detail(rule_name: str, mask: pd.Series, df: pd.DataFrame) -> None:
    flagged = df[mask]
    print(f"\n{rule_name}  —  {len(flagged)} row(s) flagged")
    if len(flagged) == 0:
        print("  (none)")
        return
    show_cols = ["id", "name", "cusip", "isin", "pctval", "valusd", "assetcat", "issuercat"]
    show_cols = [c for c in show_cols if c in flagged.columns]
    print(flagged[show_cols].head(EXAMPLE_ROWS).to_string(index=False))
    if len(flagged) > EXAMPLE_ROWS:
        print(f"  ... and {len(flagged) - EXAMPLE_ROWS} more row(s)")


def print_summary(masks: dict) -> None:
    total_flagged = sum(1 for i in range(len(next(iter(masks.values())))) if any(m.iloc[i] for m in masks.values()))
    print()
    print("=" * 50)
    print("QC SUMMARY — security_master (2026-08-25)")
    print("=" * 50)
    print(f"  {'Rule':<30}  {'Rows Flagged':>12}")
    print("  " + "-" * 44)
    for rule, mask in masks.items():
        print(f"  {rule:<30}  {mask.sum():>12,}")
    print("  " + "-" * 44)
    print(f"  {'TOTAL UNIQUE FLAGGED ROWS':<30}  {total_flagged:>12,}")
    print("=" * 50)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    df = load_data()
    df, masks = apply_rules(df)

    # Per-rule detail with examples
    for rule_name, mask in masks.items():
        print_rule_detail(rule_name, mask, df)

    print_summary(masks)

    # Save flagged rows only
    flagged = df[df["rules_failed"] != ""].copy()
    flagged = flagged.sort_values("rules_failed")

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    flagged.to_csv(OUTPUT_CSV, index=False, float_format="%.6f")
    print(f"\nFlagged rows saved to {OUTPUT_CSV.relative_to(REPO_ROOT)}")
    print(f"  {len(flagged)} rows  |  columns: {list(flagged.columns)}")


if __name__ == "__main__":
    main()
