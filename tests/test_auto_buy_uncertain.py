"""自動発注の「結果不明 (uncertain)」判定のテスト (2026-11-12 監査 指摘1・2)。

- 指摘1: execute_purchase のタイムアウト時に _run_execute_purchase が 2 値を返し、
  呼び出し側の 3 値 unpack が ValueError で落ちていた (2026-07-26 実例)。
- 指摘2: 「投票する」click 以降の失敗も failed 扱いで、上限に計上されず手動購入
  ボタン付き候補メールが出ていた (2026-05-31 浜松 R5 は failed なのに成立)。

実発注・Playwright・メール送信は一切行わない: subprocess.run / _notify /
state ファイル / bet_history を monkeypatch と tmp_path に差し替える。
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import auto_buy  # noqa: E402
import daily_predict  # noqa: E402
import execute_purchase as ep  # noqa: E402
import night_report  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
# 23:30 = 夜間帯 (AUTO_BUY_ANYTIME の設定に依らず時間帯ガードを通る)
NOW = dt.datetime(2026, 11, 12, 23, 30, tzinfo=JST)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """state / bet_history を tmp に、通知を記録に、subprocess.run を差し替え可能に。"""
    monkeypatch.setattr(auto_buy, "DATA", tmp_path)
    monkeypatch.setattr(auto_buy, "STATE_FILE", tmp_path / "auto_buy_state.json")
    monkeypatch.setattr(auto_buy, "BET_HISTORY_CSV", tmp_path / "bet_history.csv")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(auto_buy, "_notify",
                        lambda subject, body: sent.append((subject, body)))
    calls: list[list[str]] = []

    def _no_real_run(*a, **kw):
        raise AssertionError("subprocess.run の差し替え忘れ (実発注禁止)")
    monkeypatch.setattr(auto_buy.subprocess, "run", _no_real_run)
    ns = types.SimpleNamespace(tmp=tmp_path, sent=sent, calls=calls,
                               monkeypatch=monkeypatch)
    return ns


def _set_run(sb, fn):
    def _run(cmd, *a, **kw):
        sb.calls.append(cmd)
        return fn(cmd, *a, **kw)
    sb.monkeypatch.setattr(auto_buy.subprocess, "run", _run)


def _completed(rc: int, payload: dict | None):
    return types.SimpleNamespace(
        returncode=rc,
        stdout=json.dumps(payload, ensure_ascii=False) if payload else "",
        stderr="")


def _cand(rno=7, amount=300):
    return {"race_date": "2026-11-12", "place_code": 5, "venue": "iizuka",
            "venue_jp": "飯塚", "race_no": rno, "car_no": 5, "ev": 1.9,
            "amount": amount,
            "bets": [{"type": "fns", "cars": [5], "amount": amount}]}


def _state(sb) -> dict:
    return json.loads((sb.tmp / "auto_buy_state.json").read_text(encoding="utf-8"))


# ── _run_execute_purchase: 常に 3 値・終了コードの分類 ───────────────────

def test_exit_code_constant_matches_execute_purchase():
    assert ep.EXIT_UNCERTAIN == auto_buy.EXECUTE_EXIT_UNCERTAIN == 4
    assert ep.EXIT_UNCERTAIN not in auto_buy.EXECUTE_EXIT_PRE_CLICK_FAILURES


def test_run_execute_purchase_timeout_returns_three_values(sandbox):
    def _timeout(cmd, *a, **kw):
        raise subprocess.TimeoutExpired(cmd, 180)
    _set_run(sandbox, _timeout)
    res = auto_buy._run_execute_purchase("2026-11-12", 5, 7, _cand()["bets"])
    assert len(res) == 3
    assert res[0] == auto_buy.EXEC_UNCERTAIN and "タイムアウト" in res[1]


@pytest.mark.parametrize("rc,payload,expect", [
    (0, {"success": True, "message": "投票完了"}, auto_buy.EXEC_OK),
    (0, {"success": False, "message": "?"}, auto_buy.EXEC_FAILED),
    (1, {"success": False, "error": "click 前"}, auto_buy.EXEC_FAILED),
    (2, None, auto_buy.EXEC_FAILED),
    (3, None, auto_buy.EXEC_FAILED),
    (4, {"success": False, "uncertain": True, "error": "GraphQL 不足"},
     auto_buy.EXEC_UNCERTAIN),
    (-1, None, auto_buy.EXEC_UNCERTAIN),            # 強制終了等 = 想定外
    (0xC000013A, None, auto_buy.EXEC_UNCERTAIN),
])
def test_run_execute_purchase_classifies_returncode(sandbox, rc, payload, expect):
    _set_run(sandbox, lambda cmd, *a, **kw: _completed(rc, payload))
    status, detail, balance = auto_buy._run_execute_purchase(
        "2026-11-12", 5, 7, _cand()["bets"])
    assert status == expect, (rc, status, detail)


def test_normalize_exec_result_is_count_agnostic():
    n = auto_buy._normalize_exec_result
    assert n((True, "ok", {"total": 1}))[0] == auto_buy.EXEC_OK
    assert n((False, "x"))[0] == auto_buy.EXEC_FAILED          # 旧 2 値
    assert n(("uncertain", "t"))[0] == auto_buy.EXEC_UNCERTAIN
    assert n(None)[0] == auto_buy.EXEC_UNCERTAIN
    assert n(())[0] == auto_buy.EXEC_UNCERTAIN
    assert n(("???", "x", None))[0] == auto_buy.EXEC_UNCERTAIN


# ── run 全体: 結果不明の扱い ───────────────────────────────────────

def _assert_uncertain_handling(sb, out):
    assert [r["verdict"] for r in out] == ["uncertain"], out
    st = _state(sb)
    assert st["spent_yen"] == 300                     # 予定額を上限に計上
    assert st["consecutive_failures"] == 1            # 連続失敗 +1
    assert st["executions"][-1]["verdict"] == "uncertain"
    warn = [s for s in sb.sent if "結果不明 (投票履歴を確認)" in s[0]]
    assert len(warn) == 1, sb.sent
    subj, body = warn[0]
    assert subj.startswith("[AUTO-BUY]") and "飯塚 R7" in subj and "¥300" in subj
    assert "成立していた場合、手動で買い直さないでください" in body
    assert "公式の投票履歴" in body and "予定額: ¥300" in body
    # 手動購入ボタン付き候補メールの対象外
    send, reason = daily_predict.candidate_mail_decision(
        1, True, False, False, 1, out, False)
    assert send is False and "結果不明" in reason
    # 2 回目 (同日・同 R) は冪等ガードで発注しない
    n_calls = len(sb.calls)
    out2 = auto_buy._run_auto_buy_locked([_cand()], NOW, dry_run=False)
    assert [r["verdict"] for r in out2] == ["skip_duplicate"], out2
    assert len(sb.calls) == n_calls
    assert _state(sb)["spent_yen"] == 300


def test_timeout_is_uncertain_not_exception(sandbox):
    """(a) タイムアウト → 例外にならず uncertain・上限計上・冪等・連続失敗+1・警告・候補メールなし。"""
    def _timeout(cmd, *a, **kw):
        raise subprocess.TimeoutExpired(cmd, 180)
    _set_run(sandbox, _timeout)
    out = auto_buy._run_auto_buy_locked([_cand()], NOW, dry_run=False)
    _assert_uncertain_handling(sandbox, out)
    assert "タイムアウト" in _state(sandbox)["executions"][-1]["detail"]


def test_post_click_exit_code_is_uncertain(sandbox):
    """(b) execute_purchase が click 以降の不確定 (exit 4) → 同じ扱い。"""
    _set_run(sandbox, lambda cmd, *a, **kw: _completed(
        4, {"success": False, "uncertain": True, "error": "GraphQL exact match 不足"}))
    out = auto_buy._run_auto_buy_locked([_cand()], NOW, dry_run=False)
    _assert_uncertain_handling(sandbox, out)


def test_exception_from_execute_call_is_uncertain(sandbox):
    """発注呼び出し自体の例外も auto_buy の外へ漏らさない (_ab_error 経路に落ちない)。"""
    def _boom(*a, **kw):
        raise OSError("spawn failed")
    sandbox.monkeypatch.setattr(auto_buy, "_run_execute_purchase", _boom)
    out = auto_buy._run_auto_buy_locked([_cand()], NOW, dry_run=False)
    assert [r["verdict"] for r in out] == ["uncertain"]
    assert _state(sandbox)["spent_yen"] == 300


def test_pre_click_failure_stays_failed(sandbox):
    """(c) 回帰: click 前の失敗 (exit 1) は failed のまま・上限に計上しない・候補メールは送る。"""
    _set_run(sandbox, lambda cmd, *a, **kw: _completed(
        1, {"success": False, "error": "「投票する」が disabled (締切超過?)"}))
    out = auto_buy._run_auto_buy_locked([_cand()], NOW, dry_run=False)
    assert [r["verdict"] for r in out] == ["failed"]
    st = _state(sandbox)
    assert st["spent_yen"] == 0 and st["consecutive_failures"] == 1
    assert any("失敗" in s[0] for s in sandbox.sent)
    assert not any("結果不明" in s[0] for s in sandbox.sent)
    send, _ = daily_predict.candidate_mail_decision(1, True, False, False, 1, out, False)
    assert send is True


def test_success_unchanged(sandbox):
    """(d) 回帰: 成功は executed・上限計上・連続失敗リセット・投票完了メール。"""
    (sandbox.tmp / "auto_buy_state.json").write_text(json.dumps({
        "date": "2026-11-12", "spent_yen": 100, "profit_yen": 0,
        "consecutive_failures": 2, "executions": []}), encoding="utf-8")
    _set_run(sandbox, lambda cmd, *a, **kw: _completed(
        0, {"success": True, "message": "投票完了: 複勝 5 ¥300",
            "balance": {"points": 1, "cash": 1, "total": 99_999}}))
    out = auto_buy._run_auto_buy_locked([_cand()], NOW, dry_run=False)
    assert [r["verdict"] for r in out] == ["executed"]
    st = _state(sandbox)
    assert st["spent_yen"] == 400 and st["consecutive_failures"] == 0
    assert any("投票完了" in s[0] for s in sandbox.sent)
    send, _ = daily_predict.candidate_mail_decision(1, True, False, False, 1, out, False)
    assert send is False


def test_uncertain_overrides_send_always():
    """AUTO_BUY_SEND_CANDIDATE_MAIL=1 でも結果不明の R に購入ボタンは出さない。"""
    v = [{"race": "iizuka_R7", "verdict": "uncertain"}]
    send, _ = daily_predict.candidate_mail_decision(1, True, False, True, 1, v, False)
    assert send is False
    assert daily_predict.AUTO_BUY_UNCERTAIN_VERDICT == auto_buy.VERDICT_UNCERTAIN


# ── execute_purchase: click 前後の分類と終了コード (Playwright は動かさない) ──

def test_classify_after_click():
    pre = RuntimeError("「投票する」が disabled")
    assert ep._classify_after_click(pre, clicked=False) is pre
    post = ep._classify_after_click(RuntimeError("GraphQL 不足"), clicked=True)
    assert isinstance(post, ep.PurchaseUncertain) and "GraphQL 不足" in str(post)
    u = ep.PurchaseUncertain("x")
    assert ep._classify_after_click(u, clicked=True) is u


def _run_main(monkeypatch, exc):
    async def _fake_buy(*a, **kw):
        raise exc
    monkeypatch.setattr(ep, "execute_buy", _fake_buy)
    monkeypatch.setattr(ep, "_stop_flags_block", lambda bets: (False, None))
    today = dt.datetime.now(JST).date().isoformat()
    monkeypatch.setattr(sys, "argv", [
        "execute_purchase.py", "--race-date", today, "--place", "5",
        "--race", "7", "--car", "5", "--amount", "100"])
    with pytest.raises(SystemExit) as ei:
        ep.main()
    return ei.value.code


def test_main_exit_code_uncertain(monkeypatch, capsys):
    code = _run_main(monkeypatch, ep.PurchaseUncertain("click 後に確認取れず"))
    assert code == ep.EXIT_UNCERTAIN
    out = json.loads(capsys.readouterr().out.strip())
    assert out["success"] is False and out["uncertain"] is True


def test_main_exit_code_pre_click_failure(monkeypatch, capsys):
    code = _run_main(monkeypatch, RuntimeError("投票確認へ click 失敗"))
    assert code == 1
    out = json.loads(capsys.readouterr().out.strip())
    assert out["success"] is False and "uncertain" not in out


def test_execute_buy_boundary_is_vote_click():
    """境界 (clicked=True) は「投票する」click の直前 1 箇所だけで、外側で分類される。"""
    import inspect
    src = inspect.getsource(ep.execute_buy)
    assert src.count("clicked = True") == 1
    i_flag = src.index("clicked = True")
    i_click = src.index("await vote_btn.click(")
    # フラグ設定と click の間に他の await (=別の画面操作) を挟まない
    assert i_flag < i_click and "await " not in src[i_flag:i_click]
    # 停止フラグの最終再検査 (中止 = 未発注) は境界より前
    assert src.index("click 直前の最終再検査で中止") < i_flag
    assert "_classify_after_click(e, clicked)" in src


# ── 夜次: 結果不明を数える (暫定精算には含めない) ───────────────────────

def test_night_report_counts_uncertain(tmp_path, monkeypatch):
    st = {"date": "2026-11-12", "spent_yen": 400, "profit_yen": 0,
          "consecutive_failures": 1, "executions": [
              {"race": "sanyou_R5", "amount": 100, "verdict": "executed",
               "bets": "複勝 8号 ¥100", "timestamp": "2026-11-12T12:41:08+09:00"},
              {"race": "iizuka_R7", "amount": 300, "verdict": "uncertain",
               "bets": "複勝 5号 ¥300", "timestamp": "2026-11-12T21:30:00+09:00"},
          ]}
    p = tmp_path / "auto_buy_state.json"
    p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(night_report, "STATE_FILE", p)
    execs, n_failed, _ = night_report.load_today_executions("2026-11-12")
    assert len(execs) == 1 and n_failed == 0           # 暫定精算は executed のみ
    assert night_report.load_uncertain_executions("2026-11-12") == ["飯塚 R7"]
    today = {"date": "2026-11-12", "n": 1, "n_settled": 1, "n_pending": 0,
             "n_hit": 1, "roi": 180.0, "profit": 80, "uncertain": ["飯塚 R7"]}
    assert "❓結果不明1" in night_report.build_subject("2026-11-12", today, 0)
    assert any("結果不明 1R" in n for n in night_report._scope_notes(today))
