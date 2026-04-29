"""前日予想モデル: 試走タイム・オッズ・直前情報を一切使わずに walk-forward

target_top3 を、出走表・過去成績・ランク・ハンデなど 「前日に取れる」 特徴量だけで予測。
予測結果を data/walkforward_predictions_preday_top3.parquet に保存し、
後段の EV 計算は実払戻データを使って評価する。
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

logger = logging.getLogger(__name__)

# --- 前日に取れない特徴(除外) ---
PREDAY_EXCLUDE = {
    # 試走系
    "trial_run_time", "trial_retry_code", "has_trial",
    "trial_diff_min", "trial_diff_mean",
    "race_trial_min", "race_trial_mean",
    # オッズ系
    "win_odds", "log_win_odds", "win_odds_rank", "win_implied_prob",
    "place_odds_min", "place_odds_max",
    "log_place_odds_min", "log_place_odds_max",
    "place_odds_min_rank",
    # 試走後 AI 予想印
    "ai_expect_code",
}

# --- 元の EXCLUDE(ID系・テキスト・target)+ pre-day で除外する分 ---
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


def load() -> pd.DataFrame:
    df = pd.read_parquet(DATA / "ml_features.parquet")
    df["race_date"] = pd.to_datetime(df["race_date"])
    return df


def prepare(df: pd.DataFrame, target_col: str) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    mask = (df["is_absent"] == 0) & (df["finished"] == 1)
    df = df[mask].copy()

    exclude = BASE_EXCLUDE | PREDAY_EXCLUDE
    feature_cols = [c for c in df.columns if c not in exclude]
    X = df[feature_cols].copy()

    for c in CATEGORICAL:
        if c in X.columns:
            X[c] = X[c].astype("category")
    for c in X.select_dtypes(include="object").columns:
        X[c] = X[c].astype("category")

    y = df[target_col].astype(int)
    logger.info("Pre-day feature count: %d (excluded %d)", X.shape[1], len(PREDAY_EXCLUDE))
    return X, y, df


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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", choices=["top3", "win"], default="top3")
    p.add_argument("--min-train-months", type=int, default=12)
    args = p.parse_args()

    target_col = f"target_{args.target}"
    df = load()
    X, y, df_kept = prepare(df, target_col)

    months = sorted(df_kept["year_month"].unique())
    test_months = months[args.min_train_months:]
    logger.info("Test months=%d (%s 〜 %s)", len(test_months), test_months[0], test_months[-1])

    pred_frames = []
    for i, tm in enumerate(test_months, 1):
        is_train_full = df_kept["year_month"] < tm
        is_test = df_kept["year_month"] == tm
        X_tr_full = X.loc[is_train_full.values]
        y_tr_full = y.loc[is_train_full.values]
        train_dates = df_kept.loc[is_train_full, "race_date"]
        cutoff = train_dates.quantile(0.9)
        is_val = train_dates >= cutoff
        X_tr = X_tr_full[~is_val.values]
        X_va = X_tr_full[is_val.values]
        y_tr = y_tr_full[~is_val.values]
        y_va = y_tr_full[is_val.values]

        model = _train_silent(X_tr, y_tr, X_va, y_va)

        X_te = X.loc[is_test.values]
        y_te = y.loc[is_test.values]
        df_te = df_kept.loc[is_test, ["race_date", "place_code", "race_no", "car_no", "target_top3"]].copy()
        from sklearn.metrics import roc_auc_score, log_loss
        p_te = model.predict(X_te, num_iteration=model.best_iteration)
        auc = roc_auc_score(y_te, p_te)
        ll = log_loss(y_te, p_te)
        df_te["pred"] = p_te
        df_te["test_month"] = tm
        pred_frames.append(df_te)
        logger.info("[%2d/%d] %s | n=%5d auc=%.4f logloss=%.4f best_iter=%d",
                    i, len(test_months), tm, len(X_te), auc, ll, model.best_iteration)

    pred_df = pd.concat(pred_frames, ignore_index=True)
    out = DATA / f"walkforward_predictions_preday_{args.target}.parquet"
    pred_df.to_parquet(out, index=False)
    logger.info("Saved %s (%s rows)", out, f"{len(pred_df):,}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
