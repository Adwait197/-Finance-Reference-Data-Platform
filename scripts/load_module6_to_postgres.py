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

  3. Builds a hardcoded scorecard that includes both the before-improvement score
     on the hard round (v1 reconciler: 2/11 correct, 7 false alarms) and all
     three after-improvement scores (v2: all perfect) →
     reporting.cash_recon_scorecard

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

EXPECTED_CSV  = PROC / "module6_expected_coupons.csv"
RECON_EASY    = PROC / "module6_recon_results.csv"
RECON_HARD    = PROC / "module6_recon_results_hard.csv"
RECON_ROUND3  = PROC / "module6_recon_results_round3.csv"
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

def build_cash_recon_scorecard() -> pd.DataFrame:
    rows = [
        (1, "Round 1 (easy, seed 42)",  "v2",
         19, 19, 19,  0,
         "19 standard break types: missing, late (1–3 biz days), "
         "short_paid ($25 fee or 0.5%), duplicate, surprise (fake CUSIP), "
         "cusip_typo. All caught correctly by both v1 and v2."),
        (2, "Round 2 (hard, seed 7)",   "v1_before_fix",
         11, 11,  2,  7,
         "Original 6-pass algorithm. Very_late (8 days) and large_deduction "
         "(15%) had no same-bond pairing pass — expected rows showed 'missing' "
         "and statement rows showed 'unexpected' (7 false alarms). "
         "Split/combined had no detection passes. Only valid_surprise (2) "
         "typed correctly."),
        (2, "Round 2 (hard, seed 7)",   "v2_after_fix",
         11, 11, 11,  0,
         "9-pass algorithm. Same-bond pairing (pass 8) classifies very_late "
         "as late_needs_review and large_deduction as large_deduction_needs_review "
         "without false alarms. Combined (pass 2) and split (pass 3) detected "
         "exactly. All 11 entries correct, 0 false alarms."),
        (3, "Round 3 (mixed, seed 99)", "v2",
         16, 16, 16,  0,
         "All 12 break types combined in one statement. Correct on first run "
         "with the improved reconciler — no further changes needed."),
    ]
    return pd.DataFrame(rows, columns=[
        "round_number", "round_label", "reconciler_version",
        "breaks_planted", "breaks_found", "correct_type",
        "false_alarms", "notes",
    ])


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
    print("  GRANT SELECT → powerbi_reader on all 4 tables  [done]")


# ── Reconciliation ────────────────────────────────────────────────────────────

def reconcile(exp: pd.DataFrame, recon: pd.DataFrame):
    engine = sqlalchemy.create_engine(engine_url())

    print()
    print("=" * 70)
    print("Reconciliation: CSV row counts vs DB row counts")
    print("=" * 70)
    print(f"  {'Table':<35}  {'CSV':>6}  {'DB':>6}  {'Match':>7}")
    print("  " + "─" * 60)

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
        print("  " + "─" * 60)
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

    print("Building cash_recon_scorecard (4 rows: v1 before + v2 after) ...")
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

    confirm_powerbi_reader()

    print()
    print("Done.")


if __name__ == "__main__":
    main()
