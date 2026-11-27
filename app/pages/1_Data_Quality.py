"""
Data Quality — Modules 3 & 4
Reads only from app/data/*.parquet — no database connection.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="Data Quality · Finance Reference Data Platform",
    page_icon="🔍",
    layout="wide",
)

DATA = Path(__file__).resolve().parent.parent / "data"


@st.cache_data
def load(name: str) -> pd.DataFrame:
    return pd.read_parquet(DATA / f"{name}.parquet")


DATE_RANGE_CAPTION = (
    "Mar 2025 – Jun 2026 · one snapshot per fiscal quarter "
    "(SEC N-PORT filings, IVV series)"
)

RULE_LABELS = [
    "R1 · No identifier",
    "R2 · Weight out of range",
    "R3 · Negative value",
    "R4 · Duplicate CUSIP",
    "R5 · Missing category",
    "R6 · Possible cash instrument",
    "E1 · Cash collateral / money-market",
    "D1 · Derivative (review only)",
]
RULE_COLS  = ["r1_count", "r2_count", "r3_count", "r4_count",
              "r5_count", "r6_count", "e1_count", "d1_count"]

DISPLAY_COLS = [
    "name", "status", "reason",
    "cusip", "isin", "assetcat", "issuercat", "iscashcollateral",
    "balance", "valusd", "pctval",
]
DISPLAY_RENAME = {
    "assetcat": "assetCat", "issuercat": "issuerCat",
    "iscashcollateral": "isCashCollateral",
    "valusd": "valUSD", "pctval": "pctVal",
    "status": "Status", "reason": "Reason",
}


def _fmt_display(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "balance" in df.columns:
        df["balance"] = df["balance"].apply(
            lambda x: f"{x:,.2f}" if pd.notna(x) else "—")
    if "valusd" in df.columns:
        df["valusd"] = df["valusd"].apply(
            lambda x: f"${x:,.2f}" if pd.notna(x) else "—")
    if "pctval" in df.columns:
        df["pctval"] = df["pctval"].apply(
            lambda x: f"{x:.4f}%" if pd.notna(x) else "—")
    for col in ("cusip", "isin", "iscashcollateral"):
        if col in df.columns:
            df[col] = (df[col].fillna("").astype(str).str.strip()
                       .replace({"": "—", "N/A": "—", "n/a": "—", "NA": "—"}))
    return df


# ── Load data ─────────────────────────────────────────────────────────────────

aum  = load("v_fund_size")
qc   = load("qc_trend")
exc  = load("qc_exceptions")
rules = load("dim_rule")
ml   = load("ml_anomalies")

aum["period_end_date"] = pd.to_datetime(aum["period_end_date"])
qc["period_end_date"]  = pd.to_datetime(qc["period_end_date"])
if "period_end_date" in exc.columns:
    exc["period_end_date"] = pd.to_datetime(exc["period_end_date"])

x_dates  = qc["period_end_date"]
x_labels = [d.strftime("%b %Y") for d in x_dates]
latest_period = x_dates.iloc[-1].strftime("%b %Y")

# ── Page header ───────────────────────────────────────────────────────────────

st.title("Data Quality — Modules 3 & 4")
st.markdown(
    "Module 3 applies six automated rules to every holding and runs an "
    "IsolationForest anomaly-detection pass.  Module 4 tracks exceptions "
    "over time, investigates root causes, and re-scopes over-broad rules."
)
st.caption(f"Holdings as of: {DATE_RANGE_CAPTION}")

st.divider()

# ── Chart 1: Fund size ────────────────────────────────────────────────────────

st.subheader("Fund size over time")

fig_aum = go.Figure()
fig_aum.add_trace(go.Scatter(
    x=aum["period_end_date"],
    y=aum["total_value_usd_bn"],
    mode="lines+markers",
    line=dict(width=2.5, color="#1f77b4"),
    marker=dict(size=8),
    hovertemplate="<b>%{x|%b %Y}</b><br>AUM: $%{y:.1f}B<extra></extra>",
))
fig_aum.update_layout(
    xaxis=dict(
        title="Holdings as of",
        tickvals=aum["period_end_date"].tolist(),
        ticktext=[d.strftime("%b %Y") for d in aum["period_end_date"]],
    ),
    yaxis_title="Total value ($ billions)",
    yaxis=dict(rangemode="tozero", tickprefix="$", ticksuffix="B"),
    hovermode="x unified",
    margin=dict(t=20, b=20),
)
st.plotly_chart(fig_aum, width="stretch")
st.caption(
    "Total USD value of all holdings per quarter.  Axes start at zero.  "
    + DATE_RANGE_CAPTION
)

st.divider()

# ── Chart 2a: Open vs Expected exceptions over time ───────────────────────────

st.subheader("Data quality exceptions over time")

expected_total = qc["d1_count"] + qc["e1_count"]

fig_exc = go.Figure()
fig_exc.add_trace(go.Scatter(
    x=x_dates,
    y=qc["total_unique_exceptions"],
    name="Open (R1–R6 + N1)",
    mode="lines+markers",
    line=dict(width=2.5, color="#d62728"),
    marker=dict(size=9),
    hovertemplate="<b>%{x|%b %Y}</b><br>Open: %{y}<extra></extra>",
))
fig_exc.add_trace(go.Scatter(
    x=x_dates,
    y=expected_total,
    name="Expected (D1 + E1)",
    mode="lines+markers",
    line=dict(width=2, color="#78909c", dash="dot"),
    marker=dict(size=7),
    hovertemplate="<b>%{x|%b %Y}</b><br>Expected: %{y}<extra></extra>",
))
fig_exc.update_layout(
    xaxis=dict(
        title="Holdings as of",
        tickvals=x_dates.tolist(),
        ticktext=x_labels,
    ),
    yaxis_title="Number of flagged rows",
    yaxis=dict(range=[0, max(5, int(qc["total_unique_exceptions"].max()) + 1)], dtick=1),
    hovermode="x unified",
    margin=dict(t=20, b=100),
    legend=dict(orientation="h", yanchor="top", y=-0.18, xanchor="center", x=0.5),
)
st.plotly_chart(fig_exc, width="stretch")
st.caption(
    "Red = open exceptions (R1–R6 violations + any unreviewed N1 rows) — these need action.  "
    "Grey dotted = reviewed rows with an Expected label (D1 derivatives, E1 cash collateral) — "
    "not counted as errors.  " + DATE_RANGE_CAPTION
)

# ── Chart 2b: Rule heatmap ────────────────────────────────────────────────────

st.subheader("Exception detail by rule and quarter")

n_quarters = len(x_labels)
n_rules    = len(RULE_COLS)
counts     = qc[RULE_COLS].values.T

EXPECTED_ROW_INDICES = {n_rules - 2, n_rules - 1}

z_color = np.zeros((n_rules, n_quarters), dtype=float)
for ri in range(n_rules):
    row_counts = counts[ri]
    all_same   = len(set(row_counts)) == 1
    for qi in range(n_quarters):
        v = row_counts[qi]
        if ri in EXPECTED_ROW_INDICES:
            z_color[ri, qi] = 3
        elif v == 0:
            z_color[ri, qi] = 0
        elif all_same:
            z_color[ri, qi] = 1
        elif qi > 0 and v != row_counts[qi - 1]:
            z_color[ri, qi] = 2
        else:
            z_color[ri, qi] = 1

colorscale = [
    [0.00, "#fafafa"], [0.32, "#fafafa"],
    [0.33, "#b0bec5"], [0.65, "#b0bec5"],
    [0.66, "#ff8f00"], [0.98, "#ff8f00"],
    [0.99, "#78909c"], [1.00, "#78909c"],
]

fig_heat = go.Figure(go.Heatmap(
    z=z_color.tolist(),
    x=x_labels,
    y=RULE_LABELS,
    colorscale=colorscale,
    zmin=0, zmax=3,
    showscale=False,
    hoverongaps=False,
    customdata=counts.tolist(),
    hovertemplate="<b>%{y}</b><br>%{x}<br>Count: %{customdata}<extra></extra>",
))
for ri in range(n_rules):
    for qi in range(n_quarters):
        font_color = "#ffffff" if z_color[ri, qi] == 3 else "#444444"
        fig_heat.add_annotation(
            x=x_labels[qi], y=RULE_LABELS[ri],
            text=str(int(counts[ri, qi])),
            showarrow=False,
            font=dict(size=14, color=font_color),
        )
fig_heat.update_layout(
    xaxis_title="Holdings as of",
    yaxis=dict(autorange="reversed"),
    margin=dict(t=20, b=20, l=240),
    height=400,
)
fig_heat.add_shape(
    type="line", xref="paper", yref="y",
    x0=-0.02, x1=1.02, y0=5.5, y1=5.5,
    line=dict(color="#999999", width=1.5, dash="dot"),
)
st.plotly_chart(fig_heat, width="stretch")
st.caption(
    "Each cell shows the count for that rule and quarter.  "
    "**White** = zero.  **Grey-blue** = same count every quarter (structural).  "
    "**Amber** = count changed from the previous quarter.  "
    "**Steel grey** (below divider) = Expected label — not counted as errors.  "
    + DATE_RANGE_CAPTION
)

st.divider()

# ── Quarter drill-down ────────────────────────────────────────────────────────

st.subheader("Quarter drill-down — flagged rows")

quarter_opts = {d.strftime("%b %Y"): d.strftime("%Y-%m-%d") for d in x_dates}
selected_q   = st.selectbox(
    "Holdings as of",
    list(quarter_opts.keys()),
    index=len(quarter_opts) - 1,
    key="drill_down_quarter",
)
selected_ped = quarter_opts[selected_q]

exc_q       = exc[exc["period_end_date"].dt.strftime("%Y-%m-%d") == selected_ped].copy()
error_df    = exc_q[exc_q["status"] == "Open"].reset_index(drop=True)
expected_df = exc_q[exc_q["status"] == "Expected"].reset_index(drop=True)

total_holdings = int(
    qc.loc[qc["period_end_date"].dt.strftime("%Y-%m-%d") == selected_ped, "total_holdings"].iloc[0]
)

t1, t2, t3 = st.columns(3)
t1.metric("Total holdings",               total_holdings)
t2.metric("Open exceptions (R1–R6 + N1)", len(error_df))
t3.metric("Expected (D1 + E1)",           len(expected_df))

if len(error_df) == 0:
    st.success("No open exceptions for this quarter.")
else:
    st.markdown(f"**{len(error_df)} open exception{'s' if len(error_df) != 1 else ''} — R1–R6 + N1**")
    avail = [c for c in DISPLAY_COLS if c in error_df.columns]
    st.dataframe(_fmt_display(error_df[avail]).rename(columns=DISPLAY_RENAME),
                 width="stretch", hide_index=True)

if len(expected_df) > 0:
    with st.expander(
        f"Expected — reviewed (D1 derivatives + E1 cash collateral / STIV) "
        f"— {len(expected_df)} row{'s' if len(expected_df) != 1 else ''}",
        expanded=False,
    ):
        avail = [c for c in DISPLAY_COLS if c in expected_df.columns]
        st.dataframe(_fmt_display(expected_df[avail]).rename(columns=DISPLAY_RENAME),
                     width="stretch", hide_index=True)

st.divider()

# ── Rules dictionary ──────────────────────────────────────────────────────────

st.subheader("Rules dictionary")

rule_display = rules[["rule_code", "rule_name", "rule_group", "rule_description"]].copy()
rule_display.columns = ["Code", "Name", "Group", "Description"]

open_rules     = rule_display[rule_display["Group"] == "Open"]
expected_rules = rule_display[rule_display["Group"] == "Expected"]

col_r1, col_r2 = st.columns([2, 1])
with col_r1:
    st.markdown("**Open rules (R1–R6, N1) — trigger action**")
    st.dataframe(open_rules[["Code", "Name", "Description"]],
                 width="stretch", hide_index=True)
with col_r2:
    st.markdown("**Expected labels (E1, D1) — reviewed, not errors**")
    st.dataframe(expected_rules[["Code", "Name", "Description"]],
                 width="stretch", hide_index=True)

st.caption(
    "R4 checks for duplicate CUSIPs within the same isCashCollateral bucket (not across buckets).  "
    "R6 skips correctly-labelled STIV money-market fund rows.  "
    "Both rules were re-scoped in Module 4 after root-cause investigation."
)

st.divider()

# ── ML outliers ───────────────────────────────────────────────────────────────

st.subheader("Machine-learning anomaly detection — IsolationForest results")

st.markdown(
    "IsolationForest (contamination=0.02, random_state=42) was run on the latest "
    "quarter's holdings using three features: valUSD, pctVal, and balance — all "
    "z-scored before fitting.  11 of 508 rows were flagged."
)

anomalies = ml[ml["if_prediction"] == -1].copy()
anomalies = anomalies.sort_values("valusd", ascending=False).reset_index(drop=True)

# Add explanations
def _explain(row) -> str:
    if row["ml_label"] == "anomaly (also rule-flagged)":
        return ("Securities-lending cash collateral held in a money-market fund. "
                "balance ≈ valUSD at $1 NAV is expected for this structure. "
                "Already flagged by Rule R4; reviewed as E1.")
    # CUSIP 066922477 — directly-held money-market fund (led to Rule R6)
    if "477" in str(row.get("cusip", "")) or (
        str(row.get("name", "")).startswith("BlackRock")
        and row.get("ml_label") == "anomaly (ML only)"
    ):
        return ("Money-market fund held directly as a short-term investment, not as "
                "securities-lending cash collateral.  balance ≈ valUSD at $1 NAV is "
                "expected for this vehicle.  ML finding — not flagged by rules initially; "
                "led to the creation of Rule R6.")
    # Large equity positions
    return ("Largest equity holding in the fund — statistically unusual by size, "
            "not a data error.  Flagged purely because extreme values are rare.")

anomalies["explanation"] = anomalies.apply(_explain, axis=1)

# Separate the two BlackRock rows for special treatment
anom_equity = anomalies[anomalies["ml_label"] == "anomaly (ML only)"].copy()
anom_rule   = anomalies[anomalies["ml_label"] == "anomaly (also rule-flagged)"].copy()

# Second BlackRock row (ML only, not already rule-flagged)
second_bk = anom_equity[anom_equity["name"].str.contains("BlackRock", na=False)].copy()
anom_equity_clean = anom_equity[~anom_equity["name"].str.contains("BlackRock", na=False)].copy()

col_a, col_b = st.columns([2, 1])

with col_a:
    st.markdown("**9 largest equity positions — ML flagged, not data errors**")
    eq_show = anom_equity_clean[["name", "cusip", "pctval", "valusd"]].copy()
    eq_show["pctval"]  = eq_show["pctval"].apply(lambda x: f"{x:.2f}%")
    eq_show["valusd"]  = eq_show["valusd"].apply(lambda x: f"${x/1e9:.1f}B")
    eq_show.columns    = ["Name", "CUSIP", "% of fund", "Value (USD)"]
    st.dataframe(eq_show, width="stretch", hide_index=True)
    st.caption(
        "These 9 holdings are the fund's largest positions.  IsolationForest flags "
        "them because extreme values are rare in the dataset — not because there is "
        "anything wrong with the data."
    )

with col_b:
    st.markdown("**2 BlackRock money-market holdings**")
    bk_all = pd.concat([anom_rule, second_bk], ignore_index=True)
    bk_show = bk_all[["name", "cusip", "ml_label", "explanation"]].copy()
    bk_show.columns = ["Name", "CUSIP", "Label", "Explanation"]
    st.dataframe(bk_show, width="stretch", hide_index=True)
    st.caption(
        "One was already flagged by Rule R4 (duplicate CUSIP) and reviewed as E1.  "
        "The other — a new ML find — led to the creation and later narrowing of Rule R6."
    )

st.divider()

# ── Glossary ──────────────────────────────────────────────────────────────────

with st.expander("Glossary"):
    st.markdown(
        """
| Term | Meaning |
|---|---|
| Exception | A data row that failed at least one quality rule |
| Open exception | An exception that has not yet been explained or resolved |
| Expected | A row that consistently fails a rule for a known, valid structural reason (e.g. a money-market fund) |
| R1–R6 | The six automated quality rules (see rules dictionary above) |
| E1 | Label for a money-market or cash-collateral holding: balance ≈ valUSD at $1 NAV is expected |
| D1 | Label for a derivative (exchange-traded futures contract): held at zero or near-zero value |
| N1 | Label for a new STIV or derivative row that has not yet been reviewed |
| IsolationForest | A machine-learning algorithm that assigns anomaly scores by measuring how easy each point is to isolate from the rest |
| contamination | The fraction of data the model expects to be anomalous (set to 0.02 = 2%) |
| pctVal | Each holding's weight as a percentage of the fund's total value |
| valUSD | US-dollar market value of the holding |
| balance | Number of shares or units held |
| STIV | Short-term investment vehicle — a money-market fund used to hold cash collateral |
        """
    )
