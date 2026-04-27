"""(A.1) entries に欠落している 1 race を再取得して追記。

DQ レポート (2026-04-27) で発見:
  results / odds には存在するが entries / stats に無い: 2023-05-25 浜松 (pc=4) R7

backfill 中の transient 失敗と推定。Program API を再叩きして
race_entries.csv と race_stats.csv に追記する。idempotent: 既に存在すれば何もしない。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.client import AutoraceClient
from src.parser import parse_program_entries, parse_program_stats
from src.storage import append_rows, read_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TARGET = {"place_code": 4, "race_date": "2023-05-25", "race_no": 7}


def already_present(csv_name: str, target: dict) -> bool:
    rows = read_csv(csv_name)
    pc, rd, rn = str(target["place_code"]), target["race_date"], str(target["race_no"])
    return any(
        r.get("place_code") == pc and r.get("race_date") == rd and r.get("race_no") == rn
        for r in rows
    )


def main() -> None:
    if already_present("race_entries.csv", TARGET):
        logger.info("entries already present for %s — skipping (idempotent)", TARGET)
        return

    client = AutoraceClient()
    logger.info("Fetching Program API for %s", TARGET)
    resp = client.get_program(**{"place_code": TARGET["place_code"], "race_date": TARGET["race_date"], "race_no": TARGET["race_no"]})

    if resp.get("result") != "Success":
        logger.error("API failure: %s", resp)
        sys.exit(1)

    body = resp["body"]
    entries = parse_program_entries(TARGET["place_code"], TARGET["race_date"], TARGET["race_no"], body)
    stats = parse_program_stats(TARGET["place_code"], TARGET["race_date"], TARGET["race_no"], body)
    logger.info("Parsed: %d entries, %d stats rows", len(entries), len(stats))

    n_e = append_rows("race_entries.csv", entries)
    n_s = append_rows("race_stats.csv", stats)
    logger.info("Appended: race_entries.csv += %d, race_stats.csv += %d", n_e, n_s)


if __name__ == "__main__":
    main()
