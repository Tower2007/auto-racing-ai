"""メール ROI 整合監査 (2026-11-11) の回帰テスト。

- 夜次の暫定精算が返還 (欠車・不成立) を払戻に数え、的中には数えない
  (11/06 山陽 R3: 暫定 ¥-900 → 正 ¥-600 の再現)
- 夜次の対象日は race_date 基準 (0:00〜4:59 発売は前日開催のミッドナイト)
- 夜次の件名: 購入なし / ±0 を 🟢「プラス」にしない
- picks_audit: (race, car) で重複除去、払戻行が空の不成立レースは除外
- 取込窓: 前回成功日から (固定 2 日窓の取りこぼし防止)

`python tests/test_mail_roi_consistency.py` でも実行可能。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import night_report as nr  # noqa: E402
import picks_audit as pa  # noqa: E402
import daily_ingest as di  # noqa: E402
import daily_fetch_order_history as dfo  # noqa: E402


# ── 11/06 山陽 R3 の RaceRefund 実応答 (必要部分) ─────────────────────────
RI_SANYO_1106_R3 = {
    "absent": [6],
    "fns": {"typeCode": 0, "typeName": "通常", "list": [
        {"carNo": 8, "refund": 100}, {"carNo": 7, "refund": 300},
        {"carNo": 2, "refund": 180}]},
    "rt3": {"typeCode": 0, "typeName": "通常", "list": [
        {"1thCarNo": 8, "2thCarNo": 7, "3thCarNo": 2, "refund": 7120}]},
    "rf3": {"typeCode": 0, "typeName": "通常", "list": [
        {"1thCarNo": 8, "2thCarNo": 7, "3thCarNo": 2, "refund": 1500}]},
}
RI_VOID = {  # 6/27 伊勢崎 R4 型: 競走不成立
    "absent": [1, 2, 3, 4, 5, 6, 7, 8],
    "fns": {"typeCode": 3, "typeName": "不成立",
            "list": [{"carNo": None, "refund": None}]},
}


def _bets(desc: str) -> list[dict]:
    return nr.parse_bets_desc(desc)


def test_absent_car_is_refunded_not_hit():
    bets = _bets("複勝 6号 ¥100 / 三連単 6→8→2 ¥100 / 三連複 2=6=8 ¥100")
    got = [nr.settle_bet_detail(b, RI_SANYO_1106_R3) for b in bets]
    assert got == [(100, "henkan")] * 3, got


def test_normal_hit_and_miss_unchanged():
    hit = nr.settle_bet_detail(_bets("複勝 7号 ¥200")[0], RI_SANYO_1106_R3)
    miss = nr.settle_bet_detail(_bets("複勝 1号 ¥100")[0], RI_SANYO_1106_R3)
    assert hit == (600, "hit"), hit
    assert miss == (0, "miss"), miss


def test_void_race_is_refunded():
    got = nr.settle_bet_detail(_bets("複勝 5号 ¥100")[0], RI_VOID)
    assert got == (100, "henkan"), got


def _evaluate(date: str, execs: list[dict], refunds: dict) -> dict:
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    tmp.write_text(json.dumps({"date": date, "executions": execs},
                              ensure_ascii=False), encoding="utf-8")
    orig = (nr.STATE_FILE, nr.fetch_refunds, nr.load_shadow_today,
            nr.load_final_fns_odds, nr.load_after_midnight_races)
    try:
        nr.STATE_FILE = tmp
        nr.fetch_refunds = lambda pcs, today: refunds
        nr.load_shadow_today = lambda today: {}
        nr.load_final_fns_odds = lambda today: {}
        nr.load_after_midnight_races = lambda today: []
        today, _ = nr.evaluate_today(date)
    finally:
        (nr.STATE_FILE, nr.fetch_refunds, nr.load_shadow_today,
         nr.load_final_fns_odds, nr.load_after_midnight_races) = orig
    return today


def test_1106_sanyo_r3_reproduces_confirmed_minus_600():
    """返還 ¥300 が損失扱いだった (暫定 ¥-900)。確定 (bet_history) は ¥-600。"""
    miss = {"absent": [], "fns": {"typeCode": 0, "list": [{"carNo": 9, "refund": 100}]},
            "rt3": {"typeCode": 0, "list": []}, "rf3": {"typeCode": 0, "list": []}}
    hit1 = {"absent": [], "fns": {"typeCode": 0, "list": [{"carNo": 7, "refund": 100}]}}
    hit8 = {"absent": [], "fns": {"typeCode": 0, "list": [{"carNo": 8, "refund": 100}]}}
    ts = "2026-11-06T12:00:00+09:00"
    execs = [
        {"race": "sanyou_R1", "verdict": "executed", "bets": "複勝 7号 ¥100", "timestamp": ts},
        {"race": "sanyou_R3", "verdict": "executed",
         "bets": "複勝 6号 ¥100 / 三連単 6→8→2 ¥100 / 三連複 2=6=8 ¥100", "timestamp": ts},
        {"race": "sanyou_R6", "verdict": "executed",
         "bets": "複勝 1号 ¥100 / 三連単 1→8→6 ¥100 / 三連複 1=6=8 ¥100", "timestamp": ts},
        {"race": "sanyou_R10", "verdict": "executed",
         "bets": "複勝 5号 ¥100 / 三連単 5→1→7 ¥100 / 三連複 1=5=7 ¥100", "timestamp": ts},
        {"race": "isesaki_R5", "verdict": "executed", "bets": "複勝 8号 ¥100", "timestamp": ts},
    ]
    refunds = {(6, 1): hit1, (6, 3): RI_SANYO_1106_R3, (6, 6): miss,
               (6, 10): miss, (3, 5): hit8}
    t = _evaluate("2026-11-06", execs, refunds)
    assert t["stake"] == 1100 and t["payout"] == 500, (t["stake"], t["payout"])
    assert t["profit"] == -600, t["profit"]
    assert t["n_hit"] == 2, t["n_hit"]   # 返還は的中に数えない


def test_exec_race_date_midnight_is_previous_day():
    assert nr.exec_race_date("2026-11-10T00:27:07+09:00", "2026-11-10") == "2026-11-09"
    assert nr.exec_race_date("2026-11-10T10:53:08+09:00", "2026-11-10") == "2026-11-10"
    assert nr.exec_race_date(None, "2026-11-10") == "2026-11-10"


def test_previous_day_midnight_is_excluded_from_today():
    """11/11 00:00 の夜次 (対象 11/10) が飯塚 R8 (11/09 開催) を結果待ちにしない。"""
    fns = {"absent": [], "fns": {"typeCode": 0, "list": [{"carNo": 7, "refund": 100}]}}
    execs = [
        {"race": "iizuka_R8", "verdict": "executed", "bets": "複勝 4号 ¥100",
         "timestamp": "2026-11-10T00:27:07+09:00"},
        {"race": "kawaguchi_R1", "verdict": "executed", "bets": "複勝 7号 ¥100",
         "timestamp": "2026-11-10T10:53:08+09:00"},
    ]
    t = _evaluate("2026-11-10", execs, {(2, 1): fns})
    assert t["n"] == 1 and t["n_pending"] == 0, (t["n"], t["n_pending"])
    assert t["carried_prev"] == ["飯塚 R8"], t["carried_prev"]


def _today(n, n_settled, profit, roi=100.0, n_hit=0, n_pending=0):
    return {"n": n, "n_settled": n_settled, "profit": profit, "roi": roi,
            "n_hit": n_hit, "n_pending": n_pending}


def test_subject_no_purchase_and_zero():
    s0 = nr.build_subject("2026-11-02", _today(0, 0, 0, roi=0.0), 0)
    assert s0.startswith("[autorace] 🌙 夜次 2026-11-02 ") and "購入なし" in s0, s0
    assert "🔴" not in s0 and "ROI" not in s0, s0
    s1 = nr.build_subject("2026-11-08", _today(1, 1, 0, n_hit=1), 0)
    assert "🟢" not in s1 and "⚪" in s1 and "¥±0" in s1, s1
    s2 = nr.build_subject("2026-11-05", _today(3, 3, 2200, roi=414.0, n_hit=2), 0)
    assert "🟢" in s2 and "¥+2,200" in s2, s2


def test_reflection_zero_is_not_plus():
    t = {"n": 1, "n_settled": 1, "n_hit": 1, "n_pending": 0, "profit": 0,
         "stats": {"fns": [1, 1], "sanren": [0, 0]}, "execs": []}
    txt = nr._template_reflection(t, None, None)
    assert "プラス" not in txt and "¥±0" in txt, txt


def test_picks_audit_dedupe_by_race_car_and_void_excluded():
    tmp = Path(tempfile.mkdtemp())
    picks = pd.DataFrame([
        # 5/28 型: 同じ R・車番が 2 つのバッチ名で記録
        {"race_date": "2026-05-28", "place_code": 6, "venue": "sanyou", "race_no": 4,
         "car_no": 6, "batch": "sanyou_R4", "sent_at": "2026-05-28T12:00:00"},
        {"race_date": "2026-05-28", "place_code": 6, "venue": "sanyou", "race_no": 4,
         "car_no": 6, "batch": "4,6", "sent_at": "2026-05-28T12:01:00"},
        # 不成立レース (払戻行は車番空)
        {"race_date": "2026-05-21", "place_code": 6, "venue": "sanyou", "race_no": 8,
         "car_no": 3, "batch": "sanyou_R8", "sent_at": "2026-05-21T20:00:00"},
        # 結果未取込の日 (結果待ちのまま)
        {"race_date": "2026-05-30", "place_code": 2, "venue": "kawaguchi", "race_no": 1,
         "car_no": 1, "batch": "kawaguchi_R1", "sent_at": "2026-05-30T11:00:00"},
    ])
    picks.to_csv(tmp / "daily_predict_picks.csv", index=False)
    pd.DataFrame([
        {"race_date": "2026-05-28", "place_code": 6},
        {"race_date": "2026-05-21", "place_code": 6},
    ]).to_csv(tmp / "race_results.csv", index=False)
    pd.DataFrame([
        {"race_date": "2026-05-28", "place_code": 6, "race_no": 4, "bet_type": "fns",
         "car_no_1": 6, "refund": 110},
        {"race_date": "2026-05-21", "place_code": 6, "race_no": 8, "bet_type": "fns",
         "car_no_1": None, "refund": 0},
    ]).to_csv(tmp / "payouts.csv", index=False)
    orig = pa.DATA
    try:
        pa.DATA = tmp
        audit = pa.attach_results(pa.load_picks())
    finally:
        pa.DATA = orig
    s = pa.summarize(audit)
    assert s["n_settled"] == 1 and s["payout"] == 110, s
    assert s["n_pending"] == 1, s          # 5/30 は未取込 → 結果待ち
    assert s["n_total"] == 2, s            # 重複 1 と不成立 1 は数えない


def test_ingest_window_extends_to_last_complete_day():
    got = di.target_dates(dt.date(2026, 10, 17), 2, dt.date(2026, 10, 13))
    assert got == ["2026-10-14", "2026-10-15", "2026-10-16"], got
    got = di.target_dates(dt.date(2026, 11, 11), 2, dt.date(2026, 11, 9))
    assert got == ["2026-11-09", "2026-11-10"], got
    got = di.target_dates(dt.date(2026, 11, 11), 2, dt.date(2026, 1, 1))
    assert len(got) == di.MAX_BACKFILL_DAYS, len(got)


def test_fetch_since_extends_to_last_success():
    assert dfo.default_since(dt.date(2026, 10, 17), dt.date(2026, 10, 14)) == "3d"
    assert dfo.default_since(dt.date(2026, 11, 11), dt.date(2026, 11, 10)) == "2d"
    assert dfo.default_since(dt.date(2026, 11, 11), None) == "2d"
    assert dfo.default_since(dt.date(2026, 11, 11), dt.date(2026, 1, 1)) == "31d"


def _run_all() -> bool:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    return passed == len(fns)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(0 if _run_all() else 1)
