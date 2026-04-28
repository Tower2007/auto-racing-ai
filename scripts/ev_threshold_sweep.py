"""EV 閾値スイープ:ROI / n_bets / 総利益 / 年度別安定性のトレードオフ分析

ev_avg_calib (校正済 EV avg) ベース、top-1 限定で thr を細かく振る。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100
CALIB_CUTOFF = "2024-04"


def load() -> pd.DataFrame:
    preds = pd.read_parquet(DATA / "walkforward_predictions_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()

    df = preds.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]],
        on=RACE_KEY + ["car_no"], how="left",
    )
    df = df.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "payout"}),
        on=RACE_KEY + ["car_no"], how="left",
    )
    df["payout"] = df["payout"].fillna(0)
    df["hit"] = (df["payout"] > 0).astype(int)
    df["pred_rank"] = df.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)
    df["is_top1"] = (df["pred_rank"] == 1).astype(int)
    df["year"] = df["race_date"].dt.year
    df["year_month"] = df["race_date"].dt.to_period("M").astype(str)
    return df.dropna(subset=["place_odds_min"])


def calibrate_and_split(df: pd.DataFrame) -> pd.DataFrame:
    calib = df[df["test_month"] < CALIB_CUTOFF]
    eval_set = df[df["test_month"] >= CALIB_CUTOFF].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    eval_set["pred_calib"] = iso.transform(eval_set["pred"].values)
    eval_set["ev_avg_calib"] = (
        eval_set["pred_calib"] * (eval_set["place_odds_min"] + eval_set["place_odds_max"]) / 2
    )
    eval_set["ev_min_calib"] = eval_set["pred_calib"] * eval_set["place_odds_min"]
    return eval_set


def sweep(eval_set: pd.DataFrame, ev_col: str, top1_only: bool) -> pd.DataFrame:
    rows = []
    thrs = [round(x, 2) for x in np.arange(1.00, 3.01, 0.05)]
    for thr in thrs:
        sub = eval_set[eval_set[ev_col] >= thr]
        if top1_only:
            sub = sub[sub["is_top1"] == 1]
        if len(sub) == 0:
            continue
        cost = len(sub) * BET
        payout = sub["payout"].sum()
        # 月次 ROI 分散
        m = sub.groupby("year_month").agg(
            n=("payout", "size"), p=("payout", "sum"),
        ).reset_index()
        m["roi"] = m["p"] / (m["n"] * BET)
        rows.append({
            "thr": thr,
            "n_bets": len(sub),
            "hit_rate": sub["hit"].mean(),
            "roi": payout / cost,
            "profit": payout - cost,
            "n_months": len(m),
            "month_roi_mean": m["roi"].mean() if len(m) else 0,
            "month_roi_std": m["roi"].std() if len(m) > 1 else 0,
            "month_roi_min": m["roi"].min() if len(m) else 0,
            "month_ge_1": int((m["roi"] >= 1.0).sum()),
        })
    return pd.DataFrame(rows)


def fmt_yen(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(int(v)):,}"


def main():
    df = load()
    eval_set = calibrate_and_split(df)
    print(f"Eval set: {len(eval_set):,} rows ({CALIB_CUTOFF} 〜)")

    sw_avg_top1 = sweep(eval_set, "ev_avg_calib", top1_only=True)
    sw_min_top1 = sweep(eval_set, "ev_min_calib", top1_only=True)
    sw_avg_all = sweep(eval_set, "ev_avg_calib", top1_only=False)

    # 利益最大点
    best_avg_top1 = sw_avg_top1.loc[sw_avg_top1["profit"].idxmax()]
    best_avg_all = sw_avg_all.loc[sw_avg_all["profit"].idxmax()]
    best_avg_top1_roi = sw_avg_top1.loc[sw_avg_top1["roi"].idxmax()]

    print("\n=== top1, ev_avg_calib スイープ ===")
    print(sw_avg_top1.assign(
        roi=lambda d: (d["roi"] * 100).round(2).astype(str) + "%",
        hit_rate=lambda d: (d["hit_rate"] * 100).round(1).astype(str) + "%",
        month_roi_mean=lambda d: (d["month_roi_mean"] * 100).round(1).astype(str) + "%",
        month_roi_std=lambda d: (d["month_roi_std"] * 100).round(1).astype(str) + "%",
        month_roi_min=lambda d: (d["month_roi_min"] * 100).round(1).astype(str) + "%",
        profit=lambda d: d["profit"].astype(int),
    )[["thr", "n_bets", "hit_rate", "roi", "profit",
       "month_roi_mean", "month_roi_std", "month_roi_min",
       "month_ge_1", "n_months"]].to_string(index=False))

    print(f"\nBest profit (top1 ev_avg_calib): thr={best_avg_top1['thr']}, "
          f"n={int(best_avg_top1['n_bets']):,}, ROI={best_avg_top1['roi']*100:.2f}%, "
          f"profit={fmt_yen(best_avg_top1['profit'])}")
    print(f"Best ROI (top1 ev_avg_calib): thr={best_avg_top1_roi['thr']}, "
          f"n={int(best_avg_top1_roi['n_bets']):,}, ROI={best_avg_top1_roi['roi']*100:.2f}%, "
          f"profit={fmt_yen(best_avg_top1_roi['profit'])}")

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_threshold_sweep_{today}.md"
    REPORTS.mkdir(exist_ok=True)

    md = [
        f"# EV 閾値スイープ ({today})",
        "",
        f"対象: eval set 25ヶ月 (2024-04 〜 2026-04)、isotonic-calibrated pred 使用。",
        "",
        "閾値を上げると ROI は上がるが、ベット数・総利益・統計信頼性が変動する。",
        "ROI / 利益 / 月次安定性 のバランスを見つけるための表。",
        "",
        "## 1. top1 + ev_avg_calib(本命の honest 評価)",
        "",
        "ROI 列が ROI、profit が円換算の総利益、month_ge_1 は 25 ヶ月中 ROI≥100% だった月数",
        "",
        sw_avg_top1.assign(
            roi=lambda d: (d["roi"] * 100).round(2).astype(str) + "%",
            hit_rate=lambda d: (d["hit_rate"] * 100).round(1).astype(str) + "%",
            month_roi_mean=lambda d: (d["month_roi_mean"] * 100).round(1).astype(str) + "%",
            month_roi_std=lambda d: (d["month_roi_std"] * 100).round(1).astype(str) + "%",
            month_roi_min=lambda d: (d["month_roi_min"] * 100).round(1).astype(str) + "%",
            profit=lambda d: d["profit"].apply(fmt_yen),
            n_bets=lambda d: d["n_bets"].apply(lambda v: f"{int(v):,}"),
        )[["thr", "n_bets", "hit_rate", "roi", "profit",
           "month_roi_mean", "month_roi_std", "month_roi_min",
           "month_ge_1", "n_months"]].to_markdown(index=False),
        "",
        "### 注目点",
        "",
        f"- **利益最大**: thr={best_avg_top1['thr']:.2f} で profit={fmt_yen(best_avg_top1['profit'])} "
        f"(ROI {best_avg_top1['roi']*100:.2f}%, n={int(best_avg_top1['n_bets']):,})",
        f"- **ROI 最大**: thr={best_avg_top1_roi['thr']:.2f} で ROI={best_avg_top1_roi['roi']*100:.2f}% "
        f"(profit={fmt_yen(best_avg_top1_roi['profit'])}, n={int(best_avg_top1_roi['n_bets']):,})",
        "",
        "## 2. top1 + ev_min_calib(参考、min ベース)",
        "",
        sw_min_top1.assign(
            roi=lambda d: (d["roi"] * 100).round(2).astype(str) + "%",
            hit_rate=lambda d: (d["hit_rate"] * 100).round(1).astype(str) + "%",
            profit=lambda d: d["profit"].apply(fmt_yen),
            n_bets=lambda d: d["n_bets"].apply(lambda v: f"{int(v):,}"),
            month_roi_min=lambda d: (d["month_roi_min"] * 100).round(1).astype(str) + "%",
        )[["thr", "n_bets", "hit_rate", "roi", "profit", "month_roi_min", "month_ge_1"]]
        .to_markdown(index=False),
        "",
        "## 3. all_cars + ev_avg_calib(参考)",
        "",
        sw_avg_all.assign(
            roi=lambda d: (d["roi"] * 100).round(2).astype(str) + "%",
            hit_rate=lambda d: (d["hit_rate"] * 100).round(1).astype(str) + "%",
            profit=lambda d: d["profit"].apply(fmt_yen),
            n_bets=lambda d: d["n_bets"].apply(lambda v: f"{int(v):,}"),
            month_roi_min=lambda d: (d["month_roi_min"] * 100).round(1).astype(str) + "%",
        )[["thr", "n_bets", "hit_rate", "roi", "profit", "month_roi_min", "month_ge_1"]]
        .to_markdown(index=False),
        "",
        "## 4. 解釈",
        "",
        "- **ROI と n_bets はトレードオフ**(ROI 上げるなら機会減)",
        "- **profit(円)は thr で凹凸あり** — n の減速が ROI 上昇を超えるとピーク",
        "- **month_roi_std と month_roi_min** が分散指標。閾値高いと振れ幅大",
        "- 高 thr ほど **out-of-sample で再現する保証は薄れる**(サンプル少 = 偶然の幅大)",
    ]
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
