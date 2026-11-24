#!/usr/bin/env python3
"""
Module 5 — FWP (Free Writing Prospectus) lookup for each 424B2 deal.
Reads  data/processed/module5_documents.csv
Writes data/processed/module5_documents.csv  (adds fwp_accession, fwp_url,
       fwp_has_cusip, fwp_has_isin columns)
Survey only — no extractor yet.
"""
import html as _html
import os, re, time, json, urllib.request
from datetime import datetime
import pandas as pd
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

USER_AGENT = "Adwait Gaur adwaitgaur197@gmail.com"
PAUSE      = 0.11       # ~9 req/s, under SEC's 10 req/s cap

IN_CSV = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "processed",
                 "module5_documents.csv")
)

# CIK for each issuer (must match ACTIVE_ISSUERS in module5_documents.py)
ISSUER_CIK = {
    "Apple":             "0000320193",
    "Microsoft":         "0000789019",
    "Alphabet":          "0001652044",
    "Eli Lilly":         "0000059478",
    "Johnson & Johnson": "0000200406",
    "Meta":              "0001326801",
    "Broadcom":          "0001730168",
}

# CUSIP: 9 uppercase alphanumeric chars following "CUSIP" or "CUSIP/ISIN".
# Many FWP preparers space the CUSIP as "037833 FC9" (6 + space + 3), so
# we allow an optional single space after char 6 and after char 8.
CUSIP_RE = re.compile(
    r'CUSIP(?:[/\s]ISIN)?(?:\s+No\.?)?[\s:#]+'
    r'([0-9A-Z]{6}\s?[0-9A-Z]{2}\s?[0-9A-Z])\b'
)
# ISIN: standard "ISIN: US037833FC97"  OR  the slash-separated form used in
# the combined "CUSIP/ISIN: 037833 FC9 / US037833FC97" label.
ISIN_RE = re.compile(
    r'(?:\bISIN\b[\s:/#]*|CUSIP/ISIN[^/]*/\s*)'
    r'([A-Z]{2}[0-9A-Z]{10})\b'
)


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _headers():
    return {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}

def get_json(url):
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"    WARN json {url[:70]} -> {e}")
        return None

def get_html(url, nbytes=300_000):
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(nbytes).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"    WARN html {url[:70]} -> {e}")
        return ""

def html_to_text(raw):
    t = re.sub(r"<[^>]+>", " ", raw)
    t = _html.unescape(t)
    # Replace all Unicode space variants (non-breaking space, thin space, etc.)
    # with a plain ASCII space BEFORE the \s+ collapse, so the regex engine
    # doesn't silently miss CUSIP/ISIN labels separated by &nbsp;.
    t = re.sub(r"[\xa0   ​­]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


# ── EDGAR FWP helpers ─────────────────────────────────────────────────────────
def _extract_fwp(filings_dict, since):
    forms  = filings_dict.get("form", [])
    dates  = filings_dict.get("filingDate", [])
    accs   = filings_dict.get("accessionNumber", [])
    pdocs  = filings_dict.get("primaryDocument", [""] * len(forms))
    return [
        {"date": d, "accession": a, "primary_doc": p or ""}
        for f, d, a, p in zip(forms, dates, accs, pdocs)
        if f in ("FWP", "FWP/A") and d >= since
    ]

def fetch_all_fwp(cik, min_date, max_older=5):
    """Fetch all FWP / FWP/A filings for a CIK on or after min_date."""
    sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    time.sleep(PAUSE)
    sub = get_json(sub_url)
    if sub is None:
        return []

    all_fwp = _extract_fwp(sub.get("filings", {}).get("recent", {}), min_date)

    older_files = sub.get("filings", {}).get("files", [])
    fetched = 0
    for page_info in older_files:
        if fetched >= max_older:
            break
        page_name = page_info.get("name", "")
        if not page_name:
            continue
        time.sleep(PAUSE)
        page = get_json(f"https://data.sec.gov/submissions/{page_name}")
        if page is None:
            break
        all_fwp.extend(_extract_fwp(page, min_date))
        fetched += 1
        all_dates = page.get("filingDate", [])
        if all_dates and max(all_dates) < min_date:
            break

    return sorted(all_fwp, key=lambda x: x["date"])

def fwp_doc_url(cik, accession, primary_doc):
    cik_n   = cik.lstrip("0") or "0"
    acc_dir = accession.replace("-", "")
    if primary_doc:
        return (f"https://www.sec.gov/Archives/edgar/data"
                f"/{cik_n}/{acc_dir}/{primary_doc}")
    return (f"https://www.sec.gov/Archives/edgar/data"
            f"/{cik_n}/{acc_dir}/{accession}-index.htm")

def find_fwp_for_deal(fwp_list, deal_date_str, window=3):
    """
    Return the FWP with the minimum absolute date gap from deal_date.
    If multiple FWPs share the minimum gap, return the latest one (most filing).
    Returns None if nothing is within `window` days.
    """
    deal_dt  = datetime.fromisoformat(deal_date_str)
    best, best_gap = None, window + 1
    for fwp in fwp_list:
        gap = abs((datetime.fromisoformat(fwp["date"]) - deal_dt).days)
        if gap < best_gap or (best is not None and gap == best_gap
                              and fwp["date"] > best["date"]):
            best, best_gap = fwp, gap
    return best if best_gap <= window else None


# ── FWP content inspection ────────────────────────────────────────────────────
def inspect_fwp(url):
    """
    Fetch and plain-text the FWP.  Return dict with:
      text, has_cusip, has_isin, cusip_list, isin_list
    """
    time.sleep(PAUSE)
    raw  = get_html(url)
    text = html_to_text(raw) if raw else ""
    cusips = CUSIP_RE.findall(text)
    isins  = ISIN_RE.findall(text)
    return {
        "text":      text,
        "has_cusip": len(cusips) > 0,
        "has_isin":  len(isins)  > 0,
        "cusips":    cusips,
        "isins":     isins,
    }

def lines_around(text, pattern, n_before=15, n_after=15, line_width=90):
    """
    Find first match of pattern in text.  Wrap the surrounding chars into
    ~line_width-char lines and return n_before + 1 (match line) + n_after lines.
    """
    m = pattern.search(text)
    if m is None:
        return [], -1
    pos     = m.start()
    # grab a window of raw characters
    start   = max(0, pos - n_before * line_width)
    end     = min(len(text), pos + n_after * line_width)
    snippet = text[start:end]
    # wrap into display lines
    words   = snippet.split()
    lines, cur = [], []
    for w in words:
        cur.append(w)
        if len(" ".join(cur)) >= line_width:
            lines.append(" ".join(cur))
            cur = []
    if cur:
        lines.append(" ".join(cur))
    return lines, pos


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
print("=" * 65)
print("MODULE 5 — FWP LOOKUP")
print("=" * 65)

deals = pd.read_csv(IN_CSV)
min_date = deals["deal_date"].min()      # earliest deal → start of FWP search
print(f"Loaded {len(deals)} deals from {IN_CSV}")
print(f"Date range: {min_date} to {deals['deal_date'].max()}")
print(f"Issuers: {', '.join(sorted(deals['issuer'].unique()))}\n")

# ── Step 1: fetch FWP lists per issuer ───────────────────────────────────────
print("Fetching FWP filings per issuer …")
fwp_by_issuer = {}
for issuer in sorted(deals["issuer"].unique()):
    cik   = ISSUER_CIK[issuer]
    fwps  = fetch_all_fwp(cik, min_date)
    fwp_by_issuer[issuer] = fwps
    print(f"  {issuer:<22} CIK={cik}  FWP count since {min_date}: {len(fwps)}")

# ── Step 2: match each deal to its FWP and inspect ───────────────────────────
print("\nMatching deals to FWPs and inspecting …")

new_cols = {col: [] for col in ("fwp_accession", "fwp_url",
                                 "fwp_has_cusip", "fwp_has_isin")}
# Cache inspected FWPs (accession -> inspect result) to avoid re-fetching
# when the same FWP covers two same-day tranches.
inspect_cache: dict[str, dict] = {}

apple_nov2026_info = None   # captured for the drill-down print

for _, row in deals.iterrows():
    issuer     = row["issuer"]
    deal_date  = row["deal_date"]
    cik        = ISSUER_CIK[issuer]

    fwp = find_fwp_for_deal(fwp_by_issuer[issuer], deal_date, window=3)

    if fwp is None:
        print(f"  {issuer:<22} {deal_date}  no FWP within +/-3 days")
        new_cols["fwp_accession"].append("")
        new_cols["fwp_url"].append("")
        new_cols["fwp_has_cusip"].append(False)
        new_cols["fwp_has_isin"].append(False)
        continue

    url = fwp_doc_url(cik, fwp["accession"], fwp["primary_doc"])
    acc = fwp["accession"]

    if acc not in inspect_cache:
        inspect_cache[acc] = inspect_fwp(url)
    result = inspect_cache[acc]

    gap_days = abs((datetime.fromisoformat(fwp["date"]) -
                    datetime.fromisoformat(deal_date)).days)
    print(f"  {issuer:<22} {deal_date}  FWP {fwp['date']} "
          f"(gap={gap_days}d)  "
          f"CUSIP={result['has_cusip']}  ISIN={result['has_isin']}  "
          f"{fwp['primary_doc'][:35]}")

    new_cols["fwp_accession"].append(acc)
    new_cols["fwp_url"].append(url)
    new_cols["fwp_has_cusip"].append(result["has_cusip"])
    new_cols["fwp_has_isin"].append(result["has_isin"])

    # Capture Apple Nov 2026 for drill-down
    if issuer == "Apple" and deal_date.startswith("2026-11"):
        apple_nov2026_info = {"url": url, "result": result, "fwp_date": fwp["date"]}

# ── Step 3: update and save CSV ───────────────────────────────────────────────
for col, values in new_cols.items():
    deals[col] = values

deals.to_csv(IN_CSV, index=False)
print(f"\nSaved: {IN_CSV}  ({len(deals)} rows, now with fwp_* columns)\n")

# ── Step 4: per-issuer summary ────────────────────────────────────────────────
print("=" * 65)
print("SUMMARY: FWP coverage per issuer")
print("=" * 65)
for issuer, grp in deals.groupby("issuer"):
    n_deals    = len(grp)
    n_fwp      = int((grp["fwp_accession"] != "").sum())
    n_cusip    = int(grp["fwp_has_cusip"].sum())
    n_isin     = int(grp["fwp_has_isin"].sum())
    print(f"  {issuer:<22}  deals={n_deals}  with_fwp={n_fwp}"
          f"  fwp_has_cusip={n_cusip}  fwp_has_isin={n_isin}")

# ── Step 5: Apple November 2026 FWP drill-down ───────────────────────────────
print("\n" + "=" * 65)
print("DRILL-DOWN: Apple November 2026 deal — FWP CUSIP section")
print("=" * 65)

if apple_nov2026_info is None:
    print("  Apple Nov 2026 deal not found in results.")
else:
    print(f"\n  FWP URL:  {apple_nov2026_info['url']}")
    print(f"  FWP date: {apple_nov2026_info['fwp_date']}")
    text   = apple_nov2026_info["result"]["text"]
    cusips = apple_nov2026_info["result"]["cusips"]
    isins  = apple_nov2026_info["result"]["isins"]
    print(f"  CUSIPs found: {cusips}")
    print(f"  ISINs  found: {isins[:5]}{'...' if len(isins) > 5 else ''}")

    # Find first CUSIP occurrence and print ~30 wrapped lines around it
    display_lines, pos = lines_around(text, CUSIP_RE, n_before=12, n_after=18)
    if pos >= 0:
        print(f"\n  Plain-text context around first CUSIP (char {pos:,}):")
        print("  " + "-" * 61)
        for ln in display_lines:
            print(f"  {ln}")
        print("  " + "-" * 61)
    else:
        # Debug: show what's near the word "CUSIP" in the plain text
        idx = text.lower().find("cusip")
        if idx >= 0:
            snippet = text[max(0, idx - 20): idx + 80]
            print(f"  Word 'CUSIP' found at char {idx:,} but regex did not match.")
            print(f"  Raw chars: {repr(snippet)}")
        else:
            print("  [!] The word 'CUSIP' does not appear in first 300 KB of text.")
