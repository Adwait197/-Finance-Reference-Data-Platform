#!/usr/bin/env python3
"""
Module 5 — Validation checks V1-V7 for all 106 build-set bonds.

Re-uses the same check logic as validate_tranches.py but:
  - reads module5_build_tranches.csv (5 issuers, 106 bonds)
  - V3 looks up each issuer's own stock CUSIP prefix from security_master_history
  - V7 groups by (issuer, trade_date) so cross-company same-date deals don't collide
  - outputs a failure-count matrix (checks × companies) and top-10 failure list
"""
import os, re, sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

IN_CSV  = Path(__file__).parent.parent / "data" / "processed" / "module5_build_tranches.csv"
OUT_CSV = Path(__file__).parent.parent / "data" / "processed" / "module5_build_validation.csv"

PASS = "pass"
FAIL = "fail"
NA   = "not applicable"

# Map issuer column value → search fragment for security_master_history ILIKE
ISSUER_SEARCH = {
    "Apple":                 "Apple, Inc",
    "Alphabet Inc.":         "Alphabet, Inc",
    "Eli Lilly and Company": "Eli Lilly",
    "Johnson & Johnson":     "Johnson & Johnson",
    "Microsoft Corporation": "Microsoft Corp",
}

# Short labels for the output table
ISSUER_SHORT = {
    "Apple":                 "Apple",
    "Alphabet Inc.":         "Alphabet",
    "Eli Lilly and Company": "Eli Lilly",
    "Johnson & Johnson":     "J&J",
    "Microsoft Corporation": "Microsoft",
}


# ── check functions (identical logic to validate_tranches.py) ─────────────────

def _cusip_char_val(c):
    if c.isdigit(): return int(c)
    if c.isalpha(): return ord(c.upper()) - ord("A") + 10
    if c == "*":    return 36
    if c == "@":    return 37
    if c == "#":    return 38
    raise ValueError(f"invalid CUSIP char {c!r}")

def check_v1(cusip):
    cusip = str(cusip).strip().upper()
    if len(cusip) != 9:
        return FAIL, f"length {len(cusip)} ≠ 9"
    total = 0
    for i in range(8):
        v = _cusip_char_val(cusip[i])
        if i % 2 == 1: v *= 2
        if v >= 10:    v = v // 10 + v % 10
        total += v
    expected = (10 - total % 10) % 10
    actual   = _cusip_char_val(cusip[8])
    if expected == actual:
        return PASS, f"check digit {cusip[8]} verified"
    return FAIL, f"computed {expected} ≠ stored '{cusip[8]}'"

def _isin_digit_string(isin):
    s = ""
    for c in isin:
        if c.isdigit():  s += c
        elif c.isalpha(): s += str(ord(c.upper()) - ord("A") + 10)
        else: raise ValueError(f"invalid ISIN char {c!r}")
    return s

def check_v2(isin):
    isin = str(isin).strip().upper()
    if len(isin) != 12:
        return FAIL, f"length {len(isin)} ≠ 12"
    if not isin[:2].isalpha():
        return FAIL, f"country code {isin[:2]!r} not alphabetic"
    try: digits = _isin_digit_string(isin)
    except ValueError as e: return FAIL, str(e)
    total = 0
    for i, d in enumerate(reversed(digits)):
        v = int(d)
        if i % 2 == 1:
            v *= 2
            if v > 9: v -= 9
        total += v
    if total % 10 == 0:
        return PASS, f"Luhn sum {total} ≡ 0"
    return FAIL, f"Luhn sum {total} not ≡ 0"

def check_v3(cusip, stock_prefix, issuer):
    bond_prefix = str(cusip).strip().upper()[:6]
    if stock_prefix is None:
        return FAIL, f"no stock CUSIP found for {issuer!r} in security_master_history"
    if bond_prefix == stock_prefix:
        return PASS, f"bond prefix {bond_prefix} = stock prefix {stock_prefix}"
    return FAIL, f"bond prefix {bond_prefix} ≠ stock prefix {stock_prefix}"

def check_v4(rate_type, bm_yield, spread_bps, yld):
    if str(rate_type).strip().lower() != "fixed":
        return NA, "floating-rate bond"
    try:
        bm, sp, y = float(bm_yield), float(spread_bps), float(yld)
    except (TypeError, ValueError):
        return FAIL, "one or more numeric fields missing/non-numeric"
    diff = abs(bm + sp / 100.0 - y)
    if diff <= 0.005:
        return PASS, f"|{bm}+{sp}/100−{y}| = {diff:.4f} ≤ 0.005"
    return FAIL, f"|{bm}+{sp}/100−{y}| = {diff:.4f} > 0.005"

def check_v5(rate_type, coupon, yld, price):
    if str(rate_type).strip().lower() != "fixed":
        return NA, "floating-rate bond"
    try:
        c, y, p = float(coupon), float(yld), float(price)
    except (TypeError, ValueError):
        return FAIL, "one or more numeric fields missing/non-numeric"
    if abs(c - y) < 1e-6:
        ok = abs(p - 100.0) <= 0.01
        return (PASS if ok else FAIL), f"par bond, price {p}"
    elif c < y:
        ok = p < 100.0
        return (PASS if ok else FAIL), f"coupon {c}% < yield {y}%, price {p} {'✓' if ok else '✗ (expected <100)'}"
    else:
        ok = p > 100.0
        return (PASS if ok else FAIL), f"coupon {c}% > yield {y}%, price {p} {'✓' if ok else '✗ (expected >100)'}"

_DATE_FMT = "%B %d, %Y"

def _parse_date(s):
    s = re.sub(r"\s*\(.*\)\s*$", "", str(s).strip()).strip()
    return datetime.strptime(s, _DATE_FMT).date()

def check_v6(bond_name, trade_str, sett_str, mat_str):
    try:
        trade = _parse_date(trade_str)
        sett  = _parse_date(sett_str)
        mat   = _parse_date(mat_str)
    except ValueError as e:
        return FAIL, f"date parse error: {e}"
    if trade > sett:
        return FAIL, f"trade {trade} > settlement {sett}"
    if sett >= mat:
        return FAIL, f"settlement {sett} ≥ maturity {mat}"
    m = re.search(r"due\s+(\d{4})", bond_name, re.IGNORECASE)
    if not m:
        return FAIL, f"'due YYYY' not in bond_name"
    name_year = int(m.group(1))
    if name_year != mat.year:
        return FAIL, f"name year {name_year} ≠ maturity year {mat.year}"
    return PASS, f"trade {trade} ≤ sett {sett} < mat {mat}, year {mat.year} ✓"

def check_v8(bond, all_df):
    """Within one deal (same issuer + trade_date), no two bonds may share a CUSIP."""
    issuer     = str(bond["issuer"]).strip()
    trade_date = str(bond["trade_date"]).strip()
    cusip      = str(bond["cusip"]).strip()
    deal_df = all_df[
        (all_df["issuer"].astype(str).str.strip() == issuer) &
        (all_df["trade_date"].astype(str).str.strip() == trade_date)
    ]
    count = (deal_df["cusip"].astype(str).str.strip() == cusip).sum()
    if count > 1:
        return FAIL, f"CUSIP {cusip} appears {count} times in this deal"
    return PASS, f"CUSIP {cusip} is unique in this deal"


def check_v7(bond, all_df):
    issuer     = str(bond["issuer"]).strip()
    trade_date = str(bond["trade_date"]).strip()
    # Group by (issuer, trade_date) so cross-company same-date deals don't collide
    deal_df = all_df[
        (all_df["issuer"].astype(str).str.strip() == issuer) &
        (all_df["trade_date"].astype(str).str.strip() == trade_date)
    ]
    issues = []
    for field in ["rating_moodys", "rating_sp", "trade_date", "settlement_date"]:
        vals = deal_df[field].astype(str).str.strip().unique()
        if len(vals) > 1:
            issues.append(f"{field} not uniform in deal: {list(vals)}")
    rate_type = str(bond["rate_type"]).strip()
    dc        = str(bond["day_count"]).strip()
    if rate_type == "Fixed":
        if "30/360" not in dc:
            issues.append(f"day_count={dc!r} (Fixed → expected 30/360)")
    elif rate_type == "Floating":
        if not re.search(r"actual/360", dc, re.IGNORECASE):
            issues.append(f"day_count={dc!r} (Floating → expected Actual/360)")
    if issues:
        return FAIL, "; ".join(issues)
    dc_pass = "30/360" if rate_type == "Fixed" else "Actual/360"
    return PASS, f"ratings/dates uniform; day_count={dc_pass!r} ✓"


# ── runner ────────────────────────────────────────────────────────────────────

def run_all_checks(df, prefix_map):
    rows = []
    for _, bond in df.iterrows():
        cusip     = str(bond["cusip"]).strip()
        bond_name = str(bond["bond_name"]).strip()
        rate_type = str(bond["rate_type"]).strip()
        issuer    = str(bond["issuer"]).strip()

        stock_prefix = prefix_map.get(issuer)
        checks = {
            "V1": check_v1(cusip),
            "V2": check_v2(str(bond["isin"]).strip()),
            "V3": check_v3(cusip, stock_prefix, issuer),
            "V4": check_v4(rate_type,
                           bond["benchmark_treasury_yield_pct"],
                           bond["spread_to_treasury_bps"],
                           bond["yield_pct"]),
            "V5": check_v5(rate_type,
                           bond["coupon_pct"],
                           bond["yield_pct"],
                           bond["price_to_public_pct"]),
            "V6": check_v6(bond_name,
                           str(bond["trade_date"]),
                           str(bond["settlement_date"]),
                           str(bond["maturity_date"])),
            "V7": check_v7(bond, df),
            "V8": check_v8(bond, df),
        }
        for check_id, (result, detail) in checks.items():
            rows.append({
                "issuer":    issuer,
                "cusip":     cusip,
                "bond_name": bond_name,
                "check":     check_id,
                "result":    result,
                "detail":    detail,
            })
    return pd.DataFrame(rows)


def main():
    df = pd.read_csv(IN_CSV, dtype=str)
    for col in ["coupon_pct", "yield_pct", "benchmark_treasury_yield_pct",
                "spread_to_treasury_bps", "price_to_public_pct"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    print(f"Loaded {len(df)} bonds from {IN_CSV.name}")

    # ── V3: look up stock CUSIP prefix per issuer ─────────────────────────────
    prefix_map = {}
    try:
        conn = psycopg2.connect(
            host=os.getenv("PGHOST", "localhost"),
            port=int(os.getenv("PGPORT", "5432")),
            dbname=os.getenv("PGDATABASE", "finance_platform"),
            user=os.getenv("PGUSER", "postgres"),
            password=os.getenv("PGPASSWORD"),
        )
        with conn.cursor() as cur:
            for issuer, search in ISSUER_SEARCH.items():
                cur.execute(
                    """SELECT cusip FROM security_master_history
                       WHERE name ILIKE %s AND cusip IS NOT NULL AND cusip != ''
                       ORDER BY period_end_date DESC LIMIT 1""",
                    (f"%{search}%",),
                )
                row = cur.fetchone()
                prefix = row[0][:6] if row and row[0] else None
                prefix_map[issuer] = prefix
                short = ISSUER_SHORT.get(issuer, issuer)
                print(f"  V3 prefix  {short:<12}: {prefix!r}")
        conn.close()
    except Exception as e:
        print(f"WARNING: DB connection failed — V3 will fail for all bonds: {e}")

    # ── run checks ────────────────────────────────────────────────────────────
    val = run_all_checks(df, prefix_map)
    val.to_csv(OUT_CSV, index=False)
    print(f"\nWrote {len(val)} rows → {OUT_CSV.name}")

    # ── compact failure-count table: checks × companies ───────────────────────
    fails = val[val["result"] == FAIL].copy()
    fails["company"] = fails["issuer"].map(ISSUER_SHORT)

    companies = ["Apple", "Alphabet", "Eli Lilly", "J&J", "Microsoft"]
    checks    = ["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8"]

    # Build matrix
    pivot = (
        fails.groupby(["check", "company"])
             .size()
             .unstack(fill_value=0)
             .reindex(index=checks, columns=companies, fill_value=0)
    )
    pivot["TOTAL"] = pivot.sum(axis=1)

    print("\nFailure counts (0 = all passed):")
    print(pivot.to_string())

    # ── top-10 failures: one representative per (check, company) bucket ──────
    if fails.empty:
        print("\nAll bonds passed every check.")
    else:
        # Pick the first failure per (check, company) so all checks/companies are visible
        diverse = (
            fails.groupby(["check", "company"], sort=True)
                 .first()
                 .reset_index()
                 .sort_values(["check", "company"])
                 .head(10)
        )
        print(f"\nSample failures ({len(fails)} total — one per check/company bucket):")
        for _, r in diverse.iterrows():
            short_name = r["bond_name"][:40].strip()
            company = r["company"] if "company" in r else ISSUER_SHORT.get(r["issuer"], r["issuer"])
            print(f"  {r['check']}  {company:<10}  {r['cusip']}  "
                  f"{short_name:<40}  {r['detail'][:75]}")


if __name__ == "__main__":
    main()
