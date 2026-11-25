#!/usr/bin/env python3
"""
Module 5 — Cross-document reconciliation: FWP term sheets vs. 424B2 prospectus.

For each Apple deal:
  1. Extract from 424B2 cover page:
     - Headline total ($X,XXX,XXX,XXX right before "Apple Inc.")
     - Bond list: principal_amount, rate_type, maturity_year, coupon_pct
     - Pricing table: price_to_public_pct per bond
  2. Load matching FWP bonds from module5_apple_tranches.csv
  3. Match bonds by key (rate_type, maturity_year); warn on duplicate keys
  4. Compare fields: principal_amount, coupon_pct, maturity_year, price_to_public_pct
     result = match | break | not_found | not_applicable
  5. Deal-level check: sum of FWP principal amounts vs. 424B2 headline total
  6. Save data/processed/module5_reconciliation.csv; print deal-by-deal summary
"""
import re, html, unicodedata
from pathlib import Path

import pandas as pd

DOCS_CSV = Path("data/processed/module5_documents.csv")
TRANCHES = Path("data/processed/module5_apple_tranches.csv")
RAW_DIR  = Path("data/raw/prospectus/Apple")
OUT_CSV  = Path("data/processed/module5_reconciliation.csv")

MATCH = "match"
BREAK = "break"
NF    = "not_found"
NA    = "not_applicable"


# ── HTML flattener ────────────────────────────────────────────────────────────

def html_to_flat(raw: str) -> str:
    text = re.sub(r'<(p|div|br|tr|li|h[1-6])[^>]*>', '\n', raw, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'[​‌‍﻿]', '', text)  # zero-width spaces
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_amount(s: str) -> int:
    """'$1,000,000,000' or '$ 500,000,000' → 1000000000"""
    return int(re.sub(r'[^\d]', '', str(s)))


# ── 424B2 extraction ──────────────────────────────────────────────────────────

def extract_424b2(flat: str) -> dict:
    """
    Returns:
      headline_total : int
      bonds          : list of {amount, rate_type, maturity_year, coupon_pct}
      prices         : {(rate_type, maturity_year): float}
    """
    result: dict = {"headline_total": None, "bonds": [], "prices": {}}

    # ── headline total: $ amount immediately before "Apple Inc." ─────────────
    m = re.search(r'\$\s*([\d,]+)\s*\n\s*Apple\s+Inc\.', flat)
    if m:
        result["headline_total"] = parse_amount(m.group(1))

    # ── cover section: bond listing between "Apple Inc." and "We are offering"
    cs = re.search(r'Apple\s+Inc\.\s*\n([\s\S]+?)(?=We are offering)', flat)
    cover = cs.group(1) if cs else flat

    bonds: list[dict] = []

    # Pattern A — 2023 multi-line format:
    #   $ 500,000,000
    #   4.150%
    #   Notes due 2030
    for m in re.finditer(
        r'\$\s*([\d,]+)\s*\n\s*([\d.]+)\s*%\s*\n\s*Notes due\s+(\d{4})',
        cover, re.IGNORECASE
    ):
        bonds.append({
            "amount":        parse_amount(m.group(1)),
            "rate_type":     "Fixed",
            "maturity_year": int(m.group(3)),
            "coupon_pct":    float(m.group(2)),
        })

    # Pattern B — 2025/2026 single-line fixed:
    #   $1,000,000,000 4.650% Notes due 2028
    for m in re.finditer(
        r'\$\s*([\d,]+)\s+([\d.]+)\s*%\s+Notes due\s+(\d{4})',
        cover, re.IGNORECASE
    ):
        bonds.append({
            "amount":        parse_amount(m.group(1)),
            "rate_type":     "Fixed",
            "maturity_year": int(m.group(3)),
            "coupon_pct":    float(m.group(2)),
        })

    # Pattern C — 2026 single-line floating:
    #   $500,000,000 Floating Rate Notes due 2028
    for m in re.finditer(
        r'\$\s*([\d,]+)\s+Floating Rate Notes due\s+(\d{4})',
        cover, re.IGNORECASE
    ):
        bonds.append({
            "amount":        parse_amount(m.group(1)),
            "rate_type":     "Floating",
            "maturity_year": int(m.group(2)),
            "coupon_pct":    None,
        })

    # Deduplicate by key (keeps first; patterns are mutually exclusive anyway)
    seen: set = set()
    for b in bonds:
        key = (b["rate_type"], b["maturity_year"])
        if key not in seen:
            seen.add(key)
            result["bonds"].append(b)

    # ── pricing table: between "Public Offering Price" and "(N)\nPlus accrued"
    pt = re.search(
        r'Public Offering Price([\s\S]+?)\(\d\)\s*\n\s*Plus accrued interest',
        flat
    )
    pricing_text = pt.group(1) if pt else flat

    # Each row: "YYYY [Floating Rate|Fixed Rate] Note ... XX.XXX%"
    # The first percentage value after the note label is the public offering price.
    for m in re.finditer(
        r'(\d{4})\s+((?:Floating Rate|Fixed Rate)\s)?Note\b[\s\n ]+(\d[\d.]*)\s*%',
        pricing_text, re.IGNORECASE
    ):
        year  = int(m.group(1))
        rtype_raw = (m.group(2) or "").strip().lower()
        price = float(m.group(3))
        rt = "Floating" if "float" in rtype_raw else "Fixed"
        result["prices"][(rt, year)] = price

    return result


# ── field comparison ──────────────────────────────────────────────────────────

def compare_field(
    p_val,
    f_val,
    tol: float = 0.0,
) -> tuple[str, str]:
    """Compare a prospectus value vs a FWP value. Returns (result, detail)."""
    p_none = p_val is None or (isinstance(p_val, float) and pd.isna(p_val))
    f_none = f_val is None or (isinstance(f_val, float) and pd.isna(f_val))

    if p_none and f_none:
        return NA, "both sides N/A"
    if p_none:
        return NF, f"not in 424B2; FWP={f_val}"
    if f_none:
        return BREAK, f"424B2={p_val}; FWP missing"

    if isinstance(p_val, (int, float)) and isinstance(f_val, (int, float)):
        diff = abs(float(p_val) - float(f_val))
        if diff <= tol:
            return MATCH, f"{p_val} = {f_val}"
        return BREAK, f"424B2={p_val}, FWP={f_val} (diff={diff:.6g})"

    if str(p_val) == str(f_val):
        return MATCH, str(p_val)
    return BREAK, f"424B2={p_val!r}, FWP={f_val!r}"


# ── main ──────────────────────────────────────────────────────────────────────

docs     = pd.read_csv(DOCS_CSV)
apple    = docs[docs["issuer"] == "Apple"].copy()
tranches = pd.read_csv(TRANCHES)
tranches["maturity_year"] = tranches["maturity_date"].str[-4:].astype(int)
# Numeric columns
for col in ["coupon_pct", "price_to_public_pct"]:
    tranches[col] = pd.to_numeric(tranches[col], errors="coerce")

all_rows: list[dict] = []

for _, deal_row in apple.iterrows():
    deal_date = deal_row["deal_date"]
    b2_acc    = deal_row["accession_number"]

    print(f"\n{'═'*64}")
    print(f"  Deal {deal_date}  (424B2: {b2_acc})")
    print(f"{'═'*64}")

    # ── load and parse 424B2 ─────────────────────────────────────────────────
    b2_path = RAW_DIR / f"{b2_acc}.htm"
    raw     = b2_path.read_bytes()[:600_000].decode("utf-8", errors="replace")
    flat    = html_to_flat(raw)
    b2      = extract_424b2(flat)

    print(f"  424B2 headline:    ${b2['headline_total']:,}" if b2["headline_total"] else "  424B2 headline:    NOT FOUND")
    print(f"  Bonds in cover:    {len(b2['bonds'])}")
    print(f"  Pricing rows:      {len(b2['prices'])}")

    # ── load FWP bonds for this deal (match by trade_date ≈ deal_date ± 3 d) ─
    from datetime import datetime
    deal_dt   = datetime.strptime(deal_date, "%Y-%m-%d").date()
    fwp_rows: list = []
    for _, tb in tranches.iterrows():
        trade_str = re.sub(r"\s*\(.*?\)\s*$", "", str(tb["trade_date"])).strip()
        try:
            trade_dt = datetime.strptime(trade_str, "%B %d, %Y").date()
        except ValueError:
            continue
        if abs((trade_dt - deal_dt).days) <= 3:
            fwp_rows.append(tb)

    if not fwp_rows:
        print(f"  WARNING: no FWP bonds found for deal {deal_date}")
        continue

    print(f"  FWP bonds:         {len(fwp_rows)}")

    # ── duplicate-key check ───────────────────────────────────────────────────
    b2_keys  = [(b["rate_type"], b["maturity_year"]) for b in b2["bonds"]]
    for k in set(b2_keys):
        if b2_keys.count(k) > 1:
            print(f"  WARNING: duplicate key in 424B2: {k}")

    fwp_keys = [(b["rate_type"], int(b["maturity_year"])) for b in fwp_rows]
    for k in set(fwp_keys):
        if fwp_keys.count(k) > 1:
            print(f"  WARNING: duplicate key in FWP:   {k}")

    b2_map = {(b["rate_type"], b["maturity_year"]): b for b in b2["bonds"]}

    # ── per-bond field comparisons ────────────────────────────────────────────
    matched = 0
    for tb in fwp_rows:
        key = (str(tb["rate_type"]), int(tb["maturity_year"]))
        b2b = b2_map.get(key)
        if b2b is None:
            print(f"  WARNING: FWP bond {tb['cusip']} key {key} not in 424B2")
            continue
        matched += 1

        fwp_amt = parse_amount(str(tb["principal_amount"]))
        fwp_cpn = None if pd.isna(tb["coupon_pct"]) else float(tb["coupon_pct"])
        fwp_yr  = int(tb["maturity_year"])
        fwp_px  = float(tb["price_to_public_pct"])

        b2_px   = b2["prices"].get(key)

        for field, p_val, f_val, tol in [
            ("principal_amount",    b2b["amount"],        fwp_amt, 0.0),
            ("coupon_pct",          b2b["coupon_pct"],    fwp_cpn, 0.0001),
            ("maturity_year",       b2b["maturity_year"], fwp_yr,  0.0),
            ("price_to_public_pct", b2_px,                fwp_px,  0.001),
        ]:
            res, detail = compare_field(p_val, f_val, tol)
            all_rows.append({
                "deal_date":         deal_date,
                "cusip":             tb["cusip"],
                "bond_name":         tb["bond_name"],
                "field":             field,
                "prospectus_value":  p_val,
                "fwp_value":         f_val,
                "result":            res,
                "detail":            detail,
            })

    # ── deal total check ─────────────────────────────────────────────────────
    fwp_total = sum(parse_amount(str(b["principal_amount"])) for b in fwp_rows)
    b2_total  = b2["headline_total"]
    tot_match = fwp_total == b2_total
    all_rows.append({
        "deal_date":        deal_date,
        "cusip":            "DEAL_TOTAL",
        "bond_name":        f"Sum of {len(fwp_rows)} bonds",
        "field":            "deal_total",
        "prospectus_value": b2_total,
        "fwp_value":        fwp_total,
        "result":           MATCH if tot_match else BREAK,
        "detail":           (
            f"${b2_total:,} = ${fwp_total:,}"
            if tot_match
            else f"424B2 ${b2_total:,} ≠ FWP ${fwp_total:,}"
        ),
    })

    # ── deal summary ──────────────────────────────────────────────────────────
    deal_field_rows = [r for r in all_rows
                       if r["deal_date"] == deal_date and r["cusip"] != "DEAL_TOTAL"]
    n_match = sum(1 for r in deal_field_rows if r["result"] == MATCH)
    n_break = sum(1 for r in deal_field_rows if r["result"] == BREAK)
    n_nf    = sum(1 for r in deal_field_rows if r["result"] == NF)
    n_na    = sum(1 for r in deal_field_rows if r["result"] == NA)

    print(f"\n  Bonds matched:    {matched}/{len(fwp_rows)}")
    print(f"  Fields:  match={n_match}  break={n_break}  not_found={n_nf}  not_applicable={n_na}")
    print(f"  Deal total:      {'✓ match' if tot_match else f'✗ BREAK  424B2=${b2_total:,}  FWP=${fwp_total:,}'}")

    issues = [(r["cusip"], r["field"], r["result"], r["detail"])
              for r in deal_field_rows if r["result"] in (BREAK, NF)]
    if issues:
        print(f"\n  Issues:")
        for cusip, field, res, det in issues:
            print(f"    [{res}]  {cusip}  {field}  →  {det}")
    else:
        print("  No breaks or missing fields.")

# ── save ──────────────────────────────────────────────────────────────────────

out_df = pd.DataFrame(all_rows, columns=[
    "deal_date", "cusip", "bond_name", "field",
    "prospectus_value", "fwp_value", "result", "detail",
])
out_df.to_csv(OUT_CSV, index=False)

print(f"\n{'─'*64}")
print(f"Saved {len(out_df)} rows → {OUT_CSV}")

# ── cross-deal summary ────────────────────────────────────────────────────────
print(f"\n{'─'*64}")
print("  Overall summary (field-level rows, excluding deal_total)")
summary = (
    out_df[out_df["cusip"] != "DEAL_TOTAL"]
    .groupby("result").size()
    .reindex([MATCH, BREAK, NF, NA], fill_value=0)
    .rename("count")
)
print(summary.to_string())
print(f"{'─'*64}")
