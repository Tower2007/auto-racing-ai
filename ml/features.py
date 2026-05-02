"""特徴量データセット構築

6 つの CSV を join して 1 行 = (race, car_no) の特徴量 dataframe を作る。
リーク防止のため race_results は target 列の生成にのみ使用。

出力: data/ml_features.parquet
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT = DATA_DIR / "ml_features.parquet"

logger = logging.getLogger(__name__)

RACE_KEY = ["race_date", "place_code", "race_no"]
CAR_KEY = RACE_KEY + ["car_no"]


def _load() -> dict[str, pd.DataFrame]:
    out = {}
    for name in ["race_entries", "race_stats", "odds_summary", "race_results"]:
        path = DATA_DIR / f"{name}.csv"
        df = pd.read_csv(path, dtype={"player_code": str}, low_memory=False)
        df["race_date"] = pd.to_datetime(df["race_date"])
        out[name] = df
        logger.info("Loaded %s: %s rows", name, f"{len(df):,}")
    return out


def _engineer_entries(entries: pd.DataFrame) -> pd.DataFrame:
    df = entries.copy()

    # rank: "S-12" / "A-116" / "B-22" → 上位ランクと内部順位に分解
    rank_split = df["rank"].fillna("X-0").str.split("-", n=1, expand=True)
    df["rank_class"] = rank_split[0]  # S/A/B
    df["rank_num"] = pd.to_numeric(rank_split[1], errors="coerce")

    # race_dev は "062" のような3桁文字列 (偏差). 数値化
    df["race_dev_num"] = pd.to_numeric(df["race_dev"], errors="coerce")

    # absent: NULL なら出走、非NULL なら欠車。バイナリ化
    df["is_absent"] = df["absent"].notna().astype(int)

    # 試走未実施は trial_run_time NULL に集約
    df["has_trial"] = df["trial_run_time"].notna().astype(int)

    return df


def _engineer_race_context(df: pd.DataFrame) -> pd.DataFrame:
    """レース全体の文脈特徴(各車に共通)を追加"""
    # 同レース内の集約: ハンデ最大・試走T最小/平均 等
    grp = df.groupby(RACE_KEY)
    df["race_handicap_max"] = grp["handicap"].transform("max")
    df["race_handicap_min"] = grp["handicap"].transform("min")
    df["race_trial_min"] = grp["trial_run_time"].transform("min")
    df["race_trial_mean"] = grp["trial_run_time"].transform("mean")
    df["race_n_cars"] = grp["car_no"].transform("count")
    df["race_n_absent"] = grp["is_absent"].transform("sum")

    # 自車の相対値
    df["handicap_diff_min"] = df["handicap"] - df["race_handicap_min"]
    df["trial_diff_min"] = df["trial_run_time"] - df["race_trial_min"]
    df["trial_diff_mean"] = df["trial_run_time"] - df["race_trial_mean"]

    return df


def _engineer_odds(odds: pd.DataFrame) -> pd.DataFrame:
    df = odds.copy()
    # log オッズ (重い裾を圧縮)
    df["log_win_odds"] = np.log1p(df["win_odds"])
    df["log_place_odds_min"] = np.log1p(df["place_odds_min"])
    df["log_place_odds_max"] = np.log1p(df["place_odds_max"])

    # 同レース内のオッズランク
    grp = df.groupby(RACE_KEY)
    df["win_odds_rank"] = grp["win_odds"].rank(method="min")
    df["place_odds_min_rank"] = grp["place_odds_min"].rank(method="min")

    # implied prob (オッズ逆数): 単純な確率推定
    df["win_implied_prob"] = 1.0 / df["win_odds"]
    return df


def _build_target(results: pd.DataFrame) -> pd.DataFrame:
    """race_results から binary target (top3) を作る。"""
    df = results[CAR_KEY + ["order"]].copy()
    df["target_top3"] = ((df["order"] >= 1) & (df["order"] <= 3)).astype(int)
    df["target_win"] = (df["order"] == 1).astype(int)
    # finished = race_results に行が存在する = 結果取得済(失格・落車も含む)。
    # parser が order>=9 を NULL 化するので order.notna() だと DQ/落車が学習から漏れる。
    df["finished"] = 1
    return df[CAR_KEY + ["target_top3", "target_win", "finished"]]


def build() -> pd.DataFrame:
    src = _load()
    entries = _engineer_entries(src["race_entries"])
    entries = _engineer_race_context(entries)
    odds = _engineer_odds(src["odds_summary"])
    target = _build_target(src["race_results"])

    # join: entries + stats + odds + target
    feat = entries.merge(
        src["race_stats"].drop(columns=["player_code"], errors="ignore"),
        on=CAR_KEY, how="left", validate="one_to_one",
    )
    # odds の good_track_* / st_ave は stats 側と重複するので落とす
    odds_cols_to_drop = [
        "player_code", "st_ave",
        "good_track_trial_ave", "good_track_race_ave", "good_track_race_best",
    ]
    feat = feat.merge(
        odds.drop(columns=[c for c in odds_cols_to_drop if c in odds.columns]),
        on=CAR_KEY, how="left", validate="one_to_one",
    )
    feat = feat.merge(target, on=CAR_KEY, how="left", validate="one_to_one")

    # 欠車 / 結果未取得は学習対象から除外する判断は呼び出し側に任せる
    # ただし target が NaN の行は残してフラグで判別可能にする
    feat["target_top3"] = feat["target_top3"].fillna(0).astype(int)
    feat["target_win"] = feat["target_win"].fillna(0).astype(int)
    feat["finished"] = feat["finished"].fillna(0).astype(int)

    # 時間特徴
    feat["year"] = feat["race_date"].dt.year
    feat["month"] = feat["race_date"].dt.month
    feat["dow"] = feat["race_date"].dt.dayofweek
    feat["year_month"] = feat["race_date"].dt.to_period("M").astype(str)

    logger.info("Built feature df: %s rows × %d cols", f"{len(feat):,}", feat.shape[1])
    return feat


def save(df: pd.DataFrame) -> Path:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUTPUT, index=False)
    logger.info("Saved %s (%.1f MB)", OUTPUT, OUTPUT.stat().st_size / 1024 / 1024)
    return OUTPUT


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    df = build()
    save(df)
    print()
    print("=== Feature columns ===")
    print(df.columns.tolist())
    print()
    print("=== Target distribution ===")
    print(df["target_top3"].value_counts(normalize=True))
    print()
    print(f"=== Coverage by year_month (head) ===")
    print(df.groupby("year_month").size().head(15))
