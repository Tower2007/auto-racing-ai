"""Walk-forward 月次評価

各テスト月について「その月以前の全データ(expanding window)」で訓練し、
その月の予測精度・ROI を計算。49 ヶ月程度回す。

使い方: python -m ml.walkforward [--target top3|win] [--min-train-months 12]
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from ml.train import (
    EXCLUDE_COLS, CATEGORICAL,
    load, prepare, train_lgb, evaluate, compute_roi, race_level_topk_acc,
)

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
DATA = ROOT / "data"

logger = logging.getLogger(__name__)


def _inner_val_split(X: pd.DataFrame, y: pd.Series, dates: pd.Series, frac: float = 0.1):
    """訓練データの末尾 frac を validation に切る(時系列順)。"""
    cutoff = dates.quantile(1 - frac)
    is_val = dates >= cutoff
    return (
        X[~is_val.values], X[is_val.values],
        y[~is_val.values], y[is_val.values],
    )


def walk_forward(target: str, min_train_months: int) -> pd.DataFrame:
    target_col = f"target_{target}"
    df = load()
    X, y, df_kept = prepare(df, target_col)

    payouts = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    payouts["race_date"] = pd.to_datetime(payouts["race_date"])

    months = sorted(df_kept["year_month"].unique())
    test_months = months[min_train_months:]
    logger.info(
        "Months total=%d, warmup=%d, test=%d (first=%s, last=%s)",
        len(months), min_train_months, len(test_months), test_months[0], test_months[-1],
    )

    rows = []
    for i, tm in enumerate(test_months, 1):
        is_train_full = df_kept["year_month"] < tm
        is_test = df_kept["year_month"] == tm
        if is_train_full.sum() == 0 or is_test.sum() == 0:
            continue

        X_tr_full = X.loc[is_train_full.values]
        y_tr_full = y.loc[is_train_full.values]
        train_dates = df_kept.loc[is_train_full, "race_date"]

        X_tr, X_va, y_tr, y_va = _inner_val_split(X_tr_full, y_tr_full, train_dates)

        # train (suppress per-iteration logging via custom callbacks)
        model = _train_silent(X_tr, y_tr, X_va, y_va)

        X_te = X.loc[is_test.values]
        y_te = y.loc[is_test.values]
        df_te = df_kept.loc[is_test, ["race_date", "place_code", "race_no", "car_no", "target_top3"]]

        m, p_te = evaluate(model, X_te, y_te)
        m["topk_pick_acc"] = race_level_topk_acc(df_te, p_te, k=3)
        m.update(compute_roi(df_te, p_te, payouts))
        m["month"] = tm
        m["n_train"] = int(len(X_tr_full))
        m["best_iter"] = model.best_iteration

        rows.append(m)
        logger.info(
            "[%2d/%d] %s | n_test=%5d auc=%.3f win_roi=%.3f place_roi=%.3f",
            i, len(test_months), tm, m["n"], m["auc"], m["win_roi"], m["place_roi"],
        )

    return pd.DataFrame(rows)


def _train_silent(X_tr, y_tr, X_va, y_va) -> lgb.Booster:
    train_data = lgb.Dataset(X_tr, label=y_tr, categorical_feature="auto")
    valid_data = lgb.Dataset(X_va, label=y_va, categorical_feature="auto", reference=train_data)
    params = {
        "objective": "binary",
        "metric": ["binary_logloss", "auc"],
        "learning_rate": 0.05,
        "num_leaves": 63,
        "feature_fraction": 0.85,
        "bagging_fraction": 0.85,
        "bagging_freq": 5,
        "min_data_in_leaf": 50,
        "verbosity": -1,
        "seed": 42,
    }
    return lgb.train(
        params, train_data, num_boost_round=2000,
        valid_sets=[valid_data], valid_names=["valid"],
        callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)],
    )


def write_report(df: pd.DataFrame, target: str) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"walkforward_{target}_{today}.md"

    summary = {
        "n_months": len(df),
        "auc_mean": df["auc"].mean(),
        "auc_std": df["auc"].std(),
        "logloss_mean": df["logloss"].mean(),
        "win_roi_mean": df["win_roi"].mean(),
        "win_roi_std": df["win_roi"].std(),
        "place_roi_mean": df["place_roi"].mean(),
        "place_roi_std": df["place_roi"].std(),
        "win_months_ge100": int((df["win_roi"] >= 1.0).sum()),
        "place_months_ge100": int((df["place_roi"] >= 1.0).sum()),
        "best_win_month": df.loc[df["win_roi"].idxmax(), "month"],
        "worst_win_month": df.loc[df["win_roi"].idxmin(), "month"],
    }

    lines = [
        f"# Walk-forward 月次評価 (target = {target}, {today})",
        "",
        f"- 評価月数: **{summary['n_months']}**",
        f"- 訓練方式: expanding window (各テスト月 t について、t より前の全データで訓練)",
        f"- 戦略: 各レースで予測 top-1 車を 100 円購入(単勝・複勝それぞれ)",
        "",
        "## サマリ",
        "",
        "| 指標 | 値 |",
        "|---|---:|",
        f"| AUC 平均 | {summary['auc_mean']:.4f} |",
        f"| AUC 標準偏差 | {summary['auc_std']:.4f} |",
        f"| logloss 平均 | {summary['logloss_mean']:.4f} |",
        f"| 単勝 ROI 平均 | **{summary['win_roi_mean']:.4f}** |",
        f"| 単勝 ROI 標準偏差 | {summary['win_roi_std']:.4f} |",
        f"| 単勝 ROI ≥ 1.0 月数 | **{summary['win_months_ge100']} / {summary['n_months']}** ({summary['win_months_ge100']/summary['n_months']*100:.1f}%) |",
        f"| 複勝 ROI 平均 | **{summary['place_roi_mean']:.4f}** |",
        f"| 複勝 ROI 標準偏差 | {summary['place_roi_std']:.4f} |",
        f"| 複勝 ROI ≥ 1.0 月数 | **{summary['place_months_ge100']} / {summary['n_months']}** ({summary['place_months_ge100']/summary['n_months']*100:.1f}%) |",
        f"| 最良 単勝 月 | {summary['best_win_month']} (ROI={df['win_roi'].max():.3f}) |",
        f"| 最悪 単勝 月 | {summary['worst_win_month']} (ROI={df['win_roi'].min():.3f}) |",
        "",
        "## 月次詳細",
        "",
    ]

    show_cols = ["month", "n", "n_train", "auc", "logloss",
                 "win_hit_rate", "win_roi", "place_hit_rate", "place_roi",
                 "topk_pick_acc", "best_iter"]
    fmt = df[show_cols].copy()
    for c in ["auc", "logloss", "win_hit_rate", "win_roi", "place_hit_rate", "place_roi", "topk_pick_acc"]:
        fmt[c] = fmt[c].round(4)
    lines.append(fmt.to_markdown(index=False))

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["top3", "win"], default="top3")
    parser.add_argument("--min-train-months", type=int, default=12)
    args = parser.parse_args()

    df = walk_forward(args.target, args.min_train_months)
    csv_out = DATA / f"walkforward_{args.target}.csv"
    df.to_csv(csv_out, index=False)
    logger.info("CSV saved: %s", csv_out)

    md = write_report(df, args.target)
    logger.info("Report saved: %s", md)

    # 簡易表示
    print()
    print(f"=== Summary (n_months={len(df)}) ===")
    print(f"  AUC: mean={df['auc'].mean():.4f}, std={df['auc'].std():.4f}")
    print(f"  Win ROI: mean={df['win_roi'].mean():.4f}, std={df['win_roi'].std():.4f}, "
          f">=1.0: {int((df['win_roi']>=1.0).sum())}/{len(df)}")
    print(f"  Place ROI: mean={df['place_roi'].mean():.4f}, std={df['place_roi'].std():.4f}, "
          f">=1.0: {int((df['place_roi']>=1.0).sum())}/{len(df)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
