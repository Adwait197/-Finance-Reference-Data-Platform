#!/usr/bin/env python3
"""
Module 6, Step 4 — Score the reconciliation output against the sealed answer key.

This is the ONLY script in the project that reads module6_answer_key.csv.
It compares what the reconciliation script found against what was actually planted,
and reports: how many planted breaks were found, how many had the right type,
how many false alarms were raised, and what was missed or mislabelled.

Inputs:
  data/processed/module6_recon_results.csv   — output of reconcile_cash.py
  data/processed/module6_answer_key.csv      — sealed answer key (planted breaks)
"""
from pathlib import Path

import pandas as pd

ROOT           = Path(__file__).parent.parent
RECON_CSV      = ROOT / "data" / "processed" / "module6_recon_results.csv"
ANSWER_KEY_CSV = ROOT / "data" / "processed" / "module6_answer_key.csv"

# Maps answer-key break_type → reconciliation result name
AK_TO_RECON = {
    "missing":    "missing",
    "late":       "late",
    "short":      "short_paid",
    "duplicate":  "duplicate",
    "surprise":   "unexpected",
    "cusip_typo": "cusip_typo",
}


def main():
    ak    = pd.read_csv(ANSWER_KEY_CSV)
    recon = pd.read_csv(RECON_CSV)

    # Only non-matched recon rows are relevant
    breaks = recon[recon["result"] != "matched"].copy()

    total_planted = len(ak)
    found_count   = 0
    correct_type  = 0
    missed        = []
    mislabelled   = []
    matched_break_indices = set()

    for _, ak_row in ak.iterrows():
        pid   = str(ak_row["payment_id"])          if pd.notna(ak_row["payment_id"])          else ""
        sid   = str(ak_row["statement_row_id"])     if pd.notna(ak_row["statement_row_id"])     else ""
        btype = ak_row["break_type"]
        expected_recon = AK_TO_RECON.get(btype, "")

        # ── Match by identifier: expected-side breaks → payment_id,
        #    statement-side breaks (duplicate, surprise) → statement_row_id
        found_row = None
        if btype in ("missing", "late", "short", "cusip_typo") and pid:
            cands = breaks[breaks["expected_payment_id"].astype(str) == pid]
            if not cands.empty:
                found_row = cands.iloc[0]
        elif btype in ("duplicate", "surprise") and sid:
            cands = breaks[breaks["statement_row_id"].astype(str) == sid]
            if not cands.empty:
                found_row = cands.iloc[0]

        if found_row is not None:
            matched_break_indices.add(found_row.name)
            found_count += 1
            if found_row["result"] == expected_recon:
                correct_type += 1
            else:
                mislabelled.append({
                    "payment_id":    pid or "—",
                    "stmt_row_id":   sid or "—",
                    "planted_type":  btype,
                    "found_type":    found_row["result"],
                    "detail":        ak_row["detail"],
                })
        else:
            missed.append({
                "payment_id":   pid or "—",
                "stmt_row_id":  sid or "—",
                "planted_type": btype,
                "detail":       ak_row["detail"],
            })

    false_alarms = set(breaks.index) - matched_break_indices
    fa_count     = len(false_alarms)

    # ── Print score ───────────────────────────────────────────────────────────
    print(f"{'='*62}")
    print(f"RECONCILIATION SCORE  (Module 6, Step 4)")
    print(f"{'='*62}")
    print(f"  Total planted breaks        : {total_planted}")
    print(f"  Breaks found                : {found_count} / {total_planted}")
    print(f"  Found with correct type     : {correct_type} / {found_count}")
    print(f"  False alarms (extra flags)  : {fa_count}")

    # ── Planted breaks found by type ──────────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"PLANTED BREAKS — FOUND vs. MISSED by type")
    print(f"{'='*62}")
    for btype, recon_type in AK_TO_RECON.items():
        total = (ak["break_type"] == btype).sum()
        found = sum(
            1 for m in mislabelled + []  # start empty; below we count correctly
            if m["planted_type"] == btype
        )
        # Correctly: count answer-key rows of this type that ARE in matched_break_indices
        ak_type = ak[ak["break_type"] == btype]
        type_found = 0
        for _, r in ak_type.iterrows():
            pid = str(r["payment_id"])       if pd.notna(r["payment_id"])          else ""
            sid = str(r["statement_row_id"]) if pd.notna(r["statement_row_id"])     else ""
            if btype in ("missing", "late", "short", "cusip_typo") and pid:
                cands = breaks[breaks["expected_payment_id"].astype(str) == pid]
            else:
                cands = breaks[breaks["statement_row_id"].astype(str) == sid]
            if not cands.empty and cands.index[0] in matched_break_indices:
                type_found += 1
        print(f"  {btype:<12} planted {total}  found {type_found}  "
              f"{'✓' if type_found == total else f'✗ ({total - type_found} missed)'}")

    # ── Missed ────────────────────────────────────────────────────────────────
    if missed:
        print(f"\n{'='*62}")
        print(f"MISSED BREAKS  ({len(missed)})")
        print(f"{'='*62}")
        for m in missed:
            print(f"  planted_type : {m['planted_type']}")
            print(f"  payment_id   : {m['payment_id']}   stmt_row: {m['stmt_row_id']}")
            print(f"  detail       : {m['detail']}")
            print(f"  reason       : this break was not found in the reconciliation output")
            print()
    else:
        print(f"\n  No missed breaks — all {total_planted} planted breaks were found.")

    # ── Mislabelled ───────────────────────────────────────────────────────────
    if mislabelled:
        print(f"\n{'='*62}")
        print(f"MISLABELLED BREAKS  ({len(mislabelled)})")
        print(f"{'='*62}")
        for m in mislabelled:
            print(f"  payment_id   : {m['payment_id']}   stmt_row: {m['stmt_row_id']}")
            print(f"  planted type : {m['planted_type']}   found as: {m['found_type']}")
            print(f"  detail       : {m['detail']}")
            print(f"  reason       : payment was detected but classified incorrectly")
            print()
    else:
        print(f"\n  No mislabelled breaks — every found break has the correct type.")

    # ── False alarms ──────────────────────────────────────────────────────────
    if false_alarms:
        print(f"\n{'='*62}")
        print(f"FALSE ALARMS  ({fa_count})")
        print(f"{'='*62}")
        fa_rows = breaks.loc[list(false_alarms)]
        for _, row in fa_rows.iterrows():
            ep  = row["expected_payment_id"] if pd.notna(row["expected_payment_id"]) else "—"
            sid = row["statement_row_id"]     if pd.notna(row["statement_row_id"])     else "—"
            ae  = f"${float(row['amount_expected']):,.2f}" if pd.notna(row["amount_expected"]) else "—"
            ar  = f"${float(row['amount_received']):,.2f}" if pd.notna(row["amount_received"]) else "—"
            print(f"  result={row['result']}  CUSIP={row['cusip']}  "
                  f"exp_payment={ep}  stmt_row={sid}")
            print(f"  expected={ae}   received={ar}")
            print(f"  reason: flagged as a break but no matching planted break in answer key")
            print()
    else:
        print(f"\n  No false alarms — every flagged break was a real planted break.")

    # ── Final verdict ─────────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"VERDICT")
    print(f"{'='*62}")
    if found_count == total_planted and correct_type == found_count and fa_count == 0:
        print(f"  PERFECT SCORE: {found_count}/{total_planted} planted breaks found,")
        print(f"  all with correct type, zero false alarms.")
    else:
        pct = round(100 * found_count / total_planted)
        print(f"  {found_count}/{total_planted} breaks found ({pct}%),  "
              f"{correct_type}/{found_count} correct type,  {fa_count} false alarm(s).")
    print(f"\nDone.")


if __name__ == "__main__":
    main()
