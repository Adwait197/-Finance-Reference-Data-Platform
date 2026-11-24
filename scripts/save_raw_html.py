#!/usr/bin/env python3
"""
Archive raw HTML for every Apple 424B2 and FWP filing used in Module 5.
Saves:  data/raw/prospectus/<issuer>/<accession>.htm
Writes: data/raw/lineage_log.csv  (appends; skips rows already logged)

Run once to populate the archive; re-running is safe (idempotent).
"""
import csv, json, os, re, time, urllib.request
from datetime import date
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

USER_AGENT = "Adwait Gaur adwaitgaur197@gmail.com"
PAUSE      = 0.11
TODAY      = date.today().isoformat()

DOCS_CSV = Path(__file__).parent.parent / "data" / "processed" / "module5_documents.csv"
RAW_DIR  = Path(__file__).parent.parent / "data" / "raw" / "prospectus"
LOG_CSV  = Path(__file__).parent.parent / "data" / "raw" / "lineage_log.csv"

LOG_COLS = ["issuer", "form_type", "accession",
            "filing_date", "source_url", "downloaded_date"]

# Column rename map for migrating old Module 1 lineage_log schema
_MIGRATE = {
    "accession_number": "accession",
    "fund_ticker":      "issuer",
}

ISSUER_CIK = {
    "Apple": "0000320193",
}


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _headers():
    return {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}

def fetch_bytes(url: str, nbytes: int = 1_000_000) -> bytes:
    time.sleep(PAUSE)
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(nbytes)

def fetch_json(url: str) -> dict:
    time.sleep(PAUSE)
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


# ── EDGAR filing-date lookup ──────────────────────────────────────────────────
def _build_acc_date_map(cik: str, form_types: tuple) -> dict:
    """
    Return {accession: filing_date} for all filings of the given form types.
    Fetches the submissions JSON (recent + older pages).
    """
    sub    = fetch_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    result = {}

    def _parse(filings_dict: dict):
        forms = filings_dict.get("form",          [])
        dates = filings_dict.get("filingDate",    [])
        accs  = filings_dict.get("accessionNumber", [])
        for f, d, a in zip(forms, dates, accs):
            if f in form_types:
                result[a] = d

    _parse(sub.get("filings", {}).get("recent", {}))
    for page_info in sub.get("filings", {}).get("files", []):
        name = page_info.get("name", "")
        if not name:
            continue
        page = fetch_json(f"https://data.sec.gov/submissions/{name}")
        _parse(page)
        dates_on_page = page.get("filingDate", [])
        if dates_on_page and min(dates_on_page) < "2022-01-01":
            break
    return result


# ── lineage log helpers ───────────────────────────────────────────────────────
def _load_and_migrate_log() -> pd.DataFrame:
    """
    Read lineage_log.csv (if it exists) and migrate old column names to the
    current schema.  Returns an empty DataFrame with LOG_COLS if file missing.
    """
    if not LOG_CSV.exists():
        return pd.DataFrame(columns=LOG_COLS)
    df = pd.read_csv(LOG_CSV, dtype=str).fillna("")
    # Rename any old columns to the new schema
    df = df.rename(columns=_MIGRATE)
    # Add columns that did not exist in the old schema
    for col in LOG_COLS:
        if col not in df.columns:
            df[col] = ""
    # Drop columns that are no longer in the schema (Module 1 extras)
    df = df[LOG_COLS]
    return df

def _load_logged_accessions() -> set:
    df = _load_and_migrate_log()
    return set(df["accession"].astype(str))

def _write_log(df: pd.DataFrame):
    df.to_csv(LOG_CSV, index=False)


# ── main ──────────────────────────────────────────────────────────────────────
RAW_DIR.mkdir(parents=True, exist_ok=True)
LOG_CSV.parent.mkdir(parents=True, exist_ok=True)

docs   = pd.read_csv(DOCS_CSV)
logged = _load_logged_accessions()
new_log_rows: list[dict] = []

for issuer in sorted(docs["issuer"].unique()):
    cik = ISSUER_CIK.get(issuer)
    if cik is None:
        continue

    issuer_dir = RAW_DIR / issuer
    issuer_dir.mkdir(parents=True, exist_ok=True)

    # Build a date map covering both 424B2 and FWP filings
    print(f"\n{issuer}  (CIK {cik})")
    print("  Fetching EDGAR filing-date index …")
    date_map = _build_acc_date_map(cik, ("424B2", "FWP", "FWP/A"))

    apple_rows = docs[docs["issuer"] == issuer].copy()

    # Collect (accession, url, form_type, deal_date) pairs to download
    targets: list[dict] = []
    for _, row in apple_rows.iterrows():
        targets.append({
            "accession":  row["accession_number"],
            "url":        row["document_url"],
            "form_type":  "424B2",
            "deal_date":  row["deal_date"],
        })
        if pd.notna(row.get("fwp_accession")) and row["fwp_accession"] != "":
            targets.append({
                "accession": row["fwp_accession"],
                "url":       row["fwp_url"],
                "form_type": "FWP",
                "deal_date": row["deal_date"],
            })

    for t in targets:
        acc      = t["accession"]
        url      = t["url"]
        ftype    = t["form_type"]
        # Prefer exact EDGAR date; fall back to deal_date (always within 1 day)
        fdate    = date_map.get(acc, t["deal_date"])
        out_path = issuer_dir / f"{acc}.htm"

        status = ""
        if out_path.exists():
            status = "already on disk"
        else:
            try:
                raw = fetch_bytes(url)
                out_path.write_bytes(raw)
                status = f"downloaded {len(raw):,} bytes"
            except Exception as e:
                status = f"FAILED: {e}"
                print(f"  [!] {ftype} {acc}: {status}")
                continue

        print(f"  {ftype}  {acc}  {fdate}  {status}")

        if acc not in logged:
            new_log_rows.append({
                "issuer":          issuer,
                "form_type":       ftype,
                "accession":       acc,
                "filing_date":     fdate,
                "source_url":      url,
                "downloaded_date": TODAY,
            })
            logged.add(acc)

if new_log_rows:
    existing = _load_and_migrate_log()
    combined = pd.concat(
        [existing, pd.DataFrame(new_log_rows, columns=LOG_COLS)],
        ignore_index=True,
    )
    _write_log(combined)
    print(f"\nWrote {len(combined)} total rows to {LOG_CSV} "
          f"({len(new_log_rows)} new)")
else:
    print("\nNo new rows (all accessions already in log).")

print("\nDone.")
