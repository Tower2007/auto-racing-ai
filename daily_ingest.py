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

対象日の窓 (2026-11-11 変更): --catchup N は「最低 N 日」。manifest 上で 5 場とも
完了 (ok/no_race) した最後の日 (= 前回成功日) の翌日まで遡って窓を広げる
(上限 MAX_BACKFILL_DAYS)。固定の 2 日窓だと PC 停止などで 2 日以上空いた日が
永久欠損になる (10/14 が実例: 10/15〜16 に実行が無く、10/17 の窓は 10/15〜16)。
取込済みの日は manifest で skip されるので広げても重複取込はしない。
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
MANIFEST_CSV = ROOT / "data" / "ingest_manifest.csv"
MAX_BACKFILL_DAYS = 31


def last_complete_date(manifest_csv: Path = MANIFEST_CSV,
                       n_venues: int = len(VENUE_CODES)) -> dt.date | None:
    """manifest 上で全場の最終 status が ok/no_race の日のうち最新の日 (前回成功日)。"""
    if not manifest_csv.exists():
        return None
    import csv
    last: dict[tuple[str, str], str] = {}
    with open(manifest_csv, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            last[(row.get("race_date", ""), row.get("place_code", ""))] = row.get("status", "")
    done: dict[str, int] = {}
    for (d, _pc), st in last.items():
        if st in ("ok", "no_race"):
            done[d] = done.get(d, 0) + 1
    full = [d for d, n in done.items() if n >= n_venues]
    if not full:
        return None
    try:
        return dt.date.fromisoformat(max(full))
    except ValueError:
        return None


def target_dates(today: dt.date, catchup: int,
                 last_done: dt.date | None) -> list[str]:
    """昨日〜(N 日前 と 前回成功日の翌日 の古い方) を古い順で返す (上限 MAX_BACKFILL_DAYS)。"""
    start = today - dt.timedelta(days=catchup)
    if last_done is not None:
        start = min(start, last_done + dt.timedelta(days=1))
    start = max(start, today - dt.timedelta(days=MAX_BACKFILL_DAYS))
    n = (today - start).days
    return [(today - dt.timedelta(days=i)).isoformat() for i in range(n, 0, -1)]


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        # ingest_day.py が import 時に basicConfig 済みのため、force が無いと
        # ここが no-op になり FileHandler が付かない (ログが2026-04-28から
        # 一度も書かれていなかった原因)
        force=True,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", type=str, default=None,
                   help="YYYY-MM-DD 形式の特定日。指定すれば catchup は無視。")
    p.add_argument("--catchup", type=int, default=2,
                   help="最低 N 日前まで遡る (default: 2)。前回成功日の翌日が"
                        "それより古ければそこまで広げる")
    args = p.parse_args()

    setup_logging()
    logger = logging.getLogger("daily_ingest")

    # 対象日リスト
    if args.date:
        dates = [args.date]
    else:
        today = dt.date.today()
        # 1 日前 〜 max(N 日前, 前回成功日の翌日) (古い日から処理)
        dates = target_dates(today, args.catchup, last_complete_date())

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

    # エラーがあれば即時メール通知
    if n_error > 0:
        try:
            from gmail_notify import send_email
            today_str = dt.date.today().isoformat()
            subject = f"[autorace] 🚨 daily_ingest 失敗 {today_str} (error={n_error})"
            body = (
                f"daily_ingest で {n_error} 件の失敗が発生しました。\n\n"
                f"対象日: {', '.join(dates)}\n"
                f"成功: {n_processed} / 開催なし: {n_no_race} / "
                f"重複スキップ: {n_skip} / 失敗: {n_error}\n\n"
                f"【エラー詳細】\n"
                + "\n".join(f"  - {e}" for e in err_details)
                + f"\n\n対処: data/daily_ingest.log を確認。\n"
                f"必要に応じて手動再実行: python ingest_day.py YYYY-MM-DD <place_code>"
            )
            send_email(subject=subject, body=body)
            logger.info("エラー通知メール送信済")
        except Exception as e:
            logger.error("エラー通知メール送信も失敗: %s", e)

    return 1 if n_error > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
