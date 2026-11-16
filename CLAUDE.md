# Security Reference Data Governance & Analytics Platform

## Purpose
Portfolio project for Adwait's data analyst/engineer job search, targeting BlackRock
and Capital One data roles specifically (plus general finance-sector data roles).
Modeled on the rigor of a prior completed project: GLP-1 Market Intelligence
Platform (PostgreSQL + dbt + SAS + Streamlit).

## Modules (build in this order)
1. **Reference data ingestion** (targets BlackRock DIG QC + CMDS) — pull ETF
   holdings data from SEC EDGAR N-PORT filings (free, public, no API key) into
   a security "master" table in PostgreSQL.
2. **Cross-source reconciliation / break detection** (targets BlackRock DIG QC) —
   compare the master table against a second free public source; flag
   mismatched/missing/stale records as "breaks."
3. **Automated QC rule engine + ML anomaly detection** (targets BlackRock DIG QC +
   Capital One Data Management) — null/range/duplicate checks in SQL/dbt, plus a
   scikit-learn IsolationForest pass for anomalies the rules miss.
4. **Prospectus/document extraction** (targets BlackRock CMDS — mirrors their
   "Docuflow" tool) — parse real SEC EDGAR 424B/497 prospectus filings with
   Python (pdfplumber/BeautifulSoup + regex) to extract issuer, coupon, maturity,
   CUSIP into structured fields.
5. **Power BI dashboards** (targets Capital One Business Intelligence pillar) —
   exception trend charts, break rates by category, self-service filtering.
6. **Data governance documentation** (targets Capital One Data Management pillar) —
   write real lineage docs, metadata definitions, and control documentation —
   not just code comments.
7. **Innovation/self-service layer** (targets Capital One Innovation pillar) —
   ad-hoc exploratory querying on top of the warehouse.

## Tooling — all free, nothing with a trial-expiry clock
- Python: pandas, numpy, scikit-learn, pdfplumber, BeautifulSoup
- PostgreSQL (local) + dbt Core for transforms
- SEC EDGAR as the only external data source (free, public, no signup)
- Power BI Desktop for dashboards
- Git/GitHub for version control

## Working style (apply throughout)
- Explain every important command/step in plain language: what it does and why,
  before running it.
- Step-by-step pacing — confirm each phase is working before moving to the next.
- Learning priorities to actively reinforce: SQL + Python automation, Power BI/DAX,
  data governance documentation, ML anomaly detection fundamentals.
- Never claim a skill, tool, or result that wasn't actually built and verified —
  honesty-first, same as the resume work (see job-search project for the full
  honesty rules if this repo is ever cross-referenced).

## Not yet built — do not claim until done
Everything above is the target scope. Nothing is built yet as of this file's
creation. Update this section as modules complete, same discipline as the
GLP-1 project's status doc.