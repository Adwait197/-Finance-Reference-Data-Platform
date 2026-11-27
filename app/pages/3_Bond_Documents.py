"""
Bond Documents — Module 5
Reads only from app/data/*.parquet — no database connection.
"""

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

st.set_page_config(
    page_title="Bond Documents · Finance Reference Data Platform",
    page_icon="📄",
    layout="wide",
)

DATA = Path(__file__).resolve().parent.parent / "data"


@st.cache_data
def load(name: str) -> pd.DataFrame:
    return pd.read_parquet(DATA / f"{name}.parquet")


# ── Load ──────────────────────────────────────────────────────────────────────

bt_all  = load("bond_terms")
bc_all  = load("bond_checks")
m5sc    = load("module5_scorecard")

# ── Page header ───────────────────────────────────────────────────────────────

st.title("Bond Documents — Module 5")
st.markdown(
    "Module 5 parses real SEC 424B2 and FWP filings to extract structured bond "
    "terms: issuer, coupon, maturity, CUSIP, principal amount, yield, ratings, "
    "day count, settlement date.  Build-set companies (Apple, Alphabet, Eli Lilly, "
    "Johnson & Johnson, Microsoft) were used to develop and validate the parser.  "
    "Meta and Broadcom are the unseen test set — the parser was run on them blind."
)

st.divider()

# ── Company filter ────────────────────────────────────────────────────────────

companies = ["All companies"] + sorted(bt_all["company"].unique())
selected_company = st.selectbox("Filter by company", companies)

bt = bt_all.copy() if selected_company == "All companies" else bt_all[bt_all["company"] == selected_company].copy()
bc = bc_all.copy() if selected_company == "All companies" else bc_all[bc_all["company"] == selected_company].copy()

latest_deal = pd.to_datetime(bt["deal_date"]).max().strftime("%d %b %Y")

# ── KPIs ──────────────────────────────────────────────────────────────────────

n_bonds      = len(bt)
n_companies  = bt["company"].nunique()
n_currencies = bt["currency"].nunique()
n_checks     = len(bc)
n_pass       = int((bc["result"] == "pass").sum())
n_issues     = n_checks - n_pass

k1, k2, k3, k4 = st.columns(4)
k1.metric("Bonds in view",        n_bonds,
          help="All bonds whose terms were extracted from SEC filings")
k2.metric("Companies",            n_companies,
          help="5 build set · 2 test set (Meta and Broadcom)")
k3.metric("Currencies",           n_currencies,
          help="USD · EUR · CAD · GBP")
k4.metric("Checks passed",        f"{n_pass}/{n_checks}",
          help=f"{n_issues} not applicable, reviewed, or not found; 0 fail")

st.caption(f"Bond data from SEC filings dated up to {latest_deal}.")
st.divider()

# ── Chart 1: Bond count by company and currency ───────────────────────────────

st.subheader("Bond count by company and currency")

count_df = bt.groupby(["company", "currency"]).size().reset_index(name="count")
currency_colours = {"USD": "#1f77b4", "EUR": "#2ca02c", "CAD": "#ff7f0e", "GBP": "#9467bd"}
currencies_present = sorted(count_df["currency"].unique())
company_order = count_df.groupby("company")["count"].sum().sort_values(ascending=False).index.tolist()

fig_count = go.Figure()
for ccy in currencies_present:
    sub = count_df[count_df["currency"] == ccy]
    sub = sub.set_index("company").reindex(company_order).reset_index()
    fig_count.add_trace(go.Bar(
        name=ccy,
        x=sub["company"],
        y=sub["count"],
        marker_color=currency_colours.get(ccy, "#aec7e8"),
        hovertemplate="<b>%{x}</b><br>%{y} bonds in " + ccy + "<extra></extra>",
    ))

fig_count.update_layout(
    barmode="group",
    xaxis_title="Company",
    yaxis_title="Number of bonds",
    yaxis=dict(rangemode="tozero"),
    margin=dict(t=20, b=100),
    legend=dict(orientation="h", yanchor="top", y=-0.18, xanchor="center", x=0.5),
    height=360,
)
st.plotly_chart(fig_count, use_container_width=True)
st.caption(
    "Build set (Alphabet, Apple, Eli Lilly, Johnson & Johnson, Microsoft) in blue/green/orange/purple by currency.  "
    "Test set (Broadcom, Meta) are USD-only.  "
    "Chart counts distinct bond lines; a single deal may issue multiple tranches."
)

st.divider()

# ── Chart 2: Principal amount per company per currency ────────────────────────

st.subheader("Amount issued per company and currency")

amt_df = bt.groupby(["company", "currency"])["principal_amount"].sum().reset_index()
amt_df["amount_bn"] = amt_df["principal_amount"] / 1e9

# One subplot per currency — amounts are NOT comparable across currencies
currencies_present_amt = sorted(amt_df["currency"].unique())
n_ccy = len(currencies_present_amt)
ncols = min(n_ccy, 2)
nrows = (n_ccy + ncols - 1) // ncols

fig_amt = make_subplots(
    rows=nrows, cols=ncols,
    subplot_titles=[f"{c} (billions)" for c in currencies_present_amt],
    horizontal_spacing=0.12,
    vertical_spacing=0.18,
)

for idx, ccy in enumerate(currencies_present_amt):
    row = idx // ncols + 1
    col = idx % ncols + 1
    sub = amt_df[amt_df["currency"] == ccy].sort_values("amount_bn", ascending=False)
    fig_amt.add_trace(
        go.Bar(
            x=sub["company"],
            y=sub["amount_bn"],
            marker_color=currency_colours.get(ccy, "#aec7e8"),
            name=ccy,
            showlegend=False,
            hovertemplate="<b>%{x}</b><br>%{y:.1f}B " + ccy + "<extra></extra>",
        ),
        row=row, col=col,
    )
    fig_amt.update_yaxes(rangemode="tozero", row=row, col=col)

fig_amt.update_layout(
    margin=dict(t=60, b=20),
    height=240 * nrows,
)
st.plotly_chart(fig_amt, use_container_width=True)
st.warning(
    "Each panel shows amounts in a single currency.  "
    "USD billions and EUR billions represent different values.  "
    "No cross-currency totals are shown — do not add amounts across panels.",
    icon="⚠️",
)

st.divider()

# ── V1–V10 check results ──────────────────────────────────────────────────────

st.subheader("V1–V10 check results")

result_order  = ["pass", "not_applicable", "expected_reviewed", "not_found", "fail"]
result_labels = {
    "pass":              "Pass",
    "not_applicable":    "N/A",
    "expected_reviewed": "Reviewed",
    "not_found":         "Not found",
    "fail":              "Fail",
}
result_colours = {
    "pass":              "#2ca02c",
    "not_applicable":    "#aec7e8",
    "expected_reviewed": "#ffbb78",
    "not_found":         "#ff7f0e",
    "fail":              "#d62728",
}

# Plain-English overrides for check names where the stored label is too technical
CHECK_NAME_OVERRIDE = {
    "V6": "Dates in the right order, and maturity year matches the bond name",
}

checks_meta = (
    bc.groupby(["check", "check_name"])
    .first()
    .reset_index()[["check", "check_name"]]
    .sort_values("check")
)
checks_meta["check_name"] = checks_meta.apply(
    lambda r: CHECK_NAME_OVERRIDE.get(r["check"], r["check_name"]), axis=1
)
pivot = bc.groupby(["check", "result"]).size().unstack(fill_value=0)

checks_present = sorted(bc["check"].unique(), key=lambda c: int(c[1:]))
fig_heat = go.Figure()

for res in result_order:
    if res not in pivot.columns:
        continue
    vals = [pivot.loc[c, res] if c in pivot.index else 0 for c in checks_present]
    fig_heat.add_trace(go.Bar(
        name=result_labels[res],
        x=checks_present,
        y=vals,
        marker_color=result_colours[res],
        hovertemplate="<b>%{x}</b> — " + result_labels[res] + ": %{y}<extra></extra>",
    ))

fig_heat.update_layout(
    barmode="stack",
    xaxis_title="Check",
    yaxis_title="Bonds",
    yaxis=dict(rangemode="tozero"),
    margin=dict(t=20, b=100),
    legend=dict(orientation="h", yanchor="top", y=-0.18, xanchor="center", x=0.5),
    height=360,
)
st.plotly_chart(fig_heat, use_container_width=True)
st.caption(
    "Green = pass.  Light blue = not applicable for this bond type (e.g. V4/V5 for floating-rate bonds).  "
    "Yellow = expected deviation reviewed and accepted.  Orange = field not found in document.  "
    "Red = fail (none in this dataset).  "
    "Stacked bars sum to the total bonds in view."
)

# Check summary table
check_rows = []
for check in checks_present:
    check_bc = bc[bc["check"] == check]
    raw_name = check_bc.iloc[0]["check_name"] if len(check_bc) > 0 else ""
    name = CHECK_NAME_OVERRIDE.get(check, raw_name)
    counts = {res: int((check_bc["result"] == res).sum()) for res in result_order}
    check_rows.append({"Check": check, "Name": name, **{result_labels[k]: v for k, v in counts.items() if v > 0}})

check_tbl = pd.DataFrame(check_rows).fillna(0)
for col in result_labels.values():
    if col in check_tbl.columns:
        check_tbl[col] = check_tbl[col].astype(int)

st.dataframe(check_tbl, use_container_width=True, hide_index=True)

st.divider()

# ── Scorecard: run 1 vs run 2 ─────────────────────────────────────────────────

st.subheader("Test-set scorecard — run 1 vs run 2")

st.markdown(
    "Module 5 was first run on the 17 Broadcom bonds from the unseen test set "
    "(run 1 = test set, Broadcom only; Meta bonds were not yet extracted).  "
    "Run 2 added the remaining test-set company: Meta (16 bonds), giving a full test set "
    "of 36 bonds (Meta 16 + Broadcom 20).  "
    "Run 1 did not yet define V9 or V10, so all bonds failed 'pass all checks'.  "
    "Run 2 defines all checks V1–V10."
)

score_display = {
    "bonds_found":          "Bonds extracted",
    "meta_bonds":           "  Meta bonds extracted",
    "broadcom_bonds":       "  Broadcom bonds extracted",
    "pct_fields_filled":    "Fields filled (% of applicable)",
    "bonds_pass_all_checks":"Bonds passing all checks",
    "figi_coupon_pass":     "Coupon matches OpenFIGI",
    "figi_maturity_pass":   "Maturity matches OpenFIGI",
}

rows_sc = []
for key, label in score_display.items():
    row_m = m5sc[m5sc["measure"] == key]
    if row_m.empty:
        continue
    r = row_m.iloc[0]
    unit = r["unit"]
    v1 = r["run1_value"]
    v2 = r["run2_value"]
    rows_sc.append({
        "Measure":  label,
        "Run 1":    f"{v1:.0f}{' %' if unit=='pct' else ''}",
        "Run 2":    f"{v2:.0f}{' %' if unit=='pct' else ''}",
        "Denominator": r["description"],
    })

sc_df = pd.DataFrame(rows_sc)
st.dataframe(sc_df, use_container_width=True, hide_index=True)
st.caption(
    "Run 1 = Broadcom only (17 bonds); checks V9/V10 not yet defined — 'bonds pass all checks' was 0.  "
    "Run 2 = full unseen test set (36 bonds, Meta + Broadcom); all checks V1–V10 defined.  "
    "OpenFIGI maturity match improved from 12/17 to 36/36 after fixing the date-pattern parser."
)

st.divider()

# ── Bond list (searchable) ────────────────────────────────────────────────────

st.subheader("Bond list")

search = st.text_input("Search by company, bond name or CUSIP", "")

display_bt = bt.copy()
if search.strip():
    mask = (
        display_bt["company"].str.contains(search, case=False, na=False)
        | display_bt["bond_name"].str.contains(search, case=False, na=False)
        | display_bt["cusip"].str.contains(search, case=False, na=False)
    )
    display_bt = display_bt[mask]

display_bt["deal_date"] = pd.to_datetime(display_bt["deal_date"]).dt.strftime("%Y-%m-%d")
display_bt["maturity_date_fmt"] = pd.to_datetime(display_bt["maturity_date"], errors="coerce").dt.strftime("%Y-%m-%d")

cols_show = ["company", "set", "bond_name", "currency", "principal_amount",
             "coupon_pct", "rate_type", "maturity_date_fmt", "cusip",
             "source_doc_url", "prospectus_url"]
bond_list = display_bt[cols_show].copy()
bond_list.columns = [
    "Company", "Set", "Bond name", "Currency", "Principal",
    "Coupon %", "Type", "Maturity", "CUSIP",
    "FWP (source)", "424B2 (prospectus)",
]
bond_list["Principal"] = bond_list["Principal"].apply(
    lambda x: f"{x/1e9:.3f}B" if pd.notna(x) else "—"
)
bond_list["Coupon %"] = bond_list["Coupon %"].apply(
    lambda x: f"{x:.3f}" if pd.notna(x) else "—"
)

st.dataframe(
    bond_list,
    use_container_width=True,
    hide_index=True,
    column_config={
        "FWP (source)":       st.column_config.LinkColumn("FWP (source)",       display_text="SEC link"),
        "424B2 (prospectus)": st.column_config.LinkColumn("424B2 (prospectus)", display_text="SEC link"),
    },
    height=350,
)
st.caption(
    f"Showing {len(bond_list)} of {n_bonds} bonds.  "
    "Principal shown in billions of native currency.  "
    "FWP = Free Writing Prospectus (term sheet); 424B2 = final prospectus supplement.  "
    "Links open the SEC EDGAR filing directly."
)

st.divider()

# ── Glossary ──────────────────────────────────────────────────────────────────

with st.expander("Glossary"):
    st.markdown(
        """
| Term | Meaning |
|---|---|
| FWP (Free Writing Prospectus) | A term sheet filed with the SEC before the final prospectus; contains pricing and key terms |
| 424B2 | The final prospectus supplement for shelf-registered bonds |
| Build set | Companies whose documents were used to develop and validate the parser |
| Test set | Unseen companies; the parser was run on their documents to test generalisation |
| V1–V10 | The 10 automated validation checks applied to every extracted bond |
| CUSIP | 9-character US security identifier (first 6 = issuer, next 2 = issue, last = check digit) |
| ISIN | 12-character international security identifier |
| Day count | Convention for counting days between coupon dates (e.g. 30/360, Actual/360, Actual/365) |
| Benchmark yield | Yield on a comparable on-the-run US Treasury at the time of pricing |
| Spread to Treasury | The extra yield above the benchmark Treasury (in basis points) |
| OpenFIGI | A free public API that maps CUSIPs to financial instrument identifiers and terms |
        """
    )
