#!/usr/bin/env python3
"""
Module 5 — test-set FWP extractor (Meta, Broadcom).

Downloads FWP HTML files from EDGAR for the test set and processes them with
the UNCHANGED extract_flat_labeled function from extract_build_tranches.py.
No field-specific tuning is applied; this measures how well the existing
extractor generalises to new issuers.

Output: data/processed/module5_test_tranches.csv
"""
import io, sys, re, csv, time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

# ── paths ─────────────────────────────────────────────────────────────────────
ROOT     = Path(__file__).parent.parent
DOCS_CSV = ROOT / "data" / "processed" / "module5_documents.csv"
OUT_CSV  = ROOT / "data" / "processed" / "module5_test_tranches.csv"
RAW_DIR  = ROOT / "data" / "raw" / "prospectus"
LINEAGE  = ROOT / "data" / "raw" / "lineage_log.csv"

EDGAR_HEADERS = {
    "User-Agent": "FinanceDataPlatform research@example.com",
    "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# CIKs on EDGAR (10-digit, zero-padded)
COMPANY_CIKS = {
    "Meta":     "0001326801",   # Meta Platforms, Inc.
    "Broadcom": "0001730168",   # Broadcom Inc.
}


# ── EDGAR downloader ──────────────────────────────────────────────────────────

def _find_primary_doc(sub_data, acc):
    """Search a submissions JSON page for accession → primaryDocument name."""
    accs  = sub_data.get("accessionNumber",  sub_data.get("filings", {}).get("recent", {}).get("accessionNumber",  []))
    pdocs = sub_data.get("primaryDocument",  sub_data.get("filings", {}).get("recent", {}).get("primaryDocument", []))
    # handle both root-level (pagination page) and nested (submissions root)
    if not accs:
        recent = sub_data.get("filings", {}).get("recent", {})
        accs  = recent.get("accessionNumber", [])
        pdocs = recent.get("primaryDocument", [])
    for i, a in enumerate(accs):
        if a == acc and i < len(pdocs):
            return pdocs[i]
    return None


def download_fwp(issuer, acc):
    """Download one FWP HTML to data/raw/prospectus/{issuer}/{acc}.htm."""
    out_dir  = RAW_DIR / issuer
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{acc}.htm"

    if out_path.exists():
        print(f"    cached: {acc}")
        return True

    cik_padded = COMPANY_CIKS[issuer]
    cik_int    = str(int(cik_padded))
    acc_nd     = acc.replace("-", "")

    # 1. EDGAR submissions API
    doc_name = None
    sub_url  = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    try:
        r = requests.get(sub_url, headers=EDGAR_HEADERS, timeout=30)
        if r.status_code == 200:
            sub_data = r.json()
            doc_name = _find_primary_doc(sub_data, acc)
            if not doc_name:
                for pref in sub_data.get("filings", {}).get("files", []):
                    purl = f"https://data.sec.gov/submissions/{pref['name']}"
                    pr   = requests.get(purl, headers=EDGAR_HEADERS, timeout=30)
                    if pr.status_code == 200:
                        doc_name = _find_primary_doc(pr.json(), acc)
                    if doc_name:
                        break
                    time.sleep(0.3)
    except Exception as exc:
        print(f"    WARN submissions API: {exc}")

    # 2. Fallback: index HTM page
    if not doc_name:
        idx_url = (f"https://www.sec.gov/Archives/edgar/data/"
                   f"{cik_int}/{acc_nd}/{acc}-index.htm")
        try:
            ir = requests.get(idx_url, headers=EDGAR_HEADERS, timeout=30)
            if ir.status_code == 200:
                for lnk in re.findall(r'href="([^"]+\.htm)"', ir.text, re.I):
                    base = lnk.split("/")[-1]
                    if "index" not in base.lower():
                        doc_name = base
                        break
        except Exception as exc:
            print(f"    WARN index fallback: {exc}")

    if not doc_name:
        print(f"    ERROR: cannot locate primary document for {acc}")
        return False

    # 3. Download
    doc_url = (f"https://www.sec.gov/Archives/edgar/data/"
               f"{cik_int}/{acc_nd}/{doc_name}")
    try:
        dr = requests.get(doc_url, headers=EDGAR_HEADERS, timeout=60)
        dr.raise_for_status()
        out_path.write_text(dr.text, encoding="utf-8", errors="replace")
        print(f"    downloaded: {acc} → {doc_name}  ({len(dr.content):,} bytes)")
        _log_lineage(issuer, acc, doc_url, len(dr.content))
        return True
    except Exception as exc:
        print(f"    ERROR downloading {acc}: {exc}")
        return False


def _log_lineage(issuer, acc, url, size_bytes):
    fieldnames = ["run_date", "source", "accession", "url", "file_size_bytes"]
    row = {
        "run_date":        datetime.now().strftime("%Y-%m-%d"),
        "source":          issuer,
        "accession":       acc,
        "url":             url,
        "file_size_bytes": size_bytes,
    }
    write_hdr = not LINEAGE.exists()
    with LINEAGE.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_hdr:
            w.writeheader()
        w.writerow(row)


# ── import extraction functions (build re-extraction suppressed) ───────────────
sys.path.insert(0, str(Path(__file__).parent))
_captured = io.StringIO()
_saved    = sys.stdout
sys.stdout = _captured
try:
    import extract_build_tranches as _ext
finally:
    sys.stdout = _saved
_build_lines = _captured.getvalue().strip().splitlines()
_err_lines   = [l for l in _build_lines if "ERROR" in l or "WARN" in l]
print("Loaded extraction functions from extract_build_tranches.py")
if _err_lines:
    print("  Warnings/errors during build re-extraction:")
    for l in _err_lines:
        print(f"    {l}")

extract_flat_labeled = _ext.extract_flat_labeled
html_to_flat         = _ext.html_to_flat
load_html            = _ext.load_html
OUTPUT_COLUMNS       = _ext.OUTPUT_COLUMNS


# ── processors ────────────────────────────────────────────────────────────────

def process_meta(docs):
    """
    Apply extract_flat_labeled to each Meta FWP with no issuer-specific tuning.
    Meta issues USD bonds; the flat-labeled format matches Microsoft/Alphabet.
    """
    deals = docs[docs["issuer"] == "Meta"].dropna(subset=["fwp_accession"])
    rows, seen = [], set()
    print(f"  Meta: {len(deals)} FWP(s)")
    for _, deal in deals.iterrows():
        acc = deal["fwp_accession"]
        if acc in seen:
            continue
        seen.add(acc)
        raw = load_html("Meta", acc)
        if not raw:
            print(f"    SKIP — file not found: {acc}")
            continue
        flat      = html_to_flat(raw)
        bond_rows = extract_flat_labeled(flat, "Meta Platforms, Inc.")
        print(f"    {acc}  → {len(bond_rows)} bond(s)")
        rows.extend(bond_rows)
    return rows


def process_broadcom(docs):
    """
    Apply extract_flat_labeled to each Broadcom FWP with no issuer-specific tuning.
    """
    deals = docs[docs["issuer"] == "Broadcom"].dropna(subset=["fwp_accession"])
    rows, seen = [], set()
    print(f"  Broadcom: {len(deals)} FWP(s)")
    for _, deal in deals.iterrows():
        acc = deal["fwp_accession"]
        if acc in seen:
            continue
        seen.add(acc)
        raw = load_html("Broadcom", acc)
        if not raw:
            print(f"    SKIP — file not found: {acc}")
            continue
        flat      = html_to_flat(raw)
        bond_rows = extract_flat_labeled(flat, "Broadcom Inc.")
        print(f"    {acc}  → {len(bond_rows)} bond(s)")
        rows.extend(bond_rows)
    return rows


# ── main ──────────────────────────────────────────────────────────────────────
docs = pd.read_csv(DOCS_CSV)
test = docs[docs["set"] == "test"].copy()

print("=" * 70)
print("Test-set FWP downloader (Meta + Broadcom)")
print("=" * 70)

for issuer in ["Meta", "Broadcom"]:
    cik   = COMPANY_CIKS[issuer]
    sub   = test[test["issuer"] == issuer].dropna(subset=["fwp_accession"])
    print(f"\n{issuer} (CIK {cik}) — {len(sub)} accession(s):")
    for _, row in sub.iterrows():
        download_fwp(issuer, row["fwp_accession"])
        time.sleep(0.4)

print("\n" + "=" * 70)
print("Test-set FWP extractor (extract_flat_labeled, unchanged)")
print("=" * 70)

all_rows = []
all_rows.extend(process_meta(test))
all_rows.extend(process_broadcom(test))

out_df = pd.DataFrame(all_rows)
for col in OUTPUT_COLUMNS:
    if col not in out_df.columns:
        out_df[col] = ""
out_df = out_df[OUTPUT_COLUMNS]
out_df = out_df.fillna("")

OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
out_df.to_csv(OUT_CSV, index=False)
print(f"\nSaved {OUT_CSV}  ({len(out_df)} total bonds)")
