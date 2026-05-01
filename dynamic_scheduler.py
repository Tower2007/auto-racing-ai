"""動的発火スケジューラ: 各場の R 毎の正確な発走時刻を取得し、
各レース発走 LEAD_MIN 分前に daily_predict.py を 1 R 単位で起動する
schtasks /SC ONCE one-shot を一括登録する。

毎日 07:00(daily_ingest 06:30 完了後)に起動する想定。

設計:
- 発走時刻は autorace.jp の印刷用 Program ページから R 毎にスクレイプして取得
  (`/race_info/Program/Print/{venueKey}/{YYYY-MM-DD}`、12 R 全て掲載)
- 取得失敗時のみ Hold/Today の anchor + 平均 interval で推定する fallback あり
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
# 発走 LEAD_MIN 分前に発火。
# 当初 30 分前 → 15 分前(2026-04-30) → 10 分前(2026-05-01)へ段階的に短縮。
# 15 分前でも人気車の複勝オッズ (fnsOddsList) が API センチネル 0.0/0.0 で
# 戻ることがあり (sanyou R1 で観測)、daily_predict 側で 1min×5 リトライ
# (合計最大 5 分待機)で救済する設計。発火 -10min + リトライ -5min 完了で
# 投票時間 5 分を確保。
LEAD_MIN = 10
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


def build_exact_race_starts(
    times_hhmm: dict[int, str], today: dt.date,
) -> dict[int, dt.datetime]:
    """{race_no: 'HH:MM'} → {race_no: datetime}。
    R 番号順に走査し、前 R より早い時刻が出たら +1 日(深夜跨ぎ対策)。
    """
    out: dict[int, dt.datetime] = {}
    day_offset = 0
    prev: dt.datetime | None = None
    for rn in sorted(times_hhmm.keys()):
        t = parse_hhmm(times_hhmm[rn])
        if t is None:
            continue
        cand = dt.datetime.combine(today + dt.timedelta(days=day_offset), t)
        if prev is not None and cand <= prev:
            day_offset += 1
            cand = dt.datetime.combine(today + dt.timedelta(days=day_offset), t)
        out[rn] = cand
        prev = cand
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
        venue_key = VENUE_CODES.get(pc, str(pc))
        venue_short = VENUE_SHORT.get(pc, str(pc))

        if str(info.get("cancelFlg")) == "1":
            logging.info("pc=%d (%s) cancelFlg=1 — スキップ", pc, venue_key)
            skipped_other += RACES_PER_DAY
            continue

        # まず Program/Print から R 毎の正確な発走時刻を取得
        exact_times = client.get_program_print_times(venue_key, today.isoformat())
        race_starts = build_exact_race_starts(exact_times, today)

        if race_starts:
            logging.info(
                "pc=%d (%s) Program/Print から R 毎発走時刻取得: R1=%s, R12=%s (%d R)",
                pc, venue_key,
                race_starts.get(1).strftime("%H:%M") if 1 in race_starts else "?",
                race_starts.get(RACES_PER_DAY).strftime("%H:%M")
                if RACES_PER_DAY in race_starts else "?",
                len(race_starts),
            )

        # 取得できなかった R 用の anchor + interval fallback を準備
        anchor_r, anchor_time = derive_anchor(info)
        end = parse_hhmm(info.get("liveEndTime"))
        if anchor_time is None:
            interval = float(DEFAULT_RACE_INTERVAL_MIN)
        else:
            interval = estimate_interval_min(today, anchor_r, anchor_time, end)

        if not race_starts:
            if anchor_time is None:
                logging.info("pc=%d (%s) Program/Print 失敗 + anchor 取得失敗 — スキップ",
                             pc, venue_key)
                skipped_other += RACES_PER_DAY
                continue
            logging.info(
                "pc=%d (%s) Program/Print 失敗 → fallback anchor=R%d@%s, "
                "liveEnd=%s, 推定 interval=%.1f min, R12=%s",
                pc, venue_key, anchor_r,
                anchor_time.strftime("%H:%M"),
                end.strftime("%H:%M") if end else "?",
                interval,
                estimate_race_start(today, anchor_r, anchor_time,
                                    RACES_PER_DAY, interval).strftime("%H:%M"),
            )

        for race_no in range(1, RACES_PER_DAY + 1):
            if race_no in race_starts:
                race_start = race_starts[race_no]
                source = "exact"
            elif anchor_time is not None:
                race_start = estimate_race_start(today, anchor_r, anchor_time,
                                                 race_no, interval)
                source = "estimate"
            else:
                skipped_other += 1
                continue
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
                logging.info("registered %s @ %s (race=%s, %s)",
                             task_name, fire_at.strftime("%H:%M"),
                             race_start.strftime("%H:%M"), source)
                registered += 1

    logging.info("=== dynamic_scheduler done: registered=%d skipped_past=%d skipped_other=%d ===",
                 registered, skipped_past, skipped_other)
    return 0


if __name__ == "__main__":
    sys.exit(main())
