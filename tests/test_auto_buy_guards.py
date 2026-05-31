"""auto_buy.check_guards の単体テスト (2026-05-31)。

pytest があれば `pytest tests/test_auto_buy_guards.py`、
無くても `python tests/test_auto_buy_guards.py` で実行可能。

ブリーフ要件 (Opinion/codex_briefs/2026-05-31_auto_buy_phase1.md):
  時間帯外 / 1日上限超過 / 累積損失停止 / EV異常値 / 連続失敗停止 → skip
  全条件満たす → ok
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_buy  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))

# 夜間 (許可時間帯内) と 昼 (時間帯外) の基準時刻
NIGHT = dt.datetime(2026, 6, 1, 23, 30, tzinfo=JST)   # 23:30 → in (22-6)
DAY = dt.datetime(2026, 6, 1, 12, 0, tzinfo=JST)      # 12:00 → out


def _state(spent=0, profit=0, fails=0):
    return {"date": "2026-06-01", "spent_yen": spent,
            "profit_yen": profit, "consecutive_failures": fails,
            "executions": []}


def test_in_buy_hours_wraparound():
    assert auto_buy.in_buy_hours(NIGHT, 22, 6) is True
    assert auto_buy.in_buy_hours(DAY, 22, 6) is False
    # 境界: 22:00 in, 06:00 out
    assert auto_buy.in_buy_hours(dt.datetime(2026, 6, 1, 22, 0, tzinfo=JST), 22, 6) is True
    assert auto_buy.in_buy_hours(dt.datetime(2026, 6, 1, 6, 0, tzinfo=JST), 22, 6) is False
    assert auto_buy.in_buy_hours(dt.datetime(2026, 6, 1, 5, 59, tzinfo=JST), 22, 6) is True


def test_skip_hours():
    ok, reason = auto_buy.check_guards(_state(), DAY, 300, 1.9)
    assert ok is False and "hours" in reason


def test_skip_ev_anomaly():
    ok, reason = auto_buy.check_guards(_state(), NIGHT, 300, 99.0, ev_cap=10.0)
    assert ok is False and "ev_anomaly" in reason


def test_skip_daily_cap():
    # 既出1800 + 300 = 2100 > 2000
    ok, reason = auto_buy.check_guards(_state(spent=1800), NIGHT, 300, 1.9,
                                       max_daily_yen=2000)
    assert ok is False and "daily_cap" in reason


def test_skip_loss_stop():
    ok, reason = auto_buy.check_guards(_state(profit=-1600), NIGHT, 300, 1.9,
                                       loss_stop_yen=-1500)
    assert ok is False and "loss_stop" in reason


def test_skip_consecutive_failures():
    ok, reason = auto_buy.check_guards(_state(fails=3), NIGHT, 300, 1.9,
                                       consecutive_stop=3)
    assert ok is False and "failures" in reason


def test_all_pass():
    ok, reason = auto_buy.check_guards(_state(spent=600, profit=-200, fails=1),
                                       NIGHT, 300, 1.9)
    assert ok is True and reason == "ok"


def test_cap_boundary_exact():
    # 1700 + 300 = 2000 ちょうどは OK (超過のみ skip)
    ok, _ = auto_buy.check_guards(_state(spent=1700), NIGHT, 300, 1.9,
                                  max_daily_yen=2000)
    assert ok is True


def test_build_bets():
    assert auto_buy.build_bets(5, 300, None, include_rt3=False) == \
        [{"type": "fns", "cars": [5], "amount": 300}]
    bets = auto_buy.build_bets(
        5, 300, {"cars_ordered": [5, 6, 7], "cars_sorted": [5, 6, 7]},
        include_rt3=True)
    assert len(bets) == 3
    assert bets[1]["type"] == "rt3" and bets[1]["amount"] == 100
    assert bets[2]["type"] == "rf3" and bets[2]["amount"] == 100
    # include_rt3=False なら rt3_ref があっても複勝のみ
    assert len(auto_buy.build_bets(
        5, 300, {"cars_ordered": [5, 6, 7], "cars_sorted": [5, 6, 7]},
        include_rt3=False)) == 1


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
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
