#!/usr/bin/env python3
"""
Module 5 — Apple FWP term-sheet extractor (Apple only, 3 deals).
Reads Apple FWP URLs from data/processed/module5_documents.csv.
Splits each FWP into one block per bond using underlined (<u>) headings.
Extracts structured fields from each block.
Outputs:
  data/processed/module5_apple_tranches.csv    — one row per bond
  data/processed/module5_blocks/<id>.txt       — raw text block per bond
Survey only — no validation yet.
"""
import html as _html
import os, re, time, json, urllib.request
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

USER_AGENT = "Adwait Gaur adwaitgaur197@gmail.com"
PAUSE      = 0.11

DOCS_CSV   = Path(__file__).parent.parent / "data" / "processed" / "module5_documents.csv"
OUT_CSV    = Path(__file__).parent.parent / "data" / "processed" / "module5_apple_tranches.csv"
BLOCKS_DIR = Path(__file__).parent.parent / "data" / "processed" / "module5_blocks"
RAW_DIR    = Path(__file__).parent.parent / "data" / "raw" / "prospectus"

OUTPUT_COLUMNS = [
    "issuer", "bond_name", "currency", "principal_amount",
    "rate_type", "coupon_pct",
    "floating_benchmark", "floating_spread_pct",
    "maturity_date", "price_to_public_pct",
    "benchmark_treasury_yield_pct", "spread_to_treasury_bps", "yield_pct",
    "trade_date", "settlement_date", "day_count",
    "rating_moodys", "rating_sp",
    "cusip", "isin",
    "missing_fields",
]

# Pattern that terminates any label's value: the start of the NEXT label.
_TERM = (
    r"(?=\s*(?:"
    r"Issuer:|Principal Amount:|Maturity(?:\s+Date)?:|"
    r"Interest(?:\s+Payment(?:\s+Dates?)?)?:|"
    r"Price to(?:\s+the)?\s+Public:|"
    r"Benchmark Treasury Yield:|Benchmark Treasury:|"
    r"Spread to(?:\s+Benchmark)?\s+Treasury:|"
    r"Yield(?:\s+to\s+Maturity)?:|"
    r"Trade Date:|Settlement Date:|Denominations:|"
    r"Ratings:\*?|Net Proceeds:|CUSIP|Underwriters:|"
    r"Day Count Convention:|No Redemption:|Calculation Agent:|"
    r"Par (?:call|Call):|"
    r"\Z"
    r"))"
)

# CUSIP: 9 chars, with optional single space at positions 6 and/or 8
_CUSIP_RE = re.compile(
    r"CUSIP(?:/ISIN)?(?:\s+No(?:mber)?\.?)?[\s:#]+"
    r"([0-9A-Z]{6}\s?[0-9A-Z]{2}\s?[0-9A-Z])\b",
    re.IGNORECASE,
)
# ISIN: slash-separated form "CUSIP/ISIN: xxx / US..." or standalone "ISIN: US..."
_ISIN_RE = re.compile(
    r"(?:\bISIN\b[\s:/#]*|CUSIP/ISIN[^/]*/\s*)"
    r"([A-Z]{2}[0-9A-Z]{10})\b"
)
# Bond heading patterns in <u> tags
_HEADING_RE = re.compile(
    r"\d+\.\d+%\s+Notes?\s+due\s+\d{4}|"
    r"Floating\s+Rate\s+Notes?\s+due\s+\d{4}|"
    r"Zero\s+Coupon\s+Notes?\s+due\s+\d{4}",
    re.IGNORECASE,
)


# ── HTTP ──────────────────────────────────────────────────────────────────────
def _headers():
    return {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}

def load_html(issuer: str, accession: str, url: str, nbytes: int = 600_000) -> str:
    """
    Return the raw HTML for a filing, preferring the local archive copy.
    Local path: data/raw/prospectus/<issuer>/<accession>.htm
    Falls back to live download if the file is not found locally.
    """
    local = RAW_DIR / issuer / f"{accession}.htm"
    if local.exists():
        return local.read_bytes()[:nbytes].decode("utf-8", errors="replace")
    print(f"  WARN local file not found — downloading {url[:70]}")
    time.sleep(PAUSE)
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(nbytes).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARN fetch failed: {e}")
        return ""


# ── HTML helpers ──────────────────────────────────────────────────────────────
def _unescape_normalize(t: str) -> str:
    t = _html.unescape(t)
    t = re.sub(r"[\xa0\u2009\u2002\u2003\u200b\u00ad]", " ", t)
    return t

def html_to_flat(raw: str) -> str:
    """Strip all tags, unescape entities, collapse to single space-separated string."""
    t = re.sub(r"<[^>]+>", " ", raw)
    t = _unescape_normalize(t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()

def html_to_lines(raw: str) -> str:
    """Replace block-level tags with newlines for readable block output."""
    t = re.sub(r"<(?:p|div|br|li|tr|h[1-6]|table|td|th)[^>]*/?>",
               "\n", raw, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", "", t)
    t = _unescape_normalize(t)
    t = re.sub(r"[^\S\n]+", " ", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ── Section splitter ──────────────────────────────────────────────────────────
def split_into_bond_blocks(html: str) -> list:
    """
    Find all <u>...</u> tags whose inner text matches a bond-name pattern.
    Split the HTML at those positions.
    Returns list of dicts: {heading (str), block_html (str)}
    """
    u_re = re.compile(r"<u[^>]*>(.*?)</u>", re.DOTALL | re.IGNORECASE)

    section_starts = []
    for m in u_re.finditer(html):
        inner = re.sub(r"<[^>]+>", "", m.group(1))
        inner = _unescape_normalize(inner).strip()
        inner = re.sub(r"\s+", " ", inner)
        if _HEADING_RE.search(inner):
            section_starts.append({"start": m.start(), "end": m.end(),
                                   "heading": inner})

    if not section_starts:
        return []

    blocks = []
    for i, sec in enumerate(section_starts):
        next_start = (section_starts[i + 1]["start"]
                      if i + 1 < len(section_starts) else len(html))
        blocks.append({"heading":    sec["heading"],
                       "block_html": html[sec["end"]:next_start]})
    return blocks


# ── Field extractor ───────────────────────────────────────────────────────────
def extract_fields(text: str, heading: str) -> tuple:
    """
    Extract all specified fields from the flat plain text of one bond block.
    Returns (fields_dict, missing_fields_list).
    Never guesses; missing fields get '' and are added to missing_fields.
    """
    fields: dict = {}
    missing: list = []

    def find_raw(label_re: str) -> str | None:
        """Return raw value text after label up to the next field label (or end)."""
        m = re.search(rf"(?i){label_re}\s*(.*?){_TERM}", text, re.DOTALL)
        return m.group(1).strip() if m else None

    def req(key: str, val) -> None:
        """Required field: record or mark missing."""
        fields[key] = val if val else ""
        if not val:
            missing.append(key)

    def opt(key: str, val) -> None:
        fields[key] = val if val else ""

    # ── from heading ──────────────────────────────────────────────────────────
    fields["bond_name"] = heading
    if re.search(r"[Ff]loating\s+[Rr]ate", heading):
        fields["rate_type"]  = "Floating"
        fields["coupon_pct"] = ""
    else:
        fields["rate_type"] = "Fixed"
        m = re.search(r"(\d+\.\d+)%", heading)
        fields["coupon_pct"] = m.group(1) if m else ""
        if not fields["coupon_pct"]:
            missing.append("coupon_pct")

    # ── label: value fields ───────────────────────────────────────────────────
    # Issuer
    issuer_raw = find_raw(r"Issuer:")
    req("issuer", issuer_raw)

    # Principal Amount (keep the full "$X,XXX,XXX,XXX" string)
    principal_raw = find_raw(r"Principal Amount:")
    req("principal_amount", principal_raw)

    # Currency (derived from principal)
    if principal_raw:
        if re.search(r"C\$|C \$|CAD", principal_raw):
            fields["currency"] = "CAD"
        elif re.search(r"€|EUR", principal_raw):
            fields["currency"] = "EUR"
        else:
            fields["currency"] = "USD"
    else:
        fields["currency"] = ""

    # Maturity date
    mat_raw = find_raw(r"Maturity(?:\s+Date)?:")
    mat_m = re.search(r"([A-Z][a-z]+ \d+, \d{4})", mat_raw) if mat_raw else None
    req("maturity_date", mat_m.group(1) if mat_m else None)

    # Price to Public (extract numeric part only)
    price_raw = find_raw(r"Price to(?:\s+the)?\s+Public[^:]*:")
    if price_raw is None:
        # Also try without the colon (sometimes the label is "Price to Public (1)")
        m = re.search(
            r"Price to(?:\s+the)?\s+Public[^0-9]*(\d+\.\d+)\s*%", text, re.IGNORECASE
        )
        price_val = m.group(1) if m else None
    else:
        m = re.search(r"(\d+\.\d+)", price_raw)
        price_val = m.group(1) if m else None
    req("price_to_public_pct", price_val)

    # Trade date
    trade_raw = find_raw(r"Trade Date:")
    trade_m = re.search(r"([A-Z][a-z]+ \d+, \d{4})", trade_raw) if trade_raw else None
    req("trade_date", trade_m.group(1) if trade_m else None)

    # Settlement date
    sett_raw = find_raw(r"Settlement Date:")
    sett_m = re.search(r"([A-Z][a-z]+ \d+, \d{4})", sett_raw) if sett_raw else None
    req("settlement_date", sett_m.group(1) if sett_m else None)

    # Day count convention — strip trailing footnote numbers/noise
    dc_raw = find_raw(r"Day Count Convention:")
    if dc_raw:
        dc_m = re.search(r"(Actual/\d+|30/\d+|ACT/\d+)", dc_raw, re.IGNORECASE)
        dc_val = dc_m.group(1) if dc_m else dc_raw.split()[0]
    else:
        dc_val = None
    opt("day_count", dc_val)

    # Ratings — extract both Moody's and S&P from the ratings block
    ratings_raw = find_raw(r"Ratings:\*?")
    if ratings_raw:
        # Moody's: Aaa, Aa1–Aa3, A1–A3, Baa1–Baa3 … followed by "(stable/…) by Moody"
        mm = re.search(
            r"(Aa?a|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]|Caa[123]|Ca|C)"
            r"\s*\([^)]+\)",
            ratings_raw,
        )
        # S&P: AAA, AA+/AA/AA−, A+/A/A−, BBB+… followed by "(stable/…) by Standard"
        ms = re.search(
            r"(AAA|AA[+\-]?|A[+\-]?|BBB[+\-]?|BB[+\-]?|B[+\-]?)"
            r"\s*\([^)]+\)",
            ratings_raw,
        )
        opt("rating_moodys", mm.group(1) if mm else None)
        opt("rating_sp",     ms.group(1) if ms else None)
    else:
        opt("rating_moodys", None)
        opt("rating_sp",     None)

    # CUSIP & ISIN
    cm = _CUSIP_RE.search(text)
    im = _ISIN_RE.search(text)
    cusip_raw = cm.group(1) if cm else None
    req("cusip", cusip_raw.replace(" ", "") if cusip_raw else None)
    req("isin",  im.group(1) if im else None)

    # ── rate-type-specific fields ─────────────────────────────────────────────
    if fields["rate_type"] == "Fixed":
        # Benchmark treasury yield — Apple FWPs use a dedicated label
        # "Benchmark Treasury Yield: X.XXX%" separate from "Benchmark Treasury: X% due ..."
        bm_yield_raw = find_raw(r"Benchmark Treasury Yield:")
        if bm_yield_raw is None:
            # Fallback: older FWP format may embed "(yield to maturity: X%)" in the description
            bm_desc_raw = find_raw(r"Benchmark Treasury:")
            bm_m = re.search(r"\(yield to maturity:\s*(\d+\.\d+)\s*%\)",
                             bm_desc_raw or "", re.IGNORECASE)
            bm_yield_val = bm_m.group(1) if bm_m else None
        else:
            bm_m = re.search(r"(\d+\.\d+)", bm_yield_raw)
            bm_yield_val = bm_m.group(1) if bm_m else None
        opt("benchmark_treasury_yield_pct", bm_yield_val)
        if not bm_yield_val:
            missing.append("benchmark_treasury_yield_pct")

        # Spread to benchmark treasury
        spread_raw = find_raw(r"Spread to(?:\s+Benchmark)?\s+Treasury:")
        spread_m = None
        if spread_raw:
            spread_m = re.search(r"\+?(\d+)\s*(?:bps?|basis\s+points?)", spread_raw, re.IGNORECASE)
        if spread_m is None:
            spread_m = re.search(r"[Tt]\+(\d+)\s*(?:bps?|basis\s+points?)", text, re.IGNORECASE)
        spread_val = spread_m.group(1) if spread_m else None
        opt("spread_to_treasury_bps", spread_val)
        if not spread_val:
            missing.append("spread_to_treasury_bps")

        # Apple bond yield — take the LAST "Yield: X.XXX%" match in the block.
        # "Benchmark Treasury Yield: X%" always precedes "Yield: X%" in Apple FWPs,
        # so the last match is always the Apple bond's own yield to maturity.
        yield_hits = list(re.finditer(
            r"(?:Yield to Maturity|Yield):\s*(\d+\.\d+)\s*%",
            text, re.IGNORECASE,
        ))
        yield_val = yield_hits[-1].group(1) if yield_hits else None
        opt("yield_pct", yield_val)
        if not yield_val:
            missing.append("yield_pct")

        # Floating-only → empty
        fields["floating_benchmark"]  = ""
        fields["floating_spread_pct"] = ""

    else:  # Floating
        # Benchmark: "Compounded SOFR" or "SOFR"
        fb_m = re.search(
            r"will\s+be\s+(Compounded\s+SOFR|(?:Term\s+)?SOFR)",
            text, re.IGNORECASE | re.DOTALL,
        )
        fb_val = fb_m.group(1) if fb_m else None
        opt("floating_benchmark", fb_val)
        if not fb_val:
            missing.append("floating_benchmark")

        # Spread: "plus 0.45%"
        spread_m = re.search(r"plus\s+(\d+\.\d+)\s*%", text, re.IGNORECASE)
        opt("floating_spread_pct", spread_m.group(1) if spread_m else None)
        if not spread_m:
            missing.append("floating_spread_pct")

        # Fixed-only → empty
        fields["benchmark_treasury_yield_pct"] = ""
        fields["spread_to_treasury_bps"]       = ""
        fields["yield_pct"]                    = ""

    return fields, missing


# ── main ──────────────────────────────────────────────────────────────────────
def safe_filename(s: str) -> str:
    return re.sub(r"[^\w\-]", "_", s)[:60]


BLOCKS_DIR.mkdir(parents=True, exist_ok=True)

docs   = pd.read_csv(DOCS_CSV)
apple  = docs[(docs["issuer"] == "Apple") & docs["fwp_url"].notna()
              & (docs["fwp_url"] != "")].copy()
apple  = apple.sort_values("deal_date").reset_index(drop=True)

print("=" * 70)
print("Apple FWP term-sheet extractor")
print("=" * 70)
print(f"Deals to process: {len(apple)}")
for _, r in apple.iterrows():
    print(f"  {r['deal_date']}  {r['fwp_url']}")
print()

all_rows: list[dict] = []

for _, deal in apple.iterrows():
    fwp_url    = deal["fwp_url"]
    fwp_acc    = deal["fwp_accession"]
    deal_date  = deal["deal_date"]
    print(f"\n--- Deal {deal_date} ---")
    print(f"    {fwp_url}")

    raw = load_html("Apple", fwp_acc, fwp_url)
    if not raw:
        print("  [!] fetch failed — skipping")
        continue

    blocks = split_into_bond_blocks(raw)
    print(f"  Bond sections found: {len(blocks)}")

    if not blocks:
        print("  [!] No <u> bond headings found — check HTML structure")
        continue

    for blk in blocks:
        heading   = blk["heading"]
        flat_text = html_to_flat(blk["block_html"])
        line_text = html_to_lines(blk["block_html"])

        fields, missing = extract_fields(flat_text, heading)
        fields["missing_fields"] = ", ".join(missing)

        # Save block text for traceability
        file_id  = fields["cusip"] if fields["cusip"] else safe_filename(heading)
        txt_path = BLOCKS_DIR / f"{file_id}.txt"
        txt_path.write_text(
            f"DEAL:    {deal_date}\n"
            f"FWP URL: {fwp_url}\n"
            f"HEADING: {heading}\n"
            f"{'─' * 60}\n"
            + line_text,
            encoding="utf-8",
        )

        all_rows.append(fields)

        m_flag = f"  ** missing: {fields['missing_fields']}" if missing else ""
        print(f"  [{fields['rate_type'][:3]}] {heading[:55]}"
              f"  CUSIP={fields['cusip'] or '?'}  ISIN={fields['isin'] or '?'}"
              f"{m_flag}")

    print(f"  Total bonds this deal: {len(blocks)}")

# ── save CSV ──────────────────────────────────────────────────────────────────
out_df = pd.DataFrame(all_rows, columns=OUTPUT_COLUMNS)
OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
out_df.to_csv(OUT_CSV, index=False)
print(f"\nSaved: {OUT_CSV}  ({len(out_df)} bonds)")
print(f"Block files in: {BLOCKS_DIR}")

# ── print table ───────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("EXTRACTED BONDS")
print("=" * 70)
display_cols = ["bond_name", "rate_type", "coupon_pct", "principal_amount",
                "maturity_date", "cusip", "isin", "missing_fields"]
print(out_df[display_cols].to_string(index=False))

print(f"\nTotal bonds: {len(out_df)}")
deal_counts = out_df.apply(
    lambda row: row["bond_name"][:7], axis=1
)  # just to group by deal — use maturity year instead
# Count by deal (use trade_date grouping)
print("\nBonds per deal:")
deal_groups = {}
for _, r in out_df.iterrows():
    td = r.get("trade_date", "unknown")
    deal_groups.setdefault(td, 0)
    deal_groups[td] += 1
for td, cnt in sorted(deal_groups.items()):
    print(f"  Trade date {td}: {cnt} bonds")
