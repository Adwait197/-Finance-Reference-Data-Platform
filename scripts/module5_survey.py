#!/usr/bin/env python3
"""
Module 5 — data availability survey (DO NOT COMMIT yet).
Counts 424B2 prospectus filings on EDGAR for the 15 largest non-financial IVV
holdings (by valUSD, latest quarter).  Checks the most-recent filing's first
8 KB for plain fixed-rate vs. structured signals.
Output: data/processed/module5_availability.csv
"""
import os, re, time, json, urllib.request
from datetime import date
import pandas as pd
import sqlalchemy
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

USER_AGENT = "Adwait Gaur adwaitgaur197@gmail.com"
PAUSE      = 0.11           # ~9 req/s — stays under SEC's 10 req/s limit
START_DATE = "2023-01-01"
END_DATE   = date.today().isoformat()
OUT_CSV    = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "processed",
                 "module5_availability.csv")
)

# ── financial name fragments → skip these holdings ───────────────────────────
FINANCIAL_FRAGS = [
    "bank", "bancorp", " financial", "finance", "insurance",
    "berkshire", "jpmorgan", "goldman sachs", "morgan stanley",
    "citigroup", "wells fargo", "visa", "mastercard",
    "american express", "intercontinental exchange",
    "cboe", "cme group", "moody", "s&p global",
    "charles schwab", "ameriprise", "aflac", "travelers",
    "allstate", "metlife", "lincoln national", "principal financial",
    "unum", "hartford",
]

# ── content signals in the prospectus first 8 KB ─────────────────────────────
PLAIN_SIGNALS      = ["% Notes due", "% notes due", "fixed rate", "Fixed Rate",
                       "Fixed-Rate", "Fixed-rate"]
STRUCTURED_SIGNALS = ["linked to", "Linked to", "autocallable", "Autocallable",
                       "contingent coupon", "Contingent Coupon",
                       "market linked", "Market Linked",
                       "barrier", "Knockout", "knockout"]


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _headers():
    return {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}


def get_json(url: str):
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    except Exception as exc:
        print(f"    WARN json {url[:70]!r} -> {exc}")
        return None


def get_text_8k(url: str) -> str:
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.read(8192).decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"    WARN text {url[:70]!r} -> {exc}")
        return ""


# ── name normalisation for CIK matching ──────────────────────────────────────
_STRIP_SUFFIXES = [
    " INCORPORATED", " INC", " CORPORATION", " CORP", " COMPANY", " CO",
    " LIMITED", " LTD", " LLC", " GROUP", " HOLDINGS", " INTERNATIONAL",
    " INTL", " TECHNOLOGIES", " TECHNOLOGY", " SCIENCES", " SCIENCE",
    " SYSTEMS", " SOLUTIONS", " SERVICES", " ENTERPRISES",
]

def normalise(name: str) -> str:
    n = name.upper()
    n = re.sub(r"\bCL(?:ASS)?\s*[ABC]\b", "", n)   # strip class designations
    n = re.sub(r"[,.]", " ", n)                     # commas/periods -> space
    n = re.sub(r"\s+", " ", n).strip()
    for sfx in _STRIP_SUFFIXES:
        if n.endswith(sfx):
            n = n[: -len(sfx)].strip()
    return n


def is_financial(name: str) -> bool:
    low = name.lower()
    return any(f in low for f in FINANCIAL_FRAGS)


# ── 1. top 15 non-financial holdings ─────────────────────────────────────────
print("Querying security_master_history (latest quarter) …")
engine = sqlalchemy.create_engine(
    "postgresql+psycopg2://{user}:{pw}@{host}:{port}/{db}".format(
        user=os.getenv("PGUSER"), pw=os.getenv("PGPASSWORD"),
        host=os.getenv("PGHOST"), port=os.getenv("PGPORT"),
        db=os.getenv("PGDATABASE"),
    )
)
with engine.connect() as conn:
    raw = pd.read_sql("""
        SELECT name, cusip, assetcat, valusd
        FROM   security_master_history
        WHERE  period_end_date = (SELECT MAX(period_end_date)
                                   FROM security_master_history)
          AND  assetcat NOT IN ('STIV', 'DE')
          AND  valusd    > 0
        ORDER  BY valusd DESC
        LIMIT  100
    """, conn)

raw["financial"] = raw["name"].apply(is_financial)
top15 = raw[~raw["financial"]].head(15).reset_index(drop=True)
print(f"\nTop 15 non-financial holdings (latest quarter, filtered):")
for i, r in top15.iterrows():
    print(f"  {i+1:2d}. {r['name']:<42}  valUSD {r['valusd']:>14,.0f}  cusip={r['cusip']}")


# ── 2. load EDGAR company-tickers → CIK map ──────────────────────────────────
print("\nLoading EDGAR company tickers map …")
time.sleep(PAUSE)
tickers_raw = get_json("https://www.sec.gov/files/company_tickers.json")
cik_by_norm: dict[str, str] = {}
if tickers_raw:
    for entry in tickers_raw.values():
        cik_by_norm[normalise(entry["title"])] = str(entry["cik_str"]).zfill(10)
    print(f"  {len(cik_by_norm):,} entries loaded.")
else:
    print("  FAILED — CIK lookups will fail.")


def find_cik(company_name: str) -> tuple[str | None, str]:
    """Return (CIK, match_note).  None if not found."""
    norm = normalise(company_name)
    if norm in cik_by_norm:
        return cik_by_norm[norm], "exact"
    words = norm.split()
    for n in range(min(4, len(words)), 1, -1):
        prefix = " ".join(words[:n])
        matches = [(k, v) for k, v in cik_by_norm.items() if k.startswith(prefix)]
        if len(matches) == 1:
            return matches[0][1], f"prefix '{prefix}' -> {matches[0][0]!r}"
    return None, "not found"


# ── 3. EDGAR submissions → 424B2 filings ─────────────────────────────────────
results = []

for _, row in top15.iterrows():
    name  = row["name"]
    cusip = str(row["cusip"]) if pd.notna(row["cusip"]) else ""
    val   = row["valusd"]
    print(f"\n{'-'*62}")
    print(f"  {name}  (cusip={cusip})")

    cik, match_note = find_cik(name)
    if not cik:
        print(f"  [!] CIK not found")
        results.append(dict(name=name, cusip=cusip, val_usd=int(val), cik="",
            edgar_name="", count_424b2="n/a", recent_1="", recent_2="", recent_3="",
            most_recent_url="", plain_fixed_rate="", structured_flag="",
            notes="CIK not found in company_tickers.json"))
        continue

    sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    time.sleep(PAUSE)
    sub = get_json(sub_url)
    if not sub:
        results.append(dict(name=name, cusip=cusip, val_usd=int(val), cik=cik,
            edgar_name="", count_424b2="n/a", recent_1="", recent_2="", recent_3="",
            most_recent_url="", plain_fixed_rate="", structured_flag="",
            notes="submissions fetch failed"))
        continue

    edgar_name = sub.get("name", "")
    print(f"  CIK={cik}  EDGAR name={edgar_name!r}  match={match_note}")

    recent = sub.get("filings", {}).get("recent", {})
    b2 = sorted(
        [(d, a)
         for f, d, a in zip(
             recent.get("form", []),
             recent.get("filingDate", []),
             recent.get("accessionNumber", []),
         )
         if f == "424B2" and d >= START_DATE],
        reverse=True,
    )
    count = len(b2)

    # note if the submissions JSON has older-filings pages not yet fetched
    older_pages = sub.get("filings", {}).get("files", [])
    note = (f"+ {len(older_pages)} older pages not fetched in survey"
            if older_pages else "recent window covers all filings")

    print(f"  424B2 since {START_DATE}: {count}   ({note})")
    d1, d2, d3 = (b2[0][0] if len(b2) > 0 else ""), \
                 (b2[1][0] if len(b2) > 1 else ""), \
                 (b2[2][0] if len(b2) > 2 else "")

    # check content of most-recent filing
    plain_flag = struct_flag = doc_url = ""
    if b2:
        acc_raw = b2[0][1]                       # e.g. "0000789019-24-012345"
        acc_dir = acc_raw.replace("-", "")        # directory: no dashes
        cik_n   = cik.lstrip("0") or "0"
        idx_url = (f"https://www.sec.gov/Archives/edgar/data"
                   f"/{cik_n}/{acc_dir}/{acc_raw}-index.htm")
        time.sleep(PAUSE)
        idx_text = get_text_8k(idx_url)

        # find first .htm that isn't the index page itself
        for m in re.findall(
            r'href="(/Archives/edgar/data/[^"]+\.htm)"', idx_text, re.I
        ):
            if acc_dir.lower() not in m.lower():
                doc_url = "https://www.sec.gov" + m
                break
        if not doc_url:  # fallback: any .htm in the index
            for m in re.findall(
                r'href="(/Archives/edgar/data/[^"]+\.htm)"', idx_text, re.I
            ):
                doc_url = "https://www.sec.gov" + m
                break

        if doc_url:
            time.sleep(PAUSE)
            doc_text = get_text_8k(doc_url)
            plain_flag  = str(any(p in doc_text for p in PLAIN_SIGNALS))
            struct_flag = str(any(s in doc_text for s in STRUCTURED_SIGNALS))
            print(f"  doc: …{doc_url[-60:]}")
            print(f"  plain_fixed_rate={plain_flag}  structured_flag={struct_flag}")
        else:
            print("  [!] could not parse primary doc URL from filing index")

    results.append(dict(
        name=name, cusip=cusip, val_usd=int(val), cik=cik,
        edgar_name=edgar_name, count_424b2=count,
        recent_1=d1, recent_2=d2, recent_3=d3,
        most_recent_url=doc_url, plain_fixed_rate=plain_flag,
        structured_flag=struct_flag, notes=note,
    ))


# ── 4. save + print summary ───────────────────────────────────────────────────
out_df = pd.DataFrame(results)
os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
out_df.to_csv(OUT_CSV, index=False)

print(f"\n{'='*70}")
print("SURVEY RESULTS")
print(f"{'='*70}")
print(out_df[["name", "cusip", "count_424b2", "recent_1", "recent_2",
              "recent_3", "plain_fixed_rate", "structured_flag"]].to_string(index=False))
print(f"\nSaved: {OUT_CSV}")
