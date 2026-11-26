#!/usr/bin/env python3
"""
Module 6, Step 3 — Cash reconciliation (v2: same-bond pairing, split/combined detection).

Matches module6_expected_coupons.csv ("our books") against
module6_bank_statement.csv (bank statement) using nine ordered passes.
Each row is used at most once on either side.

Thresholds are read from data/reference/reconciliation_rules.csv:
  late_window      = 5 business days   (beyond → late_needs_review)
  short_threshold  = 1.0 %             (beyond → large_deduction_needs_review)

MUST NOT read data/processed/module6_answer_key.csv (or any hard/round3 answer key).

Passes (in order):
  1. exact            — same CUSIP, same date, same amount (< $0.01 diff)
  2. combined         — one stmt row (CUSIP in books) = sum of 2+ same-company
                        same-date unmatched expected rows
  3. split            — 2+ stmt rows with same CUSIP + date sum to one expected
  4. late             — same CUSIP, same amount, 1–LATE_MAX bdays after expected date
  5. short_paid       — same CUSIP, EXACT date, shortfall 0 < x < SHORT_PCT of expected
  6. cusip_typo       — CUSIP not in books, Hamming-1 from an unmatched expected CUSIP,
                        same amount and date
  7. duplicate        — matches a statement row already matched in passes 1–5
  8. same_bond_pairing— remaining stmt rows whose CUSIP is in books: pair with the
                        nearest-dated unmatched expected for that CUSIP, then classify
                        the difference (labels: late, short_paid, late_and_short,
                        late_needs_review, large_deduction_needs_review, needs_review)
  9. remaining        — truly missing (expected never matched) /
                        unexpected (stmt CUSIP not in books and not Hamming-1 typo)

Output: data/processed/module6_recon_results.csv
"""
import itertools
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

ROOT          = Path(__file__).parent.parent
EXPECTED_CSV  = ROOT / "data" / "processed" / "module6_expected_coupons.csv"
STATEMENT_CSV = ROOT / "data" / "processed" / "module6_bank_statement.csv"
RECON_CSV     = ROOT / "data" / "processed" / "module6_recon_results.csv"
RULES_CSV     = ROOT / "data" / "reference"  / "reconciliation_rules.csv"

_USCAL    = USFederalHolidayCalendar()
_HOLIDAYS = (
    _USCAL.holidays(start="2020-01-01", end="2030-12-31")
    .strftime("%Y-%m-%d")
    .tolist()
)


def _bdays(d1, d2) -> int:
    """Signed US-business-day count d1→d2 (positive = d2 is later)."""
    return int(np.busday_count(str(d1), str(d2), holidays=_HOLIDAYS))


def _cusip_valid(cusip: str) -> bool:
    """True iff the 9-character CUSIP passes the standard check-digit algorithm."""
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


def _classify(days_late: int, shortfall: float, exp_amt: float,
              late_max: int, short_pct: float) -> str:
    """
    Label a same-bond pairing by how the payment differs from expectation.

    days_late  : signed business-day count (expected→statement date);
                 positive = arrived late, negative = arrived early
    shortfall  : expected_amount − received_amount;
                 positive = underpayment, negative = overpayment
    exp_amt    : expected amount (used to compute % thresholds)
    late_max   : LATE_MAX from reconciliation_rules (e.g. 5)
    short_pct  : SHORT_PCT from reconciliation_rules (e.g. 0.01)
    """
    is_late    = days_late >= 1
    is_early   = days_late < 0
    is_exact   = abs(shortfall) < 0.01
    is_short   = shortfall > 0.009  # > $0.01 underpayment
    is_over    = shortfall < -0.009  # overpayment

    if is_early:
        return "needs_review"

    if is_exact:
        if days_late == 0:
            return "matched"   # shouldn't reach here normally
        if 1 <= days_late <= late_max:
            return "late"
        return "late_needs_review"

    if is_short:
        short_ok = shortfall < short_pct * exp_amt
        if not is_late:
            return "short_paid" if short_ok else "large_deduction_needs_review"
        # Both late and short
        late_ok = 1 <= days_late <= late_max
        if late_ok and short_ok:
            return "late_and_short"
        if not late_ok and short_ok:
            return "late_needs_review"
        if late_ok and not short_ok:
            return "large_deduction_needs_review"
        return "needs_review"

    if is_over:
        return "needs_review"

    return "needs_review"


def main():
    # ── Load thresholds from rules CSV ─────────────────────────────────────
    rules      = pd.read_csv(RULES_CSV).set_index("rule_name")
    LATE_MAX   = int(rules.loc["late_window",     "threshold_value"])   # 5
    SHORT_PCT  = float(rules.loc["short_threshold", "threshold_value"]) / 100  # 0.01

    exp  = pd.read_csv(EXPECTED_CSV)
    stmt = pd.read_csv(STATEMENT_CSV)

    exp["payment_date"]    = pd.to_datetime(exp["payment_date"]).dt.date
    exp["expected_amount"] = exp["expected_amount"].astype(float)
    exp["cusip"]           = exp["cusip"].astype(str)

    stmt["value_date"] = pd.to_datetime(stmt["value_date"]).dt.date
    stmt["amount"]     = stmt["amount"].astype(float)
    stmt["cusip"]      = stmt["cusip"].astype(str)

    book_cusips     = set(exp["cusip"])
    cusip_to_co     = {str(r["cusip"]): r["company"] for r in exp.to_dict("records")}

    exp_matched  = set()   # payment_ids already matched
    stmt_matched = set()   # statement_row_ids already matched
    results      = []

    exp_rows  = exp.to_dict("records")
    stmt_rows = stmt.to_dict("records")

    def _record(result, er=None, sr=None, days_late=None, note=None):
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
            "_note":               note,
        })

    # ── Pass 1: exact match ───────────────────────────────────────────────
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

    # ── Pass 2: combined — one stmt row = sum of 2+ same-company same-date
    #            expected rows ────────────────────────────────────────────
    for sr in stmt_rows:
        if sr["statement_row_id"] in stmt_matched:
            continue
        if sr["cusip"] not in book_cusips:
            continue

        company   = cusip_to_co.get(sr["cusip"])
        stmt_date = sr["value_date"]

        same_co_date = [er for er in exp_rows
                        if er["company"] == company
                        and er["payment_date"] == stmt_date
                        and er["payment_id"] not in exp_matched]

        if len(same_co_date) < 2:
            continue

        matched_subset = None
        for size in range(2, min(len(same_co_date) + 1, 6)):
            for subset in itertools.combinations(same_co_date, size):
                if abs(sum(e["expected_amount"] for e in subset) - sr["amount"]) < 0.01:
                    matched_subset = subset
                    break
            if matched_subset:
                break

        if matched_subset:
            pids = [e["payment_id"] for e in matched_subset]
            note = "combined with " + ", ".join(pids)
            for er in matched_subset:
                _record("combined", er, sr, days_late=0,
                        note=f"combined with {', '.join(p for p in pids if p != er['payment_id'])}")
                exp_matched.add(er["payment_id"])
            stmt_matched.add(sr["statement_row_id"])

    # ── Pass 3: split — 2+ stmt rows same CUSIP + date sum to one expected ─
    for er in exp_rows:
        if er["payment_id"] in exp_matched:
            continue

        candidates = [sr for sr in stmt_rows
                      if sr["statement_row_id"] not in stmt_matched
                      and sr["cusip"] == er["cusip"]
                      and sr["value_date"] == er["payment_date"]]

        if len(candidates) < 2:
            continue

        matched_parts = None
        for size in range(2, min(len(candidates) + 1, 6)):
            for subset in itertools.combinations(candidates, size):
                if abs(sum(s["amount"] for s in subset) - er["expected_amount"]) < 0.01:
                    matched_parts = subset
                    break
            if matched_parts:
                break

        if matched_parts:
            for sr in matched_parts:
                _record("split", er, sr, days_late=0,
                        note=f"1 of {len(matched_parts)} parts")
                stmt_matched.add(sr["statement_row_id"])
            exp_matched.add(er["payment_id"])

    # ── Pass 4: late — same CUSIP + amount, 1–LATE_MAX bdays late ─────────
    for er in exp_rows:
        if er["payment_id"] in exp_matched:
            continue
        for sr in stmt_rows:
            if sr["statement_row_id"] in stmt_matched:
                continue
            if (sr["cusip"] == er["cusip"] and
                    abs(sr["amount"] - er["expected_amount"]) < 0.01):
                days = _bdays(er["payment_date"], sr["value_date"])
                if 1 <= days <= LATE_MAX:
                    _record("late", er, sr, days_late=days)
                    exp_matched.add(er["payment_id"])
                    stmt_matched.add(sr["statement_row_id"])
                    break

    # ── Pass 5: short_paid — same CUSIP, EXACT date, 0 < shortfall < SHORT_PCT ─
    for er in exp_rows:
        if er["payment_id"] in exp_matched:
            continue
        for sr in stmt_rows:
            if sr["statement_row_id"] in stmt_matched:
                continue
            if (sr["cusip"] == er["cusip"] and
                    sr["value_date"] == er["payment_date"]):
                shortfall = er["expected_amount"] - sr["amount"]
                if 0 < shortfall < SHORT_PCT * er["expected_amount"]:
                    _record("short_paid", er, sr, days_late=0)
                    exp_matched.add(er["payment_id"])
                    stmt_matched.add(sr["statement_row_id"])
                    break

    # ── Pass 6: cusip_typo — not in books, Hamming-1, same amount + date ──
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
                _record("cusip_typo", er, sr, days_late=0, note=note)
                exp_matched.add(er["payment_id"])
                stmt_matched.add(sr["statement_row_id"])
                break

    # ── Pass 7: duplicate — matches already-matched expected ──────────────
    for sr in stmt_rows:
        if sr["statement_row_id"] in stmt_matched:
            continue
        for er in exp_rows:
            if er["payment_id"] not in exp_matched:
                continue
            if (sr["cusip"] == er["cusip"] and
                    sr["value_date"] == er["payment_date"] and
                    abs(sr["amount"] - er["expected_amount"]) < 0.01):
                _record("duplicate", er, sr, days_late=0)
                stmt_matched.add(sr["statement_row_id"])
                break

    # ── Pass 8: same_bond_pairing — any remaining in-books stmt row ───────
    # Pair with the nearest-dated unmatched expected for that CUSIP, then
    # classify the difference.  A known CUSIP is NEVER labelled "unexpected".
    for sr in sorted(
        [s for s in stmt_rows if s["statement_row_id"] not in stmt_matched],
        key=lambda s: s["value_date"]
    ):
        if sr["cusip"] not in book_cusips:
            continue

        # Find unmatched expected rows for this CUSIP
        candidates = [er for er in exp_rows
                      if er["cusip"] == sr["cusip"]
                      and er["payment_id"] not in exp_matched]

        if candidates:
            er = min(candidates,
                     key=lambda e: abs(_bdays(e["payment_date"], sr["value_date"])))
        else:
            # All expected for this CUSIP already matched → pair with nearest matched
            candidates_all = [er for er in exp_rows if er["cusip"] == sr["cusip"]]
            if not candidates_all:
                _record("needs_review", None, sr,
                        note=f"CUSIP {sr['cusip']} in books but no expected row found")
                stmt_matched.add(sr["statement_row_id"])
                continue
            er = min(candidates_all,
                     key=lambda e: abs(_bdays(e["payment_date"], sr["value_date"])))

        days_late = _bdays(er["payment_date"], sr["value_date"])
        shortfall = round(er["expected_amount"] - sr["amount"], 2)
        label     = _classify(days_late, shortfall, er["expected_amount"],
                              LATE_MAX, SHORT_PCT)

        if label == "late_needs_review":
            note = (f"{days_late} biz days late — beyond {LATE_MAX}-day window")
        elif label == "large_deduction_needs_review":
            pct = abs(shortfall) / er["expected_amount"] * 100
            note = f"short ${abs(shortfall):,.2f} ({pct:.1f}%) — exceeds {SHORT_PCT*100:.0f}% threshold"
        elif label == "late_and_short":
            note = f"{days_late} biz days late + short ${shortfall:,.2f}"
        else:
            note = f"days_late={days_late}  shortfall=${shortfall:,.2f}"

        _record(label, er, sr, days_late=days_late, note=note)
        exp_matched.add(er["payment_id"])
        stmt_matched.add(sr["statement_row_id"])

    # ── Pass 9: remaining → missing / unexpected ──────────────────────────
    for er in exp_rows:
        if er["payment_id"] not in exp_matched:
            _record("missing", er, None)

    for sr in stmt_rows:
        if sr["statement_row_id"] not in stmt_matched:
            _record("unexpected", None, sr)

    # ── Build DataFrame and save ──────────────────────────────────────────
    df = pd.DataFrame(results)
    _order = {
        "missing": 0, "unexpected": 1,
        "late": 2, "late_needs_review": 3,
        "short_paid": 4, "large_deduction_needs_review": 5,
        "late_and_short": 6,
        "split": 7, "combined": 8,
        "duplicate": 9, "cusip_typo": 10,
        "needs_review": 11,
        "matched": 99,
    }
    df["_sort"] = df["result"].map(lambda r: _order.get(r, 50))
    df = df.sort_values(["_sort", "amount_expected"],
                        ascending=[True, False]).reset_index(drop=True)

    out = df[["expected_payment_id", "statement_row_id", "result", "cusip",
              "amount_expected", "amount_received", "difference", "days_late"]]
    out.to_csv(RECON_CSV, index=False)

    print(f"Loaded  {len(exp)} expected rows,  {len(stmt)} statement rows")
    print(f"Results written → {RECON_CSV.name}  ({len(df)} rows)")

    # ── Break report ──────────────────────────────────────────────────────
    breaks = df[df["result"] != "matched"].copy()

    def _stake(row) -> float:
        r = row["result"]
        if r == "missing":
            return float(row["amount_expected"]) if pd.notna(row["amount_expected"]) else 0.0
        if r in ("unexpected", "duplicate", "cusip_typo",
                 "split", "combined", "needs_review"):
            return float(row["amount_received"]) if pd.notna(row["amount_received"]) else 0.0
        if r in ("late", "late_needs_review", "late_and_short"):
            return float(row["amount_received"]) if pd.notna(row["amount_received"]) else 0.0
        if r in ("short_paid", "large_deduction_needs_review"):
            return float(row["difference"])      if pd.notna(row["difference"])      else 0.0
        return 0.0

    breaks = breaks.copy()
    breaks["_stake"] = breaks.apply(_stake, axis=1)
    breaks = breaks.sort_values("_stake", ascending=False)

    print(f"\n{'='*78}")
    print(f"BREAK REPORT  (sorted by amount at stake, largest first)")
    print(f"{'='*78}")
    hdr = (f"  {'#':>3}  {'Result':<30}  {'CUSIP':<9}  {'Exp.Payment':<11}  "
           f"{'Stmt.Row':<8}  {'Expected':>12}  {'Received':>12}  {'Info'}")
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))

    for n, (_, row) in enumerate(breaks.iterrows(), 1):
        r   = row["result"].upper()
        cu  = str(row["cusip"])          if pd.notna(row["cusip"])          else "—"
        ep  = str(row["expected_payment_id"]) if pd.notna(row["expected_payment_id"]) else "—"
        sid = str(row["statement_row_id"])    if pd.notna(row["statement_row_id"])    else "—"
        ae  = f"${float(row['amount_expected']):>11,.2f}" if pd.notna(row["amount_expected"]) else " " * 13
        ar  = f"${float(row['amount_received']):>11,.2f}" if pd.notna(row["amount_received"]) else " " * 13
        note = str(row["_note"]) if pd.notna(row.get("_note")) else ""

        if row["result"] == "late":
            info = f"{int(row['days_late'])} biz day(s) late"
        elif row["result"] == "late_needs_review":
            info = note or f"{int(row['days_late'])} biz days late (beyond window)"
        elif row["result"] == "short_paid":
            info = f"short ${float(row['difference']):,.2f}"
        elif row["result"] == "large_deduction_needs_review":
            info = note or f"large shortfall ${float(row['difference']):,.2f}"
        elif row["result"] == "late_and_short":
            info = note or f"late + short"
        elif row["result"] == "missing":
            info = f"${float(row['amount_expected']):,.2f} not received"
        elif row["result"] == "duplicate":
            info = "extra copy"
        elif row["result"] == "unexpected":
            info = "CUSIP not in books"
        elif row["result"] == "cusip_typo":
            info = note if note else "CUSIP 1 char off"
        elif row["result"] == "split":
            info = note or "split payment"
        elif row["result"] == "combined":
            info = note or "combined payment"
        elif row["result"] == "needs_review":
            info = note or "review required"
        else:
            info = ""

        print(f"  {n:>3}  {r:<30}  {cu:<9}  {ep:<11}  {sid:<8}  {ae}  {ar}  {info}")

    # ── Counts by result ──────────────────────────────────────────────────
    print(f"\n{'='*78}")
    print(f"COUNTS BY RESULT")
    print(f"{'='*78}")
    counts = df["result"].value_counts().sort_index()
    for result, count in counts.items():
        marker = "  ← break" if result != "matched" else ""
        print(f"  {result:<32} : {count:>4}{marker}")

    total_breaks = len(df[df["result"] != "matched"])
    print(f"\n  Total breaks flagged  : {total_breaks}")
    print(f"  Total matched (clean) : {len(df[df['result'] == 'matched'])}")
    print(f"\nDone.")


if __name__ == "__main__":
    main()
