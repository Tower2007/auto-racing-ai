"""weekly_status の schtasks 健全性判定 (2026-11-12 監査 指摘7) のテスト。

実タスクスケジューラは読まない: evaluate_schtasks_rows に行と取りこぼし回数を直接渡す。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import weekly_status as ws  # noqa: E402


def _row(name, rc, last="2026/11/09 7:20:00"):
    return ("\\" + name, rc, last)


def test_running_self_is_not_ng():
    """実行中 (0x41301 = 267009) の自分自身 (WeeklyStatus) を NG に数えない。"""
    out = ws.evaluate_schtasks_rows([_row("AutoraceWeeklyStatus", 267009)], {})
    assert out["ng_count"] == 0
    assert out["tasks"][0]["running"] is True and out["tasks"][0]["ok"] is True


def test_not_yet_run_and_odds_prerace_refusal_are_ok():
    rows = [_row("AutoraceMonthlyReport", 267011),
            _row("AutoraceOddsPrerace", -2147020576),      # 0x800710E0 (負の 10 進)
            _row("AutoraceOddsPrerace", 0x800710E0)]       # 重複行 (トリガー数ぶん)
    out = ws.evaluate_schtasks_rows(rows, {})
    assert out["ng_count"] == 0
    assert [t["name"] for t in out["tasks"]] == ["AutoraceMonthlyReport",
                                                 "AutoraceOddsPrerace"]


def test_refusal_code_is_ng_for_other_tasks():
    out = ws.evaluate_schtasks_rows([_row("AutoraceNightReport", -2147020576)], {})
    assert out["ng_count"] == 1


def test_missed_runs_count_as_ng():
    rows = [_row("AutoraceFetchOrderHistory", 0), _row("AutoraceDailyIngest", 0)]
    out = ws.evaluate_schtasks_rows(rows, {"AutoraceFetchOrderHistory": 1})
    assert out["ng_count"] == 1
    t = {x["name"]: x for x in out["tasks"]}
    assert t["AutoraceFetchOrderHistory"]["missed"] == 1
    assert t["AutoraceFetchOrderHistory"]["ok"] is False
    assert ws._schtasks_mark(t["AutoraceFetchOrderHistory"]) == "🔴"
    assert t["AutoraceDailyIngest"]["ok"] is True


def test_rc_ng_and_missed_counted_once_per_task():
    out = ws.evaluate_schtasks_rows([_row("AutoraceNightReport", 1)],
                                    {"AutoraceNightReport": 2})
    assert out["ng_count"] == 1


def test_deleted_and_dynamic_tasks_are_not_monitored():
    rows = [_row("AutoraceMorningPredict", 1), _row("AutoraceNoonPredict", 1),
            _row("AutoraceEveningPredict", 1), _row("AutoraceDyn_iizuka_R3", 1),
            _row("AutoraceDyn_iizuka_R3_alt", 1), _row("AutoraceStreamlitApp", 1)]
    out = ws.evaluate_schtasks_rows(rows, {"AutoraceDyn_iizuka_R3": 5})
    assert out["tasks"] == [] and out["ng_count"] == 0


def test_monitored_list_covers_current_periodic_tasks():
    for name in ("AutoraceNightReport", "AutoraceOddsPrerace", "AutoraceMonthlyReport",
                 "AutoraceFetchOrderHistory", "AutoraceDynamicScheduler",
                 "AutoraceDynSchedulerOnLogon", "AutoraceWeeklyRetrain",
                 "AutoraceWeeklyStatus", "AutoraceDailyIngest"):
        assert name in ws.SCHTASKS_MONITORED
    for name in ("AutoraceMorningPredict", "AutoraceNoonPredict", "AutoraceEveningPredict"):
        assert name not in ws.SCHTASKS_MONITORED
