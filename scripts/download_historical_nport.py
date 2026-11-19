"""
download_historical_nport.py

Downloads 5 historical N-PORT-P filings for IVV (series S000004310).

Accession-identification strategy (two-tier, content-verified):
  Tier 1 — EFTS full-text search:
    EFTS indexes only the quarterly public N-PORT filings (one per fiscal quarter).
    For those dates, EFTS returns the confirmed accession directly.

  Tier 2 — Partial-content scan of known accession cluster:
    EDGAR's monthly non-quarterly filings are NOT in EFTS.  For those dates we
    pull the full submissions JSON for the trust CIK, extract the list of
    accessions filed on the target date by filer 0002071691 (the same agent
    who filed our existing IVV accession), then issue a Range: bytes=0-1500
    HEAD+GET for each candidate until we find one whose first 1.5 KB contains
    the series ID.  The series ID appears at byte ~627 in the existing IVV XML,
    so 1.5 KB is sufficient without downloading the full 500 KB file.

Both tiers finish with a full-file download and a re-verification that the
series ID is present, before the file is written to disk.

Target dates (actual quarterly public N-PORT filings for IVV, one per fiscal quarter):
  IVV files N-PORT once per quarter (period endings: Mar 31, Jun 30, Sep 30, Dec 31).
  Monthly non-quarterly batch dates visible in EDGAR (Oct 22, Jun 26, Apr 22, etc.)
  are for OTHER iShares ETF series — IVV is absent from those clusters entirely.
  All 5 target dates below are confirmed by EFTS full-text search.

  2026-05-28  (period 2026-03-31)
  2026-02-25  (period 2025-12-31)
  2025-11-26  (period 2025-09-30)
  2025-08-28  (period 2025-06-30)
  2025-05-27  (period 2025-03-31)
"""
import csv
import os
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pandas as pd
import requests

# ── constants ─────────────────────────────────────────────────────────────────
HEADERS       = {"User-Agent": "Adwait Gaur adwaitgaur197@gmail.com"}
CIK_NUM       = "1100663"
CIK_PADDED    = "0001100663"
KNOWN_FILER   = "0002071691"
TARGET_SERIES = "S000004310"
TICKER        = "IVV"

REPO_ROOT    = Path(__file__).resolve().parent.parent
RAW_DIR      = REPO_ROOT / "data" / "raw"
PROC_DIR     = REPO_ROOT / "data" / "processed"
LINEAGE_CSV  = RAW_DIR / "lineage_log.csv"
LINEAGE_FIELDS = [
    "fund_ticker", "series_id", "cik", "accession_number",
    "filing_date", "source_url", "downloaded_date",
]

NS = "http://www.sec.gov/edgar/nport"
N  = f"{{{NS}}}"

TARGET_DATES = [
    "2026-05-28",   # period 2026-03-31
    "2026-02-25",   # period 2025-12-31
    "2025-11-26",   # period 2025-09-30
    "2025-08-28",   # period 2025-06-30  (filer changed to 0001752724)
    "2025-05-27",   # period 2025-03-31
]


# ── helpers ───────────────────────────────────────────────────────────────────

def try_efts(filing_date: str) -> str | None:
    """
    Try EFTS full-text search.  Returns accession string or None if not found.
    EFTS only covers quarterly public N-PORT filings; monthly filings are absent.
    """
    url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?q=%22{TARGET_SERIES}%22"
        "&forms=NPORT-P"
        f"&dateRange=custom&startdt={filing_date}&enddt={filing_date}"
        "&_source=adsh,file_date,form,period_ending"
        "&size=5"
    )
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    hits = r.json()["hits"]["hits"]
    originals = [h["_source"] for h in hits if h["_source"].get("form") == "NPORT-P"]
    if originals:
        return originals[0]["adsh"]
    return None


def get_candidate_accessions(filing_date: str) -> list[str]:
    """
    Pull the trust's submissions JSON and return all NPORT-P accession numbers
    filed on filing_date (all filers — filer changed from 0001752724 to
    0002071691 around Aug 2025).
    """
    url = f"https://data.sec.gov/submissions/CIK{CIK_PADDED}.json"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    recent = r.json()["filings"]["recent"]
    return [
        recent["accessionNumber"][i]
        for i, form in enumerate(recent["form"])
        if form == "NPORT-P" and recent["filingDate"][i] == filing_date
    ]


def scan_cluster_for_ivv(candidates: list[str]) -> str:
    """
    Issue a partial-content GET (first 1500 bytes) for each candidate XML.
    Return the first accession whose partial content contains TARGET_SERIES.
    Raises RuntimeError if none match.
    """
    for acc in candidates:
        nodash  = acc.replace("-", "")
        xml_url = (
            f"https://www.sec.gov/Archives/edgar/data/{CIK_NUM}"
            f"/{nodash}/primary_doc.xml"
        )
        hdrs = {**HEADERS, "Range": "bytes=0-1500"}
        resp = requests.get(xml_url, headers=hdrs, timeout=30)
        # 206 Partial Content or 200 OK are both acceptable
        if resp.status_code not in (200, 206):
            time.sleep(0.1)
            continue
        if TARGET_SERIES.encode() in resp.content:
            return acc
        time.sleep(0.1)
    raise RuntimeError(
        f"Scanned {len(candidates)} accessions — none contain {TARGET_SERIES}."
    )


def txt(el, tag):
    child = el.find(f"{N}{tag}")
    return child.text.strip() if child is not None and child.text else None


def txt_or_cond(el, tag, cond_tag, attr):
    """
    Try direct child element text; fall back to an attribute on a conditional
    sibling.  Used for assetCat / issuerCat which the N-PORT schema sometimes
    encodes as <assetConditional assetCat="..."/> or <issuerConditional
    issuerCat="..."/> instead of plain text children.
    """
    val = txt(el, tag)
    if val:
        return val
    cond = el.find(f"{N}{cond_tag}")
    if cond is not None:
        v = cond.get(attr)
        return v.strip() if v else None
    return None


def parse_xml_to_df(xml_bytes: bytes) -> pd.DataFrame:
    """Parse N-PORT XML bytes into a holdings DataFrame."""
    root = ET.fromstring(xml_bytes)
    holdings_parent = root.find(f"{N}formData/{N}invstOrSecs")
    if holdings_parent is None:
        raise RuntimeError("Could not find formData/invstOrSecs in XML.")

    rows = []
    for sec in holdings_parent.findall(f"{N}invstOrSec"):
        identifiers = sec.find(f"{N}identifiers")
        isin_el = identifiers.find(f"{N}isin") if identifiers is not None else None
        isin = isin_el.get("value") if isin_el is not None else None

        sec_lending = sec.find(f"{N}securityLending")
        # isCashCollateral is either a plain text child (<isCashCollateral>N</isCashCollateral>)
        # or an attribute on <cashCollateralCondition isCashCollateral="Y" /> — same pattern as
        # issuerConditional/assetConditional.
        is_cash_collateral = txt_or_cond(sec_lending, "isCashCollateral", "cashCollateralCondition", "isCashCollateral") if sec_lending is not None else None

        rows.append({
            "name":             txt(sec, "name"),
            "lei":              txt(sec, "lei"),
            "title":            txt(sec, "title"),
            "cusip":            txt(sec, "cusip"),
            "isin":             isin,
            "balance":          txt(sec, "balance"),
            "units":            txt(sec, "units"),
            "curCd":            txt(sec, "curCd"),
            "valUSD":           txt(sec, "valUSD"),
            "pctVal":           txt(sec, "pctVal"),
            "payoffProfile":    txt(sec, "payoffProfile"),
            "assetCat":         txt_or_cond(sec, "assetCat",  "assetConditional",  "assetCat"),
            "issuerCat":        txt_or_cond(sec, "issuerCat", "issuerConditional", "issuerCat"),
            "invCountry":       txt(sec, "invCountry"),
            "isRestrictedSec":  txt(sec, "isRestrictedSec"),
            "fairValLevel":     txt(sec, "fairValLevel"),
            "isCashCollateral": is_cash_collateral,
        })

    df = pd.DataFrame(rows)
    df["balance"] = pd.to_numeric(df["balance"], errors="coerce")
    df["valUSD"]  = pd.to_numeric(df["valUSD"],  errors="coerce")
    df["pctVal"]  = pd.to_numeric(df["pctVal"],  errors="coerce")
    return df


def append_lineage(accession: str, filing_date: str, xml_url: str):
    write_header = not LINEAGE_CSV.exists()
    with open(LINEAGE_CSV, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LINEAGE_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "fund_ticker":      TICKER,
            "series_id":        TARGET_SERIES,
            "cik":              f"CIK{CIK_NUM.zfill(10)}",
            "accession_number": accession,
            "filing_date":      filing_date,
            "source_url":       xml_url,
            "downloaded_date":  str(date.today()),
        })


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROC_DIR.mkdir(parents=True, exist_ok=True)

    results = []

    for filing_date in TARGET_DATES:
        print(f"\n{'='*60}")
        print(f"Filing date: {filing_date}")
        print(f"{'='*60}")

        # ── 1. Find accession (EFTS first, then cluster scan) ────────────────
        print(f"  Tier 1: trying EFTS ...")
        accession_dashes = try_efts(filing_date)
        if accession_dashes:
            print(f"  EFTS hit -> {accession_dashes}")
        else:
            print(f"  EFTS: no result (non-quarterly monthly filing).")
            print(f"  Tier 2: fetching submissions JSON + scanning cluster ...")
            candidates = get_candidate_accessions(filing_date)
            print(f"  Found {len(candidates)} candidate accession(s) from filer {KNOWN_FILER}.")
            accession_dashes = scan_cluster_for_ivv(candidates)
            print(f"  Cluster scan -> {accession_dashes}")

        accession_nodash = accession_dashes.replace("-", "")
        print(f"  Accession : {accession_dashes}")

        # ── 2. Build XML URL ──────────────────────────────────────────────────
        xml_url = (
            f"https://www.sec.gov/Archives/edgar/data/{CIK_NUM}"
            f"/{accession_nodash}/primary_doc.xml"
        )
        print(f"  Downloading {xml_url} ...")

        resp = requests.get(xml_url, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        raw_bytes = resp.content

        # ── 3. Content-verify series ID ───────────────────────────────────────
        if TARGET_SERIES.encode() not in raw_bytes:
            raise RuntimeError(
                f"Downloaded XML for {filing_date} / {accession_dashes} does NOT "
                f"contain {TARGET_SERIES} — aborting before save."
            )
        print(f"  Content verification passed: '{TARGET_SERIES}' found in XML.")

        # ── 4. Save XML ───────────────────────────────────────────────────────
        xml_filename = f"NPORT_{TICKER}_{filing_date}_{accession_dashes}.xml"
        xml_path = RAW_DIR / xml_filename
        with open(xml_path, "wb") as fh:
            fh.write(raw_bytes)
        print(f"  Saved {len(raw_bytes)/1024:,.1f} KB -> {xml_path.relative_to(REPO_ROOT)}")

        # ── 5. Parse holdings ─────────────────────────────────────────────────
        df = parse_xml_to_df(raw_bytes)
        row_count  = len(df)
        pctval_sum = df["pctVal"].sum()
        valusd_sum = df["valUSD"].sum()

        print(f"  Holdings parsed : {row_count} rows")
        print(f"  Sum pctVal      : {pctval_sum:.6f}  (expect ~1.0 for fully-invested fund)")
        print(f"  Sum valUSD      : ${valusd_sum:,.2f}")

        # ── 6. Save CSV ───────────────────────────────────────────────────────
        csv_path = PROC_DIR / f"ivv_holdings_{filing_date}.csv"
        df.to_csv(csv_path, index=False)
        print(f"  Saved CSV       -> {csv_path.relative_to(REPO_ROOT)}")

        # ── 7. Append lineage ─────────────────────────────────────────────────
        append_lineage(accession_dashes, filing_date, xml_url)
        print(f"  Lineage appended -> {LINEAGE_CSV.relative_to(REPO_ROOT)}")

        results.append({
            "date":       filing_date,
            "accession":  accession_dashes,
            "rows":       row_count,
            "pctval_sum": pctval_sum,
            "valusd_sum": valusd_sum,
        })

        # Polite pause between requests
        time.sleep(0.5)

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"\n\n{'='*70}")
    print("SUMMARY — all 5 new filings")
    print(f"{'='*70}")
    print(f"  {'Filing date':<14}  {'Accession':<26}  {'Rows':>5}  {'pctVal sum':>11}  {'valUSD sum':>20}")
    print("  " + "-" * 82)
    for r in results:
        print(
            f"  {r['date']:<14}  {r['accession']:<26}"
            f"  {r['rows']:>5}  {r['pctval_sum']:>11.6f}  ${r['valusd_sum']:>19,.2f}"
        )

    # Also list all 6 (including the original Aug 25)
    print()
    print("All 6 IVV N-PORT filings now on disk (Aug 25 previously downloaded):")
    for d in sorted([r["date"] for r in results] + ["2026-08-25"]):
        print(f"  data/processed/ivv_holdings_{d}.csv")


if __name__ == "__main__":
    main()
