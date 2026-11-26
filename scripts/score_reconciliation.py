#!/usr/bin/env python3
"""
Module 6, Step 4 — Score the reconciliation output against the sealed answer key.

This is the ONLY script in the project that reads answer key files.
It compares what the reconciliation script found against what was actually planted,
and reports: how many planted breaks were found, how many had the right type,
how many false alarms were raised, and what was missed or mislabelled.

Works for the standard round (module6_answer_key.csv), the harder round
(module6_answer_key_hard.csv), and round 3 (module6_answer_key_round3.csv).

Usage:
  python score_reconciliation.py                        # uses default paths
  python score_reconciliation.py --recon <path> --ak <path>  # explicit paths

Inputs:
  data/processed/module6_recon_results.csv   — output of reconcile_cash.py
  data/processed/module6_answer_key.csv      — sealed answer key (planted breaks)
  data/processed/module6_expected_coupons.csv — used to identify portfolio CUSIPs
"""
import argparse
from pathlib import Path

import pandas as pd

ROOT           = Path(__file__).parent.parent
RECON_CSV      = ROOT / "data" / "processed" / "module6_recon_results.csv"
ANSWER_KEY_CSV = ROOT / "data" / "processed" / "module6_answer_key.csv"
EXPECTED_CSV   = ROOT / "data" / "processed" / "module6_expected_coupons.csv"

# Maps answer-key break_type → what reconcile_cash.py v2 produces (ideal type).
AK_TO_RECON = {
    # Standard round (original six types)
    "missing":        "missing",
    "late":           "late",
    "short":          "short_paid",
    "duplicate":      "duplicate",
    "surprise":       "unexpected",
    "cusip_typo":     "cusip_typo",
    # Hard round / round 3 extended types
    "late_and_short": "late_and_short",
    "very_late":      "late_needs_review",
    "large_deduction":"large_deduction_needs_review",
    "valid_surprise": "unexpected",
    "split":          "split",
    "combined":       "combined",
}

# Break types matched by payment_id (expected-side rows)
_MATCH_BY_PID = frozenset({
    "missing", "late", "short", "cusip_typo",
    "late_and_short", "very_late", "large_deduction", "split", "combined",
})
# Break types matched by statement_row_id (statement-side rows)
_MATCH_BY_SID = frozenset({
    "duplicate", "surprise", "valid_surprise",
})

# Plain-English reasons for known mislabellings
_MISLABEL_REASON = {
    ("late_and_short", "short_paid"):
        "The payment was both late and short, but the reconciler's short_paid pass "
        "requires an exact date — it only fires for on-time short payments. "
        "The late+short combo should now be caught by same_bond_pairing as 'late_and_short'.",
    ("very_late", "missing"):
        "The payment arrived more than 5 business days late. The old late pass required "
        "1–5 days; the new same_bond_pairing pass should label it 'late_needs_review'.",
    ("large_deduction", "missing"):
        "The payment was deducted by more than 1%, beyond the short_paid threshold. "
        "The new same_bond_pairing pass should label it 'large_deduction_needs_review'.",
    ("split", "missing"):
        "The payment arrived as multiple halves. The old algorithm had no split detection. "
        "The new split pass (pass 3) should catch this.",
    ("combined", "missing"):
        "Two payments were merged into one row. The old algorithm had no combined detection. "
        "The new combined pass (pass 2) should catch this.",
}


def score(recon_csv: Path, ak_csv: Path, exp_csv: Path = EXPECTED_CSV) -> dict:
    """
    Score one round.  Returns a dict with:
      total_planted, found, correct_type, false_alarms,
      missed (list), mislabelled (list), false_alarm_rows (list)
    """
    ak    = pd.read_csv(ak_csv)
    recon = pd.read_csv(recon_csv)
    exp   = pd.read_csv(exp_csv)

    book_cusips = set(exp["cusip"].astype(str))
    breaks      = recon[recon["result"] != "matched"].copy()

    total_planted = len(ak)
    found_count   = 0
    correct_type  = 0
    missed        = []
    mislabelled   = []
    matched_break_indices = set()

    for _, ak_row in ak.iterrows():
        pid   = str(ak_row["payment_id"])       if pd.notna(ak_row["payment_id"])       else ""
        sid   = str(ak_row["statement_row_id"])  if pd.notna(ak_row["statement_row_id"]) else ""
        btype = ak_row["break_type"]
        ideal = AK_TO_RECON.get(btype, "")

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
            # For split, one answer-key entry maps to N recon rows (one per half).
            # Mark all of them so none shows up as a false alarm.
            if btype == "split":
                for idx in cands.index:
                    matched_break_indices.add(idx)
            else:
                matched_break_indices.add(found_row.name)
            found_count += 1
            if found_row["result"] == ideal:
                correct_type += 1
            else:
                mislabelled.append({
                    "payment_id":    pid or "—",
                    "stmt_row_id":   sid or "—",
                    "planted_type":  btype,
                    "ideal_recon":   ideal,
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

    false_alarms      = set(breaks.index) - matched_break_indices
    fa_count          = len(false_alarms)
    false_alarm_rows  = breaks.loc[sorted(false_alarms)].to_dict("records")

    return {
        "total_planted":    total_planted,
        "found":            found_count,
        "correct_type":     correct_type,
        "false_alarms":     fa_count,
        "missed":           missed,
        "mislabelled":      mislabelled,
        "false_alarm_rows": false_alarm_rows,
        "book_cusips":      book_cusips,
        "ak":               ak,
        "breaks":           breaks,
        "matched_break_indices": matched_break_indices,
    }


def print_report(result: dict, ak_csv: Path, recon_csv: Path) -> None:
    total_planted = result["total_planted"]
    found_count   = result["found"]
    correct_type  = result["correct_type"]
    fa_count      = result["false_alarms"]
    missed        = result["missed"]
    mislabelled   = result["mislabelled"]
    book_cusips   = result["book_cusips"]
    ak            = result["ak"]
    breaks        = result["breaks"]
    matched_break_indices = result["matched_break_indices"]

    print(f"{'='*62}")
    print(f"RECONCILIATION SCORE  (Module 6, Step 4)")
    print(f"  recon : {recon_csv.name}")
    print(f"  key   : {ak_csv.name}")
    print(f"{'='*62}")
    print(f"  Total planted breaks        : {total_planted}")
    print(f"  Breaks found                : {found_count} / {total_planted}")
    print(f"  Found with correct type     : {correct_type} / {found_count}")
    print(f"  False alarms (extra flags)  : {fa_count}")

    # Machine-parseable stats line (used by score_all_rounds.py)
    print(f"STATS: planted={total_planted} found={found_count} "
          f"correct={correct_type} false_alarms={fa_count}")

    # ── By type ──────────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"PLANTED BREAKS — FOUND vs. MISSED by type")
    print(f"{'='*62}")
    for btype in ak["break_type"].unique():
        ak_type   = ak[ak["break_type"] == btype]
        total     = len(ak_type)
        type_found = 0
        for _, r in ak_type.iterrows():
            r_pid = str(r["payment_id"])       if pd.notna(r["payment_id"])       else ""
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

    # ── Missed ────────────────────────────────────────────────────────────
    if missed:
        print(f"\n{'='*62}")
        print(f"MISSED BREAKS  ({len(missed)})")
        print(f"{'='*62}")
        for m in missed:
            print(f"  planted_type : {m['planted_type']}")
            print(f"  payment_id   : {m['payment_id']}   stmt_row: {m['stmt_row_id']}")
            print(f"  detail       : {m['detail']}")
            print()
    else:
        print(f"\n  No missed breaks — all {total_planted} planted breaks were found.")

    # ── Mislabelled ───────────────────────────────────────────────────────
    if mislabelled:
        print(f"\n{'='*62}")
        print(f"MISLABELLED BREAKS  ({len(mislabelled)})")
        print(f"{'='*62}")
        for m in mislabelled:
            reason = _MISLABEL_REASON.get(
                (m["planted_type"], m["found_type"]),
                f"Reconciler found it as '{m['found_type']}' "
                f"instead of '{m['ideal_recon']}'.",
            )
            print(f"  payment_id   : {m['payment_id']}   stmt_row: {m['stmt_row_id']}")
            print(f"  planted type : {m['planted_type']}   found as: {m['found_type']}"
                  f"  (ideal: {m['ideal_recon']})")
            print(f"  detail       : {m['detail']}")
            print(f"  plain-English reason: {reason}")
            print()
    else:
        print(f"\n  No mislabelled breaks — every found break has the correct type.")

    # ── False alarms ──────────────────────────────────────────────────────
    if result["false_alarm_rows"]:
        print(f"\n{'='*62}")
        print(f"FALSE ALARMS  ({fa_count})")
        print(f"{'='*62}")
        for row in result["false_alarm_rows"]:
            cu   = str(row["cusip"]) if row.get("cusip") else "—"
            sid  = str(row.get("statement_row_id", "—"))
            ep   = str(row.get("expected_payment_id", "—"))
            ar   = f"${float(row['amount_received']):,.2f}" if row.get("amount_received") else "—"
            in_books = cu in book_cusips
            if in_books:
                fa_reason = (
                    f"CUSIP {cu} IS in our portfolio. This row belongs to a planted "
                    "break that the reconciler could not pair — wrongly flagged as unknown."
                )
            else:
                fa_reason = f"CUSIP {cu} is NOT in our portfolio — genuinely unknown."
            print(f"  result={row['result']}  CUSIP={cu} {'(in books)' if in_books else '(unknown)'}")
            print(f"  stmt_row={sid}  exp_payment={ep}  received={ar}")
            print(f"  plain-English: {fa_reason}")
            print()
    else:
        print(f"\n  No false alarms — every flagged break was a real planted break.")

    # ── Verdict ───────────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"VERDICT")
    print(f"{'='*62}")
    if found_count == total_planted and correct_type == found_count and fa_count == 0:
        print(f"  PERFECT SCORE: {found_count}/{total_planted} planted breaks found,")
        print(f"  all with correct type, zero false alarms.")
    else:
        pct = round(100 * found_count / total_planted) if total_planted else 0
        print(f"  {found_count}/{total_planted} breaks found ({pct}%),  "
              f"{correct_type}/{found_count} correct type,  {fa_count} false alarm(s).")
    print(f"\nDone.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recon", type=Path, default=RECON_CSV)
    parser.add_argument("--ak",    type=Path, default=ANSWER_KEY_CSV)
    args = parser.parse_args()

    result = score(args.recon, args.ak)
    print_report(result, args.ak, args.recon)


if __name__ == "__main__":
    main()
