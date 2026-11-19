"""
Module 3: Automated QC rule engine — deterministic checks on security_master.

Rules applied
  R1  Both cusip AND isin are blank / null           (equity/debt rows only)
  R2  pctval is negative or greater than 100         (equity/debt rows only)
  R3  valusd is negative                             (equity/debt rows only)
  R4  Non-null cusip value appears on more than one row (duplicate)
  R5  assetcat OR issuercat is blank / null
  R6  balance is within 1% of valUSD (ratio 0.99–1.01), indicating a
      $1-per-unit money-market or cash instrument that may be miscategorized
      as an equity holding; derived from the IsolationForest finding for
      id=415 (BlackRock Funds III, CUSIP 066922477)
  D1  Derivative row (assetCat starts with 'D') — expected, review only.
      Derivatives are EXCLUDED from R1/R2/R3 because exchange-traded futures
      have no CUSIP or ISIN (they are contractual rights, not issued
      securities), and their valUSD represents unrealised P&L which can
      legitimately be negative when the position is marked against the fund.

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
    Evaluate all rules; return (df_with_annotations, masks).

    df gets three new columns:
      rules_failed  — pipe-separated internal rule keys for every rule that fired
      status        — "Open" (R1-R6 violation needs action) | "Expected" (D1/E1) | ""
      reason        — plain-English note for Expected rows
    """
    masks = {}

    # D1: derivatives (assetCat starting with 'D', e.g. 'DE' for exchange-traded futures).
    # Futures have no CUSIP/ISIN and their valUSD is unrealised P&L (can be negative)
    # — R1/R2/R3 are false positives for them.
    is_derivative = df["assetcat"].fillna("").str.upper().str.startswith("D")

    # E1: STIV (Short-Term Investment Vehicle) = money-market fund or reinvested cash
    # collateral from securities lending.  IVV lends stocks and reinvests the received
    # cash in STIV instruments — this is intentional fund structure, not a data error.
    # Previously caught by old R4 (cross-bucket CUSIP duplicate) and old R6 (balance ≈
    # valUSD on a $1-NAV fund); now reclassified as Expected so the rules don't fire.
    is_stiv = df["assetcat"].fillna("") == "STIV"

    # R1 — both identifier fields blank (derivatives excluded)
    masks["R1_no_identifier"] = is_blank(df["cusip"]) & is_blank(df["isin"]) & ~is_derivative

    # R2 — pctval out of valid range [0, 100] (derivatives excluded)
    masks["R2_pctval_range"] = df["pctval"].notna() & (
        (df["pctval"] < 0) | (df["pctval"] > 100)
    ) & ~is_derivative

    # R3 — negative valUSD (derivatives excluded)
    masks["R3_valusd_negative"] = df["valusd"].notna() & (df["valusd"] < 0) & ~is_derivative

    # R4 — duplicate non-blank CUSIP within the same isCashCollateral bucket.
    # An index ETF can legitimately hold the same security twice: once as a direct
    # portfolio position (isCashCollateral=N) and once as reinvested cash collateral
    # (isCashCollateral=Y).  Cross-bucket pairs are intentional; only same-bucket
    # duplicates are real errors.
    bucket   = df["iscashcollateral"].fillna("unknown")
    comp_key = df["cusip"].fillna("") + "||" + bucket
    nonblank_key = comp_key[~is_blank(df["cusip"])]
    dup_keys = nonblank_key[nonblank_key.duplicated(keep=False)].unique()
    masks["R4_duplicate_cusip"] = comp_key.isin(dup_keys) & ~is_blank(df["cusip"])

    # R5 — blank assetcat or issuercat
    masks["R5_category_blank"] = is_blank(df["assetcat"]) | is_blank(df["issuercat"])

    # R6 — balance within 1% of valUSD for non-STIV rows.
    # STIV money-market funds have $1-NAV so balance = valUSD by design — R6 would
    # fire on every STIV row and produce only false positives.
    both_positive = df["balance"].notna() & df["valusd"].notna() & (df["valusd"] > 0) & (df["balance"] > 0)
    ratio = df["balance"] / df["valusd"].replace(0, float("nan"))
    masks["R6_possible_cash_mm"] = both_positive & (ratio >= 0.99) & (ratio <= 1.01) & ~is_stiv

    # D1 — derivative row: Expected, excluded from error totals
    masks["D1_derivative_review"] = is_derivative

    # E1 — STIV money-market / cash collateral: Expected, excluded from error totals
    masks["E1_cash_collateral_mm"] = is_stiv

    any_open = (
        masks["R1_no_identifier"] | masks["R2_pctval_range"] | masks["R3_valusd_negative"]
        | masks["R4_duplicate_cusip"] | masks["R5_category_blank"] | masks["R6_possible_cash_mm"]
    )

    def _rules_for_row(idx):
        return " | ".join(rule for rule, mask in masks.items() if mask.iloc[idx])

    def _status_for_row(idx):
        if masks["D1_derivative_review"].iloc[idx] or masks["E1_cash_collateral_mm"].iloc[idx]:
            return "Expected"
        if any_open.iloc[idx]:
            return "Open"
        return ""

    def _reason_for_row(idx):
        if masks["D1_derivative_review"].iloc[idx]:
            return (
                "Exchange-traded derivative (futures contract): "
                "no CUSIP/ISIN by design; valUSD = unrealised P&L (can be negative)"
            )
        if masks["E1_cash_collateral_mm"].iloc[idx]:
            return (
                "Short-Term Investment Vehicle (STIV): money-market fund or reinvested "
                "cash collateral from securities lending — expected fund structure for "
                "a large index ETF"
            )
        return ""

    df = df.copy()
    df["rules_failed"] = [_rules_for_row(i) for i in range(len(df))]
    df["status"]       = [_status_for_row(i) for i in range(len(df))]
    df["reason"]       = [_reason_for_row(i) for i in range(len(df))]

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


def print_summary(masks: dict, df: pd.DataFrame) -> None:
    open_rules = ["R1_no_identifier", "R2_pctval_range", "R3_valusd_negative",
                  "R4_duplicate_cusip", "R5_category_blank", "R6_possible_cash_mm"]
    any_open    = pd.concat([masks[r] for r in open_rules if r in masks], axis=1).any(axis=1)
    is_expected = masks.get("D1_derivative_review", pd.Series(False, index=df.index)) | \
                  masks.get("E1_cash_collateral_mm", pd.Series(False, index=df.index))

    n_open     = int((any_open & ~is_expected).sum())
    n_expected = int(is_expected.sum())

    print()
    print("=" * 56)
    print("QC SUMMARY — security_master (2026-08-25)")
    print("=" * 56)
    print(f"  {'Rule':<34}  {'Rows Flagged':>12}")
    print("  " + "-" * 50)
    for rule, mask in masks.items():
        print(f"  {rule:<34}  {mask.sum():>12,}")
    print("  " + "-" * 50)
    print(f"  {'OPEN (R1–R6, need action)':<34}  {n_open:>12,}")
    print(f"  {'EXPECTED (D1+E1, reviewed)':<34}  {n_expected:>12,}")
    print("=" * 56)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    df = load_data()
    df, masks = apply_rules(df)

    # Per-rule detail with examples
    for rule_name, mask in masks.items():
        print_rule_detail(rule_name, mask, df)

    print_summary(masks, df)

    # Save flagged rows only
    flagged = df[df["rules_failed"] != ""].copy()
    flagged = flagged.sort_values("rules_failed")

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    flagged.to_csv(OUTPUT_CSV, index=False, float_format="%.6f")
    print(f"\nFlagged rows saved to {OUTPUT_CSV.relative_to(REPO_ROOT)}")
    print(f"  {len(flagged)} rows  |  columns: {list(flagged.columns)}")


if __name__ == "__main__":
    main()
