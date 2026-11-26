#!/usr/bin/env python3
"""
Module 5 — test-set FWP extractor, run 2 (Meta, Broadcom).

Re-runs extraction with the improved extractor that adds grouped-layout
support, label aliases (Pricing/Closing Date), maturity truncation fix,
ISIN Number: support, and benchmark "and X%" pattern.

Reads the same raw HTML files downloaded for run 1 (no new downloads).
Run 1 output files (module5_test_tranches.csv, etc.) are NEVER overwritten.

Output: data/processed/module5_test_run2_tranches.csv
"""
import io, sys, re, csv, time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

# ── paths ─────────────────────────────────────────────────────────────────────
ROOT     = Path(__file__).parent.parent
DOCS_CSV = ROOT / "data" / "processed" / "module5_documents.csv"
OUT_CSV  = ROOT / "data" / "processed" / "module5_test_run2_tranches.csv"
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

extract_flat_labeled             = _ext.extract_flat_labeled
extract_grouped_labeled          = _ext.extract_grouped_labeled
lookup_day_count_from_prospectus = _ext.lookup_day_count_from_prospectus
html_to_flat                     = _ext.html_to_flat
load_html                        = _ext.load_html
extract_principal_from_html      = _ext.extract_principal_from_html
OUTPUT_COLUMNS                  = _ext.OUTPUT_COLUMNS


# ── processors ────────────────────────────────────────────────────────────────

def process_meta(docs):
    """
    Extract Meta bonds with improved extractor.

    Meta FWPs use the grouped-table layout: one row per field, all bonds listed
    with bond-label prefixes.  Try flat first (future-proofs against format
    changes), then fall back to grouped.  Look up day count from 424B2.
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
        prosp_dc  = lookup_day_count_from_prospectus("Meta", acc)
        bond_rows = extract_flat_labeled(flat, "Meta Platforms, Inc.",
                                         prospectus_dc=prosp_dc)
        if not bond_rows:
            bond_rows = extract_grouped_labeled(flat, "Meta Platforms, Inc.",
                                                prospectus_dc=prosp_dc)
        for r in bond_rows:
            r["fwp_accession"] = acc
        layout = "flat" if bond_rows and not any(
            r.get("bond_name", "").startswith("Note") for r in bond_rows) else "grouped"
        print(f"    {acc}  → {len(bond_rows)} bond(s)")
        rows.extend(bond_rows)
    return rows


def process_broadcom(docs):
    """
    Extract Broadcom bonds with improved extractor.

    Five of six Broadcom FWPs use flat-labeled layout; one (Jul 2024) uses
    grouped-table.  Try flat first; fall back to grouped if no bonds found.
    Pass day count from 424B2 prospectus (Broadcom FWPs omit the Day Count label).

    Principal amount: Broadcom FWPs list it in a grouped preamble section rather
    than in the term-sheet table, so the flat/grouped extractors cannot see it.
    After extraction, fill principal_amount from extract_principal_from_html:
    try the FWP first; fall back to the matching 424B2 if the FWP misses a bond.
    """
    import re as _re
    bc_docs = docs[docs["issuer"] == "Broadcom"].dropna(subset=["fwp_accession"])

    # Build FWP → 424B2 accession map (one 424B2 per FWP)
    fwp_to_b2 = {
        str(row["fwp_accession"]).strip(): str(row["accession_number"]).strip()
        for _, row in bc_docs.iterrows()
        if pd.notna(row.get("accession_number"))
    }

    deals = bc_docs.copy()
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
        flat     = html_to_flat(raw)
        prosp_dc = lookup_day_count_from_prospectus("Broadcom", acc)
        bond_rows = extract_flat_labeled(flat, "Broadcom Inc.",
                                         prospectus_dc=prosp_dc)
        if not bond_rows:
            bond_rows = extract_grouped_labeled(flat, "Broadcom Inc.",
                                                prospectus_dc=prosp_dc)
        for r in bond_rows:
            r["fwp_accession"] = acc

        # Build principal lookup from FWP preamble
        fwp_principal = extract_principal_from_html(raw)

        # Optionally load 424B2 as fallback
        b2_principal = {}
        b2_acc = fwp_to_b2.get(acc, "")
        if b2_acc:
            b2_raw = load_html("Broadcom", b2_acc)
            if b2_raw:
                b2_principal = extract_principal_from_html(b2_raw)

        # Fill principal_amount and principal_source for each bond
        for r in bond_rows:
            mat_yr_m = _re.search(r'(\d{4})', str(r.get("maturity_date", ""))[-8:])
            mat_yr   = int(mat_yr_m.group(1)) if mat_yr_m else None
            try:
                coupon = round(float(r.get("coupon_pct", "") or ""), 4)
            except (ValueError, TypeError):
                coupon = None

            if coupon is not None and mat_yr is not None:
                key = (coupon, mat_yr)
                if key in fwp_principal:
                    r["principal_amount"] = f'${fwp_principal[key]}'
                    r["principal_source"] = "fwp"
                elif key in b2_principal:
                    r["principal_amount"] = f'${b2_principal[key]}'
                    r["principal_source"] = "prospectus"
                else:
                    r["principal_source"] = "not_found"
            else:
                r["principal_source"] = "not_found"

        print(f"    {acc}  → {len(bond_rows)} bond(s)")
        rows.extend(bond_rows)
    return rows


# ── main ──────────────────────────────────────────────────────────────────────
docs = pd.read_csv(DOCS_CSV)
test = docs[docs["set"] == "test"].copy()

print("=" * 70)
print("Test-set FWP downloader — run 2 (Meta + Broadcom)")
print("=" * 70)

# Download FWPs (usually cached from run 1) and 424B2 prospectuses (for day count)
for issuer in ["Meta", "Broadcom"]:
    cik   = COMPANY_CIKS[issuer]
    sub   = test[test["issuer"] == issuer].dropna(subset=["fwp_accession"])
    print(f"\n{issuer} (CIK {cik}) — {len(sub)} FWP(s):")
    for _, row in sub.iterrows():
        download_fwp(issuer, row["fwp_accession"])
        time.sleep(0.3)
        # 424B2 prospectus (needed for day count lookup)
        b2_acc = str(row.get("accession_number", "")).strip()
        if b2_acc:
            download_fwp(issuer, b2_acc)
            time.sleep(0.3)

print("\n" + "=" * 70)
print("Test-set FWP extractor — run 2 (grouped layout + label aliases)")
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
