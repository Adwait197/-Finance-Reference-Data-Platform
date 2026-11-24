# Security Reference Data Governance & Analytics Platform

A personal project to build finance-domain knowledge and upgrade my data skills:
an end-to-end reference-data quality platform built on public SEC data for the
iShares Core S&P 500 ETF (IVV).

Data source: SEC EDGAR N-PORT filings (free, public, no sign-up required).
Instrument universe: iShares Core S&P 500 ETF (IVV), series S000004310, 6
quarters (Mar 2025 – Jun 2026, ~507–508 holdings per quarter).

---

## Modules

| # | Module | Status |
|---|--------|--------|
| 1 | Reference data ingestion (N-PORT → PostgreSQL security master) | Complete |
| 2 | Cross-source reconciliation / break detection | Planned |
| 3 | QC rule engine + ML anomaly detection | Complete |
| 4 | Data quality dashboards (Streamlit + Power BI) | Complete |
| 5 | Prospectus/document extraction | Planned |
| 6 | Data governance documentation | Planned |
| 7 | Innovation / self-service layer | Planned |

---

## Module 3 — QC Rule Engine

Nine rules applied to every holding each quarter:

| Code | Short name | Technical check |
|------|-----------|-----------------|
| R1 | No identifier | Both CUSIP and ISIN are blank |
| R2 | Weight out of range | pctVal outside [0, 100] |
| R3 | Negative value | valUSD < 0 |
| R4 | Duplicate CUSIP | Same CUSIP in same isCashCollateral bucket |
| R5 | Missing category | assetCat or issuerCat blank |
| R6 | Possible cash instrument | balance ≈ valUSD on a non-STIV row |
| N1 | New – needs review | STIV or derivative not yet in reviewed list |
| E1 | Money-market / cash collateral (reviewed) | STIV confirmed expected |
| D1 | Derivative (reviewed) | Exchange-traded futures confirmed expected |

**Expected exceptions** are recorded in `data/reference/expected_exceptions.csv`
and reviewed manually before being promoted from N1 to E1 or D1. Only R1–R6 and
N1 count toward the open-exception total; E1 and D1 are suppressed.

Script: `scripts/qc_trend.py`

---

## Module 4 — Data Quality Dashboards

### What the dashboards show

**Streamlit app** (`scripts/dashboard.py`) — four panels on one page:
- **Fund size trend** — total holdings and AUM (USD bn) across 6 quarters.
- **Open vs expected exceptions** — two-line trend chart: open exceptions (R1–R6
  + N1, red) and expected exceptions (E1 + D1, grey dotted).
- **Rule-by-quarter heatmap** — count of exceptions per rule per quarter; steel-
  grey cells for Expected rows (E1/D1), white/light for open rules.
- **Flagged-holdings drill-down** — per-row detail for the selected quarter:
  security name, status, reason, CUSIP, ISIN, assetCat, issuerCat,
  isCashCollateral, balance, valUSD, pctVal.

**Power BI file** (`PowerBI/IVV_Data_Quality.pbix`) — the same views rebuilt as
an interactive Power BI report with a quarter slicer and drill-through. A PDF
export is included as `PowerBI/IVV_Data_Quality.pdf`.

### Reporting schema (single source for both dashboards)

Both dashboards read exclusively from the `reporting` schema in PostgreSQL:

| Object | Contents |
|--------|----------|
| `reporting.qc_exceptions` | One row per flagged holding per quarter |
| `reporting.qc_trend` | One row per quarter — open/expected counts, AUM |
| `reporting.v_fund_size` | View: total holdings and AUM per quarter |
| `reporting.dim_quarter` | One row per quarter — labels and slicer_label |
| `reporting.dim_rule` | One row per rule code — name, group, description |

Run `scripts/qc_trend.py` to refresh all five objects from the raw
`security_master_history` table.

### Read-only Power BI login

A `powerbi_reader` PostgreSQL role has SELECT access on the `reporting` schema
only. It cannot access `public` or any other schema. The password is stored in
`.env` as `PBI_READER_PASSWORD` (never committed). The placeholder key
`PBI_READER_PASSWORD=` is in `.env.example`.

### Running the Streamlit app

```
# From the project root, with the venv activated:
streamlit run scripts/dashboard.py
```

Then open http://localhost:8501 in a browser.

### Opening the Power BI report

Open `PowerBI/IVV_Data_Quality.pbix` in Power BI Desktop. The report connects
to the local PostgreSQL instance as `powerbi_reader`. Refresh data in Power BI
after running `qc_trend.py` to pick up the latest quarter.

---

## Setup

```
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env           # then fill in PGPASSWORD and PBI_READER_PASSWORD
```

PostgreSQL 18 must be running locally with a database named `finance_platform`.
Run `scripts/download_historical_nport.py` to ingest N-PORT filings, then
`scripts/qc_trend.py` to populate the reporting schema.
