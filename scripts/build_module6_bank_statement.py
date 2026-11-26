#!/usr/bin/env python3
"""
Module 6, Step 2 — Simulated bank statement with planted breaks.

Starts from module6_expected_coupons.csv ("our books") and produces a
statement that a bank might send, containing deliberate discrepancies for
the reconciliation step (Module 6 Step 3) to find.

===========================================================================
SEALED ANSWER KEY: data/processed/module6_answer_key.csv
The reconciliation script MUST NEVER read or import that file.
Treat it as a sealed envelope: open only after reconciliation is complete.
===========================================================================

Planted breaks (randomised with seed 42):
  - 4 missing payments     : expected coupons that never arrived
  - 5 late payments        : arrived 1–3 business days after the booked date
  - 4 short payments       : arrived with a $25 fee deducted or 0.5% less
  - 2 duplicate payments   : appeared twice on the statement
  - 2 surprise payments    : CUSIP not in our books (made-up)
  - 2 CUSIP typos          : one character of the CUSIP mistyped

Output:
  data/processed/module6_bank_statement.csv  — the statement
  data/processed/module6_answer_key.csv      — sealed answer key
"""
import random
import string
from pathlib import Path
from datetime import date

import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

ROOT         = Path(__file__).parent.parent
EXPECTED_CSV = ROOT / "data" / "processed" / "module6_expected_coupons.csv"
STATEMENT_CSV = ROOT / "data" / "processed" / "module6_bank_statement.csv"
ANSWER_KEY_CSV = ROOT / "data" / "processed" / "module6_answer_key.csv"

SEED = 42

_USCAL = USFederalHolidayCalendar()
_US_BD = CustomBusinessDay(calendar=_USCAL)


def _add_bdays(d: date, n: int) -> date:
    return (pd.Timestamp(d) + n * _US_BD).date()


def _mutate_cusip(cusip: str, rng: random.Random) -> str:
    """Change one character of a CUSIP to a visually similar wrong value."""
    idx = rng.randrange(len(cusip))
    ch  = cusip[idx]
    # Rotate: digit → next digit (mod 10), letter → next letter (mod 26)
    if ch.isdigit():
        new_ch = str((int(ch) + 1) % 10)
    elif ch.isupper():
        new_ch = chr((ord(ch) - ord('A') + 1) % 26 + ord('A'))
    else:
        new_ch = str((int(ch) + 1) % 10) if ch.isdigit() else ch
    return cusip[:idx] + new_ch + cusip[idx + 1:]


def main():
    rng = random.Random(SEED)

    df = pd.read_csv(EXPECTED_CSV)
    df["payment_date"] = pd.to_datetime(df["payment_date"]).dt.date
    df["expected_amount"] = df["expected_amount"].astype(float)

    # Separate coupon and principal rows
    coupon_df    = df[df["flow_type"] == "coupon"].copy()
    principal_df = df[df["flow_type"] == "principal"].copy()

    # ── Allocate break slots ───────────────────────────────────────────────────
    all_pids = coupon_df["payment_id"].tolist()
    rng.shuffle(all_pids)

    # 4 + 5 + 4 + 2 + 2 = 17 coupon rows affected (4 missing, 13 appear modified)
    missing_pids  = set(all_pids[0:4])
    late_pids     = {pid: rng.randint(1, 3) for pid in all_pids[4:9]}
    short_pids    = {}
    for pid in all_pids[9:13]:
        short_pids[pid] = ("fee", 25.0) if rng.random() < 0.5 else ("pct", 0.005)
    duplicate_pids = set(all_pids[13:15])
    typo_pids      = set(all_pids[15:17])

    # ── Build statement rows (each carries hidden _pid and _break for answer key)
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
            # Missing: not added to statement; recorded in answer key only
            continue

        if pid in late_pids:
            days = late_pids[pid]
            stmt.append(dict(value_date=_add_bdays(pdate, days), cusip=cusip,
                             amount=amount, description=desc,
                             _pid=pid, _break=f"late:{days}"))
            continue

        if pid in short_pids:
            typ, val = short_pids[pid]
            new_amt = round(amount - val, 2) if typ == "fee" else round(amount * (1 - val), 2)
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=new_amt,
                             description=desc, _pid=pid,
                             _break=f"short:{'fee' if typ=='fee' else 'pct'}:{val}"))
            continue

        if pid in duplicate_pids:
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=amount,
                             description=desc, _pid=pid, _break="correct"))
            stmt.append(dict(value_date=pdate, cusip=cusip, amount=amount,
                             description=desc, _pid=pid, _break="duplicate"))
            continue

        if pid in typo_pids:
            bad_cusip = _mutate_cusip(cusip, rng)
            stmt.append(dict(value_date=pdate, cusip=bad_cusip, amount=amount,
                             description=desc, _pid=pid,
                             _break=f"cusip_typo:{cusip}:{bad_cusip}"))
            continue

        # Correct row
        stmt.append(dict(value_date=pdate, cusip=cusip, amount=amount,
                         description=desc, _pid=pid, _break="correct"))

    # ── Add 2 surprise rows ────────────────────────────────────────────────────
    surprise_names   = ["Globex Corporation", "Initech Industries"]
    surprise_dates   = [date(2026, 3, 15), date(2026, 9, 14)]
    surprise_amounts = [round(rng.randint(5, 50) * 10_000, 2),
                        round(rng.randint(5, 50) * 10_000, 2)]
    surprise_cusips  = ["ZZZZZZ001", "ZZZZZZ002"]

    for i in range(2):
        stmt.append(dict(
            value_date  = surprise_dates[i],
            cusip       = surprise_cusips[i],
            amount      = surprise_amounts[i],
            description = f"Coupon received — {surprise_names[i]}",
            _pid        = f"SURPRISE_{i+1}",
            _break      = f"surprise:{surprise_cusips[i]}",
        ))

    # ── Sort and assign statement_row_ids ─────────────────────────────────────
    stmt_df = pd.DataFrame(stmt)
    stmt_df = stmt_df.sort_values(["value_date", "cusip"]).reset_index(drop=True)
    stmt_df.insert(0, "statement_row_id",
                   [f"S-{i+1:04d}" for i in range(len(stmt_df))])

    # ── Build answer key ──────────────────────────────────────────────────────
    ak_rows = []

    # Missing payments (no statement row)
    for pid in sorted(missing_pids):
        ak_rows.append(dict(statement_row_id="", payment_id=pid,
                            break_type="missing",
                            detail="payment not received"))

    # Non-correct statement rows
    for _, srow in stmt_df.iterrows():
        brk = srow["_break"]
        pid = srow["_pid"]
        sid = srow["statement_row_id"]

        if brk == "correct":
            continue

        if brk.startswith("late:"):
            days = int(brk.split(":")[1])
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="late",
                                detail=f"{days} business day(s) late"))

        elif brk.startswith("short:fee"):
            fee = float(brk.split(":")[2])
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="short",
                                detail=f"${fee:.2f} processing fee deducted"))

        elif brk.startswith("short:pct"):
            pct = float(brk.split(":")[2])
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="short",
                                detail=f"{pct*100:.1f}% deduction applied"))

        elif brk == "duplicate":
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="duplicate",
                                detail=f"extra copy of {pid}"))

        elif brk.startswith("cusip_typo:"):
            parts = brk.split(":")
            orig, wrong = parts[1], parts[2]
            ak_rows.append(dict(statement_row_id=sid, payment_id=pid,
                                break_type="cusip_typo",
                                detail=f"CUSIP shown as {wrong}; correct is {orig}"))

        elif brk.startswith("surprise:"):
            fake_cusip = brk.split(":")[1]
            ak_rows.append(dict(statement_row_id=sid, payment_id="",
                                break_type="surprise",
                                detail=f"CUSIP {fake_cusip} not in books"))

    ak_df = pd.DataFrame(ak_rows)[
        ["statement_row_id", "payment_id", "break_type", "detail"]
    ]

    # ── Save outputs (strip internal columns from statement) ──────────────────
    out_stmt = stmt_df[["statement_row_id", "value_date", "cusip",
                         "amount", "description"]]
    out_stmt.to_csv(STATEMENT_CSV, index=False)
    ak_df.to_csv(ANSWER_KEY_CSV, index=False)

    # ── Summary (counts only — no row-level detail) ───────────────────────────
    break_counts = {}
    for _, r in ak_df.iterrows():
        break_counts[r["break_type"]] = break_counts.get(r["break_type"], 0) + 1

    print(f"Loaded {len(df)} rows from module6_expected_coupons.csv")
    print(f"\nStatement written → {STATEMENT_CSV.name}")
    print(f"  Rows in statement : {len(out_stmt)}")
    print(f"  (159 coupons - 4 missing + 2 duplicates + 2 surprises + 1 principal)")

    print(f"\nAnswer key written → {ANSWER_KEY_CSV.name}")
    print(f"  Total breaks recorded : {len(ak_df)}")
    print(f"\n  Breaks by type:")
    for bt in ["missing", "late", "short", "duplicate", "surprise", "cusip_typo"]:
        print(f"    {bt:<12} : {break_counts.get(bt, 0)}")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
