#!/usr/bin/env python3
"""
Module 5 — OpenFIGI cross-check: verify 16 Apple CUSIPs against OpenFIGI.

Calls the free OpenFIGI mapping API (no API key needed).
Checks per bond:
  - issuer  : `name` field should contain "APPLE"
  - coupon  : fixed-rate only; parsed from securityDescription vs FWP coupon_pct
  - maturity: full date parsed from securityDescription vs FWP maturity_date

API notes:
  - Endpoint : POST https://api.openfigi.com/v3/mapping
  - Free tier: up to 10 items/request, ~25 requests/min (no API key)
  - Response : [{data: [{name, ticker, securityDescription, ...}]} | {warning: ...}]
"""
import json, re, time, urllib.request
from datetime import date, datetime
from pathlib import Path

import pandas as pd

TRANCHES  = Path("data/processed/module5_apple_tranches.csv")
OUT_CSV   = Path("data/processed/module5_openfigi_check.csv")
RAW_DIR   = Path("data/raw/openfigi")
RAW_DIR.mkdir(parents=True, exist_ok=True)

FIGI_URL   = "https://api.openfigi.com/v3/mapping"
BATCH_SIZE = 10
PAUSE      = 3.0   # seconds between batches (free tier: 25 req/min)

MATCH = "match"
BREAK = "break"
NF    = "not_found"
NA    = "not_applicable"

DATE_FMT = "%B %d, %Y"   # "November 9, 2028"


# ── OpenFIGI API ──────────────────────────────────────────────────────────────

def call_openfigi(cusips: list[str], batch_num: int) -> list[dict]:
    """POST one batch to OpenFIGI; save raw JSON; return parsed JSON list."""
    payload = [{"idType": "ID_CUSIP", "idValue": c} for c in cusips]
    body    = json.dumps(payload).encode("utf-8")
    req     = urllib.request.Request(
        FIGI_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()

    out = RAW_DIR / f"batch_{batch_num:02d}.json"
    out.write_bytes(raw)
    print(f"  Saved raw JSON → {out}")
    return json.loads(raw)


# ── description parsing ───────────────────────────────────────────────────────

def _mat_from_parts(parts: list[str]) -> date | None:
    """
    Find the last token matching MM/DD/YY (or MM/DD/YYYY) in `parts`.
    All Apple bonds in this dataset mature 2026–2053, so 2-digit year:
    year = 2000 + year_2d is always correct.
    """
    for part in reversed(parts):
        m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$', part)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            yr = int(m.group(3))
            if yr < 100:
                yr += 2000
            try:
                return date(yr, month, day)
            except ValueError:
                pass
    return None


def parse_description(desc: str) -> dict:
    """
    Parse OpenFIGI securityDescription:
      "AAPL 4.421 05/08/26"   → coupon=4.421, maturity=2026-05-08
      "AAPL 4 05/12/28"       → coupon=4.0,   maturity=2028-05-12
      "AAPL Float 11/09/28"   → is_floating=True, maturity=2028-11-09
    Returns: {ticker, coupon (float|None), is_floating (bool), maturity (date|None)}
    """
    r: dict = {"ticker": None, "coupon": None, "is_floating": False, "maturity": None}
    parts = desc.strip().split()
    if not parts:
        return r

    r["ticker"]   = parts[0]
    r["maturity"] = _mat_from_parts(parts)

    # Look for coupon token: numeric, fractional (A B/C like "4 1/2"), or Float/FLT
    for idx, tok in enumerate(parts[1:], 1):
        if re.match(r'^(\d+)/(\d+)/(\d{2}|\d{4})$', tok):
            break                             # hit the date — stop
        if re.match(r'^\d+\.?\d*$', tok):
            # Peek ahead: if next token is a simple fraction (B/C, one slash),
            # combine as integer + fraction  e.g. "4 1/2" → 4.5
            nxt = parts[idx + 1] if (idx + 1) < len(parts) else ""
            frac_m = re.match(r'^(\d+)/(\d+)$', nxt)
            if frac_m:
                r["coupon"] = float(tok) + int(frac_m.group(1)) / int(frac_m.group(2))
            else:
                r["coupon"] = float(tok)
            break
        if re.match(r'^\d+/\d+$', tok):      # pure fraction with no integer part
            num, denom = tok.split('/')
            r["coupon"] = int(num) / int(denom)
            break
        if tok.lower() in ("float", "flt", "f", "fl", "var"):
            r["is_floating"] = True
            break

    return r


# ── field comparison ──────────────────────────────────────────────────────────

def compare(p_val, f_val, tol: float = 0.0) -> tuple[str, str]:
    """Compare an OpenFIGI value vs a FWP value. Returns (result, detail)."""
    p_none = p_val is None or (isinstance(p_val, float) and pd.isna(p_val))
    f_none = f_val is None or (isinstance(f_val, float) and pd.isna(f_val))

    if p_none and f_none:
        return NA, "both sides N/A"
    if p_none:
        return NF, f"not in OpenFIGI; FWP={f_val}"
    if f_none:
        return BREAK, f"OpenFIGI={p_val}; FWP missing"

    if isinstance(p_val, (int, float)) and isinstance(f_val, (int, float)):
        diff = abs(float(p_val) - float(f_val))
        if diff <= tol:
            return MATCH, f"{p_val} ≈ {f_val}" if diff > 0 else f"{p_val} = {f_val}"
        return BREAK, f"OpenFIGI={p_val}, FWP={f_val} (diff={diff:.6g})"

    if p_val == f_val:
        return MATCH, str(p_val)
    return BREAK, f"OpenFIGI={p_val!r}, FWP={f_val!r}"


# ── main ──────────────────────────────────────────────────────────────────────

tranches = pd.read_csv(TRANCHES)
tranches["coupon_pct"]     = pd.to_numeric(tranches["coupon_pct"],     errors="coerce")
tranches["maturity_dt"]    = tranches["maturity_date"].apply(
    lambda s: datetime.strptime(re.sub(r'\s*\(.*?\)$', '', s).strip(), DATE_FMT).date()
)

cusips = tranches["cusip"].tolist()
print(f"Loaded {len(cusips)} CUSIPs from {TRANCHES.name}")

# ── API calls ─────────────────────────────────────────────────────────────────

raw_results: list[dict] = []   # one element per CUSIP, same order

batches = [cusips[i:i+BATCH_SIZE] for i in range(0, len(cusips), BATCH_SIZE)]
for b_idx, batch in enumerate(batches, 1):
    print(f"\nBatch {b_idx}/{len(batches)}: {len(batch)} CUSIPs")
    # Use cached response if already on disk (avoids re-hitting the API during reruns)
    cache = RAW_DIR / f"batch_{b_idx:02d}.json"
    cached = cache.exists()
    if cached:
        resp = json.loads(cache.read_bytes())
        print(f"  Using cached response from {cache}")
    else:
        resp = call_openfigi(batch, b_idx)

    # Print full raw JSON for the VERY FIRST bond only
    if b_idx == 1:
        print(f"\n── Full raw JSON for first bond ({batch[0]}) ──────────────────────")
        print(json.dumps(resp[0], indent=2))
        print("──────────────────────────────────────────────────────────────────\n")

    raw_results.extend(resp)

    if b_idx < len(batches) and not cached:
        print(f"  Sleeping {PAUSE:.0f}s (free-tier rate limit) …")
        time.sleep(PAUSE)

print(f"\nTotal responses received: {len(raw_results)}")

# ── build comparison rows ─────────────────────────────────────────────────────

all_rows: list[dict] = []

for tb_row, figi_resp in zip(tranches.itertuples(index=False), raw_results):
    cusip      = tb_row.cusip
    bond_name  = tb_row.bond_name
    rate_type  = tb_row.rate_type
    fwp_coupon = tb_row.coupon_pct         # NaN for floating
    fwp_mat    = tb_row.maturity_dt        # datetime.date

    # ── get the best FIGI entry for this CUSIP ────────────────────────────────
    if "data" not in figi_resp or not figi_resp["data"]:
        # No FIGI found
        for field in ("issuer", "coupon", "maturity"):
            all_rows.append({
                "cusip":          cusip,
                "bond_name":      bond_name,
                "field":          field,
                "openfigi_value": None,
                "fwp_value":      None,
                "result":         NF,
                "detail":         figi_resp.get("warning", "no data returned"),
            })
        continue

    # Prefer Corp sector; fall back to first entry
    entries = figi_resp["data"]
    entry   = next((e for e in entries if e.get("marketSector") == "Corp"), entries[0])

    figi_name = entry.get("name", "")
    figi_desc = entry.get("securityDescription", "")
    parsed    = parse_description(figi_desc)

    # ── 1. Issuer ─────────────────────────────────────────────────────────────
    # name = "APPLE INC"; check it contains "apple"
    p_issuer = figi_name.upper() if figi_name else None
    issuer_ok = p_issuer and "APPLE" in p_issuer
    res_i, det_i = compare(
        "APPLE INC" if issuer_ok else p_issuer or None,
        "APPLE INC",
        tol=0.0,
    )
    all_rows.append({
        "cusip":          cusip,
        "bond_name":      bond_name,
        "field":          "issuer",
        "openfigi_value": p_issuer,
        "fwp_value":      "APPLE INC",
        "result":         res_i,
        "detail":         det_i,
    })

    # ── 2. Coupon (fixed only) ────────────────────────────────────────────────
    if rate_type == "Fixed":
        p_cpn = parsed["coupon"]          # float or None if not found
        f_cpn = float(fwp_coupon)
        res_c, det_c = compare(p_cpn, f_cpn, tol=0.005)
        all_rows.append({
            "cusip":          cusip,
            "bond_name":      bond_name,
            "field":          "coupon_pct",
            "openfigi_value": p_cpn,
            "fwp_value":      f_cpn,
            "result":         res_c,
            "detail":         det_c,
        })
    else:
        # Floating — just confirm description signals floating
        is_flt = parsed["is_floating"] or (
            "float" in figi_desc.lower() or "flt" in figi_desc.lower()
        )
        all_rows.append({
            "cusip":          cusip,
            "bond_name":      bond_name,
            "field":          "coupon_pct",
            "openfigi_value": "Float" if is_flt else figi_desc,
            "fwp_value":      NA,
            "result":         NA,
            "detail":         "floating-rate bond; coupon not applicable",
        })

    # ── 3. Maturity date ─────────────────────────────────────────────────────
    p_mat = parsed["maturity"]          # datetime.date or None
    res_m, det_m = compare(
        str(p_mat) if p_mat else None,
        str(fwp_mat),
        tol=0.0,
    )
    all_rows.append({
        "cusip":          cusip,
        "bond_name":      bond_name,
        "field":          "maturity_date",
        "openfigi_value": str(p_mat) if p_mat else None,
        "fwp_value":      str(fwp_mat),
        "result":         res_m,
        "detail":         det_m,
    })

# ── save ──────────────────────────────────────────────────────────────────────

out_df = pd.DataFrame(all_rows, columns=[
    "cusip", "bond_name", "field",
    "openfigi_value", "fwp_value",
    "result", "detail",
])
out_df.to_csv(OUT_CSV, index=False)
print(f"\nWrote {len(out_df)} rows → {OUT_CSV}")

# ── summary ───────────────────────────────────────────────────────────────────

print(f"\n{'─'*62}")
print("  Summary")
print(f"{'─'*62}")

counts = out_df.groupby(["field", "result"]).size().unstack(fill_value=0)
counts = counts.reindex(columns=[MATCH, BREAK, NF, NA], fill_value=0)
print(counts.to_string())
print(f"{'─'*62}")

breaks_nf = out_df[out_df["result"].isin([BREAK, NF])]
if breaks_nf.empty:
    print("\n  No breaks or missing FIGIs.")
else:
    print(f"\n  Issues ({len(breaks_nf)} rows):")
    for _, r in breaks_nf.iterrows():
        print(f"  [{r['result']}] {r['cusip']}  {r['field']}")
        print(f"    OpenFIGI: {r['openfigi_value']}")
        print(f"    FWP:      {r['fwp_value']}")
