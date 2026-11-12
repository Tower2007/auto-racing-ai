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
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.client import AutoraceClient, VENUE_CODES
from daily_predict import fetch_today_schedule

DATA = ROOT / "data"
LOG_FILE = DATA / "dynamic_scheduler.log"

TASK_PREFIX = "AutoraceDyn_"
DEFAULT_RACE_INTERVAL_MIN = 30  # liveEndTime 取得失敗時のフォールバック
# 発走 LEAD_MIN 分前に発火。場別に設定。
# 30→15→10→5→2→4→3→2→3→4→場別 分前へ変遷。
# 2 分前 (2026-05-14〜05-17): 処理+メール送信で締切ギリギリに到着する問題。
# 4 分前に戻し near-miss retry を廃止することで、通知が締切 ~2 分前に安定到着。
# 3 分前 (2026-06-04): retry 廃止済みのため安全。
# 2 分前 (2026-06-04): 投票締切が発走 -2:30 のため、-2:00 発火では締切後ページになり失敗。
# ※ 2026-06-06 確認: vote.autorace.jp の発注締切は発走 -2:30 (2分30秒前)。
# 3 分前 (2026-06-05〜06-06): RT3(複勝+三連単+三連複=3点)は Playwright 49〜57s かかり
#   締切後になり失敗(2026-06-07 山陽R3 で確認)。
# 場別 (2026-06-07〜): 三連系購入場は 4 分前、川口のみ 3 分前。
# RF3 対象場(伊勢崎/浜松/飯塚/山陽): 複勝+三連複 2点 → Playwright ~40s のため 4 分前
# (daily_predict.py RF3_ELIGIBLE_PLACES と同値)
LEAD_MIN_DEFAULT = 3          # 三連系購入なしの場 (川口, 飯塚): 締切まで 60s 余裕あり
LEAD_MIN_RF3_VENUES = 4       # RF3/RT3 対象場: Playwright 2〜3点で締切まで 90s 確保
# 2026-07-11: 飯塚(5) を RF3 除外 (実弾 0/17) に伴い 4分前 → デフォルト 3分前へ
LEAD_MIN_VENUE_MAP = {3: LEAD_MIN_RF3_VENUES, 4: LEAD_MIN_RF3_VENUES,
                      6: LEAD_MIN_RF3_VENUES}  # 伊勢崎,浜松,山陽 (daily_predict RF3_ELIGIBLE_PLACES と同値)
RACES_PER_DAY = 12
# 最終R終了時刻 = R12 発走 + おおよそレース3分 + 払戻数分。
# liveEndTime からこの分を差し引いて R12 発走時刻と扱う。
LIVE_END_TO_R12_START_OFFSET_MIN = 5

# venue_code -> short ASCII 名(タスク名・ログ用、cmd 安全)
VENUE_SHORT = {2: "kawaguchi", 3: "isesaki", 4: "hamamatsu", 5: "iizuka", 6: "sanyou"}

PROJECT_DIR = str(ROOT)

# VBScript ラッパー経由の非表示実行 (2026-06-11)。
# 旧: 'cmd /c chcp 65001 ... python daily_predict.py ...' 直接実行
#   → one-shot 発火のたびにコンソール窓が表示されていた (1日10回以上)。
# 新: wscript //B + WshShell.Run(cmd, 0) でコンソール窓を出さない。
#   対話セッションのまま動くので auto_buy の Playwright Chrome
#   (headless=False) には影響しない。実コマンド組立・dynamic_run.log への
#   redirect は scripts/run_predict_hidden.vbs 側 (project root は VBS が自己解決)。
VBS_WRAPPER = ROOT / "scripts" / "run_predict_hidden.vbs"
# 2026-09-04 監査 P1: 第4引数で開催日 {date} (このスケジューラの実行日) を渡す。
#   日跨ぎミッドナイト (飯塚/山陽の R7〜 は Program/Print 上 24:04 = 翌暦日 00:04)
#   の one-shot は 00:00 以降に発火するため、daily_predict 側の既定
#   `dt.date.today()` が翌暦日になり program 取得が空 → 無音スキップしていた
#   (2026-09-03 00:25 iizuka_R7 date=2026-09-03 eval=0)。vbs が --date に渡す。
CMD_TEMPLATE = 'wscript.exe //B "{vbs}" {pc} {race_no} "{label}" {date}'


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


def parse_hhmm_ex(s: str | None) -> tuple[dt.time, int] | None:
    """'HH:MM' → (time, day_offset)。不正値は None。

    Program/Print はミッドナイト開催の日跨ぎ R を '24:04' のように 24 時以降表記で
    返す。2026-09-04 監査 P3: 従来はこれが None に落ちて R7〜 が推定補間
    (30 分刻み) に置き換わり、8R 開催なのに R9-R12 の幽霊タスクまで登録されていた。
    h>=24 は (h-24, +1 日) として扱う (day_offset は 0 or 1)。
    """
    if not s or ":" not in s:
        return None
    try:
        h_str, m_str = s.split(":", 1)
        h, m = int(h_str), int(m_str)
    except Exception:
        return None
    if h < 0 or h >= 48 or not (0 <= m < 60):
        return None
    day_offset, h = divmod(h, 24)
    return dt.time(h, m), day_offset


def parse_hhmm(s: str | None) -> dt.time | None:
    """'HH:MM' → time。'24:04' は 00:04 に丸める (日付の繰り上げは呼び出し側が
    「前 R より早い / end <= start なら +1 日」で吸収する)。"""
    parsed = parse_hhmm_ex(s)
    return parsed[0] if parsed else None


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
    '24:04' 表記 (parse_hhmm_ex の day_offset=1) もそのまま +1 日として扱う。
    """
    out: dict[int, dt.datetime] = {}
    day_offset = 0
    prev: dt.datetime | None = None
    for rn in sorted(times_hhmm.keys()):
        parsed = parse_hhmm_ex(times_hhmm[rn])
        if parsed is None:
            continue
        t, t_offset = parsed
        day_offset = max(day_offset, t_offset)
        cand = dt.datetime.combine(today + dt.timedelta(days=day_offset), t)
        if prev is not None and cand <= prev:
            day_offset += 1
            cand = dt.datetime.combine(today + dt.timedelta(days=day_offset), t)
        out[rn] = cand
        prev = cand
    return out


def register_one_shot_ex(task_name: str, fire_at: dt.datetime,
                         command: str) -> tuple[bool, str]:
    """schtasks /Create /SC ONCE で登録。(成功, 失敗時のエラー文言)。"""
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
        return False, f"exception: {e}"
    if r.returncode == 0:
        return True, ""
    err = (r.stderr or "").strip() or (r.stdout or "").strip() or f"rc={r.returncode}"
    logging.warning("register failed %s: %s", task_name, err)
    return False, err


def register_one_shot(task_name: str, fire_at: dt.datetime, command: str) -> bool:
    return register_one_shot_ex(task_name, fire_at, command)[0]


# ── ログオン時の救済登録 (2026-11-12 監査 指摘4) ─────────────────────────────
# 07:00 の本体タスク (AutoraceDynamicScheduler, LogonType=Password) が作った
# AutoraceDyn_* を、ログオン時タスク (AutoraceDynSchedulerOnLogon, Interactive) は
# 削除も上書きもできず全件 Access denied で空振りしていた (7/19〜、rc=0・無通知)。
# 両タスクは同一コマンド・引数なしで区別できないため、時刻とマーカーで振る舞いを決める:
#   - 当日の登録成功マーカーあり → 「本日は登録済み」で何もしない
#   - マーカーなし & 定時 (07:00) 前 → 本体に任せて何もしない
#     (前日のミッドナイト R の未発火タスクを消さない)
#   - マーカーなし & 定時以降 → 通常登録 (本体の定時実行もここ)。本体が 07:00 を
#     逃した日のログオン時実行は救済登録になる
# 通常名が Access denied で上書きできない R は、同名タスクが既に同じ発火時刻で
# 登録済みなら登録済みとみなし、そうでなければ別名 (通常名 + ALT_SUFFIX、
# `^AutoraceDyn_` に一致) で XML 登録する (EndBoundary + DeleteExpiredTaskAfter で自動失効)。
# 1 件でも登録できなければ非 0 終了 + 要確認メール。全件登録できたらマーカーを書く。
STATE_FILE = DATA / "dynamic_scheduler_state.json"
MAIN_SCHEDULE_TIME = dt.time(7, 0)     # 本体 AutoraceDynamicScheduler の定時
# 「定時前」の判定は定時の 5 分前まで。本体の起動時刻が時計のずれで 07:00 より僅かに
# 前と読まれると、その日の登録が丸ごと行われなくなるため余裕を持たせる。
GATE_OPEN_TIME = dt.time(6, 55)
ALT_SUFFIX = "_alt"
ALT_END_AFTER = dt.timedelta(hours=2)  # 別名タスクのトリガー失効 = 発火 + 2 時間
ALT_DELETE_EXPIRED_AFTER = "PT1H"      # 失効後 1 時間でタスク自体を削除
_ACCESS_DENIED_MARKERS = ("access is denied", "access denied",
                          "アクセスが拒否", "アクセス拒否")


def _now() -> dt.datetime:
    """現在時刻 (テストで差し替える)。"""
    return dt.datetime.now()


def is_access_denied(text: str | None) -> bool:
    """schtasks のエラー文言が Access denied か (英語/日本語)。判別不能は False。"""
    t = (text or "").lower()
    return any(m.lower() in t for m in _ACCESS_DENIED_MARKERS)


def load_marker(today: dt.date, path: Path | None = None) -> dict | None:
    """当日の登録成功マーカー。無い/別日/壊れている場合は None。"""
    p = path or STATE_FILE
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return st if isinstance(st, dict) and st.get("date") == today.isoformat() else None


def write_marker(today: dt.date, now: dt.datetime, races: list[dict],
                 path: Path | None = None) -> None:
    """登録成功マーカーを atomic に書く (失敗はログのみ)。"""
    p = path or STATE_FILE
    body = {"date": today.isoformat(),
            "written_at": now.isoformat(timespec="seconds"),
            "n_registered": len(races), "races": races}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        logging.warning("登録マーカー書き込み失敗: %s", e)


def startup_gate(now: dt.datetime, marker: dict | None) -> str | None:
    """登録処理に進まない理由 (進むなら None)。"""
    if marker is not None:
        return (f"本日は登録済み (マーカー {marker.get('written_at')}、"
                f"{marker.get('n_registered')} 件) — 何もしない")
    if now.time() < GATE_OPEN_TIME:
        return (f"定時 {MAIN_SCHEDULE_TIME.strftime('%H:%M')} 前で登録記録なし — "
                f"本体の定時実行に任せて何もしない")
    return None


def existing_task_start(task_name: str) -> dt.datetime | None:
    """既存タスクのトリガー開始時刻 (StartBoundary)。無い/読めなければ None。"""
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/XML"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"<StartBoundary>([^<]+)</StartBoundary>", r.stdout or "")
    if not m:
        return None
    try:
        return dt.datetime.fromisoformat(m.group(1).strip()).replace(tzinfo=None)
    except ValueError:
        return None


def build_alt_task_xml(fire_at: dt.datetime, command: str) -> str:
    """別名タスクの XML (Interactive・自動失効付き)。command は CMD_TEMPLATE の展開結果。"""
    from xml.sax.saxutils import escape
    exe, _, args = command.partition(" ")
    start = fire_at.replace(second=0, microsecond=0)
    end = start + ALT_END_AFTER
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>AutoraceDyn rescue (dynamic_scheduler)</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start.isoformat()}</StartBoundary>
      <EndBoundary>{end.isoformat()}</EndBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>false</StartWhenAvailable>
    <DeleteExpiredTaskAfter>{ALT_DELETE_EXPIRED_AFTER}</DeleteExpiredTaskAfter>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(exe)}</Command><Arguments>{escape(args)}</Arguments></Exec>
  </Actions>
</Task>
"""


def register_alt_task(task_name: str, fire_at: dt.datetime,
                      command: str) -> tuple[bool, str]:
    """別名タスクを XML で登録。(成功, エラー文言)。"""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".xml")
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(build_alt_task_xml(fire_at, command))
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/XML", tmp, "/F"],
            capture_output=True, text=True, encoding="cp932", errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        return False, f"exception: {e}"
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if r.returncode == 0:
        return True, ""
    return False, ((r.stderr or "").strip() or (r.stdout or "").strip()
                   or f"rc={r.returncode}")


def register_race(task_name: str, fire_at: dt.datetime,
                  command: str) -> tuple[str, str, str]:
    """1 R を登録。(status, 実際のタスク名, エラー)。

    status: "registered" (通常名) / "existing" (同名・同時刻で登録済み) /
            "registered_alt" (別名) / "failed"。
    Access denied 以外の失敗 (判別不能を含む) は別名を試さず failed。
    """
    ok, err = register_one_shot_ex(task_name, fire_at, command)
    if ok:
        return "registered", task_name, ""
    if not is_access_denied(err):
        return "failed", task_name, err
    ex = existing_task_start(task_name)
    if ex is not None and abs((ex - fire_at.replace(second=0, microsecond=0))
                              .total_seconds()) < 60:
        return "existing", task_name, ""
    alt = task_name + ALT_SUFFIX
    ok2, err2 = register_alt_task(alt, fire_at, command)
    if ok2:
        return "registered_alt", alt, ""
    return "failed", task_name, f"{err} / 別名 {alt}: {err2}"


def notify_failures(today: dt.date, failed: list[str]) -> None:
    """登録できなかった R の要確認メール (送信失敗はログのみ)。"""
    try:
        from gmail_notify import send_email
        send_email(
            subject=(f"[autorace] ⚠️ 動的タスク登録 失敗 {len(failed)}R "
                     f"{today.isoformat()} (要確認)"),
            body=("dynamic_scheduler が次の R の発火タスクを登録できませんでした。\n"
                  "これらの R は予測・自動発注が行われません。\n\n"
                  + "\n".join(f"  - {x}" for x in failed)
                  + "\n\nログ: data/dynamic_scheduler.log\n"
                  "対処: タスクスケジューラで AutoraceDyn_* の状態を確認し、"
                  "必要なら AutoraceDynamicScheduler を手動実行してください。"))
    except Exception as e:  # noqa: BLE001
        logging.error("登録失敗メール送信失敗: %s", e)


def main() -> int:
    setup_logging()
    now = _now()
    today = now.date()
    logging.info("=== dynamic_scheduler start: %s (now=%s) ===",
                 today.isoformat(), now.strftime("%H:%M:%S"))

    # ログオン時実行の空振り防止 / 定時前の誤登録防止 (2026-11-12 監査 指摘4)
    skip_reason = startup_gate(now, load_marker(today))
    if skip_reason:
        logging.info("%s", skip_reason)
        return 0

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
        # マーカーは書かない: fetch_today_schedule は取得失敗でも {} を返すため、
        # 「開催なし」と通信エラーを区別できない。書くとその日の再実行・救済が
        # すべて「登録済み」で止まる。開催なしの日に再実行されても害は無い。
        return 0

    registered = 0
    skipped_past = 0
    skipped_other = 0
    marker_races: list[dict] = []   # マーカーに残す登録 R 一覧
    failed: list[str] = []          # 登録できなかった R (救済後も)

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
            last_r = max(race_starts)
            logging.info(
                "pc=%d (%s) Program/Print から R 毎発走時刻取得: R1=%s, R%d=%s (%d R)",
                pc, venue_key,
                race_starts.get(1).strftime("%H:%M") if 1 in race_starts else "?",
                last_r, race_starts[last_r].strftime("%H:%M"),
                len(race_starts),
            )
            if last_r < RACES_PER_DAY:
                # ミッドナイト 8R 開催など。実レース数で打ち切り、R9〜 は推定補間しない
                # (2026-09-04 監査 P3: 幽霊タスク R9-R12 対策)
                logging.info("pc=%d (%s) Program/Print は R%d まで — R%d 以降は登録しない "
                             "(推定補間なし)", pc, venue_key, last_r, last_r + 1)

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
            elif race_starts:
                # Program/Print が取れている場は実レース数で打ち切る (推定補間しない)
                skipped_other += 1
                continue
            elif anchor_time is not None:
                race_start = estimate_race_start(today, anchor_r, anchor_time,
                                                 race_no, interval)
                source = "estimate"
            else:
                skipped_other += 1
                continue
            lead_min = LEAD_MIN_VENUE_MAP.get(pc, LEAD_MIN_DEFAULT)
            fire_at = race_start - dt.timedelta(minutes=lead_min)

            if fire_at <= now + dt.timedelta(minutes=1):
                skipped_past += 1
                continue

            task_name = f"{TASK_PREFIX}{venue_short}_R{race_no}"
            label = f"{venue_short}_R{race_no}"
            command = CMD_TEMPLATE.format(
                vbs=VBS_WRAPPER, pc=pc, race_no=race_no, label=label,
                date=today.isoformat(),
            )

            status, actual_name, err = register_race(task_name, fire_at, command)
            if status == "failed":
                failed.append(f"{label} (発火 {fire_at.strftime('%m/%d %H:%M')}): "
                              f"{err[:200]}")
                continue
            # ログ行は通常名で書く (night_report が "registered AutoraceDyn_<場>_R<n> @ HH:MM"
            # を読む)。別名・登録済みの区別は括弧内に残す。
            note = {"registered": "",
                    "existing": ", 同名タスク登録済み (Access denied のため既存を使用)",
                    "registered_alt": f", alt={actual_name}"}[status]
            logging.info("registered %s @ %s (race=%s, lead=%dm, %s%s)",
                         task_name, fire_at.strftime("%H:%M"),
                         race_start.strftime("%H:%M"), lead_min, source, note)
            registered += 1
            marker_races.append({"race": label, "task": actual_name,
                                 "fire_at": fire_at.isoformat(timespec="minutes"),
                                 "status": status})

    logging.info("=== dynamic_scheduler done: registered=%d skipped_past=%d skipped_other=%d"
                 " failed=%d ===", registered, skipped_past, skipped_other, len(failed))
    if failed:
        # 救済後も登録できなかった R がある → マーカーを書かず (次回ログオン時に再試行)、
        # 非 0 終了 + 要確認メール (2026-11-12 監査 指摘4)
        logging.error("登録できなかった R: %d 件 — %s", len(failed), "; ".join(failed))
        notify_failures(today, failed)
        return 2
    write_marker(today, now, marker_races)
    return 0


if __name__ == "__main__":
    sys.exit(main())
