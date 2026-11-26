#!/usr/bin/env python3
"""
Module 6 — Hard-round orchestrator.

Runs the unchanged reconcile_cash.py against the hard bank statement by temporarily
swapping the two data files it reads, then scores the results with score_reconciliation.py
against the hard answer key, and saves recon output as module6_recon_results_hard.csv.

reconcile_cash.py has hardcoded paths:
  STATEMENT_CSV = .../module6_bank_statement.csv
  RECON_CSV     = .../module6_recon_results.csv
so we swap the files in and out rather than patching the script.

Steps:
  1. Copy hard statement → module6_bank_statement.csv       (reconciler reads this)
  2. Run reconcile_cash.py                                  (writes recon_results.csv)
  3. Copy recon_results.csv → module6_recon_results_hard.csv
  4. Copy hard answer key  → module6_answer_key.csv         (scorer reads this)
  5. Run score_reconciliation.py                            (scores hard recon vs hard key)
  6. Restore both originals                                 (guaranteed by finally block)
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT    = Path(__file__).parent.parent
PROC    = ROOT / "data" / "processed"

STMT_ORIG  = PROC / "module6_bank_statement.csv"
STMT_HARD  = PROC / "module6_bank_statement_hard.csv"
AK_ORIG    = PROC / "module6_answer_key.csv"
AK_HARD    = PROC / "module6_answer_key_hard.csv"
RECON_ORIG = PROC / "module6_recon_results.csv"
RECON_HARD = PROC / "module6_recon_results_hard.csv"

PYTHON           = Path(sys.executable)
RECONCILE_SCRIPT = Path(__file__).parent / "reconcile_cash.py"
SCORE_SCRIPT     = Path(__file__).parent / "score_reconciliation.py"

# Temporary backups (kept only during this run)
STMT_BACKUP = PROC / "_backup_module6_bank_statement.csv"
AK_BACKUP   = PROC / "_backup_module6_answer_key.csv"


def _run(label: str, script: Path) -> None:
    print(f"\n{'─'*62}")
    print(f"Running: {label}")
    print(f"{'─'*62}")
    result = subprocess.run([str(PYTHON), str(script)], text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{label} exited with code {result.returncode}")


def main():
    for path, name in [(STMT_HARD, "hard statement"), (AK_HARD, "hard answer key")]:
        if not path.exists():
            sys.exit(f"ERROR: {name} not found: {path}")

    # ── Backup originals (statement, answer key, and recon results) ──────────
    RECON_BACKUP = PROC / "_backup_module6_recon_results.csv"

    shutil.copy2(STMT_ORIG,  STMT_BACKUP)
    shutil.copy2(AK_ORIG,    AK_BACKUP)
    shutil.copy2(RECON_ORIG, RECON_BACKUP)
    print("Originals backed up.")

    try:
        # ── Step 1: swap in hard statement ────────────────────────────────────
        shutil.copy2(STMT_HARD, STMT_ORIG)
        print(f"Swapped in hard statement ({STMT_HARD.name}).")

        # ── Step 2: run reconciler (unchanged) ───────────────────────────────
        _run("reconcile_cash.py  (hard statement)", RECONCILE_SCRIPT)

        # ── Step 3: preserve hard recon results ──────────────────────────────
        shutil.copy2(RECON_ORIG, RECON_HARD)
        print(f"\nHard recon results saved → {RECON_HARD.name}")

        # ── Step 4: swap in hard answer key ──────────────────────────────────
        shutil.copy2(AK_HARD, AK_ORIG)
        print(f"Swapped in hard answer key ({AK_HARD.name}).")

        # ── Step 5: score ─────────────────────────────────────────────────────
        _run("score_reconciliation.py  (hard answer key)", SCORE_SCRIPT)

    finally:
        # ── Step 6: restore originals (always) ───────────────────────────────
        shutil.copy2(STMT_BACKUP,  STMT_ORIG)
        shutil.copy2(AK_BACKUP,    AK_ORIG)
        shutil.copy2(RECON_BACKUP, RECON_ORIG)
        STMT_BACKUP.unlink(missing_ok=True)
        AK_BACKUP.unlink(missing_ok=True)
        RECON_BACKUP.unlink(missing_ok=True)
        print(f"\nOriginals restored.")


if __name__ == "__main__":
    main()
