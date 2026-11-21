"""
Finance Reference Data Platform — Quality Dashboard
Module 4: Streamlit app for QC analytics and holdings trend visualisation.

X-axis uses period_end_date (repPdDate from N-PORT XML) — the quarter-end
date the holdings are as of — not the SEC filing date.
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import sqlalchemy
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

PGHOST     = os.getenv("PGHOST", "localhost")
PGPORT     = int(os.getenv("PGPORT", "5432"))
PGDATABASE = os.getenv("PGDATABASE", "finance_platform")
PGUSER     = os.getenv("PGUSER", "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

REPO_ROOT = Path(__file__).resolve().parent.parent

DATE_RANGE_CAPTION = (
    "Mar 2025 – Jun 2026, one snapshot per fiscal quarter  "
    "(SEC N-PORT filings, series S000004310 / IVV)"
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
RULE_COLS  = ["r1_count", "r2_count", "r3_count", "r4_count", "r5_count", "r6_count", "e1_count", "d1_count"]
ERROR_COLS = ["r1_count", "r2_count", "r3_count", "r4_count", "r5_count", "r6_count"]   # Open only


@st.cache_resource
def get_engine():
    return sqlalchemy.create_engine(
        f"postgresql+psycopg2://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}"
    )


@st.cache_data(ttl=300)
def load_aum_trend():
    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql(
            sqlalchemy.text(
                "SELECT period_end_date, total_holdings, total_value_usd_bn "
                "FROM reporting.v_fund_size ORDER BY period_end_date"
            ),
            conn,
        )
    df["period_end_date"] = pd.to_datetime(df["period_end_date"])
    return df


@st.cache_data(ttl=300)
def load_qc_trend():
    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql(
            sqlalchemy.text(
                "SELECT period_end_date, filing_date, total_holdings, "
                "       open_count, expected_count, "
                "       r1_count, r2_count, r3_count, r4_count, r5_count, r6_count, "
                "       d1_count, e1_count, n1_count, total_unique_exceptions "
                "FROM reporting.qc_trend ORDER BY period_end_date"
            ),
            conn,
        )
    df["period_end_date"] = pd.to_datetime(df["period_end_date"])
    return df


@st.cache_data(ttl=300)
def load_quarter_exceptions(period_end_date: str) -> pd.DataFrame:
    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql(
            sqlalchemy.text(
                "SELECT name, cusip, isin, assetcat, issuercat, iscashcollateral, "
                "       balance, valusd, pctval, rule_code, rule_name, status, reason "
                "FROM reporting.qc_exceptions "
                "WHERE period_end_date = :ped "
                "ORDER BY status, name"
            ),
            conn,
            params={"ped": period_end_date},
        )
    for col in ("balance", "valusd", "pctval"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ── display helpers ───────────────────────────────────────────────────────────

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
    """Pre-format numeric columns and replace empty identifiers with '—'."""
    df = df.copy()
    if "balance" in df.columns:
        df["balance"] = df["balance"].apply(
            lambda x: f"{x:,.2f}" if pd.notna(x) else "—"
        )
    if "valusd" in df.columns:
        df["valusd"] = df["valusd"].apply(
            lambda x: f"${x:,.2f}" if pd.notna(x) else "—"
        )
    if "pctval" in df.columns:
        df["pctval"] = df["pctval"].apply(
            lambda x: f"{x:.4f}%" if pd.notna(x) else "—"
        )
    for col in ("cusip", "isin", "iscashcollateral"):
        if col in df.columns:
            df[col] = (
                df[col]
                .fillna("")
                .astype(str)
                .str.strip()
                .replace({"": "—", "N/A": "—", "n/a": "—", "NA": "—"})
            )
    return df


# ── layout ────────────────────────────────────────────────────────────────────

st.title("Finance Reference Data Platform — Quality Dashboard")

try:
    engine = get_engine()
    with engine.connect() as conn:
        row_count = conn.execute(
            sqlalchemy.text("SELECT COUNT(*) FROM security_master_history")
        ).scalar()
    st.success(
        f"Connected to PostgreSQL ({PGDATABASE}) — "
        f"security_master_history has {row_count:,} rows."
    )
except Exception as e:
    st.error(f"Connection failed: {e}")
    st.stop()

st.divider()

# ── Chart 1: Fund size over time ──────────────────────────────────────────────

st.subheader("Fund size over time")

aum = load_aum_trend()

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
    xaxis_title="Holdings as of",
    yaxis_title="Total valUSD ($ billions)",
    yaxis=dict(rangemode="tozero", tickprefix="$", ticksuffix="B"),
    hovermode="x unified",
    margin=dict(t=20, b=20),
)
st.plotly_chart(fig_aum, use_container_width=True)
st.caption(DATE_RANGE_CAPTION)

st.divider()

# ── Chart 2a: Total exceptions over time ──────────────────────────────────────

st.subheader("Data quality exceptions over time")

qc = load_qc_trend()
x_dates = qc["period_end_date"]

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
    xaxis_title="Holdings as of",
    yaxis_title="Rows",
    yaxis=dict(range=[0, 5], dtick=1),
    hovermode="x unified",
    margin=dict(t=20, b=50),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
)
st.plotly_chart(fig_exc, use_container_width=True)
st.caption(
    "Red = open exceptions (R1–R6 violations + any N1 unreviewed rows, needs action).  "
    "Grey dotted = expected rows matched in data/reference/expected_exceptions.csv (D1 + E1).  "
    + DATE_RANGE_CAPTION
)

# ── Chart 2b: Rule-by-rule heatmap ───────────────────────────────────────────

st.subheader("Exception detail by rule and quarter (heatmap)")

x_labels = [d.strftime("%b %Y") for d in x_dates]
n_quarters = len(x_labels)
n_rules    = len(RULE_COLS)

# Build the raw count matrix: shape (n_rules, n_quarters)
counts = qc[RULE_COLS].values.T   # row = rule, col = quarter

# Encode color semantics (not the raw count):
#   0 → zero count                    → white   (#fafafa)
#   1 → non-zero, structural/stable   → muted grey-blue  (#b0bec5)
#   2 → non-zero, changed quarter-over-quarter → amber (#ff8f00)
#   3 → Expected row (E1/D1)          → steel grey (#78909c)  (always, any count)
# E1 is second-to-last row; D1 is last row — both always grey.
EXPECTED_ROW_INDICES = {n_rules - 2, n_rules - 1}

z_color = np.zeros((n_rules, n_quarters), dtype=float)
for ri in range(n_rules):
    row_counts = counts[ri]
    all_same   = len(set(row_counts)) == 1
    for qi in range(n_quarters):
        v = row_counts[qi]
        if ri in EXPECTED_ROW_INDICES:
            z_color[ri, qi] = 3                 # always grey (Expected label)
        elif v == 0:
            z_color[ri, qi] = 0                 # white
        elif all_same:
            z_color[ri, qi] = 1                 # structural/stable → muted
        elif qi > 0 and v != row_counts[qi - 1]:
            z_color[ri, qi] = 2                 # changed from previous → amber
        else:
            z_color[ri, qi] = 1                 # non-structural but unchanged → muted

# Cell text shows the actual count
text_ann = [[str(int(counts[ri, qi])) for qi in range(n_quarters)]
            for ri in range(n_rules)]

# Colorscale: positions 0.0 / 0.33 / 0.67 / 1.0 for values 0 / 1 / 2 / 3
colorscale = [
    [0.00, "#fafafa"],  # 0 → zero (white)
    [0.32, "#fafafa"],
    [0.33, "#b0bec5"],  # 1 → structural stable (muted blue-grey)
    [0.65, "#b0bec5"],
    [0.66, "#ff8f00"],  # 2 → changed quarter-over-quarter (amber)
    [0.98, "#ff8f00"],
    [0.99, "#78909c"],  # 3 → D1 informational (steel grey)
    [1.00, "#78909c"],
]

fig_heat = go.Figure(go.Heatmap(
    z=z_color.tolist(),
    x=x_labels,
    y=RULE_LABELS,
    colorscale=colorscale,
    zmin=0,
    zmax=3,
    showscale=False,
    hoverongaps=False,
    customdata=counts.tolist(),
    hovertemplate="<b>%{y}</b><br>%{x}<br>Count: %{customdata}<extra></extra>",
))

# Per-cell annotations: white text on dark-grey Expected cells, #444444 on all others.
# Using annotations instead of heatmap texttemplate/textfont avoids Plotly's
# browser-side auto-contrast override which causes text to alternate blue/orange.
for ri in range(n_rules):
    for qi in range(n_quarters):
        font_color = "#ffffff" if z_color[ri, qi] == 3 else "#444444"
        fig_heat.add_annotation(
            x=x_labels[qi],
            y=RULE_LABELS[ri],
            text=str(int(counts[ri, qi])),
            showarrow=False,
            font=dict(size=14, color=font_color),
        )
fig_heat.update_layout(
    xaxis_title="Holdings as of",
    yaxis=dict(autorange="reversed"),   # R1 at top, D1 at bottom
    margin=dict(t=20, b=20, l=240),
    height=400,
)
# Thin divider between R6 (index 5) and E1 (index 6): separates Open from Expected rows.
# y=5.5 is the midpoint between categories at positions 5 and 6 in data coordinates.
fig_heat.add_shape(
    type="line",
    xref="paper", yref="y",
    x0=-0.02, x1=1.02,
    y0=5.5, y1=5.5,
    line=dict(color="#999999", width=1.5, dash="dot"),
)
st.plotly_chart(fig_heat, use_container_width=True)
st.caption(
    "**Color key:** white = zero · grey-blue = same every quarter (structural) · "
    "amber = changed from previous quarter · "
    "steel grey = Expected label (E1 / D1, below divider, not counted as errors).  "
    + DATE_RANGE_CAPTION
)

st.divider()

# ── Drill-down: Quarter detail ────────────────────────────────────────────────

st.subheader("Quarter drill-down — flagged rows")

quarter_opts = {d.strftime("%b %Y"): d.strftime("%Y-%m-%d") for d in x_dates}
selected_q = st.selectbox(
    "Holdings as of",
    list(quarter_opts.keys()),
    index=len(quarter_opts) - 1,
    key="drill_down_quarter",
)
selected_ped = quarter_opts[selected_q]

exc_all    = load_quarter_exceptions(selected_ped)
error_df   = exc_all[exc_all["status"] == "Open"].reset_index(drop=True)
expected_df = exc_all[exc_all["status"] == "Expected"].reset_index(drop=True)

# Total holdings for the selected quarter from reporting.qc_trend
total_holdings = int(
    qc.loc[qc["period_end_date"].dt.strftime("%Y-%m-%d") == selected_ped, "total_holdings"]
    .iloc[0]
) if selected_ped in qc["period_end_date"].dt.strftime("%Y-%m-%d").values else len(exc_all)

# ── Number tiles ──────────────────────────────────────────────────────────────

t1, t2, t3 = st.columns(3)
t1.metric("Total holdings",              total_holdings)
t2.metric("Open exceptions (R1–R6 + N1)", len(error_df))
t3.metric("Expected (D1 + E1)",           len(expected_df))

# ── Exception table ───────────────────────────────────────────────────────────

if len(error_df) == 0:
    st.success("No open exceptions for this quarter.")
else:
    st.markdown(
        f"**{len(error_df)} open exception{'s' if len(error_df) != 1 else ''} — R1–R6 + N1**"
    )
    show_err = _fmt_display(error_df[DISPLAY_COLS]).rename(columns=DISPLAY_RENAME)
    st.dataframe(show_err, use_container_width=True, hide_index=True)

# ── Expected rows (D1 + E1) ───────────────────────────────────────────────────

if len(expected_df) > 0:
    with st.expander(
        f"Expected — reviewed (D1 derivatives + E1 cash collateral / STIV) "
        f"— {len(expected_df)} row{'s' if len(expected_df) != 1 else ''}",
        expanded=False,
    ):
        show_exp = _fmt_display(expected_df[DISPLAY_COLS]).rename(columns=DISPLAY_RENAME)
        st.dataframe(show_exp, use_container_width=True, hide_index=True)
