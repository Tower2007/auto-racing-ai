"""LightGBM 学習スクリプト(初版・holdout 評価)

時系列ベースで分割して target_top3 を予測。
ベースライン感を掴むのが目的、walk-forward は別スクリプトに切り出す。

使い方: python -m ml.train [--target top3|win] [--test-months 6]
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from datetime import datetime

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (
    log_loss, roc_auc_score, brier_score_loss, average_precision_score,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "ml_features.parquet"
REPORTS = ROOT / "reports"

logger = logging.getLogger(__name__)

# 学習に使わない列(ID系・リーク・テキスト)
EXCLUDE_COLS = {
    "race_date", "year_month",
    "player_code", "player_name", "bike_name", "rank",
    "absent", "trial_retry_code", "race_dev",
    "good_track_race_best_place",  # 場名テキスト
    "target_top3", "target_win", "finished",
    "order",  # results 由来 (リーク防止)
}

# カテゴリ列(LightGBM に明示)
CATEGORICAL = [
    "place_code", "race_no", "car_no", "player_place_code",
    "graduation_code", "bike_class", "rank_class",
    "sunny_expect_code", "rain_expect_code", "ai_expect_code",
    "year", "month", "dow",
]


def load() -> pd.DataFrame:
    df = pd.read_parquet(DATA)
    df["race_date"] = pd.to_datetime(df["race_date"])
    return df


def prepare(df: pd.DataFrame, target_col: str) -> tuple[pd.DataFrame, pd.Series]:
    """訓練用に欠車・未結果行を除外、特徴量とターゲットを返す。"""
    mask = (df["is_absent"] == 0) & (df["finished"] == 1)
    df = df[mask].copy()
    logger.info("After absent/unfinished filter: %s rows (%.1f%% kept)",
                f"{len(df):,}", len(df) / mask.shape[0] * 100)

    feature_cols = [c for c in df.columns if c not in EXCLUDE_COLS]
    X = df[feature_cols].copy()

    # カテゴリ列を category dtype に
    for c in CATEGORICAL:
        if c in X.columns:
            X[c] = X[c].astype("category")

    # その他の object 列も category 化(欠損値含む文字列)
    for c in X.select_dtypes(include="object").columns:
        X[c] = X[c].astype("category")

    y = df[target_col].astype(int)
    return X, y, df  # df も返す(race_date 等が必要)


def time_split(df: pd.DataFrame, test_months: int) -> tuple[pd.Series, pd.Series]:
    cutoff = df["race_date"].max() - pd.DateOffset(months=test_months)
    is_train = df["race_date"] < cutoff
    is_test = df["race_date"] >= cutoff
    logger.info("Split: train < %s (%s), test >= %s (%s)",
                cutoff.date(), f"{is_train.sum():,}", cutoff.date(), f"{is_test.sum():,}")
    return is_train, is_test


def train_lgb(
    X_tr: pd.DataFrame, y_tr: pd.Series,
    X_va: pd.DataFrame, y_va: pd.Series,
) -> lgb.Booster:
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
    booster = lgb.train(
        params,
        train_data,
        num_boost_round=2000,
        valid_sets=[train_data, valid_data],
        valid_names=["train", "valid"],
        callbacks=[
            lgb.early_stopping(stopping_rounds=50),
            lgb.log_evaluation(period=100),
        ],
    )
    return booster


def evaluate(model: lgb.Booster, X_te: pd.DataFrame, y_te: pd.Series) -> dict:
    p = model.predict(X_te, num_iteration=model.best_iteration)
    return {
        "n": int(len(y_te)),
        "positive_rate": float(y_te.mean()),
        "logloss": float(log_loss(y_te, p)),
        "auc": float(roc_auc_score(y_te, p)),
        "brier": float(brier_score_loss(y_te, p)),
        "ap": float(average_precision_score(y_te, p)),
    }, p


def compute_roi(
    df_te: pd.DataFrame, p: np.ndarray, payouts: pd.DataFrame,
) -> dict:
    """単勝 / 複勝 / 連対 系の ROI を 100 円単位で評価。

    各レースで pred 最大の car を 1 点買い (100 円)。
    """
    df = df_te[["race_date", "place_code", "race_no", "car_no"]].copy()
    df["pred"] = p
    picks = df.loc[df.groupby(["race_date", "place_code", "race_no"])["pred"].idxmax()]

    tns = payouts[payouts["bet_type"] == "tns"][["race_date", "place_code", "race_no", "car_no_1", "refund"]]
    fns = payouts[payouts["bet_type"] == "fns"][["race_date", "place_code", "race_no", "car_no_1", "refund"]]
    rfw = payouts[payouts["bet_type"] == "rfw"]  # 2連複(参考)

    # 単勝
    win = picks.merge(tns, on=["race_date", "place_code", "race_no"], how="left")
    win["hit"] = (win["car_no"] == win["car_no_1"]).astype(int)
    win["payout"] = np.where(win["hit"] == 1, win["refund"], 0)

    # 複勝(top-1 prob を 100 円で複勝)
    place = picks.merge(
        fns, left_on=["race_date", "place_code", "race_no", "car_no"],
        right_on=["race_date", "place_code", "race_no", "car_no_1"], how="left",
    )
    place["hit"] = place["refund"].notna().astype(int)
    place["payout"] = place["refund"].fillna(0)

    return {
        "win_n_bets": int(len(win)),
        "win_hit_rate": float(win["hit"].mean()),
        "win_roi": float(win["payout"].sum() / (len(win) * 100)),
        "place_n_bets": int(len(place)),
        "place_hit_rate": float(place["hit"].mean()),
        "place_roi": float(place["payout"].sum() / (len(place) * 100)),
    }


def race_level_topk_acc(df_te: pd.DataFrame, p: np.ndarray, k: int = 3) -> float:
    """各レース内で上位k件の予測が実際 top3 だった精度。"""
    df = df_te.copy()
    df["pred"] = p
    grp = df.groupby(["race_date", "place_code", "race_no"])
    correct = 0
    total = 0
    for _, g in grp:
        if len(g) < 4:
            continue
        topk = g.nlargest(k, "pred")
        correct += topk["target_top3"].sum()
        total += k
    return correct / total if total else 0.0


def feature_importance(model: lgb.Booster, top: int = 25) -> pd.DataFrame:
    imp = pd.DataFrame({
        "feature": model.feature_name(),
        "gain": model.feature_importance("gain"),
        "split": model.feature_importance("split"),
    }).sort_values("gain", ascending=False).reset_index(drop=True)
    return imp.head(top)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["top3", "win"], default="top3")
    parser.add_argument("--test-months", type=int, default=6)
    args = parser.parse_args()

    target_col = f"target_{args.target}"
    df = load()

    X, y, df_kept = prepare(df, target_col)
    is_train, is_test = time_split(df_kept, args.test_months)

    X_tr, X_te = X.loc[is_train.values], X.loc[is_test.values]
    y_tr, y_te = y.loc[is_train.values], y.loc[is_test.values]

    # train 内から validation を時系列末尾10%で切る
    cutoff = df_kept.loc[is_train].index
    train_dates = df_kept.loc[is_train, "race_date"]
    val_cutoff = train_dates.quantile(0.9)
    is_val_in_train = train_dates >= val_cutoff
    X_tr2 = X_tr[~is_val_in_train.values]
    X_va = X_tr[is_val_in_train.values]
    y_tr2 = y_tr[~is_val_in_train.values]
    y_va = y_tr[is_val_in_train.values]
    logger.info("Inner split: train=%s val=%s", f"{len(X_tr2):,}", f"{len(X_va):,}")

    model = train_lgb(X_tr2, y_tr2, X_va, y_va)

    # 評価
    metrics_test, p_test = evaluate(model, X_te, y_te)
    df_test = df_kept.loc[is_test, ["race_date", "place_code", "race_no", "car_no", "target_top3"]].copy()
    metrics_test["topk_pick_acc"] = race_level_topk_acc(df_test, p_test, k=3)

    # ROI: 単勝 / 複勝
    payouts = pd.read_csv(ROOT / "data" / "payouts.csv", low_memory=False)
    payouts["race_date"] = pd.to_datetime(payouts["race_date"])
    roi = compute_roi(df_test, p_test, payouts)
    metrics_test.update(roi)

    # baseline: オッズ直接使った確率推定との比較
    odds_baseline_p = df_kept.loc[is_test, "win_implied_prob"].fillna(0).clip(0, 1).values
    # 単勝 implied prob は top1 の指標。top3 確率近似として log_place_odds を逆数化
    place_implied = 1.0 / df_kept.loc[is_test, "place_odds_min"].fillna(99.0)
    place_implied = place_implied.clip(0, 1).fillna(0)
    odds_metrics = {
        "logloss": float(log_loss(y_te, place_implied.clip(1e-6, 1-1e-6))),
        "auc": float(roc_auc_score(y_te, place_implied)),
    }

    print()
    print("=== Test metrics (target = %s) ===" % target_col)
    for k, v in metrics_test.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
    print()
    print("=== Odds-only baseline (place_odds_min reciprocal) ===")
    for k, v in odds_metrics.items():
        print(f"  {k}: {v:.4f}")
    print()
    print("=== Top 25 features by gain ===")
    print(feature_importance(model, 25).to_string(index=False))

    # save report
    REPORTS.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ml_holdout_{args.target}_{today}.md"
    lines = [
        f"# ML holdout 評価レポート ({today})",
        f"",
        f"- target: `{target_col}`",
        f"- test 期間: 末尾 {args.test_months} ヶ月 (cutoff = {df_kept.loc[is_train, 'race_date'].max().date()})",
        f"- train 行数: {len(X_tr2):,} / val: {len(X_va):,} / test: {len(X_te):,}",
        f"- best_iteration: {model.best_iteration}",
        f"",
        f"## Test metrics",
        f"",
        f"| metric | value |",
        f"|---|---:|",
    ]
    for k, v in metrics_test.items():
        lines.append(f"| {k} | {v:.4f} |" if isinstance(v, float) else f"| {k} | {v} |")
    lines += [
        f"",
        f"## Odds-only baseline",
        f"",
        f"| metric | value |",
        f"|---|---:|",
    ]
    for k, v in odds_metrics.items():
        lines.append(f"| {k} | {v:.4f} |")
    lines += [
        f"",
        f"## Top 25 features by gain",
        f"",
        feature_importance(model, 25).to_markdown(index=False),
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport saved to {out}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
