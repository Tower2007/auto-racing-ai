"""dynamic_scheduler の日跨ぎミッドナイト対応 (2026-09-04 監査 P1/P3) の回帰テスト。

  P1: one-shot コマンドに開催日 {date} が含まれる (vbs → daily_predict --date)
  P3: Program/Print の '24:04' 表記を (00:04, +1 日) として扱い、
      8R 開催で R9-R12 の幽霊タスクを推定登録しない

pytest があれば `pytest tests/test_dynamic_scheduler.py`、
無くても `python tests/test_dynamic_scheduler.py` で実行可能。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dynamic_scheduler as ds  # noqa: E402

TODAY = dt.date(2026, 9, 2)
# 2026-09-02 飯塚ミッドナイト (Program/Print 実データ): 8R 開催、R7〜 が 24 時台表記
IIZUKA_MIDNIGHT = {1: "21:28", 2: "21:54", 3: "22:20", 4: "22:46",
                   5: "23:12", 6: "23:38", 7: "24:04", 8: "24:30"}


def test_parse_hhmm_ex_handles_24h_notation():
    assert ds.parse_hhmm_ex("21:28") == (dt.time(21, 28), 0)
    assert ds.parse_hhmm_ex("24:04") == (dt.time(0, 4), 1)
    assert ds.parse_hhmm_ex("24:50") == (dt.time(0, 50), 1)
    assert ds.parse_hhmm_ex("25:10") == (dt.time(1, 10), 1)
    # 不正値は None (従来どおり)
    assert ds.parse_hhmm_ex(None) is None
    assert ds.parse_hhmm_ex("") is None
    assert ds.parse_hhmm_ex("2130") is None
    assert ds.parse_hhmm_ex("ab:cd") is None
    assert ds.parse_hhmm_ex("21:60") is None
    assert ds.parse_hhmm_ex("48:00") is None


def test_parse_hhmm_wraps_24h_for_legacy_callers():
    # odds_prerace_daemon / odds_timeseries_collector が使う旧 API は time のみ返す
    assert ds.parse_hhmm("24:50") == dt.time(0, 50)
    assert ds.parse_hhmm("23:38") == dt.time(23, 38)
    assert ds.parse_hhmm("x") is None


def test_build_exact_race_starts_midnight_crossing():
    starts = ds.build_exact_race_starts(IIZUKA_MIDNIGHT, TODAY)
    assert sorted(starts) == list(range(1, 9))  # 8R ぶん全部 (R7/R8 が落ちない)
    assert starts[6] == dt.datetime(2026, 9, 2, 23, 38)
    assert starts[7] == dt.datetime(2026, 9, 3, 0, 4)   # +1 日
    assert starts[8] == dt.datetime(2026, 9, 3, 0, 30)
    # 単調増加
    vals = [starts[r] for r in sorted(starts)]
    assert vals == sorted(vals)


def test_build_exact_race_starts_first_race_after_midnight():
    # R1 から 24 時台 (理論上) でも +1 日される
    starts = ds.build_exact_race_starts({1: "24:10", 2: "24:36"}, TODAY)
    assert starts[1] == dt.datetime(2026, 9, 3, 0, 10)
    assert starts[2] == dt.datetime(2026, 9, 3, 0, 36)


def test_estimate_interval_accepts_24h_live_end():
    # liveEndTime='24:50' (旧: parse_hhmm が None → DEFAULT 30 分補間) が
    # 実 span から間隔推定できる (anchor R1@21:28 → R12@00:45 = 197 分 / 11)
    end = ds.parse_hhmm("24:50")
    iv = ds.estimate_interval_min(TODAY, 1, dt.time(21, 28), end)
    assert abs(iv - 197 / 11) < 1e-6


def test_cmd_template_carries_race_date():
    cmd = ds.CMD_TEMPLATE.format(vbs=ds.VBS_WRAPPER, pc=5, race_no=7,
                                 label="iizuka_R7", date=TODAY.isoformat())
    assert cmd.endswith(' 5 7 "iizuka_R7" 2026-09-02')
    assert "run_predict_hidden.vbs" in cmd


def test_no_ghost_races_when_program_print_available():
    """main() のループ規則を関数化せず再現: race_starts が非空なら推定しない。"""
    starts = ds.build_exact_race_starts(IIZUKA_MIDNIGHT, TODAY)
    anchor_time = dt.time(21, 28)
    registered, skipped = [], []
    for race_no in range(1, ds.RACES_PER_DAY + 1):
        if race_no in starts:
            registered.append(race_no)
        elif starts:
            skipped.append(race_no)
        elif anchor_time is not None:  # pragma: no cover (fallback 経路)
            registered.append(race_no)
    assert registered == list(range(1, 9))
    assert skipped == [9, 10, 11, 12]


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
