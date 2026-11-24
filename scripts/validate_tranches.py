#!/usr/bin/env python3
"""
Module 5 — Validation checks for module5_apple_tranches.csv (Apple only).

Checks:
  V1 — CUSIP check digit  (double-add-double modulus-10 algorithm)
  V2 — ISIN check digit   (Luhn on digit-expanded 12-char ISIN)
  V3 — bond CUSIP issuer prefix matches Apple stock CUSIP prefix in
       security_master_history (latest quarter)
  V4 — fixed-rate only: |benchmark_yield + spread_bps/100 - yield| <= 0.005
  V5 — fixed-rate only: coupon vs yield → price direction (discount/par/premium)
  V6 — trade_date <= settlement_date < maturity_date;
       maturity year equals year in bond_name ("due YYYY")

Floating-rate bonds receive "not applicable" for V4 and V5 (not "pass").
"""
import os, re
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

IN_CSV  = Path(__file__).parent.parent / "data" / "processed" / "module5_apple_tranches.csv"
OUT_CSV = Path(__file__).parent.parent / "data" / "processed" / "module5_validation.csv"

PASS = "pass"
FAIL = "fail"
NA   = "not applicable"

CHECKS = ["V1", "V2", "V3", "V4", "V5", "V6"]


# ── V1: CUSIP check digit ─────────────────────────────────────────────────────

def _cusip_char_val(c: str) -> int:
    if c.isdigit():
        return int(c)
    if c.isalpha():
        return ord(c.upper()) - ord("A") + 10
    if c == "*":
        return 36
    if c == "@":
        return 37
    if c == "#":
        return 38
    raise ValueError(f"invalid CUSIP character {c!r}")


def check_v1(cusip: str) -> tuple[str, str]:
    """CUSIP check digit — double-add-double algorithm."""
    cusip = str(cusip).strip().upper()
    if len(cusip) != 9:
        return FAIL, f"length {len(cusip)} ≠ 9"
    total = 0
    for i in range(8):
        v = _cusip_char_val(cusip[i])
        if i % 2 == 1:          # 1-indexed even positions (2,4,6,8) are doubled
            v *= 2
        if v >= 10:              # sum digits once
            v = (v // 10) + (v % 10)
        total += v
    expected = (10 - total % 10) % 10
    actual   = _cusip_char_val(cusip[8])
    if expected == actual:
        return PASS, f"check digit {cusip[8]} verified (sum={total})"
    return FAIL, f"computed check digit {expected} ≠ stored '{cusip[8]}' (={actual})"


# ── V2: ISIN check digit (Luhn) ───────────────────────────────────────────────

def _isin_digit_string(isin: str) -> str:
    s = ""
    for c in isin:
        if c.isdigit():
            s += c
        elif c.isalpha():
            s += str(ord(c.upper()) - ord("A") + 10)
        else:
            raise ValueError(f"invalid ISIN character {c!r}")
    return s


def check_v2(isin: str) -> tuple[str, str]:
    """ISIN check digit — Luhn algorithm on digit-expanded ISIN."""
    isin = str(isin).strip().upper()
    if len(isin) != 12:
        return FAIL, f"length {len(isin)} ≠ 12"
    if not isin[:2].isalpha():
        return FAIL, f"country code {isin[:2]!r} not alphabetic"
    try:
        digits = _isin_digit_string(isin)
    except ValueError as exc:
        return FAIL, str(exc)
    total = 0
    for i, d in enumerate(reversed(digits)):
        v = int(d)
        if i % 2 == 1:          # every 2nd position from right is doubled
            v *= 2
            if v > 9:
                v -= 9
        total += v
    if total % 10 == 0:
        return PASS, f"Luhn sum {total} ≡ 0 (mod 10)"
    return FAIL, f"Luhn sum {total} not divisible by 10"


# ── V3: issuer CUSIP prefix from security_master_history ─────────────────────

def _get_stock_cusip_prefix(conn, ticker_name: str = "Apple, Inc.") -> str | None:
    """Return first 6 chars of issuer's stock CUSIP from the latest quarter."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cusip
            FROM security_master_history
            WHERE name ILIKE %s
              AND cusip IS NOT NULL
              AND cusip != ''
            ORDER BY period_end_date DESC
            LIMIT 1
            """,
            (f"%{ticker_name}%",),
        )
        row = cur.fetchone()
    return row[0][:6] if row and row[0] else None


def check_v3(cusip: str, stock_prefix: str | None) -> tuple[str, str]:
    """Bond CUSIP issuer prefix matches Apple stock CUSIP prefix."""
    bond_prefix = str(cusip).strip().upper()[:6]
    if stock_prefix is None:
        return FAIL, "no Apple CUSIP found in security_master_history"
    if bond_prefix == stock_prefix:
        return PASS, f"bond prefix {bond_prefix} = stock prefix {stock_prefix}"
    return FAIL, f"bond prefix {bond_prefix} ≠ stock prefix {stock_prefix}"


# ── V4: yield arithmetic (fixed rate only) ───────────────────────────────────

def check_v4(rate_type: str, bm_yield, spread_bps, yld) -> tuple[str, str]:
    """Fixed only: |benchmark_yield + spread_bps/100 - yield| <= 0.005."""
    if str(rate_type).strip().lower() != "fixed":
        return NA, "floating-rate bond"
    bm, sp, y = float(bm_yield), float(spread_bps), float(yld)
    diff = abs(bm + sp / 100.0 - y)
    if diff <= 0.005:
        return PASS, f"|{bm}+{sp}/100−{y}| = {diff:.6f} ≤ 0.005"
    return FAIL, f"|{bm}+{sp}/100−{y}| = {diff:.6f} > 0.005"


# ── V5: coupon/yield/price direction (fixed rate only) ───────────────────────

def check_v5(rate_type: str, coupon, yld, price) -> tuple[str, str]:
    """Fixed only: coupon vs yield implies price < / ≈ / > 100."""
    if str(rate_type).strip().lower() != "fixed":
        return NA, "floating-rate bond"
    c, y, p = float(coupon), float(yld), float(price)
    if abs(c - y) < 1e-6:                           # par bond
        ok = abs(p - 100.0) <= 0.01
        verb = "within 0.01 of 100" if ok else f"{abs(p-100):.4f} from 100 (> 0.01)"
        return (PASS if ok else FAIL), f"coupon=yield={c:.4f}%, price {p} {verb}"
    elif c < y:                                      # discount bond
        ok = p < 100.0
        return (PASS if ok else FAIL), f"coupon {c}% < yield {y}%, price {p} {'<' if ok else '≥'} 100"
    else:                                            # premium bond
        ok = p > 100.0
        return (PASS if ok else FAIL), f"coupon {c}% > yield {y}%, price {p} {'>' if ok else '≤'} 100"


# ── V6: date ordering + maturity year vs bond_name ───────────────────────────

_DATE_FMT = "%B %d, %Y"   # "November 9, 2026"


def _parse_date(s: str):
    s = str(s).strip()
    # Strip trailing parenthetical like " (T+2)"
    s = re.sub(r"\s*\(.*\)\s*$", "", s).strip()
    return datetime.strptime(s, _DATE_FMT).date()


def check_v6(bond_name: str, trade_str: str, sett_str: str, mat_str: str) -> tuple[str, str]:
    """trade_date <= settlement_date < maturity_date; year matches bond_name."""
    try:
        trade = _parse_date(trade_str)
        sett  = _parse_date(sett_str)
        mat   = _parse_date(mat_str)
    except ValueError as exc:
        return FAIL, f"date parse error: {exc}"

    if trade > sett:
        return FAIL, f"trade {trade} > settlement {sett}"
    if sett >= mat:
        return FAIL, f"settlement {sett} ≥ maturity {mat}"

    m = re.search(r"due\s+(\d{4})", bond_name, re.IGNORECASE)
    if not m:
        return FAIL, f"cannot find 'due YYYY' in bond_name: {bond_name!r}"

    name_year = int(m.group(1))
    mat_year  = mat.year
    if name_year != mat_year:
        return FAIL, (
            f"maturity year {mat_year} ≠ year in bond_name {name_year} "
            f"(trade {trade} ≤ settlement {sett} < maturity {mat} ✓)"
        )
    return PASS, f"trade {trade} ≤ settlement {sett} < maturity {mat}, year {mat_year} ✓"


# ── runner ────────────────────────────────────────────────────────────────────

def run_all_checks(df: pd.DataFrame, stock_prefix: str | None) -> pd.DataFrame:
    rows = []
    for _, bond in df.iterrows():
        cusip     = str(bond["cusip"]).strip()
        bond_name = str(bond["bond_name"]).strip()
        rate_type = str(bond["rate_type"]).strip()

        checks = {
            "V1": check_v1(cusip),
            "V2": check_v2(str(bond["isin"]).strip()),
            "V3": check_v3(cusip, stock_prefix),
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
        }
        for check_id, (result, detail) in checks.items():
            rows.append({
                "cusip":     cusip,
                "bond_name": bond_name,
                "check":     check_id,
                "result":    result,
                "detail":    detail,
            })
    return pd.DataFrame(rows)


def print_summary(val: pd.DataFrame, label: str = ""):
    title = f"Summary{' — ' + label if label else ''}"
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")
    summary = (
        val.groupby(["check", "result"])
           .size()
           .unstack(fill_value=0)
           .reindex(columns=[PASS, FAIL, NA], fill_value=0)
    )
    print(summary.to_string())
    print(f"{'─'*60}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    # ── load data ────────────────────────────────────────────────────────────
    df = pd.read_csv(IN_CSV, dtype=str)
    for col in ["coupon_pct", "yield_pct", "benchmark_treasury_yield_pct",
                "spread_to_treasury_bps", "price_to_public_pct"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    print(f"Loaded {len(df)} bonds from {IN_CSV.name}")

    # ── V3: get Apple stock CUSIP prefix from DB ──────────────────────────────
    stock_prefix = None
    try:
        conn = psycopg2.connect(
            host=os.getenv("PGHOST", "localhost"),
            port=int(os.getenv("PGPORT", "5432")),
            dbname=os.getenv("PGDATABASE", "finance_platform"),
            user=os.getenv("PGUSER", "postgres"),
            password=os.getenv("PGPASSWORD"),
        )
        stock_prefix = _get_stock_cusip_prefix(conn)
        print(f"V3: Apple stock CUSIP prefix from DB = '{stock_prefix}' "
              f"(from security_master_history latest quarter)")
        conn.close()
        conn = psycopg2.connect(
            host=os.getenv("PGHOST", "localhost"),
            port=int(os.getenv("PGPORT", "5432")),
            dbname=os.getenv("PGDATABASE", "finance_platform"),
            user=os.getenv("PGUSER", "postgres"),
            password=os.getenv("PGPASSWORD"),
        )
    except Exception as exc:
        print(f"WARNING: DB connection failed — V3 will fail: {exc}")
        conn = None

    # ── run checks on real data ───────────────────────────────────────────────
    val = run_all_checks(df, stock_prefix)
    val.to_csv(OUT_CSV, index=False)
    print(f"Wrote {len(val)} rows to {OUT_CSV.name}")
    print_summary(val, "real data (16 bonds × 6 checks)")

    if conn:
        conn.close()

    # ── deliberate error test (in-memory only, never saved) ──────────────────
    print("\n" + "═"*60)
    print("  Deliberate error test — 3 injected faults")
    print("  (this modified copy is NEVER saved to disk)")
    print("═"*60)

    broken = df.copy()

    # Error 1 — V1: flip the check digit of 037833FC9 → 037833FC8
    mask1 = broken["cusip"] == "037833FC9"
    broken.loc[mask1, "cusip"] = "037833FC8"
    print("\n  Error 1 (V1): CUSIP 037833FC9 → 037833FC8 (wrong check digit)")

    # Error 2 — V4: swap yield_pct ↔ benchmark_treasury_yield_pct for 037833FH8
    mask2 = broken["cusip"] == "037833FH8"
    idx2  = broken.index[mask2][0]
    bm_orig = broken.at[idx2, "benchmark_treasury_yield_pct"]
    y_orig  = broken.at[idx2, "yield_pct"]
    broken.at[idx2, "benchmark_treasury_yield_pct"] = y_orig
    broken.at[idx2, "yield_pct"]                    = bm_orig
    print(f"  Error 2 (V4): 037833FH8 benchmark {bm_orig}↔yield {y_orig} swapped")

    # Error 3 — V6: shift maturity_date year by +1 for 037833FJ4
    mask3 = broken["cusip"] == "037833FJ4"
    old_mat = broken.loc[mask3, "maturity_date"].iloc[0]
    new_mat = old_mat.replace("2036", "2037")        # bond_name still says "due 2036"
    broken.loc[mask3, "maturity_date"] = new_mat
    print(f"  Error 3 (V6): 037833FJ4 maturity '{old_mat}' → '{new_mat}' (name unchanged)")

    val_broken = run_all_checks(broken, stock_prefix)

    # Show only the checks expected to catch each injected error
    targets = [
        ("V1", "037833FC8"),   # broken CUSIP
        ("V4", "037833FH8"),   # swapped yields
        ("V6", "037833FJ4"),   # wrong maturity year
    ]
    print()
    for check_id, cusip in targets:
        row = val_broken[(val_broken["cusip"] == cusip) & (val_broken["check"] == check_id)]
        if row.empty:
            print(f"  {check_id} {cusip}: <no row found>")
        else:
            r = row.iloc[0]
            print(f"  {check_id} {cusip}: result={r['result']}")
            print(f"    detail: {r['detail']}")

    print_summary(val_broken, "broken copy (3 errors injected)")


if __name__ == "__main__":
    main()
