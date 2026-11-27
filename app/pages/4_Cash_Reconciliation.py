"""
Cash Reconciliation — Module 6
Reads only from app/data/*.parquet — no database connection.
"""

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="Cash Reconciliation · Finance Reference Data Platform",
    page_icon="💰",
    layout="wide",
)

DATA = Path(__file__).resolve().parent.parent / "data"


@st.cache_data
def load(name: str) -> pd.DataFrame:
    return pd.read_parquet(DATA / f"{name}.parquet")


# ── Load ──────────────────────────────────────────────────────────────────────

expected = load("cash_expected")
results  = load("cash_recon_results")
crsc     = load("cash_recon_scorecard")
rules    = load("cash_recon_rules")
bt       = load("bond_terms")

expected["payment_date"] = pd.to_datetime(expected["payment_date"])
expected["month"]        = expected["payment_date"].dt.to_period("M").astype(str)

# Pre-compute caption facts (101 USD fixed-rate bonds; 86 have 2026 payments)
_usd_fixed         = bt[(bt["currency"] == "USD") & (bt["rate_type"] == "Fixed")]
N_USD_FIXED        = len(_usd_fixed)
_in_2026           = expected[expected["payment_date"].dt.year == 2026]
N_USD_FIXED_2026   = _in_2026[_in_2026["cusip"].isin(_usd_fixed["cusip"])]["cusip"].nunique()

RESULT_COLOURS = {
    "matched":                     "#2ca02c",
    "cusip_typo":                  "#1f77b4",
    "duplicate":                   "#9467bd",
    "late":                        "#ff7f0e",
    "late_and_short":              "#e377c2",
    "late_needs_review":           "#d62728",
    "short_paid":                  "#bcbd22",
    "large_deduction_needs_review":"#8c564b",
    "missing":                     "#7f7f7f",
    "unexpected":                  "#17becf",
    "split":                       "#aec7e8",
    "combined":                    "#ffbb78",
    "valid_surprise":              "#98df8a",
    "surprise":                    "#c5b0d5",
}

# ── Page header ───────────────────────────────────────────────────────────────

st.title("Cash Reconciliation — Module 6")

st.info(
    "**The bank statements in this module are simulated.**  "
    "Real coupon and principal payment records are not publicly available.  "
    "Module 6 instead generates a synthetic bank statement from the payment schedule "
    "derived from the Module 5 bond terms, plants a controlled set of realistic "
    "discrepancies using a fixed random seed, and then runs the reconciler blind.  "
    "This design lets the reconciler's accuracy be measured against a known ground truth "
    "without using private data.",
    icon="ℹ️",
)

st.markdown(
    "Module 6 tests the reconciliation checker using payments calculated from the "
    "extracted bond terms.  Three test rounds were run: easy (standard break types), "
    "hard (edge cases that broke the first algorithm), and mixed (all 12 break types "
    "together)."
)

st.divider()

# ── Round selector ────────────────────────────────────────────────────────────

v2_sc = crsc[~crsc["reconciler_version"].str.contains("before_fix")].copy()
round_opts = v2_sc.sort_values("round_number")[["round_number", "round_label"]].values.tolist()
round_labels = [f"Round {rn} — {rl}" for rn, rl in round_opts]
round_numbers = [int(rn) for rn, _ in round_opts]

sel_idx = st.selectbox(
    "Select round",
    range(len(round_labels)),
    format_func=lambda i: round_labels[i],
)
sel_round = round_numbers[sel_idx]
sel_sc = v2_sc[v2_sc["round_number"] == sel_round].iloc[0]

round_results = results[results["round"] == sel_round].copy()

# ── KPIs ──────────────────────────────────────────────────────────────────────

n_expected    = len(expected)
n_planted     = int(sel_sc["breaks_planted"])
n_found       = int(sel_sc["breaks_found"])
n_correct     = int(sel_sc["correct_type"])
n_alarms      = int(sel_sc["false_alarms"])

k1, k2, k3, k4 = st.columns(4)
k1.metric("Expected payments (2026)", n_expected,
          help="Scheduled coupons and principal payments for all 142 bonds in calendar year 2026")
k2.metric("Breaks planted",           n_planted,
          help=f"Discrepancies planted in the synthetic bank statement for {sel_sc['round_label']}")
k3.metric("Found and classified",     f"{n_found}/{n_planted}",
          help="How many planted breaks the reconciler detected (v2 algorithm)")
k4.metric("Correctly typed",          f"{n_correct}/{n_planted}",
          help=f"Break type labels that matched the planted type exactly; {n_alarms} false alarms")

st.caption(
    f"Showing results for {sel_sc['round_label']} (v2 algorithm).  "
    "Payment schedule covers calendar year 2026."
)
st.divider()

# ── Chart: Expected cash flows in 2026 ───────────────────────────────────────

st.subheader("Expected cash flows in 2026")

monthly = (
    expected.groupby(["month", "company", "flow_type"])["expected_amount"]
    .sum()
    .reset_index()
)
monthly_total = monthly.groupby("month")["expected_amount"].sum().reset_index()
months_sorted = sorted(monthly["month"].unique())

companies_exp = sorted(expected["company"].unique())
company_colours = {
    "Alphabet":          "#1f77b4",
    "Apple":             "#ff7f0e",
    "Broadcom":          "#2ca02c",
    "Eli Lilly":         "#d62728",
    "Johnson & Johnson": "#9467bd",
    "Meta":              "#8c564b",
    "Microsoft":         "#e377c2",
}

fig_exp = go.Figure()
for company in companies_exp:
    sub = monthly[monthly["company"] == company].groupby("month")["expected_amount"].sum().reset_index()
    sub = sub.set_index("month").reindex(months_sorted, fill_value=0).reset_index()
    fig_exp.add_trace(go.Bar(
        name=company,
        x=sub["month"],
        y=sub["expected_amount"] / 1e6,
        marker_color=company_colours.get(company, "#aec7e8"),
        hovertemplate="<b>%{x}</b><br>" + company + ": $%{y:,.1f}M<extra></extra>",
    ))

fig_exp.update_layout(
    barmode="stack",
    xaxis_title="Month",
    yaxis_title="Expected payment ($M)",
    yaxis=dict(rangemode="tozero"),
    xaxis=dict(tickangle=-45, tickfont=dict(size=9)),
    margin=dict(t=20, b=120),
    legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="center", x=0.5),
    height=400,
)
st.plotly_chart(fig_exp, width="stretch")
st.caption(
    f"Schedule covers the {N_USD_FIXED} USD fixed-rate bonds, {N_USD_FIXED_2026} of which "
    f"have at least one coupon or principal payment in 2026.  "
    f"{n_expected} payments totalling "
    f"${expected['expected_amount'].sum()/1e6:,.0f}M in calendar year 2026.  "
    "Amounts are based on simulated face holdings ($10M per bond)."
)

st.divider()

# ── Break types in selected round ─────────────────────────────────────────────

st.subheader(f"Break types — {sel_sc['round_label']}")

break_rows = round_results[round_results["result"] != "matched"].copy()

type_counts = (
    break_rows.groupby("result").size()
    .reset_index(name="count")
    .sort_values("count", ascending=True)
)

fig_types = go.Figure(go.Bar(
    x=type_counts["count"],
    y=type_counts["result"],
    orientation="h",
    marker_color=[RESULT_COLOURS.get(r, "#aec7e8") for r in type_counts["result"]],
    hovertemplate="<b>%{y}</b>: %{x}<extra></extra>",
))
fig_types.update_layout(
    xaxis_title="Count",
    yaxis_title="Break type",
    xaxis=dict(rangemode="tozero", dtick=1),
    margin=dict(t=20, b=40, l=200),
    height=max(260, 40 * len(type_counts) + 80),
)
st.plotly_chart(fig_types, width="stretch")
st.caption(
    "Only non-matched rows shown.  "
    "'late_needs_review' and 'large_deduction_needs_review' require analyst follow-up.  "
    "Unexpected rows arise when the reconciler detects a statement entry with no matching expected payment."
)

st.divider()

# ── Break list sorted by amount ───────────────────────────────────────────────

st.subheader(f"Break list — {sel_sc['round_label']} — sorted by amount at stake")

if break_rows.empty:
    st.info("No breaks in this round (all matched).")
else:
    # Join with cash_expected to get payment_date
    brk = break_rows.merge(
        expected[["payment_id", "payment_date"]],
        left_on="expected_payment_id", right_on="payment_id", how="left"
    ).sort_values("amount_expected", ascending=False).copy()

    def _fmt_diff(x):
        """Display difference as negative-for-shortfall, positive-for-overpayment."""
        if pd.isna(x):
            return "—"
        d = -x  # stored difference = expected - received; negate so shortfall is negative
        if d == 0:
            return "$0"
        return f"-${abs(d):,.0f}" if d < 0 else f"+${d:,.0f}"

    brk["amount_expected_fmt"] = brk["amount_expected"].apply(
        lambda x: f"${x:,.0f}" if pd.notna(x) else "—")
    brk["amount_received_fmt"] = brk["amount_received"].apply(
        lambda x: f"${x:,.0f}" if pd.notna(x) else "—")
    brk["difference_fmt"] = brk["difference"].apply(_fmt_diff)
    brk["days_late_fmt"] = brk["days_late"].apply(
        lambda x: f"{x:.0f}" if pd.notna(x) and x != 0 else "—")
    brk["needs_review_fmt"] = brk["needs_review"].apply(lambda x: "Yes" if x else "—")
    brk["payment_date_fmt"] = pd.to_datetime(brk["payment_date"], errors="coerce").dt.strftime("%Y-%m-%d")

    brk_show = brk[["payment_date_fmt","cusip","result","amount_expected_fmt",
                     "amount_received_fmt","difference_fmt","days_late_fmt","needs_review_fmt"]].copy()
    brk_show.columns = ["Payment date","CUSIP","Break type","Expected","Received","Difference","Days late","Needs review"]
    st.dataframe(brk_show, width="stretch", hide_index=True)
    st.caption(
        "Sorted by expected amount descending — largest payments at stake first.  "
        "Negative difference = shortfall (received less than expected); "
        "positive = overpayment.  "
        "'Needs review' = break type requires analyst investigation before closing."
    )

st.divider()

# ── Before / after scorecard ──────────────────────────────────────────────────

st.subheader("Reconciler scorecard — all rounds")

st.markdown(
    "Round 2 was run twice: first with the original v1 algorithm (6 passes), "
    "then with the improved v2 algorithm (9 passes).  "
    "The v1 score of **2 / 11** with **7 false alarms** is the honest baseline."
)

sc_rows = []
for _, row in crsc.sort_values(["round_number","reconciler_version"]).iterrows():
    is_before = "before_fix" in str(row["reconciler_version"])
    sc_rows.append({
        "Round":         row["round_label"],
        "Algorithm":     row["reconciler_version"],
        "Breaks planted":row["breaks_planted"],
        "Found":         row["breaks_found"],
        "Correctly typed": row["correct_type"],
        "False alarms":  row["false_alarms"],
        "Score":         f"{row['correct_type']}/{row['breaks_planted']}",
        "Notes":         row["notes"][:120] + "…" if len(str(row["notes"])) > 120 else row["notes"],
    })

sc_df = pd.DataFrame(sc_rows)
st.dataframe(sc_df, width="stretch", hide_index=True,
             column_config={"Notes": st.column_config.TextColumn("Notes", width="large")})
st.caption(
    "Round 1 = easy (standard breaks: missing, late, short, duplicate, surprise, CUSIP typo).  "
    "Round 2 hard = added very_late, large_deduction, split, combined.  "
    "Round 3 = all 12 break types in one statement.  "
    "v1_before_fix = original 6-pass algorithm.  v2_after_fix and v2 = improved 9-pass algorithm."
)

st.divider()

# ── Reconciliation rules ──────────────────────────────────────────────────────

st.subheader("Reconciliation rules")

rules_show = rules.copy()
rules_show.columns = ["Rule", "Threshold", "Unit", "Label (within)", "Label (beyond)", "Plain English"]
st.dataframe(rules_show, width="stretch", hide_index=True,
             column_config={"Plain English": st.column_config.TextColumn("Plain English", width="large")})
st.caption(
    "These two rules are machine-readable and stored in cash_recon_rules.csv.  "
    "They define the boundary between 'standard processing variation' (auto-approved label) "
    "and 'unusual — needs analyst review' (escalated label)."
)

st.divider()

# ── Glossary ──────────────────────────────────────────────────────────────────

with st.expander("Glossary"):
    st.markdown(
        """
| Term | Meaning |
|---|---|
| Cash reconciliation | Matching expected coupon / principal payments against received payments |
| Break | A discrepancy between expected and received: missing, late, short, duplicate, etc. |
| Planted break | A deliberate discrepancy introduced into the simulated bank statement to test the reconciler |
| False alarm | A reconciler flag on a row that is actually fine (expected = received) |
| Correctly typed | The reconciler's break-type label matches the planted type |
| v1 algorithm | Original 6-pass reconciler; no same-bond pairing pass |
| v2 algorithm | Improved 9-pass reconciler; adds split/combined detection and same-bond pairing |
| late_needs_review | Arrived more than 5 business days late — too late to auto-approve |
| large_deduction_needs_review | Shortfall > 1% of expected amount — requires analyst investigation |
| split | One expected payment arrived as two or more partial payments |
| combined | Two expected payments arrived as a single combined payment |
| CUSIP typo | Payment references a CUSIP with a one-character difference from the expected one |
| surprise | Payment references a CUSIP not in the portfolio at all |
| Face holding | Notional principal amount held ($10M per bond in this simulation) |
        """
    )
