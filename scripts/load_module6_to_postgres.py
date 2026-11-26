"""
load_module6_to_postgres.py

Loads Module 6 cash-reconciliation results into the reporting schema.

What it does, in plain English:
  1. Reads module6_expected_coupons.csv → reporting.cash_expected
       One row per expected coupon or principal payment (160 rows in 2026).

  2. Reads all three rounds' recon-results CSVs, tags each with a round number,
     adds a needs_review flag → reporting.cash_recon_results
       Round 1 (easy, seed 42), Round 2 (hard, seed 7), Round 3 (seed 99).
       Includes matched rows so the table is a complete ledger of every payment.

  3. Computes scorecard rows by calling score_reconciliation.score() against
     the actual saved result files for all four round+version combinations:
       Round 1 after_fix, Round 2 before_fix (v1, from module6_recon_results_hard_v1.csv
       restored from git history), Round 2 after_fix, Round 3 after_fix.
     → reporting.cash_recon_scorecard

  4. Reads data/reference/reconciliation_rules.csv →
     reporting.cash_recon_rules
       The two documented thresholds (late_window, short_threshold) with
       plain-English rationale.

  5. Grants SELECT on all four tables to the powerbi_reader role.

  6. Prints a reconciliation table (CSV counts vs DB counts per round); raises
     on any mismatch.

  7. Confirms powerbi_reader can query each table.

Re-runnable: each table is dropped and recreated on every run.
"""

import os
import sys
from pathlib import Path

import pandas as pd
import sqlalchemy
from dotenv import load_dotenv

load_dotenv()

PGHOST     = os.getenv("PGHOST",     "localhost")
PGPORT     = os.getenv("PGPORT",     "5432")
PGDATABASE = os.getenv("PGDATABASE", "finance_platform")
PGUSER     = os.getenv("PGUSER",     "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

ROOT     = Path(__file__).resolve().parent.parent
PROC     = ROOT / "data" / "processed"
REF      = ROOT / "data" / "reference"
SCRIPTS  = Path(__file__).resolve().parent

# Make score_reconciliation importable from the same scripts/ directory
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from score_reconciliation import score as _score  # noqa: E402

EXPECTED_CSV  = PROC / "module6_expected_coupons.csv"
RECON_EASY    = PROC / "module6_recon_results.csv"
RECON_HARD    = PROC / "module6_recon_results_hard.csv"
RECON_HARD_V1 = PROC / "module6_recon_results_hard_v1.csv"   # v1 reconciler, restored from git
RECON_ROUND3  = PROC / "module6_recon_results_round3.csv"
AK_EASY       = PROC / "module6_answer_key.csv"
AK_HARD       = PROC / "module6_answer_key_hard.csv"
AK_ROUND3     = PROC / "module6_answer_key_round3.csv"
RULES_CSV     = REF  / "reconciliation_rules.csv"

TABLES = ["cash_expected", "cash_recon_results",
          "cash_recon_scorecard", "cash_recon_rules"]


def engine_url():
    return (f"postgresql+psycopg2://{PGUSER}:{PGPASSWORD}"
            f"@{PGHOST}:{PGPORT}/{PGDATABASE}")


# ── 1. cash_expected ─────────────────────────────────────────────────────────

def build_cash_expected() -> pd.DataFrame:
    df = pd.read_csv(EXPECTED_CSV)
    df["payment_date"]    = pd.to_datetime(df["payment_date"]).dt.date
    df["scheduled_date"]  = pd.to_datetime(df["scheduled_date"]).dt.date
    df["expected_amount"] = df["expected_amount"].astype(float)
    df["face_held"]       = df["face_held"].astype(float)
    df["coupon_pct"]      = df["coupon_pct"].astype(float)
    return df


# ── 2. cash_recon_results ────────────────────────────────────────────────────

def build_cash_recon_results() -> pd.DataFrame:
    frames = []
    for rnd, path in [(1, RECON_EASY), (2, RECON_HARD), (3, RECON_ROUND3)]:
        f = pd.read_csv(path)
        f["round"] = rnd
        frames.append(f)
    df = pd.concat(frames, ignore_index=True)
    df["needs_review"] = df["result"].str.contains("needs_review", na=False)
    # column order
    return df[["round", "expected_payment_id", "statement_row_id", "result",
               "cusip", "amount_expected", "amount_received",
               "difference", "days_late", "needs_review"]]


# ── 3. cash_recon_scorecard ──────────────────────────────────────────────────

# Expected values used for verification after computing from files
_EXPECTED_SCORECARD = [
    # (round_number, reconciler_version, planted, found, correct, false_alarms)
    (1, "v2",            19, 19, 19,  0),
    (2, "v1_before_fix", 11, 11,  2,  7),
    (2, "v2_after_fix",  11, 11, 11,  0),
    (3, "v2",            16, 16, 16,  0),
]

_SCORECARD_META = {
    # (round_number, reconciler_version) → (round_label, notes)
    (1, "v2"): (
        "Round 1 (easy, seed 42)",
        "19 standard break types: missing, late (1–3 biz days), "
        "short_paid ($25 fee or 0.5%), duplicate, surprise (fake CUSIP), cusip_typo.",
    ),
    (2, "v1_before_fix"): (
        "Round 2 (hard, seed 7)",
        "Original 6-pass algorithm. Very_late and large_deduction had no same-bond "
        "pairing pass — expected rows showed 'missing' and statement rows showed "
        "'unexpected' (7 false alarms). Split/combined had no detection passes. "
        "Only valid_surprise (2) typed correctly.",
    ),
    (2, "v2_after_fix"): (
        "Round 2 (hard, seed 7)",
        "9-pass algorithm. Same-bond pairing (pass 8) classifies very_late as "
        "late_needs_review and large_deduction as large_deduction_needs_review "
        "without false alarms. Combined (pass 2) and split (pass 3) detected exactly.",
    ),
    (3, "v2"): (
        "Round 3 (mixed, seed 99)",
        "All 12 break types in one statement. Correct on first run with the improved "
        "reconciler — no further changes needed.",
    ),
}

_SCORE_RUNS = [
    # (round_number, version_label, recon_file, ak_file)
    (1, "v2",            RECON_EASY,    AK_EASY),
    (2, "v1_before_fix", RECON_HARD_V1, AK_HARD),
    (2, "v2_after_fix",  RECON_HARD,    AK_HARD),
    (3, "v2",            RECON_ROUND3,  AK_ROUND3),
]


def build_cash_recon_scorecard() -> pd.DataFrame:
    rows = []
    for rnd, ver, recon_path, ak_path in _SCORE_RUNS:
        result = _score(recon_path, ak_path)
        label, notes = _SCORECARD_META[(rnd, ver)]
        rows.append({
            "round_number":        rnd,
            "round_label":         label,
            "reconciler_version":  ver,
            "breaks_planted":      result["total_planted"],
            "breaks_found":        result["found"],
            "correct_type":        result["correct_type"],
            "false_alarms":        result["false_alarms"],
            "notes":               notes,
        })
    return pd.DataFrame(rows)


# ── 4. cash_recon_rules ──────────────────────────────────────────────────────

def build_cash_recon_rules() -> pd.DataFrame:
    return pd.read_csv(RULES_CSV)


# ── Write + grant ────────────────────────────────────────────────────────────

def write_tables(exp, recon, scorecard, rules):
    engine = sqlalchemy.create_engine(engine_url())

    with engine.connect() as conn:
        conn.execute(sqlalchemy.text("CREATE SCHEMA IF NOT EXISTS reporting"))
        conn.commit()

    for tbl in TABLES:
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text(
                f"DROP TABLE IF EXISTS reporting.{tbl}"
            ))
            conn.commit()

    exp.to_sql("cash_expected", engine, schema="reporting",
               if_exists="replace", index=False)
    print(f"  reporting.cash_expected       : {len(exp)} rows written")

    recon.to_sql("cash_recon_results", engine, schema="reporting",
                 if_exists="replace", index=False)
    print(f"  reporting.cash_recon_results  : {len(recon)} rows written")

    scorecard.to_sql("cash_recon_scorecard", engine, schema="reporting",
                     if_exists="replace", index=False)
    print(f"  reporting.cash_recon_scorecard: {len(scorecard)} rows written")

    rules.to_sql("cash_recon_rules", engine, schema="reporting",
                 if_exists="replace", index=False)
    print(f"  reporting.cash_recon_rules    : {len(rules)} rows written")

    with engine.connect() as conn:
        for tbl in TABLES:
            conn.execute(sqlalchemy.text(
                f"GRANT SELECT ON reporting.{tbl} TO powerbi_reader"
            ))
        conn.commit()
    print("  GRANT SELECT -> powerbi_reader on all 4 tables  [done]")


# ── Reconciliation ────────────────────────────────────────────────────────────

def reconcile(exp: pd.DataFrame, recon: pd.DataFrame):
    engine = sqlalchemy.create_engine(engine_url())

    print()
    print("=" * 70)
    print("Reconciliation: CSV row counts vs DB row counts")
    print("=" * 70)
    print(f"  {'Table':<35}  {'CSV':>6}  {'DB':>6}  {'Match':>7}")
    print("  " + "-" * 60)

    checks = [
        ("cash_expected",   len(exp),   "SELECT COUNT(*) FROM reporting.cash_expected"),
        ("cash_recon_results", len(recon),
         "SELECT COUNT(*) FROM reporting.cash_recon_results"),
    ]

    # Per-round counts for cash_recon_results
    round_csv = recon.groupby("round").size()
    round_queries = {
        1: "SELECT COUNT(*) FROM reporting.cash_recon_results WHERE round = 1",
        2: "SELECT COUNT(*) FROM reporting.cash_recon_results WHERE round = 2",
        3: "SELECT COUNT(*) FROM reporting.cash_recon_results WHERE round = 3",
    }

    all_pass = True
    with engine.connect() as conn:
        for name, csv_n, sql in checks:
            db_n = conn.execute(sqlalchemy.text(sql)).scalar()
            match = csv_n == db_n
            flag  = "[PASS]" if match else "[FAIL]"
            if not match:
                all_pass = False
            print(f"  {name:<35}  {csv_n:>6}  {db_n:>6}  {flag}")

        print()
        print(f"  {'cash_recon_results by round':<35}  {'CSV':>6}  {'DB':>6}  {'Match':>7}")
        print("  " + "-" * 60)
        for rnd in [1, 2, 3]:
            csv_n = int(round_csv.get(rnd, 0))
            db_n  = conn.execute(sqlalchemy.text(round_queries[rnd])).scalar()
            match = csv_n == db_n
            flag  = "[PASS]" if match else "[FAIL]"
            if not match:
                all_pass = False
            lbl = f"  round {rnd}"
            print(f"  {'  Round ' + str(rnd):<35}  {csv_n:>6}  {db_n:>6}  {flag}")

    if not all_pass:
        raise AssertionError("Row count mismatch — see [FAIL] rows above.")
    print(f"\n  All counts match. Total recon rows: {len(recon)} "
          f"({int(round_csv.get(1,0))} + {int(round_csv.get(2,0))} + "
          f"{int(round_csv.get(3,0))})")


# ── powerbi_reader check ──────────────────────────────────────────────────────

def confirm_powerbi_reader():
    pbi_pw = os.getenv("PBI_READER_PASSWORD", "")
    if not pbi_pw:
        print("\n(PBI_READER_PASSWORD not set — skipping powerbi_reader test)")
        return
    pbi_engine = sqlalchemy.create_engine(
        f"postgresql+psycopg2://powerbi_reader:{pbi_pw}"
        f"@{PGHOST}:{PGPORT}/{PGDATABASE}"
    )
    print()
    print("=" * 70)
    print("powerbi_reader access check")
    print("=" * 70)
    with pbi_engine.connect() as conn:
        for tbl in TABLES:
            df = pd.read_sql(
                f"SELECT * FROM reporting.{tbl} LIMIT 1", conn
            )
            print(f"  reporting.{tbl:<30}  {len(df)} row returned  [PASS]")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("Building cash_expected (160 rows) ...")
    exp = build_cash_expected()

    print("Building cash_recon_results (all 3 rounds, matched + breaks) ...")
    recon = build_cash_recon_results()

    print("Computing cash_recon_scorecard (scoring 4 round+version combinations) ...")
    scorecard = build_cash_recon_scorecard()

    print("Building cash_recon_rules (2 rows: late_window, short_threshold) ...")
    rules = build_cash_recon_rules()

    print()
    print("Writing to reporting schema ...")
    write_tables(exp, recon, scorecard, rules)

    reconcile(exp, recon)

    print()
    print("=" * 70)
    print("cash_recon_scorecard")
    print("=" * 70)
    display_cols = ["round_number", "round_label", "reconciler_version",
                    "breaks_planted", "breaks_found", "correct_type", "false_alarms"]
    print(scorecard[display_cols].to_string(index=False))

    # Verify computed scorecard matches expected values
    print()
    print("=" * 70)
    print("Scorecard verification (computed vs expected)")
    print("=" * 70)
    all_pass = True
    for exp_row in _EXPECTED_SCORECARD:
        rnd, ver, e_planted, e_found, e_correct, e_fa = exp_row
        actual = scorecard[
            (scorecard["round_number"] == rnd) &
            (scorecard["reconciler_version"] == ver)
        ].iloc[0]
        match = (
            actual["breaks_planted"] == e_planted and
            actual["breaks_found"]   == e_found   and
            actual["correct_type"]   == e_correct  and
            actual["false_alarms"]   == e_fa
        )
        flag = "[PASS]" if match else "[FAIL]"
        if not match:
            all_pass = False
        print(f"  Round {rnd} {ver:<18}  "
              f"planted={actual['breaks_planted']}  found={actual['breaks_found']}  "
              f"correct={actual['correct_type']}  fa={actual['false_alarms']}  {flag}")
    if not all_pass:
        raise AssertionError(
            "Scorecard mismatch — computed values differ from reported values. "
            "See [FAIL] rows above."
        )

    confirm_powerbi_reader()

    print()
    print("Done.")


if __name__ == "__main__":
    main()
