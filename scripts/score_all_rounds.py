#!/usr/bin/env python3
"""
Module 6 — Score all three rounds and print a comparison table.

Round 1 (easy)  : seed 42, 19 planted breaks
Round 2 (hard)  : seed 7,  11 planted breaks (6 advanced types)
Round 3         : seed 99, 16 planted breaks (all types)

For rounds 2 and 3, the reconciler is run via file swap since reconcile_cash.py
uses hardcoded paths.  Round 1 uses the already-current files.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT    = Path(__file__).parent.parent
PROC    = ROOT / "data" / "processed"
PYTHON  = Path(sys.executable)

RECONCILE = Path(__file__).parent / "reconcile_cash.py"
SCORE     = Path(__file__).parent / "score_reconciliation.py"

# Production files (always restored)
STMT_ORIG  = PROC / "module6_bank_statement.csv"
AK_ORIG    = PROC / "module6_answer_key.csv"
RECON_ORIG = PROC / "module6_recon_results.csv"

# Round-specific files
ROUNDS = {
    "Round 1 (easy)": {
        "stmt": None,  # already the production statement
        "ak":   AK_ORIG,
        "recon_out": PROC / "module6_recon_results.csv",
        "saved_recon": None,
    },
    "Round 2 (hard)": {
        "stmt": PROC / "module6_bank_statement_hard.csv",
        "ak":   PROC / "module6_answer_key_hard.csv",
        "recon_out": PROC / "module6_recon_results.csv",
        "saved_recon": PROC / "module6_recon_results_hard.csv",
    },
    "Round 3": {
        "stmt": PROC / "module6_bank_statement_round3.csv",
        "ak":   PROC / "module6_answer_key_round3.csv",
        "recon_out": PROC / "module6_recon_results.csv",
        "saved_recon": PROC / "module6_recon_results_round3.csv",
    },
}

_STATS_RE = re.compile(
    r"STATS: planted=(\d+) found=(\d+) correct=(\d+) false_alarms=(\d+)"
)


def _run_round(name: str, cfg: dict) -> dict:
    """Run reconcile + score for one round; return parsed stats dict."""
    stmt        = cfg["stmt"]
    ak          = cfg["ak"]
    saved_recon = cfg["saved_recon"]

    stmt_bak  = PROC / "_bak_stmt.csv"
    ak_bak    = PROC / "_bak_ak.csv"
    recon_bak = PROC / "_bak_recon.csv"

    swapped = stmt is not None   # Round 1 needs no swap

    if swapped:
        shutil.copy2(STMT_ORIG,  stmt_bak)
        shutil.copy2(AK_ORIG,    ak_bak)
        shutil.copy2(RECON_ORIG, recon_bak)
        shutil.copy2(stmt, STMT_ORIG)

    try:
        subprocess.run([str(PYTHON), str(RECONCILE)],
                       check=True, capture_output=True)

        if saved_recon:
            shutil.copy2(RECON_ORIG, saved_recon)

        result = subprocess.run(
            [str(PYTHON), str(SCORE),
             "--recon", str(RECON_ORIG),
             "--ak",    str(ak)],
            capture_output=True, text=True, check=True
        )
        output = result.stdout

    finally:
        if swapped:
            shutil.copy2(stmt_bak,  STMT_ORIG)
            shutil.copy2(ak_bak,    AK_ORIG)
            shutil.copy2(recon_bak, RECON_ORIG)
            stmt_bak.unlink(missing_ok=True)
            ak_bak.unlink(missing_ok=True)
            recon_bak.unlink(missing_ok=True)

    m = _STATS_RE.search(output)
    if not m:
        print(f"\n[{name}] scorer output:\n{output}")
        raise RuntimeError(f"Could not parse STATS line from {name}")

    return {
        "name":         name,
        "planted":      int(m.group(1)),
        "found":        int(m.group(2)),
        "correct_type": int(m.group(3)),
        "false_alarms": int(m.group(4)),
        "output":       output,
    }


def main():
    rows = []
    for name, cfg in ROUNDS.items():
        print(f"\n{'─'*62}")
        print(f"Running: {name}")
        print(f"{'─'*62}")
        r = _run_round(name, cfg)
        rows.append(r)
        # Print the full scorer output for this round
        print(r["output"])

    # ── Comparison table ──────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"COMPARISON TABLE — All Rounds")
    print(f"{'='*72}")
    hdr = (f"  {'Round':<22}  {'Planted':>7}  {'Found':>7}  "
           f"{'Correct type':>12}  {'False alarms':>12}")
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for r in rows:
        p   = r["planted"]
        f   = r["found"]
        ct  = r["correct_type"]
        fa  = r["false_alarms"]
        mark = "✓ perfect" if (f == p and ct == f and fa == 0) else ""
        print(f"  {r['name']:<22}  {p:>7}  {f:>5}/{p:<1}  "
              f"{ct:>8}/{f:<1}  {'':>4}{fa:>3}  {mark}")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
