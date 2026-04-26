"""過去データ一括取得 (5年分バックフィル)

使い方:
  python backfill.py                          # 全期間 (2021-04-26 ~ 今日)
  python backfill.py 2024-01-01 2024-12-31    # 期間指定
  python backfill.py 2024-01-01 2024-12-31 5  # 期間 + 場指定

中断しても再実行で自動的に続きから再開。
進捗は data/backfill_done.txt に記録。
"""

import json
import sys
import time
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from src.client import AutoraceClient, VENUE_CODES
from ingest_day import ingest_one_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

PROGRESS_FILE = Path("data/backfill_done.txt")
STATS_FILE = Path("data/backfill_stats.json")

DEFAULT_START = "2021-04-26"


def load_done() -> set[str]:
    """完了済み 'YYYY-MM-DD:placeCode' セットを読み込む。"""
    if not PROGRESS_FILE.exists():
        return set()
    with open(PROGRESS_FILE, "r") as f:
        return {line.strip() for line in f if line.strip()}


def mark_done(race_date: str, place_code: int) -> None:
    """進捗ファイルに追記。"""
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "a") as f:
        f.write(f"{race_date}:{place_code}\n")


def save_stats(stats: dict) -> None:
    """累計統計を保存。"""
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)


def date_range(start: date, end: date):
    """start から end まで1日ずつ yield。"""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main() -> None:
    # 引数パース
    start_str = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START
    end_str = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()
    venues = [int(sys.argv[3])] if len(sys.argv) > 3 else sorted(VENUE_CODES.keys())

    start_date = date.fromisoformat(start_str)
    end_date = date.fromisoformat(end_str)
    total_days = (end_date - start_date).days + 1

    logger.info("=== Backfill: %s ~ %s, venues=%s ===", start_str, end_str, venues)
    logger.info("  Total date range: %d days x %d venues = %d candidates",
                total_days, len(venues), total_days * len(venues))

    done = load_done()
    logger.info("  Already done: %d", len(done))

    client = AutoraceClient()

    # 統計
    stats = {
        "started_at": datetime.now().isoformat(),
        "range": f"{start_str} ~ {end_str}",
        "venues": venues,
        "ingested": 0,
        "no_race": 0,
        "skipped": 0,
        "errors": 0,
        "total_rows": {},
    }
    t0 = time.time()

    try:
        for d in date_range(start_date, end_date):
            ds = d.isoformat()
            for pc in venues:
                key = f"{ds}:{pc}"
                if key in done:
                    stats["skipped"] += 1
                    continue

                try:
                    counts = ingest_one_day(client, pc, ds)

                    if counts.get("skipped"):
                        # has_race_day duplicate check
                        mark_done(ds, pc)
                        done.add(key)
                        stats["skipped"] += 1
                        continue

                    if not counts or sum(v for v in counts.values() if isinstance(v, int)) == 0:
                        stats["no_race"] += 1
                    else:
                        stats["ingested"] += 1
                        for k, v in counts.items():
                            if isinstance(v, int):
                                stats["total_rows"][k] = stats["total_rows"].get(k, 0) + v

                    mark_done(ds, pc)
                    done.add(key)

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.error("FAILED %s venue=%d: %s", ds, pc, e)
                    stats["errors"] += 1
                    # エラーでも mark_done しない → 再実行時にリトライ

            # 日ごとに進捗表示
            elapsed = time.time() - t0
            processed = stats["ingested"] + stats["no_race"] + stats["skipped"] + stats["errors"]
            total = total_days * len(venues)
            if processed > 0 and processed % (len(venues)) == 0:
                pct = processed / total * 100
                remaining = total - processed
                rate = elapsed / max(processed - stats["skipped"], 1)
                eta_sec = remaining * rate
                eta_h = eta_sec / 3600
                logger.info(
                    "Progress: %d/%d (%.1f%%) | ingested=%d no_race=%d errors=%d | ETA: %.1fh",
                    processed, total, pct,
                    stats["ingested"], stats["no_race"], stats["errors"],
                    eta_h,
                )

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        stats["finished_at"] = datetime.now().isoformat()
        stats["elapsed_sec"] = round(time.time() - t0, 1)
        save_stats(stats)
        logger.info("=== Backfill summary ===")
        logger.info("  Ingested: %d days", stats["ingested"])
        logger.info("  No race: %d days", stats["no_race"])
        logger.info("  Skipped (already done): %d", stats["skipped"])
        logger.info("  Errors: %d", stats["errors"])
        logger.info("  Elapsed: %.1f min", stats["elapsed_sec"] / 60)
        logger.info("  Total rows: %s", stats["total_rows"])
        logger.info("  Stats saved to %s", STATS_FILE)


if __name__ == "__main__":
    main()
