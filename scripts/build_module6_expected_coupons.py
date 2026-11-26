#!/usr/bin/env python3
"""
Module 6, Step 1 — Expected coupon schedule ("our books"), built from document dates.

For every USD fixed-rate bond in reporting.bond_terms:
  - Uses interest_payment_dates from the FWP (e.g. "May 3 and November 3")
    to build the payment schedule.
  - Falls back to the maturity-day rule (maturity month/day and 6 months before)
    for bonds with no document dates.
  - Cross-checks document dates vs. the maturity-day rule; prints any disagreements.
  - Moves any date that falls on a weekend or US federal holiday to the next
    business day.
  - Keeps only payments falling in the calendar year 2026.
  - Adds principal repayment rows (flow_type = 'principal') for bonds maturing in 2026.

Output: data/processed/module6_expected_coupons.csv
"""
import os
import re
import calendar
from pathlib import Path
from datetime import date

import pandas as pd
import psycopg2
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

load_dotenv(Path(__file__).parent.parent / ".env")

OUT_CSV    = Path(__file__).parent.parent / "data" / "processed" / "module6_expected_coupons.csv"
FACE       = 10_000_000
YEAR_START = date(2026, 1, 1)
YEAR_END   = date(2026, 12, 31)

_USCAL = USFederalHolidayCalendar()
_US_BD = CustomBusinessDay(calendar=_USCAL)

_MONTH_NAMES = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


def _next_business_day(d: date) -> date:
    """Return d itself if it is a US business day, else the next business day."""
    result = _US_BD.rollforward(pd.Timestamp(d))
    return result.date()


def _parse_date(text) -> date | None:
    if not text or str(text).strip().lower() in ("", "nan", "none"):
        return None
    for fmt in ("%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return pd.to_datetime(text, format=fmt).date()
        except (ValueError, TypeError):
            pass
    try:
        return pd.to_datetime(text).date()
    except Exception:
        return None


def _parse_ip_months(ip_str) -> list[tuple[int, int]]:
    """
    Parse document interest payment dates string into (month, day) pairs.

    'May 3 and November 3' → [(5, 3), (11, 3)]
    Deduplicates so 'February 13 of each year, beginning February 13' → [(2, 13)]
    """
    if not ip_str or str(ip_str).strip().lower() in ('', 'nan', 'none'):
        return []
    pairs, seen = [], set()
    for m in re.finditer(
        r'(January|February|March|April|May|June|July|August'
        r'|September|October|November|December)\s+(\d{1,2})\b',
        str(ip_str), re.I,
    ):
        key = (_MONTH_NAMES[m.group(1).lower()], int(m.group(2)))
        if key not in seen:
            pairs.append(key)
            seen.add(key)
    return pairs


def _anchors_from_maturity(maturity: date, freq: str) -> list[tuple[int, int]]:
    """
    Derive (month, day) anchors from the maturity date using the period rule.
    semi-annual: maturity + 6 months earlier; annual: maturity only.
    """
    n = {'quarterly': 4, 'annual': 1}.get((freq or '').lower(), 2)
    anchors, seen = [], set()
    cur = maturity
    for _ in range(n):
        key = (cur.month, cur.day)
        if key not in seen:
            anchors.append(key)
            seen.add(key)
        cur = cur - relativedelta(months=12 // n)
    return anchors


def _payment_schedule(settlement: date, maturity: date,
                      anchors: list[tuple[int, int]]) -> list[date]:
    """
    All unadjusted coupon dates strictly after settlement up to and including maturity,
    built by hitting the given (month, day) anchors in every year of the bond's life.
    """
    if not anchors:
        return []
    dates: set[date] = set()
    for year in range(settlement.year, maturity.year + 1):
        for month, day in anchors:
            max_day = calendar.monthrange(year, month)[1]
            d = date(year, month, min(day, max_day))
            if settlement < d <= maturity:
                dates.add(d)
    return sorted(dates)


def main():
    conn = psycopg2.connect(
        host=os.environ["PGHOST"], port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
    )
    cur = conn.cursor()
    cur.execute("""
        SELECT company, cusip, bond_name, coupon_pct,
               coupon_frequency, settlement_date, maturity_date,
               interest_payment_dates
        FROM reporting.bond_terms
        WHERE currency = 'USD' AND rate_type = 'Fixed'
        ORDER BY company, settlement_date, cusip
    """)
    bonds = cur.fetchall()
    conn.close()

    print(f"Loaded {len(bonds)} USD fixed-rate bonds from reporting.bond_terms")

    # ── Frequency-change summary ───────────────────────────────────────────────
    freq_counts = {}
    for *_, cf, _, _, _ in bonds:
        k = (cf or 'unknown').lower()
        freq_counts[k] = freq_counts.get(k, 0) + 1

    print(f"\nCurrent coupon_frequency distribution (USD fixed bonds after Step 1&2 fix):")
    for k, v in sorted(freq_counts.items()):
        print(f"  {k:<12} : {v}")

    print(f"\nBonds whose coupon_frequency changed vs. the original extractor:")
    print(f"  23 Alphabet USD+CAD & Microsoft fixed bonds  : quarterly  → semi-annual")
    print(f"   5 Alphabet GBP fixed bonds (not in USD set) : semi-annual → annual  (bonus fix)")

    # ── Build the schedule ────────────────────────────────────────────────────
    all_rows      = []
    skipped       = []
    no_doc_dates  = []
    disagreements = []

    for co, cu, bn, cpn, cf, sett_raw, mat_raw, ip_str in bonds:
        sett = _parse_date(sett_raw)
        mat  = _parse_date(mat_raw)

        if sett is None or mat is None:
            skipped.append((cu, "unparseable dates"))
            continue
        if cpn is None:
            skipped.append((cu, "coupon_pct missing"))
            continue

        # Anchors from document payment dates
        doc_anchors  = _parse_ip_months(ip_str)
        # Anchors from maturity-day rule (used as cross-check / fallback)
        rule_anchors = _anchors_from_maturity(mat, cf or 'semi-annual')

        if doc_anchors and (mat.month, mat.day) in doc_anchors:
            # Document dates available and maturity aligns → use document
            anchors = doc_anchors
            src     = "document"
        elif doc_anchors:
            # Document dates available but maturity NOT in them → cross-check fails
            # Fall back to rule and record the disagreement
            disagreements.append(dict(
                company=co, cusip=cu, bond_name=bn, maturity=mat,
                doc_anchors=doc_anchors, rule_anchors=rule_anchors,
            ))
            anchors = rule_anchors
            src     = "rule"
        else:
            no_doc_dates.append((co, cu, bn))
            anchors = rule_anchors
            src     = "rule"

        n_per_year    = max(len(anchors), 1)
        coupon_amount = round(FACE * cpn / 100 / n_per_year, 2)
        raw_dates     = _payment_schedule(sett, mat, anchors)

        for raw_d in raw_dates:
            adj_d = _next_business_day(raw_d)
            if YEAR_START <= adj_d <= YEAR_END:
                all_rows.append({
                    "company":           co,
                    "cusip":             cu,
                    "bond_name":         bn,
                    "flow_type":         "coupon",
                    "payment_date_source": src,
                    "scheduled_date":    raw_d,
                    "payment_date":      adj_d,
                    "face_held":         FACE,
                    "coupon_pct":        cpn,
                    "expected_amount":   coupon_amount,
                })

        # Principal repayment row for bonds maturing in 2026
        if YEAR_START <= mat <= YEAR_END:
            mat_adj = _next_business_day(mat)
            all_rows.append({
                "company":           co,
                "cusip":             cu,
                "bond_name":         bn,
                "flow_type":         "principal",
                "payment_date_source": "document",
                "scheduled_date":    mat,
                "payment_date":      mat_adj,
                "face_held":         FACE,
                "coupon_pct":        cpn,
                "expected_amount":   FACE,
            })

    # ── Cross-check results ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"CROSS-CHECK: document dates vs. maturity-day rule")
    print(f"{'='*60}")
    if disagreements:
        print(f"  {len(disagreements)} bond(s) where maturity month/day ∉ document payment anchors:")
        for d in disagreements:
            doc_str  = ', '.join(
                f"{calendar.month_abbr[m]} {dy}" for m, dy in d['doc_anchors']
            )
            rule_str = ', '.join(
                f"{calendar.month_abbr[m]} {dy}" for m, dy in d['rule_anchors']
            )
            print(f"  {d['company']:<12} {d['cusip']}  {d['bond_name'][:45]}")
            print(f"    maturity  : {d['maturity']}")
            print(f"    doc dates : {doc_str}")
            print(f"    rule dates: {rule_str}")
    else:
        print("  No disagreements — every bond's maturity month/day matches a document date.")

    if no_doc_dates:
        print(f"\n  {len(no_doc_dates)} bond(s) used maturity-rule fallback (no document dates found):")
        for co, cu, bn in no_doc_dates:
            print(f"    {co:<12} {cu}  {bn[:50]}")


    if skipped:
        print(f"\nSkipped {len(skipped)} bond(s):")
        for cu, reason in skipped:
            print(f"  {cu}: {reason}")

    # ── Build DataFrame ───────────────────────────────────────────────────────
    df = pd.DataFrame(all_rows)
    df["payment_id"] = [f"M6-{i+1:04d}" for i in range(len(df))]
    df = df[["payment_id", "company", "cusip", "bond_name", "flow_type",
             "payment_date_source", "scheduled_date", "payment_date",
             "face_held", "coupon_pct", "expected_amount"]]
    df = df.sort_values(["payment_date", "company", "cusip"]).reset_index(drop=True)
    df["payment_id"] = [f"M6-{i+1:04d}" for i in range(len(df))]

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    # ── Payment date source counts ────────────────────────────────────────────
    coupon_rows = df[df["flow_type"] == "coupon"]
    src_counts  = coupon_rows.groupby("payment_date_source")["cusip"].agg(
        rows="count", bonds="nunique"
    ).reset_index()
    print(f"\n{'='*60}")
    print(f"PAYMENT DATE SOURCE (coupon rows only)")
    print(f"{'='*60}")
    for _, r in src_counts.iterrows():
        print(f"  {r['payment_date_source']:<10} : {int(r['rows']):>4} rows  "
              f"({int(r['bonds'])} bond(s))")

    # ── Overall summary ───────────────────────────────────────────────────────
    coupon_df    = df[df["flow_type"] == "coupon"]
    principal_df = df[df["flow_type"] == "principal"]

    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"  Bonds with ≥1 row in 2026     : {df['cusip'].nunique()}")
    print(f"  Coupon payments in 2026        : {len(coupon_df)}")
    print(f"  Principal repayments in 2026   : {len(principal_df)}")
    print(f"  Total rows in 2026             : {len(df)}")
    print(f"  Total coupon cash in 2026      : ${coupon_df['expected_amount'].sum():>15,.2f}")
    print(f"  Total principal cash in 2026   : ${principal_df['expected_amount'].sum():>15,.2f}")
    print(f"  Total cash in 2026             : ${df['expected_amount'].sum():>15,.2f}")
    print(f"  Saved → {OUT_CSV.name}")

    # ── Payments by company ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"PAYMENTS BY COMPANY (2026)")
    print(f"{'='*60}")
    by_co = (
        df.groupby("company")
        .agg(
            bonds      =("cusip",        "nunique"),
            coupons    =("flow_type",    lambda x: (x == "coupon").sum()),
            principals =("flow_type",    lambda x: (x == "principal").sum()),
            total      =("expected_amount", "sum"),
        )
        .sort_values("total", ascending=False)
    )
    print(f"  {'Company':<12}  {'Bonds':>5}  {'Coupons':>7}  {'Principal':>9}  {'Total cash':>18}")
    for co, row in by_co.iterrows():
        print(f"  {co:<12}  {int(row['bonds']):>5}  {int(row['coupons']):>7}"
              f"  {int(row['principals']):>9}  ${row['total']:>17,.2f}")

    # ── Apple 4.421% Notes due May 8, 2026 ───────────────────────────────────
    target = "037833ES5"
    print(f"\n{'='*60}")
    print(f"APPLE 4.421% NOTES DUE MAY 8, 2026  (CUSIP {target})")
    print(f"{'='*60}")
    ex = df[df["cusip"] == target]
    if ex.empty:
        print(f"  No 2026 rows found for {target}.")
    else:
        hdr = f"  {'payment_id':<10}  {'flow_type':<10}  {'scheduled':<12}  {'payment':<12}  {'amount':>16}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for _, row in ex.iterrows():
            print(f"  {row['payment_id']:<10}  {row['flow_type']:<10}  "
                  f"{str(row['scheduled_date']):<12}  {str(row['payment_date']):<12}  "
                  f"${row['expected_amount']:>15,.2f}")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
