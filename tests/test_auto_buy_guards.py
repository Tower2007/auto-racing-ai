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


def test_anytime_default_ignores_hours():
    # デフォルト anytime=True: 昼でも時間帯では skip しない
    ok, reason = auto_buy.check_guards(_state(), DAY, 300, 1.9)
    assert ok is True and reason == "ok"


def test_skip_hours_when_anytime_false():
    # anytime=False の時のみ夜間限定ガードが効く
    ok, reason = auto_buy.check_guards(_state(), DAY, 300, 1.9, anytime=False)
    assert ok is False and "hours" in reason
    # 夜間なら通る
    ok2, _ = auto_buy.check_guards(_state(), NIGHT, 300, 1.9, anytime=False)
    assert ok2 is True


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
    # 2026-07-18〜 複勝 (fns) デフォルト ON (AUTO_BUY_INCLUDE_FNS=True、娯楽目的で復活):
    # 三連系対象外でも複勝 1 点は返る (2026-06-26〜07-18 の「デフォルト OFF → 空 list」は旧仕様)
    assert auto_buy.build_bets(5, 300, None, include_rt3=False) == [
        {"type": "fns", "cars": [5], "amount": 300}]
    # 複勝 OFF (明示) × 三連系対象外: 空 list
    assert auto_buy.build_bets(5, 300, None, include_rt3=False, include_fns=False) == []
    # 複勝 OFF (明示) × 三連系 ON: rt3+rf3 のみ (fns なし)
    bets = auto_buy.build_bets(
        5, 300, {"cars_ordered": [5, 6, 7], "cars_sorted": [5, 6, 7]},
        include_rt3=True, include_fns=False)
    assert [b["type"] for b in bets] == ["rt3", "rf3"]
    assert all(b["amount"] == 100 for b in bets)
    # has_rt3=False (伊勢崎・飯塚) は rf3 のみ
    assert [b["type"] for b in auto_buy.build_bets(
        5, 300, {"cars_ordered": [5, 6, 7], "cars_sorted": [5, 6, 7],
                 "has_rt3": False}, include_rt3=True, include_fns=False)] == ["rf3"]
    # 複勝 ON (既定) × 三連系 ON: 複勝先頭 + 三連系
    bets_fns = auto_buy.build_bets(
        5, 300, {"cars_ordered": [5, 6, 7], "cars_sorted": [5, 6, 7]},
        include_rt3=True)
    assert bets_fns[0] == {"type": "fns", "cars": [5], "amount": 300}
    assert len(bets_fns) == 3
    # 全 OFF (複勝 OFF × 三連系 OFF) は空
    assert auto_buy.build_bets(
        5, 300, {"cars_ordered": [5, 6, 7], "cars_sorted": [5, 6, 7]},
        include_rt3=False, include_fns=False) == []


def test_skip_duplicate_race_in_same_day_state():
    """2026-09-04 監査 P2: 当日 state に同一 race の executed/dry_run があれば
    _run_auto_buy_locked が skip_duplicate で二重発注しない (boat と同型の冪等ガード)。
    実発注 / メール / state 保存は全てスタブ。"""
    saved = []
    orig = {k: getattr(auto_buy, k) for k in (
        "load_state", "save_state", "today_profit_from_history", "check_guards",
        "rt3_final_gate_blocks", "_notify", "_run_execute_purchase")}
    try:
        st = _state()
        st["executions"] = [
            {"race": "iizuka_R7", "amount": 300, "verdict": "executed",
             "timestamp": "2026-06-01T23:00:00+09:00"},
            {"race": "iizuka_R8", "amount": 300, "verdict": "skip_daily_cap",
             "timestamp": "2026-06-01T23:10:00+09:00"},
        ]
        auto_buy.load_state = lambda now=None: st
        auto_buy.save_state = lambda s: saved.append(dict(s))
        auto_buy.today_profit_from_history = lambda d: 0
        auto_buy.check_guards = lambda *a, **kw: (True, "ok")
        auto_buy.rt3_final_gate_blocks = lambda bets: False
        auto_buy._notify = lambda subject, body: None

        def _forbidden(*a, **kw):
            raise AssertionError("実発注経路が呼ばれた")
        auto_buy._run_execute_purchase = _forbidden

        def cand(rno):
            return {"race_date": "2026-06-01", "place_code": 5,
                    "venue": "iizuka", "venue_jp": "飯塚", "race_no": rno,
                    "car_no": 5, "ev": 1.9, "amount": 300,
                    "bets": [{"type": "fns", "cars": [5], "amount": 300}]}
        out = auto_buy._run_auto_buy_locked([cand(7), cand(8)], NIGHT, dry_run=True)
        verdicts = {r["race"]: r["verdict"] for r in out}
        # R7 は executed 済 → skip_duplicate、R8 は skip 記録しか無い → 再評価 (dry_run)
        assert verdicts == {"iizuka_R7": "skip_duplicate", "iizuka_R8": "dry_run"}, verdicts
        assert st["spent_yen"] == 300  # R8 の模擬加算のみ (R7 は加算しない)
        assert [e["race"] for e in st["executions"]].count("iizuka_R7") == 1
    finally:
        for k, v in orig.items():
            setattr(auto_buy, k, v)


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
