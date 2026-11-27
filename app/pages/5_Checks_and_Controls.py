"""
Checks and Controls — all automatic checks in the project.
Reads only from app/data/*.parquet — no database connection.
"""

from pathlib import Path
import glob

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Checks and Controls · Finance Reference Data Platform",
    page_icon="✅",
    layout="wide",
)

DATA   = Path(__file__).resolve().parent.parent / "data"
REPO   = Path(__file__).resolve().parent.parent.parent
PROC   = REPO / "data" / "processed"


@st.cache_data
def load(name: str) -> pd.DataFrame:
    return pd.read_parquet(DATA / f"{name}.parquet")


# ── Load ──────────────────────────────────────────────────────────────────────

ic      = load("internal_consistency")
qce     = load("qc_exceptions")
dr      = load("dim_rule")
bc      = load("bond_checks")
crsc    = load("cash_recon_scorecard")
recon   = load("reconciliation")
qct     = load("qc_trend")

qce["period_end_date"] = pd.to_datetime(qce["period_end_date"])
qct["period_end_date"] = pd.to_datetime(qct["period_end_date"])

latest_ped      = qce["period_end_date"].max()
latest_ped_str  = latest_ped.strftime("%b %Y")
exc_latest      = qce[qce["period_end_date"] == latest_ped]


# ── Build checks table ────────────────────────────────────────────────────────

STATUS_ICON = {
    "pass":    "✅",
    "warn":    "⚠️",
    "fail":    "❌",
    "info":    "ℹ️",
    "pending": "🔄",
}

def _icon(s):
    return STATUS_ICON.get(s, "—")


def build_checks() -> pd.DataFrame:
    rows = []

    # ── Module 2: internal consistency ────────────────────────────────────────
    n_ic      = len(ic)
    n_ic_fail = int(ic["flag"].sum())
    rows.append({
        "Module":    "2 — Source comparison",
        "Check ID":  "IC",
        "Name":      "Internal consistency",
        "Plain English": "pctVal ≈ valUSD ÷ total fund value (tolerance 0.001 pp)",
        "Result":    f"{n_ic_fail} / {n_ic} fail",
        "_status":   "pass" if n_ic_fail == 0 else "fail",
    })

    # ── Module 3: rules and review labels ─────────────────────────────────────
    for _, rule in dr.sort_values("rule_sort").iterrows():
        rc   = rule["rule_code"]
        rg   = rule["rule_group"]
        exc_r = exc_latest[exc_latest["rule_code"] == rc]
        n_open     = int((exc_r["status"] == "Open").sum())
        n_expected = int((exc_r["status"] == "Expected").sum())

        if rg == "Open":
            result  = f"{n_open} open (latest quarter: {latest_ped_str})" if n_open else f"0 open (latest quarter: {latest_ped_str})"
            status  = "pass" if n_open == 0 else "fail"
        else:
            result  = f"{n_expected} expected (latest quarter: {latest_ped_str})"
            status  = "info"

        rows.append({
            "Module":        "3 — Data quality",
            "Check ID":      rc,
            "Name":          rule["rule_name"],
            "Plain English": rule["rule_description"],
            "Result":        result,
            "_status":       status,
        })

    # ── Module 5: V1–V10 ──────────────────────────────────────────────────────
    CHECK_NAME_OVERRIDE = {
        "V6": "Dates in the right order, and maturity year matches the bond name",
    }
    result_order = ["pass", "not_applicable", "expected_reviewed", "not_found", "fail"]
    result_short = {
        "pass":              "pass",
        "not_applicable":    "N/A",
        "expected_reviewed": "reviewed",
        "not_found":         "not found",
        "fail":              "FAIL",
    }
    checks_m5 = sorted(bc["check"].unique(), key=lambda c: int(c[1:]))
    for check in checks_m5:
        check_bc  = bc[bc["check"] == check]
        name      = CHECK_NAME_OVERRIDE.get(check, check_bc.iloc[0]["check_name"])
        n_total   = len(check_bc)
        n_pass    = int((check_bc["result"] == "pass").sum())
        n_fail    = int((check_bc["result"] == "fail").sum())
        counts    = {r: int((check_bc["result"] == r).sum()) for r in result_order if (check_bc["result"] == r).sum() > 0}
        parts     = [f"{v} {result_short[k]}" for k, v in counts.items()]
        result_str = "; ".join(parts)
        rows.append({
            "Module":        "5 — Bond documents",
            "Check ID":      check,
            "Name":          name,
            "Plain English": f"Bond-level: {name.lower()} (applied to all {n_total} bonds)",
            "Result":        result_str,
            "_status":       "fail" if n_fail > 0 else "pass" if n_pass == n_total else "info",
        })

    # ── Module 6: reconciliation scoring (v2 algorithm only) ─────────────────
    # The v1_before_fix score (Round 2, 2/11) is shown on the Cash Reconciliation
    # page as historical context; it is not an open check here.
    for _, row in crsc[~crsc["reconciler_version"].str.contains("before_fix")].sort_values(
        ["round_number", "reconciler_version"]
    ).iterrows():
        planted    = int(row["breaks_planted"])
        correct    = int(row["correct_type"])
        alarms     = int(row["false_alarms"])
        result_str = f"{correct}/{planted} correctly typed; {alarms} false alarms"
        status     = "pass" if correct == planted and alarms == 0 else "fail"
        rows.append({
            "Module":        "6 — Cash reconciliation",
            "Check ID":      f"R{row['round_number']}-{row['reconciler_version']}",
            "Name":          row["round_label"],
            "Plain English": "Planted breaks found and correctly classified by the reconciler (v2 algorithm)",
            "Result":        result_str,
            "_status":       status,
        })

    # ── App: parquet freshness ─────────────────────────────────────────────────
    csvs = sorted(glob.glob(str(PROC / "reconciliation_*.csv")))
    if csvs:
        csv_df    = pd.read_csv(csvs[-1])
        p_rows    = len(recon)
        c_rows    = len(csv_df)
        p_matched = int((recon["match_status"] == "matched").sum())
        c_matched = int((csv_df["match_status"] == "matched").sum())
        fresh     = p_rows == c_rows and p_matched == c_matched
        rows.append({
            "Module":        "App — freshness",
            "Check ID":      "F1",
            "Name":          "Reconciliation parquet vs CSV",
            "Plain English": "app/data/reconciliation.parquet rows and matched count equal the latest processed CSV",
            "Result":        f"{'Match' if fresh else 'MISMATCH'}: parquet {p_rows} rows / {p_matched} matched  ·  CSV {c_rows} rows / {c_matched} matched",
            "_status":       "pass" if fresh else "fail",
        })

    df = pd.DataFrame(rows)
    df["Status"] = df["_status"].map(_icon)
    return df.drop(columns=["_status"])


checks_df = build_checks()

# ── Page header ───────────────────────────────────────────────────────────────

st.title("Checks and Controls")
st.markdown(
    "Every automatic check in the platform in one place — where it runs, "
    "what it looks for in plain language, and its current result computed live "
    "from the exported data files."
)

st.divider()

# ── KPIs ──────────────────────────────────────────────────────────────────────

n_total   = len(checks_df)
n_pass    = int((checks_df["Status"] == "✅").sum())
n_info    = int((checks_df["Status"] == "ℹ️").sum())
n_fail    = int((checks_df["Status"] == "❌").sum())

k1, k2, k3, k4 = st.columns(4)
k1.metric("Checks defined",           n_total,
          help=f"Total = {n_pass} passing + {n_info} reviewed/info + {n_fail} failing")
k2.metric("Passing (✅)",              n_pass,  help="Zero open issues or all correctly typed")
k3.metric("Reviewed / info (ℹ️)",     n_info,  help="Review-status labels: expected deviations and derivatives that have been reviewed and accepted")
k4.metric("Failing (❌)",              n_fail,  help="Open issues requiring attention")

st.divider()

# ── Filter ────────────────────────────────────────────────────────────────────

modules = ["All modules"] + sorted(checks_df["Module"].unique())
sel_module = st.selectbox("Filter by module", modules)

view = checks_df if sel_module == "All modules" else checks_df[checks_df["Module"] == sel_module]

# ── Checks table ──────────────────────────────────────────────────────────────

display_cols = ["Status", "Module", "Check ID", "Name", "Plain English", "Result"]
st.dataframe(
    view[display_cols],
    width="stretch",
    hide_index=True,
    column_config={
        "Plain English": st.column_config.TextColumn("Plain English", width="large"),
        "Result":        st.column_config.TextColumn("Result",        width="large"),
    },
    height=min(60 * len(view) + 60, 700),
)
st.caption(
    f"Showing {len(view)} of {n_total} checks.  "
    f"Results computed from app/data/*.parquet as of this page load.  "
    f"ℹ️ rows = review-status labels (expected deviations and derivatives accepted by a reviewer); "
    f"these are not errors.  All Module 6 scores shown are v2 algorithm."
)

st.divider()

# ── Glossary ──────────────────────────────────────────────────────────────────

with st.expander("Glossary"):
    st.markdown(
        """
| Term | Meaning |
|---|---|
| ✅ Pass | Check ran and found no issues (or all issues have been reviewed and accepted) |
| ℹ️ Reviewed / info | Review-status label — an expected deviation or derivative reviewed and accepted; not an error |
| ❌ Fail | Check ran and found unresolved issues |
| IC | Internal consistency: pctVal ≈ valUSD ÷ total fund value |
| R1–R6 | Data-quality rules applied to every holding in every quarter |
| D1 / E1 / N1 | Review-status labels: Derivative reviewed / Expected / New–needs review |
| V1–V10 | Bond-document validation checks applied to every extracted bond |
| R1–R3 (Module 6) | Cash-reconciliation scoring for each test round |
| F1 | App freshness check: app/data parquet matches the processed source CSV |
        """
    )
