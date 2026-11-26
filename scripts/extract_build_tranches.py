#!/usr/bin/env python3
"""
Module 5 — multi-company FWP term-sheet extractor (build set).
Companies: Apple (from CSV), Eli Lilly, Microsoft, Alphabet, Johnson & Johnson.
Output: data/processed/module5_build_tranches.csv  (one row per bond).

Strategies:
  Apple       — load existing module5_apple_tranches.csv
  Eli Lilly   — block_u (<u> headings) + preamble prepend
  Microsoft   — flat_labeled (all bonds listed with "YYYY Notes: VALUE" rows)
  Alphabet    — block_b (<b> headings) for Nov 2025 USD; flat_labeled for rest
  J&J         — flat_positional (bonds listed by position/column order)
"""
import html as _html
import re
from pathlib import Path
import pandas as pd

DOCS_CSV  = Path(__file__).parent.parent / "data" / "processed" / "module5_documents.csv"
APPLE_CSV = Path(__file__).parent.parent / "data" / "processed" / "module5_apple_tranches.csv"
OUT_CSV   = Path(__file__).parent.parent / "data" / "processed" / "module5_build_tranches.csv"
RAW_DIR   = Path(__file__).parent.parent / "data" / "raw" / "prospectus"

OUTPUT_COLUMNS = [
    "issuer", "bond_name", "currency", "principal_amount", "principal_source",
    "rate_type", "coupon_pct", "coupon_frequency", "interest_payment_dates",
    "floating_benchmark", "floating_spread_pct",
    "maturity_date", "price_to_public_pct",
    "benchmark_treasury_yield_pct", "spread_to_treasury_bps",
    "yield_pct", "yield_semiannual", "yield_annual",
    "trade_date", "settlement_date", "day_count", "day_count_source",
    "rating_moodys", "rating_sp",
    "cusip", "isin",
    "fwp_accession",
    "missing_fields",
]

# ─── Terminal lookahead ───────────────────────────────────────────────────────
_TERM = (
    r"(?=\s*(?:"
    r"Issuer:|Principal Amount(?: Offered)?:|Aggregate Principal Amount:|"
    r"Maturity(?:\s+Date)?:|"
    r"(?:Public Offering )?Price(?:\s+to(?:\s+the)?\s+Public)?[^:]{0,30}:|"
    r"Benchmark(?:\s+Treasury|\s+Gilt|\s+Bund|\s+Bond|\s+Bond\s+Price)[^:]{0,40}:|"
    r"Spread to(?:\s+Benchmark)?\s+(?:Treasury|Gilt|GoC|Mid-Swap|Bund)|"
    r"Re-Offer Spread:|"
    r"Yield(?:\s+to\s+Maturity)?(?:\s*\([^)]*\))?:|"
    r"Coupon(?: \(Interest Rate\))?:|"
    r"Trade Date:|Settlement Date:|Denominations?:|"
    r"(?:Expected )?Ratings?(?:\s*\*{0,2})?:|"
    r"Net Proceeds:|CUSIP|Underwriting|"
    r"Day Count Convention:|Calculation Agent:|"
    r"Par (?:call|Call):|Sinking|"
    r"Title(?:\s+of\s+Securities)?:|Security:|Security Type:|"
    r"Listing:|Ranking:|Optional Redemption:|Interest Payment[^:\n]{0,20}:|"
    r"\Z"
    r"))"
)

# ─── Regex patterns ───────────────────────────────────────────────────────────
# CUSIP: handles optional bond-label prefix "2028 Notes: "
_CUSIP_RE = re.compile(
    r"CUSIP(?:\s*/\s*(?:Common\s+Code|ISIN))*(?:\s+No(?:mber)?\.?)?[\s:/#]+"
    r"(?:[0-9A-Z][^:]{1,40}:\s*)?"
    r"([0-9A-Z]{5,6}\s?[0-9A-Z]{2}\s?[0-9A-Z])\b",
    re.IGNORECASE,
)

_ISIN_RE = re.compile(r"\b([A-Z]{2}[0-9A-Z]{10})\b")

_APPLE_HDG  = re.compile(
    r"\d+\.\d+%\s+Notes?\s+due\s+\d{4}|Floating\s+Rate\s+Notes?\s+due\s+\d{4}", re.I)
_LILLY_HDG  = re.compile(r"The\s+\d{4}\s+(?:Floating\s+Rate\s+)?Notes?", re.I)
_ALPHA_B_HDG = re.compile(
    r"\d+\.\d+%\s+Notes?\s+due\s+\d{4}|Floating\s+Rate\s+Notes?\s+due\s+\d{4}", re.I)


# ─── HTML helpers ─────────────────────────────────────────────────────────────
def _unn(t):
    t = _html.unescape(t)
    return re.sub(r"[\xa0\u2009\u2002\u2003\u200b\u00ad]", " ", t)

def html_to_flat(raw):
    t = re.sub(r"<[^>]+>", " ", raw)
    t = _unn(t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_day_count(raw):
    """Normalize day count string to a canonical form for comparison."""
    if not raw:
        return raw
    r = re.sub(r"\s*/\s*", "/", str(raw).strip())
    r = re.sub(r"\s+", " ", r)
    if re.match(r"(?i)30/360", r):                      return "30/360"
    if re.match(r"(?i)actual/360", r):                  return "Actual/360"
    if re.match(r"(?i)act(?:ual)?/act(?:ual)?", r):     return "Actual/Actual (ICMA)"
    if re.match(r"(?i)actual/365", r):                  return "Actual/365"
    return r


def detect_coupon_frequency(text):
    """Detect coupon payment frequency from 'Interest Payment Dates' in document text.
    Returns 'annual', 'semi-annual', or 'quarterly'."""
    ip_m = re.search(
        r"(?i)Interest\s+Payment\s+Date[^:]*:?\s*(.*?)"
        r"(?=\s*(?:Day Count|CUSIP|Listing|Denominations?|Optional|\Z))",
        text, re.DOTALL
    )
    if ip_m:
        ip_text = ip_m.group(1)[:400]
        if re.search(r"(?i)\bquarterly\b", ip_text):
            return "quarterly"
        if re.search(r"(?i)\bsemi.annually\b", ip_text):
            return "semi-annual"
        # Annual: a single month-day before "of each year" (not "and Month Day of each year")
        single_ann = re.search(
            r"(?i)\b(?:January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+\d+\s+of\s+each\s+year",
            ip_text
        )
        if single_ann:
            two_dates = re.search(
                r"(?i)[A-Z][a-z]+\s+\d+\s+and\s+[A-Z][a-z]+\s+\d+[^.]*of\s+each\s+year",
                ip_text
            )
            if not two_dates:
                return "annual"
    return "semi-annual"

_MONTHS_PAT = (
    r"(?:January|February|March|April|May|June|July|August"
    r"|September|October|November|December)"
)

def _parse_ip_text(text):
    """Return (frequency, date_string) from an Interest Payment Dates value.
    frequency: 'quarterly', 'semi-annual', 'annual', or ''
    date_string: e.g. 'February 15 and August 15' or 'May 3 and November 3'
    """
    if not text:
        return '', ''
    t = text[:400]
    if re.search(r'(?i)\bquarterly\b', t):
        freq = 'quarterly'
    elif re.search(r'(?i)\bsemi.annually\b', t):
        freq = 'semi-annual'
    else:
        # Deduplicate: "February 13 of each year, beginning on February 13" → 1 unique date
        raw = re.findall(rf'(?i){_MONTHS_PAT}\s+\d{{1,2}}\b', t)
        unique = list(dict.fromkeys(d.title() for d in raw))
        if len(unique) >= 4:
            freq = 'quarterly'
        elif len(unique) >= 2:
            freq = 'semi-annual'
        elif unique:
            freq = 'annual'
        else:
            freq = ''
    dates_all = list(dict.fromkeys(
        d.title() for d in re.findall(rf'(?i)({_MONTHS_PAT}\s+\d{{1,2}})\b', t)
    ))
    n = {'quarterly': 4, 'semi-annual': 2, 'annual': 1}.get(freq, 2)
    date_str = ' and '.join(dates_all[:n]) if dates_all else ''
    return freq, date_str


def load_html(issuer, accession):
    p = RAW_DIR / issuer / f"{accession}.htm"
    if not p.exists():
        return ""
    raw = p.read_bytes()[:800_000]
    # Windows-1252 files use 0x93/0x94 for curly quotes; detect and decode correctly
    if b"\x93" in raw or b"\x94" in raw:
        return raw.decode("cp1252", errors="replace")
    return raw.decode("utf-8", errors="replace")


def lookup_day_count_from_prospectus(issuer_folder, fwp_accession):
    """Read the 424B2 prospectus for this deal and extract day count conventions.
    Returns dict: {"Fixed": (value, found_bool), "Floating": (value, found_bool)}."""
    result = {"Fixed": ("", False), "Floating": ("", False)}
    docs = pd.read_csv(DOCS_CSV, dtype=str)
    row = docs[docs["fwp_accession"] == fwp_accession]
    if row.empty:
        return result
    b2_accn = row.iloc[0]["accession_number"]
    b2_path = RAW_DIR / issuer_folder / f"{b2_accn}.htm"
    if not b2_path.exists():
        return result
    flat = html_to_flat(b2_path.read_bytes().decode("utf-8", errors="replace"))
    # Fixed: "360-day year of/composed of twelve 30-day months" near "computed on the basis of"
    if re.search(r"360.day\s+year\s+(?:of|composed\s+of)\s+twelve\s+30.day\s+months", flat, re.I):
        result["Fixed"] = ("30/360", True)
    # Floating: "actual number of days" within ~80 chars of "360-day year"
    # Proximity check avoids false positives from fixed-rate language in the same doc
    if re.search(
        r"(?:actual\s+number\s+of\s+days.{1,80}360.day\s+year"
        r"|360.day\s+year\s+and\s+the\s+actual\s+number\s+of\s+days)",
        flat, re.I,
    ):
        result["Floating"] = ("Actual/360", True)
    return result


# ─── Principal-amount extractor for grouped-preamble FWPs (Broadcom) ─────────
import html as _html_mod

# Format A: "$AMOUNT aggregate principal amount of COUPON% ... Notes ... due/mature YEAR"
_PAT_PRINCIPAL_A = re.compile(
    r'\$([0-9,]+)\s+aggregate\s+principal\s+amount\s+of\s+([\d.]+)%'
    r'\s+\S+\s+Notes[^$]{0,300}?'
    r'(?:due\s+|mature[sd]?\s+on\s+(?:\S+\s+\d{1,2},?\s+)?)(\d{4})',
    re.I,
)
# Format B: cover-page title "$AMOUNT COUPON% Senior Notes due YEAR"
_PAT_PRINCIPAL_B = re.compile(
    r'\$([0-9,]+)\s+([\d.]+)%\s+\w+\s+Notes\s+due\s+(\d{4})',
    re.I,
)

def extract_principal_from_html(raw):
    """
    Return {(coupon_float, maturity_year_int): amount_string} from raw HTML.

    Handles two FWP layouts Broadcom uses:
      Format A: "$1,250,000,000 aggregate principal amount of 5.050% ... due 2027"
                or "... mature on July 12, 2027"
      Format B: "$1,250,000,000 5.050% Senior Notes due 2027" (cover-page title)
    """
    plain = re.sub(r'<[^>]+>', ' ', raw)
    plain = _html_mod.unescape(plain)
    plain = re.sub(r'\s+', ' ', plain)
    result = {}
    for pat in (_PAT_PRINCIPAL_A, _PAT_PRINCIPAL_B):
        for m in pat.finditer(plain):
            coupon = round(float(m.group(2)), 4)
            year   = int(m.group(3))
            if (coupon, year) not in result:
                result[(coupon, year)] = m.group(1)
    return result


# ─── Rating parser (handles forward/reversed/mixed formats) ───────────────────
#
# Agency-name sub-patterns use inline (?i:...) so matching is case-insensitive
# for the label but case-sensitive for the grade itself:
#   Moody's grades  → mixed-case  (Aaa, Aa2, A1, Baa3 …)
#   S&P grades      → all-caps   (AAA, AA+, A+, BBB- …)
#
# The (?![0-9A-Za-z]) lookahead prevents partial-token matches, e.g. it stops
# the grade pattern 'A' from matching the 'A' in 'A1', or 'B' from matching
# the 'b' in '(Stable)'.
#
# ’ is the curly right-single-quotation mark used in many FWP HTML files
# for the apostrophe in "Moody's"; the straight apostrophe ' is also kept.

_MDY = r"(?i:Moody[’']?s(?:\s+Investors?\s+Service(?:,?\s+Inc\.?)?)?)"
_SP  = r"(?i:S&?P(?:\s+Global\s+Ratings?)?|Standard\s*&\s*Poor[’']?s)"

_MDY_GRADE = r"(?:Aaa|Aa[1-3]|A[1-3]|Baa[1-3]|Ba[1-3]|B[1-3]|Caa[1-3]|Ca|C)(?![0-9A-Za-z])"
_SP_GRADE  = r"(?:AAA|AA[+\-]?|A[+\-]?|BBB[+\-]?|BB[+\-]?|B[+\-]?|CCC[+\-]?|CC|D)(?![0-9A-Za-z])"


def parse_ratings(raw):
    if not raw:
        return None, None
    t = raw

    # Format 1: label then grade ("Moody's: Aaa ..." or "S&P: AA+ ...")
    mm = re.search(rf"{_MDY}:?\s*({_MDY_GRADE})", t)
    ms = re.search(rf"{_SP}:?\s*({_SP_GRADE})", t)
    if mm and ms:
        return mm.group(1), ms.group(1)

    # Format 2: grade then label ("Aaa (stable) Moody's" or "A1 (outlook) (Moody's ...)")
    # 40-char window: wide enough for multi-word outlook phrases but short enough to
    # separate BBB- (Fitch, 46 chars from S&P) from BBB (S&P, 10 chars from S&P)
    mm2 = re.search(rf"({_MDY_GRADE})[^\n/]{{0,40}}?{_MDY}", t)
    ms2 = re.search(rf"({_SP_GRADE})[^\n/]{{0,40}}?{_SP}", t)
    if mm2 or ms2:
        return (mm2.group(1) if mm2 else None), (ms2.group(1) if ms2 else None)

    # Format 3: grade + parenthetical, no label (Apple "Aaa (stable)")
    mm3 = re.search(rf"({_MDY_GRADE})\s*\([^)]+\)", t)
    ms3 = re.search(rf"({_SP_GRADE})\s*\([^)]+\)", t)
    return (mm3.group(1) if mm3 else None), (ms3.group(1) if ms3 else None)


# ─── ISIN extractor ───────────────────────────────────────────────────────────
def find_isin(text):
    """Find ISIN (US/XS/GB/CA prefix, 12 chars) from CUSIP/ISIN line or standalone."""
    # Prefer ISIN found after a CUSIP/ISIN label
    m = re.search(
        r"(?:CUSIP(?:\s*/\s*(?:Common\s+Code|ISIN))+\s*:\s*|ISIN\s*:\s*|/\s*)"
        r"(?:[0-9A-Z][^/\n]{0,20}/\s*){0,2}"
        r"([A-Z]{2}[0-9A-Z]{10})\b",
        text, re.I,
    )
    if m:
        return m.group(1)
    # Fallback: standalone ISIN code
    m2 = re.search(r"\b(US[0-9A-Z]{10}|XS[0-9A-Z]{10}|GB[0-9A-Z]{10}|CA[0-9A-Z]{10})\b", text)
    return m2.group(1) if m2 else None


# ─── Labeled value finder (flat_labeled documents) ───────────────────────────
def find_field_section(doc_flat, field_re):
    """Return the text of a labeled field's value section (first occurrence)."""
    m = re.search(rf"(?i)(?:{field_re}):?\s*(.*?){_TERM}", doc_flat, re.DOTALL)
    v = m.group(1) if m else None
    return v.strip() if v is not None else None

def find_all_field_sections(doc_flat, field_re):
    """Return combined text of ALL occurrences of a labeled field's value section.
    Needed when a document has separate tables for each bond group (e.g. Microsoft)."""
    parts = [m.group(1).strip()
             for m in re.finditer(rf"(?i)(?:{field_re}):?\s*(.*?){_TERM}", doc_flat, re.DOTALL)
             if m.group(1).strip()]
    return " ".join(parts) if parts else None

def find_labeled_value(section, bond_label):
    """Within a field section, find the value for bond_label."""
    if not section or not bond_label:
        return None
    # Require "Notes" or "Floating" after the year to avoid stopping at a year
    # that is part of a date (e.g. "October 15, 2034 Redemption at Broadcom's Option:").
    term = r"(?=\s+\d{4}\s+(?:Notes?|Floating)[^:\n]{0,35}:|\s*$|\Z)"
    lm = re.search(
        rf"(?i){re.escape(bond_label)}\s*:\s*(.*?){term}",
        section, re.DOTALL,
    )
    return lm.group(1).strip() if lm else None

def find_cusip_labeled(doc_flat, bond_label):
    """Find CUSIP for bond_label in a labeled CUSIP block."""
    # Find ALL CUSIP sections (some docs split by bond group)
    sec = find_all_field_sections(doc_flat, r"CUSIP(?:\s*/\s*(?:Common\s+Code|ISIN))*")
    if not sec:
        return None
    term = r"(?=\s+\d{4}\s+(?:Notes?|Floating)|\Z)"
    lm = re.search(
        rf"(?i){re.escape(bond_label)}\s*:\s*(.*?){term}",
        sec, re.DOTALL,
    )
    if not lm:
        return None
    piece = lm.group(1).strip()
    cm = re.search(r"([0-9A-Z]{5,6}\s?[0-9A-Z]{2}\s?[0-9A-Z])\b", piece)
    return cm.group(1) if cm else None

def find_isin_labeled(doc_flat, bond_label):
    """Find ISIN for bond_label in a labeled CUSIP/ISIN block.

    Checks both combined 'CUSIP / ISIN:' sections and separate 'ISIN Number:'
    sections (the latter is used by Broadcom FWPs).
    """
    # Put the pure ISIN section first so find_labeled_value returns the ISIN
    # (12-char "USxxxxx"), not the CUSIP (9-char "11135FCA7").
    # Broadcom FWPs have separate "CUSIP Number:" and "ISIN Number:" fields;
    # for combined "CUSIP / ISIN:" sections (e.g. Meta, Apple) s2 also exists
    # and find_isin() can recover the ISIN from either order.
    s1 = find_all_field_sections(doc_flat, r"CUSIP(?:\s*/\s*(?:Common\s+Code|ISIN))*")
    s2 = find_all_field_sections(doc_flat, r"ISIN(?:\s+Number)?")
    sec = " ".join(filter(None, [s2, s1])) or None
    if not sec:
        return None
    term = r"(?=\s+\d{4}\s+(?:Notes?|Floating)|\Z)"
    lm = re.search(
        rf"(?i){re.escape(bond_label)}\s*:\s*(.*?){term}",
        sec, re.DOTALL,
    )
    if not lm:
        return None
    piece = lm.group(1).strip()
    return find_isin(piece)

def detect_currency(text):
    if re.search(r"C\$|C \$|\bCAD\b", text):
        return "CAD"
    if re.search(r"£|\bGBP\b", text):
        return "GBP"
    if re.search(r"€|\bEUR\b", text):
        return "EUR"
    return "USD"


# ─── Block splitter ───────────────────────────────────────────────────────────
def split_blocks(html, heading_re, tag="u"):
    """Split HTML on <tag> headings matching heading_re.
    Returns (preamble_html, [{heading, block_html}])."""
    pat = re.compile(rf"<{tag}[^>]*>(.*?)</{tag}>", re.DOTALL | re.I)
    sections = []
    for m in pat.finditer(html):
        inner = html_to_flat(m.group(1))
        inner = re.sub(r"\s+", " ", inner).strip()
        if heading_re.search(inner):
            sections.append({"start": m.start(), "end": m.end(), "heading": inner})
    if not sections:
        return html, []
    preamble = html[:sections[0]["start"]]
    blocks = []
    for i, sec in enumerate(sections):
        nxt = sections[i + 1]["start"] if i + 1 < len(sections) else len(html)
        blocks.append({"heading": sec["heading"],
                       "block_html": html[sec["end"]:nxt]})
    return preamble, blocks


# ─── Block-level field extractor (Apple / Eli Lilly / Alphabet Nov2025) ───────
def extract_fields_block(text, heading, doc_flat=None, bond_label=None, block_text=None):
    """
    Extract all OUTPUT_COLUMNS fields from one bond's flat text block.
    text:       preamble + block flat text
    heading:    bond heading string
    doc_flat:   full doc flat (for labeled CUSIP fallback, Alphabet Nov 2025)
    bond_label: short label "2028 Notes" (for labeled CUSIP fallback)
    block_text: block-only flat text (no preamble); used to prefer block data
                over preamble data when preamble contains a prior bond's fields
    """
    fields = {}
    missing = []

    def find_raw(label_re, src=None):
        # When block_text is provided and no explicit src, search block first
        # (prevents preamble fields from bleeding into the wrong bond's block)
        for t in ([src] if src is not None
                  else ([block_text, text] if block_text else [text])):
            if t is None:
                continue
            m = re.search(rf"(?i)(?:{label_re}):?\s*(.*?){_TERM}", t, re.DOTALL)
            v = m.group(1) if m else None
            if v is not None and v.strip():
                return v.strip()
        return None

    def req(k, v):
        fields[k] = v if v else ""
        if not v:
            missing.append(k)

    def opt(k, v):
        fields[k] = v if v else ""

    # ── Bond name & rate type ─────────────────────────────────────────────────
    is_float = bool(re.search(r"[Ff]loating\s+[Rr]ate", heading))
    cpn_in_hdg = re.search(r"(\d+\.\d+)%", heading)

    if cpn_in_hdg and not is_float:
        fields["rate_type"]  = "Fixed"
        fields["coupon_pct"] = cpn_in_hdg.group(1)
        # Strip parenthetical "(the ...)" from Alphabet headings
        fields["bond_name"]  = re.sub(r"\s*\(the\s+[^)]+\)", "", heading).strip()
    elif is_float:
        fields["rate_type"]  = "Floating"
        fields["coupon_pct"] = ""
        fields["bond_name"]  = re.sub(r"\s*\(the\s+[^)]+\)", "", heading).strip()
    else:
        # Eli Lilly: "The 2027 Notes" — get coupon from Coupon: field
        cf = find_raw(r"Coupon(?: \(Interest Rate\))?")
        cm = re.search(r"(\d+\.\d+)\s*%", cf or "")
        yr = re.search(r"\b(\d{4})\b", heading)
        if cm and yr:
            fields["rate_type"]  = "Fixed"
            fields["coupon_pct"] = cm.group(1)
            fields["bond_name"]  = f"{cm.group(1)}% Notes due {yr.group(1)}"
        else:
            fields["rate_type"]  = "Fixed"
            fields["coupon_pct"] = ""
            fields["bond_name"]  = heading
            missing.append("coupon_pct")

    # ── Issuer ────────────────────────────────────────────────────────────────
    # Require colon: EDGAR file headers contain bare "Issuer" without colon
    req("issuer", find_raw(r"Issuer(?=\s*:)"))

    # ── Principal ─────────────────────────────────────────────────────────────
    pa = (find_raw(r"Principal Amount Offered")
          or find_raw(r"Principal Amount")
          or find_raw(r"Aggregate Principal Amount"))
    req("principal_amount", pa)
    opt("currency", detect_currency(pa or "") if pa else "")

    # ── Maturity ──────────────────────────────────────────────────────────────
    # Prefer block_text to avoid a preamble bond's maturity bleeding into this block
    mr = (find_raw(r"Maturity(?:\s+Date)?", src=block_text) if block_text else None) \
         or find_raw(r"Maturity(?:\s+Date)?")
    mm = re.search(r"([A-Z][a-z]+ \d+, \d{4})", mr or "")
    req("maturity_date", mm.group(1) if mm else None)

    # ── Price to Public ───────────────────────────────────────────────────────
    pr = (find_raw(r"Price to(?:\s+the)?\s+Public[^:]{0,20}")
          or find_raw(r"Public Offering Price"))
    if pr:
        pm = re.search(r"(\d+\.\d+)", pr)
        pv = pm.group(1) if pm else None
    else:
        m2 = re.search(
            r"(?:Price to(?:\s+the)?\s+Public|Public Offering Price)[^0-9]*(\d+\.\d+)\s*%",
            text, re.I)
        pv = m2.group(1) if m2 else None
    req("price_to_public_pct", pv)

    # ── Dates ─────────────────────────────────────────────────────────────────
    # Dates are shared per-deal and always live in the preamble; use combined text
    # to avoid block-first picking up an interest-accrual date inside the block.
    tr = find_raw(r"Trade Date", src=text)
    tm = re.search(r"([A-Z][a-z]+ \d+, \d{4})", tr or "")
    req("trade_date", tm.group(1) if tm else None)

    sr = find_raw(r"Settlement Date[^:]{0,30}", src=text)
    sm = re.search(r"([A-Z][a-z]+ \d+, \d{4})", sr or "")
    req("settlement_date", sm.group(1) if sm else None)

    # ── Day count ─────────────────────────────────────────────────────────────
    dcr = find_raw(r"Day Count Convention")
    if dcr:
        # When a doc states both rates (e.g. "Actual/360 for Floating ... 30/360 for Fixed"),
        # pick the convention that matches this bond's rate type.
        rate_now = fields.get("rate_type", "")
        if rate_now == "Fixed" and re.search(r"30\s*/\s*360", dcr, re.I):
            dcm = re.search(r"(30\s*/\s*360)", dcr, re.I)
        elif rate_now == "Floating" and re.search(r"Actual\s*/\s*\d+", dcr, re.I):
            dcm = re.search(r"(Actual\s*/\s*\d+)", dcr, re.I)
        else:
            dcm = re.search(
                r"(Actual\s*/\s*\d+|30\s*/\s*360|ACT(?:UAL)?\s*/\s*ACT(?:UAL)?(?:\s*\(ICMA\))?)",
                dcr, re.I)
        raw_dc = dcm.group(1) if dcm else dcr.split()[0]
        opt("day_count", normalize_day_count(raw_dc))
        opt("day_count_source", "term_sheet")
    else:
        opt("day_count", None)
        opt("day_count_source", "")  # caller fills from 424B2 prospectus or sets "not_found"

    # ── Ratings ───────────────────────────────────────────────────────────────
    # Ratings are shared per-deal; use combined text so all bonds in a deal
    # extract the same value regardless of whether the block has a copy.
    rr = find_raw(r"(?:Expected )?Ratings?(?:\*{0,2})?", src=text)
    if rr:
        mdy, sp = parse_ratings(rr)
        opt("rating_moodys", mdy)
        opt("rating_sp", sp)
    else:
        opt("rating_moodys", None)
        opt("rating_sp", None)

    # ── CUSIP & ISIN ──────────────────────────────────────────────────────────
    # Prefer block_text to avoid a preamble bond's CUSIP bleeding into this block
    cc = (_CUSIP_RE.search(block_text) if block_text else None) or _CUSIP_RE.search(text)
    if cc:
        raw_c = cc.group(1)
    elif doc_flat and bond_label:
        raw_c = find_cusip_labeled(doc_flat, bond_label)
    else:
        raw_c = None
    req("cusip", raw_c.replace(" ", "") if raw_c else None)

    iv = find_isin(block_text) if block_text else None
    if not iv:
        iv = find_isin(text)
    if not iv and doc_flat and bond_label:
        iv = find_isin_labeled(doc_flat, bond_label)
    req("isin", iv)

    # ── Rate-specific ─────────────────────────────────────────────────────────
    if fields["rate_type"] == "Fixed":
        # Benchmark yield
        byr = find_raw(r"Benchmark(?:\s+Treasury)?\s+Yield")
        if not byr:
            # Eli Lilly format: "Benchmark Treasury Price and Yield: 99-12 3⁄4 / 4.219%"
            bdr_pay = find_raw(r"Benchmark(?:\s+Treasury|\s+Gilt|\s+Bund|\s+Bond)\s+Price\s+and\s+Yield")
            if bdr_pay:
                b3 = re.search(r"/\s*(\d+\.\d+)\s*%", bdr_pay)
                if b3:
                    byv = b3.group(1)
                else:
                    all_pcts = re.findall(r"(\d+\.\d+)%", bdr_pay)
                    byv = all_pcts[-1] if all_pcts else None
            else:
                bdr = find_raw(r"Benchmark(?:\s+Treasury|\s+Gilt|\s+Bund|\s+Bond)(?:\s+Price\s+and\s+Yield)?")
                if bdr:
                    b1 = re.search(r"\(yield to maturity:\s*(\d+\.\d+)\s*%\)", bdr, re.I)
                    b2 = re.search(r"[\d.]+%\s*/\s*(\d+\.\d+)%", bdr)
                    byv = (b1 or b2).group(1) if (b1 or b2) else None
                else:
                    byv = None
        else:
            bm = re.search(r"(\d+\.\d+)", byr)
            byv = bm.group(1) if bm else None
        opt("benchmark_treasury_yield_pct", byv)
        if not byv:
            missing.append("benchmark_treasury_yield_pct")

        # Spread
        spr = find_raw(
            r"Spread to(?:\s+Benchmark)?\s+(?:Treasury|Gilt|GoC|Mid.Swap|Bund)"
            r"|Re-Offer Spread")
        if spr:
            sm2 = re.search(r"\+?\s*(\d+(?:\.\d+)?)\s*(?:bps?|basis\s+points?)", spr, re.I)
            spv = sm2.group(1) if sm2 else None
        else:
            sm3 = re.search(r"[Tt]\+(\d+)\s*(?:bps?|basis\s+points?)", text, re.I)
            spv = sm3.group(1) if sm3 else None
        opt("spread_to_treasury_bps", spv)
        if not spv:
            missing.append("spread_to_treasury_bps")

        # Yield (all block extractor bonds are USD → semi-annual pay)
        yh = list(re.finditer(r"(?:Yield to Maturity|Yield):\s*(\d+\.\d+)\s*%", text, re.I))
        yv = yh[-1].group(1) if yh else None
        if not yv:
            ym2 = re.search(r"Yield to Maturity[^:]*:\s*(\d+\.\d+)%", text, re.I)
            yv = ym2.group(1) if ym2 else None
        opt("yield_pct", yv)
        opt("yield_semiannual", yv or "")
        opt("yield_annual",     "")
        if not yv:
            missing.append("yield_pct")

        opt("floating_benchmark",  "")
        opt("floating_spread_pct", "")

        # Coupon frequency and payment dates from document
        ip_raw = find_raw(r"Interest Payment Date")
        _b_freq, _b_ip_dates = _parse_ip_text(ip_raw or "")
        opt("coupon_frequency", _b_freq or detect_coupon_frequency(text) or "semi-annual")
        opt("interest_payment_dates", _b_ip_dates)

    else:  # Floating
        opt("coupon_frequency", "")
        opt("interest_payment_dates", "")
        opt("yield_semiannual", "")
        opt("yield_annual",     "")
        fb = re.search(
            r"will\s+be\s+(Compounded\s+SOFR|(?:Term\s+)?SOFR|EURIBOR|CORRA|SONIA)",
            text, re.I | re.DOTALL)
        if not fb:
            fb2 = re.search(r"\b(SOFR|EURIBOR|CORRA|SONIA)\b", text, re.I)
            fbv = fb2.group(1).upper() if fb2 else None
        else:
            fbv = fb.group(1)
        opt("floating_benchmark", fbv)
        if not fbv:
            missing.append("floating_benchmark")

        # Spread: "plus X%" or "plus X basis points" or "plus XX bps"
        fs = re.search(r"plus\s+(\d+(?:\.\d+)?)\s*%", text, re.I)
        if fs:
            opt("floating_spread_pct", fs.group(1))
        else:
            fs2 = re.search(r"plus\s+(\d+(?:\.\d+)?)\s*(?:basis\s+points?|bps?)", text, re.I)
            if fs2:
                opt("floating_spread_pct", f"{float(fs2.group(1))/100:.3f}")
            else:
                opt("floating_spread_pct", None)
                missing.append("floating_spread_pct")

        opt("benchmark_treasury_yield_pct", "")
        opt("spread_to_treasury_bps",       "")
        opt("yield_pct",                    "")

    return fields, missing


# ─── Flat-labeled bond labels extractor ──────────────────────────────────────
def get_bond_labels(doc_flat):
    """Extract (full_name, short_label) pairs from Title / Title of Securities."""
    pairs = []
    # Curly quotes (U+201C/U+201D), straight quote, and U+FFFD (cp1252 replacement)
    _Q = r'[\u201c\u201d\"\ufffd]'
    for m in re.finditer(
        r"([1-9]\d*\.?\d*%\s+(?:[^(]|\((?!the\s)){0,50}Notes?\s+due\s+\d{4}"
        r"|Floating\s+Rate\s+Notes?\s+due\s+\d{4})"
        r"\s*\(the\s+" + _Q + r"([^\u201c\u201d\"\ufffd\n]+)" + _Q,
        doc_flat, re.I
    ):
        full  = re.sub(r"\s+", " ", m.group(1)).strip()
        label = m.group(2).strip()
        pairs.append((full, label))
    return pairs


# ─── Grouped-format helpers ───────────────────────────────────────────────────
def _normalize_grouped_flat(flat):
    """
    Normalize year-with-space artifacts that appear after HTML→flat conversion
    in some FWPs (e.g. Broadcom Jul 2024):
      "20 27 Notes: ..." → "2027 Notes: ..."
      "July 12, 20 27"   → "July 12, 2027"
    Safe to call on any flat text; non-matching text is unchanged.
    """
    # Bond labels: "20 27 Notes" → "2027 Notes"
    flat = re.sub(
        r'\b(\d{2})\s+(\d{2})\s+((?:Floating\s+Rate\s+)?Notes?)\b',
        lambda m: m.group(1) + m.group(2) + ' ' + m.group(3).strip(),
        flat, flags=re.I,
    )
    # Dates: ", 20 27" → ", 2027"
    flat = re.sub(r'(?<=,\s)(\d{2})\s+(\d{2})\b', r'\1\2', flat)
    return flat


def get_bond_labels_grouped(doc_flat):
    """
    Discover (full_name, label) pairs from a grouped-table FWP.

    In grouped FWPs every field row lists values for all bonds with their label
    as a prefix: "Coupon: 2028 Notes: 4.6%  2030 Notes: 4.8%".
    Bond labels are discovered from the CUSIP/ISIN block at the bottom.
    Returns the same (full_name, label) format as get_bond_labels().
    """
    # Find the combined or separate CUSIP block
    sec = (find_all_field_sections(doc_flat, r"CUSIP(?:\s*/\s*(?:Common\s+Code|ISIN))*")
           or find_all_field_sections(doc_flat, r"CUSIP(?:\s+Number)?"))
    if not sec:
        return []

    # Extract bond labels — "2028 Notes:", "2033 Notes:", "2027 Floating Rate Notes:" etc.
    labels, seen = [], set()
    for m in re.finditer(r'\b(\d{4})\s+((?:Floating\s+Rate\s+)?Notes?)\s*:', sec, re.I):
        label = f"{m.group(1)} {m.group(2).strip()}"
        key = label.lower()
        if key not in seen:
            seen.add(key)
            labels.append(label)
    if not labels:
        return []

    # For each label extract coupon from the grouped fields to build a full_name
    cpn_sec = (find_all_field_sections(doc_flat, r"Coupon(?:\s*\(Interest Rate\))?")
               or find_all_field_sections(doc_flat, r"Interest\s+Rate(?!\s+Notes?)"))

    pairs = []
    for label in labels:
        yr_m = re.search(r'\d{4}', label)
        yr = yr_m.group(0) if yr_m else "????"
        is_float = bool(re.search(r'Floating', label, re.I))

        coupon = None
        if not is_float and cpn_sec:
            cpn_val = find_labeled_value(cpn_sec, label)
            if cpn_val:
                cpn_m = re.search(r'(\d+\.\d+)\s*%', cpn_val)
                if cpn_m:
                    coupon = cpn_m.group(1)

        if is_float:
            full_name = f"Floating Rate Notes due {yr}"
        elif coupon:
            full_name = f"{coupon}% Notes due {yr}"
        else:
            full_name = f"Notes due {yr}"
        pairs.append((full_name, label))

    return pairs


# ─── Flat-labeled full extractor (Microsoft, Alphabet flat) ──────────────────
def extract_flat_labeled(doc_flat, shared_issuer, fwp_date=None, prospectus_dc=None,
                         _bond_pairs=None):
    """
    Extract all bonds from a flat multi-bond document.
    Returns list of field dicts.
    """
    bond_pairs = _bond_pairs if _bond_pairs is not None else get_bond_labels(doc_flat)
    if not bond_pairs:
        return []

    # Shared top-level fields
    def top(label_re):
        m = re.search(rf"(?i){label_re}:?\s*(.*?){_TERM}", doc_flat, re.DOTALL)
        return m.group(1).strip() if m else None

    # "Pricing Date" is an alias for "Trade Date" (used by Broadcom FWPs)
    trade_raw  = top(r"(?:Trade Date|Pricing Date)")
    trade_date = re.search(r"([A-Z][a-z]+ \d+, \d{4})", trade_raw or "")
    trade_date = trade_date.group(1) if trade_date else ""

    # "Closing Date" is an alias for "Settlement Date" (used by Broadcom FWPs)
    sett_raw   = top(r"(?:Settlement Date[^:]{0,30}|Closing Date[^:]{0,30})")
    sett_date  = re.search(r"([A-Z][a-z]+ \d+, \d{4})", sett_raw or "")
    sett_date  = sett_date.group(1) if sett_date else ""

    ratings_raw = top(r"(?:Expected )?Ratings?(?:\*{0,2})?")
    mdy, sp = parse_ratings(ratings_raw)

    # Collect ALL Day Count Convention occurrences in the document.
    # Some FWPs (Alphabet USD with both floating and fixed tranches) list one per
    # bond type: "Actual/360" for the floating section, "30/360" for the fixed section.
    # Picking only the first occurrence assigns the floating convention to fixed bonds.
    _DC_PAT = r"(?i)Day Count[^:]{0,30}:\s*([^\n]{1,120})"
    _DC_VAL = r"(Actual\s*/\s*\d+|30\s*/\s*360|ACT(?:UAL)?\s*/\s*ACT(?:UAL)?(?:\s*\(ICMA\))?)"
    all_dcs = []
    for dc_text in re.findall(_DC_PAT, doc_flat):
        dcm = re.search(_DC_VAL, dc_text, re.I)
        if dcm:
            all_dcs.append(normalize_day_count(dcm.group(1)))
    day_count       = all_dcs[0] if all_dcs else ""   # first occurrence (floating section default)
    day_count_fixed = next((d for d in all_dcs if d != "Actual/360"), "") or day_count

    # Coupon frequency for the whole document (fallback only)
    doc_coupon_freq = detect_coupon_frequency(doc_flat)

    # Per-bond IP dates lookup (avoids contamination from floating-rate section)
    _ip_sec   = find_all_field_sections(doc_flat, r"Interest Payment Date")
    _fixed_m  = re.search(r"Fixed Rate Notes\s+(.*)", doc_flat, re.DOTALL | re.I)
    _fixed_flat = _fixed_m.group(1) if _fixed_m else None

    rows = []
    for full_name, label in bond_pairs:
        fields = {}
        missing = []

        def fv(field_re, lbl=label):
            sec = find_all_field_sections(doc_flat, field_re)
            return find_labeled_value(sec, lbl)

        # Rate type & coupon from full name
        is_float = bool(re.search(r"[Ff]loating", full_name))
        cm_hdg = re.search(r"(\d+\.\d+)%", full_name)
        fields["issuer"]    = shared_issuer
        fields["bond_name"] = full_name
        fields["rate_type"] = "Floating" if is_float else "Fixed"
        fields["coupon_pct"]= "" if is_float else (cm_hdg.group(1) if cm_hdg else "")
        if not is_float and not fields["coupon_pct"]:
            missing.append("coupon_pct")

        # Principal & currency (search all occurrences — docs may split by bond group)
        # Meta uses "Principal Amount:" (no "Aggregate"); try both
        pa_sec  = (find_all_field_sections(doc_flat, r"Aggregate Principal Amount")
                   or find_all_field_sections(doc_flat, r"Principal Amount(?!\s+Offered)"))
        pa_val  = find_labeled_value(pa_sec, label)
        # Truncate to just the currency amount (strip redemption/make-whole text)
        if pa_val:
            am = re.match(r'((?:C\s?)?[$\u20ac\xa3]\s*[\d,]+(?:\.\d+)?)', pa_val)
            if am:
                pa_val = am.group(1)
        # Fallback for floating rate bonds whose principal section is standalone
        # (unlabeled leading amount, before any "NNNN Notes:" entries).
        # E.g. Alphabet Nov-2026: floating rate note in its own <u> block \u2192 principal
        # section = "\u20ac750,000,000 2028 Notes: \u20ac750,000,000 2030 Notes: ..." and the
        # label "2028 Floating Rate Notes" never appears with a colon.
        if not pa_val and is_float and pa_sec:
            first_amt = re.match(r'\s*((?:C\s?)?[$\u20ac\xa3]\s*[\d,]+(?:\.\d+)?)', pa_sec)
            first_lbl = re.search(r'\d{4}\s+Notes?:', pa_sec)
            if first_amt and (not first_lbl or first_amt.end() < first_lbl.start()):
                pa_val = first_amt.group(1)
        fields["principal_amount"] = pa_val or ""
        if not pa_val:
            missing.append("principal_amount")
        fields["currency"] = detect_currency(pa_val or doc_flat[:500])

        # Maturity
        mat_sec = find_all_field_sections(doc_flat, r"Maturity(?:\s+Date)?")
        mat_val = find_labeled_value(mat_sec, label)
        if mat_val:
            md = re.search(r"([A-Z][a-z]+ \d+, \d{4})", mat_val)
            fields["maturity_date"] = md.group(1) if md else mat_val.split()[0]
        else:
            fields["maturity_date"] = ""
            missing.append("maturity_date")

        # Price
        pr_sec = (find_all_field_sections(doc_flat, r"Public Offering Price")
                  or find_all_field_sections(doc_flat, r"Price to(?:\s+the)?\s+Public[^:]{0,20}"))
        pr_val = find_labeled_value(pr_sec, label)
        if pr_val:
            pm = re.search(r"(\d+\.\d+)", pr_val)
            fields["price_to_public_pct"] = pm.group(1) if pm else ""
        else:
            fields["price_to_public_pct"] = ""
            missing.append("price_to_public_pct")

        # Day count: floating bonds always use Actual/360; fixed bonds use the
        # fixed-specific day count (the first non-Actual/360 value in the document).
        # When the document has no day count at all, fall back to the prospectus.
        if is_float:
            if day_count:
                # Term sheet mentions a day count → bond type is confirmed in doc
                fields["day_count"]        = "Actual/360"
                fields["day_count_source"] = "term_sheet"
            elif prospectus_dc:
                prosp_val, prosp_found = prospectus_dc.get("Floating", ("", False))
                fields["day_count"]        = prosp_val if prosp_found else ""
                fields["day_count_source"] = "prospectus" if prosp_found else "not_found"
            else:
                fields["day_count"]        = ""
                fields["day_count_source"] = "not_found"
        elif day_count_fixed:
            fields["day_count"]        = day_count_fixed
            fields["day_count_source"] = "term_sheet"
        elif prospectus_dc:
            prosp_val, prosp_found = prospectus_dc.get("Fixed", ("", False))
            fields["day_count"]        = prosp_val if prosp_found else ""
            fields["day_count_source"] = "prospectus" if prosp_found else "not_found"
        else:
            fields["day_count"]        = ""
            fields["day_count_source"] = "not_found"
        # Shared fields
        fields["trade_date"]      = trade_date
        fields["settlement_date"] = sett_date
        fields["rating_moodys"]   = mdy or ""
        fields["rating_sp"]       = sp  or ""

        # CUSIP & ISIN
        raw_c = find_cusip_labeled(doc_flat, label)
        fields["cusip"] = raw_c.replace(" ", "") if raw_c else ""
        if not raw_c:
            missing.append("cusip")
        fields["isin"] = find_isin_labeled(doc_flat, label) or ""
        if not fields["isin"]:
            missing.append("isin")

        # Per-bond IP dates: try labeled lookup first; fall back to fixed section;
        # final fallback: shared IP section (safe only when it has exactly 2 unique
        # dates — skipped when multiple bond groups are merged into _ip_sec, which
        # would produce 4+ dates and a spurious 'quarterly' classification)
        _ip_val = find_labeled_value(_ip_sec, label)
        if not _ip_val and not is_float and _fixed_flat:
            _ip_val = find_field_section(_fixed_flat, r"Interest Payment Date")
        if not _ip_val and not is_float and _ip_sec:
            _test_freq, _ = _parse_ip_text(_ip_sec)
            if _test_freq == "semi-annual":
                _ip_val = _ip_sec
        _b_freq, _b_ip_dates = _parse_ip_text(_ip_val or "")

        # Rate-specific
        if is_float:
            cf_sec = find_all_field_sections(doc_flat, r"Coupon(?: \(Interest Rate\))?")
            cf_val = find_labeled_value(cf_sec, label) or ""
            fb = re.search(r"\b(SOFR|EURIBOR|CORRA|SONIA)\b", cf_val, re.I)
            fields["floating_benchmark"]  = fb.group(1).upper() if fb else ""
            sp_m = re.search(r"plus\s+(\d+(?:\.\d+)?)\s*%", cf_val, re.I)
            sp_m3 = re.search(r"plus\s+\d+(?:\.\d+)?\s+basis\s+points?\s*\((\d+\.\d+)[)%]{1,2}", cf_val, re.I)
            if sp_m:
                fields["floating_spread_pct"] = sp_m.group(1)
            elif sp_m3:
                fields["floating_spread_pct"] = sp_m3.group(1)
            else:
                sp_m2 = re.search(r"(\d+(?:\.\d+)?)\s*%\s*per\s*ann", cf_val, re.I)
                fields["floating_spread_pct"] = sp_m2.group(1) if sp_m2 else ""
            if not fields["floating_benchmark"]:
                missing.append("floating_benchmark")
            if not fields["floating_spread_pct"]:
                missing.append("floating_spread_pct")
            fields["benchmark_treasury_yield_pct"] = ""
            fields["spread_to_treasury_bps"]       = ""
            fields["yield_pct"]        = ""
            fields["yield_semiannual"] = ""
            fields["yield_annual"]     = ""
            fields["coupon_frequency"]       = _b_freq or doc_coupon_freq
            fields["interest_payment_dates"] = _b_ip_dates
        else:
            # Yield — handle dual format "X% / Y%" (GBP bonds quote semi-annual then annual)
            ys = find_all_field_sections(doc_flat, r"Yield to Maturity(?:\s*\([^)]*\))?")
            yv = find_labeled_value(ys, label)
            if yv:
                dual_m = re.search(r"(\d+\.\d+)%\s*/\s*(\d+\.\d+)%", yv)
                if dual_m:
                    # Semi-Annual / Annual format (GBP bond convention)
                    fields["yield_semiannual"] = dual_m.group(1)
                    fields["yield_annual"]     = dual_m.group(2)
                    fields["yield_pct"]        = dual_m.group(2)  # annual is the standard for annual-pay
                else:
                    ym = re.search(r"(\d+\.\d+)", yv)
                    yval = ym.group(1) if ym else ""
                    fields["yield_pct"] = yval
                    curr = fields.get("currency", "USD")
                    if curr == "EUR":
                        fields["yield_annual"]     = yval
                        fields["yield_semiannual"] = ""
                    else:  # USD, CAD → semi-annual convention
                        fields["yield_semiannual"] = yval
                        fields["yield_annual"]     = ""
            else:
                fields["yield_pct"]        = ""
                fields["yield_semiannual"] = ""
                fields["yield_annual"]     = ""
                missing.append("yield_pct")
            fields["coupon_frequency"]       = _b_freq or doc_coupon_freq
            fields["interest_payment_dates"] = _b_ip_dates

            # Spread
            sps = (find_all_field_sections(doc_flat, r"Spread to(?:\s+Benchmark)?\s+(?:Treasury|Gilt|Bund|GoC|Mid.Swap)")
                   or find_all_field_sections(doc_flat, r"Re-Offer Spread"))
            spv = find_labeled_value(sps, label)
            if spv:
                sm = re.search(r"\+?\s*(\d+(?:\.\d+)?)\s*(?:bps?|basis\s+points?)", spv, re.I)
                fields["spread_to_treasury_bps"] = sm.group(1) if sm else ""
            else:
                fields["spread_to_treasury_bps"] = ""
                missing.append("spread_to_treasury_bps")

            # Benchmark yield — require "Price and/or Yield" to avoid the bare
            # "Benchmark Treasury: UST 4.75%..." label that only names the ref bond.
            # Also covers J&J "Benchmark Security Price and Yield" (EUR bonds).
            # "Price/Yield" (Meta/Broadcom grouped) is matched by "and|/"  separator.
            bps = find_all_field_sections(doc_flat,
                r"Benchmark(?:\s+Treasury|\s+Gilt|\s+Bund|\s+Bond|\s+Security)\s+Price\s*(?:and|/)\s*Yield")
            bpv = find_labeled_value(bps, label)
            if bpv:
                # "XX.XXX% / Y.YYY%" (pct/pct format, some Alphabet EUR)
                b2 = re.search(r"[\d.]+%\s*/\s*(\d+\.\d+)%", bpv)
                # "(yield to maturity: X.XXX%)" (some Apple / older FWPs)
                b1 = re.search(r"\(yield to maturity:\s*(\d+\.\d+)\s*%\)", bpv, re.I)
                # "99-14+ / 4.030%", "99-09+ and 3.655%" (bond-price / or and / yield)
                b3 = re.search(r"(?:/|and)\s*(\d+\.\d+)\s*%", bpv, re.I)
                byv = (b2 or b1 or b3).group(1) if (b2 or b1 or b3) else ""
                fields["benchmark_treasury_yield_pct"] = byv
                if not byv:
                    missing.append("benchmark_treasury_yield_pct")
            else:
                fields["benchmark_treasury_yield_pct"] = ""
                missing.append("benchmark_treasury_yield_pct")

            fields["floating_benchmark"]  = ""
            fields["floating_spread_pct"] = ""

        fields["missing_fields"] = ", ".join(missing)
        rows.append(fields)

    return rows


# ─── Grouped-table extractor (Meta, oldest Broadcom FWP) ─────────────────────
def extract_grouped_labeled(doc_flat, shared_issuer, fwp_date=None, prospectus_dc=None):
    """
    Extract bonds from a grouped-table FWP where every field row lists all
    bonds' values with their bond-label as a prefix, e.g.:
      "Coupon: 2028 Notes: 4.6%  2030 Notes: 4.8%"
      "CUSIP / ISIN: 2028 Notes: 30303M8L9 / US30303M8L96"

    Also normalises year-with-space rendering artefacts before processing
    (e.g. Broadcom Jul 2024 FWP: "20 27 Notes:" → "2027 Notes:").

    Delegates to extract_flat_labeled with the pre-discovered bond-label pairs.
    """
    flat = _normalize_grouped_flat(doc_flat)
    bond_pairs = get_bond_labels_grouped(flat)
    if not bond_pairs:
        return []
    return extract_flat_labeled(flat, shared_issuer,
                                fwp_date=fwp_date,
                                prospectus_dc=prospectus_dc,
                                _bond_pairs=bond_pairs)


# ─── J&J flat-positional extractor ───────────────────────────────────────────
def extract_jj(raw_html, issuer_name="Johnson & Johnson"):
    """
    J&J: bonds listed by position in "Security:" field, all other fields
    have N values in the same order.
    """
    flat = html_to_flat(raw_html)
    rows = []

    # 1. Bond names from "Security:" section
    sec_m = re.search(r"(?i)Security:\s*(.*?){_TERM}".replace("{_TERM}", _TERM), flat, re.DOTALL)
    if not sec_m:
        return []
    sec_txt = sec_m.group(1).strip()
    bond_names = re.findall(r"\d+\.\d+%\s+Notes\s+due\s+\d{4}", sec_txt, re.I)
    n = len(bond_names)
    if n == 0:
        return []

    def nth_value(section_text, idx):
        """Return the idx-th EUR-amount or pct or date value from a positional section."""
        if not section_text:
            return None
        # Try splitting by "€" prefix
        vals = re.findall(r"€[\d,]+(?:,\d{3})*(?:\.\d+)?", section_text)
        if vals and len(vals) >= n:
            return vals[idx]
        return None

    def nth_pct(section_text, idx):
        if not section_text:
            return None
        vals = re.findall(r"\d+\.\d+%", section_text)
        # filter out very small pcts (discount/spread) if many
        large = [v for v in vals if float(v.rstrip("%")) > 1.0]
        if len(large) >= n:
            return large[idx]
        if len(vals) >= n:
            return vals[idx]
        return None

    def nth_date(section_text, idx):
        if not section_text:
            return None
        vals = re.findall(r"[A-Z][a-z]+ \d+, \d{4}", section_text)
        if len(vals) >= n:
            return vals[idx]
        return None

    # Shared fields
    trade_raw = re.search(r"(?i)Trade Date:?\s*(.*?){_TERM}".replace("{_TERM}", _TERM), flat, re.DOTALL)
    trade_raw = trade_raw.group(1).strip() if trade_raw else ""
    trade_date_m = re.search(r"([A-Z][a-z]+ \d+, \d{4})", trade_raw)
    trade_date = trade_date_m.group(1) if trade_date_m else ""

    sett_raw = re.search(r"(?i)Settlement Date[^:]{0,30}:?\s*(.*?){_TERM}".replace("{_TERM}", _TERM), flat, re.DOTALL)
    sett_raw = sett_raw.group(1).strip() if sett_raw else ""
    sett_date_m = re.search(r"([A-Z][a-z]+ \d+, \d{4})", sett_raw)
    sett_date = sett_date_m.group(1) if sett_date_m else ""

    rat_raw = re.search(r"(?i)(?:Expected )?Ratings?:?\s*(.*?){_TERM}".replace("{_TERM}", _TERM), flat, re.DOTALL)
    rat_raw = rat_raw.group(1).strip() if rat_raw else ""
    mdy, sp = parse_ratings(rat_raw)

    dc_raw = re.search(r"(?i)Day Count[^:]*:?\s*(.*?){_TERM}".replace("{_TERM}", _TERM), flat, re.DOTALL)
    dc_raw = dc_raw.group(1).strip() if dc_raw else ""
    # ACT(?:UAL)? handles both "ACT/ACT(ICMA)" and "ACTUAL/ACTUAL (ICMA)" (J&J EUR)
    dc_m = re.search(r"(Actual\s*/\s*\d+|30\s*/\s*360|ACT(?:UAL)?\s*/\s*ACT(?:UAL)?(?:\s*\(ICMA\))?)", dc_raw, re.I)
    raw_dc = dc_m.group(1) if dc_m else dc_raw if dc_raw else ""
    day_count = normalize_day_count(raw_dc)

    # Per-bond sections
    size_sec = find_field_section(flat, r"Size")
    mat_sec  = find_field_section(flat, r"Maturity(?:\s+Date)?")
    cpn_sec  = find_field_section(flat, r"Coupon(?: \(Interest Rate\))?")
    prc_sec  = find_field_section(flat, r"(?:Issue\s+)?Price|Public Offering Price")
    yld_sec  = find_field_section(flat, r"Yield to Maturity|Yield")

    # CUSIPs: listed one per line in order
    cusip_sec = find_field_section(flat, r"CUSIP(?:\s*/\s*(?:Common\s+Code|ISIN))*")
    cusip_all = re.findall(r"[0-9A-Z]{5,6}\s?[0-9A-Z]{2}\s?[0-9A-Z]\b", cusip_sec or "")
    # ISINs: XS-prefixed
    isin_all  = re.findall(r"\b(XS[0-9A-Z]{10})\b", cusip_sec or "")

    for i, name in enumerate(bond_names):
        f = {}
        miss = []

        f["issuer"]    = issuer_name
        f["bond_name"] = name
        f["rate_type"] = "Fixed"
        f["currency"]  = "EUR"
        f["floating_benchmark"]  = ""
        f["floating_spread_pct"] = ""

        cm_name = re.search(r"(\d+\.\d+)%", name)
        f["coupon_pct"] = cm_name.group(1) if cm_name else ""
        if not f["coupon_pct"]:
            miss.append("coupon_pct")

        # Principal
        pav = nth_value(size_sec, i)
        f["principal_amount"] = pav or ""
        if not pav:
            miss.append("principal_amount")

        # Maturity
        mdv = nth_date(mat_sec, i)
        f["maturity_date"] = mdv or ""
        if not mdv:
            miss.append("maturity_date")

        # Price (look for pct near 99-100 range)
        prc_sec2 = find_field_section(flat, r"Issue\s+Price|Price")
        prc_all = re.findall(r"(\d{2,3}\.\d{3})", prc_sec2 or "")
        f["price_to_public_pct"] = prc_all[i] if len(prc_all) >= n and i < len(prc_all) else ""
        if not f["price_to_public_pct"]:
            miss.append("price_to_public_pct")

        # Yield
        yv_all = re.findall(r"(\d+\.\d+)%", yld_sec or "")
        f["yield_pct"] = yv_all[i] if len(yv_all) >= n and i < len(yv_all) else ""
        if not f["yield_pct"]:
            miss.append("yield_pct")
        f["yield_annual"]     = f["yield_pct"]   # J&J EUR bonds pay annually (ICMA)
        f["yield_semiannual"] = ""
        f["coupon_frequency"] = "annual"

        # Spread — J&J EUR uses "Spread to Benchmark Security"; match that BEFORE
        # broader alternatives (Mid-Swap / Treasury) so we get Bund-spread not swap-spread.
        spread_sec = (find_field_section(flat, r"Spread to\s+Benchmark\s+Security")
            or find_field_section(flat, r"Spread to(?:\s+Benchmark)?\s+(?:Treasury|Bund|Gilt|Mid.Swap)")
            or find_field_section(flat, r"Re-Offer Spread|Reoffer Spread"))
        sp_all = re.findall(r"\+?(\d+(?:\.\d+)?)\s*(?:bps?|basis)", spread_sec or "", re.I)
        f["spread_to_treasury_bps"] = sp_all[i] if len(sp_all) >= n and i < len(sp_all) else ""
        if not f["spread_to_treasury_bps"]:
            miss.append("spread_to_treasury_bps")

        # Benchmark yield — J&J EUR uses "Benchmark Security Price and Yield: €XX.XX/ Y.YYY%"
        bps_sec = find_field_section(flat, r"Benchmark(?:\s+Security)?\s+Price\s+and\s+Yield")
        if bps_sec:
            # Each entry: "€XX.XX/ Y.YYY%" — extract yields (after "/")
            bpy_all = re.findall(r"/\s*(\d+\.\d+)\s*%", bps_sec)
            f["benchmark_treasury_yield_pct"] = bpy_all[i] if i < len(bpy_all) else ""
        else:
            f["benchmark_treasury_yield_pct"] = ""
        if not f["benchmark_treasury_yield_pct"]:
            miss.append("benchmark_treasury_yield_pct")

        # Shared
        f["trade_date"]       = trade_date
        f["settlement_date"]  = sett_date
        f["day_count"]        = day_count
        f["day_count_source"] = "term_sheet" if day_count else "not_found"
        f["rating_moodys"]    = mdy or ""
        f["rating_sp"]        = sp  or ""

        # CUSIP/ISIN
        f["cusip"] = cusip_all[i].replace(" ", "") if i < len(cusip_all) else ""
        if not f["cusip"]:
            miss.append("cusip")
        f["isin"] = isin_all[i] if i < len(isin_all) else ""
        if not f["isin"]:
            miss.append("isin")

        f["missing_fields"] = ", ".join(miss)
        rows.append(f)

    return rows


# ─── Alphabet Nov 2026 EUR hybrid extractor ───────────────────────────────────
def extract_alphabet_hybrid_eur(raw_html):
    """Nov 2026 EUR: floating single-bond section + fixed flat_labeled section."""
    flat = html_to_flat(raw_html)

    # Shared
    trade_raw = find_field_section(flat, r"Trade Date")
    trade_m = re.search(r"([A-Z][a-z]+ \d+, \d{4})", trade_raw or "")
    trade_date = trade_m.group(1) if trade_m else ""

    sett_raw = find_field_section(flat, r"Settlement Date[^:]{0,30}")
    sett_m = re.search(r"([A-Z][a-z]+ \d+, \d{4})", sett_raw or "")
    sett_date = sett_m.group(1) if sett_m else ""

    rat_raw = find_field_section(flat, r"(?:Expected )?Ratings?(?:\*{0,2})?")
    mdy, sp = parse_ratings(rat_raw)

    rows = []

    # ── 1. Floating Rate Notes ────────────────────────────────────────────────
    # Find the section between "Floating Rate Notes" and "Fixed Rate Notes"
    float_m = re.search(r"Floating Rate Notes\s+(.*?)(?=Fixed Rate Notes|\Z)", flat, re.DOTALL | re.I)
    if float_m:
        fsec = float_m.group(1)
        f = {}
        miss = []
        # Bond name: first "Floating Rate Notes due YYYY"
        fn_m = re.search(r"(Floating\s+Rate\s+Notes\s+due\s+\d{4})", flat, re.I)
        f["bond_name"] = fn_m.group(1) if fn_m else "Floating Rate Notes"
        f["issuer"] = "Alphabet Inc."
        f["rate_type"] = "Floating"
        f["coupon_pct"] = ""
        f["currency"] = "EUR"

        # Principal: search for the labeled "Aggregate Principal Amount:" field.
        # Must NOT use "first € number" — the preamble contains Denominations: €100,000.
        pa_m = re.search(r"Aggregate Principal Amount[^:]*:\s*(€[\d,]+)", fsec, re.I)
        f["principal_amount"] = pa_m.group(1) if pa_m else ""
        if not f["principal_amount"]:
            miss.append("principal_amount")

        mat_m2 = re.search(r"Maturity Date[^:]*:\s*([A-Z][a-z]+ \d+, \d{4})", fsec, re.I)
        f["maturity_date"] = mat_m2.group(1) if mat_m2 else ""
        if not f["maturity_date"]:
            miss.append("maturity_date")

        prc_m = re.search(r"(\d{2,3}\.\d{3})%", fsec)
        f["price_to_public_pct"] = prc_m.group(1) if prc_m else ""
        if not f["price_to_public_pct"]:
            miss.append("price_to_public_pct")

        # EURIBOR
        fb = re.search(r"\b(EURIBOR)\b", fsec, re.I)
        f["floating_benchmark"] = fb.group(1).upper() if fb else ""
        if not f["floating_benchmark"]:
            miss.append("floating_benchmark")

        fs_m2 = re.search(r"plus\s+(\d+(?:\.\d+)?)\s*%", fsec, re.I)
        f["floating_spread_pct"] = fs_m2.group(1) if fs_m2 else ""
        if not f["floating_spread_pct"]:
            miss.append("floating_spread_pct")

        # CUSIP/ISIN from floating section
        cc = _CUSIP_RE.search(fsec)
        f["cusip"] = cc.group(1).replace(" ", "") if cc else ""
        if not f["cusip"]:
            miss.append("cusip")
        f["isin"] = find_isin(fsec) or ""
        if not f["isin"]:
            miss.append("isin")

        dc_m2 = re.search(r"(Actual\s*/\s*\d+|30\s*/\s*360)", fsec, re.I)
        f["day_count"]        = normalize_day_count(dc_m2.group(1) if dc_m2 else "Actual/360")
        f["day_count_source"] = "term_sheet"

        f["trade_date"]        = trade_date
        f["settlement_date"]   = sett_date
        f["rating_moodys"]     = mdy or ""
        f["rating_sp"]         = sp  or ""
        f["benchmark_treasury_yield_pct"] = ""
        f["spread_to_treasury_bps"]       = ""
        f["yield_pct"]         = ""
        f["yield_semiannual"]  = ""
        f["yield_annual"]      = ""
        f["coupon_frequency"]  = "annual"   # EUR floating notes pay annually
        f["missing_fields"] = ", ".join(miss)
        rows.append(f)

    # ── 2. Fixed Rate Notes (flat_labeled section) ────────────────────────────
    # Get bond labels from the flat text "Title:" section
    bond_pairs = get_bond_labels(flat)
    # Keep only fixed bonds (floating already handled)
    fixed_pairs = [(n, l) for n, l in bond_pairs if "Floating" not in n]

    # Fixed section starts after "Fixed Rate Notes"
    fixed_m = re.search(r"Fixed Rate Notes\s+(.*)", flat, re.DOTALL | re.I)
    fixed_flat = fixed_m.group(1) if fixed_m else flat

    # Day count for fixed bonds
    dc_raw = re.search(r"Day Count[^:]*:\s*(.*?){_TERM}".replace("{_TERM}", _TERM), fixed_flat, re.DOTALL | re.I)
    dc_raw = dc_raw.group(1).strip() if dc_raw else ""
    dc_m = re.search(r"(Actual\s*/\s*\d+|30\s*/\s*360|ACT\s*/\s*ACT(?:\s*\(ICMA\))?)", dc_raw, re.I)
    day_count = normalize_day_count(dc_m.group(1) if dc_m else "Actual/Actual (ICMA)")

    for full_name, label in fixed_pairs:
        f = {}
        miss = []

        f["issuer"] = "Alphabet Inc."
        f["bond_name"] = full_name
        f["rate_type"] = "Fixed"
        f["currency"] = "EUR"

        cm_n = re.search(r"(\d+\.\d+)%", full_name)
        f["coupon_pct"] = cm_n.group(1) if cm_n else ""
        if not f["coupon_pct"]:
            miss.append("coupon_pct")

        pa_sec = find_field_section(fixed_flat, r"Aggregate Principal Amount")
        pa_v = find_labeled_value(pa_sec, label)
        f["principal_amount"] = pa_v or ""
        if not f["principal_amount"]:
            miss.append("principal_amount")

        mat_s = find_field_section(fixed_flat, r"Maturity(?:\s+Date)?")
        mat_v = find_labeled_value(mat_s, label)
        if mat_v:
            md = re.search(r"([A-Z][a-z]+ \d+, \d{4})", mat_v)
            f["maturity_date"] = md.group(1) if md else ""
        else:
            f["maturity_date"] = ""
            miss.append("maturity_date")

        prc_s = find_field_section(fixed_flat, r"Public Offering Price")
        prc_v = find_labeled_value(prc_s, label)
        if prc_v:
            pm = re.search(r"(\d+\.\d+)", prc_v)
            f["price_to_public_pct"] = pm.group(1) if pm else ""
        else:
            f["price_to_public_pct"] = ""
            miss.append("price_to_public_pct")

        ys = find_field_section(fixed_flat, r"Yield to Maturity")
        yv = find_labeled_value(ys, label)
        if yv:
            ym = re.search(r"(\d+\.\d+)", yv)
            f["yield_pct"] = ym.group(1) if ym else ""
        else:
            f["yield_pct"] = ""
            miss.append("yield_pct")

        sps = find_field_section(fixed_flat, r"Spread to(?:\s+Benchmark)?\s+(?:Bund|Treasury|Gilt)|Re-Offer Spread")
        spv = find_labeled_value(sps, label)
        if spv:
            sm = re.search(r"\+?\s*(\d+(?:\.\d+)?)\s*(?:bps?|basis)", spv, re.I)
            f["spread_to_treasury_bps"] = sm.group(1) if sm else ""
        else:
            f["spread_to_treasury_bps"] = ""
            miss.append("spread_to_treasury_bps")

        # Require "Price and Yield" to skip the bare "Benchmark Bund: BKO..." name label
        bps2 = find_field_section(fixed_flat,
            r"Benchmark(?:\s+Bund|\s+Treasury|\s+Gilt)\s+Price\s+and\s+Yield")
        bpv = find_labeled_value(bps2, label)
        if bpv:
            b2 = re.search(r"[\d.]+%\s*/\s*(\d+\.\d+)%", bpv)
            f["benchmark_treasury_yield_pct"] = b2.group(1) if b2 else ""
            if not f["benchmark_treasury_yield_pct"]:
                miss.append("benchmark_treasury_yield_pct")
        else:
            f["benchmark_treasury_yield_pct"] = ""
            miss.append("benchmark_treasury_yield_pct")

        # Yield — EUR fixed bonds quote a single annual yield
        ys2 = find_field_section(fixed_flat, r"Yield to Maturity")
        yv2 = find_labeled_value(ys2, label)
        if yv2:
            ym2 = re.search(r"(\d+\.\d+)", yv2)
            yval = ym2.group(1) if ym2 else ""
            f["yield_pct"]        = yval
            f["yield_annual"]     = yval
            f["yield_semiannual"] = ""
        else:
            f["yield_pct"]        = ""
            f["yield_annual"]     = ""
            f["yield_semiannual"] = ""
            miss.append("yield_pct")

        f["coupon_frequency"] = "annual"   # EUR fixed notes pay annually

        # CUSIP/ISIN
        rc = find_cusip_labeled(fixed_flat, label)
        f["cusip"] = rc.replace(" ", "") if rc else ""
        if not f["cusip"]:
            miss.append("cusip")
        f["isin"] = find_isin_labeled(fixed_flat, label) or ""
        if not f["isin"]:
            miss.append("isin")

        f["trade_date"]       = trade_date
        f["settlement_date"]  = sett_date
        f["day_count"]        = day_count
        f["day_count_source"] = "term_sheet" if day_count else "not_found"
        f["rating_moodys"]    = mdy or ""
        f["rating_sp"]        = sp  or ""
        f["floating_benchmark"]  = ""
        f["floating_spread_pct"] = ""
        f["missing_fields"] = ", ".join(miss)
        rows.append(f)

    return rows


# ─── Alphabet format detector ─────────────────────────────────────────────────
def detect_alpha_format(raw_html, flat):
    """Return 'block_b', 'hybrid_eur', or 'flat_labeled'."""
    # block_b: individual <b> bond headings
    b_texts = [html_to_flat(m.group(1))
               for m in re.finditer(r"<b[^>]*>(.*?)</b>", raw_html, re.DOTALL | re.I)]
    if any(_ALPHA_B_HDG.search(t) for t in b_texts):
        return "block_b"
    # hybrid_eur: "Aggregate Principal Amount:" immediately followed by € (single bond)
    m1 = re.search(r"Aggregate Principal Amount:\s*€", flat, re.I)
    m2 = re.search(r"Aggregate Principal Amount:\s*\d{4}", flat, re.I)
    if m1 and (not m2 or m1.start() < m2.start()):
        return "hybrid_eur"
    return "flat_labeled"


# ─── Company processors ───────────────────────────────────────────────────────
def process_apple(docs_df):
    """Load Apple bonds from existing CSV; ensure currency column."""
    df = pd.read_csv(APPLE_CSV)
    if "currency" not in df.columns:
        df["currency"] = "USD"
    df["issuer"] = "Apple"
    # Apple FWPs always carry "Day Count Convention:" — mark all as term_sheet
    df["day_count_source"] = df["day_count"].apply(
        lambda v: "term_sheet" if str(v).strip() else "not_found")
    df["day_count"]        = df["day_count"].apply(normalize_day_count)
    df["coupon_frequency"] = "semi-annual"           # Apple = USD; USD pays semi-annual
    df["yield_semiannual"] = df["yield_pct"].apply(  # semi-annual yield = yield_pct for USD
        lambda v: "" if pd.isna(v) else str(v))
    df["yield_annual"]     = ""
    # Map each bond to its FWP accession via the deal year (Apple has one deal per year)
    apple_deals = docs_df[docs_df["issuer"] == "Apple"].dropna(subset=["fwp_accession"])
    year_to_acc = {str(d["deal_date"])[:4]: str(d["fwp_accession"])
                   for _, d in apple_deals.iterrows()}
    def _apple_acc(td_str):
        m = re.search(r'\b(\d{4})\b', str(td_str))
        return year_to_acc.get(m.group(1), "") if m else ""
    df["fwp_accession"] = df["trade_date"].apply(_apple_acc)
    # Extract interest_payment_dates from Apple FWP HTML (all bonds in a deal share dates)
    _ip_cache: dict = {}
    def _apple_ip(acc):
        if not acc:
            return ""
        if acc not in _ip_cache:
            raw = load_html("Apple", acc)
            flat = html_to_flat(raw) if raw else ""
            ip_raw = find_field_section(flat, r"Interest Payment Date")
            _, ip_str = _parse_ip_text(ip_raw or "")
            _ip_cache[acc] = ip_str
        return _ip_cache[acc]
    df["interest_payment_dates"] = df["fwp_accession"].apply(_apple_ip)
    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    rows = df[OUTPUT_COLUMNS].to_dict("records")
    print(f"  Apple:  {len(rows)} bonds (from {APPLE_CSV.name})")
    return rows


def process_eli_lilly(docs):
    deals = docs[docs["issuer"] == "Eli Lilly"].dropna(subset=["fwp_accession"])
    rows = []
    print(f"  Eli Lilly: {len(deals)} FWP(s)")
    for _, deal in deals.iterrows():
        raw = load_html("Eli Lilly", deal["fwp_accession"])
        if not raw:
            print(f"    SKIP — file not found: {deal['fwp_accession']}")
            continue
        preamble_html, blocks = split_blocks(raw, _LILLY_HDG, tag="u")
        if not blocks:
            print(f"    WARN no <u> headings in {deal['fwp_accession']}")
            continue
        preamble_flat = html_to_flat(preamble_html)
        # Only activate block-first searching when the preamble itself contains a
        # CUSIP — that signals a full prior-bond section landed in the preamble
        # (e.g. FWP 0001193125-25-182780 where the Floating Rate Notes have no
        # <u> heading).  For normal FWPs the preamble is just the common header
        # and block-first searching would cause settlement_date to be taken from
        # a relative "T+5" in the block rather than the absolute date in the header.
        preamble_has_cusip = bool(_CUSIP_RE.search(preamble_flat))
        # Pre-load prospectus day count for bonds whose term sheet lacks the label
        prosp_dc = lookup_day_count_from_prospectus("Eli Lilly", deal["fwp_accession"])
        print(f"    {deal['fwp_accession']}  → {len(blocks)} bonds")
        for blk in blocks:
            blk_flat = html_to_flat(blk["block_html"])
            flat_text = preamble_flat + " " + blk_flat
            blk_text_arg = blk_flat if preamble_has_cusip else None
            fields, miss = extract_fields_block(flat_text, blk["heading"], block_text=blk_text_arg)
            # Fill day_count from 424B2 when not found in term sheet
            if not fields.get("day_count"):
                rate_key = "Floating" if fields.get("rate_type") == "Floating" else "Fixed"
                prosp_val, prosp_found = prosp_dc.get(rate_key, ("", False))
                fields["day_count"]        = prosp_val if prosp_found else ""
                fields["day_count_source"] = "prospectus" if prosp_found else "not_found"
            fields["fwp_accession"]  = str(deal["fwp_accession"])
            fields["missing_fields"] = ", ".join(miss)
            rows.append(fields)
    return rows


def process_microsoft(docs):
    deals = docs[docs["issuer"] == "Microsoft"].dropna(subset=["fwp_accession"])
    rows = []
    print(f"  Microsoft: {len(deals)} FWP(s)")
    for _, deal in deals.iterrows():
        raw = load_html("Microsoft", deal["fwp_accession"])
        if not raw:
            print(f"    SKIP — file not found: {deal['fwp_accession']}")
            continue
        flat = html_to_flat(raw)
        prosp_dc = lookup_day_count_from_prospectus("Microsoft", deal["fwp_accession"])
        bond_rows = extract_flat_labeled(flat, "Microsoft Corporation", prospectus_dc=prosp_dc)
        for r in bond_rows:
            r["fwp_accession"] = str(deal["fwp_accession"])
        print(f"    {deal['fwp_accession']}  → {len(bond_rows)} bonds")
        rows.extend(bond_rows)
    return rows


def process_alphabet(docs):
    deals = docs[docs["issuer"] == "Alphabet"].dropna(subset=["fwp_accession"])
    # Deduplicate by accession (multiple 424B2 may share one FWP)
    seen = set()
    rows = []
    print(f"  Alphabet: processing unique FWPs")
    for _, deal in deals.iterrows():
        acc = deal["fwp_accession"]
        if acc in seen:
            continue
        seen.add(acc)
        raw = load_html("Alphabet", acc)
        if not raw:
            print(f"    SKIP — file not found: {acc}")
            continue
        flat = html_to_flat(raw)
        fmt = detect_alpha_format(raw, flat)
        print(f"    {acc}  format={fmt}")

        if fmt == "block_b":
            # For block_b, fixed-bond HTML blocks are empty (~110 chars each);
            # all labeled per-bond data lives in the preamble's flat-labeled sections.
            # Use extract_flat_labeled on the full flat text.
            bond_rows = extract_flat_labeled(flat, "Alphabet Inc.")
            for r in bond_rows:
                r["fwp_accession"] = acc
            print(f"      → {len(bond_rows)} bonds (block_b → flat_labeled)")
            rows.extend(bond_rows)

        elif fmt == "hybrid_eur":
            bond_rows = extract_alphabet_hybrid_eur(raw)
            for r in bond_rows:
                r["fwp_accession"] = acc
            print(f"      → {len(bond_rows)} bonds (hybrid_eur)")
            rows.extend(bond_rows)

        else:  # flat_labeled
            bond_rows = extract_flat_labeled(flat, "Alphabet Inc.")
            for r in bond_rows:
                r["fwp_accession"] = acc
            print(f"      → {len(bond_rows)} bonds (flat_labeled)")
            rows.extend(bond_rows)

    return rows


def process_jj(docs):
    deals = docs[docs["issuer"] == "Johnson & Johnson"].dropna(subset=["fwp_accession"])
    rows = []
    seen = set()
    print(f"  J&J: processing unique FWPs")
    for _, deal in deals.iterrows():
        acc = deal["fwp_accession"]
        if acc in seen:
            continue
        seen.add(acc)
        raw = load_html("Johnson & Johnson", acc)
        if not raw:
            print(f"    SKIP — file not found: {acc}")
            continue
        bond_rows = extract_jj(raw)
        for r in bond_rows:
            r["fwp_accession"] = acc
        print(f"    {acc}  → {len(bond_rows)} bonds")
        rows.extend(bond_rows)
    return rows


# ─── Main ─────────────────────────────────────────────────────────────────────
docs = pd.read_csv(DOCS_CSV)
# Keep only build set (not test)
build = docs[docs["set"] == "build"].copy()

print("=" * 70)
print("Build-set FWP extractor")
print("=" * 70)

all_rows = []
all_rows.extend(process_apple(build))
all_rows.extend(process_eli_lilly(build))
all_rows.extend(process_microsoft(build))
all_rows.extend(process_alphabet(build))
all_rows.extend(process_jj(build))

# ── Assemble output ───────────────────────────────────────────────────────────
out_df = pd.DataFrame(all_rows)
# Ensure all columns present in correct order
for col in OUTPUT_COLUMNS:
    if col not in out_df.columns:
        out_df[col] = ""
out_df = out_df[OUTPUT_COLUMNS]
# Fill NaN (can appear from Apple CSV empty strings loaded as float NaN)
out_df = out_df.fillna("")

OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
out_df.to_csv(OUT_CSV, index=False)
print(f"\nSaved {OUT_CSV}  ({len(out_df)} total bonds)")

# ── Per-company summary ───────────────────────────────────────────────────────
print("\n" + "=" * 70)
print(f"{'Issuer':<24}  {'Bonds':>5}  {'All fields':>10}  {'Most missing'}")
print("-" * 70)
for issuer, grp in out_df.groupby("issuer"):
    n_total = len(grp)
    n_complete = (grp["missing_fields"] == "").sum()
    # Count which fields are most often missing
    all_miss = []
    for mf in grp["missing_fields"]:
        if mf:
            all_miss.extend([f.strip() for f in mf.split(",") if f.strip()])
    from collections import Counter
    top_miss = Counter(all_miss).most_common(3)
    top_str = ", ".join(f"{f}({c})" for f, c in top_miss) if top_miss else "—"
    print(f"  {issuer:<22}  {n_total:>5}  {n_complete:>8}/{n_total:<2}  {top_str}")

print("\nAll bonds:")
disp_cols = ["issuer", "bond_name", "rate_type", "coupon_pct",
             "principal_amount", "maturity_date", "cusip", "missing_fields"]
pd.set_option("display.max_colwidth", 50)
pd.set_option("display.width", 160)
print(out_df[disp_cols].to_string(index=False))

# ── day_count_source summary ──────────────────────────────────────────────────
_co_map = {
    "Apple": "Apple", "Alphabet Inc.": "Alphabet",
    "Eli Lilly and Company": "Eli Lilly",
    "Johnson & Johnson": "J&J", "Microsoft Corporation": "Microsoft",
}
out_df["_co"] = out_df["issuer"].map(_co_map).fillna(out_df["issuer"])
src_counts = (
    out_df.groupby(["_co", "day_count_source"]).size()
    .unstack(fill_value=0)
    .reindex(columns=["term_sheet", "prospectus", "not_found"], fill_value=0)
)
print("\nday_count_source counts per company:")
print(src_counts.to_string())
out_df.drop(columns=["_co"], inplace=True)
