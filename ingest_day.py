"""1日分のレースデータを API 取得 → CSV 保存

使い方:
  python ingest_day.py YYYY-MM-DD placeCode
  python ingest_day.py 2026-04-24 5        # 飯塚 2026-04-24

placeCode: 2=川口, 3=伊勢崎, 4=浜松, 5=飯塚, 6=山陽
"""

import csv
import datetime as _dt
import sys
import logging
from pathlib import Path

from src.client import AutoraceClient, VENUE_CODES
from src.parser import (
    parse_program_entries,
    parse_program_stats,
    parse_race_results,
    parse_race_laps,
    parse_payouts,
    parse_odds_summary,
    parse_odds_combo,
)
from src.storage import DATA_DIR, append_rows, has_race_day

# ── 日別完了 manifest (2026-07-11 監査 P1-1 対応) ────────────────────
# 旧設計は「race_entries に当日行があれば skip」だったため、途中失敗した日が
# 完了扱いになり永久欠損になり得た (各テーブルの失敗はログのみで継続する設計)。
# manifest に day 単位の status (ok/no_race/partial) を記録し、
#   - ok / no_race     → skip
#   - partial          → 当日行を全テーブルから purge して再取得
#   - manifest 無し + entries あり → 旧データ (監査で join 欠損 0 確認済) は ok 扱い
MANIFEST_CSV = DATA_DIR / "ingest_manifest.csv"
MANIFEST_FIELDS = ["race_date", "place_code", "status", "errors", "completed_at"]
# purge 対象 (ingest が書く全テーブル)
DAY_TABLES = ["race_entries.csv", "race_stats.csv", "race_results.csv",
              "race_laps.csv", "payouts.csv", "odds_summary.csv", "odds_combo.csv"]


def manifest_status(place_code: int, race_date: str) -> str | None:
    """manifest 上の status を返す (無ければ None)。同一キー複数行は最終行優先。"""
    if not MANIFEST_CSV.exists():
        return None
    status = None
    with open(MANIFEST_CSV, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("place_code") == str(place_code)
                    and row.get("race_date") == race_date):
                status = row.get("status")
    return status


def write_manifest(place_code: int, race_date: str, status: str, errors: int) -> None:
    new_file = not MANIFEST_CSV.exists()
    with open(MANIFEST_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow({
            "race_date": race_date, "place_code": place_code,
            "status": status, "errors": errors,
            "completed_at": _dt.datetime.now().isoformat(timespec="seconds"),
        })


def purge_race_day(place_code: int, race_date: str) -> None:
    """全テーブルから当日 (place_code, race_date) の行を削除 (partial 再取得用)。

    行フィルタの CSV 書き直し。retry 経路でのみ実行される低頻度処理。
    """
    for name in DAY_TABLES:
        path = DATA_DIR / name
        if not path.exists():
            continue
        tmp = path.with_suffix(".purge_tmp")
        removed = 0
        with open(path, "r", encoding="utf-8", newline="") as fin, \
                open(tmp, "w", encoding="utf-8", newline="") as fout:
            reader = csv.DictReader(fin)
            writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                if (row.get("place_code") == str(place_code)
                        and row.get("race_date") == race_date):
                    removed += 1
                    continue
                writer.writerow(row)
        tmp.replace(path)
        if removed:
            logger.info("  purge %s: %d rows removed", name, removed)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

MAX_RACES = 12


def ingest_one_day(client: AutoraceClient, place_code: int, race_date: str) -> dict:
    """1日分の全データを取得して CSV に保存。戻り値は投入行数サマリー。

    2026-07-11: 完了 manifest 対応。skip 判定は manifest 優先:
      ok/no_race → skip / partial → purge して再取得 /
      記録なし + entries あり → 旧データ (完全性監査済) として skip。
    """
    m_status = manifest_status(place_code, race_date)
    if m_status in ("ok", "no_race"):
        logger.info("Already ingested (manifest=%s): %s place=%d, skipping",
                    m_status, race_date, place_code)
        return {"skipped": True}
    if m_status == "partial":
        logger.warning("Partial day detected: %s place=%d — purge して再取得",
                       race_date, place_code)
        purge_race_day(place_code, race_date)
    elif has_race_day("race_entries.csv", place_code, race_date):
        # manifest 導入 (2026-07-11) 以前のデータ。監査で join 欠損 0 確認済み。
        logger.warning("Already ingested (pre-manifest): %s place=%d, skipping",
                       race_date, place_code)
        return {"skipped": True}

    # 取込開始マーカ (2026-07-11 監査 P2-2): データ取得より先に partial 行を書き、
    # 完了時に ok/no_race/partial の行で上書きする (追記・最終行優先)。
    # プロセス kill 等で完了行が書けなかった場合も manifest=partial が残り、
    # 次回 catchup で purge+再取得される。旧方式では kill された日が
    # 「pre-manifest 旧データ扱い → ok skip」となり永久欠損の穴があった。
    # errors=-1 は「取込中 (未完了)」の意。
    write_manifest(place_code, race_date, "partial", -1)

    venue = VENUE_CODES.get(place_code, f"code{place_code}")
    logger.info("=== Ingest: %s %s (%d) ===", race_date, venue, place_code)

    counts: dict[str, int] = {}
    n_errors = 0

    # --- 早期チェック: Program R1 で開催有無を判定 (1 API コールで済む) ---
    try:
        prog1 = client.get_program(place_code, race_date, 1)
        body1 = prog1.get("body", {})
        # 開催なしの日は body が list ([]) で返る
        if isinstance(body1, list) or not body1.get("playerList"):
            logger.info("  No race on %s at %s", race_date, venue)
            write_manifest(place_code, race_date, "no_race", 0)
            return counts  # 空 dict = no_race
    except Exception as e:
        # 取得失敗は「開催なし」と区別し partial 扱い (次回 catchup で再試行)
        logger.error("Program R1 check failed: %s", e)
        write_manifest(place_code, race_date, "partial", 1)
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
        n_errors += 1

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
            n_errors += 1

        # Odds
        try:
            odds = client.get_odds(place_code, race_date, race_no)
            odds_body = odds.get("body", {})
            if not isinstance(odds_body, list):
                odds_rows = parse_odds_summary(place_code, race_date, race_no, odds_body)
                counts["odds"] = counts.get("odds", 0) + append_rows("odds_summary.csv", odds_rows)
                # 連勝式オッズ (2連単/2連複/ワイド/3連単/3連複) も同じレスポンスから保存
                combo_rows = parse_odds_combo(place_code, race_date, race_no, odds_body)
                counts["odds_combo"] = counts.get("odds_combo", 0) + append_rows("odds_combo.csv", combo_rows)
        except Exception as e:
            logger.error("  Odds R%d failed: %s", race_no, e)
            n_errors += 1

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
            n_errors += 1

    day_status = "ok" if n_errors == 0 else "partial"
    write_manifest(place_code, race_date, day_status, n_errors)
    if day_status == "partial":
        logger.warning("  day marked PARTIAL (%d errors) — 次回 catchup で再取得",
                       n_errors)
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
