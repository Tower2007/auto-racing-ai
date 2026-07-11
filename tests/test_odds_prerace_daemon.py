"""odds_prerace_daemon の単体テスト (2026-07-03)。

pytest があれば `pytest tests/test_odds_prerace_daemon.py`、
無くても `python tests/test_odds_prerace_daemon.py` で実行可能。

オフラインで検証する対象:
  1. 新 CSV スキーマ (= odds_combo_snapshots + target_offset_min)
  2. build_snapshot_rows: 全券種の行構築 (tns/fns 縦持ち + offset 付与)
  3. build_events: T-5/T-1 の時刻計算と昇順ソート
  4. acquire_singleton: 単一インスタンスガード (named mutex 排他 +
     ガード故障時の fail-open が「二重起動」と区別されること)
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds_prerace_daemon as opd  # noqa: E402
from src.storage import CSV_SCHEMAS  # noqa: E402


# ─── 1. スキーマ ───────────────────────────────────────────────

def test_schema_is_snapshots_plus_offset():
    base = CSV_SCHEMAS["odds_combo_snapshots.csv"]
    prerace = CSV_SCHEMAS["odds_combo_prerace.csv"]
    assert prerace == base + ["target_offset_min"]
    # 既存スキーマが変わっていないこと (回帰ガード)
    assert base == [
        "race_date", "place_code", "race_no", "bet_type",
        "car_no_1", "car_no_2", "car_no_3",
        "odds", "odds_min", "odds_max", "captured_at",
    ]


# ─── 2. build_snapshot_rows ───────────────────────────────────

def _sample_odds_body() -> dict:
    """Odds API body の最小フィクスチャ (2車 + 各券種1点以上)。"""
    return {
        "playerList": [
            {"carNo": 1, "playerCode": "9999"},
            {"carNo": 2, "playerCode": "8888"},
        ],
        "tnsOddsList": {"1": "2.5", "2": "3.0"},
        "fnsOddsList": {"1": {"min": "1.1", "max": "1.5"},
                        "2": {"min": "1.3", "max": "2.0"}},
        "rtwOddsList": {"1": {"2": "5.6"}, "2": {"1": "8.2"}},
        "rfwOddsList": {"1": {"2": "4.1"}},
        "widOddsList": {"1": {"2": {"min": "1.8", "max": "2.2"}}},
        "rt3OddsList": {"1": {"2": {"3": "12.3"}}},
        "rf3OddsList": {"1": {"2": {"3": "6.7"}}},
    }


def test_build_snapshot_rows_all_bet_types():
    rows = opd.build_snapshot_rows(6, "2026-07-03", 5, _sample_odds_body(),
                                   off=5, captured_at="2026-07-03T12:38:00")
    by_type = {}
    for r in rows:
        by_type.setdefault(r["bet_type"], []).append(r)
    assert set(by_type) == {"tns", "fns", "rtw", "rfw", "wid", "rt3", "rf3"}
    assert len(by_type["tns"]) == 2
    assert len(by_type["fns"]) == 2
    assert len(by_type["rtw"]) == 2
    assert len(by_type["rt3"]) == 1
    # 全行に offset と captured_at が入る
    assert all(r["target_offset_min"] == 5 for r in rows)
    assert all(r["captured_at"] == "2026-07-03T12:38:00" for r in rows)
    # tns は odds、fns は odds_min/odds_max に値
    tns1 = next(r for r in by_type["tns"] if r["car_no_1"] == 1)
    assert tns1["odds"] == 2.5 and tns1["odds_min"] is None
    fns2 = next(r for r in by_type["fns"] if r["car_no_1"] == 2)
    assert fns2["odds_min"] == 1.3 and fns2["odds_max"] == 2.0
    # 全行が CSV スキーマ内のキーのみで構成される (DictWriter 安全)
    schema = set(CSV_SCHEMAS["odds_combo_prerace.csv"])
    for r in rows:
        assert set(r) <= schema, f"schema 外のキー: {set(r) - schema}"


def test_build_snapshot_rows_odds_not_published():
    """オッズ未公開 (list/None) は 0 行 — 例外を出さない。"""
    body = {"playerList": [], "tnsOddsList": [], "fnsOddsList": None,
            "rtwOddsList": [], "rt3OddsList": None}
    rows = opd.build_snapshot_rows(2, "2026-07-03", 1, body, off=1)
    assert rows == []


# ─── 3. build_events ──────────────────────────────────────────

def test_build_events_offsets_and_order():
    d = dt.date(2026, 7, 3)
    starts = {
        6: {1: dt.datetime.combine(d, dt.time(10, 56)),
            2: dt.datetime.combine(d, dt.time(11, 21))},
        3: {1: dt.datetime.combine(d, dt.time(15, 0))},
    }
    events = opd.build_events(starts)
    # 3 レース × 2 時点
    assert len(events) == 6
    # 昇順ソート
    times = [e[0] for e in events]
    assert times == sorted(times)
    # 先頭は sanyou R1 の T-5 (10:51)、次は T-1 (10:55)
    assert events[0] == (dt.datetime.combine(d, dt.time(10, 51)), 6, 1, 5)
    assert events[1] == (dt.datetime.combine(d, dt.time(10, 55)), 6, 1, 1)
    # 最後は isesaki R1 の T-1 (14:59)
    assert events[-1] == (dt.datetime.combine(d, dt.time(14, 59)), 3, 1, 1)


def test_build_events_midnight_crossover():
    """日跨ぎ (build_exact_race_starts が +1 日した datetime) もそのまま扱える。"""
    d = dt.date(2026, 7, 3)
    starts = {6: {12: dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0, 10))}}
    events = opd.build_events(starts)
    assert events[0][0] == dt.datetime(2026, 7, 4, 0, 5)   # T-5
    assert events[1][0] == dt.datetime(2026, 7, 4, 0, 9)   # T-1


# ─── 4. 単一インスタンスガード ─────────────────────────────────
# 2026-07-12: 固定ポート bind 方式 (58620) は Windows の動的除外ポート帯
# (WinNAT/Hyper-V の excludedportrange。誰も LISTEN していなくても bind が
# WinError 10013 で拒否される) に入ると「二重起動」と誤認して起動を拒否した
# ため、named mutex 方式 (auto_buy._acquire_lock と同型) に置換。
# テストもポート非依存になり除外帯 flake が消える。

IS_WINDOWS = os.name == "nt"


def _test_mutex_name() -> str:
    """テスト専用 mutex 名 (本番名 _mutex_name() と干渉しない)。"""
    import uuid
    return f"Global\\AutoRacingAIOddsPreraceTest_{uuid.uuid4().hex[:8]}"


def test_mutex_name_contains_project_hash():
    name = opd._mutex_name()
    assert name.startswith("Global\\AutoRacingAI_odds_prerace_")
    assert not name.endswith("_odds_prerace_")   # ハッシュが空でない
    # auto_buy の mutex 名前空間と衝突しない (収集と発注は独立に動いてよい)
    assert "_auto_buy_" not in name


def test_singleton_guard():
    """保持中は取得不可 (None)、解放後は再取得できる。

    named mutex は同一スレッドからは再帰取得できてしまうため、
    2 回目の取得は別スレッドから試す (test_auto_buy_lock.py と同じ手法。
    実運用の二重起動は別プロセス = 別スレッドなのでこれで等価)。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    import threading
    name = _test_mutex_name()
    results: dict[str, bool] = {}

    def _try(key: str) -> None:
        lk = opd.acquire_singleton(name)
        results[key] = lk is not None and lk._handle is not None
        if lk is not None:
            lk.close()

    lock1 = opd.acquire_singleton(name)
    assert lock1 is not None and lock1._handle is not None, "初回取得が成功する"
    try:
        t = threading.Thread(target=_try, args=("held",))
        t.start(); t.join(timeout=15)
        assert results.get("held") is False, "保持中の取得は None (二重起動検知)"
    finally:
        lock1.close()
    # 解放後は再取得できる (プロセス終了で OS が解放する挙動の等価確認)
    t2 = threading.Thread(target=_try, args=("after",))
    t2.start(); t2.join(timeout=15)
    assert results.get("after") is True, "解放後は再取得できる"


def test_singleton_guard_fail_open_on_mutex_error():
    """ガード自体の故障 (mutex 生成不可) は「二重起動」(None) と区別され、
    fail-open の no-op lock で収集を続行する — 旧実装が WinError 10013
    (除外帯) を二重起動と誤認して黙って止まった問題の回帰ガード。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    # オブジェクト名に許されない '\' 入りの名前で CreateMutexW を確実に失敗させる
    lock = opd.acquire_singleton("Global\\bad\\name\\odds_prerace_test")
    assert lock is not None, "ガード故障は起動拒否 (None) にしない"
    assert lock._handle is None, "fail-open は no-op lock (保証なしの明示)"
    lock.close()  # no-op でも安全に呼べる


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
