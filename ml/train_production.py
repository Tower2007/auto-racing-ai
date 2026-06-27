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

# 教師データ定義バージョン。フィルタ条件や target の定義を変えたら上げる。
# AUC/best_iter の差が「モデル品質」か「教師データ定義差」かを見分けるため。
# v1: finished = order.notna() (DQ/落車を除外)
# v2: finished = 1 (DQ/落車を含む) — 2026-05-02 導入、05-14 revert で v1 に戻す
TARGET_DEFINITION_VERSION = 1


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


def train_full(X: pd.DataFrame, y: pd.Series, dates: pd.Series
               ) -> tuple[lgb.Booster, dict, pd.DataFrame, pd.Series]:
    """末尾 10% を val に切って early stopping。残りを train。

    品質ゲートの公平比較のため、val 集合 (X_va, y_va) も返す
    (現役モデルを同一 val で再採点して候補と比べる、2026-06-26)。
    """
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
    return model, metrics, X_va, y_va


def fit_calibration() -> tuple[IsotonicRegression, dict]:
    """walk-forward 予測(過去の honest 予測)から isotonic を fit。

    metrics は 2 系統に分離して記録(2026-05-06 audit 反映):
      - fit_data_diagnostic: OOF 全体で fit + 同じデータで eval(in-sample 診断値)。
        live 用 calibrator としては妥当だが honest 評価値ではないので診断扱い。
      - honest_split: test_month < CALIB_CUTOFF で fit、>= CALIB_CUTOFF で eval。
        校正性能の honest な評価指標。calib_auc/logloss を引用するならこちら。
    """
    pq = DATA / "walkforward_predictions_morning_top3.parquet"
    if not pq.exists():
        # fallback: 直前モデル
        pq = DATA / "walkforward_predictions_top3.parquet"
        logger.warning("morning preds not found, falling back to %s", pq.name)
    df = pd.read_parquet(pq)

    # live 用 calibrator(全 OOF で fit、本番予測時に呼ばれる側)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(df["pred"].values, df["target_top3"].values)
    p_calib = iso.transform(df["pred"].values)
    fit_data_diagnostic = {
        "n_calib": int(len(df)),
        "raw_auc": float(roc_auc_score(df["target_top3"], df["pred"])),
        "calib_auc": float(roc_auc_score(df["target_top3"], p_calib)),
        "raw_logloss": float(log_loss(df["target_top3"], df["pred"].clip(1e-6, 1-1e-6))),
        "calib_logloss": float(log_loss(df["target_top3"], p_calib.clip(1e-6, 1-1e-6))),
        "note": (
            "in-sample (fit data 上の診断値)。honest 評価値として引用しないこと。"
            "校正性能の評価指標は honest_split を見ること。"
        ),
    }

    # honest 評価(cutoff split)
    CALIB_CUTOFF = "2024-04"
    honest_split: dict = {"cutoff": CALIB_CUTOFF}
    try:
        calib_df = df[df["test_month"] < CALIB_CUTOFF]
        eval_df = df[df["test_month"] >= CALIB_CUTOFF]
        if len(calib_df) > 0 and len(eval_df) > 0:
            iso_honest = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            iso_honest.fit(calib_df["pred"].values, calib_df["target_top3"].values)
            p_eval_calib = iso_honest.transform(eval_df["pred"].values)
            honest_split.update({
                "n_calib": int(len(calib_df)),
                "n_eval": int(len(eval_df)),
                "raw_auc": float(roc_auc_score(eval_df["target_top3"], eval_df["pred"])),
                "calib_auc": float(roc_auc_score(eval_df["target_top3"], p_eval_calib)),
                "raw_logloss": float(log_loss(
                    eval_df["target_top3"], eval_df["pred"].clip(1e-6, 1-1e-6))),
                "calib_logloss": float(log_loss(
                    eval_df["target_top3"], p_eval_calib.clip(1e-6, 1-1e-6))),
                "note": (
                    f"honest: fit on test_month < {CALIB_CUTOFF}, "
                    f"eval on test_month >= {CALIB_CUTOFF}"
                ),
            })
        else:
            honest_split["error"] = (
                f"分割不能: calib n={len(calib_df)}, eval n={len(eval_df)}"
            )
    except Exception as e:
        honest_split["error"] = f"honest 分割計算失敗: {e}"

    metrics = {
        "fit_data_diagnostic": fit_data_diagnostic,
        "honest_split": honest_split,
        # 後方互換: 旧キー(in-sample 値)を残しつつ、deprecated note を付ける
        "n_calib": fit_data_diagnostic["n_calib"],
        "raw_auc": fit_data_diagnostic["raw_auc"],
        "calib_auc": fit_data_diagnostic["calib_auc"],
        "raw_logloss": fit_data_diagnostic["raw_logloss"],
        "calib_logloss": fit_data_diagnostic["calib_logloss"],
        "_legacy_keys_note": (
            "n_calib/raw_auc/calib_auc/raw_logloss/calib_logloss は in-sample 値で "
            "後方互換のため残置。新しいコードは fit_data_diagnostic か honest_split を参照。"
        ),
    }
    return iso, metrics


def _load_current_meta() -> dict | None:
    """現行モデルの meta を読み込む。なければ None。"""
    meta_path = DATA / "production_meta.json"
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _append_retrain_history(verdict: str, reason: str, metrics: dict) -> None:
    """再学習結果を data/retrain_history.csv に追記。

    weekly_status の塩漬けリスク監視 (Antigravity 2026-05-23 提案) で
    連続却下回数を集計するために OK/WARN/NG 全件を蓄積する。
    """
    import csv
    history_path = DATA / "retrain_history.csv"
    header = ["timestamp", "verdict", "reason",
              "valid_auc", "valid_logloss", "best_iteration",
              "target_definition_version"]
    row = [
        datetime.now().isoformat(timespec="seconds"),
        verdict,
        reason,
        metrics.get("valid_auc", ""),
        metrics.get("valid_logloss", ""),
        metrics.get("best_iteration", ""),
        TARGET_DEFINITION_VERSION,
    ]
    try:
        new_file = not history_path.exists()
        with open(history_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(header)
            writer.writerow(row)
    except Exception as e:
        logger.warning("retrain_history.csv 追記失敗: %s", e)


# 品質ゲート パラメータ (2026-06-26 再設計)。
# 旧設計は「凍結した高値 valid_auc(0.8266) を恒久ベースラインに、候補ごとに別の
# val 窓で測った AUC を引き算」していたため、別データ上の比較が不公平 + 永久凍結
# (5/24〜6/14 の4回が同一データで NG 連発、モデル 4/29 塩漬け) だった。
# 新設計: 現役モデルを **候補と同一の val 集合で再採点** して公平比較し、
#   許容バンド + 鮮度オーバーライドで「更新すべき時に更新できる」ようにする。
ADOPT_TOL_AUC = 0.003    # 同一val で現役-この値 以内なら採用 (ラン間ノイズ吸収)
STALE_DAYS = 35          # 現役がこの日数超 → 鮮度オーバーライド発動
STALE_TOL_AUC = 0.010    # 鮮度オーバーライド時の許容 (古いモデルは多少劣っても更新)


def _model_age_days(old_meta: dict | None) -> int | None:
    """現役モデルの学習からの経過日数。"""
    if not old_meta:
        return None
    ts = old_meta.get("trained_at")
    if not ts:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(ts)).days
    except Exception:
        return None


def _incumbent_auc_on_val(X_va: pd.DataFrame, y_va: pd.Series,
                          old_meta: dict | None) -> float | None:
    """現役 production_model.lgb を候補と同一の val 集合で採点し AUC を返す。

    窓ズレのない公平比較のため。特徴量列は old_meta の feature_columns に揃える。
    再採点不能 (モデル無し / 列不一致 / 例外) なら None。
    """
    model_path = DATA / "production_model.lgb"
    if old_meta is None or not model_path.exists():
        return None
    try:
        booster = lgb.Booster(model_file=str(model_path))
        cols = old_meta.get("feature_columns")
        if cols and all(c in X_va.columns for c in cols):
            Xv = X_va[cols]
        else:
            Xv = X_va  # 列不一致時は素のまま (名前で解決)
        p = booster.predict(Xv)
        return float(roc_auc_score(y_va, p))
    except Exception as e:
        logger.warning("現役モデルの val 再採点に失敗 (fallback): %s", e)
        return None


def _should_adopt(new_metrics: dict, old_meta: dict | None,
                  incumbent_auc_same_val: float | None,
                  model_age_days: int | None,
                  force: bool = False) -> tuple[str, str]:
    """新モデルを採用すべきか判定 (2026-06-26 公平比較版)。

    NG=見送り / WARN=採用するが警告 / OK=採用。
      1. 旧モデルなし / --force → OK
      2. target_definition_version 変更 → WARN
      3. best_iteration が旧の 40% 未満 → NG (degenerate 学習の sanity)
      4. 主判定 (同一val AUC):
         - 候補 >= 現役 - ADOPT_TOL_AUC → OK
         - 上記外でも 現役が STALE_DAYS 超 かつ 候補 >= 現役 - STALE_TOL_AUC
           → WARN (鮮度オーバーライド: 多少劣っても鮮度優先で採用)
         - それ以外 → NG
         - 再採点不能なら record 値比較に fallback (許容バンド付き)
    """
    if old_meta is None:
        return "OK", "旧モデルなし、初回採用"
    if force:
        return "OK", "--force 指定、強制採用"

    old_tdv = old_meta.get("target_definition_version")
    if old_tdv is not None and old_tdv != TARGET_DEFINITION_VERSION:
        return "WARN", (
            f"target_definition_version 変更: {old_tdv} -> {TARGET_DEFINITION_VERSION}. "
            "教師データ定義差の可能性あり、採用するが要確認"
        )

    new_auc = new_metrics.get("valid_auc", 0)
    new_best_iter = new_metrics.get("best_iteration", 0)
    old_best_iter = old_meta.get("train_metrics", {}).get("best_iteration", 0)

    # sanity: degenerate 学習 (best_iter 激減) は無条件 NG
    if old_best_iter > 0 and new_best_iter < old_best_iter * 0.4:
        return "NG", (
            f"best_iteration 激減: {old_best_iter} -> {new_best_iter} "
            f"({new_best_iter / old_best_iter * 100:.0f}%、閾値40% degenerate疑い)"
        )

    age = model_age_days if model_age_days is not None else 0

    # 主判定: 同一 val 上の公平比較
    if incumbent_auc_same_val is not None:
        diff = new_auc - incumbent_auc_same_val
        if diff >= -ADOPT_TOL_AUC:
            return "OK", (
                f"同一val 公平比較: 候補{new_auc:.4f} vs 現役{incumbent_auc_same_val:.4f} "
                f"(diff={diff:+.4f}, 許容-{ADOPT_TOL_AUC})"
            )
        if age >= STALE_DAYS and diff >= -STALE_TOL_AUC:
            return "WARN", (
                f"鮮度オーバーライド採用: 現役{age}日経過, 同一val diff={diff:+.4f} "
                f"(許容-{STALE_TOL_AUC})。鮮度優先で更新"
            )
        return "NG", (
            f"同一val AUC 低下: 候補{new_auc:.4f} vs 現役{incumbent_auc_same_val:.4f} "
            f"(diff={diff:+.4f}, 許容-{ADOPT_TOL_AUC}, 現役{age}日)"
        )

    # fallback: 再採点不能 → record 値比較 (許容バンド付き、旧来より緩い)
    old_auc = old_meta.get("train_metrics", {}).get("valid_auc", 0)
    diff = new_auc - old_auc
    if diff >= -ADOPT_TOL_AUC:
        return "OK", (
            f"(fallback record比較) 候補{new_auc:.4f} vs 現役記録{old_auc:.4f} "
            f"(diff={diff:+.4f}, 許容-{ADOPT_TOL_AUC})"
        )
    if age >= STALE_DAYS and diff >= -STALE_TOL_AUC:
        return "WARN", (
            f"(fallback) 鮮度オーバーライド: 現役{age}日, diff={diff:+.4f}"
        )
    return "NG", (
        f"(fallback record比較) valid_auc 低下: {old_auc:.4f} -> {new_auc:.4f} "
        f"(diff={diff:+.4f}, 許容-{ADOPT_TOL_AUC})"
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="target_top3", choices=["target_top3", "target_win"])
    p.add_argument("--force", action="store_true",
                   help="品質ゲートを無視して強制的に新モデルを採用")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    df = load_features()
    X, y, df_kept, feature_cols = prepare(df, args.target)
    logger.info("Total kept: %s rows × %d features", f"{len(df_kept):,}", X.shape[1])

    model, train_metrics, X_va, y_va = train_full(X, y, df_kept["race_date"])
    iso, calib_metrics = fit_calibration()

    # 品質ゲート: 現役モデルを同一 val で再採点して公平比較 (2026-06-26)
    old_meta = _load_current_meta()
    incumbent_auc = _incumbent_auc_on_val(X_va, y_va, old_meta)
    age_days = _model_age_days(old_meta)
    if incumbent_auc is not None:
        logger.info("現役モデル 同一val 再採点 AUC=%.4f (候補=%.4f, 現役%s日経過)",
                    incumbent_auc, train_metrics["valid_auc"],
                    age_days if age_days is not None else "?")
    verdict, reason = _should_adopt(
        train_metrics, old_meta, incumbent_auc, age_days, force=args.force)

    # 学習履歴を append (OK/WARN/NG 全件、塩漬け監視用)
    _append_retrain_history(verdict, reason, train_metrics)

    if verdict == "NG":
        logger.warning("=" * 60)
        logger.warning("新モデル採用見送り (NG): %s", reason)
        logger.warning("旧モデルを維持します。--force で強制採用可。")
        logger.warning("=" * 60)
        rejected_path = DATA / "production_meta.rejected.json"
        rejected = {
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "verdict": verdict,
            "rejected_reason": reason,
            "train_metrics": train_metrics,
        }
        with open(rejected_path, "w", encoding="utf-8") as f:
            json.dump(rejected, f, ensure_ascii=False, indent=2)
        logger.info("見送りメタ保存: %s", rejected_path.name)
        return

    if verdict == "WARN":
        logger.warning("品質ゲート WARN (採用するが要注意): %s", reason)
    else:
        logger.info("品質ゲート通過 (OK): %s", reason)

    # 旧モデルの meta をバックアップ
    model_path = DATA / "production_model.lgb"
    calib_path = DATA / "production_calib.pkl"
    meta_path = DATA / "production_meta.json"
    prev_meta_path = DATA / "production_meta.prev.json"

    if meta_path.exists():
        import shutil
        shutil.copy2(meta_path, prev_meta_path)

    model.save_model(str(model_path))
    with open(calib_path, "wb") as f:
        pickle.dump(iso, f)

    meta = {
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "target": args.target,
        "target_definition_version": TARGET_DEFINITION_VERSION,
        "n_features": int(X.shape[1]),
        "feature_columns": feature_cols,
        "categorical": [c for c in CATEGORICAL if c in feature_cols],
        "data_date_range": {
            "min": df_kept["race_date"].min().date().isoformat(),
            "max": df_kept["race_date"].max().date().isoformat(),
        },
        "train_metrics": train_metrics,
        "calib_metrics": calib_metrics,
        "quality_gate_verdict": verdict,
        "adoption_reason": reason,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    logger.info("Saved: %s, %s, %s", model_path.name, calib_path.name, meta_path.name)
    honest = calib_metrics.get("honest_split", {})
    if "calib_auc" in honest:
        logger.info(
            "Train AUC=%.4f, Valid AUC=%.4f, Calib AUC (honest)=%.4f / (in-sample)=%.4f",
            train_metrics["train_auc"], train_metrics["valid_auc"],
            honest["calib_auc"], calib_metrics["fit_data_diagnostic"]["calib_auc"],
        )
    else:
        logger.info(
            "Train AUC=%.4f, Valid AUC=%.4f, Calib AUC (in-sample only)=%.4f",
            train_metrics["train_auc"], train_metrics["valid_auc"],
            calib_metrics["fit_data_diagnostic"]["calib_auc"],
        )


if __name__ == "__main__":
    main()
