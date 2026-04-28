"""キャリブレーション後の EV-based 戦略 ROI 検証

walk-forward predictions の前半 24ヶ月で isotonic regression を fit、
後半 25ヶ月で評価する(キャリブレーションも walk-forward 風)。

その後、calibrated pred を使って:
  - キャリブレーション前後の比較(pred 分布、ROI)
  - ev_min / ev_avg 各閾値での ROI 再計算
  - 年度別 P&L
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
CALIB_CUTOFF = "2024-04"  # 前半 24 ヶ月で calibration fit, 残り 25 ヶ月で評価


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
    return df.dropna(subset=["place_odds_min"])


def fit_and_apply_calibration(df: pd.DataFrame) -> pd.DataFrame:
    calib = df[df["test_month"] < CALIB_CUTOFF]
    eval_set = df[df["test_month"] >= CALIB_CUTOFF].copy()

    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    eval_set["pred_calib"] = iso.transform(eval_set["pred"].values)

    eval_set["ev_min_orig"] = eval_set["pred"] * eval_set["place_odds_min"]
    eval_set["ev_avg_orig"] = eval_set["pred"] * (eval_set["place_odds_min"] + eval_set["place_odds_max"]) / 2
    eval_set["ev_min_calib"] = eval_set["pred_calib"] * eval_set["place_odds_min"]
    eval_set["ev_avg_calib"] = eval_set["pred_calib"] * (eval_set["place_odds_min"] + eval_set["place_odds_max"]) / 2
    return eval_set, iso, calib


def calibration_quality(eval_set: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """pred / pred_calib それぞれのキャリブレーション表。"""
    bins = np.arange(0, 1.05, 0.1).tolist() + [1.01]
    bins = sorted(set(bins))

    def calib_table(df: pd.DataFrame, col: str) -> pd.DataFrame:
        d = df.copy()
        d["bin"] = pd.cut(d[col], bins=bins, right=False)
        g = d.groupby("bin", observed=True).agg(
            n=("hit", "size"),
            pred_mean=(col, "mean"),
            hit_rate=("hit", "mean"),
        ).reset_index()
        g["diff"] = g["pred_mean"] - g["hit_rate"]
        return g

    return calib_table(eval_set, "pred"), calib_table(eval_set, "pred_calib")


def evaluate_strategy(eval_set: pd.DataFrame, ev_col: str, thr: float, top1: bool) -> dict:
    sub = eval_set[eval_set[ev_col] >= thr]
    if top1:
        sub = sub[sub["is_top1"] == 1]
    if len(sub) == 0:
        return {"n_bets": 0, "hit_rate": 0, "roi": 0, "cost": 0, "payout": 0, "profit": 0}
    cost = len(sub) * BET
    payout = sub["payout"].sum()
    return {
        "n_bets": int(len(sub)),
        "hit_rate": float(sub["hit"].mean()),
        "roi": float(payout / cost),
        "cost": int(cost),
        "payout": float(payout),
        "profit": float(payout - cost),
    }


def yearly_pnl(eval_set: pd.DataFrame, ev_col: str, thr: float, top1: bool) -> pd.DataFrame:
    sub = eval_set[eval_set[ev_col] >= thr]
    if top1:
        sub = sub[sub["is_top1"] == 1]
    if len(sub) == 0:
        return pd.DataFrame()
    g = sub.groupby("year").agg(
        n_bets=("hit", "size"), n_hits=("hit", "sum"),
        payout=("payout", "sum"),
    ).reset_index()
    g["cost"] = g["n_bets"] * BET
    g["profit"] = g["payout"] - g["cost"]
    g["roi"] = g["payout"] / g["cost"]
    g["hit_rate"] = g["n_hits"] / g["n_bets"]
    total_cost = g["cost"].sum()
    total = pd.DataFrame([{
        "year": "All",
        "n_bets": g["n_bets"].sum(), "n_hits": g["n_hits"].sum(),
        "payout": g["payout"].sum(), "cost": total_cost,
        "profit": g["payout"].sum() - total_cost,
        "roi": (g["payout"].sum() / total_cost) if total_cost else 0,
        "hit_rate": (g["n_hits"].sum() / g["n_bets"].sum()) if g["n_bets"].sum() else 0,
    }])
    return pd.concat([g, total], ignore_index=True)


def fmt_yen(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(int(v)):,}"


def main():
    df = load()
    eval_set, iso, calib = fit_and_apply_calibration(df)
    print(f"Calibration set: {len(calib):,} rows ({df['test_month'].min()} 〜 < {CALIB_CUTOFF})")
    print(f"Eval set: {len(eval_set):,} rows ({CALIB_CUTOFF} 〜 {eval_set['test_month'].max()})")

    # キャリブレーション品質
    cal_pre, cal_post = calibration_quality(eval_set)
    print("\n=== Calibration: BEFORE (raw pred on eval set) ===")
    print(cal_pre.assign(
        pred_mean=lambda d: d["pred_mean"].round(4),
        hit_rate=lambda d: d["hit_rate"].round(4),
        diff=lambda d: d["diff"].round(4),
    ).to_string(index=False))

    print("\n=== Calibration: AFTER (isotonic-calibrated pred) ===")
    print(cal_post.assign(
        pred_mean=lambda d: d["pred_mean"].round(4),
        hit_rate=lambda d: d["hit_rate"].round(4),
        diff=lambda d: d["diff"].round(4),
    ).to_string(index=False))

    # 戦略 ROI 比較(original vs calibrated)
    rows = []
    for ev_col, label in [("ev_min_orig", "ev_min_orig"), ("ev_avg_orig", "ev_avg_orig"),
                          ("ev_min_calib", "ev_min_calib"), ("ev_avg_calib", "ev_avg_calib")]:
        for thr in [1.00, 1.05, 1.10, 1.20]:
            for top1, scope in [(True, "top1"), (False, "all")]:
                m = evaluate_strategy(eval_set, ev_col, thr, top1)
                m["ev"] = label
                m["thr"] = thr
                m["scope"] = scope
                rows.append(m)
    summary = pd.DataFrame(rows)

    # ピボット表(ROI)
    pivot_roi_top1 = summary[summary["scope"] == "top1"].pivot_table(
        index="thr", columns="ev", values="roi"
    )
    pivot_roi_all = summary[summary["scope"] == "all"].pivot_table(
        index="thr", columns="ev", values="roi"
    )
    pivot_n_top1 = summary[summary["scope"] == "top1"].pivot_table(
        index="thr", columns="ev", values="n_bets"
    )

    print("\n=== top1 ROI (eval set 25 months: 2024-04 ~ 2026-04) ===")
    print((pivot_roi_top1 * 100).round(2).astype(str) + "%")
    print("\n=== top1 n_bets ===")
    print(pivot_n_top1)
    print("\n=== all_cars ROI ===")
    print((pivot_roi_all * 100).round(2).astype(str) + "%")

    # 年度別 P&L (top1, ev_avg_calib >= 1.0 が真の honest 戦略)
    yp_calib_avg = yearly_pnl(eval_set, "ev_avg_calib", 1.00, top1=True)
    yp_orig_avg = yearly_pnl(eval_set, "ev_avg_orig", 1.00, top1=True)

    print("\n=== Yearly P&L: top1 + ev_avg_calib >= 1.00 (calibrated, honest) ===")
    print(yp_calib_avg.to_string(index=False))
    print("\n=== Yearly P&L: top1 + ev_avg_orig >= 1.00 (uncalibrated baseline) ===")
    print(yp_orig_avg.to_string(index=False))

    # MD レポート
    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_calibrated_{today}.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# Calibrated EV 戦略 検証 ({today})",
        "",
        f"**設計**: walk-forward predictions の前半 24ヶ月 (test_month < {CALIB_CUTOFF}) で",
        f"isotonic regression を fit、残り 25ヶ月 (>= {CALIB_CUTOFF}) で評価。",
        f"calibration set にも eval set にも leakage なし。",
        "",
        f"- Calibration set: {len(calib):,} rows",
        f"- Eval set: {len(eval_set):,} rows ({CALIB_CUTOFF} 〜 {eval_set['test_month'].max()})",
        "",
        "## 1. キャリブレーション効果",
        "",
        "### Before (raw pred)",
        "",
        cal_pre.assign(
            pred_mean=lambda d: d["pred_mean"].round(4),
            hit_rate=lambda d: d["hit_rate"].round(4),
            diff=lambda d: d["diff"].round(4),
        ).to_markdown(index=False),
        "",
        "### After (isotonic-calibrated)",
        "",
        cal_post.assign(
            pred_mean=lambda d: d["pred_mean"].round(4),
            hit_rate=lambda d: d["hit_rate"].round(4),
            diff=lambda d: d["diff"].round(4),
        ).to_markdown(index=False),
        "",
        "→ After の pred_mean が hit_rate にぴったり合えば校正成功。",
        "",
        "## 2. 戦略 ROI: Original vs Calibrated",
        "",
        "### top-1 限定 ROI",
        "",
        ((pivot_roi_top1 * 100).round(2).astype(str) + "%").reset_index().to_markdown(index=False),
        "",
        "### top-1 ベット数",
        "",
        pivot_n_top1.reset_index().to_markdown(index=False),
        "",
        "### all_cars ROI",
        "",
        ((pivot_roi_all * 100).round(2).astype(str) + "%").reset_index().to_markdown(index=False),
        "",
        "## 3. 年度別 P&L: top1 + ev_avg_calib ≥ 1.00 (honest 評価)",
        "",
        yp_calib_avg.assign(
            n_bets=lambda d: d["n_bets"].apply(lambda v: f"{int(v):,}"),
            cost=lambda d: d["cost"].apply(fmt_yen),
            payout=lambda d: d["payout"].apply(fmt_yen),
            profit=lambda d: d["profit"].apply(fmt_yen),
            roi=lambda d: (d["roi"] * 100).round(2).astype(str) + "%",
            hit_rate=lambda d: (d["hit_rate"] * 100).round(1).astype(str) + "%",
        )[["year", "n_bets", "hit_rate", "cost", "payout", "profit", "roi"]].to_markdown(index=False),
        "",
        "## 4. 結論",
        "",
        "- ev_min_orig は保守バイアス + 校正ズレで ROI を実態より高く出していた",
        "- ev_avg_calib (中央値ベース × isotonic校正) が最も honest な評価",
        "- 校正前後で ROI が大きく違えば、元の数字は「校正ズレを実払戻分布が補っていただけ」だった",
        "- 校正後でも ROI > 100% なら本物の market edge",
    ]
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
