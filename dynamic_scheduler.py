"""動的発火スケジューラ: Hold/Today から各場 R1 開始時刻を取得し、
各レース発走 LEAD_MIN 分前に daily_predict.py を 1 R 単位で起動する
schtasks /SC ONCE one-shot を一括登録する。

毎日 07:00(daily_ingest 06:30 完了後)に起動する想定。

設計:
- 場毎の R 発走時刻は R1 + (n-1) * RACE_INTERVAL_MIN で推定
  (autorace.jp は通常 30 分間隔、ミッドナイトのみ短縮の可能性あり)
- 既に過ぎた発火時刻は登録しない(再走時の冪等化)
- 既存 AutoraceDyn_* タスクを毎回全削除してから登録(冪等化)
- 候補なしメールは --suppress-noresult-email で抑止 → 候補ありの R のみ通知
"""

from __future__ import annotations

import datetime as dt
import logging
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.client import AutoraceClient, VENUE_CODES
from daily_predict import fetch_today_schedule

DATA = ROOT / "data"
LOG_FILE = DATA / "dynamic_scheduler.log"

TASK_PREFIX = "AutoraceDyn_"
DEFAULT_RACE_INTERVAL_MIN = 30  # liveEndTime 取得失敗時のフォールバック
LEAD_MIN = 30
RACES_PER_DAY = 12
# 最終R終了時刻 = R12 発走 + おおよそレース3分 + 払戻数分。
# liveEndTime からこの分を差し引いて R12 発走時刻と扱う。
LIVE_END_TO_R12_START_OFFSET_MIN = 5

# venue_code -> short ASCII 名(タスク名・ログ用、cmd 安全)
VENUE_SHORT = {2: "kawaguchi", 3: "isesaki", 4: "hamamatsu", 5: "iizuka", 6: "sanyou"}

PROJECT_DIR = str(ROOT)
RUN_LOG = "data\\dynamic_run.log"

# cmd /c で起動。chcp 65001 を頭に挟んで Python の UTF-8 出力を文字化けさせない
CMD_TEMPLATE = (
    'cmd /c chcp 65001 >nul && cd /d "{project}" && '
    'python daily_predict.py --venues {pc} --races {race_no} '
    '--suppress-noresult-email --time-label "{label}" '
    '>> {run_log} 2>&1'
)


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


def list_dyn_tasks() -> list[str]:
    """既存の AutoraceDyn_* タスク名一覧を返す。"""
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV"],
            capture_output=True, text=True, encoding="cp932", errors="replace",
        )
    except Exception as e:
        logging.warning("schtasks /Query 失敗: %s", e)
        return []
    if result.returncode != 0:
        logging.warning("schtasks /Query exit=%d: %s", result.returncode, result.stderr)
        return []

    names: list[str] = []
    for line in result.stdout.splitlines():
        # CSV 1列目 "TaskName"。ヘッダ行 / 空行をスキップ
        if not line.startswith('"\\'):
            continue
        # "\AutoraceDyn_sanyou_R1","2026/04/30 09:55:00","Ready"
        first = line.split('","', 1)[0].lstrip('"').lstrip("\\")
        if first.startswith(TASK_PREFIX):
            names.append(first)
    return names


def cleanup_stale_tasks() -> int:
    """既存 AutoraceDyn_* を全削除。冪等化のため毎回呼ぶ。"""
    deleted = 0
    for name in list_dyn_tasks():
        try:
            r = subprocess.run(
                ["schtasks", "/Delete", "/TN", name, "/F"],
                capture_output=True, text=True, encoding="cp932", errors="replace",
            )
            if r.returncode == 0:
                deleted += 1
            else:
                logging.warning("delete failed %s: %s", name, r.stderr.strip())
        except Exception as e:
            logging.warning("delete exception %s: %s", name, e)
    if deleted:
        logging.info("cleanup: %d stale tasks deleted", deleted)
    return deleted


def parse_hhmm(s: str | None) -> dt.time | None:
    if not s or ":" not in s:
        return None
    try:
        h, m = s.split(":", 1)
        return dt.time(int(h), int(m))
    except Exception:
        return None


def derive_anchor(info: dict) -> tuple[int, dt.time | None]:
    """Hold/Today の info dict から (anchor_race_no, anchor_time) を返す。

    raceStartTime は nowRaceNo が指す R の実発走時刻なので、これを anchor にする。
    liveStartTime は放送開始(R1 より約 30 分早い)で誤差源なので fallback のみ。
    """
    rstart = parse_hhmm(info.get("raceStartTime"))
    now_r = info.get("nowRaceNo")
    if rstart is not None and now_r is not None:
        try:
            return int(now_r), rstart
        except (TypeError, ValueError):
            pass
    # fallback: liveStartTime を R1 と仮定(誤差大、最後の手段)
    lstart = parse_hhmm(info.get("liveStartTime"))
    return 1, lstart


def estimate_interval_min(
    today: dt.date,
    anchor_r: int, anchor_time: dt.time,
    end_time: dt.time | None,
) -> float:
    """anchor R の発走時刻と liveEndTime から平均 race 間隔(分)を推定。
    end_time が None / span<=0 の場合は DEFAULT_RACE_INTERVAL_MIN を返す。
    end_time < anchor(ナイター日跨ぎ)は +1 日として扱う。
    """
    if end_time is None or anchor_r >= RACES_PER_DAY:
        return float(DEFAULT_RACE_INTERVAL_MIN)
    start = dt.datetime.combine(today, anchor_time)
    end = dt.datetime.combine(today, end_time)
    if end <= start:
        end += dt.timedelta(days=1)
    r12_start = end - dt.timedelta(minutes=LIVE_END_TO_R12_START_OFFSET_MIN)
    span = (r12_start - start).total_seconds() / 60.0
    n_steps = RACES_PER_DAY - anchor_r
    if span <= 0 or n_steps <= 0:
        return float(DEFAULT_RACE_INTERVAL_MIN)
    return span / n_steps


def estimate_race_start(
    today: dt.date,
    anchor_r: int, anchor_time: dt.time,
    race_no: int, interval_min: float,
) -> dt.datetime:
    """anchor を起点に race_no の発走時刻を推定。日跨ぎナイター対応。"""
    base = dt.datetime.combine(today, anchor_time)
    out = base + dt.timedelta(minutes=(race_no - anchor_r) * interval_min)
    return out


def register_one_shot(task_name: str, fire_at: dt.datetime, command: str) -> bool:
    sd = fire_at.strftime("%Y/%m/%d")
    st = fire_at.strftime("%H:%M")
    try:
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name,
             "/SC", "ONCE", "/SD", sd, "/ST", st,
             "/TR", command, "/F"],
            capture_output=True, text=True, encoding="cp932", errors="replace",
        )
    except Exception as e:
        logging.warning("register exception %s: %s", task_name, e)
        return False
    if r.returncode == 0:
        return True
    logging.warning("register failed %s: %s", task_name, r.stderr.strip() or r.stdout.strip())
    return False


def main() -> int:
    setup_logging()
    today = dt.date.today()
    now = dt.datetime.now()
    logging.info("=== dynamic_scheduler start: %s (now=%s) ===",
                 today.isoformat(), now.strftime("%H:%M:%S"))

    cleanup_stale_tasks()

    try:
        client = AutoraceClient()
    except Exception as e:
        logging.error("AutoraceClient 初期化失敗: %s", e)
        return 1

    schedule = fetch_today_schedule(client)
    if not schedule:
        logging.info("Hold/Today 開催なし — 何も登録しない")
        logging.info("=== dynamic_scheduler done: registered=0 ===")
        return 0

    registered = 0
    skipped_past = 0
    skipped_other = 0

    for pc in sorted(schedule.keys()):
        info = schedule[pc]
        venue_jp = VENUE_CODES.get(pc, str(pc))
        venue_short = VENUE_SHORT.get(pc, str(pc))

        if str(info.get("cancelFlg")) == "1":
            logging.info("pc=%d (%s) cancelFlg=1 — スキップ", pc, venue_jp)
            skipped_other += RACES_PER_DAY
            continue
        anchor_r, anchor_time = derive_anchor(info)
        if anchor_time is None:
            logging.info("pc=%d (%s) 発走時刻情報取得失敗 — スキップ", pc, venue_jp)
            skipped_other += RACES_PER_DAY
            continue
        end = parse_hhmm(info.get("liveEndTime"))
        interval = estimate_interval_min(today, anchor_r, anchor_time, end)

        logging.info(
            "pc=%d (%s) anchor=R%d@%s, liveEnd=%s, 推定 interval=%.1f min, R12=%s",
            pc, venue_jp, anchor_r,
            anchor_time.strftime("%H:%M"),
            end.strftime("%H:%M") if end else "?",
            interval,
            estimate_race_start(today, anchor_r, anchor_time,
                                RACES_PER_DAY, interval).strftime("%H:%M"),
        )

        for race_no in range(1, RACES_PER_DAY + 1):
            race_start = estimate_race_start(today, anchor_r, anchor_time,
                                             race_no, interval)
            fire_at = race_start - dt.timedelta(minutes=LEAD_MIN)

            if fire_at <= now + dt.timedelta(minutes=1):
                skipped_past += 1
                continue

            task_name = f"{TASK_PREFIX}{venue_short}_R{race_no}"
            label = f"{venue_short}_R{race_no}"
            command = CMD_TEMPLATE.format(
                project=PROJECT_DIR, pc=pc, race_no=race_no,
                label=label, run_log=RUN_LOG,
            )

            if register_one_shot(task_name, fire_at, command):
                logging.info("registered %s @ %s (race=%s)",
                             task_name, fire_at.strftime("%H:%M"),
                             race_start.strftime("%H:%M"))
                registered += 1

    logging.info("=== dynamic_scheduler done: registered=%d skipped_past=%d skipped_other=%d ===",
                 registered, skipped_past, skipped_other)
    return 0


if __name__ == "__main__":
    sys.exit(main())
