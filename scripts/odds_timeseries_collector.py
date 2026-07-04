"""オッズ時系列コレクタ (2026-07-04 導入)。

目的: 「群衆の行動の観測」データを貯める。各レースのオッズを朝〜締切〜確定の
複数時点でスナップショットし、将来の研究 (確定オッズ予測 / smart money 検知 /
三連単の順序モデル) の学習データにする。収集自体は投票と無関係の読み取り専用。

設計:
  - 毎朝 07:05 に Task Scheduler (AutoraceOddsTsCollector) が起動、
    当日の全開催場の R 毎発走時刻を取得し、各レースを
    OFFSETS_MIN = [-60, -30, -15, -8, -4, -3, +2] 分の 7 時点で観測。
    +2 分 (発走後) は締切済みプール = 確定オッズで、教師データになる。
  - 全券種 (tns/fns/wid/rfw/rtw/rf3/rt3) の API body を JSON のまま
    data/odds_ts/YYYY-MM-DD.jsonl に 1 スナップショット 1 行で追記。
    後から必要な券種だけ parse すればよい (保存時に間引かない)。
  - 再起動安全: 当日 jsonl を読み直して取得済み (race, offset) を復元。
  - 観測時刻を 180 秒過ぎた点は欠測として捨てる (遅延スナップは時刻が
    汚れて時系列研究に有害)。
  - 全レース + 2 分が過ぎたら自動終了。

使い方:
  python scripts/odds_timeseries_collector.py            # 当日分を収集
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.client import AutoraceClient, VENUE_CODES  # noqa: E402
from daily_predict import fetch_today_schedule  # noqa: E402
from dynamic_scheduler import (  # noqa: E402
    build_exact_race_starts, derive_anchor, estimate_interval_min,
    estimate_race_start, parse_hhmm, RACES_PER_DAY,
)

OUT_DIR = ROOT / "data" / "odds_ts"
LOG_FILE = ROOT / "data" / "odds_ts_collector.log"

OFFSETS_MIN = [-60, -30, -15, -8, -4, -3, 2]  # 発走からの分 (+2 = 確定)
CAPTURE_GRACE_SEC = 180   # 予定時刻からこの秒数を過ぎたら欠測扱い
POLL_SEC = 20             # メインループの巡回間隔


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def build_race_plan(client: AutoraceClient, today: dt.date) -> dict:
    """{(place_code, race_no): race_start_datetime} を構築。

    dynamic_scheduler と同じ手順: Program/Print の実時刻を第一候補、
    取得失敗時は Hold/Today anchor + 平均間隔の線形補間 fallback。
    """
    schedule = fetch_today_schedule(client)
    plan: dict[tuple[int, int], dt.datetime] = {}
    for pc, info in sorted(schedule.items()):
        if str(info.get("cancelFlg")) == "1":
            logging.info("pc=%d cancelFlg=1 skip", pc)
            continue
        venue_key = VENUE_CODES.get(pc, str(pc))
        exact = client.get_program_print_times(venue_key, today.isoformat())
        starts = build_exact_race_starts(exact, today)
        if not starts:
            anchor_r, anchor_time = derive_anchor(info)
            if anchor_time is None:
                logging.info("pc=%d 発走時刻取得不能 skip", pc)
                continue
            end = parse_hhmm(info.get("liveEndTime"))
            interval = estimate_interval_min(today, anchor_r, anchor_time, end)
            starts = {
                rn: estimate_race_start(today, anchor_r, anchor_time, rn, interval)
                for rn in range(1, RACES_PER_DAY + 1)
            }
            logging.info("pc=%d fallback 推定時刻を使用", pc)
        for rn, st in starts.items():
            plan[(pc, int(rn))] = st
        logging.info("pc=%d races=%d (R1=%s)", pc, len(starts),
                     min(starts.values()).strftime("%H:%M") if starts else "?")
    return plan


def load_captured(path: Path) -> set[str]:
    """当日 jsonl から取得済みキー {pc_rno_offset} を復元 (再起動安全)。"""
    done: set[str] = set()
    if not path.exists():
        return done
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                    done.add(f"{o['place_code']}_{o['race_no']}_{o['offset_min']}")
                except Exception:
                    continue
    except Exception as e:
        logging.warning("existing jsonl read failed: %s", e)
    logging.info("resume: %d snapshots already captured today", len(done))
    return done


def capture(client: AutoraceClient, out_path: Path, today: dt.date,
            pc: int, rno: int, offset_min: int,
            scheduled_for: dt.datetime) -> bool:
    """1 スナップショット取得して jsonl 追記。成功 True。"""
    try:
        resp = client.get_odds(pc, today.isoformat(), rno)
        body = resp.get("body", {}) if isinstance(resp, dict) else {}
    except Exception as e:
        logging.warning("get_odds failed pc=%d R%d off=%d: %s", pc, rno, offset_min, e)
        return False
    rec = {
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "race_date": today.isoformat(),
        "place_code": pc,
        "race_no": rno,
        "offset_min": offset_min,
        "scheduled_for": scheduled_for.isoformat(timespec="seconds"),
        "body": body,
    }
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def main() -> int:
    setup_logging()
    today = dt.date.today()
    out_path = OUT_DIR / f"{today.isoformat()}.jsonl"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    logging.info("=== odds_ts_collector start: %s ===", today.isoformat())

    try:
        client = AutoraceClient()
    except Exception as e:
        logging.error("AutoraceClient init failed: %s", e)
        return 1

    plan = build_race_plan(client, today)
    if not plan:
        logging.info("no races today - exit")
        return 0

    # 観測ポイント一覧 (時刻順)
    points = []  # (when, pc, rno, offset)
    for (pc, rno), start in plan.items():
        for off in OFFSETS_MIN:
            points.append((start + dt.timedelta(minutes=off), pc, rno, off, start))
    points.sort(key=lambda x: x[0])
    end_at = max(p[0] for p in points) + dt.timedelta(minutes=3)

    captured = load_captured(out_path)
    n_taken = n_missed = 0

    while True:
        now = dt.datetime.now()
        if now > end_at:
            break
        for when, pc, rno, off, start in points:
            key = f"{pc}_{rno}_{off}"
            if key in captured:
                continue
            if now < when:
                continue
            late = (now - when).total_seconds()
            if late > CAPTURE_GRACE_SEC:
                captured.add(key)  # 欠測として確定 (遅延スナップは撮らない)
                n_missed += 1
                continue
            ok = capture(client, out_path, today, pc, rno, off, start)
            captured.add(key)  # 失敗でも同一点の再試行はしない (時刻が汚れる)
            if ok:
                n_taken += 1
                logging.info("captured pc=%d R%d off=%+dmin (late %.0fs)",
                             pc, rno, off, late)
        time.sleep(POLL_SEC)

    logging.info("=== odds_ts_collector done: taken=%d missed=%d file=%s ===",
                 n_taken, n_missed, out_path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
