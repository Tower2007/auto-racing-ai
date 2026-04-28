"""EV-based 戦略の年度別安定性 + キャリブレーション検証

ev_selection.py で見つけた「全戦略 ROI > 100%」が
overfitting / 偶然 / leakage でないかを多角的に検証する。

検証 1: 複数 EV 閾値で年度別 ROI を一覧
検証 2: EV ビン別の実 ROI(キャリブレーション確認)
        モデルが calibrated なら EV と実 ROI が一致するはず
検証 3: 月次 ROI 分布(分散・最小最大)
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100


def load() -> pd.DataFrame:
    preds = pd.read_parquet(DATA / "walkforward_predictions_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])

    df = preds.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max", "win_odds"]],
        on=RACE_KEY + ["car_no"], how="left",
    )
    df["ev_min"] = df["pred"] * df["place_odds_min"]
    df["ev_avg"] = df["pred"] * (df["place_odds_min"] + df["place_odds_max"]) / 2
    df["pred_rank"] = df.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)
    df["is_top1"] = (df["pred_rank"] == 1).astype(int)
    df = df.dropna(subset=["place_odds_min"])

    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()
    df = df.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "payout"}),
        on=RACE_KEY + ["car_no"], how="left",
    )
    df["payout"] = df["payout"].fillna(0)
    df["hit"] = (df["payout"] > 0).astype(int)
    df["year"] = df["race_date"].dt.year
    df["year_month"] = df["race_date"].dt.to_period("M").astype(str)
    return df


def yearly_pivot(df: pd.DataFrame, restrict_top1: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """複数 EV 閾値で年度別 ROI と n_bets を出す。"""
    thresholds = [1.00, 1.05, 1.10, 1.20, 1.30, 1.50]
    rows_roi = []
    rows_n = []
    for thr in thresholds:
        sub = df[df["ev_min"] >= thr]
        if restrict_top1:
            sub = sub[sub["is_top1"] == 1]
        for year in sorted(df["year"].unique()):
            sub_y = sub[sub["year"] == year]
            n = len(sub_y)
            cost = n * BET
            payout = sub_y["payout"].sum()
            rows_roi.append({"thr": thr, "year": year, "roi": payout / cost if cost else 0})
            rows_n.append({"thr": thr, "year": year, "n_bets": n})
        # All
        cost_all = len(sub) * BET
        rows_roi.append({"thr": thr, "year": "All", "roi": sub["payout"].sum() / cost_all if cost_all else 0})
        rows_n.append({"thr": thr, "year": "All", "n_bets": len(sub)})

    pivot_roi = pd.DataFrame(rows_roi).pivot(index="thr", columns="year", values="roi")
    pivot_n = pd.DataFrame(rows_n).pivot(index="thr", columns="year", values="n_bets")
    return pivot_roi, pivot_n


def calibration_table(df: pd.DataFrame) -> pd.DataFrame:
    """EV_min ビン別の実 ROI vs 期待 ROI を比較。
    モデルが calibrated なら expected ROI ≒ actual ROI。
    """
    bins = [0, 0.5, 0.7, 0.85, 0.95, 1.00, 1.05, 1.15, 1.30, 1.50, 10.0]
    df = df.copy()
    df["ev_bin"] = pd.cut(df["ev_min"], bins=bins, right=False)

    g = df.groupby("ev_bin", observed=True).agg(
        n=("hit", "size"),
        n_hits=("hit", "sum"),
        actual_payout=("payout", "sum"),
        ev_min_mean=("ev_min", "mean"),
        pred_mean=("pred", "mean"),
        odds_min_mean=("place_odds_min", "mean"),
    ).reset_index()
    g["actual_roi"] = g["actual_payout"] / (g["n"] * BET)
    g["expected_roi_min"] = g["ev_min_mean"]  # = pred × odds_min
    g["hit_rate"] = g["n_hits"] / g["n"]
    return g


def monthly_distribution(df: pd.DataFrame, ev_thr: float, top1: bool) -> dict:
    sub = df[df["ev_min"] >= ev_thr]
    if top1:
        sub = sub[sub["is_top1"] == 1]
    if len(sub) == 0:
        return {"n_months": 0}
    m = sub.groupby("year_month").agg(
        n=("payout", "size"),
        payout=("payout", "sum"),
    ).reset_index()
    m["cost"] = m["n"] * BET
    m["roi"] = m["payout"] / m["cost"]
    return {
        "n_months": len(m),
        "roi_mean": m["roi"].mean(),
        "roi_median": m["roi"].median(),
        "roi_std": m["roi"].std(),
        "roi_min": m["roi"].min(),
        "roi_max": m["roi"].max(),
        "months_ge_1": int((m["roi"] >= 1.0).sum()),
        "n_total_bets": int(m["n"].sum()),
    }


def fmt_yen(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(int(v)):,}"


def main():
    df = load()
    print(f"Loaded {len(df):,} (race, car) rows with predictions+odds+payouts")

    # 検証 1: 年度別 ROI ピボット
    roi_top1, n_top1 = yearly_pivot(df, restrict_top1=True)
    roi_all, n_all = yearly_pivot(df, restrict_top1=False)

    # 検証 2: キャリブレーション
    cal = calibration_table(df)

    # 検証 3: 月次分布(複数 threshold で確認)
    monthly = []
    for thr in [1.00, 1.05, 1.10, 1.20]:
        for top1 in [True, False]:
            r = monthly_distribution(df, thr, top1)
            r["ev_thr"] = thr
            r["top1"] = top1
            monthly.append(r)
    monthly_df = pd.DataFrame(monthly)

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_verify_{today}.md"
    REPORTS.mkdir(exist_ok=True)

    md = [
        f"# EV-based 戦略の検証 ({today})",
        "",
        f"対象: {len(df):,} (race, car) 行(walk-forward 49ヶ月)",
        "",
        "目的: ev_selection.py で見つけた「ROI > 100%」が安定して再現できるか確認。",
        "",
        "## 1. 年度別 ROI(top-1 限定)",
        "",
        (roi_top1 * 100).round(1).astype(str) + "%",
    ][:-1] + [
        ((roi_top1 * 100).round(1).astype(str) + "%").reset_index().to_markdown(index=False),
        "",
        "### ベット数",
        "",
        n_top1.reset_index().to_markdown(index=False),
        "",
        "## 2. 年度別 ROI(全 car、複数同時購入あり)",
        "",
        ((roi_all * 100).round(1).astype(str) + "%").reset_index().to_markdown(index=False),
        "",
        "### ベット数",
        "",
        n_all.reset_index().to_markdown(index=False),
        "",
        "## 3. キャリブレーション(EV ビン別の期待 vs 実 ROI)",
        "",
        "モデル予測が校正されていれば `期待 ROI ≒ 実 ROI` のはず。",
        "",
        cal.assign(
            actual_roi=lambda d: (d["actual_roi"] * 100).round(2).astype(str) + "%",
            expected_roi_min=lambda d: (d["expected_roi_min"] * 100).round(2).astype(str) + "%",
            hit_rate=lambda d: (d["hit_rate"] * 100).round(1).astype(str) + "%",
            pred_mean=lambda d: d["pred_mean"].round(3),
            odds_min_mean=lambda d: d["odds_min_mean"].round(2),
            ev_min_mean=lambda d: d["ev_min_mean"].round(3),
        )[["ev_bin", "n", "n_hits", "hit_rate", "pred_mean", "odds_min_mean",
            "ev_min_mean", "expected_roi_min", "actual_roi"]].to_markdown(index=False),
        "",
        "## 4. 月次安定性",
        "",
        monthly_df.assign(
            roi_mean=lambda d: (d["roi_mean"] * 100).round(2).astype(str) + "%",
            roi_median=lambda d: (d["roi_median"] * 100).round(2).astype(str) + "%",
            roi_std=lambda d: (d["roi_std"] * 100).round(2).astype(str) + "%",
            roi_min=lambda d: (d["roi_min"] * 100).round(2).astype(str) + "%",
            roi_max=lambda d: (d["roi_max"] * 100).round(2).astype(str) + "%",
        )[["ev_thr", "top1", "n_months", "roi_mean", "roi_median", "roi_std",
            "roi_min", "roi_max", "months_ge_1", "n_total_bets"]].to_markdown(index=False),
        "",
    ]
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\n=== ROI by year (top-1) ===")
    print((roi_top1 * 100).round(1).astype(str) + "%")
    print(f"\n=== n_bets by year (top-1) ===")
    print(n_top1)
    print(f"\n=== Calibration ===")
    print(cal[["ev_bin", "n", "ev_min_mean", "actual_roi"]].to_string())
    print(f"\n=== Monthly stability ===")
    print(monthly_df.to_string(index=False))
    print(f"\nReport: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
