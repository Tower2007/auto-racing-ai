"""weekly_status の schtasks 健全性判定 (2026-11-12 監査 指摘7) のテスト。

a6d15ad (自タスクの実行中のみ正常、6 時間超は固着) と統合した仕様:
監視対象は現行タスクの完全一致、常駐タスクの実行中は正常、タスク別の追加正常コード、
取りこぼし (NumberOfMissedRuns) は NG。実タスクスケジューラは読まない。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import weekly_status as ws  # noqa: E402

NOW = dt.datetime(2026, 11, 16, 7, 20, 30)
RUNNING = ws.SCHED_S_TASK_RUNNING


def _row(name, rc, last="2026/11/16 7:20:00"):
    return ("\\" + name, rc, last)


def _eval(rows, missed=None):
    return ws.evaluate_schtasks_rows(rows, missed or {}, now=NOW)


def test_self_task_running_within_6h_is_ok():
    out = _eval([_row("AutoraceWeeklyStatus", RUNNING)])
    assert out["ng_count"] == 0 and out["tasks"][0]["state"] == "self_running"


def test_self_task_running_over_6h_is_ng():
    out = _eval([_row("AutoraceWeeklyStatus", RUNNING, "2026/11/09 7:20:00")])
    assert out["ng_count"] == 1 and out["tasks"][0]["state"] == "ng"


def test_resident_task_running_is_ok():
    out = _eval([_row("AutoraceOddsPrerace", RUNNING, "2026/11/16 7:05:01")])
    assert out["ng_count"] == 0
    assert out["tasks"][0]["state"] == "resident_running"
    mark, _, note = ws._task_view(out["tasks"][0])
    assert mark == "▶️" and "常駐" in note


def test_non_resident_task_running_is_ng():
    # 非常駐 (朝バッチ・vbs で即 return するコレクタ) の実行中は異常 (a6d15ad の方針)
    out = _eval([_row("AutoraceDailyIngest", RUNNING, "2026/11/16 6:30:01"),
                 _row("AutoraceOddsTsCollector", RUNNING, "2026/11/16 7:05:01")])
    assert out["ng_count"] == 2


def test_not_yet_run_and_odds_prerace_refusal_are_ok():
    rows = [_row("AutoraceMonthlyReport", 267011),
            _row("AutoraceOddsPrerace", -2147020576),      # 0x800710E0 (負の 10 進)
            _row("AutoraceOddsPrerace", 0x800710E0)]       # 重複行 (トリガー数ぶん)
    out = _eval(rows)
    assert out["ng_count"] == 0
    assert [t["name"] for t in out["tasks"]] == ["AutoraceMonthlyReport",
                                                 "AutoraceOddsPrerace"]


def test_refusal_code_is_ng_for_other_tasks():
    out = _eval([_row("AutoraceNightReport", -2147020576)])
    assert out["ng_count"] == 1


def test_missed_runs_count_as_ng():
    rows = [_row("AutoraceFetchOrderHistory", 0), _row("AutoraceDailyIngest", 0)]
    out = _eval(rows, {"AutoraceFetchOrderHistory": 1})
    assert out["ng_count"] == 1
    t = {x["name"]: x for x in out["tasks"]}
    assert t["AutoraceFetchOrderHistory"]["state"] == "missed"
    mark, _, note = ws._task_view(t["AutoraceFetchOrderHistory"])
    assert mark == "🔴" and "取りこぼし 1 回" in note
    assert t["AutoraceDailyIngest"]["state"] == "ok"


def test_rc_ng_and_missed_counted_once_per_task():
    out = _eval([_row("AutoraceNightReport", 1)], {"AutoraceNightReport": 2})
    assert out["ng_count"] == 1 and out["tasks"][0]["state"] == "ng"


def test_deleted_and_dynamic_tasks_are_not_monitored():
    rows = [_row("AutoraceMorningPredict", 1), _row("AutoraceNoonPredict", 1),
            _row("AutoraceEveningPredict", 1), _row("AutoraceDyn_iizuka_R3", 1),
            _row("AutoraceDyn_iizuka_R3_alt", 1), _row("AutoraceStreamlitApp", 1)]
    out = _eval(rows, {"AutoraceDyn_iizuka_R3": 5})
    assert out["tasks"] == [] and out["ng_count"] == 0


def test_monitored_list_covers_current_periodic_tasks():
    for name in ("AutoraceNightReport", "AutoraceOddsPrerace", "AutoraceMonthlyReport",
                 "AutoraceFetchOrderHistory", "AutoraceDynamicScheduler",
                 "AutoraceDynSchedulerOnLogon", "AutoraceWeeklyRetrain",
                 "AutoraceWeeklyStatus", "AutoraceDailyIngest"):
        assert name in ws.SCHTASKS_MONITORED
    for name in ("AutoraceMorningPredict", "AutoraceNoonPredict", "AutoraceEveningPredict"):
        assert name not in ws.SCHTASKS_MONITORED
    assert set(ws.RESIDENT_TASKS) <= set(ws.SCHTASKS_MONITORED)


def test_single_view_function():
    # 表示の分類は _task_view 1 系統 (旧 _schtasks_mark は残さない)
    assert not hasattr(ws, "_schtasks_mark")
