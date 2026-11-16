import requests

HEADERS = {"User-Agent": "Adwait Gaur adwaitgaur197@gmail.com"}
TARGET = "IVV"


def get_company_name(cik: int) -> str:
    """Fetch the registrant name from the EDGAR submissions API."""
    cik_padded = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json().get("name", "Unknown")


# --- 1. Try operating-company registry first (stocks, closed-end funds) ---
r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=HEADERS)
r.raise_for_status()
match = next(
    (e for e in r.json().values() if e["ticker"].upper() == TARGET.upper()),
    None,
)
if match:
    cik = match["cik_str"]
    title = match["title"]
else:
    # --- 2. Fall back to mutual-fund / ETF registry ---
    # Fields: [cik, seriesId, classId, symbol]
    r2 = requests.get("https://www.sec.gov/files/company_tickers_mf.json", headers=HEADERS)
    r2.raise_for_status()
    mf_data = r2.json()
    SYM_IDX = mf_data["fields"].index("symbol")
    CIK_IDX = mf_data["fields"].index("cik")
    row = next(
        (row for row in mf_data["data"] if row[SYM_IDX] and row[SYM_IDX].upper() == TARGET.upper()),
        None,
    )
    if row is None:
        raise ValueError(f"Ticker '{TARGET}' not found in either SEC ticker registry.")
    cik = row[CIK_IDX]
    title = get_company_name(cik)

cik_padded = str(cik).zfill(10)
print(f"Ticker : {TARGET.upper()}")
print(f"Company: {title}")
print(f"CIK    : {cik_padded}")
