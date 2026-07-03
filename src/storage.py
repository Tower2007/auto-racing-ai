"""CSV ファイル読み書きモジュール

data/ 配下に CSV を蓄積。ヘッダ自動付与、追記モード。
"""

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# CSV ファイル名 → カラム定義
CSV_SCHEMAS: dict[str, list[str]] = {
    "race_entries.csv": [
        "race_date", "place_code", "race_no", "car_no",
        "player_code", "player_name", "player_place_code", "player_place_name",
        "graduation_code", "age", "bike_class", "bike_name", "rank",
        "handicap", "trial_run_time", "trial_retry_code", "absent",
        "sunny_expect_code", "rain_expect_code", "race_dev", "rate2", "rate3",
    ],
    "race_stats.csv": [
        "race_date", "place_code", "race_no", "car_no", "player_code",
        "run_count_90d", "advance_final_count_90d", "win_count_90d", "st_ave_90d",
        "order1_count_90d", "order2_count_90d", "order3_count_90d", "order_other_count_90d",
        "good_track_trial_ave", "good_track_race_ave", "good_track_race_best",
        "good_track_race_best_place",
        "good_track_rate2_180d", "good_track_run_count_180d",
        "wet_track_rate2_180d", "wet_track_run_count_180d",
        "this_year_win_count", "this_year_advance_final", "total_win_count",
        "win_rate1", "win_rate2", "win_rate3",
    ],
    "race_results.csv": [
        "race_date", "place_code", "race_no", "car_no",
        "order", "accident_code", "accident_name",
        "player_code", "player_name", "motorcycle_name",
        "handicap", "trial_time", "race_time", "st", "foul_code",
    ],
    "race_laps.csv": [
        "race_date", "place_code", "race_no", "lap_no", "car_no", "rank",
    ],
    "payouts.csv": [
        "race_date", "place_code", "race_no",
        "bet_type", "bet_name",
        "car_no_1", "car_no_2", "car_no_3",
        "refund", "pop", "refund_votes",
    ],
    "odds_summary.csv": [
        "race_date", "place_code", "race_no", "car_no", "player_code",
        "win_odds", "place_odds_min", "place_odds_max",
        "st_ave", "good_track_trial_ave", "good_track_race_ave",
        "good_track_race_best", "ai_expect_code",
    ],
    # 連勝式オッズ (2連単/2連複/ワイド/3連単/3連複)。
    # wid のみ odds_min/odds_max、他は odds に値が入る。約500行/レース。
    "odds_combo.csv": [
        "race_date", "place_code", "race_no", "bet_type",
        "car_no_1", "car_no_2", "car_no_3",
        "odds", "odds_min", "odds_max",
    ],
    # 発火時 (発走約4分前) の連勝式オッズ板スナップショット。closing との drift 検証用
    "odds_combo_snapshots.csv": [
        "race_date", "place_code", "race_no", "bet_type",
        "car_no_1", "car_no_2", "car_no_3",
        "odds", "odds_min", "odds_max", "captured_at",
    ],
    # 直前オッズ 2 時点スナップショット (odds_prerace_daemon.py が T-5/T-1 分前に記録)。
    # スキーマ = odds_combo_snapshots.csv + target_offset_min (5 or 1)。
    # bet_type は combo 5 券種 (rtw/rfw/wid/rt3/rf3) に加えて
    # tns (単勝: car_no_1 + odds) / fns (複勝: car_no_1 + odds_min/odds_max) も縦持ち。
    "odds_combo_prerace.csv": [
        "race_date", "place_code", "race_no", "bet_type",
        "car_no_1", "car_no_2", "car_no_3",
        "odds", "odds_min", "odds_max", "captured_at", "target_offset_min",
    ],
    # 発火時の気象 (Hold/Today はリアルタイムのみで遡及不能)。
    # roadtemp=走路温度 (タイヤグリップに直結するオート固有の重要特徴量候補)
    "weather_snapshots.csv": [
        "race_date", "place_code", "race_no",
        "temp", "humid", "roadtemp", "weather", "weather_code", "situation_code",
        "captured_at",
    ],
}


def _ensure_header(path: Path, columns: list[str]) -> None:
    """ファイルが無い or 空なら、ヘッダ行を書き込む。"""
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()


def append_rows(csv_name: str, rows: list[dict]) -> int:
    """CSV に行を追記。戻り値は追記した行数。"""
    if not rows:
        return 0

    if csv_name not in CSV_SCHEMAS:
        raise ValueError(f"Unknown CSV: {csv_name}")

    columns = CSV_SCHEMAS[csv_name]
    path = DATA_DIR / csv_name
    _ensure_header(path, columns)

    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writerows(rows)

    logger.debug("Appended %d rows to %s", len(rows), path)
    return len(rows)


def read_csv(csv_name: str) -> list[dict]:
    """CSV を全行読み込み。ファイルが無ければ空リスト。"""
    path = DATA_DIR / csv_name
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def row_count(csv_name: str) -> int:
    """CSV の行数（ヘッダ除く）。"""
    path = DATA_DIR / csv_name
    if not path.exists():
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for _ in f) - 1  # ヘッダ分を引く


def has_race_day(csv_name: str, place_code: int, race_date: str) -> bool:
    """指定の race-day が既に CSV に存在するか（重複投入防止）。"""
    path = DATA_DIR / csv_name
    if not path.exists():
        return False
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("place_code") == str(place_code) and row.get("race_date") == race_date:
                return True
    return False
