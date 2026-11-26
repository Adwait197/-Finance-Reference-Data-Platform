#!/usr/bin/env python3
"""
Module 5 — Validation checks V1-V10 for all 106 build-set bonds.

Changes from the previous version:
  V3  — also accepts CUSIP prefixes listed in data/reference/issuer_prefixes.csv
          (for Alphabet Canada ULC bonds confirmed via OpenFIGI)
  V4  — returns not_applicable when the document does not contain a benchmark yield
          (instead of failing the bond for missing data)
  V5  — compares coupon against the yield of the same payment frequency
          (annual yield for EUR/GBP annual-pay bonds; semi-annual for USD/CAD)
  V6  — also accepts the maturity year when it appears anywhere in the bond name
          (handles names like "The 2028 Floating Rate Notes")
  V7  — currency-aware day count check driven by data/reference/day_count_conventions.csv;
          any (currency, rate_type) combination not in that file → needs_review, not fail

Rule changes are documented in the two reference CSV files; nothing is auto-passed.
"""
import os, re
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

IN_CSV          = Path(__file__).parent.parent / "data" / "processed" / "module5_build_tranches.csv"
OUT_CSV         = Path(__file__).parent.parent / "data" / "processed" / "module5_build_validation.csv"
PREFIXES_CSV    = Path(__file__).parent.parent / "data" / "reference"  / "issuer_prefixes.csv"
CONVENTIONS_CSV = Path(__file__).parent.parent / "data" / "reference" / "day_count_conventions.csv"
BENCHMARK_CONV_CSV = Path(__file__).parent.parent / "data" / "reference" / "benchmark_conventions.csv"
EXCEPTIONS_CSV  = Path(__file__).parent.parent / "data" / "reference" / "expected_exceptions_module5.csv"

PASS             = "pass"
FAIL             = "fail"
NA               = "not applicable"    # check concept does not apply (e.g. floating-rate bond)
NOT_APPLICABLE   = "not_applicable"    # check applies but data is absent from the document
NEEDS_REVIEW     = "needs_review"      # (currency, rate_type) combo not yet in reference file
EXPECTED_REVIEWED = "expected_reviewed"  # pre-approved documented exception

# Map issuer column value → search fragment for security_master_history ILIKE
ISSUER_SEARCH = {
    "Apple":                 "Apple, Inc",
    "Alphabet Inc.":         "Alphabet, Inc",
    "Eli Lilly and Company": "Eli Lilly",
    "Johnson & Johnson":     "Johnson & Johnson",
    "Microsoft Corporation": "Microsoft Corp",
}

ISSUER_SHORT = {
    "Apple":                 "Apple",
    "Alphabet Inc.":         "Alphabet",
    "Eli Lilly and Company": "Eli Lilly",
    "Johnson & Johnson":     "J&J",
    "Microsoft Corporation": "Microsoft",
}


# ── reference-file loaders ────────────────────────────────────────────────────

def load_issuer_prefixes():
    """Return dict: issuer → set of approved extra CUSIP prefixes (6 chars, uppercase)."""
    extra = {}
    df = pd.read_csv(PREFIXES_CSV, dtype=str)
    for _, row in df.iterrows():
        company = str(row["company"]).strip()
        prefix  = str(row["cusip_prefix"]).strip().upper()[:6]
        extra.setdefault(company, set()).add(prefix)
    return extra


def load_day_count_conventions():
    """Return dict: (currency, rate_type) → expected_day_count (normalized)."""
    df = pd.read_csv(CONVENTIONS_CSV, dtype=str)
    conv = {}
    for _, row in df.iterrows():
        key = (str(row["currency"]).strip(), str(row["rate_type"]).strip())
        conv[key] = str(row["expected_day_count"]).strip()
    return conv


def load_benchmark_conventions():
    """Return dict: currency → yield_column_for_v4 (from benchmark_conventions.csv)."""
    df = pd.read_csv(BENCHMARK_CONV_CSV, dtype=str)
    return {
        str(row["currency"]).strip(): str(row["yield_column_for_v4"]).strip()
        for _, row in df.iterrows()
    }


def load_expected_exceptions():
    """Return dict: (cusip_upper, check) → reason (from expected_exceptions_module5.csv)."""
    exc = {}
    df = pd.read_csv(EXCEPTIONS_CSV, dtype=str)
    for _, row in df.iterrows():
        key = (str(row["cusip"]).strip().upper(), str(row["check"]).strip())
        exc[key] = str(row["reason"]).strip()
    return exc


def normalize_dc(raw):
    """Same normalization used by the extractor so comparisons are apples-to-apples."""
    if not raw:
        return raw
    r = re.sub(r"\s*/\s*", "/", str(raw).strip())
    r = re.sub(r"\s+", " ", r)
    if re.match(r"(?i)30/360", r):                      return "30/360"
    if re.match(r"(?i)actual/360", r):                  return "Actual/360"
    if re.match(r"(?i)act(?:ual)?/act(?:ual)?", r):     return "Actual/Actual (ICMA)"
    if re.match(r"(?i)actual/365", r):                  return "Actual/365"
    return r


# ── check functions ───────────────────────────────────────────────────────────

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

def check_v3(cusip, stock_prefix, issuer, extra_prefixes):
    """
    Bond CUSIP prefix must match the issuer's stock CUSIP prefix, OR be listed
    in data/reference/issuer_prefixes.csv (approved after OpenFIGI confirmation).
    """
    bond_prefix = str(cusip).strip().upper()[:6]
    approved = extra_prefixes.get(issuer, set())
    if bond_prefix in approved:
        return PASS, f"prefix {bond_prefix} approved in issuer_prefixes.csv for {issuer!r}"
    if stock_prefix is None:
        return FAIL, f"no stock CUSIP found for {issuer!r} in security_master_history"
    if bond_prefix == stock_prefix:
        return PASS, f"bond prefix {bond_prefix} = stock prefix {stock_prefix}"
    return FAIL, (f"bond prefix {bond_prefix} ≠ stock prefix {stock_prefix} "
                  f"and not in issuer_prefixes.csv")

def check_v4(rate_type, bm_yield, spread_bps, yld, yield_sa="", yield_ann="",
             currency="USD", bm_convs=None, cusip="", exceptions=None):
    """
    Verify: benchmark_yield + spread/100 ≈ yield (± 0.5 bps tolerance).

    Which yield column to compare is determined by benchmark_conventions.csv:
      GBP → yield_semiannual  (Gilt spread is quoted on a semi-annual basis)
      EUR → yield_annual      (Bund spread is quoted on an annual basis)
      USD → yield_pct         (equals semi-annual yield for USD bonds)
      CAD → yield_pct         (equals semi-annual yield for CAD bonds)

    Returns not_applicable when the document does not contain a benchmark yield.
    Returns expected_reviewed for bonds listed in expected_exceptions_module5.csv.
    """
    if str(rate_type).strip().lower() != "fixed":
        return NA, "floating-rate bond"

    # Pre-approved documented exception — checked before any numeric comparison
    if exceptions and (str(cusip).strip().upper(), "V4") in exceptions:
        return EXPECTED_REVIEWED, exceptions[(str(cusip).strip().upper(), "V4")]

    # Document did not contain a benchmark yield → not_applicable
    # (float(NaN) succeeds, so check pd.isna explicitly)
    try:
        bm = float(bm_yield)
    except (TypeError, ValueError):
        return NOT_APPLICABLE, "benchmark yield not in document"
    if pd.isna(bm):
        return NOT_APPLICABLE, "benchmark yield not in document"
    try:
        sp = float(spread_bps)
    except (TypeError, ValueError):
        return FAIL, "spread missing/non-numeric"
    computed = bm + sp / 100.0

    # Deterministic yield column selection from benchmark_conventions.csv
    curr = str(currency).strip()
    if bm_convs and curr in bm_convs:
        col_name = bm_convs[curr]
    else:
        col_name = "yield_pct"

    if col_name == "yield_semiannual":
        yld_val = yield_sa
    elif col_name == "yield_annual":
        yld_val = yield_ann
    else:
        yld_val = yld  # yield_pct

    try:
        y = float(yld_val)
    except (TypeError, ValueError):
        return FAIL, f"yield column {col_name!r} missing/non-numeric"

    diff = abs(computed - y)
    if diff <= 0.005:
        return PASS, f"|{bm}+{sp}/100−{y}| = {diff:.4f} ≤ 0.005 ({col_name})"
    return FAIL, f"|{bm}+{sp}/100−{y}| = {diff:.4f} > 0.005 ({col_name})"

def check_v5(rate_type, coupon, yld, price, coupon_freq="", yield_sa="", yield_ann=""):
    """
    Coupon vs yield pricing direction check.
    Uses yield of the same frequency as the coupon: annual yield for annual-pay
    bonds (EUR, GBP); semi-annual yield for semi-annual-pay bonds (USD, CAD).
    Falls back to yield_pct when the specific column is empty.
    """
    if str(rate_type).strip().lower() != "fixed":
        return NA, "floating-rate bond"
    cf = str(coupon_freq).strip().lower()
    if cf == "annual" and yield_ann:
        compare_yld = yield_ann
        freq_label  = "annual"
    elif cf in ("semi-annual",) and yield_sa:
        compare_yld = yield_sa
        freq_label  = "semi-annual"
    else:
        compare_yld = yld
        freq_label  = "yield_pct"
    try:
        c, y, p = float(coupon), float(compare_yld), float(price)
    except (TypeError, ValueError):
        return FAIL, "one or more numeric fields missing/non-numeric"
    if abs(c - y) < 1e-6:
        ok = abs(p - 100.0) <= 0.01
        return (PASS if ok else FAIL), f"par bond ({freq_label}), price {p}"
    elif c < y:
        ok = p < 100.0
        return (PASS if ok else FAIL), (
            f"coupon {c}% < {freq_label} yield {y}%, price {p} "
            f"{'✓' if ok else '✗ (expected <100)'}")
    else:
        ok = p > 100.0
        return (PASS if ok else FAIL), (
            f"coupon {c}% > {freq_label} yield {y}%, price {p} "
            f"{'✓' if ok else '✗ (expected >100)'}")

_DATE_FMT = "%B %d, %Y"

def _parse_date(s):
    s = re.sub(r"\s*\(.*\)\s*$", "", str(s).strip()).strip()
    return datetime.strptime(s, _DATE_FMT).date()

def check_v6(bond_name, trade_str, sett_str, mat_str):
    """
    Date ordering check: trade ≤ settlement < maturity.
    Accepts 'due YYYY' in bond name, and also falls back to any 4-digit year
    anywhere in the name (handles 'The 2028 Floating Rate Notes').
    """
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
    if m:
        name_year = int(m.group(1))
    else:
        # Fallback: accept any 4-digit year in the bond name
        m2 = re.search(r"\b(\d{4})\b", bond_name)
        if not m2:
            return FAIL, "no year found in bond_name"
        name_year = int(m2.group(1))
    if name_year != mat.year:
        return FAIL, f"name year {name_year} ≠ maturity year {mat.year}"
    return PASS, f"trade {trade} ≤ sett {sett} < mat {mat}, year {mat.year} ✓"

def check_v7(bond, all_df, conventions):
    """
    Deal-uniformity check + currency-aware day count check.
    Day count is validated against data/reference/day_count_conventions.csv.
    Returns needs_review (not fail) when the (currency, rate_type) combination
    is not yet covered by the reference file.
    """
    issuer     = str(bond["issuer"]).strip()
    trade_date = str(bond["trade_date"]).strip()
    currency   = str(bond.get("currency", "USD")).strip()
    rate_type  = str(bond["rate_type"]).strip()
    dc         = normalize_dc(str(bond["day_count"]).strip())

    # Deal uniformity
    deal_df = all_df[
        (all_df["issuer"].astype(str).str.strip() == issuer) &
        (all_df["trade_date"].astype(str).str.strip() == trade_date)
    ]
    issues = []
    for field in ["rating_moodys", "rating_sp", "trade_date", "settlement_date"]:
        vals = deal_df[field].astype(str).str.strip().unique()
        if len(vals) > 1:
            issues.append(f"{field} not uniform in deal: {list(vals)}")

    # Currency-aware day count using reference file
    key = (currency, rate_type)
    if key not in conventions:
        return NEEDS_REVIEW, (f"({currency}, {rate_type}) not in day_count_conventions.csv"
                              + ("; " + "; ".join(issues) if issues else ""))
    expected_dc = conventions[key]
    if dc != expected_dc:
        issues.append(f"day_count={dc!r} (expected {expected_dc!r} for {currency} {rate_type})")

    if issues:
        return FAIL, "; ".join(issues)
    return PASS, f"ratings/dates uniform; day_count={dc!r} ✓"

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


_VALID_MOODYS = {
    "Aaa", "Aa1", "Aa2", "Aa3", "A1", "A2", "A3",
    "Baa1", "Baa2", "Baa3", "Ba1", "Ba2", "Ba3",
    "B1",   "B2",   "B3",   "Caa1", "Caa2", "Caa3", "Ca", "C",
}
_VALID_SP = {
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
    "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
    "B+",   "B",   "B-",   "CCC+", "CCC", "CCC-", "CC", "C", "D",
}

def check_v10(principal_amount):
    """
    principal_amount must be a whole number of millions (divisible by 1,000,000).
    Missing principal → not_applicable.  Non-round amount → needs_review (not fail).
    Accepts both pre-parsed floats and raw strings like '€750,000,000' or '$1,250,000,000'.
    """
    import pandas as _pd
    val = principal_amount
    if _pd.isna(val) or val is None:
        return NOT_APPLICABLE, "principal_amount is null"
    s = str(val).strip()
    if s.lower() in ("", "nan", "none"):
        return NOT_APPLICABLE, "principal_amount is empty"
    # Strip currency symbols, commas, spaces so '€750,000,000' → '750000000'
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return NOT_APPLICABLE, f"principal_amount not numeric: {val!r}"
    try:
        v = float(s)
    except ValueError:
        return NOT_APPLICABLE, f"principal_amount not numeric: {val!r}"
    if v == 0:
        return NOT_APPLICABLE, "principal_amount is zero"
    if v % 1_000_000 == 0:
        return PASS, f"principal_amount {v:,.0f} is a whole number of millions"
    return NEEDS_REVIEW, f"principal_amount {v:,.0f} is not a whole number of millions"


def check_v9(rating_moodys, rating_sp):
    """
    Both ratings must be recognized credit grades.
    An absent rating is reported as 'not_found', not as pass or fail.
    A present but unrecognized value is a fail.
    """
    import pandas as _pd
    mdy = str(rating_moodys).strip() if (_pd.notna(rating_moodys) and
          str(rating_moodys).strip().lower() not in ("", "nan")) else ""
    sp  = str(rating_sp).strip()     if (_pd.notna(rating_sp)     and
          str(rating_sp).strip().lower()     not in ("", "nan")) else ""

    mdy_invalid = mdy and mdy not in _VALID_MOODYS
    sp_invalid  = sp  and sp  not in _VALID_SP

    if mdy_invalid or sp_invalid:
        parts = []
        if mdy_invalid: parts.append(f"Moody's {mdy!r} not a recognized grade")
        if sp_invalid:  parts.append(f"S&P {sp!r} not a recognized grade")
        if not mdy:     parts.append("Moody's not found")
        if not sp:      parts.append("S&P not found")
        return FAIL, "; ".join(parts)

    if not mdy or not sp:
        parts = []
        if not mdy: parts.append("Moody's not found")
        if not sp:  parts.append("S&P not found")
        return "not_found", "; ".join(parts)

    return PASS, f"Moody's={mdy}, S&P={sp}"


# ── runner ────────────────────────────────────────────────────────────────────

def run_all_checks(df, prefix_map, extra_prefixes, conventions,
                   bm_convs=None, exceptions=None):
    rows = []
    for _, bond in df.iterrows():
        cusip       = str(bond["cusip"]).strip()
        bond_name   = str(bond["bond_name"]).strip()
        rate_type   = str(bond["rate_type"]).strip()
        issuer      = str(bond["issuer"]).strip()
        currency    = str(bond.get("currency", "USD")).strip()
        coupon_freq = str(bond.get("coupon_frequency", "")).strip()
        yield_sa    = str(bond.get("yield_semiannual", "")).strip()
        yield_ann   = str(bond.get("yield_annual", "")).strip()

        stock_prefix = prefix_map.get(issuer)
        checks = {
            "V1": check_v1(cusip),
            "V2": check_v2(str(bond["isin"]).strip()),
            "V3": check_v3(cusip, stock_prefix, issuer, extra_prefixes),
            "V4": check_v4(rate_type,
                           bond["benchmark_treasury_yield_pct"],
                           bond["spread_to_treasury_bps"],
                           bond["yield_pct"],
                           yield_sa, yield_ann,
                           currency, bm_convs, cusip, exceptions),
            "V5": check_v5(rate_type,
                           bond["coupon_pct"],
                           bond["yield_pct"],
                           bond["price_to_public_pct"],
                           coupon_freq, yield_sa, yield_ann),
            "V6": check_v6(bond_name,
                           str(bond["trade_date"]),
                           str(bond["settlement_date"]),
                           str(bond["maturity_date"])),
            "V7": check_v7(bond, df, conventions),
            "V8": check_v8(bond, df),
            "V9":  check_v9(bond.get("rating_moodys", ""), bond.get("rating_sp", "")),
            "V10": check_v10(bond.get("principal_amount")),
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

    # ── load reference files ──────────────────────────────────────────────────
    extra_prefixes = load_issuer_prefixes()
    conventions    = load_day_count_conventions()
    bm_convs       = load_benchmark_conventions()
    exceptions     = load_expected_exceptions()
    print(f"  issuer_prefixes.csv: {sum(len(v) for v in extra_prefixes.values())} approved extra prefixes")
    print(f"  day_count_conventions.csv: {len(conventions)} (currency, rate_type) entries")
    print(f"  benchmark_conventions.csv: {len(bm_convs)} currencies mapped")
    print(f"  expected_exceptions_module5.csv: {len(exceptions)} pre-approved exceptions")

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
    val = run_all_checks(df, prefix_map, extra_prefixes, conventions,
                         bm_convs=bm_convs, exceptions=exceptions)
    val.to_csv(OUT_CSV, index=False)
    print(f"\nWrote {len(val)} rows → {OUT_CSV.name}")

    # ── compact failure-count table: checks × companies ───────────────────────
    fails = val[val["result"] == FAIL].copy()
    fails["company"] = fails["issuer"].map(ISSUER_SHORT)

    companies = ["Apple", "Alphabet", "Eli Lilly", "J&J", "Microsoft"]
    checks    = ["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9", "V10"]

    pivot = (
        fails.groupby(["check", "company"])
             .size()
             .unstack(fill_value=0)
             .reindex(index=checks, columns=companies, fill_value=0)
    )
    pivot["TOTAL"] = pivot.sum(axis=1)

    print("\nFailure counts (0 = all passed):")
    print(pivot.to_string())

    # ── not_found counts for V9 ───────────────────────────────────────────────
    nf_rows = val[val["result"] == "not_found"].copy()
    if not nf_rows.empty:
        nf_rows["company"] = nf_rows["issuer"].map(ISSUER_SHORT)
        nf_pivot = (
            nf_rows.groupby(["check", "company"])
                   .size()
                   .unstack(fill_value=0)
                   .reindex(index=checks, columns=companies, fill_value=0)
        )
        nf_pivot["TOTAL"] = nf_pivot.sum(axis=1)
        nf_pivot = nf_pivot[nf_pivot["TOTAL"] > 0]
        if not nf_pivot.empty:
            print("\nnot_found counts (rating absent from document):")
            print(nf_pivot.to_string())

    # ── not_applicable counts per check ──────────────────────────────────────
    na_rows = val[val["result"] == NOT_APPLICABLE].copy()
    na_rows["company"] = na_rows["issuer"].map(ISSUER_SHORT)
    na_pivot = (
        na_rows.groupby(["check", "company"])
               .size()
               .unstack(fill_value=0)
               .reindex(index=checks, columns=companies, fill_value=0)
    )
    na_pivot["TOTAL"] = na_pivot.sum(axis=1)
    na_pivot = na_pivot[na_pivot["TOTAL"] > 0]

    if not na_pivot.empty:
        print("\nnot_applicable counts (data absent from document):")
        print(na_pivot.to_string())
    else:
        print("\nnot_applicable counts: none")

    # ── needs_review counts per check ────────────────────────────────────────
    nr_rows = val[val["result"] == NEEDS_REVIEW].copy()
    nr_rows["company"] = nr_rows["issuer"].map(ISSUER_SHORT)
    nr_pivot = (
        nr_rows.groupby(["check", "company"])
               .size()
               .unstack(fill_value=0)
               .reindex(index=checks, columns=companies, fill_value=0)
    )
    nr_pivot["TOTAL"] = nr_pivot.sum(axis=1)
    nr_pivot = nr_pivot[nr_pivot["TOTAL"] > 0]

    if not nr_pivot.empty:
        print("\nneeds_review counts ((currency, rate_type) not yet in conventions file):")
        print(nr_pivot.to_string())
    else:
        print("\nneeds_review counts: none")

    # ── expected_reviewed counts per check ───────────────────────────────────
    er_rows = val[val["result"] == EXPECTED_REVIEWED].copy()
    er_rows["company"] = er_rows["issuer"].map(lambda x: ISSUER_SHORT.get(x, x))
    er_pivot = (
        er_rows.groupby(["check", "company"])
               .size()
               .unstack(fill_value=0)
    )
    if not er_pivot.empty:
        er_pivot["TOTAL"] = er_pivot.sum(axis=1)
        er_pivot = er_pivot[er_pivot["TOTAL"] > 0]
        print("\nexpected_reviewed counts (pre-approved documented exceptions):")
        print(er_pivot.to_string())
    else:
        print("\nexpected_reviewed counts: none")

    # ── sample failures ───────────────────────────────────────────────────────
    if fails.empty:
        print("\nAll bonds passed every check (no failures).")
    else:
        diverse = (
            fails.groupby(["check", "company"], sort=True)
                 .first()
                 .reset_index()
                 .sort_values(["check", "company"])
                 .head(15)
        )
        print(f"\nSample failures ({len(fails)} total — one per check/company bucket):")
        for _, r in diverse.iterrows():
            short_name = r["bond_name"][:40].strip()
            print(f"  {r['check']}  {r['company']:<10}  {r['cusip']}  "
                  f"{short_name:<40}  {r['detail'][:75]}")


if __name__ == "__main__":
    main()
