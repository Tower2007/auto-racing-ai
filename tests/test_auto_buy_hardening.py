"""自動発注まわりの堅牢化 (2026-11-12 総合検証 指摘9・10・11・13 と残る弱点) のテスト。

- 連続失敗の sticky 停止 (日付をまたいで継続・人の解除・注文履歴での自動解除)
- 正常な発注を誤って止めないこと (成功の連続・失敗→成功・開催なし・日跨ぎ・既存 state からの移行)
- 重要通知の再送キュー
- 冪等キーに開催日 / failed の再試行は 1 日 1 回 / 二重発火の skip_lock_inflight
- 追記型台帳・前日 state の退避と夜次の読み取り・結果不明の照合
- picks の重複防止・直前オッズデーモンの実レース数打ち切り

実発注・メール・実ファイルには触れない (subprocess / 通知 / state / 時刻を差し替え)。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import auto_buy  # noqa: E402
import daily_predict  # noqa: E402
import night_report  # noqa: E402
import odds_prerace_daemon  # noqa: E402
from src import order_ledger  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
D1_NIGHT = dt.datetime(2026, 11, 12, 23, 30, tzinfo=JST)
D2_MIDNIGHT = dt.datetime(2026, 11, 13, 0, 10, tzinfo=JST)


@pytest.fixture
def ab(tmp_path, monkeypatch):
    """auto_buy を tmp に閉じ込める。実発注結果は ns.results の順に返す。"""
    monkeypatch.setattr(auto_buy, "DATA", tmp_path)
    monkeypatch.setattr(auto_buy, "STATE_FILE", tmp_path / "auto_buy_state.json")
    monkeypatch.setattr(auto_buy, "BET_HISTORY_CSV", tmp_path / "bet_history.csv")
    monkeypatch.setattr(auto_buy, "ABANDONED_STOP_FLAG", tmp_path / "abandoned.flag")
    monkeypatch.setattr(auto_buy, "AUTO_BUY_ENABLED", True)
    ns = types.SimpleNamespace(tmp=tmp_path, sent=[], results=[], calls=[],
                               mail_ok=True, mp=monkeypatch)

    def _notify(subject, body):
        ns.sent.append(subject)
        return ns.mail_ok
    monkeypatch.setattr(auto_buy, "_notify", _notify)

    def _exec(race_date, pc, rno, bets):
        ns.calls.append((race_date, pc, rno))
        return ns.results.pop(0)
    monkeypatch.setattr(auto_buy, "_run_execute_purchase", _exec)
    # ロックは常に取れる (実 mutex は使わない)
    monkeypatch.setattr(auto_buy, "_acquire_lock", lambda wait_sec=None, name=None: (None, None, False))
    monkeypatch.setattr(auto_buy, "_release_lock", lambda lock: None)
    return ns


def _cand(rno=7, race_date="2026-11-12", venue="iizuka", pc=5, amount=100):
    return {"race_date": race_date, "place_code": pc, "venue": venue, "venue_jp": "飯塚",
            "race_no": rno, "car_no": 5, "ev": 1.9, "amount": amount,
            "bets": [{"type": "fns", "cars": [5], "amount": amount}]}


OK = (auto_buy.EXEC_OK, "投票完了", None)
FAIL = (auto_buy.EXEC_FAILED, "締切超過", None)
UNSURE = (auto_buy.EXEC_UNCERTAIN, "タイムアウト", None)


def _state(ns):
    return json.loads((ns.tmp / "auto_buy_state.json").read_text(encoding="utf-8"))


def _flag(ns):
    return ns.tmp / auto_buy.FAILURE_STOP_FLAG_NAME


# ── 正常な発注を止めない ─────────────────────────────────────────────

def test_consecutive_successes_never_stop(ab):
    ab.results = [OK] * 6
    for r in range(1, 7):
        out = auto_buy.run_auto_buy([_cand(rno=r)], now=D1_NIGHT, dry_run=False)
        assert [v["verdict"] for v in out] == ["executed"]
    assert not _flag(ab).exists()
    assert _state(ab)["consecutive_failures"] == 0


def test_failure_then_success_resets_streak(ab):
    ab.results = [FAIL, UNSURE, OK, FAIL, FAIL, OK]
    verdicts = [auto_buy.run_auto_buy([_cand(rno=r)], now=D1_NIGHT, dry_run=False)[0]["verdict"]
                for r in range(1, 7)]
    assert verdicts == ["failed", "uncertain", "executed", "failed", "failed", "executed"]
    assert not _flag(ab).exists()
    st = _state(ab)
    assert st["consecutive_failures"] == 0 and st["failure_streak"] == []


def test_midnight_rollover_keeps_normal_buying(ab):
    """日跨ぎ: 前日の state を退避し、翌暦日の state で通常どおり発注 (上限は新しい日の枠)。"""
    ab.results = [OK, OK]
    auto_buy.run_auto_buy([_cand(rno=6)], now=D1_NIGHT, dry_run=False)
    out = auto_buy.run_auto_buy([_cand(rno=7, race_date="2026-11-12")],
                                now=D2_MIDNIGHT, dry_run=False)
    assert out[0]["verdict"] == "executed"
    st = _state(ab)
    assert st["date"] == "2026-11-13" and st["spent_yen"] == 100
    arch = ab.tmp / order_ledger.ARCHIVE_DIRNAME / "2026-11-12.json"
    assert json.loads(arch.read_text(encoding="utf-8"))["executions"][0]["race"] == "iizuka_R6"


def test_legacy_state_without_new_keys(ab):
    """既存 state (新キーなし・race_date なしの記録) から移行しても普通に動く。"""
    (ab.tmp / "auto_buy_state.json").write_text(json.dumps({
        "date": "2026-11-12", "spent_yen": 100, "profit_yen": 0,
        "consecutive_failures": 0,
        "executions": [{"race": "sanyou_R5", "amount": 100, "verdict": "executed",
                        "timestamp": "2026-11-12T12:41:08+09:00"}]}), encoding="utf-8")
    ab.results = [OK]
    out = auto_buy.run_auto_buy([_cand(rno=7)], now=D1_NIGHT, dry_run=False)
    assert out[0]["verdict"] == "executed"
    # 旧記録 (race_date なし) はラベルだけで一致 = 同じ R は再発注しない (安全側)
    out2 = auto_buy.run_auto_buy([_cand(rno=5, venue="sanyou", pc=6)], now=D1_NIGHT,
                                 dry_run=False)
    assert out2[0]["verdict"] == "skip_duplicate"
    led = order_ledger.read_records(ab.tmp / order_ledger.LEDGER_NAME)
    assert {r["race"] for r in led} == {"sanyou_R5", "iizuka_R7"}   # 旧記録も記帳


# ── 連続失敗の sticky 停止 ──────────────────────────────────────────

def _trip(ab):
    ab.results = [FAIL, UNSURE, FAIL]
    for r in (1, 2, 3):
        auto_buy.run_auto_buy([_cand(rno=r)], now=D1_NIGHT, dry_run=False)


def test_three_failures_trip_sticky_stop_across_midnight(ab):
    _trip(ab)
    assert _flag(ab).exists()
    info = json.loads(_flag(ab).read_text(encoding="utf-8"))
    assert [r["race_no"] for r in info["races"]] == [1, 2, 3]
    assert any("連続失敗 3 回" in s for s in ab.sent)
    # 日付が変わっても解除されない (旧: 0:00 の state リセットで自動解除)
    ab.results = [OK]
    out = auto_buy.run_auto_buy([_cand(rno=8)], now=D2_MIDNIGHT, dry_run=False)
    assert out[0]["verdict"] == auto_buy.VERDICT_FAILURE_STOP
    assert ab.calls[-1][2] == 3            # 停止中は発注しない
    st = _state(ab)
    assert st["date"] == "2026-11-13" and st["consecutive_failures"] == 3
    # 停止中の通知は 1 日 1 回
    auto_buy.run_auto_buy([_cand(rno=9)], now=D2_MIDNIGHT, dry_run=False)
    assert sum("停止中" in s for s in ab.sent) == 1
    # 停止中の verdict では手動購入ボタン付き候補メールを出さない
    send, _ = daily_predict.candidate_mail_decision(1, True, False, False, 1, out, False)
    assert send is False


def test_human_release_resumes(ab):
    _trip(ab)
    _flag(ab).unlink()                      # 人が解除
    ab.results = [OK]
    out = auto_buy.run_auto_buy([_cand(rno=8)], now=D2_MIDNIGHT, dry_run=False)
    assert out[0]["verdict"] == "executed"
    st = _state(ab)
    assert st["consecutive_failures"] == 0 and not st.get("failure_stop_tripped")


def test_auto_release_after_order_history_confirms(ab):
    _trip(ab)
    bh = ab.tmp / "bet_history.csv"
    bh.write_text("date,place_code,place_name,race_no,bet_amount,refund_amount,profit\n"
                  "2026-11-12,5,iizuka,2,100,0,-100\n", encoding="utf-8")
    log = ab.tmp / "fetch_order_history.log"
    # 取得がまだ (開催日当日) → 未確認 → 解除しない
    log.write_text("=== 2026-11-12T22:00:00 END exit=0 ===\n", encoding="utf-8")
    assert auto_buy.release_failure_stop_if_confirmed(bet_history_csv=bh, fetch_log=log) is None
    assert _flag(ab).exists()
    # 翌 02:30 の取得成功 → R2 成立 / R1・R3 不成立で確定 → 解除・通知
    log.write_text("=== 2026-11-13T02:30:20 END exit=0 ===\n", encoding="utf-8")
    msg = auto_buy.release_failure_stop_if_confirmed(
        now=dt.datetime(2026, 11, 13, 2, 31, tzinfo=JST), bet_history_csv=bh, fetch_log=log)
    assert msg and "成立" in msg and "不成立" in msg
    assert not _flag(ab).exists()
    assert _state(ab)["consecutive_failures"] == 0
    assert any("停止を解除" in s for s in ab.sent)


def test_flag_write_failure_still_blocks_via_counter(ab, monkeypatch):
    """フラグを書けなくても、持ち越した連続失敗カウントで止まり続ける (fail-closed)。"""
    real_flag = auto_buy._failure_stop_flag
    monkeypatch.setattr(auto_buy, "_failure_stop_flag",
                        lambda: ab.tmp / "no_such_dir" / "x" / "flag")
    _trip(ab)
    assert not _state(ab).get("failure_stop_tripped")
    monkeypatch.setattr(auto_buy, "_failure_stop_flag", real_flag)
    out = auto_buy.run_auto_buy([_cand(rno=8)], now=D2_MIDNIGHT, dry_run=False)
    assert out[0]["verdict"].startswith("skip_failures")


def test_failed_mail_is_queued_and_resent(ab):
    ab.mail_ok = False
    ab.results = [FAIL]
    auto_buy.run_auto_buy([_cand(rno=1)], now=D1_NIGHT, dry_run=False)
    q = ab.tmp / auto_buy.PENDING_MAIL_NAME
    assert q.exists() and "失敗" in order_ledger.read_records(q)[0]["subject"]
    ab.mail_ok = True
    assert auto_buy.resend_pending_notifications() == 1
    assert not q.exists()
    assert any(s.endswith("(再送)") for s in ab.sent)


def test_success_mail_failure_is_not_queued(ab):
    ab.mail_ok = False
    ab.results = [OK]
    auto_buy.run_auto_buy([_cand(rno=1)], now=D1_NIGHT, dry_run=False)
    assert not (ab.tmp / auto_buy.PENDING_MAIL_NAME).exists()


# ── 冪等キー・再試行制限・二重発火 ─────────────────────────────────────

def test_idempotency_key_includes_race_date(ab):
    ab.results = [OK, OK]
    auto_buy.run_auto_buy([_cand(rno=8, race_date="2026-11-12")], now=D1_NIGHT, dry_run=False)
    # 同じ暦日の state に、別開催日 (翌日開催) の同じ場・R → 別の R として発注
    out = auto_buy.run_auto_buy([_cand(rno=8, race_date="2026-11-13")], now=D1_NIGHT,
                                dry_run=False)
    assert out[0]["verdict"] == "executed"
    out2 = auto_buy.run_auto_buy([_cand(rno=8, race_date="2026-11-13")], now=D1_NIGHT,
                                 dry_run=False)
    assert out2[0]["verdict"] == "skip_duplicate"


def test_failed_retry_limited_to_once_per_day(ab):
    ab.results = [FAIL, FAIL, OK]
    v = [auto_buy.run_auto_buy([_cand(rno=4)], now=D1_NIGHT, dry_run=False)[0]["verdict"]
         for _ in range(3)]
    assert v == ["failed", "failed", "skip_retry_limit"]
    assert len(ab.calls) == 2


def test_skip_lock_same_race_inflight_suppresses_manual_mail(ab, monkeypatch):
    now = dt.datetime.now(JST)
    (ab.tmp / "auto_buy_state.json").write_text(json.dumps({
        "date": now.date().isoformat(), "spent_yen": 0, "profit_yen": 0,
        "consecutive_failures": 0, "executions": [],
        "inflight": [{"race": "iizuka_R7", "race_date": "2026-11-12",
                      "started_at": now.isoformat()}]}), encoding="utf-8")
    monkeypatch.setattr(auto_buy, "_acquire_lock", lambda wait_sec=None, name=None: None)
    out = auto_buy.run_auto_buy([_cand(rno=7), _cand(rno=8)], now=now, dry_run=False)
    assert [v["verdict"] for v in out] == ["skip_lock_inflight", "skip_lock"]
    send, _ = daily_predict.candidate_mail_decision(
        1, True, False, False, 1, out[:1], False)
    assert send is False                                     # 同じ R の先行が処理中
    send2, _ = daily_predict.candidate_mail_decision(
        1, True, False, False, 1, out[1:], False)
    assert send2 is True                                     # 従来の skip_lock は送る


def test_stale_inflight_is_ignored(ab, monkeypatch):
    now = dt.datetime.now(JST)
    old = now - dt.timedelta(seconds=auto_buy.INFLIGHT_STALE_SEC + 60)
    (ab.tmp / "auto_buy_state.json").write_text(json.dumps({
        "date": now.date().isoformat(), "executions": [],
        "inflight": [{"race": "iizuka_R7", "race_date": "2026-11-12",
                      "started_at": old.isoformat()}]}), encoding="utf-8")
    monkeypatch.setattr(auto_buy, "_acquire_lock", lambda wait_sec=None, name=None: None)
    out = auto_buy.run_auto_buy([_cand(rno=7)], now=now, dry_run=False)
    assert out[0]["verdict"] == "skip_lock"


def test_inflight_cleared_after_purchase(ab):
    ab.results = [OK]
    auto_buy.run_auto_buy([_cand(rno=7)], now=D1_NIGHT, dry_run=False)
    assert "inflight" not in _state(ab)


# ── 夜次: 台帳・退避からの読み取りと結果不明の照合 ────────────────────────

def test_night_report_reads_archived_day_after_rollover(ab, monkeypatch):
    ab.results = [OK, UNSURE, OK]
    auto_buy.run_auto_buy([_cand(rno=5)], now=D1_NIGHT, dry_run=False)
    auto_buy.run_auto_buy([_cand(rno=6)], now=D1_NIGHT, dry_run=False)
    # 00:10 に日跨ぎ R7 (前日開催) を発注 → state は 11/13 に上書き
    auto_buy.run_auto_buy([_cand(rno=7)], now=D2_MIDNIGHT, dry_run=False)
    monkeypatch.setattr(night_report, "STATE_FILE", ab.tmp / "auto_buy_state.json")
    execs, n_failed, carried = night_report.load_today_executions("2026-11-12")
    assert sorted(e["race_no"] for e in execs) == [5, 7]     # 退避分 + 日跨ぎ R7
    assert night_report.load_uncertain_executions("2026-11-12") == ["飯塚 R6"]
    # 翌日分の夜次では R7 は「前日開催のミッドナイト」(carried) として除外
    e2, _, c2 = night_report.load_today_executions("2026-11-13")
    assert e2 == [] and c2 == ["飯塚 R7"]


def test_reconcile_prev_uncertain(ab, monkeypatch):
    ab.results = [UNSURE, UNSURE]
    auto_buy.run_auto_buy([_cand(rno=6)], now=D1_NIGHT, dry_run=False)
    auto_buy.run_auto_buy([_cand(rno=7)], now=D1_NIGHT, dry_run=False)
    bh = ab.tmp / "bet_history.csv"
    bh.write_text("date,place_code,place_name,race_no,bet_amount,refund_amount,profit\n"
                  "2026-11-12,5,iizuka,6,100,0,-100\n", encoding="utf-8")
    log = ab.tmp / "fetch_order_history.log"
    monkeypatch.setattr(night_report, "STATE_FILE", ab.tmp / "auto_buy_state.json")
    monkeypatch.setattr(night_report, "BET_HISTORY", bh)
    monkeypatch.setattr(night_report, "FETCH_LOG", log)
    log.write_text("=== 2026-11-13T02:30:20 END exit=0 ===\n", encoding="utf-8")
    assert night_report.reconcile_prev_uncertain("2026-11-13") == [
        "飯塚 R6: 成立", "飯塚 R7: 不成立"]
    log.write_text("=== 2026-11-12T22:00:00 END exit=0 ===\n", encoding="utf-8")
    assert night_report.reconcile_prev_uncertain("2026-11-13") == [
        "飯塚 R6: 成立", "飯塚 R7: 未確認"]
    today = {"date": "2026-11-13", "uncertain_prev_check": ["飯塚 R6: 成立"]}
    assert any("前日 (2026-11-12) の結果不明 1R の照合" in n
               for n in night_report._scope_notes(today))


# ── picks 重複防止・直前オッズデーモン ──────────────────────────────────

def test_picks_log_dedup(tmp_path, monkeypatch):
    p = tmp_path / "picks.csv"
    monkeypatch.setattr(daily_predict, "PRODUCTION_LOG", p)
    row = {"race_date": "2026-11-12", "place_code": 5, "venue": "iizuka", "race_no": 7,
           "car_no": 5, "pred": 0.8, "pred_calib": 0.8, "ev_avg_calib": 1.9,
           "place_odds_min": 1.0, "place_odds_max": 3.0, "win_odds": 2.0}
    daily_predict.append_picks_log(pd.DataFrame([row]), "iizuka_R7")
    daily_predict.append_picks_log(pd.DataFrame([row]), "iizuka_R7")     # 二重発火
    daily_predict.append_picks_log(pd.DataFrame([{**row, "race_no": 8}]), "iizuka_R8")
    df = pd.read_csv(p)
    assert len(df) == 2 and sorted(df["race_no"]) == [7, 8]


def test_odds_daemon_truncates_to_actual_races(monkeypatch):
    class _C:
        def get_program_print_times(self, venue_key, date_str):
            return {1: "21:28", 2: "21:54", 3: "24:04"}
    monkeypatch.setattr(odds_prerace_daemon, "fetch_today_schedule",
                        lambda client: {5: {"cancelFlg": "0", "raceStartTime": "21:28",
                                            "nowRaceNo": 1, "liveEndTime": "24:50"}})
    out = odds_prerace_daemon.fetch_today_race_starts(_C(), dt.date(2026, 11, 12))
    assert sorted(out[5]) == [1, 2, 3]
