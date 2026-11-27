# Finance Reference Data Platform

A personal project to build finance-domain knowledge and upgrade my data skills:
an end-to-end reference-data quality platform built on public SEC data for the
iShares Core S&P 500 ETF (IVV).

Built with AI-assisted development (Claude Code).

**Live dashboard:** https://finance-data-quality-reference-platform.streamlit.app/

**Data sources:** SEC EDGAR (N-PORT filings, 424B2 final prospectuses, FWP
free-writing prospectuses), iShares public holdings CSV, OpenFIGI API.

Instrument universe: iShares Core S&P 500 ETF (IVV), series S000004310.
Holdings as of Mar 2025 to Jun 2026 (filed May 2025 to Aug 2026),
507–508 holdings per filing.

---

## Modules

| # | Module | Status |
|---|--------|--------|
| 1 | Get the data — N-PORT filings → PostgreSQL security master | Complete |
| 2 | Compare two sources — cross-source reconciliation / break detection | Complete |
| 3 | Quality checks + ML — QC rule engine + IsolationForest anomaly detection | Complete |
| 4 | Dashboards — Streamlit + Power BI | Complete |
| 5 | Read bond documents — structured extraction from SEC 424B2/FWP filings | Complete |
| 6 | Simulated cash reconciliation | Complete |

---

## Problems found and how they were handled

- **30 holdings reported without CUSIP** — these are foreign-domiciled companies
  (Ireland, Switzerland, the Netherlands, etc.) that the SEC filing reports with
  an ISIN rather than a CUSIP. Many have CINS identifiers (a CUSIP-format ID
  for foreign securities), such as Accenture G1151C101, but the N-PORT field for
  CUSIP is blank. A surrogate key (`id`) ensures every row is uniquely addressable.
- **iShares endpoint ignores date parameters** — four methods were tried to get
  a same-date snapshot; all serve today's data. The 139-day gap is documented as
  a confirmed limitation of the free public source, not a project gap.
- **ML flagged the 11 largest holdings as anomalous** — statistically unusual
  because of extreme size, not data errors. One finding (balance ≈ valUSD for
  non-STIV rows) was promoted to deterministic Rule R6.
- **Alphabet EUR floating-rate note had wrong principal** — extractor picked up
  the €100,000 denomination line instead of the €750,000,000 aggregate amount;
  fixed by targeting the "Aggregate Principal Amount" label explicitly.
- **Broadcom principals were `not_found`** — 20 bonds had no labeled principal
  section; fixed by adding a fallback to read the preamble text.
- **Run 1 extracted 0 Meta bonds** — Meta's FWP layout differed from the other
  issuers; extractor needed updates before Meta bonds could be extracted (run 2).
- **Guessed values removed** — day-count convention defaults and an unsupported
  95% fields-filled figure were replaced with values extracted directly from the
  source documents or computed from data.
- **OpenFIGI fraction bug in comparison code** — the first run-2 OpenFIGI
  comparison returned 32/36 matches because OpenFIGI sometimes returns coupons
  as fractions ("5 3/4" instead of 5.75) and the comparison script read only the
  integer part. Fixed to 36/36 by parsing the fractional notation explicitly.
- **v1 reconciler scored 2/11 on the hard test round** — it had no same-bond
  pairing pass, so very_late and large_deduction payments were mislabelled
  `missing`/`unexpected`, causing 7 false alarms; fixed in v2 (9 passes).
- **Microsoft bonds have no 2026 payments** — all 10 bonds settle Nov 4, 2026
  with a maturity day of Nov 3; the first coupon falls after year end.

---

## Module 1 — N-PORT Data Ingestion

IVV files a Form N-PORT-P with the SEC monthly. Only quarter-end filings are
made public (filed within 60 days of quarter end); mid-quarter monthly filings
go to the SEC but are not publicly available. Each quarterly filing contains
the full portfolio — roughly 507–508 equity holdings and a few money-market or
derivative positions.

### What was downloaded

6 filings were downloaded from EDGAR by their confirmed accession numbers
(verified by checking that the target series ID `S000004310` appears in each
file before saving):

| Period end | Filing date | Holdings |
|------------|-------------|----------|
| 2025-03-31 | 2025-05-27 | 508 |
| 2025-06-30 | 2025-08-28 | 508 |
| 2025-09-30 | 2025-11-26 | 507 |
| 2025-12-31 | 2026-02-25 | 507 |
| 2026-03-31 | 2026-05-28 | 507 |
| 2026-06-30 | 2026-08-25 | 508 |

### PostgreSQL table

All six quarters are loaded into `security_master_history` (one row per
holding per filing date). Key design decisions:

- **Surrogate key `id`**: 30 holdings per filing are reported without a CUSIP.
  These are foreign-domiciled companies (Ireland, Switzerland, Netherlands, etc.)
  that the SEC filing records with an ISIN rather than a CUSIP — even though
  many have CINS identifiers (e.g. Accenture G1151C101). An auto-increment `id`
  ensures every row is uniquely addressable.
- **`isCashCollateral` flag**: used by the QC rule engine to separate
  securities-lending cash collateral from direct equity holdings.

### Scripts

| Script | Purpose |
|--------|---------|
| `scripts/download_historical_nport.py` | Download all 6 quarterly N-PORT filings |
| `scripts/parse_holdings.py` | Parse individual N-PORT XML into a DataFrame |
| `scripts/load_history_to_postgres.py` | Load all 6 quarters into `security_master_history` |

---

## Module 2 — Cross-Source Reconciliation

Compares the N-PORT holdings as of Jun 30, 2026 (filed Aug 25, 2026) against
the iShares public holdings CSV (downloaded Nov 17, 2026, reflects holdings
as of Nov 16, 2026).  Date gap: 139 days (Jun 30 to Nov 16, 2026).
An earlier figure of 84 days used the SEC filing date instead of the
N-PORT period-end date; 139 days is the correct gap between the two
holdings dates.

### Method

The two sources share no common identifier, so holdings are matched by
normalised company name (stripped of legal suffixes, punctuation, and case
differences). A **weight break** is flagged when a matched pair has
|pctVal − Market Weight| > 0.15 percentage points.

Weight was chosen over raw share count because a 139-day gap means share
counts change with stock splits and rebalancing, so a raw share-count
comparison would flag the majority of matched pairs (those with >1%
difference in share count). The weight-based criterion reduces this to 28
breaks across all 488 matched pairs.

### Results

| Category | Count |
|----------|-------|
| Matched by name (exact normalisation) | 463 |
| Matched by name (fuzzy, score ≥ 90) | 25 |
| N-PORT only (no match found) | 14 |
| iShares only (no match found) | 14 |
| Weight breaks (>0.15 pp difference) | 28 |
| Stock-split flags | 3 |

The 3 stock-split flags (CrowdStrike 4x, Amphenol 2x, Monster Beverage 2x)
have a share ratio close to 2× or 4× — possible stock splits, not verified
against corporate-action records.

A fuzzy second pass (rapidfuzz token_set_ratio, threshold 90) recovered 25
pairs that exact normalisation missed, reducing unmatched rows from 39 per
side to 14. A token_set_ratio score of 100 means one name's words are all
contained in the other, not that the names are identical, so every fuzzy pair
was reviewed by hand and recorded in fuzzy_matches_reviewed.csv. All 25 were
confirmed as the same company.
Improved name normalisation (stripping CORPORATION, COMPANIES, HOLDINGS,
COS and other legal-suffix variants) moved a further 22 previously-unmatched
companies into the exact-match pass.

With a 139-day gap between the two sources, the 14 unmatched holdings per side
and the 28 weight breaks cannot be distinguished from portfolio changes (index
rebalancing, new inclusions, deletions). The unmatched rows also reflect
name-normalisation failures — without a shared identifier, any company whose
name normalises differently in the two sources appears as unmatched.

### Date-gap limitation

The N-PORT holds positions as of Jun 30, 2026 (filing date Aug 25, 2026); the
iShares file reflects Nov 16, 2026 — a 139-day gap.  An earlier version of
this file cited 84 days, computed from the SEC filing date (Aug 25) rather
than the N-PORT period-end date (Jun 30); 139 days is correct.
Four methods were tried to get a same-date iShares snapshot: the
`asOfDate` URL parameter, the Wayback Machine, a manual browser download, and
the open-source `etf_scraper` package. All four hit the same root cause —
iShares' public AJAX endpoint ignores any date parameter and always serves the
current snapshot. This is a confirmed limitation of the free public source.

### Same-date internal consistency check

As a separate sanity check, the N-PORT's own pctVal column was verified against
the value implied by valUSD and total fund AUM for all 508 holdings on the same
date (Aug 25, 2026). Result: **0 / 508 flagged** (maximum absolute difference
0.009 percentage points, within rounding from the XML source).

### Scripts

| Script | Purpose |
|--------|---------|
| `scripts/reconcile_sources.py` | Name-match, weight-break detection, split flag |
| `scripts/internal_consistency_check.py` | Same-date pctVal vs implied weight |

---

## Module 3 — QC Rule Engine + ML Anomaly Detection

### QC rules

Six rules (R1–R6) detect data quality issues. Three review labels (N1, E1, D1)
were added in Module 4 after the initial run, to distinguish new exceptions
needing review from confirmed expected positions.

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
N1 count toward the open-exception total; E1 and D1 are reported separately as
Expected (reviewed) and not counted as open.

Script: `scripts/qc_rules.py`

### IsolationForest anomaly detection

An IsolationForest model (`contamination=0.02`, `random_state=42`) was run on
all 508 holdings for the Aug 2026 quarter using three features: `valUSD`,
`pctVal`, and `balance` (all z-scored before fitting).

**Result: 11 / 508 holdings flagged as anomalous** — mostly the largest
positions: NVIDIA, Apple, Microsoft, Amazon, BlackRock Funds III (×2),
Alphabet (×2), Broadcom, Micron, and Tesla. These are statistically unusual
because of extreme size, not because they contain data errors.

Of the two BlackRock Funds III rows: one was already flagged by the
duplicate-CUSIP rule (R4); the other was a new ML finding — a money-market STIV
position where balance ≈ valUSD on a non-STIV `isCashCollateral=N` row. That
second finding became Rule R6 ("possible cash instrument"). The rule was
subsequently narrowed in Module 4 to skip correctly-labelled STIV rows,
leaving only genuinely mis-categorised positions in scope.

Script: `scripts/ml_anomaly_detection.py`

---

## Module 4 — Data Quality Dashboards

### What the dashboards show

**Streamlit app** (`app/Home.py`) — a 6-page interactive dashboard covering all
six modules, deployed on Streamlit Community Cloud.  Run locally with
`streamlit run app/Home.py`.  The app reads only from `app/data/*.parquet`;
run `scripts/export_reporting_for_app.py` to refresh those files from PostgreSQL.

Pages: **Home** (overview, key numbers, problems log) · **Data Quality**
(fund size, exception trend, rule heatmap, quarter drill-down) · **Source
Comparison** (match breakdown, weight breaks, unmatched holdings) · **Bond
Documents** (bond explorer, V1–V10 results, run-1/run-2 scorecard) · **Cash
Reconciliation** (cash flows, break types, before/after scorecard) · **Checks
and Controls** (every automatic check in one registry table).

**Power BI file** (`PowerBI/IVV_Data_Quality.pbix`) — an interactive Power BI
report with 4 pages (Overview, Checks, Bond Documents, Cash Reconciliation) and
a quarter slicer.  `PowerBI/IVV_Data_Quality.pdf` is a PDF export of all 4 pages.

### What the trend revealed

The first run of `qc_rules.py` produced 4–5 flags per quarter. Working through
each one:

- One flag was an **S&P 500 E-Mini futures contract** — no CUSIP or ISIN by
  design, valUSD is unrealised P&L and can be negative. Not an error; promoted
  to D1 (Derivative, reviewed).
- Two flags were caused by **parser bugs in our own code** — the XML ingestion
  script read `assetCat` and `issuerCat` from the wrong XML element and the wrong
  collateral flag, causing R5 (missing category) hits on otherwise valid rows.
  Fixed in the ingestion script.
- Two more flags came from **over-broad rules** that were re-scoped:
  R4 was narrowed to check for duplicate CUSIPs only within the same
  `isCashCollateral` bucket (not across the whole portfolio); R6 was narrowed to
  skip correctly-labelled STIV rows. The affected positions were promoted to E1
  (Expected) after review.

Final result: **0 open exceptions + 4 reviewed** (3 E1 BlackRock Funds III money-
market positions, 1 D1 E-Mini futures) across all 6 quarters.

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
streamlit run app/Home.py
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

### Issuers, deals, and bonds

| Set | Issuers | Bonds | Deals | FWPs |
|-----|---------|-------|-------|------|
| Build | Apple (16), Microsoft (10), Alphabet (42), Eli Lilly (30), Johnson & Johnson (8) | 106 | 25 | 18 |
| Test | Meta (16), Broadcom (20) | 36 | 10 | 9 |
| **Total** | | **142** | **35** | **27** |

(Alphabet has 11 deals sharing some FWPs; Microsoft has 1 deal covering 10
bonds. FWP count is unique accessions — some deals share an FWP.)

### Extracted fields per bond

CUSIP, ISIN, bond name, issuer, currency, rate type, coupon, maturity date,
trade date, settlement date, day count convention, principal amount, price,
yield, benchmark treasury yield, spread, ratings (Moody's + S&P), floating
benchmark and spread, and the source document URL.

**Rate types in this dataset:** 133 fixed-rate, 9 floating-rate.
**Currencies:** USD (109), EUR (20), CAD (8), GBP (5).

### Validation checks (V1–V10)

| Check | Description |
|-------|-------------|
| V1 | CUSIP format (9 characters, check digit valid) |
| V2 | ISIN format (12 characters, Luhn check) |
| V3 | CUSIP prefix matches issuer's equity CUSIP prefix |
| V4 | Bond yield = benchmark treasury yield + spread (within 0.5 bps); GBP bonds use `yield_semiannual` (Gilt spread is quoted semi-annually) |
| V5 | Coupon / yield / price internally consistent; GBP bonds use `yield_annual` (annual-pay coupon) |
| V6 | All three dates present, trade ≤ settlement < maturity, and maturity year matches the bond name |
| V7 | Day count convention valid for the bond's currency |
| V8 | CUSIP unique within the same deal |
| V9 | Moody's and S&P ratings are recognised grades (e.g. Aaa, AA+) |
| V10 | Principal amount is a whole number of millions |

### Fields-filled definition

**Fields filled = non-null applicable cells ÷ total applicable cells.**
Current value (run 2, from `reporting.module5_scorecard`): **96%**.

A cell is "applicable" when the field makes sense for that specific bond's
type and currency. Two groups of fields are excluded from the denominator for
bonds where they do not apply:

- `floating_benchmark` and `floating_spread_pct`: only applicable to
  floating-rate bonds; null by design for fixed-rate bonds.
- `yield_annual`: only applicable to EUR and GBP bonds (annual-yield
  convention); USD and CAD bonds use the semi-annual convention only.

Both runs are scored against the same canonical 23-field list (the columns
present in the run-1 test CSV, minus tracking/metadata columns). Run-2 added
three new columns (`interest_payment_dates`, `principal_source`, `fwp_accession`)
that are not extraction fields; excluding them keeps the comparison fair.

**Run 1: 71% | Run 2: 100%** (from `reporting.module5_scorecard`).
An earlier figure of 96% for run 2 included the later-added columns in the
denominator, deflating the score. The 100% figure is the correct value and is
shown in the Power BI scorecard after the reporting schema is refreshed.

Note: Meta and Broadcom have no EUR or GBP bonds, so `yield_annual` is excluded
from the denominator for all 36 test bonds in both runs.

### Apple validation (build set, 16 bonds, 3 deals)

Three independent checks were run on the 16 Apple bonds before extending the
extractor to other issuers:

**FWP vs 424B2 reconciliation** — 67 field comparisons across 3 deals
(principal amount, coupon, maturity year, price to public). Result:
**65 / 65 applicable comparisons match** (2 not_applicable for floating-rate
bonds where coupon is not a fixed field). Zero mismatches.

**OpenFIGI cross-check** — issuer, coupon, and maturity date checked against
OpenFIGI for all 16 bonds (48 checks total). Result: **46 / 46 applicable
checks pass** (2 not_applicable for the two floating-rate Apple bonds where
coupon is not applicable). Zero mismatches.

**Blind hand-check** — 5 fields that OpenFIGI does not cover (Moody's rating,
S&P rating, day count convention, trade date, settlement date) were manually
looked up in the source FWPs for 3 bonds (one per deal). The extractor matched
on **15 / 15**; the one disagreement was a human reading slip, confirmed by
re-checking the source.

### Test scorecard — run 1 vs run 2

The extractor was scored twice on a held-out test set:

| Measure | Run 1 (Meta + Broadcom; Meta 0, Broadcom 17) | Run 2 (Meta + Broadcom, 36 bonds) |
|---------|---------------------------------------------|----------------------------------|
| Bonds extracted | 17 | 36 |
| Bonds passing all checks (V1–V8 for run 1; V1–V10 for run 2) | 0 | 36 |
| Fields filled (23-field canonical list) | 71% | 100% |
| OpenFIGI coupon match | 17 / 17 | 36 / 36 |
| OpenFIGI maturity match | 12 / 17 | 36 / 36 |

Run 1 attempted both Meta and Broadcom but extracted 0 Meta bonds because Meta's
FWP layout was not yet handled. The 17 Broadcom bonds passed 0 of V1–V8 because
the original extractor had multiple failures on Broadcom's formatting. V9 and
V10 did not exist at the time of run 1; for the V1–V8 checks, V1, V3, V5, and
V8 passed for all 17 bonds — the failures were mainly missing fields.

The 5 maturity misses in Run 1 were extractor errors: the reader's date pattern
stopped too early on these HTML documents, returning just the month name
("October" instead of "October 15, 2028"), so the comparison reported
`no_data (truncated)`. Fixed in run 2 by improving the date extraction pattern.

The run-2 OpenFIGI comparison script also fixed a fraction-parsing bug —
OpenFIGI sometimes returns coupons as "5 3/4" rather than 5.75; the first run-2
comparison returned 32/36 matches because the script read only the integer part.
Fixed to 36/36. Run-1 coupons were unaffected (17/17) because Broadcom's
OpenFIGI entries used decimal notation.

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
| `scripts/validate_build_tranches.py` | V1–V10 checks on build set |
| `scripts/validate_test_run2.py` | V1–V10 checks on test set (run 2) |
| `scripts/reconcile_fwp_vs_prospectus.py` | FWP vs 424B2 reconciliation for Apple |
| `scripts/check_cusips_openfigi.py` | OpenFIGI coupon/maturity check for build set |
| `scripts/check_test_openfigi.py` | OpenFIGI coupon/maturity check — run 1 |
| `scripts/check_test_openfigi_run2.py` | OpenFIGI coupon/maturity check — run 2 (fraction-parse fix) |
| `scripts/load_module5_to_postgres.py` | Load all results into reporting schema |

---

## Module 6 — Simulated Cash Reconciliation

### Why simulated

Real custodian bank statements are not publicly available. To build a
reconciliation engine that can be tested against ground truth, the module
generates a synthetic bank statement from the expected cash-flow schedule built
from Module 5 bond terms, plants a controlled set of realistic breaks, seals
the answer key before the reconciler sees it, and then scores the reconciler
objectively.

This is similar to how a new reconciliation system can be tested before it goes
live: build a test harness with known breaks, run the engine blind, compare
output to the answer key.

### Assumptions

- **Universe:** all 101 USD fixed-rate bonds in `reporting.bond_terms`
  (both build and test sets; 86 have at least one 2026 payment, 15 do not).
- **Hypothetical face holding:** $10,000,000 per bond (a uniform notional used
  to generate coupon cash amounts; not derived from N-PORT positions).
- **Year scope:** 2026 payments only (159 coupons + 1 principal repayment =
  160 expected cash flows).
- **Payment dates:** 66 bonds use interest payment dates extracted from the
  FWP documents; 20 bonds (Apple 4, Broadcom 16) use the maturity-day rule
  (payment on the day of the month matching the maturity date, six months apart)
  because payment dates were not extracted reliably from their documents (e.g.
  Apple's shared date line in the term sheet did not match 4 bonds). Weekend
  and US federal holiday dates are rolled forward to the next business day.

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
| `cusip_typo` | One character changed in a known CUSIP |
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
- **Payment dates** for 20 bonds are computed from the maturity-day rule
  rather than document dates. If the actual interest payment dates differ from
  the maturity month/day pattern, those bonds' payments will be on the wrong
  day.

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
copy .env.example .env         # then fill in PGPASSWORD and PBI_READER_PASSWORD
```

PostgreSQL 18 must be running locally with a database named `finance_platform`.

### How to rebuild everything (run in this order)

1. `scripts/download_historical_nport.py` — download 6 N-PORT filings
2. `scripts/parse_holdings.py` + `scripts/load_history_to_postgres.py` — ingest into `security_master_history`
3. `scripts/qc_rules.py` — run QC rule engine for all quarters
4. `scripts/ml_anomaly_detection.py` — run IsolationForest on Aug 2026 quarter
5. `scripts/qc_trend.py` — build reporting schema objects for dashboards
6. `scripts/reconcile_sources.py` — Module 2 cross-source reconciliation
7. `scripts/internal_consistency_check.py` — Module 2 same-date pctVal check
8. `scripts/module5_documents.py` — fetch 424B2 filings for 7 issuers
9. `scripts/module5_fwp.py` — match deals to FWPs
10. `scripts/extract_build_tranches.py` — extract 106 build-set bonds
11. `scripts/validate_build_tranches.py` — V1–V10 on build set
12. `scripts/reconcile_fwp_vs_prospectus.py` — Apple FWP vs prospectus check
13. `scripts/check_cusips_openfigi.py` — Apple/build OpenFIGI check
14. `scripts/extract_test_tranches.py` — extract 36 test-set bonds
15. `scripts/validate_test_run2.py` — V1–V10 on test set
16. `scripts/check_test_openfigi_run2.py` — test-set OpenFIGI check (run 2)
17. `scripts/load_module5_to_postgres.py` — load bond_terms, bond_checks, scorecard
18. `scripts/build_module6_expected_coupons.py` — build 2026 cash-flow schedule
19. `scripts/build_module6_bank_statement.py` — Round 1 synthetic statement
20. `scripts/build_module6_bank_statement_hard.py` — Round 2 synthetic statement
21. `scripts/build_module6_bank_statement_round3.py` — Round 3 synthetic statement
22. `scripts/reconcile_cash.py` — run reconciliation (all 3 rounds)
23. `scripts/score_all_rounds.py` — score all 3 rounds against answer keys
24. `scripts/load_module6_to_postgres.py` — load cash recon results into reporting schema
