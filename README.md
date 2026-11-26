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

## Module 5 — Prospectus / FWP Data Extraction

Extracts structured bond terms from SEC EDGAR free-writing prospectuses (FWP)
and 424B2 final prospectuses for 7 issuers across 35 deals.

### Issuers and sets

| Set | Issuers | Bonds | FWPs |
|-----|---------|-------|------|
| Build | Apple, Microsoft, Alphabet, Eli Lilly, Johnson & Johnson | 106 | 18 |
| Test | Meta, Broadcom | 36 | 9 |
| **Total** | | **142** | **27** |

### Extracted fields per bond

CUSIP, ISIN, bond name, issuer, currency, rate type, coupon, maturity date,
trade date, settlement date, day count convention, principal amount, price,
yield, benchmark treasury yield, spread, ratings (Moody's + S&P), floating
benchmark and spread, and the source document URL.

### Validation checks (V1–V8)

| Check | Description |
|-------|-------------|
| V1 | CUSIP format (9 characters, check digit valid) |
| V2 | ISIN format (12 characters, Luhn check) |
| V3 | CUSIP prefix matches issuer's equity CUSIP prefix |
| V4 | Bond yield = benchmark treasury yield + spread (within 0.5 bps) |
| V5 | Coupon / yield / price internally consistent |
| V6 | Trade date, settlement date, and maturity date all present |
| V7 | Day count convention valid for the bond's currency |
| V8 | CUSIP unique within the same deal |

### Fields-filled definition

**Fields filled = non-null applicable cells ÷ total applicable cells.**

A cell is "applicable" when the field makes sense for that specific bond's
type and currency.  Three fields are excluded from the denominator for the
bonds in this dataset:

- `floating_benchmark` and `floating_spread_pct`: only relevant for
  floating-rate bonds.  All 142 bonds in this dataset are fixed-rate, so
  these fields are always null by design — not a data gap.
- `yield_annual`: only reported for EUR-denominated bonds (annual yield
  convention).  USD/CAD bonds use the semi-annual convention (`yield_pct`).

> **Computed values**: run 1 = **71%**, run 2 = **97%** (applicable-fields
> definition above).  An earlier script reported 62% / 85% because it
> included all 24 columns in the denominator for every bond, penalising
> fixed-rate USD bonds for the three structurally-empty columns listed above.

### Reporting tables

| Table | Rows | Description |
|-------|------|-------------|
| `reporting.bond_terms` | 142 | One row per bond, all fields + source URLs |
| `reporting.bond_checks` | 1136 | One row per bond per check (142 × 8) |
| `reporting.module5_scorecard` | 9 | Run-1 vs run-2 test comparison |

Run `scripts/load_module5_to_postgres.py` to load or reload all three tables.

### Source document links

Each bond in `reporting.bond_terms` carries:
- `source_doc_url` — the exact FWP filing the bond was extracted from
  (resolved from the `fwp_accession` column stamped by the extractor)
- `prospectus_url` — the matching 424B2 final prospectus for the same deal

### Scripts

| Script | Purpose |
|--------|---------|
| `scripts/module5_documents.py` | Fetch 424B2 filings from EDGAR, group into deals |
| `scripts/module5_fwp.py` | Match each deal to its FWP |
| `scripts/extract_build_tranches.py` | Extract 106 build-set bonds |
| `scripts/extract_test_tranches.py` | Extract 36 test-set bonds (run 2) |
| `scripts/validate_build_tranches.py` | V1–V8 checks on build set |
| `scripts/validate_test_run2.py` | V1–V8 checks on test set |
| `scripts/check_test_openfigi_run2.py` | OpenFIGI coupon/maturity cross-check |
| `scripts/load_module5_to_postgres.py` | Load all results into reporting schema |

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
