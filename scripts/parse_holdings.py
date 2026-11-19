import os
import xml.etree.ElementTree as ET
import pandas as pd

XML_PATH = "data/raw/NPORT_IVV_2026-08-25_0002071691-26-019760.xml"
OUT_PATH = "data/processed/ivv_holdings_2026-08-25.csv"
NS = "http://www.sec.gov/edgar/nport"
N = f"{{{NS}}}"   # shorthand: N + "tagName" == fully-qualified tag

os.makedirs("data/processed", exist_ok=True)

tree = ET.parse(XML_PATH)
root = tree.getroot()

holdings_parent = root.find(f"{N}formData/{N}invstOrSecs")
if holdings_parent is None:
    raise RuntimeError("Could not find formData/invstOrSecs in XML.")

def txt(el, tag):
    """Return stripped text of a direct child element, or None if absent."""
    child = el.find(f"{N}{tag}")
    return child.text.strip() if child is not None and child.text else None


def txt_or_cond(el, tag, cond_tag, attr):
    """
    Try direct child element text first, then fall back to an attribute on a
    conditional sibling element.  Used for assetCat / issuerCat which the
    N-PORT schema sometimes encodes as <assetConditional assetCat="..."/> or
    <issuerConditional issuerCat="..."/> instead of plain text children.
    """
    val = txt(el, tag)
    if val:
        return val
    cond = el.find(f"{N}{cond_tag}")
    if cond is not None:
        v = cond.get(attr)
        return v.strip() if v else None
    return None


rows = []
for sec in holdings_parent.findall(f"{N}invstOrSec"):
    # isin lives as an attribute on <identifiers><isin value="..."/></identifiers>
    identifiers = sec.find(f"{N}identifiers")
    isin_el = identifiers.find(f"{N}isin") if identifiers is not None else None
    isin = isin_el.get("value") if isin_el is not None else None

    # isCashCollateral lives under <securityLending>
    sec_lending = sec.find(f"{N}securityLending")
    is_cash_collateral = txt(sec_lending, "isCashCollateral") if sec_lending is not None else None

    rows.append({
        "name":              txt(sec, "name"),
        "lei":               txt(sec, "lei"),
        "title":             txt(sec, "title"),
        "cusip":             txt(sec, "cusip"),
        "isin":              isin,
        "balance":           txt(sec, "balance"),
        "units":             txt(sec, "units"),
        "curCd":             txt(sec, "curCd"),
        "valUSD":            txt(sec, "valUSD"),
        "pctVal":            txt(sec, "pctVal"),
        "payoffProfile":     txt(sec, "payoffProfile"),
        "assetCat":          txt_or_cond(sec, "assetCat",  "assetConditional",  "assetCat"),
        "issuerCat":         txt_or_cond(sec, "issuerCat", "issuerConditional", "issuerCat"),
        "invCountry":        txt(sec, "invCountry"),
        "isRestrictedSec":   txt(sec, "isRestrictedSec"),
        "fairValLevel":      txt(sec, "fairValLevel"),
        "isCashCollateral":  is_cash_collateral,
    })

df = pd.DataFrame(rows)

# Cast numeric columns from string
df["balance"] = pd.to_numeric(df["balance"], errors="coerce")
df["valUSD"]  = pd.to_numeric(df["valUSD"],  errors="coerce")
df["pctVal"]  = pd.to_numeric(df["pctVal"],  errors="coerce")

# Sanity checks
print(f"Total holdings : {len(df)}")
print(f"Sum of pctVal  : {df['pctVal'].sum():.6f}  (expect ~1.0 for a fully-invested fund)")
print(f"Sum of valUSD  : ${df['valUSD'].sum():,.2f}")
print()
print(df[["name", "cusip", "isin", "valUSD", "pctVal"]].head(5).to_string(index=False))

df.to_csv(OUT_PATH, index=False)
print(f"\nSaved {len(df)} rows → {OUT_PATH}")
