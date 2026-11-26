#!/usr/bin/env python3
"""
Module 5 — V1-V8 validation for test-set run 2 (Meta, Broadcom).

Identical logic to validate_test_tranches.py; reads and writes the run-2 files.
Run-1 files (module5_test_validation.csv) are NEVER touched.

Output: data/processed/module5_test_run2_validation.csv
"""
import os, sys
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

from validate_build_tranches import (
    check_v1, check_v2, check_v3, check_v4, check_v5,
    check_v6, check_v7, check_v8, check_v9,
    load_issuer_prefixes, load_day_count_conventions,
    load_benchmark_conventions, load_expected_exceptions,
    run_all_checks,
    PASS, FAIL, NA, NOT_APPLICABLE, NEEDS_REVIEW, EXPECTED_REVIEWED,
)

load_dotenv(Path(__file__).parent.parent / ".env")

IN_CSV  = Path(__file__).parent.parent / "data" / "processed" / "module5_test_run2_tranches.csv"
OUT_CSV = Path(__file__).parent.parent / "data" / "processed" / "module5_test_run2_validation.csv"

ISSUER_SEARCH = {
    "Meta Platforms, Inc.": "Meta Platforms",
    "Broadcom Inc.":        "Broadcom",
}

ISSUER_SHORT = {
    "Meta Platforms, Inc.": "Meta",
    "Broadcom Inc.":        "Broadcom",
}


def main():
    df = pd.read_csv(IN_CSV, dtype=str)
    for col in ["coupon_pct", "yield_pct", "benchmark_treasury_yield_pct",
                "spread_to_treasury_bps", "price_to_public_pct"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    print(f"Loaded {len(df)} bonds from {IN_CSV.name}")

    extra_prefixes = load_issuer_prefixes()
    conventions    = load_day_count_conventions()
    bm_convs       = load_benchmark_conventions()
    exceptions     = load_expected_exceptions()
    print(f"  issuer_prefixes.csv:        {sum(len(v) for v in extra_prefixes.values())} extra prefixes")
    print(f"  day_count_conventions.csv:  {len(conventions)} entries")
    print(f"  benchmark_conventions.csv:  {len(bm_convs)} currencies")
    print(f"  expected_exceptions.csv:    {len(exceptions)} exceptions")

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
        print(f"WARNING: DB connection failed — V3 will fail: {e}")

    val = run_all_checks(df, prefix_map, extra_prefixes, conventions,
                         bm_convs=bm_convs, exceptions=exceptions)
    val.to_csv(OUT_CSV, index=False)
    print(f"\nWrote {len(val)} rows → {OUT_CSV.name}")

    checks    = ["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9"]
    companies = ["Meta", "Broadcom"]

    def _pivot(result_val):
        sub = val[val["result"] == result_val].copy()
        sub["company"] = sub["issuer"].map(lambda x: ISSUER_SHORT.get(x, x))
        if sub.empty:
            return pd.DataFrame(index=checks, columns=companies + ["TOTAL"]).fillna(0).astype(int)
        pv = (
            sub.groupby(["check", "company"])
               .size()
               .unstack(fill_value=0)
               .reindex(index=checks, columns=companies, fill_value=0)
        )
        pv["TOTAL"] = pv.sum(axis=1)
        return pv

    fail_piv = _pivot(FAIL)
    print("\nFailure counts (0 = all passed):")
    print(fail_piv.to_string())

    nf_piv = _pivot("not_found")
    nf_piv = nf_piv[nf_piv["TOTAL"] > 0]
    if not nf_piv.empty:
        print("\nnot_found counts (rating absent from document):")
        print(nf_piv.to_string())

    na_piv = _pivot(NOT_APPLICABLE)
    na_piv = na_piv[na_piv["TOTAL"] > 0]
    if not na_piv.empty:
        print("\nnot_applicable counts:")
        print(na_piv.to_string())
    else:
        print("\nnot_applicable counts: none")

    nr_piv = _pivot(NEEDS_REVIEW)
    nr_piv = nr_piv[nr_piv["TOTAL"] > 0]
    if not nr_piv.empty:
        print("\nneeds_review counts:")
        print(nr_piv.to_string())

    er_piv = _pivot(EXPECTED_REVIEWED)
    er_piv = er_piv[er_piv["TOTAL"] > 0]
    if not er_piv.empty:
        print("\nexpected_reviewed counts:")
        print(er_piv.to_string())

    fails = val[val["result"] == FAIL]
    if not fails.empty:
        print(f"\nFail details ({len(fails)} total):")
        for _, r in fails.sort_values(["check", "cusip"]).iterrows():
            short_name = r["bond_name"][:40].strip()
            print(f"  {r['check']}  {ISSUER_SHORT.get(r['issuer'],r['issuer']):<10}  "
                  f"{r['cusip']}  {short_name:<40}  {str(r['detail'])[:75]}")
    else:
        print("\nAll bonds pass all applicable checks.")


if __name__ == "__main__":
    main()
