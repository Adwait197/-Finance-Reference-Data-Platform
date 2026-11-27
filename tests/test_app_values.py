"""
tests/test_app_values.py

Two layers of tests:
  1. Freshness — app/data/*.parquet must match the original source files
     (reconciliation CSV, PostgreSQL reporting schema).  A stale export
     causes these to fail even if the pages look right.
  2. Values — AppTest renders each page and asserts every headline metric
     value matches what we compute from app/data/*.parquet.
"""

from pathlib import Path
import glob
import os

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "app" / "data"
PAGES = REPO / "app" / "pages"


# ── helpers ───────────────────────────────────────────────────────────────────

def load(name: str) -> pd.DataFrame:
    return pd.read_parquet(DATA / f"{name}.parquet")


def find_metric(at: AppTest, label_fragment: str):
    """Return the first metric whose label contains label_fragment (case-insensitive)."""
    for m in at.metric:
        if label_fragment.lower() in m.label.lower():
            return m
    return None


def run_page(path: Path) -> AppTest:
    at = AppTest.from_file(str(path), default_timeout=90)
    at.run()
    assert not at.exception, f"Page raised exception:\n{at.exception}"
    return at


# ── expected values (from parquet) ────────────────────────────────────────────

def _home_expected():
    fund   = load("v_fund_size")
    qct    = load("qc_trend")
    m5sc   = load("module5_scorecard")
    crsc   = load("cash_recon_scorecard")
    recon  = load("reconciliation")
    bt     = load("bond_terms")

    n_quarters      = len(fund)
    latest_fund     = fund.sort_values("period_end_date").iloc[-1]
    latest_holdings = int(latest_fund["total_holdings"])

    n_bonds          = len(bt)
    bonds_found_r2   = int(m5sc.loc[m5sc["measure"] == "bonds_found",    "run2_value"].iloc[0])
    bonds_pass_r2    = int(m5sc.loc[m5sc["measure"] == "bonds_pass_all_checks", "run2_value"].iloc[0])

    latest_qc       = qct.sort_values("period_end_date").iloc[-1]
    open_exceptions = int(latest_qc["total_unique_exceptions"])

    v2_rounds      = crsc[crsc["reconciler_version"].str.startswith("v2")]
    total_planted  = int(v2_rounds["breaks_planted"].sum())

    n_matched = int((recon["match_status"] == "matched").sum())

    return {
        "Quarters of holdings data": str(n_quarters),
        "Holdings per quarter":      f"~{latest_holdings}",
        "Bonds extracted from filings": str(n_bonds),
        "Open data-quality exceptions": str(open_exceptions),
        "Cash recon breaks found":   f"{total_planted}/{total_planted}",
        "Source comparison matches": str(n_matched),
    }


def _source_comparison_expected():
    recon = load("reconciliation")
    ic    = load("internal_consistency")

    if recon["is_break"].dtype == object:
        recon["is_break"] = recon["is_break"].map({"True": True, "False": False}).fillna(False)

    n_matched    = int((recon["match_status"] == "matched").sum())
    n_nport_only = int((recon["match_status"] == "nport_only").sum())
    matched      = recon[recon["match_status"] == "matched"].copy()
    n_weight_brk = int(matched["is_break"].sum())
    n_splits     = int(matched["split_flag"].notna().sum())
    n_ic         = len(ic)
    ic_flags     = int(ic["flag"].sum()) if "flag" in ic.columns else 0

    return {
        "Matched pairs":         str(n_matched),
        "Unmatched per side":    str(n_nport_only),
        "Weight breaks":         str(n_weight_brk),
        "Possible stock splits": str(n_splits),
        "Internal consistency":  f"{ic_flags}/{n_ic}",
        "Holdings checked":      str(n_ic),
        "Inconsistencies found": str(ic_flags),
    }


def _data_quality_expected():
    exc = load("qc_exceptions")
    qct = load("qc_trend")

    exc["period_end_date"] = pd.to_datetime(exc["period_end_date"])
    qct["period_end_date"] = pd.to_datetime(qct["period_end_date"])

    latest_ped = qct["period_end_date"].max()
    exc_q      = exc[exc["period_end_date"] == latest_ped]

    total_holdings = int(qct.loc[qct["period_end_date"] == latest_ped, "total_holdings"].iloc[0])
    n_open         = len(exc_q[exc_q["status"] == "Open"])
    n_expected     = len(exc_q[exc_q["status"] == "Expected"])

    return {
        "Total holdings":               str(total_holdings),
        "Open exceptions":              str(n_open),
        "Expected":                     str(n_expected),
    }


# ── tests ─────────────────────────────────────────────────────────────────────

class TestHomeMetrics:
    @pytest.fixture(scope="class")
    def rendered(self):
        return run_page(REPO / "app" / "Home.py")

    @pytest.fixture(scope="class")
    def expected(self):
        return _home_expected()

    def test_quarters(self, rendered, expected):
        m = find_metric(rendered, "Quarters of holdings data")
        assert m is not None, "Metric 'Quarters of holdings data' not found on Home page"
        assert m.value == expected["Quarters of holdings data"]

    def test_holdings_per_quarter(self, rendered, expected):
        m = find_metric(rendered, "Holdings per quarter")
        assert m is not None, "Metric 'Holdings per quarter' not found on Home page"
        assert m.value == expected["Holdings per quarter"]

    def test_bonds_extracted(self, rendered, expected):
        m = find_metric(rendered, "Bonds extracted from filings")
        assert m is not None, "Metric 'Bonds extracted from filings' not found on Home page"
        assert m.value == expected["Bonds extracted from filings"]

    def test_open_exceptions(self, rendered, expected):
        m = find_metric(rendered, "Open data-quality exceptions")
        assert m is not None, "Metric 'Open data-quality exceptions' not found on Home page"
        assert m.value == expected["Open data-quality exceptions"]

    def test_cash_recon(self, rendered, expected):
        m = find_metric(rendered, "Cash recon breaks found")
        assert m is not None, "Metric 'Cash recon breaks found' not found on Home page"
        assert m.value == expected["Cash recon breaks found"]

    def test_source_comparison_matches(self, rendered, expected):
        m = find_metric(rendered, "Source comparison matches")
        assert m is not None, "Metric 'Source comparison matches' not found on Home page"
        assert m.value == expected["Source comparison matches"]


class TestSourceComparisonMetrics:
    @pytest.fixture(scope="class")
    def rendered(self):
        return run_page(PAGES / "2_Source_Comparison.py")

    @pytest.fixture(scope="class")
    def expected(self):
        return _source_comparison_expected()

    def test_matched_pairs(self, rendered, expected):
        m = find_metric(rendered, "Matched pairs")
        assert m is not None, "Metric 'Matched pairs' not found on Source Comparison page"
        assert m.value == expected["Matched pairs"]

    def test_unmatched_per_side(self, rendered, expected):
        m = find_metric(rendered, "Unmatched per side")
        assert m is not None, "Metric 'Unmatched per side' not found on Source Comparison page"
        assert m.value == expected["Unmatched per side"]

    def test_weight_breaks(self, rendered, expected):
        m = find_metric(rendered, "Weight breaks")
        assert m is not None, "Metric 'Weight breaks' not found on Source Comparison page"
        assert m.value == expected["Weight breaks"]

    def test_possible_stock_splits(self, rendered, expected):
        m = find_metric(rendered, "Possible stock splits")
        assert m is not None, "Metric 'Possible stock splits' not found on Source Comparison page"
        assert m.value == expected["Possible stock splits"]

    def test_internal_consistency(self, rendered, expected):
        m = find_metric(rendered, "Internal consistency")
        assert m is not None, "Metric 'Internal consistency' not found on Source Comparison page"
        assert m.value == expected["Internal consistency"]

    def test_holdings_checked(self, rendered, expected):
        m = find_metric(rendered, "Holdings checked")
        assert m is not None, "Metric 'Holdings checked' not found on Source Comparison page"
        assert m.value == expected["Holdings checked"]

    def test_inconsistencies_found(self, rendered, expected):
        m = find_metric(rendered, "Inconsistencies found")
        assert m is not None, "Metric 'Inconsistencies found' not found on Source Comparison page"
        assert m.value == expected["Inconsistencies found"]


class TestDataQualityMetrics:
    @pytest.fixture(scope="class")
    def rendered(self):
        return run_page(PAGES / "1_Data_Quality.py")

    @pytest.fixture(scope="class")
    def expected(self):
        return _data_quality_expected()

    def test_total_holdings(self, rendered, expected):
        m = find_metric(rendered, "Total holdings")
        assert m is not None, "Metric 'Total holdings' not found on Data Quality page"
        assert m.value == expected["Total holdings"]

    def test_open_exceptions(self, rendered, expected):
        m = find_metric(rendered, "Open exceptions")
        assert m is not None, "Metric 'Open exceptions' not found on Data Quality page"
        assert m.value == expected["Open exceptions"]

    def test_expected(self, rendered, expected):
        m = find_metric(rendered, "Expected")
        assert m is not None, "Metric 'Expected' not found on Data Quality page"
        assert m.value == expected["Expected"]


class TestSpecificCompaniesMatched:
    """Verify that the 8 target companies are matched after normalisation improvements."""
    def test_all_matched(self):
        recon   = load("reconciliation")
        matched = recon[recon["match_status"] == "matched"].copy()
        # norm_name is the shared normalised key; also check nport_name for context
        matched_norm = set(matched["norm_name"].str.strip().str.upper())

        targets = {
            "INTEL":           "Intel Corp.",
            "APPLIED MATERIALS": "Applied Materials, Inc.",
            "TEXAS INSTRUMENTS": "Texas Instruments, Inc.",
            "TJX":             "TJX Companies, Inc.",
            "LOWES":           "Lowe's Companies, Inc.",
            "WILLIAMS":        "Williams Companies, Inc.",
            "MARSH MCLENNAN":  "Marsh & McLennan Cos., Inc.",
            "EXXON MOBIL":     "Exxon Mobil Corp.",
        }
        for norm, original in targets.items():
            assert norm in matched_norm, (
                f"'{original}' (norm: '{norm}') not found in matched companies.\n"
                f"Check reconciliation.parquet norm_name column."
            )


# ── freshness tests: app/data must match original source files ────────────────

PROCESSED = REPO / "data" / "processed"

def _latest_recon_csv() -> Path:
    """Return the most-recent reconciliation CSV in data/processed/."""
    csvs = sorted(glob.glob(str(PROCESSED / "reconciliation_*.csv")))
    assert csvs, "No reconciliation CSV found in data/processed/ — run reconcile_sources.py"
    return Path(csvs[-1])


def _db_conn():
    """Return a psycopg2 connection or None if PostgreSQL is unavailable."""
    try:
        import psycopg2
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
        return psycopg2.connect(
            host=os.getenv("PGHOST", "localhost"),
            port=int(os.getenv("PGPORT", 5432)),
            dbname=os.getenv("PGDATABASE", "financedb"),
            user=os.getenv("PGUSER", "postgres"),
            password=os.getenv("PGPASSWORD", "postgres"),
        )
    except Exception:
        return None


class TestParquetFreshness:
    """
    Catch stale exports: app/data/*.parquet must agree with the source files.
    If only the parquet (not the source) is stale, AppTest value tests would
    silently pass while the browser shows wrong numbers.
    """

    def test_reconciliation_row_count(self):
        """reconciliation.parquet row count must equal the latest CSV."""
        csv_df     = pd.read_csv(_latest_recon_csv())
        parquet_df = load("reconciliation")
        assert len(parquet_df) == len(csv_df), (
            f"reconciliation.parquet has {len(parquet_df)} rows but "
            f"{_latest_recon_csv().name} has {len(csv_df)} rows.\n"
            "Re-run scripts/export_reporting_for_app.py."
        )

    def test_reconciliation_matched_count(self):
        """Matched-pair count in parquet must equal the CSV."""
        csv_df     = pd.read_csv(_latest_recon_csv())
        parquet_df = load("reconciliation")
        p_matched = int((parquet_df["match_status"] == "matched").sum())
        c_matched = int((csv_df["match_status"]     == "matched").sum())
        assert p_matched == c_matched, (
            f"reconciliation.parquet shows {p_matched} matched rows "
            f"but the CSV shows {c_matched}.\n"
            "Re-run scripts/export_reporting_for_app.py."
        )

    def test_reconciliation_unmatched_count(self):
        """N-PORT-only count in parquet must equal the CSV."""
        csv_df     = pd.read_csv(_latest_recon_csv())
        parquet_df = load("reconciliation")
        p = int((parquet_df["match_status"] == "nport_only").sum())
        c = int((csv_df["match_status"]     == "nport_only").sum())
        assert p == c, (
            f"reconciliation.parquet shows {p} nport_only rows "
            f"but the CSV shows {c}.\n"
            "Re-run scripts/export_reporting_for_app.py."
        )

    @pytest.mark.skipif(_db_conn() is None, reason="PostgreSQL not reachable")
    def test_db_tables_match_parquet(self):
        """Row counts in app/data/*.parquet must match the reporting schema in PostgreSQL."""
        conn = _db_conn()
        cur  = conn.cursor()
        checks = [
            ("qc_trend",          "SELECT COUNT(*) FROM reporting.qc_trend"),
            ("qc_exceptions",     "SELECT COUNT(*) FROM reporting.qc_exceptions"),
            ("v_fund_size",       "SELECT COUNT(*) FROM reporting.v_fund_size"),
            ("bond_terms",        "SELECT COUNT(*) FROM reporting.bond_terms"),
            ("module5_scorecard", "SELECT COUNT(*) FROM reporting.module5_scorecard"),
            ("cash_recon_scorecard","SELECT COUNT(*) FROM reporting.cash_recon_scorecard"),
            ("cash_expected",     "SELECT COUNT(*) FROM reporting.cash_expected"),
            ("cash_recon_results","SELECT COUNT(*) FROM reporting.cash_recon_results"),
        ]
        failures = []
        for name, sql in checks:
            cur.execute(sql)
            db_count = cur.fetchone()[0]
            parquet_count = len(load(name))
            if db_count != parquet_count:
                failures.append(
                    f"  {name}: parquet={parquet_count}, db={db_count}"
                )
        cur.close()
        conn.close()
        assert not failures, (
            "Parquet files are stale vs PostgreSQL reporting schema.\n"
            "Re-run scripts/export_reporting_for_app.py.\n"
            + "\n".join(failures)
        )


# ── Bond Documents (page 3) ───────────────────────────────────────────────────

class TestBondDocumentsMetrics:
    """Headline KPIs on the Bond Documents page match app/data/bond_terms.parquet."""

    def test_no_exception(self):
        at = run_page(PAGES / "3_Bond_Documents.py")
        assert not at.exception

    def test_bonds_in_view(self):
        bt = load("bond_terms")
        expected_n = str(len(bt))
        at = run_page(PAGES / "3_Bond_Documents.py")
        m = find_metric(at, "Bonds in view")
        assert m is not None, "Metric 'Bonds in view' not found on Bond Documents page"
        assert m.value == expected_n, f"Expected {expected_n}, got {m.value}"

    def test_companies(self):
        bt = load("bond_terms")
        expected_n = str(bt["company"].nunique())
        at = run_page(PAGES / "3_Bond_Documents.py")
        m = find_metric(at, "Companies")
        assert m is not None, "Metric 'Companies' not found on Bond Documents page"
        assert m.value == expected_n, f"Expected {expected_n}, got {m.value}"

    def test_currencies(self):
        bt = load("bond_terms")
        expected_n = str(bt["currency"].nunique())
        at = run_page(PAGES / "3_Bond_Documents.py")
        m = find_metric(at, "Currencies")
        assert m is not None, "Metric 'Currencies' not found on Bond Documents page"
        assert m.value == expected_n, f"Expected {expected_n}, got {m.value}"

    def test_checks_passed(self):
        bc = load("bond_checks")
        n_pass   = int((bc["result"] == "pass").sum())
        n_checks = len(bc)
        expected = f"{n_pass}/{n_checks}"
        at = run_page(PAGES / "3_Bond_Documents.py")
        m = find_metric(at, "Checks passed")
        assert m is not None, "Metric 'Checks passed' not found on Bond Documents page"
        assert m.value == expected, f"Expected {expected}, got {m.value}"

    def test_no_cross_currency_totals(self):
        """The currency-separation st.warning block must appear on the page."""
        at = run_page(PAGES / "3_Bond_Documents.py")
        all_warnings = " ".join(str(w.value) for w in at.warning)
        assert "cross-currency" in all_warnings.lower() or "do not add" in all_warnings.lower(), (
            "Bond Documents page should include a cross-currency st.warning block"
        )


# ── Cash Reconciliation (page 4) ──────────────────────────────────────────────

class TestCashReconciliationMetrics:
    """Headline KPIs on the Cash Reconciliation page match app/data parquet files."""

    def test_no_exception(self):
        at = run_page(PAGES / "4_Cash_Reconciliation.py")
        assert not at.exception

    def test_expected_payments(self):
        expected_df = load("cash_expected")
        expected_n  = str(len(expected_df))
        at = run_page(PAGES / "4_Cash_Reconciliation.py")
        m = find_metric(at, "Expected payments")
        assert m is not None, "Metric 'Expected payments' not found on Cash Reconciliation page"
        assert m.value == expected_n, f"Expected {expected_n}, got {m.value}"

    def test_round1_breaks_planted(self):
        """Round 1 (default selection) planted break count matches scorecard."""
        crsc = load("cash_recon_scorecard")
        v2   = crsc[~crsc["reconciler_version"].str.contains("before_fix")]
        r1   = v2[v2["round_number"] == 1].iloc[0]
        expected_planted = str(int(r1["breaks_planted"]))
        at = run_page(PAGES / "4_Cash_Reconciliation.py")
        m = find_metric(at, "Breaks planted")
        assert m is not None, "Metric 'Breaks planted' not found on Cash Reconciliation page"
        assert m.value == expected_planted, f"Expected {expected_planted}, got {m.value}"

    def test_scorecard_has_four_rows(self):
        """Before/after scorecard must have 4 rows (Round 2 appears twice)."""
        crsc = load("cash_recon_scorecard")
        assert len(crsc) == 4, (
            f"cash_recon_scorecard.parquet should have 4 rows (Round 2 appears as v1 and v2); "
            f"found {len(crsc)}"
        )

    def test_v1_before_round2_score(self):
        """The v1_before_fix row for Round 2 should show 2/11 correct_type."""
        crsc     = load("cash_recon_scorecard")
        before   = crsc[crsc["reconciler_version"].str.contains("before_fix")]
        assert len(before) == 1, "Expected exactly one before_fix row in scorecard"
        r = before.iloc[0]
        assert int(r["correct_type"]) == 2, (
            f"Round 2 v1 should have 2 correct_type, got {r['correct_type']}"
        )
        assert int(r["false_alarms"]) == 7, (
            f"Round 2 v1 should have 7 false_alarms, got {r['false_alarms']}"
        )

    def test_simulated_note_present(self):
        """The page must render an info block noting that bank statements are simulated."""
        at = run_page(PAGES / "4_Cash_Reconciliation.py")
        all_text = " ".join(str(i.value) for i in at.info)
        assert "simulated" in all_text.lower(), (
            "Cash Reconciliation page must include a note that bank statements are simulated"
        )

    def test_usd_fixed_caption_numbers(self):
        """Caption must state the correct count of USD fixed-rate bonds and 2026-payment bonds."""
        bt = load("bond_terms")
        ce = load("cash_expected")
        ce["payment_date"] = pd.to_datetime(ce["payment_date"])
        usd_fixed = bt[(bt["currency"] == "USD") & (bt["rate_type"] == "Fixed")]
        n_usd_fixed = len(usd_fixed)
        n_2026 = ce[
            (ce["payment_date"].dt.year == 2026) & (ce["cusip"].isin(usd_fixed["cusip"]))
        ]["cusip"].nunique()
        at = run_page(PAGES / "4_Cash_Reconciliation.py")
        all_captions = " ".join(c.value for c in at.caption)
        assert str(n_usd_fixed) in all_captions, (
            f"Caption should contain USD fixed-rate bond count {n_usd_fixed}"
        )
        assert str(n_2026) in all_captions, (
            f"Caption should contain 2026-payment bond count {n_2026}"
        )


# ── Checks and Controls (page 5) ──────────────────────────────────────────────

def _cnc_expected_counts() -> dict:
    """Mirror 5_Checks_and_Controls.py status logic to compute expected KPI values."""
    import glob as _glob
    STATUS_ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}

    ic    = load("internal_consistency")
    qce   = load("qc_exceptions")
    dr    = load("dim_rule")
    bc    = load("bond_checks")
    crsc  = load("cash_recon_scorecard")
    recon = load("reconciliation")
    PROC  = REPO / "data" / "processed"

    qce["period_end_date"] = pd.to_datetime(qce["period_end_date"])
    latest_ped = qce["period_end_date"].max()
    exc_latest = qce[qce["period_end_date"] == latest_ped]

    statuses = []

    # IC
    statuses.append("pass" if int(ic["flag"].sum()) == 0 else "fail")

    # Rules
    for _, rule in dr.sort_values("rule_sort").iterrows():
        rc    = rule["rule_code"]
        rg    = rule["rule_group"]
        exc_r = exc_latest[exc_latest["rule_code"] == rc]
        n_open = int((exc_r["status"] == "Open").sum())
        statuses.append("pass" if rg == "Open" and n_open == 0
                        else "fail" if rg == "Open" else "info")

    # V1-V10
    for check in sorted(bc["check"].unique(), key=lambda c: int(c[1:])):
        sub    = bc[bc["check"] == check]
        n_pass = int((sub["result"] == "pass").sum())
        n_fail = int((sub["result"] == "fail").sum())
        statuses.append("fail" if n_fail > 0 else "pass" if n_pass == len(sub) else "info")

    # Module 6 scorecard rows — v2 algorithm only (v1_before_fix excluded from this table)
    v2_only = crsc[~crsc["reconciler_version"].str.contains("before_fix")]
    for _, row in v2_only.sort_values(["round_number", "reconciler_version"]).iterrows():
        correct = int(row["correct_type"])
        planted = int(row["breaks_planted"])
        alarms  = int(row["false_alarms"])
        statuses.append("pass" if correct == planted and alarms == 0 else "fail")

    # F1 freshness
    csvs = sorted(_glob.glob(str(PROC / "reconciliation_*.csv")))
    if csvs:
        csv_df    = pd.read_csv(csvs[-1])
        p_matched = int((recon["match_status"] == "matched").sum())
        c_matched = int((csv_df["match_status"] == "matched").sum())
        fresh     = len(recon) == len(csv_df) and p_matched == c_matched
        statuses.append("pass" if fresh else "fail")

    icons = [STATUS_ICON.get(s, "—") for s in statuses]
    return {
        "total": len(icons),
        "pass":  icons.count("✅"),
        "info":  icons.count("ℹ️"),
        "fail":  icons.count("❌"),
    }


class TestChecksAndControlsMetrics:
    """Headline KPIs on the Checks and Controls page are computed from parquet."""

    def test_no_exception(self):
        at = run_page(PAGES / "5_Checks_and_Controls.py")
        assert not at.exception

    def test_checks_defined(self):
        exp        = _cnc_expected_counts()
        expected_n = str(exp["total"])
        at = run_page(PAGES / "5_Checks_and_Controls.py")
        m = find_metric(at, "Checks defined")
        assert m is not None, "Metric 'Checks defined' not found on Checks and Controls page"
        assert m.value == expected_n, f"Expected {expected_n}, got {m.value}"

    def test_passing_count(self):
        exp        = _cnc_expected_counts()
        expected_n = str(exp["pass"])
        at = run_page(PAGES / "5_Checks_and_Controls.py")
        m = find_metric(at, "Passing")
        assert m is not None, "Metric 'Passing' not found on Checks and Controls page"
        assert m.value == expected_n, f"Expected {expected_n}, got {m.value}"

    def test_info_count(self):
        exp        = _cnc_expected_counts()
        expected_n = str(exp["info"])
        at = run_page(PAGES / "5_Checks_and_Controls.py")
        m = find_metric(at, "Reviewed")
        assert m is not None, "Metric 'Reviewed / info' not found on Checks and Controls page"
        assert m.value == expected_n, f"Expected {expected_n}, got {m.value}"

    def test_failing_count(self):
        exp        = _cnc_expected_counts()
        expected_n = str(exp["fail"])
        at = run_page(PAGES / "5_Checks_and_Controls.py")
        m = find_metric(at, "Failing")
        assert m is not None, "Metric 'Failing' not found on Checks and Controls page"
        assert m.value == expected_n, f"Expected {expected_n}, got {m.value}"

    def test_counts_sum_to_total(self):
        """pass + info + fail must equal total (no unaccounted status category)."""
        exp = _cnc_expected_counts()
        assert exp["pass"] + exp["info"] + exp["fail"] == exp["total"], (
            f"pass({exp['pass']}) + info({exp['info']}) + fail({exp['fail']}) "
            f"= {exp['pass']+exp['info']+exp['fail']} ≠ total({exp['total']})"
        )
