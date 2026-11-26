#!/usr/bin/env python3
"""
Module 6, Step 4 — Score the reconciliation output against the sealed answer key.

This is the ONLY script in the project that reads module6_answer_key.csv.
It compares what the reconciliation script found against what was actually planted,
and reports: how many planted breaks were found, how many had the right type,
how many false alarms were raised, and what was missed or mislabelled.

Works for both the standard round (module6_answer_key.csv) and the harder round
(module6_answer_key_hard.csv) since the harder answer key uses the same columns
but adds new break_type values.

Inputs:
  data/processed/module6_recon_results.csv   — output of reconcile_cash.py
  data/processed/module6_answer_key.csv      — sealed answer key (planted breaks)
"""
from pathlib import Path

import pandas as pd

ROOT           = Path(__file__).parent.parent
RECON_CSV      = ROOT / "data" / "processed" / "module6_recon_results.csv"
ANSWER_KEY_CSV = ROOT / "data" / "processed" / "module6_answer_key.csv"
EXPECTED_CSV   = ROOT / "data" / "processed" / "module6_expected_coupons.csv"

# Maps answer-key break_type → what reconcile_cash.py *should* produce (ideal type).
# Hard-round types are added below the original six.
AK_TO_RECON = {
    # Standard round (original six)
    "missing":        "missing",
    "late":           "late",
    "short":          "short_paid",
    "duplicate":      "duplicate",
    "surprise":       "unexpected",
    "cusip_typo":     "cusip_typo",
    # Hard round
    "late_and_short": "late_and_short",  # combo — reconciler has no such type
    "very_late":      "late",            # > 5 biz days — reconciler misses
    "large_deduction":"short_paid",      # > 1% short — reconciler misses
    "valid_surprise": "unexpected",      # real-looking CUSIP — still unexpected ✓
    "split":          "split",           # novel type — reconciler has none
    "combined":       "combined",        # novel type — reconciler has none
}

# Break types matched by payment_id (expected-side)
_MATCH_BY_PID = frozenset({
    "missing", "late", "short", "cusip_typo",
    "late_and_short", "very_late", "large_deduction", "split", "combined",
})
# Break types matched by statement_row_id (statement-side)
_MATCH_BY_SID = frozenset({
    "duplicate", "surprise", "valid_surprise",
})


def main():
    ak    = pd.read_csv(ANSWER_KEY_CSV)
    recon = pd.read_csv(RECON_CSV)
    exp   = pd.read_csv(EXPECTED_CSV)

    book_cusips = set(exp["cusip"].astype(str))

    # Only non-matched recon rows are relevant
    breaks = recon[recon["result"] != "matched"].copy()

    total_planted = len(ak)
    found_count   = 0
    correct_type  = 0
    missed        = []
    mislabelled   = []
    matched_break_indices = set()

    for _, ak_row in ak.iterrows():
        pid   = str(ak_row["payment_id"])      if pd.notna(ak_row["payment_id"])      else ""
        sid   = str(ak_row["statement_row_id"]) if pd.notna(ak_row["statement_row_id"]) else ""
        btype = ak_row["break_type"]
        expected_recon = AK_TO_RECON.get(btype, "")

        # Match by payment_id (expected-side breaks) or statement_row_id (statement-side)
        found_row = None
        if btype in _MATCH_BY_PID and pid:
            cands = breaks[breaks["expected_payment_id"].astype(str) == pid]
            if not cands.empty:
                found_row = cands.iloc[0]
        elif btype in _MATCH_BY_SID and sid:
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
    seen_types = ak["break_type"].unique()
    for btype in seen_types:
        ak_type = ak[ak["break_type"] == btype]
        total = len(ak_type)
        type_found = 0
        for _, r in ak_type.iterrows():
            r_pid = str(r["payment_id"])      if pd.notna(r["payment_id"])      else ""
            r_sid = str(r["statement_row_id"]) if pd.notna(r["statement_row_id"]) else ""
            if btype in _MATCH_BY_PID and r_pid:
                cands = breaks[breaks["expected_payment_id"].astype(str) == r_pid]
            elif btype in _MATCH_BY_SID and r_sid:
                cands = breaks[breaks["statement_row_id"].astype(str) == r_sid]
            else:
                cands = pd.DataFrame()
            if not cands.empty and cands.index[0] in matched_break_indices:
                type_found += 1
        ideal = AK_TO_RECON.get(btype, "?")
        mark  = "✓" if type_found == total else f"✗ ({total - type_found} missed)"
        print(f"  {btype:<18} planted {total}  found {type_found}  {mark}"
              f"  [ideal recon type: {ideal}]")

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
    _MISLABEL_REASON = {
        ("late_and_short", "short_paid"):
            "The payment was both late (2 biz days) and short ($25 fee). "
            "Pass 2 (late) requires an exact amount match — it failed because the "
            "amount differed by $25. Pass 3 (short_paid) caught it on date+amount "
            "tolerance, but the late aspect was lost.",
        ("very_late", "missing"):
            "The payment arrived 8 business days late — outside the 1–5 day "
            "window in pass 2. No pass matched the statement row to the expected "
            "payment, so the expected row fell through to 'missing' and the "
            "statement row fell through to 'unexpected'.",
        ("large_deduction", "missing"):
            "The payment was deducted by 15%, far beyond the 1% tolerance in "
            "pass 3 (short_paid). No pass could match it, so the expected row "
            "shows as 'missing' and the statement row as 'unexpected'.",
        ("split", "missing"):
            "The payment arrived as two half-size rows on the same day. Neither "
            "half matches the expected full amount in any pass, so the expected "
            "row shows as 'missing' and both halves show as 'unexpected'.",
        ("combined", "missing"):
            "Two payments were merged into one statement row under one CUSIP. "
            "The combined amount exceeds both individual expectations, so pass 3 "
            "sees no shortfall and all passes fail — both expected rows become "
            "'missing' and the combined row becomes 'unexpected'.",
    }

    if mislabelled:
        print(f"\n{'='*62}")
        print(f"MISLABELLED BREAKS  ({len(mislabelled)})")
        print(f"{'='*62}")
        for m in mislabelled:
            reason = _MISLABEL_REASON.get(
                (m["planted_type"], m["found_type"]),
                f"Reconciler found it as '{m['found_type']}' "
                f"instead of '{m['planted_type']}'.",
            )
            print(f"  payment_id   : {m['payment_id']}   stmt_row: {m['stmt_row_id']}")
            print(f"  planted type : {m['planted_type']}   found as: {m['found_type']}")
            print(f"  detail       : {m['detail']}")
            print(f"  plain-English reason: {reason}")
            print()
    else:
        print(f"\n  No mislabelled breaks — every found break has the correct type.")

    # ── False alarms ──────────────────────────────────────────────────────────
    if false_alarms:
        print(f"\n{'='*62}")
        print(f"FALSE ALARMS  ({fa_count})")
        print(f"{'='*62}")
        fa_rows = breaks.loc[list(false_alarms)].sort_values("statement_row_id")
        for _, row in fa_rows.iterrows():
            ep   = row["expected_payment_id"] if pd.notna(row["expected_payment_id"]) else "—"
            sid  = row["statement_row_id"]     if pd.notna(row["statement_row_id"])     else "—"
            ae   = f"${float(row['amount_expected']):,.2f}" if pd.notna(row["amount_expected"]) else "—"
            ar   = f"${float(row['amount_received']):,.2f}" if pd.notna(row["amount_received"]) else "—"
            cu   = str(row["cusip"]) if pd.notna(row["cusip"]) else "—"
            in_books = cu in book_cusips
            if in_books:
                fa_reason = (
                    f"CUSIP {cu} IS in our portfolio. This statement row belongs "
                    "to a planted break (very_late, large_deduction, split, or "
                    "combined) that the reconciler could not pair with its expected "
                    "payment — so it wrongly flagged it as an unknown transaction."
                )
            else:
                fa_reason = (
                    f"CUSIP {cu} is NOT in our portfolio. "
                    "This is a genuinely unknown transaction."
                )
            print(f"  result={row['result']}  CUSIP={cu}  "
                  f"{'(in books)' if in_books else '(not in books)'}")
            print(f"  exp_payment={ep}  stmt_row={sid}")
            print(f"  expected={ae}   received={ar}")
            print(f"  plain-English: {fa_reason}")
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
