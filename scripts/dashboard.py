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
    # Backfill missing columns from older CSV versions
    for col in ("d1_count", "e1_count", "n1_count"):
        if col not in df.columns:
            df[col] = 0
    return df


# ── drill-down helpers ────────────────────────────────────────────────────────

ERROR_RULE_PLAIN = {
    "R1_no_identifier":    "No identifier (R1)",
    "R2_pctval_range":     "Weight out of range (R2)",
    "R3_valusd_negative":  "Negative value (R3)",
    "R4_duplicate_cusip":  "Duplicate CUSIP (R4)",
    "R5_category_blank":   "Missing category (R5)",
    "R6_possible_cash_mm": "Possible cash instrument (R6)",
    "N1_new_needs_review": "New unreviewed STIV/derivative (N1)",
}

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
DISPLAY_COL_CFG: dict = {}   # all formatting done in Python before display


def _is_blank_col(s: pd.Series) -> pd.Series:
    return s.isna() | s.fillna("").astype(str).str.strip().isin(["", "N/A", "n/a", "NA"])


@st.cache_data
def _load_reviewed_lookup() -> dict:
    csv_path = REPO_ROOT / "data" / "reference" / "expected_exceptions.csv"
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path, keep_default_na=False)
    for col in ("cusip", "isCashCollateral", "assetcat"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].str.strip()
    lookup = {}
    for _, row in df.iterrows():
        cusip    = row.get("cusip", "")
        iscash   = row.get("isCashCollateral", "")
        assetcat = row.get("assetcat", "")
        if cusip:
            key = ("cusip", cusip, iscash)
        elif assetcat:
            key = ("assetcat", assetcat)
        else:
            continue
        lookup[key] = {"label": row["label"], "reason": row["reason"]}
    return lookup


def _find_reviewed(lookup: dict, cusip_val, iscash_val, assetcat_val):
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
    Apply QC rules; return (error_df, expected_df).

    error_df    — Open rows: R1-R6 violations + N1 (unreviewed STIV/derivative).
    expected_df — Expected rows from the reviewed-exceptions list (D1 + E1).
    Both DataFrames carry 'status' and 'reason' columns.
    """
    reviewed_lookup = _load_reviewed_lookup()

    is_deriv    = df["assetcat"].fillna("").str.upper().str.startswith("D")
    is_stiv     = df["assetcat"].fillna("") == "STIV"
    is_candidate = is_deriv | is_stiv

    # Per-row reviewed check
    review_matches = [
        _find_reviewed(reviewed_lookup, row.get("cusip"), row.get("iscashcollateral"), row.get("assetcat"))
        for _, row in df.iterrows()
    ]
    is_reviewed = pd.Series([m is not None for m in review_matches], index=df.index)
    is_expected = is_candidate & is_reviewed
    is_n1       = is_candidate & ~is_reviewed

    # R4: same isCashCollateral bucket only
    bucket   = df["iscashcollateral"].fillna("unknown")
    comp_key = df["cusip"].fillna("") + "||" + bucket
    nonblank_key = comp_key[~_is_blank_col(df["cusip"])]
    dup_keys = nonblank_key[nonblank_key.duplicated(keep=False)].unique()

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
        "R4_duplicate_cusip":  comp_key.isin(dup_keys) & ~_is_blank_col(df["cusip"]),
        "R5_category_blank":   _is_blank_col(df["assetcat"]) | _is_blank_col(df["issuercat"]),
        "R6_possible_cash_mm": both_pos & (ratio >= 0.99) & (ratio <= 1.01) & ~is_stiv,
        "N1_new_needs_review": is_n1,
    }

    def _rules_tripped(idx):
        return ", ".join(
            ERROR_RULE_PLAIN.get(r, r) for r, m in masks.items() if m.iloc[idx]
        )

    df = df.copy()
    df["rules_tripped"] = [_rules_tripped(i) for i in range(len(df))]
    df["status"] = ""
    df["reason"] = ""

    any_open = pd.concat(list(masks.values()), axis=1).any(axis=1)

    df.loc[any_open & ~is_expected, "status"] = "Open"
    df.loc[is_expected, "status"] = "Expected"

    # Reasons from reviewed list for Expected rows; N1 gets a warning reason
    for i, row in enumerate(review_matches):
        if is_expected.iloc[i] and row:
            df.iloc[i, df.columns.get_loc("reason")] = row["reason"]
    df.loc[is_n1, "reason"] = (
        "New unreviewed STIV or derivative — add to "
        "data/reference/expected_exceptions.csv after review"
    )

    error_df    = df[df["status"] == "Open"].reset_index(drop=True)
    expected_df = df[is_expected].reset_index(drop=True)

    return error_df, expected_df


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
    text=text_ann,
    texttemplate="%{text}",
    textfont=dict(size=14, color="#444444"),   # uniform text color across all backgrounds
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

detail_df                = load_quarter_detail(selected_ped)
error_df, expected_df   = flag_exceptions(detail_df)

# ── Number tiles ──────────────────────────────────────────────────────────────

t1, t2, t3 = st.columns(3)
t1.metric("Total holdings",        len(detail_df))
t2.metric("Open exceptions (R1–R6)", len(error_df))
t3.metric("Expected (D1 + E1)",    len(expected_df))

# ── Exception table (R1–R6) ───────────────────────────────────────────────────

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
