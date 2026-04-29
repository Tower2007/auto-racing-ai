"""本番用 中間モデル(試走なし・オッズあり)+ isotonic 校正の学習スクリプト

学習方針:
- 中間モデル: 試走関連 5 列を除外、その他は全て使用
- 学習データ: ml_features.parquet 全体(walk-forward と違い、単一モデルを fit)
  ただし最新月 1 ヶ月分はサンプル偏り防止で除外可(オプション)
- 検証: 末尾 10% を validation にして early stopping
- 校正: walk-forward 予測 (walkforward_predictions_morning_top3.parquet) で
  honest な (pred, target) を取得し、isotonic regression で fit

出力:
- data/production_model.lgb : LightGBM Booster
- data/production_calib.pkl : IsotonicRegression
- data/production_meta.json : メタデータ(学習日・特徴量・metrics)

月 1 回 task scheduler から呼ぶ想定。
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score, log_loss

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

logger = logging.getLogger(__name__)

# 中間モデル: 試走関連だけ除外、オッズ系は使う
MORNING_EXCLUDE = {
    "trial_run_time", "trial_retry_code", "has_trial",
    "trial_diff_min", "trial_diff_mean",
    "race_trial_min", "race_trial_mean",
    "ai_expect_code",
}

BASE_EXCLUDE = {
    "race_date", "year_month",
    "player_code", "player_name", "bike_name", "rank",
    "absent", "trial_retry_code", "race_dev",
    "good_track_race_best_place",
    "target_top3", "target_win", "finished",
    "order",
}

CATEGORICAL = [
    "place_code", "race_no", "car_no", "player_place_code",
    "graduation_code", "bike_class", "rank_class",
    "sunny_expect_code", "rain_expect_code",
    "year", "month", "dow",
]


def load_features() -> pd.DataFrame:
    df = pd.read_parquet(DATA / "ml_features.parquet")
    df["race_date"] = pd.to_datetime(df["race_date"])
    return df


def prepare(df: pd.DataFrame, target_col: str = "target_top3"):
    mask = (df["is_absent"] == 0) & (df["finished"] == 1)
    df = df[mask].copy()
    exclude = BASE_EXCLUDE | MORNING_EXCLUDE
    feature_cols = sorted(c for c in df.columns if c not in exclude)
    X = df[feature_cols].copy()
    for c in CATEGORICAL:
        if c in X.columns:
            X[c] = X[c].astype("category")
    for c in X.select_dtypes(include=["object", "string"]).columns:
        X[c] = X[c].astype("category")
    y = df[target_col].astype(int)
    return X, y, df, feature_cols


def train_full(X: pd.DataFrame, y: pd.Series, dates: pd.Series) -> tuple[lgb.Booster, dict]:
    """末尾 10% を val に切って early stopping。残りを train。"""
    cutoff = dates.quantile(0.9)
    is_val = dates >= cutoff
    X_tr, X_va = X[~is_val.values], X[is_val.values]
    y_tr, y_va = y[~is_val.values], y[is_val.values]
    logger.info("Train: %s, Val: %s", f"{len(X_tr):,}", f"{len(X_va):,}")

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
    model = lgb.train(
        params, train_data, num_boost_round=2000,
        valid_sets=[train_data, valid_data], valid_names=["train", "valid"],
        callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False),
                   lgb.log_evaluation(period=200)],
    )
    p_tr = model.predict(X_tr, num_iteration=model.best_iteration)
    p_va = model.predict(X_va, num_iteration=model.best_iteration)
    metrics = {
        "n_train": int(len(X_tr)), "n_val": int(len(X_va)),
        "best_iteration": int(model.best_iteration),
        "train_auc": float(roc_auc_score(y_tr, p_tr)),
        "valid_auc": float(roc_auc_score(y_va, p_va)),
        "valid_logloss": float(log_loss(y_va, p_va)),
    }
    return model, metrics


def fit_calibration() -> tuple[IsotonicRegression, dict]:
    """walk-forward 予測(過去の honest 予測)から isotonic を fit。"""
    pq = DATA / "walkforward_predictions_morning_top3.parquet"
    if not pq.exists():
        # fallback: 直前モデル
        pq = DATA / "walkforward_predictions_top3.parquet"
        logger.warning("morning preds not found, falling back to %s", pq.name)
    df = pd.read_parquet(pq)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(df["pred"].values, df["target_top3"].values)
    p_calib = iso.transform(df["pred"].values)
    metrics = {
        "n_calib": int(len(df)),
        "raw_auc": float(roc_auc_score(df["target_top3"], df["pred"])),
        "calib_auc": float(roc_auc_score(df["target_top3"], p_calib)),
        "raw_logloss": float(log_loss(df["target_top3"], df["pred"].clip(1e-6, 1-1e-6))),
        "calib_logloss": float(log_loss(df["target_top3"], p_calib.clip(1e-6, 1-1e-6))),
    }
    return iso, metrics


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="target_top3", choices=["target_top3", "target_win"])
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    df = load_features()
    X, y, df_kept, feature_cols = prepare(df, args.target)
    logger.info("Total kept: %s rows × %d features", f"{len(df_kept):,}", X.shape[1])

    model, train_metrics = train_full(X, y, df_kept["race_date"])
    iso, calib_metrics = fit_calibration()

    # 保存
    model_path = DATA / "production_model.lgb"
    calib_path = DATA / "production_calib.pkl"
    meta_path = DATA / "production_meta.json"

    model.save_model(str(model_path))
    with open(calib_path, "wb") as f:
        pickle.dump(iso, f)

    meta = {
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "target": args.target,
        "n_features": int(X.shape[1]),
        "feature_columns": feature_cols,
        "categorical": [c for c in CATEGORICAL if c in feature_cols],
        "data_date_range": {
            "min": df_kept["race_date"].min().date().isoformat(),
            "max": df_kept["race_date"].max().date().isoformat(),
        },
        "train_metrics": train_metrics,
        "calib_metrics": calib_metrics,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    logger.info("Saved: %s, %s, %s", model_path.name, calib_path.name, meta_path.name)
    logger.info("Train AUC=%.4f, Valid AUC=%.4f, Calib AUC=%.4f",
                train_metrics["train_auc"], train_metrics["valid_auc"], calib_metrics["calib_auc"])


if __name__ == "__main__":
    main()
