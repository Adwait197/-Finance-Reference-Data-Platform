"""
Finance Reference Data Platform — About this project
Home page (entry point for `streamlit run app/Home.py`).
Reads only from app/data/*.parquet — no database connection.
"""

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="Finance Reference Data Platform",
    page_icon="📊",
    layout="wide",
)

DATA = Path(__file__).resolve().parent / "data"


@st.cache_data
def load(name: str) -> pd.DataFrame:
    return pd.read_parquet(DATA / f"{name}.parquet")


# ── Header ────────────────────────────────────────────────────────────────────

st.title("Finance Reference Data Platform")
st.markdown(
    "A personal project that builds and tests the data-quality processes used "
    "by financial firms to manage security reference data — using public data from "
    "SEC EDGAR (N-PORT filings and 424B2/FWP prospectuses), the iShares public "
    "holdings file, and the OpenFIGI API."
)

st.markdown(
    "**Live dashboard:** https://finance-data-quality-reference-platform.streamlit.app/  \n"
    "**Source code:** https://github.com/Adwait197/-Finance-Reference-Data-Platform"
)

st.divider()

# ── What it does / Why it was built ──────────────────────────────────────────

col_what, col_why = st.columns(2)

with col_what:
    st.subheader("What it does")
    st.markdown(
        """
When financial firms buy and sell securities (stocks, bonds, ETFs), every
trade depends on accurate background information about those securities:
names, identifiers, prices, coupon rates, maturity dates.  Managing that
information — finding errors, comparing sources, flagging anomalies — is
called **reference data management**.

This platform does exactly that, end-to-end, using real public data:

- Pulls holdings data from public fund filings
- Compares two independent data sources and flags disagreements
- Applies automated data-quality rules and machine-learning anomaly detection
- Tracks issues over time in dashboards
- Extracts bond terms directly from legal documents
- Tests the reconciliation checker using payments calculated from the extracted bond terms
        """
    )

with col_why:
    st.subheader("Why it was built")
    st.markdown(
        """
Built as a personal learning project to:

- Deepen knowledge of **finance-domain data** (how securities are identified,
  priced, and tracked across systems)
- Practice **data engineering**: PostgreSQL, Python automation, SQL transforms
- Apply **machine learning** to a real governance problem (anomaly detection)
- Build **analytics dashboards** in Streamlit and Power BI
- Work the full pipeline — from raw filings to dashboards, with checks at every step

No private or paid data was used.  Inputs are public SEC filings (N-PORT,
424B2 and FWP prospectuses), the iShares public holdings file,
and the OpenFIGI API (free tier).
        """
    )

st.divider()

# ── Module diagram ────────────────────────────────────────────────────────────

st.subheader("How the six modules fit together")

modules = [
    ("1", "Get the data",          "Pull ETF holdings from\nSEC N-PORT filings\ninto a PostgreSQL master table"),
    ("2", "Compare two sources",   "Download a second public\nholdings file; flag mismatched,\nmissing, or stale records"),
    ("3", "Quality checks + ML",   "Run 6 Python rules over every\nrow; then use IsolationForest\nto catch anomalies the rules miss"),
    ("4", "Dashboards",            "Exception trend charts,\nbreak-rate heatmap, and\nquarter-by-quarter drill-down"),
    ("5", "Bond document parsing", "Extract issuer, coupon, maturity\nand CUSIP from real SEC\nprospectus filings"),
    ("6", "Cash reconciliation",   "Tests the reconciliation checker\nusing payments calculated\nfrom the extracted bond terms"),
]

cols = st.columns(6)
for col, (num, title, desc) in zip(cols, modules):
    with col:
        st.markdown(
            f"""
<div style="border:1px solid #ddd;border-radius:8px;padding:12px 10px;
            text-align:center;background:#f8f9fa;min-height:160px;">
  <div style="font-size:1.6rem;font-weight:700;color:#1f77b4;">{num}</div>
  <div style="font-weight:600;font-size:0.85rem;margin:4px 0 6px;color:#1a1a1a;">{title}</div>
  <div style="font-size:0.75rem;color:#333;line-height:1.4;">{desc}</div>
</div>
""",
            unsafe_allow_html=True,
        )

st.caption(
    "Modules 1–4 work on equity holdings data.  "
    "Module 5 switches to bond prospectus documents.  "
    "Module 6 uses the Module 5 output as its input."
)

st.divider()

# ── Key numbers ───────────────────────────────────────────────────────────────

st.subheader("Key numbers")

fund  = load("v_fund_size")
qct   = load("qc_trend")
m5sc  = load("module5_scorecard")
crsc  = load("cash_recon_scorecard")
recon = load("reconciliation")

n_quarters     = len(fund)
latest_holdings = int(fund.sort_values("period_end_date").iloc[-1]["total_holdings"])
latest_aum_bn   = float(fund.sort_values("period_end_date").iloc[-1]["total_value_usd_bn"])

bt              = load("bond_terms")
n_bonds         = len(bt)
bonds_found_r2  = int(m5sc.loc[m5sc["measure"] == "bonds_found",    "run2_value"].iloc[0])
bonds_pass_r2   = int(m5sc.loc[m5sc["measure"] == "bonds_pass_all_checks", "run2_value"].iloc[0])
n_bonds_build   = n_bonds - bonds_found_r2  # 142 - 36 = 106

# Latest quarter open exceptions
latest_qc       = qct.sort_values("period_end_date").iloc[-1]
open_exceptions = int(latest_qc["total_unique_exceptions"])
latest_period   = pd.to_datetime(latest_qc["period_end_date"]).strftime("%b %Y")

# Cash recon: total breaks across all v2 rounds
v2_rounds       = crsc[crsc["reconciler_version"].str.startswith("v2")]
total_planted   = int(v2_rounds["breaks_planted"].sum())

# Source comparison: matched pairs
n_matched       = int((recon["match_status"] == "matched").sum())
n_unmatched_side = int((recon["match_status"] == "nport_only").sum())

k1, k2, k3 = st.columns(3)
k4, k5, k6 = st.columns(3)

k1.metric("Quarters of holdings data",  n_quarters,
          help="Mar 2025 – Jun 2026")
k2.metric("Holdings per quarter",        f"~{latest_holdings}",
          help=f"${latest_aum_bn:.0f}B total value ({latest_period})")
k3.metric("Bonds extracted from filings", n_bonds,
          help=f"{n_bonds_build} build set + {bonds_found_r2} test set · {bonds_pass_r2}/{bonds_found_r2} pass all checks")
k4.metric("Open data-quality exceptions", open_exceptions,
          help=f"as of {latest_period} (all reviewed/expected)")
k5.metric("Cash recon breaks found",     f"{total_planted}/{total_planted}",
          help="across all 3 test rounds (v2 algorithm); first version scored 2/11 correct types on the hard round")
k6.metric("Source comparison matches",   n_matched,
          help=f"{n_unmatched_side} still unmatched per side (after fuzzy second pass)")

st.divider()

# ── Problems found and how we tackled them ────────────────────────────────────

st.subheader("Problems found and how we tackled them")
st.markdown(
    "Every one of these was a real finding, not a planted exercise.  "
    "Some were data problems; some were bugs in this platform's own code."
)

problems = [
    (
        "30 holdings with no CUSIP (the standard US security identifier)",
        "These are foreign-domiciled companies (e.g. semiconductor firms incorporated in Ireland).  "
        "The SEC filing reports them with an ISIN instead.  They have CINS identifiers "
        "(a CUSIP-format ID for foreign securities, e.g. Accenture G1151C101), but the N-PORT "
        "CUSIP field is left blank for them.  Documented as expected; no fix needed.",
    ),
    (
        "39 holdings per side could not be matched exactly; fuzzy matching recovered 25 more",
        "The two sources share no common identifier — matching is done by normalised name only.  "
        "Name formatting differences (abbreviations, punctuation, suffix variants) prevented "
        "39 out of 502 names from matching exactly.  A fuzzy second pass (token similarity, "
        "threshold 90) recovered 25 more pairs, leaving 14 per side unmatched.  "
        "The 139-day gap between the two snapshots also means portfolio changes "
        "(rebalancing, new inclusions, deletions) cannot be separated from name mismatches.",
    ),
    (
        "433 of 455 matched pairs differed by more than 1% in share count",
        "Share counts change with stock splits and rebalancing, not just price moves.  "
        "Switching to a weight-based comparison (portfolio weight, not share count) "
        "reduced meaningful breaks from 433 to 25.  Three holdings showed share ratios "
        "close to 2× or 4×, flagging possible stock splits — not verified against "
        "corporate-action records.",
    ),
    (
        "Parser bugs caused spurious 'missing category' flags in Module 3",
        "Module 3 flagged dozens of rows for missing asset/issuer categories.  "
        "Investigation in Module 4 showed the ingestion script was reading those fields "
        "from the wrong XML element, and the cash-collateral flag from the wrong tag.  "
        "Fixing the parser cleared all the spurious flags.",
    ),
    (
        "Two QC rules were too broad and needed re-scoping",
        "Rule R4 (duplicate CUSIP) was triggering on securities held both as a "
        "direct position and as cash collateral — a valid structure, not an error.  "
        "Rule R6 (balance ≈ valUSD) was flagging correctly-labelled money-market funds.  "
        "Both rules were narrowed to skip those expected cases.",
    ),
    (
        "An S&P 500 E-Mini futures contract flagged by multiple rules — not an error",
        "The fund holds an S&P 500 E-Mini futures contract with no CUSIP or ISIN by design — "
        "exchange-traded derivatives do not carry those identifiers.  Its reported value can be "
        "negative (unrealised loss on the position), which also triggered a rule.  Both are "
        "correct for a futures position.  The contract was promoted to D1 (Derivative, reviewed) "
        "in Module 4 and no longer counts as an open exception.",
    ),
    (
        "IsolationForest flagged 11 holdings — but mostly the largest ones",
        "The ML model (contamination=0.02) identified 11 of 508 rows as anomalous.  "
        "Nine were the fund's largest equity positions — statistically unusual by size, "
        "not data errors.  Two were BlackRock money-market holdings: one already flagged "
        "by Rule R4, and one that led to the creation of Rule R6.",
    ),
    (
        "5 of 17 bond maturities not extracted in the first test run",
        "The document reader's date pattern stopped too early on certain HTML filings.  "
        "Fixed in run 2 — all 36 maturities extracted correctly.",
    ),
    (
        "Alphabet EUR bond extractor read €100,000 denomination as the principal",
        "The extractor read the €100,000 minimum denomination line from the Alphabet EUR bond "
        "prospectus rather than the €750,000,000 aggregate principal amount.  "
        "Validation check V10 (principal must be a whole number of millions) flagged it "
        "immediately — €100,000 is 0.1 M, not an integer.  Fixed by targeting the "
        "'Aggregate Principal Amount' label explicitly.",
    ),
    (
        "Fraction notation bug: OpenFIGI comparison returned 32/36 on first try",
        "Coupons like '5 3/4' were parsed as the integer 5, so the comparison to "
        "OpenFIGI's 5.75 failed.  Fixed in the comparison script; re-run gives 36/36.  "
        "The run-1 test set (17 bonds) was unaffected because those coupons used "
        "decimal notation.",
    ),
    (
        "Guessed values removed from early scorecard versions",
        "An early version of the Module 5 scorecard included a day-count-convention "
        "default and an unsupported 95% figure.  Both removed; only verified results "
        "are reported.",
    ),
    (
        "Cash reconciliation checker scored 2 of 11 on the first hard test round",
        "The v1 reconciler had no same-bond pairing pass.  When a payment arrived very late "
        "or with a large shortfall, it could not connect the statement row to its expected "
        "payment, labelling it 'missing' and 'unexpected' — 7 false alarms and only 2 "
        "correct type labels out of 11 planted breaks.  The v2 algorithm added a same-bond "
        "pairing pass: unmatched in-portfolio CUSIPs are paired with the nearest unmatched "
        "expected payment for that issuer.  Result: 11/11 found and correctly labelled, "
        "0 false alarms.",
    ),
]

for title, detail in problems:
    with st.expander(f"**{title}**"):
        st.markdown(detail)

st.divider()

# ── Honest limits ─────────────────────────────────────────────────────────────

st.subheader("Honest limits")
st.markdown(
    """
- **One fund, one series.** All data is from iShares Core S&P 500 ETF (IVV) only.
  Results cannot be generalised to other funds without re-running the pipeline.
- **139-day source gap.** The two holdings snapshots used in Module 2 are from
  different dates (N-PORT as of 30 Jun 2026; iShares as of 16 Nov 2026). Portfolio
  changes in those 139 days look the same as data errors.
- **Stock splits not verified.** The three possible-split flags are based on share
  ratios alone; no corporate-action database was consulted.
- **Bond parsing coverage.** 142 bonds from 7 issuers (Apple, Microsoft, Alphabet,
  Eli Lilly, Johnson & Johnson as build set; Meta and Broadcom as unseen test set).
  Other issuers may have different document formats.
- **IsolationForest baseline.** The ML pass uses a single snapshot (the most recent
  quarter). A rolling baseline across all six quarters was not built.
- **Simulated cash flows only.** Module 6 tests the reconciliation algorithm against
  planted discrepancies; it does not use real payment records.
    """
)

st.divider()

# ── Tools used ────────────────────────────────────────────────────────────────

st.subheader("Tools")

col_t1, col_t2 = st.columns(2)
with col_t1:
    st.markdown(
        """
| Layer | Tool |
|---|---|
| Data storage | PostgreSQL 18 (local) |
| Ingestion / transforms | Python · pandas · sqlalchemy |
| ML anomaly detection | scikit-learn IsolationForest |
| Document parsing | pdfplumber · BeautifulSoup |
        """
    )
with col_t2:
    st.markdown(
        """
| Layer | Tool |
|---|---|
| Identifier cross-reference | OpenFIGI API (free) |
| Web dashboards | Streamlit · Plotly |
| Desktop dashboards | Power BI Desktop |
| Source data | SEC EDGAR (public, no signup) |
        """
    )

st.divider()

# ── GitHub link ───────────────────────────────────────────────────────────────

st.markdown(
    "**Source code:** "
    "[github.com/adwaitgaur/finance-data-platform]"
    "(https://github.com/adwaitgaur/finance-data-platform)"
)

# ── Glossary ──────────────────────────────────────────────────────────────────

with st.expander("Glossary"):
    st.markdown(
        """
| Term | Plain-English meaning |
|---|---|
| CUSIP | A 9-character code that uniquely identifies a US security (like a barcode for stocks and bonds) |
| ISIN | An international version of the CUSIP, used for securities from any country |
| CINS | A CUSIP-format identifier for foreign-domiciled securities |
| N-PORT | A monthly SEC filing where US funds report what they hold |
| ETF | Exchange-traded fund — a basket of securities you can buy like a stock |
| Reconciliation | Checking two records of the same thing against each other and explaining the differences |
| Reference data | The background facts about a security: its name, identifier, issuer, coupon rate, maturity date |
| IsolationForest | A machine-learning algorithm that finds unusual data points by isolating them from the rest |
| pctVal | The percentage of the fund's total value held in a single security |
| valUSD | The US-dollar market value of a security position |
| EDGAR | The SEC's public filing database |
        """
    )
