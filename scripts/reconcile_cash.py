#!/usr/bin/env python3
"""
Module 6, Step 3 — Cash reconciliation.

Matches module6_expected_coupons.csv ("our books") against
module6_bank_statement.csv (bank statement) using six ordered passes,
each row used at most once on either side.

MUST NOT read data/processed/module6_answer_key.csv —
that file is the sealed answer key; ignore it completely.

Passes (in order):
  1. exact       — same CUSIP, same date, same amount
  2. late        — same CUSIP + amount, statement 1–5 biz days after expected
  3. short_paid  — same CUSIP, date within 5 biz days, amount 0–1% low
  4. duplicate   — matches a statement row already matched in passes 1–3
  5. cusip_typo  — CUSIP not in books, one char different, same amount + date
  6. remaining   — missing (expected not matched) / unexpected (statement not matched)

Output: data/processed/module6_recon_results.csv
"""
import numpy as np
from pathlib import Path
from datetime import date

import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

ROOT          = Path(__file__).parent.parent
EXPECTED_CSV  = ROOT / "data" / "processed" / "module6_expected_coupons.csv"
STATEMENT_CSV = ROOT / "data" / "processed" / "module6_bank_statement.csv"
RECON_CSV     = ROOT / "data" / "processed" / "module6_recon_results.csv"

_USCAL    = USFederalHolidayCalendar()
_HOLIDAYS = (
    _USCAL.holidays(start="2020-01-01", end="2030-12-31")
    .strftime("%Y-%m-%d")
    .tolist()
)


def _bdays(d1: date, d2: date) -> int:
    """Signed US-business-day count from d1 to d2 (positive means d2 is later)."""
    return int(np.busday_count(str(d1), str(d2), holidays=_HOLIDAYS))


def _cusip_valid(cusip: str) -> bool:
    """Return True if the 9-character CUSIP passes the standard check-digit algorithm."""
    if len(cusip) != 9:
        return False
    s = 0
    for i, c in enumerate(cusip[:8]):
        if c.isdigit():
            v = int(c)
        elif "A" <= c.upper() <= "Z":
            v = ord(c.upper()) - ord("A") + 10
        elif c == "*":
            v = 36
        elif c == "@":
            v = 37
        elif c == "#":
            v = 38
        else:
            return False
        if i % 2 == 1:
            v *= 2
        s += v // 10 + v % 10
    check = (10 - s % 10) % 10
    return cusip[8].isdigit() and int(cusip[8]) == check


def _hamming1(a: str, b: str) -> bool:
    """True iff a and b are both length-9 and differ in exactly one character."""
    return len(a) == len(b) == 9 and sum(x != y for x, y in zip(a, b)) == 1


def main():
    exp  = pd.read_csv(EXPECTED_CSV)
    stmt = pd.read_csv(STATEMENT_CSV)

    exp["payment_date"]    = pd.to_datetime(exp["payment_date"]).dt.date
    exp["expected_amount"] = exp["expected_amount"].astype(float)
    exp["cusip"]           = exp["cusip"].astype(str)

    stmt["value_date"] = pd.to_datetime(stmt["value_date"]).dt.date
    stmt["amount"]     = stmt["amount"].astype(float)
    stmt["cusip"]      = stmt["cusip"].astype(str)

    book_cusips = set(exp["cusip"])

    exp_matched  = set()   # payment_ids already matched
    stmt_matched = set()   # statement_row_ids already matched
    results      = []

    # Build lookup dicts for fast access (index → row dict)
    exp_rows  = exp.to_dict("records")
    stmt_rows = stmt.to_dict("records")

    def _record(result, er=None, sr=None, days_late=None, stmt_cusip_note=None):
        ep    = er["payment_id"]          if er else None
        sid   = sr["statement_row_id"]    if sr else None
        cusip = er["cusip"]               if er else (sr["cusip"] if sr else None)
        ae    = float(er["expected_amount"]) if er else None
        ar    = float(sr["amount"])          if sr else None
        diff  = round(ae - ar, 2) if (ae is not None and ar is not None) else None
        results.append({
            "expected_payment_id": ep,
            "statement_row_id":    sid,
            "result":              result,
            "cusip":               cusip,
            "amount_expected":     ae,
            "amount_received":     ar,
            "difference":          diff,
            "days_late":           days_late,
            "_stmt_cusip":         stmt_cusip_note,  # internal; stripped before save
        })

    # ── Pass 1: exact match ───────────────────────────────────────────────────
    for er in exp_rows:
        if er["payment_id"] in exp_matched:
            continue
        for sr in stmt_rows:
            if sr["statement_row_id"] in stmt_matched:
                continue
            if (sr["cusip"] == er["cusip"] and
                sr["value_date"] == er["payment_date"] and
                abs(sr["amount"] - er["expected_amount"]) < 0.01):
                _record("matched", er, sr, days_late=0)
                exp_matched.add(er["payment_id"])
                stmt_matched.add(sr["statement_row_id"])
                break

    # ── Pass 2: late (same CUSIP + amount, 1–5 biz days late) ─────────────────
    for er in exp_rows:
        if er["payment_id"] in exp_matched:
            continue
        for sr in stmt_rows:
            if sr["statement_row_id"] in stmt_matched:
                continue
            if (sr["cusip"] == er["cusip"] and
                abs(sr["amount"] - er["expected_amount"]) < 0.01):
                days = _bdays(er["payment_date"], sr["value_date"])
                if 1 <= days <= 5:
                    _record("late", er, sr, days_late=days)
                    exp_matched.add(er["payment_id"])
                    stmt_matched.add(sr["statement_row_id"])
                    break

    # ── Pass 3: short paid (same CUSIP, date ≤5 biz days off, amount 0–1% low) ─
    for er in exp_rows:
        if er["payment_id"] in exp_matched:
            continue
        for sr in stmt_rows:
            if sr["statement_row_id"] in stmt_matched:
                continue
            if sr["cusip"] == er["cusip"]:
                days      = abs(_bdays(er["payment_date"], sr["value_date"]))
                shortfall = er["expected_amount"] - sr["amount"]
                if days <= 5 and 0 < shortfall < 0.01 * er["expected_amount"]:
                    _record("short_paid", er, sr, days_late=0)
                    exp_matched.add(er["payment_id"])
                    stmt_matched.add(sr["statement_row_id"])
                    break

    # ── Pass 4: duplicate (matches an already-matched expected row) ───────────
    for sr in stmt_rows:
        if sr["statement_row_id"] in stmt_matched:
            continue
        for er in exp_rows:
            if er["payment_id"] not in exp_matched:
                continue   # only check already-matched expected rows
            if (sr["cusip"] == er["cusip"] and
                sr["value_date"] == er["payment_date"] and
                abs(sr["amount"] - er["expected_amount"]) < 0.01):
                _record("duplicate", er, sr, days_late=0)
                stmt_matched.add(sr["statement_row_id"])
                break

    # ── Pass 5: CUSIP typo (not in books, 1 char diff, same amount + date) ────
    for sr in stmt_rows:
        if sr["statement_row_id"] in stmt_matched:
            continue
        if sr["cusip"] in book_cusips:
            continue
        for er in exp_rows:
            if er["payment_id"] in exp_matched:
                continue
            if (_hamming1(sr["cusip"], er["cusip"]) and
                abs(sr["amount"] - er["expected_amount"]) < 0.01 and
                sr["value_date"] == er["payment_date"]):
                note = (f"stmt CUSIP {sr['cusip']} — check digit "
                        f"{'PASS' if _cusip_valid(sr['cusip']) else 'FAIL'}")
                _record("cusip_typo", er, sr, days_late=0, stmt_cusip_note=note)
                exp_matched.add(er["payment_id"])
                stmt_matched.add(sr["statement_row_id"])
                break

    # ── Pass 6: remaining rows → missing / unexpected ─────────────────────────
    for er in exp_rows:
        if er["payment_id"] not in exp_matched:
            _record("missing", er, None)

    for sr in stmt_rows:
        if sr["statement_row_id"] not in stmt_matched:
            _record("unexpected", None, sr)

    # ── Build DataFrame and save ──────────────────────────────────────────────
    df = pd.DataFrame(results)
    # Sort: breaks first (grouped by type), then matched; within breaks by amount desc
    _order = {"missing": 0, "unexpected": 1, "late": 2, "short_paid": 3,
              "duplicate": 4, "cusip_typo": 5, "matched": 6}
    df["_sort"] = df["result"].map(_order)
    df = df.sort_values(["_sort", "amount_expected"], ascending=[True, False]).reset_index(drop=True)

    out = df[["expected_payment_id", "statement_row_id", "result", "cusip",
              "amount_expected", "amount_received", "difference", "days_late"]]
    out.to_csv(RECON_CSV, index=False)

    print(f"Loaded  {len(exp)} expected rows,  {len(stmt)} statement rows")
    print(f"Results written → {RECON_CSV.name}  ({len(df)} rows)")

    # ── Break report (non-matched, sorted by amount at stake) ─────────────────
    breaks = df[df["result"] != "matched"].copy()

    def _stake(row) -> float:
        r = row["result"]
        if r == "missing":
            return float(row["amount_expected"]) if pd.notna(row["amount_expected"]) else 0.0
        if r in ("late", "duplicate", "unexpected", "cusip_typo"):
            return float(row["amount_received"]) if pd.notna(row["amount_received"]) else 0.0
        if r == "short_paid":
            return float(row["difference"])      if pd.notna(row["difference"])      else 0.0
        return 0.0

    breaks = breaks.copy()
    breaks["_stake"] = breaks.apply(_stake, axis=1)
    breaks = breaks.sort_values("_stake", ascending=False)

    print(f"\n{'='*78}")
    print(f"BREAK REPORT  (sorted by amount at stake, largest first)")
    print(f"{'='*78}")
    hdr = (f"  {'#':>3}  {'Result':<12}  {'CUSIP':<9}  {'Exp.Payment':<11}  "
           f"{'Stmt.Row':<8}  {'Expected':>12}  {'Received':>12}  {'Info'}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for n, (_, row) in enumerate(breaks.iterrows(), 1):
        r   = row["result"].upper()
        cu  = str(row["cusip"])          if pd.notna(row["cusip"])          else "—"
        ep  = str(row["expected_payment_id"]) if pd.notna(row["expected_payment_id"]) else "—"
        sid = str(row["statement_row_id"])    if pd.notna(row["statement_row_id"])    else "—"
        ae  = f"${float(row['amount_expected']):>11,.2f}" if pd.notna(row["amount_expected"]) else " " * 13
        ar  = f"${float(row['amount_received']):>11,.2f}" if pd.notna(row["amount_received"]) else " " * 13

        if row["result"] == "late":
            info = f"{int(row['days_late'])} biz day(s) late"
        elif row["result"] == "short_paid":
            info = f"short ${float(row['difference']):,.2f}"
        elif row["result"] == "missing":
            info = f"${float(row['amount_expected']):,.2f} not received"
        elif row["result"] == "duplicate":
            info = "extra copy"
        elif row["result"] == "unexpected":
            info = "not in books"
        elif row["result"] == "cusip_typo":
            note = row.get("_stmt_cusip", "") or ""
            info = note if note else "CUSIP 1 char off"
        else:
            info = ""

        print(f"  {n:>3}  {r:<12}  {cu:<9}  {ep:<11}  {sid:<8}  {ae}  {ar}  {info}")

    # ── Counts by result ──────────────────────────────────────────────────────
    print(f"\n{'='*78}")
    print(f"COUNTS BY RESULT")
    print(f"{'='*78}")
    counts = df["result"].value_counts().sort_index()
    for result, count in counts.items():
        marker = "  ← break" if result != "matched" else ""
        print(f"  {result:<14} : {count:>4}{marker}")

    total_breaks = len(df[df["result"] != "matched"])
    print(f"\n  Total breaks flagged  : {total_breaks}")
    print(f"  Total matched (clean) : {len(df[df['result'] == 'matched'])}")
    print(f"\nDone.")


if __name__ == "__main__":
    main()
