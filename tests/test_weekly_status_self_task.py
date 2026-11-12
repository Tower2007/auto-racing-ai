"""weekly_status: 自タスクの「実行中」を NG に数えない (2026-11-12)。

レポート生成中は AutoraceWeeklyStatus 自身が必ず rc=267009 (実行中) に見える。
これを NG に数えていたため、全項目正常でも 2026-08-10〜11-09 の毎週月曜に
件名が 🟡WARN (health: schtasks NG 1 件) になっていた。

pytest があれば `pytest tests/test_weekly_status_self_task.py`、
無ければ `python tests/test_weekly_status_self_task.py`。
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import weekly_status as ws  # noqa: E402

NOW = dt.datetime(2026, 11, 9, 7, 20, 30)
RUNNING = ws.SCHED_S_TASK_RUNNING


def test_self_task_running_is_not_ng():
    state = ws.classify_task_result("AutoraceWeeklyStatus", RUNNING,
                                    "2026/11/09 7:20:00", now=NOW)
    assert state == "self_running", state


def test_other_task_running_is_still_ng():
    # 06:30 起動の取込が 07:20 にまだ実行中 = 朝バッチが終わっていない兆候
    state = ws.classify_task_result("AutoraceDailyIngest", RUNNING,
                                    "2026/11/09 6:30:01", now=NOW)
    assert state == "ng", state


def test_self_task_stuck_since_last_week_is_ng():
    state = ws.classify_task_result("AutoraceWeeklyStatus", RUNNING,
                                    "2026/11/02 7:20:00", now=NOW)
    assert state == "ng", state


def test_self_task_real_failure_is_ng():
    state = ws.classify_task_result("AutoraceWeeklyStatus", 1,
                                    "2026/11/09 7:20:00", now=NOW)
    assert state == "ng", state


def test_unparseable_last_run_does_not_raise_false_warn():
    state = ws.classify_task_result("AutoraceWeeklyStatus", RUNNING, "N/A", now=NOW)
    assert state == "self_running", state


def test_us_locale_last_run_is_parsed():
    assert ws._parse_schtasks_time("11/9/2026 7:20:00 AM") == dt.datetime(2026, 11, 9, 7, 20)
    state = ws.classify_task_result("AutoraceWeeklyStatus", RUNNING,
                                    "11/2/2026 7:20:00 AM", now=NOW)
    assert state == "ng", state


def test_ok_and_not_run_are_unchanged():
    assert ws.classify_task_result("AutoraceDailyIngest", 0) == "ok"
    assert ws.classify_task_result("AutoraceNoonPredict",
                                   ws.SCHED_S_TASK_HAS_NOT_RUN) == "not_run"


def _monday_schtasks() -> dict:
    """2026-11-09 の実メールと同じ schtasks 状態 (自タスクだけ実行中)。"""
    rows = [("AutoraceDailyIngest", 0, "2026/11/09 6:30:01"),
            ("AutoraceDynamicScheduler", 0, "2026/11/09 7:00:01"),
            ("AutoraceFetchOrderHistory", 0, "2026/11/09 2:30:01"),
            ("AutoraceWeeklyRetrain", 0, "2026/11/08 3:00:01"),
            ("AutoraceWeeklyStatus", RUNNING, "2026/11/09 7:20:00")]
    tasks = [{"name": n, "result": rc, "last_run": lr,
              "state": ws.classify_task_result(n, rc, lr, now=NOW)}
             for n, rc, lr in rows]
    return {"tasks": tasks, "warnings": [], "snapshot_path": None,
            "ng_count": sum(1 for t in tasks if t["state"] == "ng")}


def test_all_healthy_monday_is_overall_ok():
    st = _monday_schtasks()
    assert st["ng_count"] == 0
    days = [{"status": "OK"} for _ in range(7)]
    bh = {"status": "OK", "alerts": []}
    overall = ws.compute_overall_status(days, [], bh, st)
    assert overall["level"] == "OK", overall
    assert overall["badge"] == "🟢OK", overall["badge"]


def test_real_task_failure_still_warns():
    st = _monday_schtasks()
    st["tasks"][0].update(result=1, state="ng")
    st["ng_count"] = 1
    days = [{"status": "OK"} for _ in range(7)]
    overall = ws.compute_overall_status(days, [], {"status": "OK", "alerts": []}, st)
    assert overall["level"] == "WARN", overall
    assert "schtasks NG 1 件" in overall["health_reason"], overall["health_reason"]


def test_self_running_row_is_marked_as_running_not_red():
    mark, _, note = ws._task_view(_monday_schtasks()["tasks"][-1])
    assert mark == "⏳" and "本レポート自身" in note, (mark, note)


def _run_all() -> bool:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {fn.__name__}: {e!r}")
    print(f"\n{passed}/{len(fns)} passed")
    return passed == len(fns)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(0 if _run_all() else 1)
