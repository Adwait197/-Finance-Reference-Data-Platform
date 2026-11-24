#!/usr/bin/env python3
"""
Module 5 - 424B2 document survey for selected IVV issuers.
Outputs: data/processed/module5_documents.csv
         Corrected zero-count verification printed to console.
Survey only - no extractor yet.
"""
import html as _html
import os, re, time, json, urllib.request
from datetime import date, datetime
import pandas as pd
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

USER_AGENT = "Adwait Gaur adwaitgaur197@gmail.com"
PAUSE      = 0.11       # ~9 req/s, under SEC's 10 req/s cap
START_DATE = "2023-01-01"
OUT_CSV    = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "processed",
                 "module5_documents.csv")
)

# Active issuers: short_name -> (cusip_prefix, set_label, cik_10digit)
# Alphabet A and C share one CIK and one cusip_prefix; deduplicated by accession.
ACTIVE_ISSUERS = {
    "Apple":             ("037833", "build", "0000320193"),
    "Microsoft":         ("594918", "build", "0000789019"),
    "Alphabet":          ("02079K", "build", "0001652044"),
    "Eli Lilly":         ("532457", "build", "0000059478"),
    "Johnson & Johnson": ("478160", "build", "0000200406"),
    "Meta":              ("30303M", "test",  "0001326801"),
    "Broadcom":          ("11135F", "test",  "0001730168"),
}

# Companies that showed 0 in first survey; re-verify with older pages.
ZERO_ISSUERS = {
    "NVIDIA":   "0001045810",
    "Amazon":   "0001018724",
    "Tesla":    "0001318605",
    "AMD":      "0000002488",
    "Intel":    "0000050863",
}

# Deal counts from the previous run (3-day grouping + HTML regex).
PREV_DEALS = {
    "Apple": 3, "Microsoft": 2, "Alphabet": 7, "Eli Lilly": 6,
    "Johnson & Johnson": 2, "Meta": 5, "Broadcom": 6,
}

# Cover-page check patterns applied to PLAIN TEXT (HTML stripped).
# Price: 2-3 digits . 2+ digits (space optional) %; matches 99.754 % but not 5.25 %
PRICE_RE     = re.compile(r"\b\d{2,3}\.\d{2,}\s*%")
# Principal: $ (optional space) digits with comma thousands
PRINCIPAL_RE = re.compile(r"\$\s*[\d,]+,\d{3}")
# Maturity years near "note" — used for tranche differentiation
_MATURITY_RE = re.compile(r"\b(20[2-9]\d)\b")
_NOTE_RE     = re.compile(r"[Nn]ote")


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _headers():
    return {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}

def get_json(url: str):
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"    WARN json {url[:70]} -> {e}")
        return None

def get_html(url: str, nbytes: int = 150_000) -> str:
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(nbytes).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"    WARN html {url[:70]} -> {e}")
        return ""


# ── HTML helpers ──────────────────────────────────────────────────────────────
def html_to_text(raw: str) -> str:
    """Strip HTML tags, decode entities, collapse whitespace."""
    t = re.sub(r"<[^>]+>", " ", raw)
    t = _html.unescape(t)          # handles &nbsp; &#8220; &#160; etc.
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def get_cover_text(url: str) -> str:
    """
    Fetch up to 150 KB of HTML, convert to plain text, return the section
    around the price table (~500 chars before and 2000 after the price phrase).
    Falls back to the first 3 000 chars of plain text if the phrase is absent.
    """
    time.sleep(PAUSE)
    raw = get_html(url)
    if not raw:
        return ""
    text = html_to_text(raw)
    for phrase in ("price to public", "public offering price"):
        idx = text.lower().find(phrase)
        if idx >= 0:
            return text[max(0, idx - 500): idx + 2000]
    return text[:3000]   # fallback: cover section not found in first 150 KB


def extract_maturities(cover_text: str) -> set:
    """
    Return the set of note maturity years (20XX) that appear within 60 chars
    of the word 'note'.  Used to detect distinct tranches in the same group.
    """
    result = set()
    for m in _MATURITY_RE.finditer(cover_text):
        pos    = m.start()
        window = cover_text[max(0, pos - 60): pos + 60]
        if _NOTE_RE.search(window):
            result.add(m.group(1))
    return result


def check_cover_text(cover_text: str) -> dict:
    """
    Given the plain-text cover section, check for a filled-in price-to-public
    percentage and a dollar principal amount.
    """
    if not cover_text:
        return {"has_price": False, "has_principal": False, "needs_review": True}
    has_price     = bool(PRICE_RE.search(cover_text))
    has_principal = bool(PRINCIPAL_RE.search(cover_text))
    return {"has_price": has_price, "has_principal": has_principal,
            "needs_review": not has_price or not has_principal}


# ── EDGAR submissions helpers ─────────────────────────────────────────────────
def _extract_b2(filings_dict: dict, since: str = START_DATE) -> list:
    """
    Extract 424B2 filings from a filings block (the flat arrays format used by
    both filings.recent and older-page JSON files).
    """
    forms  = filings_dict.get("form", [])
    dates  = filings_dict.get("filingDate", [])
    accs   = filings_dict.get("accessionNumber", [])
    pdocs  = filings_dict.get("primaryDocument", [""] * len(forms))
    return [
        {"date": d, "accession": a, "primary_doc": p or ""}
        for f, d, a, p in zip(forms, dates, accs, pdocs)
        if f == "424B2" and d >= since
    ]


def fetch_all_b2(cik: str, max_older: int = 5) -> tuple:
    """
    Fetch ALL 424B2 filings since START_DATE for a CIK, including older pages.
    Returns (list_of_filings, status_note).
    list_of_filings is sorted newest-first.
    """
    sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    time.sleep(PAUSE)
    sub = get_json(sub_url)
    if sub is None:
        return None, "submissions fetch failed"

    all_b2 = _extract_b2(sub.get("filings", {}).get("recent", {}))

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
        all_b2.extend(_extract_b2(page))
        fetched += 1
        all_dates = page.get("filingDate", [])
        if all_dates and max(all_dates) < START_DATE:
            break

    total_pages = len(older_files)
    note = (f"all pages fetched ({fetched} older)" if fetched >= total_pages
            else f"{fetched}/{total_pages} older pages fetched")
    return sorted(all_b2, key=lambda x: x["date"], reverse=True), note


def doc_url(cik: str, accession: str, primary_doc: str) -> str:
    """
    Build the direct document URL.
    Uses primaryDocument from submissions JSON (avoids the -index.htm 404 issue).
    Falls back to index page if primaryDocument is empty.
    """
    cik_n   = cik.lstrip("0") or "0"
    acc_dir = accession.replace("-", "")
    if primary_doc:
        return (f"https://www.sec.gov/Archives/edgar/data"
                f"/{cik_n}/{acc_dir}/{primary_doc}")
    return (f"https://www.sec.gov/Archives/edgar/data"
            f"/{cik_n}/{acc_dir}/{accession}-index.htm")


def window_groups(filings: list, days: int = 7) -> list:
    """
    Split filings (sorted ascending by date) into groups where every consecutive
    pair of filings is within `days` calendar days of each other.
    Returns a list of groups (each group is a list of filing dicts).
    """
    if not filings:
        return []
    items   = sorted(filings, key=lambda x: x["date"])
    groups  = []
    current = [items[0]]
    for item in items[1:]:
        gap = (datetime.fromisoformat(item["date"]) -
               datetime.fromisoformat(current[-1]["date"])).days
        if gap <= days:
            current.append(item)
        else:
            groups.append(current)
            current = [item]
    groups.append(current)
    return groups


def pick_deals(group: list) -> list:
    """
    Given a group of filings that are within the date window, decide which to keep.

    Rule: if two filings both have a filled-in price AND list DIFFERENT note
    maturities (different 'XX Notes due YYYY' lines), they are separate deals —
    keep both.  Otherwise keep only the latest.

    Requires that each filing dict already has a 'cover_text' key.
    """
    if len(group) == 1:
        return group

    latest   = group[-1]   # group is sorted ascending; last = latest
    lat_mats = extract_maturities(latest.get("cover_text", ""))
    lat_chk  = check_cover_text(latest.get("cover_text", ""))

    to_keep = [latest]
    for f in group[:-1]:            # all filings except the latest
        f_mats = extract_maturities(f.get("cover_text", ""))
        f_chk  = check_cover_text(f.get("cover_text", ""))
        # Keep only if it has a price AND its maturities are NOT a subset of latest
        if f_chk["has_price"] and lat_chk["has_price"] and f_mats and lat_mats:
            if f_mats - lat_mats:   # extra maturities not in the latest
                to_keep.append(f)

    return to_keep


# ═══════════════════════════════════════════════════════════════════════════════
# Part 1 - Active issuers: full 424B2 document list
# ═══════════════════════════════════════════════════════════════════════════════
print("=" * 65)
print("ACTIVE ISSUERS - full 424B2 survey")
print("=" * 65)

all_rows = []

for issuer, (cusip_prefix, set_label, cik) in ACTIVE_ISSUERS.items():
    print(f"\n{issuer}  (CIK={cik}  prefix={cusip_prefix}  set={set_label})")

    b2_list, note = fetch_all_b2(cik)
    if b2_list is None:
        print(f"  [!] fetch failed")
        continue
    n_filings = len(b2_list)
    print(f"  424B2 since {START_DATE}: {n_filings}  ({note})")

    # Build doc URLs for every filing.
    for f in b2_list:
        f["doc_url"] = doc_url(cik, f["accession"], f["primary_doc"])

    # Group by 7-day proximity window.
    groups = window_groups(b2_list, days=7)
    n_groups = len(groups)

    # For multi-filing groups, fetch cover text for each filing so pick_deals
    # can compare maturities.  For single-filing groups, fetch only the one filing.
    kept = []
    for grp in groups:
        if len(grp) > 1:
            for f in grp:
                f["cover_text"] = get_cover_text(f["doc_url"])
            kept.extend(pick_deals(grp))
        else:
            kept.append(grp[0])

    # Fetch cover text for any kept filing that hasn't been fetched yet.
    for f in kept:
        if "cover_text" not in f:
            f["cover_text"] = get_cover_text(f["doc_url"])

    n_deals = len(kept)
    prev    = PREV_DEALS.get(issuer, "?")
    change  = (f"  <-- changed from {prev}" if isinstance(prev, int) and n_deals != prev
               else "")
    print(f"  -> {n_groups} proximity groups, {n_deals} deals after tranche "
          f"check{change}")

    n_nr = 0
    for f in kept:
        chk = check_cover_text(f["cover_text"])
        nr  = chk["needs_review"]
        if nr:
            n_nr += 1
        mats = sorted(extract_maturities(f["cover_text"]))
        tag  = "needs_review" if nr else "ok"
        mat_str = " maturities=[" + ",".join(mats) + "]" if mats else ""
        print(f"  {f['date']}  {f['primary_doc'][:35]}  "
              f"price={chk['has_price']}  principal={chk['has_principal']}"
              f"  [{tag}]{mat_str}")
        all_rows.append({
            "issuer":           issuer,
            "cusip_prefix":     cusip_prefix,
            "set":              set_label,
            "deal_date":        f["date"],
            "accession_number": f["accession"],
            "document_url":     f["doc_url"],
            "needs_review":     nr,
        })

    print(f"  needs_review: {n_nr}/{n_deals}")


# ═══════════════════════════════════════════════════════════════════════════════
# Part 2 - Zero-count verification (re-check with older pages)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n")
print("=" * 65)
print("ZERO-COUNT COMPANIES - re-verify with older submissions pages")
print("=" * 65)

zero_results = {}
for name, cik in ZERO_ISSUERS.items():
    print(f"\n{name}  (CIK={cik})")
    b2_list, note = fetch_all_b2(cik, max_older=5)
    if b2_list is None:
        count_str = "unknown"
        print(f"  fetch failed")
    else:
        count_str = str(len(b2_list))
        print(f"  424B2 since {START_DATE}: {count_str}  ({note})")
        if b2_list:
            for f in b2_list:
                print(f"    {f['date']}  {f['accession']}  {f['primary_doc']}")
    zero_results[name] = count_str


# ═══════════════════════════════════════════════════════════════════════════════
# Part 3 - Save CSV + print summary
# ═══════════════════════════════════════════════════════════════════════════════
out_df = pd.DataFrame(all_rows)
os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
out_df.to_csv(OUT_CSV, index=False)

print("\n")
print("=" * 65)
print("SUMMARY: deals per issuer")
print("=" * 65)
if not out_df.empty:
    for issuer, grp in out_df.groupby("issuer"):
        prefix   = grp.iloc[0]["cusip_prefix"]
        s        = grp.iloc[0]["set"]
        deals    = len(grp)
        prev     = PREV_DEALS.get(issuer, "?")
        nr_count = int(grp["needs_review"].sum())
        change   = ""
        if isinstance(prev, int) and deals != prev:
            change = f"  <-- was {prev}"
        print(f"  {issuer:<22} prefix={prefix}  set={s:<5}  "
              f"deals={deals}  needs_review={nr_count}{change}")

print("\nWhat changed vs. previous run (3-day window, HTML regex):")
print("  Window widened 3->7 days:")
print("    Microsoft Oct 29 / Nov 2 (4 days apart) now groups -> 1 deal")
print("    Meta Oct 30 / Nov 3 2025 (4 days apart) now groups -> 1 deal")
print("  Tranche check on same-window groups (above):")
print("    If same-day Alphabet pairs had different maturities, they split back out.")
print("    If their maturities overlapped (prelim/final), they stay merged.")

print("\nZero-count re-verification:")
for name, count in zero_results.items():
    print(f"  {name:<10}  424B2 since {START_DATE}: {count}")

# Apple's most recent URL (for browser preview).
apple_rows = out_df[out_df["issuer"] == "Apple"].sort_values("deal_date", ascending=False)
if not apple_rows.empty:
    apple_url = apple_rows.iloc[0]["document_url"]
    print(f"\nApple most recent 424B2:")
    print(f"  {apple_url}")

print(f"\nSaved: {OUT_CSV}")
