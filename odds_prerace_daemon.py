"""全開催・全レース 直前オッズ常駐デーモン (T-5分 / T-1分)

背景:
- daily_predict は発火時 (発走 3-4 分前) の 1 時点だけ odds_combo_snapshots.csv
  に記録している。オッズドリフト検証 (T-5→T-1 のモメンタム・確定直前値) には
  2 時点収集が必要。
- 兄弟 PJ の先例: Boat_racing_AI/odds_prerace_daemon.py (全レース T-5/T-1、
  1ヶ月無事故) / central-keiba-ai/odds_prerace_daemon.py (T-5/T-1 常駐)。

仕様:
- 毎朝 07:05 起動 (タスク AutoraceOddsPrerace。dynamic_scheduler 07:00 の直後)
- 当日の全開催・全レースの発走時刻を推定。取得・推定ロジックは既存を再利用:
  Program/Print 実時刻 (client.get_program_print_times + build_exact_race_starts)
  → 失敗時 fallback は dynamic_scheduler と同一 (derive_anchor + 線形補間)
- 各レース発走 T-5 分 / T-1 分に Odds API を 1 回 POST し、全券種
  (単勝 tns / 複勝 fns / 2連単 rtw / 2連複 rfw / ワイド wid / 3連単 rt3 / 3連複 rf3)
  を data/odds_combo_prerace.csv へ追記。**既存ファイルには一切書かない**。
  ※ T-1 は投票締切 (発走 -2:30) 後 = 確定直前の板。T-5 との差分がモメンタム。
- 最終レースの T-1 処理後に自然終了。1 レース/1 時点の失敗はデーモンを殺さない。
- 単一インスタンスガード: localhost ポート bind 方式
  (hokkaido snapshot_scheduler の先例。2026-06-10 の二重起動・窓クローズ全滅の教訓。
  Daily トリガー常駐中に ONLOGON トリガーが再起動しても二重 append しない)
- pythonw 前提: ログは data/odds_prerace_daemon.log にファイル直書き。
  sys.stdout が None でも動く (StreamHandler は stdout がある時のみ追加)。

追加アクセス見積:
- 3場開催日で 36 レース × 2 時点 = 72 POST/日 + 起動時 Hold/Today 1 GET +
  Program/Print 場数ぶん (≦5 GET) + CSRF 1 GET。
- 取得間隔・UA は AutoraceClient をそのまま使うため既存 daily_predict と同一
  (AUTORACE_REQUEST_DELAY_SEC=0.5s、リトライ/バックオフ込み)。

使い方:
  pythonw odds_prerace_daemon.py                # 常駐 (通常はタスクから)
  python odds_prerace_daemon.py --dry-run       # 当日スケジュール表示のみ
  python odds_prerace_daemon.py --probe 6 5     # 山陽 R5 を即時 fetch (CSV 書き込みなし)
  python odds_prerace_daemon.py --max-events 2  # 直近 2 時点だけ処理して終了 (検証用)
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.client import AutoraceClient, VENUE_CODES  # noqa: E402
from src.parser import parse_odds_combo, parse_odds_summary  # noqa: E402
from src.storage import append_rows  # noqa: E402
# 時刻推定ロジックは dynamic_scheduler のものを再利用 (重複実装しない)。
# ※ import するだけで dynamic_scheduler が schtasks 登録を行うことはない
#   (main() は __main__ ガード内)。fetch_today_schedule は daily_predict 由来。
from daily_predict import fetch_today_schedule  # noqa: E402
from dynamic_scheduler import (  # noqa: E402
    DEFAULT_RACE_INTERVAL_MIN,
    RACES_PER_DAY,
    build_exact_race_starts,
    derive_anchor,
    estimate_interval_min,
    estimate_race_start,
    parse_hhmm,
)

DATA = ROOT / "data"
OUT_CSV_NAME = "odds_combo_prerace.csv"  # 書き込み先は新 CSV のみ
LOG_FILE = DATA / "odds_prerace_daemon.log"

OFFSETS_MIN = (5, 1)   # 発走 T-5 分 (投票可能圏) / T-1 分 (締切後・確定直前)
GRACE_MIN = 2          # 目標時刻をこの分数まで過ぎた event は「直近」として即実行

# 単一インスタンスガード用ポート (bind 中 = 稼働中)。プロセス終了で OS が解放。
# 本システム 8521 / Boat 8511 / Auto streamlit 8501/8502 / boat daemon 58610 /
# hokkaido 49317 と非衝突の値を選定。
SINGLETON_PORT = 58620


def setup_logging() -> None:
    """pythonw 前提のログ設定。ファイル直書き + (あれば) stdout。"""
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — pythonw では stdout が None
        pass
    handlers: list[logging.Handler] = [
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def acquire_singleton(port: int = SINGLETON_PORT) -> socket.socket | None:
    """localhost ポート bind による単一インスタンスガード。

    戻り値の socket をプロセス生存中保持すること (GC で閉じると解放される)。
    None = 既に別インスタンスが稼働中。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def build_snapshot_rows(
    place_code: int, race_date: str, race_no: int,
    odds_body: dict, off: int,
    captured_at: str | None = None,
) -> list[dict]:
    """Odds API body → 全券種の CSV 行リスト (target_offset_min 付き)。

    - combo 5 券種 (rtw/rfw/wid/rt3/rf3): 既存 parse_odds_combo をそのまま再利用
    - 単勝 tns / 複勝 fns: parse_odds_summary の行を同スキーマに縦持ち変換
      (car_no_1 のみ使用。tns は odds、fns は odds_min/odds_max)
    """
    ts = captured_at or dt.datetime.now().isoformat(timespec="seconds")
    rows = parse_odds_combo(place_code, race_date, race_no, odds_body)

    for s in parse_odds_summary(place_code, race_date, race_no, odds_body):
        base = {
            "race_date": race_date,
            "place_code": place_code,
            "race_no": race_no,
            "car_no_1": s.get("car_no"),
            "car_no_2": None,
            "car_no_3": None,
        }
        if s.get("win_odds") is not None:
            rows.append({**base, "bet_type": "tns",
                         "odds": s["win_odds"], "odds_min": None, "odds_max": None})
        if s.get("place_odds_min") is not None or s.get("place_odds_max") is not None:
            rows.append({**base, "bet_type": "fns", "odds": None,
                         "odds_min": s.get("place_odds_min"),
                         "odds_max": s.get("place_odds_max")})

    for r in rows:
        r["captured_at"] = ts
        r["target_offset_min"] = off
    return rows


def build_events(
    race_starts_by_pc: dict[int, dict[int, dt.datetime]],
    offsets: tuple[int, ...] = OFFSETS_MIN,
) -> list[tuple[dt.datetime, int, int, int]]:
    """{place_code: {race_no: 発走時刻}} → [(snapshot時刻, pc, race_no, offset), ...]
    時刻昇順ソート済み。"""
    events: list[tuple[dt.datetime, int, int, int]] = []
    for pc, starts in race_starts_by_pc.items():
        for race_no, start in starts.items():
            for off in offsets:
                events.append((start - dt.timedelta(minutes=off), pc, race_no, off))
    events.sort(key=lambda e: e[0])
    return events


def fetch_today_race_starts(
    client: AutoraceClient, today: dt.date,
) -> dict[int, dict[int, dt.datetime]]:
    """当日の全開催・全レースの発走時刻を推定 (dynamic_scheduler と同一手順)。

    1. Hold/Today (fetch_today_schedule) で開催場を列挙 (cancelFlg=1 は除外)
    2. Program/Print から R 毎の実時刻 (build_exact_race_starts)
    3. 取れなかった R は anchor + 平均 interval の線形補間 fallback
    """
    schedule = fetch_today_schedule(client)
    out: dict[int, dict[int, dt.datetime]] = {}
    for pc in sorted(schedule.keys()):
        info = schedule[pc]
        venue_key = VENUE_CODES.get(pc, str(pc))
        if str(info.get("cancelFlg")) == "1":
            logging.info("pc=%d (%s) cancelFlg=1 — スキップ", pc, venue_key)
            continue

        try:
            exact_times = client.get_program_print_times(venue_key, today.isoformat())
        except Exception as e:  # noqa: BLE001 — fallback へ
            logging.warning("pc=%d Program/Print 取得例外: %s", pc, e)
            exact_times = {}
        race_starts = build_exact_race_starts(exact_times, today)

        anchor_r, anchor_time = derive_anchor(info)
        end = parse_hhmm(info.get("liveEndTime"))
        if anchor_time is None:
            interval = float(DEFAULT_RACE_INTERVAL_MIN)
        else:
            interval = estimate_interval_min(today, anchor_r, anchor_time, end)

        if not race_starts and anchor_time is None:
            logging.warning("pc=%d (%s) 実時刻もanchorも取得不可 — スキップ", pc, venue_key)
            continue

        starts: dict[int, dt.datetime] = {}
        n_exact = 0
        for race_no in range(1, RACES_PER_DAY + 1):
            if race_no in race_starts:
                starts[race_no] = race_starts[race_no]
                n_exact += 1
            elif anchor_time is not None:
                starts[race_no] = estimate_race_start(
                    today, anchor_r, anchor_time, race_no, interval)
        if starts:
            out[pc] = starts
            logging.info(
                "pc=%d (%s): %d R (exact=%d, estimate=%d) R1=%s R12=%s",
                pc, venue_key, len(starts), n_exact, len(starts) - n_exact,
                starts.get(1).strftime("%H:%M") if 1 in starts else "?",
                starts.get(RACES_PER_DAY).strftime("%H:%M")
                if RACES_PER_DAY in starts else "?",
            )
    return out


def collect_snapshot(
    client: AutoraceClient, place_code: int, race_date: str,
    race_no: int, off: int, write: bool = True,
) -> int:
    """1 レース 1 時点の全券種オッズを取得して追記。戻り値は追記行数。

    オッズ未公開/形式異常は 0 行 (warning のみ)。例外は呼び出し側で隔離する。
    """
    resp = client.get_odds(place_code, race_date, race_no)
    body = resp.get("body", {})
    if not isinstance(body, dict) or not body:
        logging.warning("pc=%d R%d T-%d: odds body が dict でない (未公開?)",
                        place_code, race_no, off)
        return 0
    rows = build_snapshot_rows(place_code, race_date, race_no, body, off)
    if not rows:
        logging.warning("pc=%d R%d T-%d: 全券種 0 行 (オッズ未公開?)",
                        place_code, race_no, off)
        return 0
    if write:
        append_rows(OUT_CSV_NAME, rows)
    return len(rows)


def _summarize_rows(rows: list[dict]) -> str:
    """probe 用: bet_type 別行数の要約文字列。"""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["bet_type"]] = counts.get(r["bet_type"], 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def run_daemon(race_date: dt.date, dry_run: bool, max_events: int) -> int:
    events: list[tuple[dt.datetime, int, int, int]] = []
    try:
        client = AutoraceClient()
        race_starts = fetch_today_race_starts(client, race_date)
        events = build_events(race_starts)
    except Exception as e:  # noqa: BLE001
        logging.error("スケジュール構築失敗: %s", e)
        return 1
    if not events:
        logging.info("%s: 開催なし (or 時刻取得不可) — 終了", race_date)
        return 0

    n_races = len({(e[1], e[2]) for e in events})
    logging.info("=== %s: %d場 %dレース x T-%s分 = %d snapshot 予定 ===",
                 race_date, len(race_starts), n_races,
                 "/".join(map(str, OFFSETS_MIN)), len(events))

    if dry_run:
        for when, pc, rno, off in events:
            logging.info("  %s pc=%d R%d (T-%d)", when.strftime("%H:%M"), pc, rno, off)
        return 0

    n_ok = n_skip = n_fail = 0
    n_done = 0
    for when, pc, rno, off in events:
        now = dt.datetime.now()
        if when < now - dt.timedelta(minutes=GRACE_MIN):
            n_skip += 1
            continue
        wait = (when - now).total_seconds()
        if wait > 0:
            time.sleep(wait)
        try:
            n_rows = collect_snapshot(client, pc, race_date.isoformat(), rno, off)
            if n_rows > 0:
                n_ok += 1
                logging.info("pc=%d R%d T-%d: %d 行追記", pc, rno, off, n_rows)
            else:
                n_fail += 1
        except Exception as e:  # noqa: BLE001 — 1 時点の失敗で常駐を止めない
            n_fail += 1
            logging.error("pc=%d R%d T-%d: %s", pc, rno, off, e)
        n_done += 1
        if max_events and n_done >= max_events:
            logging.info("--max-events %d 到達 — 早期終了 (検証モード)", max_events)
            break
    logging.info("=== 完了 ok=%d skip=%d fail=%d ===", n_ok, n_skip, n_fail)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="直前オッズ常駐デーモン (T-5/T-1)")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--dry-run", action="store_true",
                    help="スケジュール表示のみ (fetch は Hold/Today と Program/Print だけ)")
    ap.add_argument("--probe", nargs=2, type=int, metavar=("PC", "RACE_NO"),
                    help="指定レースを即時 fetch してパース結果を表示 (CSV 書き込みなし)")
    ap.add_argument("--max-events", type=int, default=0,
                    help="N 時点処理したら終了 (0=全件、検証用)")
    args = ap.parse_args()
    race_date = dt.date.fromisoformat(args.date)
    setup_logging()

    if args.probe:
        pc, rno = args.probe
        client = AutoraceClient()
        resp = client.get_odds(pc, race_date.isoformat(), rno)
        body = resp.get("body", {})
        if not isinstance(body, dict) or not body:
            logging.error("probe pc=%d R%d: odds body 取得不可", pc, rno)
            return 1
        rows = build_snapshot_rows(pc, race_date.isoformat(), rno, body, off=0)
        logging.info("probe pc=%d R%d: %d 行 (%s) — CSV 書き込みなし",
                     pc, rno, len(rows), _summarize_rows(rows))
        return 0

    lock = acquire_singleton()
    if lock is None:
        logging.info("既に別インスタンスが稼働中 (port %d) — 二重起動を回避して終了",
                     SINGLETON_PORT)
        return 0
    try:
        return run_daemon(race_date, args.dry_run, args.max_events)
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
