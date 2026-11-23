"""
Module 4: QC exception trend across 6 quarterly N-PORT filings.

Applies the same rules as qc_rules.py to security_master_history, grouped
by filing_date.  R4 (duplicate cusip) is evaluated within each partition.

Besides the CSV, this script writes three objects to PostgreSQL that act as
the single reporting source for both Streamlit and Power BI:

  reporting.qc_exceptions  — one row per flagged holding per quarter
  reporting.qc_trend       — one row per quarter with open/expected/total counts
  reporting.v_fund_size    — view: quarter, total_holdings, total_value_usd_bn
  reporting.dim_quarter    — view: one row per quarter with label and sort key
  reporting.dim_rule       — reference table: one row per rule code
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

REPO_ROOT    = Path(__file__).resolve().parent.parent
OUTPUT_CSV   = REPO_ROOT / "data" / "processed" / "qc_trend_by_quarter.csv"
EXPECTED_CSV = REPO_ROOT / "data" / "reference" / "expected_exceptions.csv"

# Primary rule code picked when multiple rules fire on the same row.
# D1/E1 (Expected) take precedence; N1 is last (Open but unreviewed).
RULE_CODE_PRIORITY = ["D1", "E1", "R1", "R2", "R3", "R4", "R5", "R6", "N1"]

RULE_CODE_NAMES = {
    "R1": "No identifier",
    "R2": "Weight out of range",
    "R3": "Negative value",
    "R4": "Duplicate CUSIP",
    "R5": "Missing category",
    "R6": "Possible cash instrument",
    "N1": "New - needs review",
    "E1": "Money-market / cash collateral (reviewed)",
    "D1": "Derivative (reviewed)",
}

EXPECTED_CODES = {"D1", "E1"}

# Sort order for dim_rule; R1-R6 shown first (errors), then N1 (unreviewed), then Expected.
RULE_CODE_SORT = {
    "R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5, "R6": 6,
    "N1": 7, "E1": 8, "D1": 9,
}

# Technical wording used in rule_description (the former RULE_CODE_NAMES values).
RULE_CODE_DESCRIPTIONS = {
    "R1": "Both cusip AND isin blank/null",
    "R2": "pctVal out of range [0, 100]",
    "R3": "valUSD negative",
    "R4": "Duplicate cusip within same isCashCollateral bucket",
    "R5": "assetCat or issuerCat blank/null",
    "R6": "balance ~= valUSD for non-STIV rows",
    "N1": "Unreviewed STIV or derivative — needs review",
    "E1": "Reviewed STIV (money-market fund / cash collateral)",
    "D1": "Reviewed derivative (exchange-traded futures)",
}


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


def _run_rules(grp: pd.DataFrame):
    """
    Evaluate all QC rules on one quarter group.
    Returns (named_masks, review_matches, any_open).
      named_masks   — dict of {code: boolean Series}
      review_matches — list of reviewed-lookup hits, one per row (None if not found)
      any_open      — boolean Series: True where status is Open
    """
    global _reviewed_lookup_cache
    if _reviewed_lookup_cache is None:
        _reviewed_lookup_cache = load_reviewed_lookup()
    reviewed_lookup = _reviewed_lookup_cache

    is_derivative = grp["assetcat"].fillna("").str.upper().str.startswith("D")
    is_stiv       = grp["assetcat"].fillna("") == "STIV"
    is_candidate  = is_derivative | is_stiv

    review_matches = [
        find_reviewed(reviewed_lookup, row.get("cusip"), row.get("iscashcollateral"), row.get("assetcat"))
        for _, row in grp.iterrows()
    ]
    is_reviewed = pd.Series([m is not None for m in review_matches], index=grp.index)

    d1 = is_derivative & is_reviewed
    e1 = is_stiv       & is_reviewed
    n1 = is_candidate  & ~is_reviewed

    r1 = is_blank(grp["cusip"]) & is_blank(grp["isin"]) & ~is_derivative
    r2 = grp["pctval"].notna() & (
        (grp["pctval"] < 0) | (grp["pctval"] > 100)
    ) & ~is_derivative
    r3 = grp["valusd"].notna() & (grp["valusd"] < 0) & ~is_derivative

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

    named_masks = {
        "R1": r1, "R2": r2, "R3": r3, "R4": r4, "R5": r5, "R6": r6,
        "D1": d1, "E1": e1, "N1": n1,
    }
    return named_masks, review_matches, any_open


def apply_rules_to_group(grp: pd.DataFrame) -> dict:
    """
    Run all rules on a single filing_date partition.
    Returns a dict of {rule_key: count_flagged}.
    """
    named_masks, _review_matches, any_open = _run_rules(grp)
    return {
        "r1_count": int(named_masks["R1"].sum()),
        "r2_count": int(named_masks["R2"].sum()),
        "r3_count": int(named_masks["R3"].sum()),
        "r4_count": int(named_masks["R4"].sum()),
        "r5_count": int(named_masks["R5"].sum()),
        "r6_count": int(named_masks["R6"].sum()),
        "d1_count": int(named_masks["D1"].sum()),
        "e1_count": int(named_masks["E1"].sum()),
        "n1_count": int(named_masks["N1"].sum()),
        "total_unique_exceptions": int(any_open.sum()),
    }


def build_exceptions_df(grp: pd.DataFrame, filing_date: str, period_end_date: str) -> pd.DataFrame:
    """
    One row per flagged holding (Open or Expected) for one quarter.
    rule_code is the primary code picked by RULE_CODE_PRIORITY when multiple fire.
    """
    named_masks, review_matches, any_open = _run_rules(grp)
    all_flagged = any_open | named_masks["D1"] | named_masks["E1"]

    rows = []
    for pos, (idx, row) in enumerate(grp.iterrows()):
        if not all_flagged.loc[idx]:
            continue

        fired_codes = [c for c in RULE_CODE_PRIORITY if named_masks[c].loc[idx]]
        if not fired_codes:
            continue

        primary = fired_codes[0]
        status  = "Expected" if primary in EXPECTED_CODES else "Open"

        rm = review_matches[pos] if pos < len(review_matches) else None
        if rm:
            reason = rm["reason"]
        elif primary == "N1":
            reason = (
                "New unreviewed STIV or derivative — add to "
                "data/reference/expected_exceptions.csv after review"
            )
        else:
            reason = ""

        rows.append({
            "period_end_date":   period_end_date,
            "filing_date":       filing_date,
            "name":              row.get("name"),
            "cusip":             row.get("cusip"),
            "isin":              row.get("isin"),
            "assetcat":          row.get("assetcat"),
            "issuercat":         row.get("issuercat"),
            "iscashcollateral":  row.get("iscashcollateral"),
            "balance":           row.get("balance"),
            "valusd":            row.get("valusd"),
            "pctval":            row.get("pctval"),
            "rule_code":         primary,
            "rule_name":         RULE_CODE_NAMES.get(primary, primary),
            "status":            status,
            "reason":            reason,
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=[
        "period_end_date", "filing_date", "name", "cusip", "isin",
        "assetcat", "issuercat", "iscashcollateral", "balance", "valusd",
        "pctval", "rule_code", "rule_name", "status", "reason",
    ])


# ── dim_rule builder ──────────────────────────────────────────────────────────

def build_dim_rule_df() -> pd.DataFrame:
    """
    Build the dim_rule reference DataFrame from the existing rule constants.
    rule_name is taken directly from RULE_CODE_NAMES (single source of truth).
    Rows ordered by RULE_CODE_PRIORITY so the DataFrame reflects the natural
    priority; rule_sort supplies the numeric sort for Power BI.
    """
    rows = []
    for code in RULE_CODE_PRIORITY:
        rows.append({
            "rule_code":        code,
            "rule_name":        RULE_CODE_NAMES[code],
            "rule_group":       "Expected" if code in EXPECTED_CODES else "Open",
            "rule_description": RULE_CODE_DESCRIPTIONS[code],
            "rule_sort":        RULE_CODE_SORT[code],
        })
    return pd.DataFrame(rows)


# ── reporting layer ────────────────────────────────────────────────────────────

def write_reporting_tables(engine, exceptions_df: pd.DataFrame, trend_write_df: pd.DataFrame) -> None:
    """
    Write / replace three reporting objects in PostgreSQL:
      reporting.qc_exceptions  — per-row exceptions
      reporting.qc_trend       — per-quarter counts
      reporting.v_fund_size    — view on security_master_history
    """
    print("\nWriting reporting schema to PostgreSQL ...")

    with engine.connect() as conn:
        conn.execute(sqlalchemy.text("CREATE SCHEMA IF NOT EXISTS reporting"))
        conn.commit()

    exceptions_df.to_sql("qc_exceptions", engine, schema="reporting",
                         if_exists="replace", index=False)
    print(f"  reporting.qc_exceptions : {len(exceptions_df)} rows written")

    trend_write_df.to_sql("qc_trend", engine, schema="reporting",
                          if_exists="replace", index=False)
    print(f"  reporting.qc_trend      : {len(trend_write_df)} rows written")

    dim_rule_df = build_dim_rule_df()
    dim_rule_df.to_sql("dim_rule", engine, schema="reporting",
                       if_exists="replace", index=False)
    print(f"  reporting.dim_rule      : {len(dim_rule_df)} rows written")

    # Cross-check: every rule_code that appears in qc_exceptions must be in dim_rule.
    exc_codes = set(exceptions_df["rule_code"].dropna().unique())
    dim_codes = set(dim_rule_df["rule_code"].unique())
    missing   = exc_codes - dim_codes
    if missing:
        print(f"\n  ERROR: rule_code(s) in qc_exceptions missing from dim_rule: {sorted(missing)}")
    else:
        print(f"  Cross-check: all {len(exc_codes)} rule_code(s) in qc_exceptions present in dim_rule  [PASS]")

    with engine.connect() as conn:
        conn.execute(sqlalchemy.text("""
            CREATE OR REPLACE VIEW reporting.v_fund_size AS
            SELECT
                period_end_date,
                COUNT(*)                             AS total_holdings,
                ROUND(SUM(valusd)::numeric / 1e9, 2) AS total_value_usd_bn
            FROM security_master_history
            GROUP BY period_end_date
            ORDER BY period_end_date
        """))
        conn.execute(sqlalchemy.text("""
            CREATE OR REPLACE VIEW reporting.dim_quarter AS
            SELECT
                period_end_date::date  AS period_end_date,
                filing_date::date      AS filing_date,
                to_char(period_end_date, 'Mon YYYY') AS quarter_label,
                ROW_NUMBER() OVER (ORDER BY period_end_date)::integer AS quarter_sort,
                CASE
                    WHEN period_end_date = MAX(period_end_date) OVER ()
                    THEN 'Latest quarter'
                    ELSE to_char(period_end_date, 'Mon YYYY')
                END AS slicer_label
            FROM (
                SELECT DISTINCT period_end_date, filing_date
                FROM security_master_history
            ) sub
            ORDER BY period_end_date
        """))
        conn.commit()
    print("  reporting.v_fund_size   : view created/updated")
    print("  reporting.dim_quarter   : view created/updated")


def print_reporting_tables(engine) -> None:
    """Print the three reporting objects so results can be visually verified."""
    with engine.connect() as conn:
        exc_df = pd.read_sql(
            "SELECT period_end_date, name, rule_code, status "
            "FROM reporting.qc_exceptions ORDER BY period_end_date, status, name",
            conn,
        )
        trend_df = pd.read_sql(
            "SELECT period_end_date, total_holdings, open_count, expected_count, "
            "       d1_count, e1_count, n1_count, total_unique_exceptions "
            "FROM reporting.qc_trend ORDER BY period_end_date",
            conn,
        )
        fs_df = pd.read_sql(
            "SELECT * FROM reporting.v_fund_size ORDER BY period_end_date",
            conn,
        )
        rule_df = pd.read_sql(
            "SELECT rule_sort, rule_code, rule_group, rule_name, rule_description "
            "FROM reporting.dim_rule ORDER BY rule_sort",
            conn,
        )

    print()
    print("=" * 70)
    print("reporting.qc_exceptions")
    print("=" * 70)
    n_open = len(exc_df[exc_df["status"] == "Open"])
    n_exp  = len(exc_df[exc_df["status"] == "Expected"])
    print(f"  {len(exc_df)} rows total  |  Open: {n_open}  |  Expected: {n_exp}")
    print(exc_df.to_string(index=False))

    print()
    print("=" * 70)
    print("reporting.qc_trend")
    print("=" * 70)
    print(trend_df.to_string(index=False))

    print()
    print("=" * 70)
    print("reporting.v_fund_size")
    print("=" * 70)
    print(fs_df.to_string(index=False))

    print()
    print("=" * 70)
    print("reporting.dim_rule")
    print("=" * 70)
    print(rule_df.to_string(index=False))


# ── main ──────────────────────────────────────────────────────────────────────

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

    n_dates = df["filing_date"].nunique()
    print(f"  {len(df)} rows loaded across {n_dates} filing dates.")

    # ── apply rules per quarter ───────────────────────────────────────────────
    records       = []
    exc_parts     = []

    for (filing_date, period_end_date), grp in df.groupby(["filing_date", "period_end_date"]):
        fd_str  = str(filing_date)[:10]
        ped_str = str(period_end_date)[:10]

        result = apply_rules_to_group(grp)
        result["filing_date"]     = fd_str
        result["period_end_date"] = ped_str
        result["total_holdings"]  = len(grp)
        result["open_count"]      = result["total_unique_exceptions"]
        result["expected_count"]  = result["d1_count"] + result["e1_count"]
        records.append(result)

        exc_parts.append(build_exceptions_df(grp, fd_str, ped_str))

    # ── trend DataFrame (CSV columns kept as before) ──────────────────────────
    csv_cols = [
        "filing_date",
        "r1_count", "r2_count", "r3_count",
        "r4_count", "r5_count", "r6_count",
        "d1_count", "e1_count", "n1_count",
        "total_unique_exceptions",
    ]
    trend_df = (
        pd.DataFrame(records)[csv_cols]
        .sort_values("filing_date")
        .reset_index(drop=True)
    )

    # reporting.qc_trend has additional columns
    trend_write_cols = [
        "period_end_date", "filing_date",
        "total_holdings", "open_count", "expected_count",
        "r1_count", "r2_count", "r3_count",
        "r4_count", "r5_count", "r6_count",
        "d1_count", "e1_count", "n1_count",
        "total_unique_exceptions",
    ]
    trend_write_df = (
        pd.DataFrame(records)[trend_write_cols]
        .sort_values("period_end_date")
        .reset_index(drop=True)
    )

    exceptions_df = pd.concat(exc_parts, ignore_index=True) if exc_parts else pd.DataFrame()

    # ── print trend table ─────────────────────────────────────────────────────
    print()
    print("=" * 91)
    print("QC EXCEPTION TREND — security_master_history (6 quarters)")
    print("=" * 91)
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

    # ── save CSV ──────────────────────────────────────────────────────────────
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    trend_df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nSaved to {OUTPUT_CSV.relative_to(REPO_ROOT)}")

    # ── write & verify reporting tables ───────────────────────────────────────
    write_reporting_tables(engine, exceptions_df, trend_write_df)
    print_reporting_tables(engine)

    # ── confirm powerbi_reader can SELECT reporting.dim_rule ──────────────────
    pbi_pw = os.getenv("PBI_READER_PASSWORD", "")
    if pbi_pw:
        pbi_engine = sqlalchemy.create_engine(
            f"postgresql+psycopg2://powerbi_reader:{pbi_pw}@{PGHOST}:{PGPORT}/{PGDATABASE}"
        )
        with pbi_engine.connect() as conn:
            pbi_df = pd.read_sql(
                sqlalchemy.text(
                    "SELECT rule_sort, rule_code, rule_group, rule_name "
                    "FROM reporting.dim_rule ORDER BY rule_sort"
                ),
                conn,
            )
        print()
        print("=" * 70)
        print("powerbi_reader SELECT reporting.dim_rule")
        print("=" * 70)
        print(f"  {len(pbi_df)} rows  [PASS]")
        print(pbi_df.to_string(index=False))
    else:
        print("\n(PBI_READER_PASSWORD not set — skipping powerbi_reader test)")


if __name__ == "__main__":
    main()
