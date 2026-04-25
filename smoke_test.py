"""1 race-day スモークテスト

飯塚(5) の直近開催日 1 日分で全 API を叩き、レスポンスを data/smoke/ に保存。
使い方: python smoke_test.py [YYYY-MM-DD] [placeCode]
"""

import json
import os
import sys
import logging
from datetime import datetime
from pathlib import Path

from src.client import AutoraceClient, VENUE_CODES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

SMOKE_DIR = Path("data/smoke")


def save_json(filename: str, data: object) -> Path:
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    path = SMOKE_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("Saved %s (%d bytes)", path, path.stat().st_size)
    return path


def find_recent_race_date(client: AutoraceClient, place_code: int) -> str | None:
    """Recent API からその場の直近の完了済み開催日を探す。"""
    resp = client.get_recent_hold(place_code)
    body = resp.get("body", [])
    if not body:
        return None

    # 最新開催の最後のレース日を返す
    for hold in body:
        race_list = hold.get("raceList", [])
        if race_list:
            # raceList は日付順、最後が最新
            return race_list[-1].get("raceDate")
    return None


def summarize(label: str, data: object) -> None:
    """データ形状のサマリーを出力。"""
    if isinstance(data, dict):
        body = data.get("body", data)
        if isinstance(body, list):
            print(f"  {label}: body is list[{len(body)}]")
            if body:
                first = body[0]
                if isinstance(first, dict):
                    print(f"    keys: {list(first.keys())[:15]}...")
        elif isinstance(body, dict):
            print(f"  {label}: body is dict, keys={list(body.keys())[:15]}")
        else:
            print(f"  {label}: body type={type(body).__name__}")
    else:
        print(f"  {label}: type={type(data).__name__}")


def main() -> None:
    place_code = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    venue_name = VENUE_CODES.get(place_code, f"code{place_code}")

    client = AutoraceClient()

    # 日付指定がなければ Recent から自動取得
    if len(sys.argv) > 1:
        race_date = sys.argv[1]
    else:
        logger.info("Finding recent race date for %s (code=%d)...", venue_name, place_code)
        race_date = find_recent_race_date(client, place_code)
        if not race_date:
            logger.error("No recent race date found for %s", venue_name)
            sys.exit(1)

    logger.info("=== Smoke test: %s (%s) %s ===", venue_name, place_code, race_date)
    prefix = f"{race_date}_{venue_name}"

    # --- Today Hold (GET) ---
    try:
        today = client.get_today_hold()
        save_json(f"{prefix}_today_hold.json", today)
        summarize("TodayHold", today)
    except Exception as e:
        logger.warning("TodayHold failed (non-race day?): %s", e)

    # --- Players ---
    try:
        players = client.get_players(place_code, race_date)
        save_json(f"{prefix}_players.json", players)
        summarize("Players", players)
    except Exception as e:
        logger.warning("Players failed: %s", e)

    # --- RaceRefund (1日分まとめて) ---
    try:
        refund = client.get_race_refund(place_code, race_date)
        save_json(f"{prefix}_refund.json", refund)
        summarize("RaceRefund", refund)
    except Exception as e:
        logger.warning("RaceRefund failed: %s", e)

    # --- Per-race: Program / Odds / RaceResult ---
    max_race = 12
    for race_no in range(1, max_race + 1):
        logger.info("--- Race %d/%d ---", race_no, max_race)

        # Program
        try:
            prog = client.get_program(place_code, race_date, race_no)
            save_json(f"{prefix}_program_R{race_no:02d}.json", prog)
            body = prog.get("body", {})
            n_players = len(body.get("playerList", []))
            logger.info("  Program R%d: %d players", race_no, n_players)
        except Exception as e:
            logger.warning("  Program R%d failed: %s", race_no, e)

        # Odds
        try:
            odds = client.get_odds(place_code, race_date, race_no)
            save_json(f"{prefix}_odds_R{race_no:02d}.json", odds)
            body = odds.get("body", {})
            has_odds = bool(body.get("rtwOddsList"))
            logger.info("  Odds R%d: has_odds=%s", race_no, has_odds)
        except Exception as e:
            logger.warning("  Odds R%d failed: %s", race_no, e)

        # RaceResult
        try:
            result = client.get_race_result(place_code, race_date, race_no)
            save_json(f"{prefix}_result_R{race_no:02d}.json", result)
            body = result.get("body", {})
            n_results = len(body.get("raceResult", []))
            has_laps = bool(body.get("grandNoteList"))
            logger.info(
                "  Result R%d: %d results, has_laps=%s",
                race_no, n_results, has_laps,
            )
        except Exception as e:
            logger.warning("  Result R%d failed: %s", race_no, e)

    # --- Summary ---
    files = sorted(SMOKE_DIR.glob(f"{prefix}_*.json"))
    total_size = sum(f.stat().st_size for f in files)
    print(f"\n=== Smoke test complete ===")
    print(f"  Venue: {venue_name} (code={place_code})")
    print(f"  Date: {race_date}")
    print(f"  Files: {len(files)}")
    print(f"  Total size: {total_size:,} bytes")
    print(f"  Output: {SMOKE_DIR.resolve()}")


if __name__ == "__main__":
    main()
