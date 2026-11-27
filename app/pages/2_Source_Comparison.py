"""
Source Comparison — Module 2
Reads only from app/data/*.parquet — no database connection.
"""

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="Source Comparison · Finance Reference Data Platform",
    page_icon="⚖️",
    layout="wide",
)

DATA = Path(__file__).resolve().parent.parent / "data"


@st.cache_data
def load(name: str) -> pd.DataFrame:
    return pd.read_parquet(DATA / f"{name}.parquet")


# ── Load ──────────────────────────────────────────────────────────────────────

recon = load("reconciliation")
ic    = load("internal_consistency")

# is_break may come back as string "True"/"False" from CSV → parquet round-trip
if recon["is_break"].dtype == object:
    recon["is_break"] = recon["is_break"].map({"True": True, "False": False}).fillna(False)

n_matched     = int((recon["match_status"] == "matched").sum())
n_nport_only  = int((recon["match_status"] == "nport_only").sum())
n_ish_only    = int((recon["match_status"] == "ishares_only").sum())

matched       = recon[recon["match_status"] == "matched"].copy()
n_weight_brk  = int(matched["is_break"].sum())
splits        = matched[matched["split_flag"].notna()][
    ["norm_name", "nport_pctval", "ishares_weight",
     "nport_balance", "ishares_quantity", "share_ratio", "split_flag"]
].copy()
n_splits      = len(splits)

ic_flags      = int(ic["flag"].sum()) if "flag" in ic.columns else 0

# ── Page header ───────────────────────────────────────────────────────────────

st.title("Source Comparison — Module 2")

# 84-day gap warning — prominent at the top
st.warning(
    "**Important limitation — 139-day gap between holdings dates.**  "
    "The N-PORT holds positions **as of 30 Jun 2026** (SEC filing date: 25 Aug 2026).  "
    "The iShares holdings file is **dated 16 Nov 2026**.  "
    "An index-tracking fund rebalances continuously, so portfolio changes over those "
    "139 days look identical to data errors.  "
    "An earlier figure of 84 days used the filing date instead of the holdings date.  "
    "The figures below show *differences*, not confirmed errors.",
    icon="⚠️",
)

st.markdown(
    "Module 2 downloads a second public source of IVV holdings (the iShares "
    "website file) and compares it row-by-row against the SEC N-PORT master.  "
    "Because the two sources share no common identifier, matching is done by "
    "normalised name only."
)

st.divider()

# ── Summary metrics ───────────────────────────────────────────────────────────

m1, m2, m3, m4, m5 = st.columns(5)
n_exact = int((recon["match_type"] == "exact").sum()) if "match_type" in recon.columns else n_matched
n_fuzzy = int((recon["match_type"] == "fuzzy").sum()) if "match_type" in recon.columns else 0
m1.metric("Matched pairs",           n_matched,
          help=f"{n_exact} exact name · {n_fuzzy} fuzzy (score ≥ 90)")
m2.metric("Unmatched per side",      n_nport_only,
          help="in N-PORT only  /  in iShares only (after fuzzy pass)")
m3.metric("Weight breaks",           n_weight_brk,
          help="matched pairs differing by > 0.15 pp")
m4.metric("Possible stock splits",   n_splits,
          help="share ratio ≈ 2× or 4×")
n_ic = len(ic)
m5.metric("Internal consistency",    f"{ic_flags}/{n_ic}",
          help="implied weight vs reported weight (same source)")

st.divider()

# ── Chart: weight breaks scatter ─────────────────────────────────────────────

st.subheader("Weight differences — matched pairs")

matched_sorted = matched.sort_values("weight_diff_abs", ascending=False).copy()

# colour: weight break = red, split candidate = amber, fine = green
def _colour(row):
    if row["is_break"] and row["split_flag"] is not None and pd.notna(row["split_flag"]):
        return "#ff8f00"
    if row["is_break"]:
        return "#d62728"
    return "#2ca02c"

matched_sorted["_colour"] = matched_sorted.apply(_colour, axis=1)

# Show only top-50 by absolute diff to keep the chart readable
top50 = matched_sorted.head(50).copy()

fig = go.Figure()
for colour, label in [
    ("#d62728", "Weight break (> 0.15 pp)"),
    ("#ff8f00", "Weight break + possible split"),
    ("#2ca02c", "Within threshold (≤ 0.15 pp)"),
]:
    subset = top50[top50["_colour"] == colour]
    if len(subset) == 0:
        continue
    fig.add_trace(go.Bar(
        x=subset["norm_name"],
        y=subset["weight_diff_abs"],
        name=label,
        marker_color=colour,
        hovertemplate=(
            "<b>%{x}</b><br>"
            "Abs weight diff: %{y:.4f} pp<extra></extra>"
        ),
    ))

fig.update_layout(
    barmode="overlay",
    xaxis_title="Holding (normalised name)",
    yaxis_title="Absolute weight difference (percentage points)",
    yaxis=dict(rangemode="tozero"),
    xaxis=dict(tickangle=-45, tickfont=dict(size=9)),
    hovermode="x unified",
    margin=dict(t=20, b=140),
    legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="center", x=0.5),
    height=480,
)
st.plotly_chart(fig, width="stretch")
st.caption(
    "Top 50 matched pairs by absolute weight difference.  "
    "Red = weight break (> 0.15 pp).  Amber = weight break AND a share ratio close "
    "to 2× or 4× (possible stock split).  Green = within threshold.  "
    "Threshold: 0.15 percentage-point difference in portfolio weight.  "
    "139-day gap means some differences may reflect rebalancing, not errors."
)

st.divider()

# ── Weight breaks table ───────────────────────────────────────────────────────

st.subheader(f"Weight breaks — {n_weight_brk} matched pairs above the 0.15 pp threshold")

breaks_show = matched[matched["is_break"]].sort_values(
    "weight_diff_abs", ascending=False
)[["norm_name", "nport_pctval", "ishares_weight", "weight_diff",
   "weight_diff_abs", "split_flag"]].copy()

breaks_show.columns = [
    "Name", "N-PORT weight (%)", "iShares weight (%)",
    "Difference (pp)", "Abs diff (pp)", "Split flag",
]
breaks_show["N-PORT weight (%)"]  = breaks_show["N-PORT weight (%)"].apply(lambda x: f"{x:.4f}")
breaks_show["iShares weight (%)"] = breaks_show["iShares weight (%)"].apply(lambda x: f"{x:.4f}")
breaks_show["Difference (pp)"]    = breaks_show["Difference (pp)"].apply(lambda x: f"{x:+.4f}")
breaks_show["Abs diff (pp)"]      = breaks_show["Abs diff (pp)"].apply(lambda x: f"{x:.4f}")
breaks_show["Split flag"]         = breaks_show["Split flag"].fillna("—")

st.dataframe(breaks_show, width="stretch", hide_index=True)

st.divider()

# ── Possible splits ───────────────────────────────────────────────────────────

st.subheader(f"Possible stock splits — {n_splits} holdings")

st.markdown(
    "Three holdings have a share ratio (N-PORT balance ÷ iShares quantity) close "
    "to 2× or 4×.  A stock split doubles or quadruples shares outstanding, which "
    "would explain the ratio.  **Not verified against corporate-action records.**"
)

splits_show = splits.copy()
splits_show["share_ratio"] = splits_show["share_ratio"].apply(lambda x: f"{x:.3f}×")
splits_show["nport_pctval"] = splits_show["nport_pctval"].apply(lambda x: f"{x:.4f}%")
splits_show["ishares_weight"] = splits_show["ishares_weight"].apply(lambda x: f"{x:.4f}%")
splits_show.columns = [
    "Name", "N-PORT weight", "iShares weight",
    "N-PORT balance", "iShares quantity", "Share ratio", "Split flag",
]
st.dataframe(splits_show, width="stretch", hide_index=True)

st.divider()

# ── Unmatched holdings ────────────────────────────────────────────────────────

st.subheader(f"Unmatched holdings — {n_nport_only} per side")

st.markdown(
    "These holdings appear in one source but not the other, after both exact and fuzzy "
    "name matching.  The two sources share no common identifier (CUSIP, ISIN), so "
    "matching relies entirely on normalised name.  Most remaining unmatched rows are "
    "name-formatting differences the fuzzy pass did not catch.  The 139-day gap also "
    "means some may reflect genuine portfolio changes."
)

col_u1, col_u2 = st.columns(2)

with col_u1:
    st.markdown("**In N-PORT only** (not found in the iShares file)")
    nport_only = recon[recon["match_status"] == "nport_only"][
        ["nport_name", "nport_pctval", "nport_balance"]
    ].sort_values("nport_pctval", ascending=False).copy()
    nport_only.columns = ["Name (N-PORT)", "Weight (%)", "Balance (shares)"]
    nport_only["Weight (%)"] = nport_only["Weight (%)"].apply(lambda x: f"{x:.4f}")
    nport_only["Balance (shares)"] = nport_only["Balance (shares)"].apply(
        lambda x: f"{x:,.0f}" if pd.notna(x) else "—")
    st.dataframe(nport_only, width="stretch", hide_index=True)

with col_u2:
    st.markdown("**In iShares only** (not found in N-PORT)")
    ish_only = recon[recon["match_status"] == "ishares_only"][
        ["ishares_name", "ishares_weight", "ishares_quantity"]
    ].sort_values("ishares_weight", ascending=False).copy()
    ish_only.columns = ["Name (iShares)", "Weight (%)", "Quantity (shares)"]
    ish_only["Weight (%)"] = ish_only["Weight (%)"].apply(lambda x: f"{x:.4f}")
    ish_only["Quantity (shares)"] = ish_only["Quantity (shares)"].apply(
        lambda x: f"{x:,.0f}" if pd.notna(x) else "—")
    st.dataframe(ish_only, width="stretch", hide_index=True)

st.divider()

# ── Fuzzy name matches ────────────────────────────────────────────────────────

if "match_type" in recon.columns:
    fuzzy = recon[recon["match_type"] == "fuzzy"].sort_values("fuzzy_score").copy()
    n_fuzzy_total = len(fuzzy)

    st.subheader(f"Fuzzy name matches — {n_fuzzy_total} pairs recovered")
    st.markdown(
        "These pairs were not found by exact name normalisation but scored ≥ 90 on "
        "token similarity (rapidfuzz token_set_ratio).  Sorted by score ascending — "
        "the most uncertain pairs first.  "
        "A token_set_ratio score of 100 means one name's words are all contained in "
        "the other, not that the names are identical, so every fuzzy pair was reviewed "
        "by hand and recorded in fuzzy_matches_reviewed.csv."
    )

    if n_fuzzy_total > 0:
        fuzzy_show = fuzzy[["fuzzy_score", "nport_name", "ishares_name",
                             "nport_pctval", "ishares_weight",
                             "weight_diff_abs", "is_break"]].copy()
        fuzzy_show["fuzzy_score"]     = fuzzy_show["fuzzy_score"].apply(lambda x: f"{x:.0f}")
        fuzzy_show["nport_pctval"]    = fuzzy_show["nport_pctval"].apply(lambda x: f"{x:.4f}%")
        fuzzy_show["ishares_weight"]  = fuzzy_show["ishares_weight"].apply(lambda x: f"{x:.4f}%")
        fuzzy_show["weight_diff_abs"] = fuzzy_show["weight_diff_abs"].apply(lambda x: f"{x:.4f}")
        fuzzy_show["is_break"]        = fuzzy_show["is_break"].apply(lambda x: "Yes" if x else "—")
        fuzzy_show.columns = ["Score", "N-PORT name", "iShares name",
                               "N-PORT wt (%)", "iShares wt (%)", "Abs diff (pp)", "Break?"]
        st.dataframe(fuzzy_show, width="stretch", hide_index=True)
        st.caption(
            "Score 100 = perfect token match; lower scores indicate more formatting "
            f"differences.  These {n_fuzzy_total} pairs are listed in "
            "data/reference/fuzzy_matches_reviewed.csv for human review.  "
            "Pairs with decision = 'same' have been confirmed by a reviewer."
        )

st.divider()

# ── Internal consistency ──────────────────────────────────────────────────────

st.subheader("Internal consistency — same-source check")

st.markdown(
    "Before comparing two sources, we check that the N-PORT file is internally "
    "consistent: does each holding's reported `pctVal` agree with what you get by "
    "dividing its `valUSD` by the total fund value?  "
    f"**Result: {ic_flags} of {n_ic} holdings fail this check.**"
)

col_ic1, col_ic2 = st.columns(2)
col_ic1.metric("Holdings checked",    n_ic)
col_ic2.metric("Inconsistencies found", ic_flags,
               help="0 = every reported weight agrees with the implied weight")

st.caption(
    "Tolerance: 0.001 percentage-point difference between reported and implied weight.  "
    "0 failures means the SEC filing's arithmetic is internally consistent — "
    "no holdings have a reported weight that disagrees with their reported value."
)

st.divider()

# ── Glossary ──────────────────────────────────────────────────────────────────

with st.expander("Glossary"):
    st.markdown(
        """
| Term | Meaning |
|---|---|
| N-PORT | The SEC monthly filing where the fund reports its holdings |
| iShares file | A separate daily holdings file published by BlackRock on the iShares website |
| Match status | Whether a holding was found in both sources (matched), only in N-PORT, or only in iShares |
| Weight break | A matched pair where the two sources report portfolio weights that differ by more than 0.15 percentage points |
| Percentage point (pp) | A direct difference in percentages: 6.00% − 5.85% = 0.15 pp |
| Share ratio | N-PORT balance ÷ iShares quantity; a ratio near 2 or 4 may indicate a stock split |
| Stock split | A corporate action that multiplies the number of shares outstanding (e.g. 2-for-1) while halving the price |
| Internal consistency | Whether a holding's reported weight matches what you calculate from its reported value and the fund total |
| Implied weight | valUSD ÷ total fund valUSD × 100 — the weight you would derive from the values in the filing |
| pctVal | The percentage of the fund's total value held in a single security |
| valUSD | US-dollar market value of a holding |
| balance / quantity | Number of shares or units held |
        """
    )
