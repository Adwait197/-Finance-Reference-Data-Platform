"""
Module 2: Cross-source reconciliation between N-PORT and iShares holdings.

Sources compared
  N-PORT  : security_master table in PostgreSQL (filing date 2026-08-25)
  iShares : data/raw/ishares_IVV_holdings_2026-11-17.csv (holdings as of 2026-11-16)

NOTE: The two sources are ~84 days apart. Share-count differences reflect
both genuine data breaks AND normal portfolio turnover / index rebalancing.
Weight (pctVal vs Market Weight) differences are expected to be large because
the market has moved significantly in that window. The 1%-of-N-PORT-balance
threshold flags share-count differences that are too large to be explained
by rounding and are therefore worth investigating.

Both pctVal (N-PORT) and Market Weight (iShares) are expressed as percentages
(e.g. 8.05 means 8.05 pct), so they are compared directly with no unit conversion.

Name matching uses a normalised key: uppercase, punctuation replaced by spaces,
common corporate suffixes and share-class designators stripped. Multi-class
stocks (Alphabet, Berkshire, etc.) are aggregated before comparison so the
reconciliation works at the company level rather than the share-class level.
"""

import os
import re
import sys
from pathlib import Path

import pandas as pd
import psycopg2
import sqlalchemy
from dotenv import load_dotenv

# ── configuration ─────────────────────────────────────────────────────────────
load_dotenv()

PGHOST     = os.getenv("PGHOST", "localhost")
PGPORT     = int(os.getenv("PGPORT", "5432"))
PGDATABASE = os.getenv("PGDATABASE", "finance_platform")
PGUSER     = os.getenv("PGUSER", "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

REPO_ROOT    = Path(__file__).resolve().parent.parent
ISHARES_CSV  = REPO_ROOT / "data" / "raw" / "ishares_IVV_holdings_2026-11-17.csv"
OUTPUT_CSV   = REPO_ROOT / "data" / "processed" / "reconciliation_2026-11-17.csv"

BREAK_THRESHOLD_PCT = 1.0   # flag rows where |balance - Quantity| / balance > 1 pct


# ── name normalisation ────────────────────────────────────────────────────────

# Patterns stripped in order (longer/more-specific first).
_STRIP_PATTERNS = [
    r"\bCLASS\s+[A-Z]\b",          # CLASS A, CLASS B, CLASS C …
    r"\bCL\s+[A-Z]\b",             # CL A, CL B …
    r"\bORDINARY\s+SHARES?\b",
    r"\bSHS\b",                     # SHS (shares abbreviation)
    r"\bEQUITY\s+WARRANTS?\b",
    r"\bWARRANTS?\b",
    r"\bREIT\b",                    # Real Estate Investment Trust label
    r"\bINC\b", r"\bCORP\b", r"\bCO\b", r"\bLTD\b",
    r"\bPLC\b", r"\bLLC\b", r"\bLP\b",
    r"\bNV\b",  r"\bSA\b",  r"\bAG\b", r"\bSE\b",
    r"\bTHE\b",
    # 'AND' is stripped so that 'Air Products & Chemicals' (& → space) and
    # 'AIR PRODUCTS AND CHEMICALS' both normalise to 'AIR PRODUCTS CHEMICALS'.
    r"\bAND\b",
]
_COMPILED_PATTERNS = [re.compile(p) for p in _STRIP_PATTERNS]


def normalise(name: str) -> str:
    """Return a canonical key used for joining the two sources."""
    if pd.isna(name):
        return ""
    s = str(name).upper()
    # Remove apostrophes entirely so "Casey's" → "CASEYS", not "CASEY S"
    s = s.replace("’", "").replace("'", "")
    s = re.sub(r"[^A-Z0-9\s]", " ", s)   # remaining punctuation → space
    for pat in _COMPILED_PATTERNS:
        s = pat.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ── data loading ──────────────────────────────────────────────────────────────

def load_nport() -> pd.DataFrame:
    """Read security_master from PostgreSQL and return a tidy DataFrame."""
    print("Loading security_master from PostgreSQL …")
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
    Read the iShares CSV, skip the 9-row metadata header, keep only equity
    rows, and return a tidy DataFrame.

    The file has comma-formatted numbers ("196,300,873.00") and some rows
    with "-" in Market Weight (recently-added constituents with no weight
    history); those become NaN.
    """
    print(f"Loading iShares CSV from {ISHARES_CSV.name} …")

    # Detect the header row (first line starting with 'Ticker')
    raw = ISHARES_CSV.read_text(encoding="utf-8").splitlines()
    header_idx = next(
        i for i, line in enumerate(raw)
        if line.startswith("Ticker") or line.startswith('"Ticker"')
    )

    df = pd.read_csv(ISHARES_CSV, skiprows=header_idx)
    df = df.dropna(how="all")  # drop blank rows

    # Drop the trailing legal-disclaimer row (it has no Ticker)
    df = df[df["Ticker"].notna() & (df["Ticker"].str.strip() != "")]

    # Keep only equity rows — Cash, Futures, and Collateral are not securities
    equity_mask = df["Asset Class"].str.strip().str.lower() == "equity"
    non_equity  = df[~equity_mask]
    df          = df[equity_mask].copy()
    if len(non_equity):
        print(f"  Dropped {len(non_equity)} non-equity rows "
              f"({non_equity['Asset Class'].value_counts().to_dict()}).")

    # Parse numeric columns
    for col in ["Quantity", "Market Value", "Market Weight"]:
        df[col] = (
            df[col]
            .astype(str)
            .str.replace(",", "", regex=False)
            .replace("-", float("nan"))
            .pipe(pd.to_numeric, errors="coerce")
        )

    print(f"  {len(df)} equity rows kept.")
    return df[["Ticker", "Name", "Quantity", "Market Weight"]]


# ── normalise and aggregate ───────────────────────────────────────────────────

def build_nport_agg(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add normalised name key and aggregate to company level.
    Some S&P 500 companies have multiple share classes; both appear in the
    N-PORT with the same legal name, so we sum their shares and weights.
    """
    df = df.copy()
    df["norm_name"] = df["name"].apply(normalise)

    # Rows whose name is null have no meaningful key; drop them before aggregating
    df = df[df["norm_name"] != ""]

    agg = (
        df.groupby("norm_name", as_index=False)
        .agg(
            nport_name=("name",    lambda s: " | ".join(sorted(str(x) for x in s.unique() if pd.notna(x)))),
            nport_balance=("balance", "sum"),
            nport_pctval=("pctval",  "sum"),
        )
    )
    return agg


def build_ishares_agg(df: pd.DataFrame) -> pd.DataFrame:
    """Add normalised name key and aggregate to company level."""
    df = df.copy()
    df["norm_name"] = df["Name"].apply(normalise)

    agg = (
        df.groupby("norm_name", as_index=False)
        .agg(
            ishares_name=("Name",          lambda s: " | ".join(sorted(s.unique()))),
            ishares_ticker=("Ticker",      lambda s: " | ".join(sorted(s.unique()))),
            ishares_quantity=("Quantity",  "sum"),
            ishares_weight=("Market Weight", "sum"),
        )
    )
    return agg


# ── reconciliation ────────────────────────────────────────────────────────────

def reconcile(nport: pd.DataFrame, ishares: pd.DataFrame) -> pd.DataFrame:
    """
    Outer-join the two aggregated frames on norm_name, compute differences,
    and tag each row as matched / break / nport_only / ishares_only.
    """
    merged = pd.merge(nport, ishares, on="norm_name", how="outer")

    # -- share-count difference (N-PORT balance vs iShares Quantity) --
    merged["share_diff"]     = merged["ishares_quantity"] - merged["nport_balance"]
    merged["share_diff_abs"] = merged["share_diff"].abs()

    # Percent of N-PORT balance — only meaningful for matched rows
    merged["share_diff_pct"] = (
        merged["share_diff_abs"] / merged["nport_balance"] * 100
    )

    # -- weight difference (pctVal vs Market Weight, both in pct) --
    merged["weight_diff"] = merged["ishares_weight"] - merged["nport_pctval"]

    # -- break flag: share-count difference > 1 pct of N-PORT balance --
    merged["is_break"] = (
        merged["nport_balance"].notna()
        & merged["ishares_quantity"].notna()
        & (merged["share_diff_pct"] > BREAK_THRESHOLD_PCT)
    )

    # -- match status --
    def _status(row):
        if pd.isna(row["nport_balance"]):
            return "ishares_only"
        if pd.isna(row["ishares_quantity"]):
            return "nport_only"
        return "matched"

    merged["match_status"] = merged.apply(_status, axis=1)

    # -- tidy column order --
    cols = [
        "norm_name",
        "nport_name", "ishares_name", "ishares_ticker",
        "nport_balance", "ishares_quantity",
        "share_diff", "share_diff_abs", "share_diff_pct",
        "nport_pctval", "ishares_weight", "weight_diff",
        "is_break", "match_status",
    ]
    return merged[cols].sort_values("share_diff_abs", ascending=False, na_position="last")


# ── reporting ─────────────────────────────────────────────────────────────────

def print_summary(df: pd.DataFrame) -> None:
    matched      = df[df["match_status"] == "matched"]
    breaks       = matched[matched["is_break"]]
    nport_only   = df[df["match_status"] == "nport_only"]
    ishares_only = df[df["match_status"] == "ishares_only"]

    print()
    print("=" * 70)
    print("RECONCILIATION SUMMARY")
    print("  N-PORT date  : 2026-08-25  (SEC EDGAR N-PORT filing)")
    print("  iShares date : 2026-11-16  (iShares public CSV)")
    print("  Date gap     : ~84 days — weight diffs are expected to be large")
    print("=" * 70)
    print(f"  Total matched rows           : {len(matched):>5}")
    print(f"  Breaks flagged (>1% balance) : {len(breaks):>5}")
    print(f"  Unmatched — N-PORT only      : {len(nport_only):>5}")
    print(f"  Unmatched — iShares only     : {len(ishares_only):>5}")
    print()

    print("Top 10 breaks by absolute share-count difference:")
    top_breaks = breaks.nlargest(10, "share_diff_abs")
    for _, row in top_breaks.iterrows():
        print(
            f"  {row['norm_name']:<35}"
            f"  nport={row['nport_balance']:>14,.0f}"
            f"  ishares={row['ishares_quantity']:>14,.0f}"
            f"  diff={row['share_diff']:>+14,.0f}"
            f"  ({row['share_diff_pct']:.1f}%)"
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

    print("\nNormalising names and aggregating to company level …")
    nport_agg   = build_nport_agg(nport_raw)
    ishares_agg = build_ishares_agg(ishares_raw)
    print(f"  N-PORT  : {len(nport_raw)} rows -> {len(nport_agg)} unique normalised names")
    print(f"  iShares : {len(ishares_raw)} rows -> {len(ishares_agg)} unique normalised names")

    print("\nJoining and computing differences …")
    result = reconcile(nport_agg, ishares_agg)

    print_summary(result)

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT_CSV, index=False, float_format="%.6f")
    print(f"Full comparison table saved to {OUTPUT_CSV.relative_to(REPO_ROOT)}")
    print(f"  {len(result)} total rows  |  columns: {list(result.columns)}")


if __name__ == "__main__":
    main()
