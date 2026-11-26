#!/usr/bin/env python3
"""
Module 6, harder round — Simulated bank statement with six advanced break types.

Seed: 7

Planted breaks (harder than the first round):
  - 2 late_and_short  : 2 biz days late AND $25 fee deducted (combo break)
  - 2 very_late       : 8 business days late (outside the 1–5 day window)
  - 2 large_deduction : 15% tax deducted (way outside the 0–1% short threshold)
  - 2 valid_surprise  : real-looking CUSIPs that pass the check-digit algorithm
  - 1 split           : one expected payment received as two equal halves, same day
  - 1 combined        : two payments from the same company on the same day
                        merged into one statement row (one CUSIP, combined amount)

===========================================================================
SEALED ANSWER KEY: data/processed/module6_answer_key_hard.csv
The reconciliation script MUST NEVER read or import this file.
===========================================================================

Output:
  data/processed/module6_bank_statement_hard.csv
  data/processed/module6_answer_key_hard.csv
"""
import random
from pathlib import Path
from datetime import date

import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

ROOT              = Path(__file__).parent.parent
EXPECTED_CSV      = ROOT / "data" / "processed" / "module6_expected_coupons.csv"
STATEMENT_HARD    = ROOT / "data" / "processed" / "module6_bank_statement_hard.csv"
ANSWER_KEY_HARD   = ROOT / "data" / "processed" / "module6_answer_key_hard.csv"

SEED = 7

# Two valid-looking CUSIPs that pass the standard check-digit algorithm
# and are NOT one character away from any CUSIP in our portfolio.
# Verified: 912828YR → check=5, 9128284W → check=7
VALID_SURPRISE_CUSIPS  = ["912828YR5", "9128284W7"]
VALID_SURPRISE_AMOUNTS = [175_000.0, 325_000.0]
VALID_SURPRISE_DATES   = [date(2026, 4, 15), date(2026, 10, 15)]

_USCAL    = USFederalHolidayCalendar()
_US_BD    = CustomBusinessDay(calendar=_USCAL)


def _add_bdays(d: date, n: int) -> date:
    return (pd.Timestamp(d) + n * _US_BD).date()


def _cusip_valid(cusip: str) -> bool:
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


def _hamming(a: str, b: str) -> int:
    if len(a) != len(b):
        return max(len(a), len(b))
    return sum(x != y for x, y in zip(a, b))


def main():
    rng = random.Random(SEED)

    df = pd.read_csv(EXPECTED_CSV)
    df["payment_date"]    = pd.to_datetime(df["payment_date"]).dt.date
    df["expected_amount"] = df["expected_amount"].astype(float)

    book_cusips = set(df["cusip"].astype(str))

    # ── Verify valid surprise CUSIPs ──────────────────────────────────────────
    for vc in VALID_SURPRISE_CUSIPS:
        assert _cusip_valid(vc), f"BUG: {vc} fails check digit!"
        assert vc not in book_cusips, f"BUG: {vc} is in our portfolio!"
        min_hd = min(_hamming(vc, bc) for bc in book_cusips)
        assert min_hd > 1, f"BUG: {vc} is only {min_hd} char from an expected CUSIP!"

    coupon_df = df[df["flow_type"] == "coupon"].copy()

    # ── 1. Find eligible combined pair (same company, same payment_date) ──────
    pair_groups = [
        (pdate, co, grp["payment_id"].tolist())
        for (pdate, co), grp in coupon_df.groupby(["payment_date", "company"])
        if len(grp) >= 2
    ]
    rng.shuffle(pair_groups)
    combined_date, combined_company, comb_pids = pair_groups[0]
    combined_pid1, combined_pid2 = comb_pids[0], comb_pids[1]

    # ── 2. Allocate remaining breaks from the other coupon rows ───────────────
    exclude = {combined_pid1, combined_pid2}
    avail   = [p for p in coupon_df["payment_id"].tolist() if p not in exclude]
    rng.shuffle(avail)

    late_and_short_pids  = set(avail[0:2])
    very_late_pids       = set(avail[2:4])
    large_deduction_pids = set(avail[4:6])
    split_pid            = avail[6]

    # ── 3. Build statement rows ───────────────────────────────────────────────
    stmt = []

    for _, row in df.iterrows():
        pid    = row["payment_id"]
        cusip  = str(row["cusip"])
        pdate  = row["payment_date"]
        amount = row["expected_amount"]
        desc   = f"{row['flow_type'].capitalize()} received — {row['company']}"

        if row["flow_type"] == "principal":
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=amount,
                             description=desc, _pid=pid, _break="correct"))
            continue

        if pid in late_and_short_pids:
            stmt.append(dict(value_date=_add_bdays(pdate, 2), cusip=cusip,
                             amount=round(amount - 25.0, 2),
                             description=desc, _pid=pid, _break="late_and_short"))

        elif pid in very_late_pids:
            stmt.append(dict(value_date=_add_bdays(pdate, 8), cusip=cusip,
                             amount=amount,
                             description=desc, _pid=pid, _break="very_late"))

        elif pid in large_deduction_pids:
            stmt.append(dict(value_date=pdate, cusip=cusip,
                             amount=round(amount * 0.85, 2),
                             description=desc, _pid=pid, _break="large_deduction"))

        elif pid == split_pid:
            half1 = round(amount / 2, 2)
            half2 = round(amount - half1, 2)
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=half1,
                             description=desc, _pid=pid, _break="split:1"))
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=half2,
                             description=desc, _pid=pid, _break="split:2"))

        elif pid == combined_pid1:
            # Combined row: pid1's CUSIP, sum of both amounts
            comb_amount = round(amount +
                                float(df.loc[df["payment_id"] == combined_pid2,
                                             "expected_amount"].iloc[0]), 2)
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=comb_amount,
                             description=desc, _pid=combined_pid1,
                             _break=f"combined_row:{combined_pid2}"))

        elif pid == combined_pid2:
            pass   # absorbed into the combined row; no statement entry

        else:
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=amount,
                             description=desc, _pid=pid, _break="correct"))

    # ── 4. Add valid-surprise rows ────────────────────────────────────────────
    for i, vc in enumerate(VALID_SURPRISE_CUSIPS):
        stmt.append(dict(
            value_date  = VALID_SURPRISE_DATES[i],
            cusip       = vc,
            amount      = VALID_SURPRISE_AMOUNTS[i],
            description = f"Coupon received — Innovatech Securitized",
            _pid        = f"VALSURPRISE_{i+1}",
            _break      = f"valid_surprise:{vc}",
        ))

    # ── 5. Sort and assign IDs ────────────────────────────────────────────────
    stmt_df = pd.DataFrame(stmt)
    stmt_df = stmt_df.sort_values(["value_date", "cusip"]).reset_index(drop=True)
    stmt_df.insert(0, "statement_row_id",
                   [f"S-{i+1:04d}" for i in range(len(stmt_df))])

    # ── 6. Build answer key ───────────────────────────────────────────────────
    ak_rows = []

    for _, srow in stmt_df.iterrows():
        brk = srow["_break"]
        pid = srow["_pid"]
        sid = srow["statement_row_id"]

        if brk == "correct":
            continue

        if brk == "late_and_short":
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="late_and_short",
                                detail="2 biz days late + $25 fee deducted"))

        elif brk == "very_late":
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="very_late",
                                detail="8 business days late"))

        elif brk == "large_deduction":
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="large_deduction",
                                detail="15% deducted from expected amount"))

        elif brk.startswith("valid_surprise:"):
            vc = brk.split(":")[1]
            ak_rows.append(dict(statement_row_id=sid, payment_id="",
                                break_type="valid_surprise",
                                detail=(f"CUSIP {vc} not in books; "
                                        f"check digit {'PASS' if _cusip_valid(vc) else 'FAIL'}")))

        elif brk.startswith("combined_row:"):
            other_pid = brk.split(":")[1]
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="combined",
                                detail=f"combined with {other_pid} into one row"))
            ak_rows.append(dict(statement_row_id=sid, payment_id=other_pid,
                                break_type="combined",
                                detail=f"combined with {pid} into one row"))

        # split:1 and split:2 are handled after the loop

    # Split: 1 answer key entry for the missing expected payment
    ak_rows.append(dict(statement_row_id="", payment_id=split_pid,
                        break_type="split",
                        detail="payment split into two equal halves, same day"))

    ak_df = pd.DataFrame(ak_rows)[
        ["statement_row_id", "payment_id", "break_type", "detail"]
    ]

    # ── 7. Save outputs ───────────────────────────────────────────────────────
    out_stmt = stmt_df[["statement_row_id", "value_date", "cusip",
                         "amount", "description"]]
    out_stmt.to_csv(STATEMENT_HARD, index=False)
    ak_df.to_csv(ANSWER_KEY_HARD, index=False)

    # ── 8. Summary ────────────────────────────────────────────────────────────
    break_counts = {}
    for _, r in ak_df.iterrows():
        break_counts[r["break_type"]] = break_counts.get(r["break_type"], 0) + 1

    print(f"Loaded {len(df)} rows from module6_expected_coupons.csv")
    print(f"\nHard statement written → {STATEMENT_HARD.name}")
    print(f"  Rows in statement  : {len(out_stmt)}")
    print(f"  (160 expected - 1 combined_absorbed + 1 extra split half + 2 surprises)")

    print(f"\nHard answer key written → {ANSWER_KEY_HARD.name}")
    print(f"  Total break entries : {len(ak_df)}")
    print(f"\n  Breaks by type:")
    for bt in ["late_and_short", "very_late", "large_deduction",
               "valid_surprise", "split", "combined"]:
        print(f"    {bt:<18} : {break_counts.get(bt, 0)}")

    print(f"\n  Combined pair : {combined_company}  "
          f"{combined_pid1} + {combined_pid2}  date {combined_date}")
    print(f"  Split payment : {split_pid}")

    print(f"\n  Valid surprise CUSIPs (both pass check digit ✓):")
    for vc in VALID_SURPRISE_CUSIPS:
        print(f"    {vc}")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
