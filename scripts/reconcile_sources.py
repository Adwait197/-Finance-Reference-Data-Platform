"""
Module 2: Cross-source reconciliation between N-PORT and iShares holdings.

Sources compared
  N-PORT  : security_master table in PostgreSQL
            holdings as of 2026-06-30, SEC filing date 2026-08-25
  iShares : data/raw/ishares_IVV_holdings_2026-11-17.csv
            "Fund Holdings as of Nov 16, 2026" (from the file header)
  Date gap: 139 days (Jun 30 to Nov 16, 2026)
            Note: an earlier version cited 84 days, computed from the SEC
            filing date (Aug 25) rather than the N-PORT period-end date
            (Jun 30). The correct gap uses the two *holdings* dates.

Name matching — two passes
  Pass 1 (exact): uppercase, punctuation->space, apostrophes removed,
      common corporate suffixes and share-class designators stripped;
      company-level aggregation before merge.  match_type = "exact".
  Pass 2 (fuzzy): rows still unmatched after pass 1 are compared using
      rapidfuzz token_set_ratio.  Pairs scoring >= FUZZY_THRESHOLD (90)
      are promoted to "matched" with match_type = "fuzzy" and a
      fuzzy_score column.

Break detection (primary criterion)
  A row is flagged as a break when the absolute difference between N-PORT
  pctVal and iShares Market Weight exceeds WEIGHT_BREAK_THRESHOLD_PP
  (0.15 percentage points).  Portfolio weight is a better signal than raw
  share count across a date gap: weight accounts for price moves and
  fund-size changes, so a large weight discrepancy is more likely to
  indicate a genuine data error rather than normal index turnover.

Share-count comparison (informational)
  The ratio ishares_quantity / nport_balance is kept as a separate column.
  When that ratio is within 5 pct of a clean integer multiple (2x, 3x, 4x)
  or its inverse (0.5x, 0.33x, 0.25x), the row is tagged "possible stock
  split" — that label explains large share-count changes that are NOT
  weight breaks.

Both pctVal (N-PORT) and Market Weight (iShares) are expressed as
percentages (e.g. 8.05 means 8.05 pct), so they are compared directly
with no unit conversion.

Date-gap limitation
  Tested 4 methods to get a same-date (Jun 30, 2026) iShares snapshot:
  the asOfDate URL parameter, the Wayback Machine, a manual browser
  download, and the open-source etf_scraper package. All four hit the same
  root cause — iShares' public AJAX endpoint ignores any date parameter and
  always serves today's snapshot. This is a confirmed limitation of the
  free public source, not a project gap.
"""

import os
import re
from pathlib import Path

import pandas as pd
import sqlalchemy
from dotenv import load_dotenv
from rapidfuzz import fuzz, process

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
REVIEWED_CSV = REPO_ROOT / "data" / "reference" / "fuzzy_matches_reviewed.csv"

# Primary break criterion: weight difference in percentage points
WEIGHT_BREAK_THRESHOLD_PP = 0.15

# Split detection: flag when share-count ratio is within this tolerance of a
# clean multiple, e.g. a 2-for-1 split makes the ratio ≈ 2.0
SPLIT_MULTIPLES = [2.0, 3.0, 4.0, 0.5, 1.0 / 3.0, 0.25]
SPLIT_TOLERANCE = 0.05   # 5 pct

# Fuzzy name matching: second-pass threshold
FUZZY_THRESHOLD = 90


# ── name normalisation ────────────────────────────────────────────────────────

_STRIP_PATTERNS = [
    r"\bCLASS\s+[A-Z]\b",          # CLASS A, CLASS B, CLASS C …
    r"\bCL\s+[A-Z]\b",             # CL A, CL B …
    r"\bORDINARY\s+SHARES?\b",
    r"\bSHS\b",
    r"\bEQUITY\s+WARRANTS?\b",
    r"\bWARRANTS?\b",
    r"\bREIT\b",
    r"\bCORPORATION\b",            # Intel Corporation → INTEL
    r"\bCOMPANIES\b",              # TJX Companies / Williams Companies → TJX / WILLIAMS
    r"\bCOMPANY\b",                # e.g. HOLDING COMPANY
    r"\bHOLDINGS?\b",              # ExxonMobil Holdings Corp → EXXONMOBIL
    r"\bCOS\b",                    # Cooper Cos. → COOPER
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
    s = s.replace("'", "").replace("'", "")
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


# ── exact reconciliation ──────────────────────────────────────────────────────

def reconcile(nport: pd.DataFrame, ishares: pd.DataFrame) -> pd.DataFrame:
    """Pass 1: exact normalised-name merge (outer join)."""
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


# ── fuzzy second pass ─────────────────────────────────────────────────────────

def _load_approved_fuzzy_pairs() -> set:
    """
    Return the set of (nport_name, ishares_name) tuples from REVIEWED_CSV
    whose decision == 'same'.  A fuzzy pair not in this set is flagged as
    'fuzzy_needs_review' and NOT counted as matched.
    """
    if not REVIEWED_CSV.exists():
        print(f"  WARNING: {REVIEWED_CSV.name} not found — all fuzzy pairs will be held for review.")
        return set()
    reviewed = pd.read_csv(REVIEWED_CSV)
    approved = reviewed[reviewed["decision"].str.strip().str.lower() == "same"]
    return set(zip(approved["nport_name"], approved["ishares_name"]))


def fuzzy_pass(df: pd.DataFrame) -> pd.DataFrame:
    """
    Pass 2: attempt token_set_ratio fuzzy matching on still-unmatched rows.

    A pair scoring >= FUZZY_THRESHOLD is promoted to match_status='matched'
    ONLY if it appears in REVIEWED_CSV with decision='same'.  Any pair that
    scores above the threshold but is NOT yet reviewed gets match_type=
    'fuzzy_needs_review' and stays unmatched so an analyst can inspect it.

    All rows get a match_type column:
      'exact'              — Pass 1 name match
      'fuzzy'              — Pass 2 match confirmed in reviewed CSV
      'fuzzy_needs_review' — Pass 2 candidate not yet in reviewed CSV
      ''                   — no match found
    """
    approved_pairs = _load_approved_fuzzy_pairs()

    exact_matched = df[df["match_status"] == "matched"].copy()
    nport_only    = df[df["match_status"] == "nport_only"].copy()
    ish_only      = df[df["match_status"] == "ishares_only"].copy()

    if nport_only.empty or ish_only.empty:
        df = df.copy()
        df["match_type"]  = df["match_status"].apply(lambda s: "exact" if s == "matched" else "")
        df["fuzzy_score"] = float("nan")
        return df

    ish_names = ish_only["norm_name"].tolist()
    used_ish  = set()
    pairs     = []   # (nport_norm, ish_norm, score)

    for _, nr in nport_only.iterrows():
        result = process.extractOne(
            nr["norm_name"], ish_names,
            scorer=fuzz.token_set_ratio,
        )
        if result is None:
            continue
        match_name, score, _ = result
        if score >= FUZZY_THRESHOLD and match_name not in used_ish:
            used_ish.add(match_name)
            pairs.append((nr["norm_name"], match_name, score))

    if not pairs:
        exact_matched["match_type"]  = "exact"
        exact_matched["fuzzy_score"] = float("nan")
        nport_only["match_type"]     = ""
        nport_only["fuzzy_score"]    = float("nan")
        ish_only["match_type"]       = ""
        ish_only["fuzzy_score"]      = float("nan")
        return pd.concat([exact_matched, nport_only, ish_only], ignore_index=True) \
                 .sort_values("weight_diff_abs", ascending=False, na_position="last")

    # Split pairs into approved (match) and needs-review (hold)
    approved_pairs_norm = set()   # (n_name_norm, i_name_norm) for approved pairs
    new_rows       = []           # rows that become matched
    needs_review   = []           # (nport_norm, ish_norm, score) — not yet confirmed

    for n_name, i_name, score in pairs:
        nr_data = nport_only.loc[nport_only["norm_name"] == n_name].iloc[0]
        ir_data = ish_only.loc[ish_only["norm_name"] == i_name].iloc[0]
        nport_display  = nr_data["nport_name"]
        ishares_display = ir_data["ishares_name"]

        if (nport_display, ishares_display) in approved_pairs:
            approved_pairs_norm.add((n_name, i_name))
            nport_pct = nr_data["nport_pctval"]
            ish_wt    = ir_data["ishares_weight"]
            nport_bal = nr_data["nport_balance"]
            ish_qty   = ir_data["ishares_quantity"]

            weight_diff     = (ish_wt - nport_pct) if pd.notna(ish_wt) and pd.notna(nport_pct) else float("nan")
            weight_diff_abs = abs(weight_diff) if pd.notna(weight_diff) else float("nan")
            share_diff      = (ish_qty - nport_bal) if pd.notna(ish_qty) and pd.notna(nport_bal) else float("nan")
            share_diff_pct  = (abs(share_diff) / nport_bal * 100
                               if pd.notna(nport_bal) and nport_bal != 0 else float("nan"))
            share_ratio     = (ish_qty / nport_bal
                               if pd.notna(nport_bal) and nport_bal != 0 else float("nan"))
            split_flag      = _split_label(share_ratio)
            is_break        = bool(pd.notna(weight_diff_abs) and weight_diff_abs > WEIGHT_BREAK_THRESHOLD_PP)

            new_rows.append({
                "norm_name":        n_name,
                "nport_name":       nport_display,
                "ishares_name":     ishares_display,
                "ishares_ticker":   ir_data["ishares_ticker"],
                "nport_pctval":     nport_pct,
                "ishares_weight":   ish_wt,
                "weight_diff":      weight_diff,
                "weight_diff_abs":  weight_diff_abs,
                "nport_balance":    nport_bal,
                "ishares_quantity": ish_qty,
                "share_diff":       share_diff,
                "share_diff_pct":   share_diff_pct,
                "share_ratio":      share_ratio,
                "split_flag":       split_flag,
                "is_break":         is_break,
                "match_status":     "matched",
                "match_type":       "fuzzy",
                "fuzzy_score":      float(score),
            })
        else:
            needs_review.append((n_name, i_name, score, nport_display, ishares_display))

    if needs_review:
        print(f"\n  {len(needs_review)} fuzzy pair(s) NEED REVIEW (score >= {FUZZY_THRESHOLD} but not in {REVIEWED_CSV.name}):")
        for n_name, i_name, score, nd, id_ in needs_review:
            print(f"    score={score:.0f}  nport='{nd}'  ishares='{id_}'")
        print(f"  Add them to {REVIEWED_CSV.relative_to(REPO_ROOT)} with decision='same' or 'different'.")

    fuzzy_df = pd.DataFrame(new_rows) if new_rows else pd.DataFrame()

    # Only remove from pools the pairs that were actually matched
    matched_n = {p[0] for p in approved_pairs_norm}
    matched_i = {p[1] for p in approved_pairs_norm}

    # Pairs that need review: flag nport_only rows with match_type='fuzzy_needs_review'
    needs_review_n = {p[0] for p in [(n, i, s, nd, id_) for n, i, s, nd, id_ in needs_review]}
    needs_review_i = {p[1] for p in [(n, i, s, nd, id_) for n, i, s, nd, id_ in needs_review]}

    remaining_nport = nport_only[~nport_only["norm_name"].isin(matched_n)].copy()
    remaining_ish   = ish_only[~ish_only["norm_name"].isin(matched_i)].copy()

    exact_matched["match_type"]  = "exact"
    exact_matched["fuzzy_score"] = float("nan")

    # Mark "needs review" rows in the remaining unmatched pool
    remaining_nport["match_type"] = remaining_nport["norm_name"].apply(
        lambda n: "fuzzy_needs_review" if n in needs_review_n else ""
    )
    remaining_nport["fuzzy_score"] = remaining_nport["norm_name"].apply(
        lambda n: next((s for nn, ii, s, nd, id_ in needs_review if nn == n), float("nan"))
    )
    remaining_ish["match_type"]  = remaining_ish["norm_name"].apply(
        lambda n: "fuzzy_needs_review" if n in needs_review_i else ""
    )
    remaining_ish["fuzzy_score"] = remaining_ish["norm_name"].apply(
        lambda n: next((s for nn, ii, s, nd, id_ in needs_review if ii == n), float("nan"))
    )

    parts = [exact_matched, remaining_nport, remaining_ish]
    if not fuzzy_df.empty:
        parts.insert(1, fuzzy_df)
    result = pd.concat(parts, ignore_index=True)
    return result.sort_values("weight_diff_abs", ascending=False, na_position="last")


# ── reporting ─────────────────────────────────────────────────────────────────

def print_summary(df: pd.DataFrame) -> None:
    matched      = df[df["match_status"] == "matched"]
    exact_m      = matched[matched["match_type"] == "exact"]
    fuzzy_m      = matched[matched["match_type"] == "fuzzy"]
    breaks       = matched[matched["is_break"]]
    splits       = matched[matched["split_flag"] != ""]
    nport_only   = df[df["match_status"] == "nport_only"]
    ishares_only = df[df["match_status"] == "ishares_only"]
    needs_review = df[df["match_type"] == "fuzzy_needs_review"]

    print()
    print("=" * 72)
    print("RECONCILIATION SUMMARY")
    print("  N-PORT date  : 2026-06-30  (portfolio holdings date; filing 2026-08-25)")
    print("  iShares date : 2026-11-16  (iShares public CSV, 'as of' in file header)")
    print("  Date gap     : 139 days (Jun 30 to Nov 16, 2026)")
    print(f"  Break rule   : |pctVal - Market Weight| > {WEIGHT_BREAK_THRESHOLD_PP} pp")
    print("=" * 72)
    print(f"  Total matched rows        (exact)  : {len(exact_m):>5}")
    print(f"  Total matched rows        (fuzzy)  : {len(fuzzy_m):>5}  (confirmed in {REVIEWED_CSV.name})")
    print(f"  Total matched rows        (all)    : {len(matched):>5}")
    print(f"  Fuzzy candidates — needs review    : {len(needs_review)//2:>5}  (scored >= {FUZZY_THRESHOLD} but not in reviewed CSV)")
    print(f"  Breaks flagged (weight-based rule) : {len(breaks):>5}")
    print(f"  Possible stock splits              : {len(splits):>5}")
    print(f"  Unmatched -- N-PORT only           : {len(nport_only):>5}")
    print(f"  Unmatched -- iShares only          : {len(ishares_only):>5}")
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

    if len(fuzzy_m) > 0:
        all_fuzzy = fuzzy_m.sort_values("fuzzy_score")
        print(f"\nAll fuzzy pairs ({len(all_fuzzy)}) — sorted by score ascending:")
        print(f"  {'Score':>5}  {'N-PORT name':<40}  {'iShares name':<40}")
        print("  " + "-" * 90)
        for _, row in all_fuzzy.iterrows():
            print(
                f"  {row['fuzzy_score']:>5.0f}"
                f"  {row['nport_name']:<40.40}"
                f"  {row['ishares_name']:<40.40}"
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

    print("\nPass 1: exact name merge ...")
    result = reconcile(nport_agg, ishares_agg)

    print("Pass 2: fuzzy name matching on unmatched rows ...")
    result = fuzzy_pass(result)

    print_summary(result)

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT_CSV, index=False, float_format="%.6f")
    print(f"Full comparison table saved to {OUTPUT_CSV.relative_to(REPO_ROOT)}")
    print(f"  {len(result)} total rows  |  columns: {list(result.columns)}")


if __name__ == "__main__":
    main()
