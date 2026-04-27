"""
日次データ収集スクリプト
-----------------------------------------------------------------------
Windows Task Scheduler から毎日呼ばれるバッチ。データ蓄積に専念し、
予想・メール送信はしない。

処理:
  1. 対象日(デフォルト: 昨日 + 一昨日)を決定
  2. 5 場 × 対象日数 で ingest_one_day() を呼ぶ
     - 既に投入済みの (place, date) は has_race_day で自動スキップ(冪等)
     - 開催無しの日は Program R1 1コールで判定して即終了
  3. ログを data/daily_ingest.log に追記

使い方:
  python daily_ingest.py                # default: --catchup 2 (昨日+一昨日)
  python daily_ingest.py --catchup 7    # 直近7日分
  python daily_ingest.py --date 2026-04-27  # 特定日のみ
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import traceback
from pathlib import Path

from src.client import AutoraceClient, VENUE_CODES
from ingest_day import ingest_one_day

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "data" / "daily_ingest.log"


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", type=str, default=None,
                   help="YYYY-MM-DD 形式の特定日。指定すれば catchup は無視。")
    p.add_argument("--catchup", type=int, default=2,
                   help="N 日前まで遡って未取得日を埋める (default: 2)")
    args = p.parse_args()

    setup_logging()
    logger = logging.getLogger("daily_ingest")

    # 対象日リスト
    if args.date:
        dates = [args.date]
    else:
        today = dt.date.today()
        # 1 日前 〜 N 日前
        dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(1, args.catchup + 1)]
        dates.reverse()  # 古い日から処理

    logger.info("=== daily_ingest start: dates=%s ===", dates)

    client = AutoraceClient()

    n_processed = 0
    n_no_race = 0
    n_skip = 0
    n_error = 0
    err_details: list[str] = []

    # 5 場 × 対象日
    for date in dates:
        for place_code in sorted(VENUE_CODES.keys()):
            venue = VENUE_CODES[place_code]
            try:
                counts = ingest_one_day(client, place_code, date)
                if counts.get("skipped"):
                    n_skip += 1
                elif not counts:
                    # 空 dict = 開催なし
                    n_no_race += 1
                else:
                    n_processed += 1
            except Exception as e:
                n_error += 1
                err_details.append(f"{date} {venue}({place_code}): {e}")
                logger.error("ERROR %s %s: %s", date, venue, e)
                logger.error(traceback.format_exc())

    logger.info(
        "=== daily_ingest done: processed=%d no_race=%d skip(already)=%d error=%d ===",
        n_processed, n_no_race, n_skip, n_error,
    )
    if err_details:
        logger.error("Errors:\n%s", "\n".join(err_details))

    return 1 if n_error > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
