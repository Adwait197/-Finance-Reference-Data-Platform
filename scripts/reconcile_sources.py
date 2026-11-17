"""
Module 2: Cross-source reconciliation between N-PORT and iShares holdings.

Sources compared
  N-PORT  : security_master table in PostgreSQL (filing date 2026-08-25)
  iShares : data/raw/ishares_IVV_holdings_2026-11-17.csv (holdings as of 2026-11-16)

Break detection (primary criterion)
  A row is flagged as a break when the absolute difference between N-PORT pctVal
  and iShares Market Weight exceeds WEIGHT_BREAK_THRESHOLD_PP (0.15 percentage
  points).  Portfolio weight is a better signal than raw share count across a
  date gap: weight accounts for price moves and fund-size changes, so a large
  weight discrepancy is more likely to indicate a genuine data error rather than
  normal index turnover.

Share-count comparison (informational)
  The ratio ishares_quantity / nport_balance is kept as a separate column.
  When that ratio is within 5 pct of a clean integer multiple (2x, 3x, 4x) or
  its inverse (0.5x, 0.33x, 0.25x), the row is tagged "possible stock split" --
  that label explains large share-count changes that are NOT weight breaks.

Name matching
  Canonical key: uppercase, punctuation->space, apostrophes removed, common
  corporate suffixes and share-class designators stripped.  Multi-class stocks
  (Alphabet, Berkshire, etc.) are aggregated before comparison so the
  reconciliation works at the company level.

Both pctVal (N-PORT) and Market Weight (iShares) are expressed as percentages
(e.g. 8.05 means 8.05 pct), so they are compared directly with no unit conversion.
"""

import os
import re
from pathlib import Path

import pandas as pd
import sqlalchemy
from dotenv import load_dotenv

# ── configuration ─────────────────────────────────────────────────────────────
load_dotenv()

PGHOST     = os.getenv("PGHOST", "localhost")
PGPORT     = int(os.getenv("PGPORT", "5432"))
PGDATABASE = os.getenv("PGDATABASE", "finance_platform")
PGUSER     = os.getenv("PGUSER", "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

REPO_ROOT   = Path(__file__).resolve().parent.parent
ISHARES_CSV = REPO_ROOT / "data" / "raw" / "ishares_IVV_holdings_2026-11-17.csv"
OUTPUT_CSV  = REPO_ROOT / "data" / "processed" / "reconciliation_2026-11-17.csv"

# Primary break criterion: weight difference in percentage points
WEIGHT_BREAK_THRESHOLD_PP = 0.15

# Split detection: flag when share-count ratio is within this tolerance of a
# clean multiple, e.g. a 2-for-1 split makes the ratio ≈ 2.0
SPLIT_MULTIPLES = [2.0, 3.0, 4.0, 0.5, 1.0 / 3.0, 0.25]
SPLIT_TOLERANCE = 0.05   # 5 pct


# ── name normalisation ────────────────────────────────────────────────────────

_STRIP_PATTERNS = [
    r"\bCLASS\s+[A-Z]\b",          # CLASS A, CLASS B, CLASS C …
    r"\bCL\s+[A-Z]\b",             # CL A, CL B …
    r"\bORDINARY\s+SHARES?\b",
    r"\bSHS\b",
    r"\bEQUITY\s+WARRANTS?\b",
    r"\bWARRANTS?\b",
    r"\bREIT\b",
    r"\bINC\b", r"\bCORP\b", r"\bCO\b", r"\bLTD\b",
    r"\bPLC\b", r"\bLLC\b", r"\bLP\b",
    r"\bNV\b",  r"\bSA\b",  r"\bAG\b", r"\bSE\b",
    r"\bTHE\b",
    # Strip AND so "Air Products & Chemicals" (& → space) and
    # "AIR PRODUCTS AND CHEMICALS" both normalise to "AIR PRODUCTS CHEMICALS".
    r"\bAND\b",
]
_COMPILED_PATTERNS = [re.compile(p) for p in _STRIP_PATTERNS]


def normalise(name: str) -> str:
    """Return a canonical key used for joining the two sources."""
    if pd.isna(name):
        return ""
    s = str(name).upper()
    # Remove apostrophes entirely: "Casey's" → "CASEYS", not "CASEY S"
    s = s.replace("’", "").replace("'", "")
    s = re.sub(r"[^A-Z0-9\s]", " ", s)
    for pat in _COMPILED_PATTERNS:
        s = pat.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# ── data loading ──────────────────────────────────────────────────────────────

def load_nport() -> pd.DataFrame:
    """Read security_master from PostgreSQL."""
    print("Loading security_master from PostgreSQL ...")
    engine = sqlalchemy.create_engine(
        f"postgresql+psycopg2://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}"
    )
    with engine.connect() as conn:
        df = pd.read_sql(
            "SELECT name, balance, pctval, isin, cusip FROM security_master",
            conn,
        )
    df["balance"] = pd.to_numeric(df["balance"], errors="coerce")
    df["pctval"]  = pd.to_numeric(df["pctval"],  errors="coerce")
    print(f"  {len(df)} rows loaded from security_master.")
    return df


def load_ishares() -> pd.DataFrame:
    """
    Read the iShares CSV, skip the metadata header, keep only equity rows.
    Comma-formatted numbers and dash placeholders are cleaned to numeric.
    """
    print(f"Loading iShares CSV from {ISHARES_CSV.name} ...")
    raw = ISHARES_CSV.read_text(encoding="utf-8").splitlines()
    header_idx = next(
        i for i, line in enumerate(raw)
        if line.startswith("Ticker") or line.startswith('"Ticker"')
    )
    df = pd.read_csv(ISHARES_CSV, skiprows=header_idx).dropna(how="all")
    df = df[df["Ticker"].notna() & (df["Ticker"].str.strip() != "")]

    equity_mask = df["Asset Class"].str.strip().str.lower() == "equity"
    dropped     = df[~equity_mask]
    df          = df[equity_mask].copy()
    if len(dropped):
        print(f"  Dropped {len(dropped)} non-equity rows "
              f"({dropped['Asset Class'].value_counts().to_dict()}).")

    for col in ["Quantity", "Market Value", "Market Weight"]:
        df[col] = (
            df[col].astype(str)
            .str.replace(",", "", regex=False)
            .replace("-", float("nan"))
            .pipe(pd.to_numeric, errors="coerce")
        )

    print(f"  {len(df)} equity rows kept.")
    return df[["Ticker", "Name", "Quantity", "Market Weight"]]


# ── aggregate to company level ────────────────────────────────────────────────

def build_nport_agg(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["norm_name"] = df["name"].apply(normalise)
    df = df[df["norm_name"] != ""]   # drop null-name rows
    return (
        df.groupby("norm_name", as_index=False)
        .agg(
            nport_name    =("name",    lambda s: " | ".join(sorted(str(x) for x in s.unique() if pd.notna(x)))),
            nport_balance =("balance", "sum"),
            nport_pctval  =("pctval",  "sum"),
        )
    )


def build_ishares_agg(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["norm_name"] = df["Name"].apply(normalise)
    return (
        df.groupby("norm_name", as_index=False)
        .agg(
            ishares_name    =("Name",          lambda s: " | ".join(sorted(s.unique()))),
            ishares_ticker  =("Ticker",        lambda s: " | ".join(sorted(s.unique()))),
            ishares_quantity=("Quantity",      "sum"),
            ishares_weight  =("Market Weight", "sum"),
        )
    )


# ── split detection ───────────────────────────────────────────────────────────

def _split_label(ratio: float) -> str:
    """
    Return a human-readable label if ratio is within SPLIT_TOLERANCE of a
    clean integer multiple, otherwise return an empty string.
    """
    if pd.isna(ratio) or ratio <= 0:
        return ""
    for m in SPLIT_MULTIPLES:
        if abs(ratio / m - 1) <= SPLIT_TOLERANCE:
            # Express the multiple as a fraction string for readability
            if m >= 1:
                label = f"{m:.0f}x split" if m == int(m) else f"{m:.2f}x split"
            else:
                label = f"1/{round(1/m):.0f}x split"
            return f"possible stock split ({label})"
    return ""


# ── reconciliation ────────────────────────────────────────────────────────────

def reconcile(nport: pd.DataFrame, ishares: pd.DataFrame) -> pd.DataFrame:
    merged = pd.merge(nport, ishares, on="norm_name", how="outer")

    # -- weight difference (primary break signal) --
    merged["weight_diff"]     = merged["ishares_weight"] - merged["nport_pctval"]
    merged["weight_diff_abs"] = merged["weight_diff"].abs()

    # -- share-count comparison (informational) --
    merged["share_diff"]     = merged["ishares_quantity"] - merged["nport_balance"]
    merged["share_diff_pct"] = (
        merged["share_diff"].abs() / merged["nport_balance"] * 100
    )
    # Ratio of iShares quantity to N-PORT balance (for split detection)
    merged["share_ratio"] = merged["ishares_quantity"] / merged["nport_balance"]

    # -- split flag --
    merged["split_flag"] = merged["share_ratio"].apply(_split_label)

    # -- primary break flag: |weight_diff| > threshold, matched rows only --
    both_have_weight = merged["nport_pctval"].notna() & merged["ishares_weight"].notna()
    merged["is_break"] = both_have_weight & (merged["weight_diff_abs"] > WEIGHT_BREAK_THRESHOLD_PP)

    # -- match status --
    def _status(row):
        if pd.isna(row["nport_balance"]):
            return "ishares_only"
        if pd.isna(row["ishares_quantity"]):
            return "nport_only"
        return "matched"

    merged["match_status"] = merged.apply(_status, axis=1)

    cols = [
        "norm_name",
        "nport_name", "ishares_name", "ishares_ticker",
        # Weight columns first — primary break signal
        "nport_pctval", "ishares_weight", "weight_diff", "weight_diff_abs",
        # Share columns — informational
        "nport_balance", "ishares_quantity", "share_diff", "share_diff_pct",
        # Split and break flags
        "share_ratio", "split_flag", "is_break", "match_status",
    ]
    return merged[cols].sort_values("weight_diff_abs", ascending=False, na_position="last")


# ── reporting ─────────────────────────────────────────────────────────────────

def print_summary(df: pd.DataFrame) -> None:
    matched      = df[df["match_status"] == "matched"]
    breaks       = matched[matched["is_break"]]
    splits       = matched[matched["split_flag"] != ""]
    nport_only   = df[df["match_status"] == "nport_only"]
    ishares_only = df[df["match_status"] == "ishares_only"]

    print()
    print("=" * 72)
    print("RECONCILIATION SUMMARY")
    print("  N-PORT date  : 2026-08-25  (SEC EDGAR N-PORT filing)")
    print("  iShares date : 2026-11-16  (iShares public CSV)")
    print(f"  Break rule   : |pctVal - Market Weight| > {WEIGHT_BREAK_THRESHOLD_PP} pp")
    print("=" * 72)
    print(f"  Total matched rows                       : {len(matched):>5}")
    print(f"  Breaks flagged (weight-based rule)       : {len(breaks):>5}")
    print(f"  Possible stock splits (share ratio rule) : {len(splits):>5}")
    print(f"  Unmatched -- N-PORT only                 : {len(nport_only):>5}")
    print(f"  Unmatched -- iShares only                : {len(ishares_only):>5}")
    print()

    print(f"Top 10 breaks sorted by absolute weight difference:")
    print(f"  {'Company':<30}  {'N-PORT %':>9}  {'iShares %':>9}  {'diff pp':>8}  {'share ratio':>12}  split?")
    print("  " + "-" * 85)
    for _, row in breaks.head(10).iterrows():
        split_note = "YES" if row["split_flag"] else ""
        print(
            f"  {row['norm_name']:<30}"
            f"  {row['nport_pctval']:>9.4f}"
            f"  {row['ishares_weight']:>9.4f}"
            f"  {row['weight_diff']:>+8.4f}"
            f"  {row['share_ratio']:>12.3f}"
            f"  {split_note}"
        )

    if len(nport_only):
        print(f"\nN-PORT only (first 10):")
        for _, row in nport_only.head(10).iterrows():
            print(f"  {row['nport_name']}")

    if len(ishares_only):
        print(f"\niShares only (first 10):")
        for _, row in ishares_only.head(10).iterrows():
            print(f"  {row['ishares_name']}  [{row['ishares_ticker']}]")

    print()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    nport_raw   = load_nport()
    ishares_raw = load_ishares()

    print("\nNormalising names and aggregating to company level ...")
    nport_agg   = build_nport_agg(nport_raw)
    ishares_agg = build_ishares_agg(ishares_raw)
    print(f"  N-PORT  : {len(nport_raw)} rows -> {len(nport_agg)} unique normalised names")
    print(f"  iShares : {len(ishares_raw)} rows -> {len(ishares_agg)} unique normalised names")

    print("\nJoining and computing differences ...")
    result = reconcile(nport_agg, ishares_agg)

    print_summary(result)

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT_CSV, index=False, float_format="%.6f")
    print(f"Full comparison table saved to {OUTPUT_CSV.relative_to(REPO_ROOT)}")
    print(f"  {len(result)} total rows  |  columns: {list(result.columns)}")


if __name__ == "__main__":
    main()
