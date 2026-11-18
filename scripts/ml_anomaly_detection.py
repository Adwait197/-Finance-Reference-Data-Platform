"""
Module 3: ML anomaly detection on security_master using IsolationForest.

Approach
  IsolationForest builds an ensemble of random binary trees and isolates
  each point by randomly selecting a feature and a split value. Points
  that require fewer splits to isolate are more anomalous — they sit in
  sparse or extreme regions of the feature space.

  Three numeric features are used:
    valUSD  — market value in USD (range: ~$28k to ~$66B)
    pctval  — portfolio weight in % (range: ~0.000 to ~7.5)
    balance — share/unit count    (range: ~5 to ~325M)

  These are on very different scales, so they are standardised (z-scored)
  before fitting so no single feature dominates by magnitude.

  contamination=0.02 tells the model to treat ~2% of 508 rows (~10) as
  anomalies.  random_state=42 ensures results are reproducible.

Cross-check
  Each ML-flagged row is checked against the deterministic QC exceptions in
  data/processed/qc_exceptions_2026-08-25.csv to show:
    - overlap  : ML and rules agree (rule-based evidence for the anomaly)
    - new finds: ML flags it but no rule caught it (candidates for new rules)
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
import sqlalchemy
from dotenv import load_dotenv
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

load_dotenv()

PGHOST     = os.getenv("PGHOST", "localhost")
PGPORT     = int(os.getenv("PGPORT", "5432"))
PGDATABASE = os.getenv("PGDATABASE", "finance_platform")
PGUSER     = os.getenv("PGUSER", "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

REPO_ROOT    = Path(__file__).resolve().parent.parent
QC_CSV       = REPO_ROOT / "data" / "processed" / "qc_exceptions_2026-08-25.csv"
OUTPUT_CSV   = REPO_ROOT / "data" / "processed" / "ml_anomalies_2026-08-25.csv"

FEATURES          = ["valusd", "pctval", "balance"]
CONTAMINATION     = 0.02
RANDOM_STATE      = 42


# ── load data ─────────────────────────────────────────────────────────────────

def load_data() -> pd.DataFrame:
    print("Loading security_master from PostgreSQL ...")
    engine = sqlalchemy.create_engine(
        f"postgresql+psycopg2://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}"
    )
    with engine.connect() as conn:
        df = pd.read_sql("SELECT * FROM security_master", conn)
    for col in FEATURES:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    print(f"  {len(df)} rows loaded.")
    return df


# ── feature preparation ───────────────────────────────────────────────────────

def prepare_features(df: pd.DataFrame):
    """
    Fill nulls with 0, record which rows were affected, then z-score scale.
    Returns the scaled array and the filled (pre-scale) DataFrame slice.
    """
    feature_df = df[FEATURES].copy()

    null_counts = feature_df.isna().sum()
    total_null_rows = feature_df.isna().any(axis=1).sum()
    if total_null_rows:
        print(f"\nNull fills applied before scaling:")
        for col, n in null_counts.items():
            if n:
                print(f"  {col}: {n} null(s) filled with 0")
        print(f"  Total rows with at least one null filled: {total_null_rows}")
    else:
        print(f"\nNo null values found in feature columns — no fills needed.")

    feature_df = feature_df.fillna(0)

    scaler     = StandardScaler()
    X_scaled   = scaler.fit_transform(feature_df)
    return X_scaled, feature_df


# ── model ─────────────────────────────────────────────────────────────────────

def run_isolation_forest(X_scaled: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Fit IsolationForest and return (predictions, scores).
    prediction: -1 = anomaly, +1 = normal
    score     : lower (more negative) = more anomalous
    """
    model = IsolationForest(
        contamination=CONTAMINATION,
        random_state=RANDOM_STATE,
        n_estimators=200,    # more trees → more stable scores
    )
    predictions = model.fit_predict(X_scaled)
    scores      = model.score_samples(X_scaled)   # higher = more normal
    return predictions, scores


# ── cross-check against rule engine ──────────────────────────────────────────

def cross_check(anomaly_ids: set, qc_path: Path) -> dict:
    if not qc_path.exists():
        print(f"\nQC exceptions file not found at {qc_path} — skipping cross-check.")
        return {"overlap": set(), "new_finds": anomaly_ids}

    qc_df    = pd.read_csv(qc_path)
    qc_ids   = set(qc_df["id"].tolist())
    overlap  = anomaly_ids & qc_ids
    new_finds = anomaly_ids - qc_ids
    return {"overlap": overlap, "new_finds": new_finds, "qc_ids": qc_ids}


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    df = load_data()

    # ── features ──────────────────────────────────────────────────────────────
    print(f"\nFeatures used: {FEATURES}")
    X_scaled, feature_df = prepare_features(df)

    # ── model ──────────────────────────────────────────────────────────────────
    print(f"\nFitting IsolationForest  "
          f"(contamination={CONTAMINATION}, n_estimators=200, random_state={RANDOM_STATE}) ...")
    predictions, scores = run_isolation_forest(X_scaled)

    df["if_prediction"] = predictions   # -1 anomaly, +1 normal
    df["if_score"]      = scores        # lower = more anomalous

    n_anomalies = (predictions == -1).sum()
    print(f"  Flagged {n_anomalies} anomalies out of {len(df)} rows "
          f"({n_anomalies/len(df)*100:.1f}%).")

    # ── print flagged rows ────────────────────────────────────────────────────
    anomalies = (
        df[df["if_prediction"] == -1]
        .sort_values("if_score")          # most anomalous first
        [["id", "name", "valusd", "pctval", "balance", "if_score"]]
    )

    print(f"\nAnomaly-flagged rows (sorted most-anomalous first):")
    print(f"  {'id':>4}  {'name':<45}  {'valUSD':>18}  {'pctVal':>8}  {'balance':>14}  {'score':>8}")
    print("  " + "-" * 103)
    for _, row in anomalies.iterrows():
        print(
            f"  {int(row['id']):>4}  "
            f"{str(row['name']):<45}  "
            f"{row['valusd']:>18,.2f}  "
            f"{row['pctval']:>8.4f}  "
            f"{row['balance']:>14,.0f}  "
            f"{row['if_score']:>8.4f}"
        )

    # ── cross-check ───────────────────────────────────────────────────────────
    anomaly_ids = set(df.loc[df["if_prediction"] == -1, "id"].tolist())
    cc = cross_check(anomaly_ids, QC_CSV)

    print(f"\nCross-check vs rule-engine exceptions ({QC_CSV.name}):")
    print(f"  ML anomalies                   : {len(anomaly_ids)}")
    print(f"  Also caught by rule engine     : {len(cc['overlap'])}  (ids: {sorted(cc['overlap'])})")
    print(f"  New finds (ML only, no rule)   : {len(cc['new_finds'])}  (ids: {sorted(cc['new_finds'])})")

    if cc["new_finds"]:
        print(f"\n  New-find detail (candidates for new deterministic rules):")
        new_df = df[df["id"].isin(cc["new_finds"])][
            ["id", "name", "valusd", "pctval", "balance", "if_score",
             "assetcat", "issuercat", "invcountry", "cusip"]
        ].sort_values("if_score")
        print(new_df.to_string(index=False))

    # ── save full results ─────────────────────────────────────────────────────
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    # Add a human-readable label and cross-check column
    def _label(row):
        if row["if_prediction"] != -1:
            return ""
        if row["id"] in cc.get("overlap", set()):
            return "anomaly (also rule-flagged)"
        return "anomaly (ML only)"

    df["ml_label"] = df.apply(_label, axis=1)

    save_cols = [
        "id", "name", "cusip", "isin",
        "valusd", "pctval", "balance",
        "assetcat", "issuercat", "invcountry",
        "if_score", "if_prediction", "ml_label",
    ]
    df[save_cols].sort_values("if_score").to_csv(
        OUTPUT_CSV, index=False, float_format="%.6f"
    )
    print(f"\nFull ML results saved to {OUTPUT_CSV.relative_to(REPO_ROOT)}")
    print(f"  {len(df)} rows total — ml_label is non-empty for anomaly rows only.")


if __name__ == "__main__":
    main()
