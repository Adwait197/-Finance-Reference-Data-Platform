"""
download_nport.py

Finds the most recent N-PORT-P filing for IVV (series S000004310) using
the EDGAR EFTS full-text search API — which indexes filing XML content, so
a match on the series ID is content-based proof, not just a metadata guess.
Downloads the confirmed filing XML to data/raw/ and writes a lineage record.
"""
import csv
import os
import requests
from datetime import date

HEADERS      = {"User-Agent": "Adwait Gaur adwaitgaur197@gmail.com"}
CIK_NUM      = "1100663"
TARGET_SERIES = "S000004310"
TICKER       = "IVV"
RAW_DIR      = "data/raw"
LINEAGE_CSV  = os.path.join(RAW_DIR, "lineage_log.csv")
LINEAGE_FIELDS = [
    "fund_ticker", "series_id", "cik", "accession_number",
    "filing_date", "source_url", "downloaded_date",
]

os.makedirs(RAW_DIR, exist_ok=True)

# ── Step 1: Use EFTS full-text search to find NPORT-P filings whose XML
#    content contains the series ID.  This is content-based verification —
#    EFTS indexed the actual filing XML, so a hit confirms the series match.
print("Querying EDGAR full-text search for series", TARGET_SERIES, "...")
efts_url = (
    "https://efts.sec.gov/LATEST/search-index"
    f"?q=%22{TARGET_SERIES}%22&forms=NPORT-P"
    "&dateRange=custom&startdt=2024-01-01"
    "&_source=adsh,file_date,form,period_ending,xsl"
)
r = requests.get(efts_url, headers=HEADERS)
r.raise_for_status()
hits = r.json()["hits"]["hits"]
print(f"  EFTS returned {len(hits)} matching filing(s).")

# Keep only original NPORT-P (drop amendments: NPORT-P/A)
originals = [
    h["_source"] for h in hits
    if h["_source"].get("form") == "NPORT-P"
]
if not originals:
    raise RuntimeError("No original NPORT-P filings found for this series in EFTS.")

# Most recent by file_date
best = sorted(originals, key=lambda s: s["file_date"], reverse=True)[0]
accession_dashes  = best["adsh"]          # e.g. "0002071691-26-019760"
accession_nodash  = accession_dashes.replace("-", "")
filing_date       = best["file_date"]
xsl               = best.get("xsl", "xslFormNPORT-P_X01")

xml_url = (
    f"https://www.sec.gov/Archives/edgar/data/{CIK_NUM}"
    f"/{accession_nodash}/{xsl}/primary_doc.xml"
)

print(f"\nMost recent NPORT-P confirmed by EFTS:")
print(f"  Accession : {accession_dashes}")
print(f"  Filed     : {filing_date}")
print(f"  Period    : {best.get('period_ending', 'unknown')}")
print(f"  URL       : {xml_url}")

# ── Step 2: Download the full XML
print("\nDownloading XML ...")
resp = requests.get(xml_url, headers=HEADERS)
resp.raise_for_status()
raw_bytes = resp.content

# ── Step 3: Verify the series ID actually appears in the downloaded content
if TARGET_SERIES.encode() not in raw_bytes:
    raise RuntimeError(
        f"Downloaded XML does not contain {TARGET_SERIES} — "
        "filing identity mismatch; aborting before save."
    )
print(f"  Verification passed: '{TARGET_SERIES}' found in downloaded content.")

# ── Step 4: Save exactly as received — no modification
filename = f"NPORT_{TICKER}_{filing_date}_{accession_dashes}.xml"
filepath = os.path.join(RAW_DIR, filename)
with open(filepath, "wb") as fh:
    fh.write(raw_bytes)
print(f"  Saved {len(raw_bytes) / 1024:,.1f} KB → {filepath}")

# ── Step 5: Append lineage record
write_header = not os.path.exists(LINEAGE_CSV)
with open(LINEAGE_CSV, "a", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=LINEAGE_FIELDS)
    if write_header:
        writer.writeheader()
    writer.writerow({
        "fund_ticker":      TICKER,
        "series_id":        TARGET_SERIES,
        "cik":              f"CIK{CIK_NUM.zfill(10)}",
        "accession_number": accession_dashes,
        "filing_date":      filing_date,
        "source_url":       xml_url,
        "downloaded_date":  str(date.today()),
    })
print(f"  Lineage record written → {LINEAGE_CSV}")
