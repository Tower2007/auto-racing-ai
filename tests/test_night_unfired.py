"""夜次レポートの未発火検知 (2026-11-12 監査 指摘5) のテスト。

dynamic_scheduler.log の登録行と daily_predict.log / dynamic_run.log の起動行を
tmp に作って突合する。実ログ・メールには触れない。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import night_report as nr  # noqa: E402

DAY = "2026-10-30"
SCHED = """\
2026-10-29 07:00:12,000 INFO registered AutoraceDyn_hamamatsu_R1 @ 10:28 (race=10:32, lead=4m, exact)
2026-10-30 07:00:11,913 INFO registered AutoraceDyn_hamamatsu_R1 @ 10:28 (race=10:32, lead=4m, exact)
2026-10-30 07:00:11,950 INFO registered AutoraceDyn_hamamatsu_R2 @ 10:54 (race=10:58, lead=4m, exact)
2026-10-30 07:00:12,250 INFO registered AutoraceDyn_hamamatsu_R10 @ 14:58 (race=15:02, lead=4m, exact)
2026-10-30 07:00:12,300 INFO registered AutoraceDyn_iizuka_R7 @ 00:01 (race=00:04, lead=3m, exact)
2026-10-30 14:50:37,283 WARNING register failed AutoraceDyn_hamamatsu_R10: ERROR: Access is denied.
2026-10-30 14:50:38,000 INFO registered AutoraceDyn_sanyou_R3 @ 20:30 (race=20:34, lead=4m, exact, alt=AutoraceDyn_sanyou_R3_alt)
"""


def _setup(tmp_path, monkeypatch, fired_lines: list[str]):
    sched = tmp_path / "dynamic_scheduler.log"
    sched.write_text(SCHED, encoding="utf-8")
    pl = tmp_path / "daily_predict.log"
    pl.write_text("\n".join(fired_lines) + "\n", encoding="utf-8")
    rl = tmp_path / "dynamic_run.log"
    rl.write_text("", encoding="utf-8")
    monkeypatch.setattr(nr, "DYN_SCHED_LOG", sched)
    monkeypatch.setattr(nr, "PREDICT_LOGS", (pl, rl))


def _start(date, label, ts="2026-10-30 14:58:13,774"):
    return f"{ts} INFO === daily_predict start: date={date} venues=[4] time={label} thr=1.50 ==="


def test_unfired_detected(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [
        _start(DAY, "hamamatsu_R10"),
        _start(DAY, "sanyou_R3"),
        _start("2026-10-29", "hamamatsu_R2"),           # 別日の起動は数えない
    ])
    now = dt.datetime(2026, 10, 31, 0, 0, 2)
    out = nr.load_unfired_races(DAY, now)
    assert out == ["浜松 R1 (10:28 発火予定)", "浜松 R2 (10:54 発火予定)"]


def test_no_unfired_returns_empty(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [
        _start(DAY, "hamamatsu_R1"), _start(DAY, "hamamatsu_R2"),
        _start(DAY, "hamamatsu_R10"), _start(DAY, "sanyou_R3"),
    ])
    assert nr.load_unfired_races(DAY, dt.datetime(2026, 10, 31, 0, 0, 2)) == []


def test_future_midnight_race_not_counted(tmp_path, monkeypatch):
    """00:01 発火 (日跨ぎ) の飯塚 R7 は夜次 (00:00:02) 時点で未到来 → 数えない。
    発火時刻を過ぎていれば (= 翌日に再集計すれば) 未発火に数える。"""
    _setup(tmp_path, monkeypatch, [
        _start(DAY, "hamamatsu_R1"), _start(DAY, "hamamatsu_R2"),
        _start(DAY, "hamamatsu_R10"), _start(DAY, "sanyou_R3"),
    ])
    assert nr.load_unfired_races(DAY, dt.datetime(2026, 10, 31, 0, 0, 2)) == []
    later = nr.load_unfired_races(DAY, dt.datetime(2026, 10, 31, 0, 10, 0))
    assert later == ["飯塚 R7 (00:01 発火予定)"]


def test_just_fired_race_within_grace_not_counted(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [
        _start(DAY, "hamamatsu_R1"), _start(DAY, "hamamatsu_R2"),
        _start(DAY, "sanyou_R3"),
    ])
    # R10 発火 14:58、1 分後はまだ起動ログが無くても数えない (猶予)
    assert nr.load_unfired_races(DAY, dt.datetime(2026, 10, 30, 14, 59)) == []


def test_subject_and_notes(tmp_path):
    today = {"date": DAY, "n": 1, "n_settled": 1, "n_pending": 0, "n_hit": 1,
             "roi": 130.0, "profit": 30,
             "unfired": ["浜松 R1 (10:28 発火予定)", "浜松 R2 (10:54 発火予定)"]}
    assert "⚠️未発火2" in nr.build_subject(DAY, today, 0)
    notes = nr._scope_notes(today)
    assert any("未発火 2R" in n and "未ログオン" in n for n in notes)
    today["unfired"] = []
    assert "未発火" not in nr.build_subject(DAY, today, 0)
    assert not any("未発火" in n for n in nr._scope_notes(today))
