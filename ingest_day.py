"""1日分のレースデータを API 取得 → CSV 保存

使い方:
  python ingest_day.py YYYY-MM-DD placeCode
  python ingest_day.py 2026-04-24 5        # 飯塚 2026-04-24

placeCode: 2=川口, 3=伊勢崎, 4=浜松, 5=飯塚, 6=山陽
"""

import sys
import logging

from src.client import AutoraceClient, VENUE_CODES
from src.parser import (
    parse_program_entries,
    parse_program_stats,
    parse_race_results,
    parse_race_laps,
    parse_payouts,
    parse_odds_summary,
)
from src.storage import append_rows, has_race_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

MAX_RACES = 12


def ingest_one_day(client: AutoraceClient, place_code: int, race_date: str) -> dict:
    """1日分の全データを取得して CSV に保存。戻り値は投入行数サマリー。"""

    # 重複チェック
    if has_race_day("race_entries.csv", place_code, race_date):
        logger.warning("Already ingested: %s place=%d, skipping", race_date, place_code)
        return {"skipped": True}

    venue = VENUE_CODES.get(place_code, f"code{place_code}")
    logger.info("=== Ingest: %s %s (%d) ===", race_date, venue, place_code)

    counts: dict[str, int] = {}

    # --- 早期チェック: Program R1 で開催有無を判定 (1 API コールで済む) ---
    try:
        prog1 = client.get_program(place_code, race_date, 1)
        body1 = prog1.get("body", {})
        # 開催なしの日は body が list ([]) で返る
        if isinstance(body1, list) or not body1.get("playerList"):
            logger.info("  No race on %s at %s", race_date, venue)
            return counts  # 空 dict = no_race
    except Exception as e:
        logger.error("Program R1 check failed: %s", e)
        return counts

    # --- RaceRefund (1日分まとめ) ---
    try:
        refund = client.get_race_refund(place_code, race_date)
        refund_body = refund.get("body", [])
        if isinstance(refund_body, list):
            payout_rows = parse_payouts(place_code, race_date, refund_body)
            counts["payouts"] = append_rows("payouts.csv", payout_rows)
    except Exception as e:
        logger.error("RaceRefund failed: %s", e)

    # --- Per-race (R1 は既に取得済み) ---
    for race_no in range(1, MAX_RACES + 1):
        # Program
        try:
            if race_no == 1:
                body = body1  # R1 は再利用
            else:
                prog = client.get_program(place_code, race_date, race_no)
                body = prog.get("body", {})
                if isinstance(body, list) or not body.get("playerList"):
                    logger.info("  R%d: no playerList, stopping", race_no)
                    break

            entries = parse_program_entries(place_code, race_date, race_no, body)
            counts["entries"] = counts.get("entries", 0) + append_rows("race_entries.csv", entries)

            stats = parse_program_stats(place_code, race_date, race_no, body)
            counts["stats"] = counts.get("stats", 0) + append_rows("race_stats.csv", stats)
        except Exception as e:
            logger.error("  Program R%d failed: %s", race_no, e)

        # Odds
        try:
            odds = client.get_odds(place_code, race_date, race_no)
            odds_body = odds.get("body", {})
            if not isinstance(odds_body, list):
                odds_rows = parse_odds_summary(place_code, race_date, race_no, odds_body)
                counts["odds"] = counts.get("odds", 0) + append_rows("odds_summary.csv", odds_rows)
        except Exception as e:
            logger.error("  Odds R%d failed: %s", race_no, e)

        # RaceResult
        try:
            result = client.get_race_result(place_code, race_date, race_no)
            res_body = result.get("body", {})
            if not isinstance(res_body, list):
                result_rows = parse_race_results(place_code, race_date, race_no, res_body)
                counts["results"] = counts.get("results", 0) + append_rows("race_results.csv", result_rows)

                lap_rows = parse_race_laps(place_code, race_date, race_no, res_body)
                counts["laps"] = counts.get("laps", 0) + append_rows("race_laps.csv", lap_rows)
        except Exception as e:
            logger.error("  Result R%d failed: %s", race_no, e)

    logger.info("=== Done: %s %s ===", race_date, venue)
    for name, count in sorted(counts.items()):
        logger.info("  %s: %d rows", name, count)
    return counts


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python ingest_day.py YYYY-MM-DD placeCode")
        print("  placeCode: 2=kawaguchi, 3=isesaki, 4=hamamatsu, 5=iizuka, 6=sanyou")
        sys.exit(1)

    race_date = sys.argv[1]
    place_code = int(sys.argv[2])
    client = AutoraceClient()
    ingest_one_day(client, place_code, race_date)


if __name__ == "__main__":
    main()
