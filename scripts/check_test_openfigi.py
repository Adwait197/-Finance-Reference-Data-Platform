#!/usr/bin/env python3
"""
Module 5 — OpenFIGI verification for the test-set bonds.

For each bond in module5_test_tranches.csv, queries OpenFIGI by CUSIP
and compares coupon and maturity against what the extractor produced.

Output: data/processed/module5_test_openfigi.csv
"""
import json, time, re
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

ROOT    = Path(__file__).parent.parent
IN_CSV  = ROOT / "data" / "processed" / "module5_test_tranches.csv"
OUT_CSV = ROOT / "data" / "processed" / "module5_test_openfigi.csv"
RAW_DIR = ROOT / "data" / "raw" / "openfigi"

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_HDR = {"Content-Type": "application/json"}
BATCH_SIZE   = 10        # max without an API key
RATE_SLEEP   = 12.0      # seconds between batches (1 req / 10 s limit)

def _parse_sec_desc(desc):
    """
    Parse coupon (%) and maturity (YYYY-MM-DD) from an OpenFIGI securityDescription.
    Format example: "AVGO 4.15 02/15/28" or "AVGO 4.421 05/08/26".
    """
    m = re.search(r"([\d.]+)\s+(\d{2}/\d{2}/(\d{2}|\d{4}))\s*", str(desc))
    if not m:
        return "", ""
    coupon = m.group(1)
    date_str = m.group(2)         # MM/DD/YY or MM/DD/YYYY
    parts = date_str.split("/")
    if len(parts) != 3:
        return coupon, ""
    mm, dd, yy = parts
    yr = int(yy)
    if yr < 100:                  # 2-digit year → assume 2000s
        yr += 2000
    mat_iso = f"{yr:04d}-{mm}-{dd}"
    return coupon, mat_iso


_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _parse_extracted_date(s):
    """Parse 'Month DD, YYYY' → 'YYYY-MM-DD', or return None on failure."""
    m = re.match(
        r"(?i)([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})",
        str(s).strip()
    )
    if not m:
        return None
    mon = _MONTH_MAP.get(m.group(1).lower())
    if not mon:
        return None
    return f"{m.group(3)}-{mon}-{int(m.group(2)):02d}"


def query_openfigi(cusips):
    """Submit a batch of CUSIPs and return the raw response list."""
    payload = [{"idType": "ID_CUSIP", "idValue": c} for c in cusips]
    r = requests.post(OPENFIGI_URL, headers=OPENFIGI_HDR,
                      data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json()   # list, one element per CUSIP


def _load_or_fetch_batch(b_idx, batch):
    """Return batch response list, loading from disk if already cached."""
    raw_path = RAW_DIR / f"test_batch_{b_idx + 1:02d}.json"
    if raw_path.exists():
        print(f"  Batch {b_idx + 1}: loading from cache ({raw_path.name})")
        with raw_path.open() as f:
            return json.load(f)
    print(f"  Batch {b_idx + 1}/{'-'}: fetching {len(batch)} CUSIPs from OpenFIGI...")
    try:
        resp = query_openfigi(batch)
    except Exception as exc:
        print(f"    ERROR: {exc}")
        resp = [None] * len(batch)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w") as f:
        json.dump(resp, f, indent=2)
    return resp


def run_openfigi_check(df):
    cusips = df["cusip"].tolist()
    results_by_cusip = {}

    batches = [cusips[i:i + BATCH_SIZE] for i in range(0, len(cusips), BATCH_SIZE)]
    for b_idx, batch in enumerate(batches):
        resp = _load_or_fetch_batch(b_idx, batch)
        for cusip, hit in zip(batch, resp):
            results_by_cusip[cusip] = hit
        if b_idx < len(batches) - 1:
            cached = (RAW_DIR / f"test_batch_{b_idx + 2:02d}.json").exists()
            if not cached:
                print(f"    Sleeping {RATE_SLEEP}s for rate limit...")
                time.sleep(RATE_SLEEP)

    rows = []
    for _, bond in df.iterrows():
        cusip      = str(bond["cusip"]).strip()
        ext_coupon = bond.get("coupon_pct", "")
        ext_mat    = str(bond.get("maturity_date", "")).strip()

        hit = results_by_cusip.get(cusip)

        if hit is None or "data" not in hit or not hit["data"]:
            rows.append({
                "cusip":             cusip,
                "bond_name":         bond["bond_name"],
                "openfigi_name":     "",
                "openfigi_coupon":   "",
                "openfigi_maturity": "",
                "extracted_coupon":  ext_coupon,
                "extracted_maturity": ext_mat,
                "coupon_match":      "no_data",
                "maturity_match":    "no_data",
            })
            continue

        figi_data = hit["data"][0]
        fi_name = figi_data.get("name", "")
        # OpenFIGI /v3/mapping TRACE responses do not populate 'coupon' or
        # 'maturityDate' fields; parse them from securityDescription instead.
        # Format: "AVGO 4.15 02/15/28"
        sec_desc = figi_data.get("securityDescription", "") or figi_data.get("ticker", "")
        fi_coupon, fi_mat = _parse_sec_desc(sec_desc)

        # Coupon match: compare numeric values (tolerance 0.001%)
        coupon_match = "no_data"
        try:
            diff = abs(float(fi_coupon) - float(ext_coupon))
            coupon_match = "pass" if diff <= 0.001 else f"fail (Δ={diff:.4f}%)"
        except (TypeError, ValueError):
            coupon_match = "no_data"

        # Maturity match: compare YYYY-MM-DD strings
        maturity_match = "no_data"
        ext_mat_iso = _parse_extracted_date(ext_mat)
        if fi_mat and ext_mat_iso:
            maturity_match = "pass" if fi_mat == ext_mat_iso else f"fail (got {fi_mat})"
        elif not ext_mat_iso:
            maturity_match = "no_data (truncated)"
        elif not fi_mat:
            maturity_match = "no_data (OpenFIGI)"

        rows.append({
            "cusip":              cusip,
            "bond_name":          bond["bond_name"],
            "openfigi_name":      fi_name,
            "openfigi_coupon":    fi_coupon,
            "openfigi_maturity":  fi_mat,
            "extracted_coupon":   ext_coupon,
            "extracted_maturity": ext_mat,
            "coupon_match":       coupon_match,
            "maturity_match":     maturity_match,
        })

    return pd.DataFrame(rows)


df = pd.read_csv(IN_CSV)
df["coupon_pct"] = pd.to_numeric(df["coupon_pct"], errors="coerce")

print("=" * 60)
print("OpenFIGI check — test set bonds")
print("=" * 60)
out = run_openfigi_check(df)
out.to_csv(OUT_CSV, index=False)
print(f"\nWrote {len(out)} rows → {OUT_CSV.name}")

# Summary
coupon_pass   = (out["coupon_match"] == "pass").sum()
maturity_pass = (out["maturity_match"] == "pass").sum()
n             = len(out)
print(f"\nCoupon match:   {coupon_pass}/{n}  ({100*coupon_pass/n:.0f}%)")
print(f"Maturity match: {maturity_pass}/{n}  ({100*maturity_pass/n:.0f}%)")
print("\nDetails:")
print(out[["cusip","bond_name","coupon_match","maturity_match","openfigi_name"]].to_string())
