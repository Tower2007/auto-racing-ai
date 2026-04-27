"""(A.2) odds_summary.csv の win_odds == 0 を NULL に置換。

HANDOFF doc 記載の通り、autorace.jp は 100倍超の超大穴オッズを 0.0 として
HTML 表示する。本来 NULL とすべきデータ。バックフィル時の parser で
吸収できなかった 35 件を後処理で修正する。

place_odds_min / place_odds_max も同じ行で同一現象が起きていれば NULL 化する。
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TARGET = DATA_DIR / "odds_summary.csv"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    if not TARGET.exists():
        logger.error("File not found: %s", TARGET)
        return

    # バックアップ作成
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = TARGET.with_suffix(f".csv.bak_{timestamp}")
    shutil.copy2(TARGET, backup)
    logger.info("Backup created: %s", backup.name)

    df = pd.read_csv(TARGET, dtype={"player_code": str}, low_memory=False)
    n_before = (df["win_odds"] == 0).sum()
    n_place_min_zero = (df["place_odds_min"] == 0).sum()
    n_place_max_zero = (df["place_odds_max"] == 0).sum()

    logger.info("Before: win_odds == 0: %d", n_before)
    logger.info("Before: place_odds_min == 0: %d", n_place_min_zero)
    logger.info("Before: place_odds_max == 0: %d", n_place_max_zero)

    if n_before == 0:
        logger.info("No win_odds == 0 found — already clean. Removing backup.")
        backup.unlink()
        return

    # 0 → NaN (pandas が NULL として CSV 出力する)
    df.loc[df["win_odds"] == 0, "win_odds"] = pd.NA
    df.loc[df["place_odds_min"] == 0, "place_odds_min"] = pd.NA
    df.loc[df["place_odds_max"] == 0, "place_odds_max"] = pd.NA

    # 念のため対象行をログに
    affected = df[df["win_odds"].isna() & ~df["win_odds"].notna()]  # all NaN
    # → simpler: count zeros became NaN
    n_after = (df["win_odds"] == 0).sum()
    n_null = df["win_odds"].isna().sum()
    logger.info("After: win_odds == 0: %d (should be 0)", n_after)
    logger.info("After: win_odds NULL count: %d", n_null)

    df.to_csv(TARGET, index=False)
    logger.info("Wrote %s", TARGET)


if __name__ == "__main__":
    main()
