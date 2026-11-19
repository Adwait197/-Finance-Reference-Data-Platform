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

REPO_ROOT    = Path(__file__).resolve().parent.parent
QC_TREND_CSV = REPO_ROOT / "data" / "processed" / "qc_trend_by_quarter.csv"

DATE_RANGE_CAPTION = (
    "Mar 2025 – Jun 2026, one snapshot per fiscal quarter  "
    "(SEC N-PORT filings, series S000004310 / IVV)"
)

# repPdDate extracted from each XML file; maps filing_date -> period_end_date
PERIOD_END_MAP = {
    "2025-05-27": "2025-03-31",
    "2025-08-28": "2025-06-30",
    "2025-11-26": "2025-09-30",
    "2026-02-25": "2025-12-31",
    "2026-05-28": "2026-03-31",
    "2026-08-25": "2026-06-30",
}

RULE_LABELS = [
    "R1 · No identifier",
    "R2 · Weight out of range",
    "R3 · Negative value",
    "R4 · Duplicate CUSIP",
    "R5 · Missing category",
    "R6 · Possible cash instrument",
    "D1 · Derivative (review only)",
]
RULE_COLS = ["r1_count", "r2_count", "r3_count", "r4_count", "r5_count", "r6_count", "d1_count"]
ERROR_COLS = ["r1_count", "r2_count", "r3_count", "r4_count", "r5_count", "r6_count"]   # D1 excluded


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
                "SELECT period_end_date, SUM(valusd) AS total_valusd "
                "FROM security_master_history "
                "GROUP BY period_end_date ORDER BY period_end_date"
            ),
            conn,
        )
    df["period_end_date"] = pd.to_datetime(df["period_end_date"])
    df["total_valusd_bn"] = df["total_valusd"].astype(float) / 1e9
    return df


@st.cache_data(ttl=300)
def load_qc_trend():
    df = pd.read_csv(QC_TREND_CSV, parse_dates=["filing_date"])
    df["period_end_date"] = pd.to_datetime(
        df["filing_date"].dt.strftime("%Y-%m-%d").map(PERIOD_END_MAP)
    )
    df = df.sort_values("period_end_date").reset_index(drop=True)
    # Backfill d1_count if column missing from older CSV versions
    if "d1_count" not in df.columns:
        df["d1_count"] = 0
    return df


# ── drill-down helpers ────────────────────────────────────────────────────────

ERROR_RULE_PLAIN = {
    "R1_no_identifier":    "No identifier (R1)",
    "R2_pctval_range":     "Weight out of range (R2)",
    "R3_valusd_negative":  "Negative value (R3)",
    "R4_duplicate_cusip":  "Duplicate CUSIP (R4)",
    "R5_category_blank":   "Missing category (R5)",
    "R6_possible_cash_mm": "Possible cash instrument (R6)",
}

DISPLAY_COLS = [
    "name", "cusip", "isin", "assetcat", "issuercat", "iscashcollateral",
    "balance", "valusd", "pctval", "rules_tripped", "reason",
]
DISPLAY_RENAME = {
    "assetcat": "assetCat", "issuercat": "issuerCat",
    "iscashcollateral": "isCashCollateral",
    "valusd": "valUSD", "pctval": "pctVal",
    "rules_tripped": "Rules tripped", "reason": "Reason",
}
DISPLAY_COL_CFG = {
    "valUSD":  st.column_config.NumberColumn(format="$,.2f"),
    "pctVal":  st.column_config.NumberColumn(format="%.6f"),
    "balance": st.column_config.NumberColumn(format=",.2f"),
}


def _is_blank_col(s: pd.Series) -> pd.Series:
    return s.isna() | s.fillna("").astype(str).str.strip().isin(["", "N/A", "n/a", "NA"])


@st.cache_data(ttl=300)
def load_quarter_detail(period_end_date: str) -> pd.DataFrame:
    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql(
            sqlalchemy.text(
                "SELECT name, cusip, isin, assetcat, issuercat, iscashcollateral, "
                "       balance, valusd, pctval "
                "FROM security_master_history WHERE period_end_date = :ped "
                "ORDER BY ABS(valusd) DESC NULLS LAST"
            ),
            conn,
            params={"ped": period_end_date},
        )
    for col in ("balance", "valusd", "pctval"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def flag_exceptions(df: pd.DataFrame):
    """
    Apply QC rules; return (error_df, d1_df).
    Derivatives (assetCat starting with 'D') are excluded from R1/R2/R3
    and labelled D1.  D1 rows are NOT included in error_df.
    """
    is_deriv = df["assetcat"].fillna("").str.upper().str.startswith("D")

    non_blank = df["cusip"][~_is_blank_col(df["cusip"])]
    dup_cusips = non_blank[non_blank.duplicated(keep=False)].unique()

    both_pos = (
        df["balance"].notna() & df["valusd"].notna()
        & (df["valusd"] > 0) & (df["balance"] > 0)
    )
    safe_denom = df["valusd"].replace(0, np.nan)
    ratio = df["balance"] / safe_denom

    masks = {
        "R1_no_identifier":    _is_blank_col(df["cusip"]) & _is_blank_col(df["isin"]) & ~is_deriv,
        "R2_pctval_range":     df["pctval"].notna() & ((df["pctval"] < 0) | (df["pctval"] > 100)) & ~is_deriv,
        "R3_valusd_negative":  df["valusd"].notna() & (df["valusd"] < 0) & ~is_deriv,
        "R4_duplicate_cusip":  df["cusip"].isin(dup_cusips),
        "R5_category_blank":   _is_blank_col(df["assetcat"]) | _is_blank_col(df["issuercat"]),
        "R6_possible_cash_mm": both_pos & (ratio >= 0.99) & (ratio <= 1.01),
    }

    def _rules_tripped(idx):
        return ", ".join(ERROR_RULE_PLAIN[r] for r, m in masks.items() if m.iloc[idx])

    df = df.copy()
    df["rules_tripped"] = [_rules_tripped(i) for i in range(len(df))]
    df["reason"] = ""

    any_error = pd.concat(list(masks.values()), axis=1).any(axis=1)
    error_df = df[any_error & ~is_deriv].reset_index(drop=True)

    d1_df = df[is_deriv].copy()
    d1_df["rules_tripped"] = "Derivative — review only"
    d1_df["reason"] = (
        "Exchange-traded derivative (futures contract): "
        "no CUSIP/ISIN by design; valUSD = unrealised P&L (can be negative)"
    )
    d1_df = d1_df.reset_index(drop=True)

    return error_df, d1_df


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
    y=aum["total_valusd_bn"],
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

fig_exc = go.Figure()
fig_exc.add_trace(go.Scatter(
    x=x_dates,
    y=qc["total_unique_exceptions"],
    mode="lines+markers",
    line=dict(width=2.5, color="#d62728"),
    marker=dict(size=9),
    hovertemplate="<b>%{x|%b %Y}</b><br>Total exceptions: %{y}<extra></extra>",
))
fig_exc.update_layout(
    xaxis_title="Holdings as of",
    yaxis_title="Unique rows flagged",
    yaxis=dict(rangemode="tozero", dtick=1),
    hovermode="x unified",
    margin=dict(t=20, b=20),
)
st.plotly_chart(fig_exc, use_container_width=True)
st.caption(
    "Each flagged row is counted once, even if it breaks more than one rule.  "
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
#   0 → zero count            → white   (#fafafa)
#   1 → non-zero, structural  → muted grey-blue  (#b0bec5)
#   2 → non-zero, changed from previous quarter → amber (#ff8f00)
#   3 → D1 informational row  → steel grey (#78909c)  (always, any count)
D1_ROW_IDX = n_rules - 1   # last row is always D1

z_color = np.zeros((n_rules, n_quarters), dtype=float)
for ri in range(n_rules):
    row_counts = counts[ri]
    all_same   = len(set(row_counts)) == 1
    for qi in range(n_quarters):
        v = row_counts[qi]
        if ri == D1_ROW_IDX:
            z_color[ri, qi] = 3                 # always D1 grey
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
    text=text_ann,
    texttemplate="%{text}",
    textfont=dict(size=14),
    colorscale=colorscale,
    zmin=0,
    zmax=3,
    showscale=False,
    hoverongaps=False,
    customdata=counts.tolist(),
    hovertemplate="<b>%{y}</b><br>%{x}<br>Count: %{customdata}<extra></extra>",
))
fig_heat.update_layout(
    xaxis_title="Holdings as of",
    yaxis=dict(autorange="reversed"),   # R1 at top
    margin=dict(t=20, b=20, l=220),
    height=370,
)
st.plotly_chart(fig_heat, use_container_width=True)
st.caption(
    "**Color key:** white = zero · grey-blue = same every quarter (structural) · "
    "amber = changed from previous quarter · steel grey = derivative (D1, not an error).  "
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

detail_df           = load_quarter_detail(selected_ped)
error_df, d1_df     = flag_exceptions(detail_df)

# ── Number tiles ──────────────────────────────────────────────────────────────

t1, t2, t3 = st.columns(3)
t1.metric("Total holdings",            len(detail_df))
t2.metric("Exceptions (R1–R6)",        len(error_df))
t3.metric("Derivatives under review",  len(d1_df))

# ── Exception table (R1–R6) ───────────────────────────────────────────────────

if len(error_df) == 0:
    st.success("No R1–R6 exceptions for this quarter.")
else:
    st.markdown(
        f"**{len(error_df)} exception{'s' if len(error_df) != 1 else ''} — R1–R6 rules**"
    )
    show_err = (
        error_df[DISPLAY_COLS]
        .rename(columns=DISPLAY_RENAME)
    )
    st.dataframe(
        show_err,
        use_container_width=True,
        hide_index=True,
        column_config=DISPLAY_COL_CFG,
    )

# ── D1 derivative table ───────────────────────────────────────────────────────

if len(d1_df) > 0:
    with st.expander(
        f"Derivatives — expected, review only ({len(d1_df)} row{'s' if len(d1_df) != 1 else ''})",
        expanded=False,
    ):
        show_d1 = (
            d1_df[DISPLAY_COLS]
            .rename(columns=DISPLAY_RENAME)
        )
        st.dataframe(
            show_d1,
            use_container_width=True,
            hide_index=True,
            column_config=DISPLAY_COL_CFG,
        )
