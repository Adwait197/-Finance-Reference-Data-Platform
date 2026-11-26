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

> **Computed values**: run 1 = **71%**, run 2 = **98%** (applicable-fields
> definition above).  Increased from 97% to 98% after Broadcom's 20 principal
> amounts were filled from the FWP preamble text (previously `not_found`).
> An earlier script reported 62% / 85% because it included all 24 columns in
> the denominator for every bond, penalising fixed-rate USD bonds for the
> three structurally-empty columns listed above.

### Reporting tables

| Table | Rows | Description |
|-------|------|-------------|
| `reporting.bond_terms` | 142 | One row per bond, all fields + source URLs |
| `reporting.bond_checks` | 1420 | One row per bond per check (142 × 10) |
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

## Module 6 — Simulated Cash Reconciliation

### Why simulated

Real custodian bank statements are not publicly available. To build a
reconciliation engine that can be tested against ground truth, the module
generates a synthetic bank statement from the Module 5 expected coupon
schedule, plants a controlled set of realistic breaks, seals the answer key
before the reconciler sees it, and then scores the reconciler objectively.

This mirrors what a data team at a custodian bank or asset manager actually
does when validating a new reconciliation system: build a test harness with
known breaks, run the engine blind, compare output to the answer key.

### What was built

| Component | Description |
|-----------|-------------|
| `module6_expected_coupons.csv` | 160 expected cash flows for 2026 (159 coupons + 1 principal) |
| `module6_bank_statement*.csv` | Synthetic bank statements with planted breaks (3 rounds) |
| `module6_answer_key*.csv` | Sealed break lists — only the scorer reads these |
| `reconcile_cash.py` | 9-pass reconciliation algorithm (v2) |
| `score_reconciliation.py` | Objective scorer: compares recon output to answer key |
| `reconciliation_rules.csv` | Machine-readable thresholds with plain-English rationale |

### How breaks are planted

Each round uses a fixed random seed to select expected payments and modify
them into one of 12 break types:

| Type | What it means |
|------|---------------|
| `missing` | Payment absent from the statement entirely |
| `late` | Arrived 1–5 business days after the due date |
| `very_late` | Arrived more than 5 business days late |
| `short_paid` | Received ≤1% less than expected (processing fee) |
| `large_deduction` | Received >1% less than expected (unusual shortfall) |
| `late_and_short` | Both late and short |
| `duplicate` | Same payment appears twice |
| `surprise` | A CUSIP with an obviously fake check digit or format |
| `valid_surprise` | A CUSIP that passes the check-digit algorithm but is not in the portfolio |
| `cusip_typo` | A one-character transposition of a known CUSIP |
| `split` | One expected payment arrives as two rows that sum to the total |
| `combined` | Two separate expected payments arrive as a single statement row |

The answer key is written to a separate sealed file *before* the reconciler
runs. The reconciler script has a comment at the top explicitly stating it
must never read the answer key — only `score_reconciliation.py` reads it.

### Reconciliation algorithm (9 passes)

`reconcile_cash.py` runs passes in order; a matched row is removed from the
unmatched pool before the next pass:

| Pass | Name | What it catches |
|------|------|----------------|
| 1 | Exact match | Same CUSIP, date, and amount within $0.01 |
| 2 | Combined | One statement row = sum of 2+ same-company same-date expected rows |
| 3 | Split | 2+ statement rows with same CUSIP+date sum to one expected amount |
| 4 | CUSIP typo | Hamming distance = 1 from a known CUSIP, check digit fails |
| 5 | Short paid | Same date, shortfall ≤1%, amount within tolerance |
| 6 | Duplicate | Exact-match statement row that already has a matched expected row |
| 7 | Late exact | Same CUSIP+amount, 1–5 business days late |
| 8 | Same-bond pairing | Remaining in-books CUSIPs paired with nearest unmatched expected; labels `late_and_short`, `late_needs_review`, `large_deduction_needs_review`, or `needs_review` |
| 9 | Remaining | Everything left becomes `unexpected` (statement) or `missing` (expected) |

A statement row whose CUSIP is in the portfolio is **never** labelled
`unexpected` — it always reaches pass 8 and gets a descriptive label.
Thresholds (late_window = 5 business days, short_threshold = 1%) are loaded
from `data/reference/reconciliation_rules.csv` at runtime.

### How the scorer decides "correct"

`score_reconciliation.py` reads the answer key and matches each planted break
to a recon row by `expected_payment_id` or `statement_row_id`. A label is
"correct" when it maps to the expected reconciler output for that break type:

| Answer key type | Expected reconciler label |
|----------------|--------------------------|
| `missing` | `missing` |
| `late` | `late` |
| `very_late` | `late_needs_review` |
| `short_paid` | `short_paid` |
| `large_deduction` | `large_deduction_needs_review` |
| `late_and_short` | `late_and_short` |
| `duplicate` | `duplicate` |
| `surprise` | `unexpected` |
| `valid_surprise` | `unexpected` |
| `cusip_typo` | `cusip_typo` |
| `split` | `split` |
| `combined` | `combined` |

The `_needs_review` labels are intentional: very_late and large_deduction
fall outside the auto-approve thresholds and are routed to analyst review
rather than silently accepted. The scorer counts these as correct — matching
them to `late` or `short_paid` would be a bug in the reconciler.

A **false alarm** is any recon row not traced to an answer-key entry. For
split breaks, the answer key has one entry but the recon produces two rows
(one per half-payment); the scorer credits both rows to the single entry so
neither generates a false alarm.

### Before / after scores

| Round | Statement | Reconciler | Planted | Found | Correct type | False alarms |
|-------|-----------|-----------|---------|-------|-------------|-------------|
| 1 | Easy (seed 42) | v2 | 19 | 19 | 19 | 0 |
| 2 | Hard (seed 7) | **v1 before fix** | 11 | 11 | **2** | **7** |
| 2 | Hard (seed 7) | v2 after fix | 11 | 11 | 11 | 0 |
| 3 | Mixed (seed 99) | v2 | 16 | 16 | 16 | 0 |

The v1 reconciler failed on Round 2 because it had no same-bond pairing pass:
`very_late` and `large_deduction` rows were paired with the wrong expected
payment and labelled `missing` / `unexpected`, causing 7 false alarms and
only 2 correct type labels (the two `valid_surprise` entries it happened to
get right).

### Limits

- **Round 1 surprise CUSIPs** (`ZZZZZZ001`, `ZZZZZZ002`) are obviously fake.
  Real wire-fraud or erroneous-payment scenarios would have structurally valid
  CUSIPs; the `valid_surprise` type in Rounds 2–3 covers this case.
- **Round 3** tests new examples of the same 12 break types used in Rounds 1
  and 2. It does not introduce new break categories — a reconciler that
  handles all 12 types will score 16/16, but that does not mean it handles
  every break that could appear in a real custodian statement.
- The expected-cash-flow schedule is derived from Module 5 bond terms (SEC
  EDGAR filings). Payment dates are correct for the bonds covered; bonds with
  missing interest payment dates fall back to a maturity-day rule.

### Reporting tables

| Table | Rows | Description |
|-------|------|-------------|
| `reporting.cash_expected` | 160 | One row per expected cash flow in 2026 |
| `reporting.cash_recon_results` | 491 | Recon results for all 3 rounds (164 + 163 + 164); includes matched rows |
| `reporting.cash_recon_scorecard` | 4 | Before/after scores: v1 Round 2, v2 Rounds 1–3 |
| `reporting.cash_recon_rules` | 2 | late_window and short_threshold with plain-English rationale |

Run `scripts/load_module6_to_postgres.py` to load or reload all four tables.

### Scripts

| Script | Purpose |
|--------|---------|
| `scripts/build_module6_expected_coupons.py` | Build expected cash flow schedule from Module 5 bond terms |
| `scripts/build_module6_bank_statement.py` | Round 1 bank statement (seed 42, 19 breaks) |
| `scripts/build_module6_bank_statement_hard.py` | Round 2 bank statement (seed 7, 11 harder breaks) |
| `scripts/build_module6_bank_statement_round3.py` | Round 3 bank statement (seed 99, 16 breaks, all 12 types) |
| `scripts/reconcile_cash.py` | 9-pass reconciliation algorithm (v2) |
| `scripts/score_reconciliation.py` | Objective scorer — the only script that reads the answer key |
| `scripts/score_all_rounds.py` | Runs all 3 rounds and prints the comparison table |
| `scripts/load_module6_to_postgres.py` | Load all results into reporting schema |

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
