#!/usr/bin/env python3
"""
Module 6, round 3 — Simulated bank statement with all break types from both rounds.

Seed: 99

Planted breaks (~15 entries):
  2 missing         : expected payments simply never arrive
  2 late            : 1 and 2 biz days late, exact amount
  1 short_fee       : $25 processing fee, exact date
  1 short_pct       : 0.5% deduction, exact date
  1 duplicate       : extra copy of a correctly-received payment
  1 surprise        : fake CUSIP not in books (ZZZZZZ003)
  1 cusip_typo      : one-character CUSIP typo, exact amount + date
  1 late_and_short  : 2 biz days late + $25 fee
  1 very_late       : 8 business days late
  1 large_deduction : 15% tax deducted
  1 valid_surprise  : valid check-digit CUSIP not in portfolio
  1 split           : one expected payment as two equal halves, same day
  1 combined pair   : two payments (same company, same date) merged into one row
                      (2 answer-key entries)

Total answer-key entries: 15

===========================================================================
SEALED ANSWER KEY: data/processed/module6_answer_key_round3.csv
The reconciliation script MUST NEVER read or import this file.
===========================================================================

Output:
  data/processed/module6_bank_statement_round3.csv
  data/processed/module6_answer_key_round3.csv
"""
import random
from pathlib import Path
from datetime import date

import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

ROOT            = Path(__file__).parent.parent
EXPECTED_CSV    = ROOT / "data" / "processed" / "module6_expected_coupons.csv"
STATEMENT_R3    = ROOT / "data" / "processed" / "module6_bank_statement_round3.csv"
ANSWER_KEY_R3   = ROOT / "data" / "processed" / "module6_answer_key_round3.csv"

SEED = 99

# Valid-looking CUSIP that passes check-digit; NOT in portfolio; Hamming dist > 1
# Verified: 38141GXZ → check=2
VALID_SURPRISE_CUSIP  = "38141GXZ2"
VALID_SURPRISE_AMOUNT = 250_000.0
VALID_SURPRISE_DATE   = date(2026, 7, 15)

FAKE_SURPRISE_CUSIP  = "ZZZZZZ003"
FAKE_SURPRISE_AMOUNT = 185_000.0
FAKE_SURPRISE_DATE   = date(2026, 6, 10)

_USCAL = USFederalHolidayCalendar()
_US_BD = CustomBusinessDay(calendar=_USCAL)


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


def _pick_cusip_typo(rng, book_cusips: list) -> tuple:
    """Return (original_cusip, typo_cusip) where typo is Hamming-1 from original."""
    rng_cusips = [c for c in book_cusips if len(c) == 9 and c.isalnum()]
    rng.shuffle(rng_cusips)
    for original in rng_cusips:
        for pos in range(9):
            for sub in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                if sub != original[pos]:
                    typo = original[:pos] + sub + original[pos+1:]
                    if typo not in set(book_cusips) and not _cusip_valid(typo):
                        return original, typo
    raise RuntimeError("Could not find a valid CUSIP typo")


def main():
    rng = random.Random(SEED)

    df = pd.read_csv(EXPECTED_CSV)
    df["payment_date"]    = pd.to_datetime(df["payment_date"]).dt.date
    df["expected_amount"] = df["expected_amount"].astype(float)

    book_cusips = df["cusip"].astype(str).tolist()
    book_set    = set(book_cusips)

    # ── Verify valid surprise CUSIP ───────────────────────────────────────
    assert _cusip_valid(VALID_SURPRISE_CUSIP), \
        f"BUG: {VALID_SURPRISE_CUSIP} fails check digit!"
    assert VALID_SURPRISE_CUSIP not in book_set, \
        f"BUG: {VALID_SURPRISE_CUSIP} is in portfolio!"
    min_hd = min(_hamming(VALID_SURPRISE_CUSIP, bc) for bc in book_set)
    assert min_hd > 1, \
        f"BUG: {VALID_SURPRISE_CUSIP} is Hamming-{min_hd} from a portfolio CUSIP!"

    coupon_df = df[df["flow_type"] == "coupon"].copy()

    # ── 1. Find eligible combined pair ────────────────────────────────────
    pair_groups = [
        (pdate, co, grp["payment_id"].tolist())
        for (pdate, co), grp in coupon_df.groupby(["payment_date", "company"])
        if len(grp) >= 2
    ]
    rng.shuffle(pair_groups)
    combined_date, combined_co, comb_pids = pair_groups[0]
    combined_pid1, combined_pid2 = comb_pids[0], comb_pids[1]

    # ── 2. Find CUSIP typo source ─────────────────────────────────────────
    typo_original, typo_cusip = _pick_cusip_typo(rng, book_cusips)
    typo_source_pid = None
    for _, row in coupon_df.iterrows():
        if str(row["cusip"]) == typo_original and row["payment_id"] not in {combined_pid1, combined_pid2}:
            typo_source_pid = row["payment_id"]
            break

    # ── 3. Allocate remaining breaks ──────────────────────────────────────
    exclude = {combined_pid1, combined_pid2, typo_source_pid}
    avail   = [p for p in coupon_df["payment_id"].tolist() if p not in exclude]
    rng.shuffle(avail)

    missing_pids       = set(avail[0:2])
    late_pids          = list(avail[2:4])      # [1-day, 2-day]
    short_fee_pid      = avail[4]
    short_pct_pid      = avail[5]
    duplicate_pid      = avail[6]
    late_and_short_pid = avail[7]
    very_late_pid      = avail[8]
    large_ded_pid      = avail[9]
    split_pid          = avail[10]

    # ── 4. Build statement rows ───────────────────────────────────────────
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

        if pid in missing_pids:
            pass  # no statement row

        elif pid == late_pids[0]:
            stmt.append(dict(value_date=_add_bdays(pdate, 1), cusip=cusip,
                             amount=amount, description=desc,
                             _pid=pid, _break="late:1"))

        elif pid == late_pids[1]:
            stmt.append(dict(value_date=_add_bdays(pdate, 2), cusip=cusip,
                             amount=amount, description=desc,
                             _pid=pid, _break="late:2"))

        elif pid == short_fee_pid:
            stmt.append(dict(value_date=pdate, cusip=cusip,
                             amount=round(amount - 25.0, 2),
                             description=desc, _pid=pid, _break="short_fee"))

        elif pid == short_pct_pid:
            stmt.append(dict(value_date=pdate, cusip=cusip,
                             amount=round(amount * 0.995, 2),
                             description=desc, _pid=pid, _break="short_pct"))

        elif pid == duplicate_pid:
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=amount,
                             description=desc, _pid=pid, _break="correct"))  # original
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=amount,
                             description=desc, _pid=pid, _break="duplicate"))  # extra copy

        elif pid == typo_source_pid:
            stmt.append(dict(value_date=pdate, cusip=typo_cusip, amount=amount,
                             description=desc, _pid=pid, _break="cusip_typo"))

        elif pid == late_and_short_pid:
            stmt.append(dict(value_date=_add_bdays(pdate, 2), cusip=cusip,
                             amount=round(amount - 25.0, 2),
                             description=desc, _pid=pid, _break="late_and_short"))

        elif pid == very_late_pid:
            stmt.append(dict(value_date=_add_bdays(pdate, 8), cusip=cusip,
                             amount=amount, description=desc,
                             _pid=pid, _break="very_late"))

        elif pid == large_ded_pid:
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
            comb_amount = round(
                amount + float(df.loc[df["payment_id"] == combined_pid2,
                                      "expected_amount"].iloc[0]), 2)
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=comb_amount,
                             description=desc, _pid=combined_pid1,
                             _break=f"combined_row:{combined_pid2}"))

        elif pid == combined_pid2:
            pass  # absorbed into combined row

        else:
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=amount,
                             description=desc, _pid=pid, _break="correct"))

    # ── 5. Add fake-surprise row ─────────────────────────────────────────
    stmt.append(dict(
        value_date=FAKE_SURPRISE_DATE, cusip=FAKE_SURPRISE_CUSIP,
        amount=FAKE_SURPRISE_AMOUNT,
        description="Coupon received — Unknown Issuer",
        _pid="FAKESURP_1", _break="surprise",
    ))

    # ── 6. Add valid-surprise row ─────────────────────────────────────────
    stmt.append(dict(
        value_date=VALID_SURPRISE_DATE, cusip=VALID_SURPRISE_CUSIP,
        amount=VALID_SURPRISE_AMOUNT,
        description="Coupon received — Innovatech Securitized",
        _pid="VALSURP_1", _break=f"valid_surprise:{VALID_SURPRISE_CUSIP}",
    ))

    # ── 7. Sort and assign IDs ─────────────────────────────────────────────
    stmt_df = pd.DataFrame(stmt)
    stmt_df = stmt_df.sort_values(["value_date", "cusip"]).reset_index(drop=True)
    stmt_df.insert(0, "statement_row_id",
                   [f"S-{i+1:04d}" for i in range(len(stmt_df))])

    # ── 8. Build answer key ───────────────────────────────────────────────
    ak_rows = []

    for pid in missing_pids:
        ak_rows.append(dict(statement_row_id="", payment_id=pid,
                            break_type="missing",
                            detail="payment not received"))

    for _, srow in stmt_df.iterrows():
        brk = srow["_break"]
        pid = srow["_pid"]
        sid = srow["statement_row_id"]

        if brk in ("correct",):
            continue

        if brk.startswith("late:"):
            days = int(brk.split(":")[1])
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="late",
                                detail=f"{days} business day(s) late"))

        elif brk == "short_fee":
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="short",
                                detail="$25.00 processing fee deducted"))

        elif brk == "short_pct":
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="short",
                                detail="0.5% deduction applied"))

        elif brk == "duplicate":
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="duplicate",
                                detail=f"extra copy of {pid}"))

        elif brk == "cusip_typo":
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="cusip_typo",
                                detail=(f"CUSIP shown as {typo_cusip}; "
                                        f"correct is {typo_original}")))

        elif brk == "late_and_short":
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

        elif brk == "surprise":
            ak_rows.append(dict(statement_row_id=sid, payment_id="",
                                break_type="surprise",
                                detail=f"CUSIP {FAKE_SURPRISE_CUSIP} not in books"))

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

    # Split: single entry for the missing expected payment
    ak_rows.append(dict(statement_row_id="", payment_id=split_pid,
                        break_type="split",
                        detail="payment split into two equal halves, same day"))

    ak_df = pd.DataFrame(ak_rows)[
        ["statement_row_id", "payment_id", "break_type", "detail"]
    ]

    # ── 9. Save ───────────────────────────────────────────────────────────
    out_stmt = stmt_df[["statement_row_id", "value_date", "cusip",
                         "amount", "description"]]
    out_stmt.to_csv(STATEMENT_R3, index=False)
    ak_df.to_csv(ANSWER_KEY_R3, index=False)

    # ── 10. Summary ────────────────────────────────────────────────────────
    break_counts = {}
    for _, r in ak_df.iterrows():
        break_counts[r["break_type"]] = break_counts.get(r["break_type"], 0) + 1

    print(f"Loaded {len(df)} rows from module6_expected_coupons.csv")
    print(f"\nRound-3 statement written → {STATEMENT_R3.name}")
    print(f"  Rows in statement: {len(out_stmt)}")

    print(f"\nRound-3 answer key written → {ANSWER_KEY_R3.name}")
    print(f"  Total break entries: {len(ak_df)}")
    print(f"\n  Breaks by type:")
    for bt in ["missing", "late", "short", "duplicate", "surprise", "cusip_typo",
               "late_and_short", "very_late", "large_deduction",
               "valid_surprise", "split", "combined"]:
        c = break_counts.get(bt, 0)
        if c:
            print(f"    {bt:<18} : {c}")

    print(f"\n  Combined pair : {combined_co}  "
          f"{combined_pid1} + {combined_pid2}  date {combined_date}")
    print(f"  Split payment : {split_pid}")
    print(f"  CUSIP typo    : {typo_original} → {typo_cusip}")
    print(f"  Valid surprise: {VALID_SURPRISE_CUSIP}  (check digit PASS ✓)")
    print(f"\nDone.")


if __name__ == "__main__":
    main()
