"""baseline_fns_only 追加監査スクリプト案。

目的:
- calibration cutoff を 2024-01..2024-06 などにずらし、
  thr 固定(1.30/1.45/1.50/1.80)と「各 cutoff 内で profit 最大 thr」を分けて見る。
- closing odds の ev_avg と実払戻の比率を top1 hit 限定で確認する。

想定実行:
    uv run python Opinion/baseline_audit/proposal_cutoff_sensitivity.py

注意:
- 本体コードは変更しない検証用。
- pyarrow/pandas/scikit-learn が必要。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100


def load_base() -> pd.DataFrame:
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])

    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])

    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = (
        pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
        .groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"]
        .sum()
        .rename(columns={"car_no_1": "car_no", "refund": "payout"})
    )

    df = preds.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]],
        on=RACE_KEY + ["car_no"],
        how="left",
    )
    df = df.merge(fns, on=RACE_KEY + ["car_no"], how="left")
    df["payout"] = df["payout"].fillna(0)
    df["pred_rank"] = df.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)
    return df.dropna(subset=["place_odds_min"]).copy()


def evaluate(df: pd.DataFrame, cutoff: str, thr: float) -> dict:
    calib = df[df["test_month"] < cutoff]
    eval_df = df[df["test_month"] >= cutoff].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    eval_df["pred_calib"] = iso.transform(eval_df["pred"].values)
    eval_df["ev_avg_calib"] = eval_df["pred_calib"] * (
        eval_df["place_odds_min"] + eval_df["place_odds_max"]
    ) / 2

    sub = eval_df[(eval_df["pred_rank"] == 1) & (eval_df["ev_avg_calib"] >= thr)]
    monthly = sub.groupby("test_month").agg(n=("payout", "size"), p=("payout", "sum"))
    monthly["roi"] = monthly["p"] / (monthly["n"] * BET)

    cost = len(sub) * BET
    payout = float(sub["payout"].sum())
    return {
        "cutoff": cutoff,
        "thr": thr,
        "eval_months": int(eval_df["test_month"].nunique()),
        "n_bets": int(len(sub)),
        "roi": payout / cost if cost else np.nan,
        "profit": int(payout - cost),
        "month_ge_1": int((monthly["roi"] >= 1.0).sum()),
        "month_min_roi": float(monthly["roi"].min()) if len(monthly) else np.nan,
    }


def payout_ratio(df: pd.DataFrame, cutoff: str = "2024-04", thr: float = 1.50) -> pd.Series:
    calib = df[df["test_month"] < cutoff]
    eval_df = df[df["test_month"] >= cutoff].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    eval_df["pred_calib"] = iso.transform(eval_df["pred"].values)
    eval_df["odds_avg"] = (eval_df["place_odds_min"] + eval_df["place_odds_max"]) / 2
    eval_df["ev_avg_calib"] = eval_df["pred_calib"] * eval_df["odds_avg"]
    hit = eval_df[
        (eval_df["pred_rank"] == 1)
        & (eval_df["ev_avg_calib"] >= thr)
        & (eval_df["payout"] > 0)
    ].copy()
    hit["realized_odds"] = hit["payout"] / 100.0
    hit["realized_div_avg"] = hit["realized_odds"] / hit["odds_avg"]
    return hit["realized_div_avg"].describe(percentiles=[0.05, 0.25, 0.5, 0.75, 0.95])


def main() -> None:
    df = load_base()
    cutoffs = ["2024-01", "2024-02", "2024-03", "2024-04", "2024-05", "2024-06"]
    thrs = [1.30, 1.45, 1.50, 1.80]
    rows = [evaluate(df, cutoff, thr) for cutoff in cutoffs for thr in thrs]
    print(pd.DataFrame(rows).to_string(index=False))
    print()
    print("realized payout / odds_avg for hit picks")
    print(payout_ratio(df).to_string())


if __name__ == "__main__":
    main()
