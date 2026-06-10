"""連勝式オッズ (odds_combo.csv) の遡及バックフィル

確定済みレースでも Odds API は全券種オッズを返すため、過去日の
クロージングオッズを遡って取得できる (2026-06-10 実証)。

既に odds_combo.csv にある (race_date, place_code) はスキップ (冪等)。
日次の新規分は ingest_day.py が同時収集するため、本スクリプトは
導入時の種まき・欠損補完用。

使い方:
  python scripts/backfill_odds_combo.py --days 30
  python scripts/backfill_odds_combo.py --start 2026-05-01 --end 2026-05-31
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.client import AutoraceClient, VENUE_CODES  # noqa: E402
from src.parser import parse_odds_combo  # noqa: E402
from src.storage import DATA_DIR, append_rows  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    force=True)
logger = logging.getLogger("backfill_odds_combo")

SLEEP_SEC = 0.5
MAX_RACES = 12


def existing_day_places() -> set[tuple[str, int]]:
    path = DATA_DIR / "odds_combo.csv"
    seen: set[tuple[str, int]] = set()
    if not path.exists():
        return seen
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            seen.add((row["race_date"], int(row["place_code"])))
    return seen


def race_day_places(start: dt.date, end: dt.date) -> list[tuple[str, int]]:
    """race_results.csv から対象期間に開催実績のある (date, place) を列挙"""
    path = DATA_DIR / "race_results.csv"
    found: set[tuple[str, int]] = set()
    s, e = start.isoformat(), end.isoformat()
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = row["race_date"]
            if s <= d <= e:
                found.add((d, int(row["place_code"])))
    return sorted(found)


def backfill_one(client: AutoraceClient, race_date: str, place_code: int) -> int:
    n = 0
    for race_no in range(1, MAX_RACES + 1):
        try:
            resp = client.get_odds(place_code, race_date, race_no)
            body = resp.get("body", {})
            if isinstance(body, list) or not body:
                break  # レース番号が尽きた
            rows = parse_odds_combo(place_code, race_date, race_no, body)
            n += append_rows("odds_combo.csv", rows)
        except Exception as e:  # noqa: BLE001
            logger.error("  %s place=%d R%d failed: %s", race_date, place_code, race_no, e)
        time.sleep(SLEEP_SEC)
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=None, help="直近N日を対象")
    p.add_argument("--start", type=str, default=None, help="YYYY-MM-DD")
    p.add_argument("--end", type=str, default=None, help="YYYY-MM-DD (default: 昨日)")
    args = p.parse_args()

    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today() - dt.timedelta(days=1)
    if args.start:
        start = dt.date.fromisoformat(args.start)
    elif args.days:
        start = end - dt.timedelta(days=args.days - 1)
    else:
        p.error("--days か --start を指定してください")

    targets = race_day_places(start, end)
    seen = existing_day_places()
    todo = [(d, pc) for d, pc in targets if (d, pc) not in seen]
    logger.info("対象 %d venue-days (既取得 %d をスキップ, 期間 %s〜%s)",
                len(todo), len(targets) - len(todo), start, end)

    client = AutoraceClient()
    total = 0
    for i, (d, pc) in enumerate(todo, 1):
        venue = VENUE_CODES.get(pc, str(pc))
        rows = backfill_one(client, d, pc)
        total += rows
        logger.info("[%d/%d] %s %s: %d rows", i, len(todo), d, venue, rows)
    logger.info("完了: 合計 %d rows 追記", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
