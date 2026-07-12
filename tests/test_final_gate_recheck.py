"""2026-07-12 Codex 再検証 (18f8601/32df58f 後の残存3件) の回帰テスト。

Codex 指定の再開条件4 に対応する 3 シナリオ:

  ① 「候補生成後にフラグ作成」 → 発注直前 (mutex 取得後) の再検査で skip
     (auto_buy.rt3_final_gate_blocks / _run_auto_buy_locked 内の recheck。
      buy_app のトークン実行直前ゲートも同じ rt3_final_gate_blocks を共有)
  ② 「壊れた台帳 (金額セル不正 / 必須ヘッダ欠落)」 → backstop 完全 fail-closed
     (evaluate_backstop が部分集計を返さず profit=None、
      backstop_blocks_purchase()=True。sticky フラグ書き出し・通知はしない)
  ③ 「投票後クラッシュ (WAIT_ABANDONED 受領)」 → 発注続行せず
     全候補 skip_abandoned_lock + sticky な「発注結果不明」フラグ
     (abandoned_lock_stop.flag、検知時刻・mutex名・照合手順を記載) 書き出し
     + 警告通知。以後の run はフラグを人間が明示削除するまで
     skip_abandoned_pending で全券種停止し、削除後だけ通常復帰する
     (2026-07-12 Codex再々検証で sticky 化)。ロック自体は正常解放。

発注経路 (execute_purchase 実体) には一切到達しない
(呼ばれたら AssertionError を出す禁止スタブを仕込む)。
実メールも送らない (_notify / _send_notify をスタブ)。
停止フラグ類は全て一時ディレクトリ内で作る (data/ の実フラグには触れない)。

pytest があれば `pytest tests/test_final_gate_recheck.py`、
無くても `python tests/test_final_gate_recheck.py` で実行可能。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import auto_buy  # noqa: E402
import src.backstop as backstop  # noqa: E402
from test_backstop_and_scope import (  # noqa: E402
    _BackstopSandbox, _detail_row, _write_detail,
)

IS_WINDOWS = os.name == "nt"


# ─── 共通スタブ / ハーネス ───────────────────────────────────────

def _forbidden_execute(*a, **kw):
    raise AssertionError("execute_purchase 経路が呼ばれた (テストでは禁止)")


class _AutoBuySandbox:
    """auto_buy の state / 通知 / 実発注 / mutex 名を tmp・スタブに差し替える。"""

    def __init__(self, mutex_name: str | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="autobuy_recheck_"))
        self.sent: list[tuple[str, str]] = []
        self.mutex_name = mutex_name or f"Global\\AutoRacingAITest_{uuid.uuid4().hex[:8]}"
        # 購入ゲートも run mutex とは別名前空間でテスト分離
        self.gate_name = f"Global\\AutoRacingAITestGate_{uuid.uuid4().hex[:8]}"
        self._orig: dict = {}

    def __enter__(self):
        self._orig = {k: getattr(auto_buy, k) for k in (
            "AUTO_BUY_ENABLED", "LOCK_WAIT_SEC", "STATE_FILE", "DATA",
            "BET_HISTORY_CSV", "ABANDONED_STOP_FLAG", "_notify",
            "_run_execute_purchase", "_mutex_name", "_purchase_gate_name",
            "check_guards")}
        auto_buy._purchase_gate_name = lambda: self.gate_name
        auto_buy.AUTO_BUY_ENABLED = True
        auto_buy.LOCK_WAIT_SEC = 5
        # 一般ガードは常に通す (.env の時間帯/上限設定に依存させず、
        # 本テストの主題 = 発注直前再検査 だけを検証する)
        auto_buy.check_guards = lambda *a, **kw: (True, "ok")
        auto_buy.DATA = self.tmp
        auto_buy.STATE_FILE = self.tmp / "auto_buy_state.json"
        auto_buy.BET_HISTORY_CSV = self.tmp / "bet_history.csv"
        auto_buy.ABANDONED_STOP_FLAG = self.tmp / "abandoned_lock_stop.flag"
        auto_buy._notify = lambda subject, body: self.sent.append((subject, body))
        auto_buy._run_execute_purchase = _forbidden_execute
        auto_buy._mutex_name = lambda: self.mutex_name
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            setattr(auto_buy, k, v)
        return False


def _sanren_candidate(amount: int = 200) -> dict:
    return {
        "race_date": "2099-01-01", "place_code": 4, "venue": "hamamatsu",
        "venue_jp": "浜松", "race_no": 7, "car_no": 5, "ev": 2.0,
        "bets": [{"type": "rt3", "cars": [5, 6, 7], "amount": 100},
                 {"type": "rf3", "cars": [5, 6, 7], "amount": 100}],
        "amount": amount,
    }


# ─── ① 候補生成後にフラグ作成 → 発注直前再検査で skip ─────────────────

def test_flag_created_after_candidates_skips_at_final_gate():
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    import daily_predict
    with _BackstopSandbox() as bs, _AutoBuySandbox() as ab:
        # 健全な台帳 (backstop 閾値内) + kill-switch フラグは tmp 上
        _write_detail(bs.detail, [_detail_row(4, "rt3", vote=100, hit=200)])
        orig_stop = daily_predict.RT3_STOP_FLAG
        try:
            daily_predict.RT3_STOP_FLAG = ab.tmp / "rt3_stop.flag"

            # 候補生成時点では購入可 (= daily_predict 側の一次判定は通過する状態)
            assert daily_predict.rt3_buy_active() is True
            assert auto_buy.rt3_final_gate_blocks(
                _sanren_candidate()["bets"]) is False

            # 「候補生成後」に停止フラグが立つ (mutex 待ち中の発動を模擬)
            daily_predict.RT3_STOP_FLAG.write_text("stop", encoding="utf-8")

            out = auto_buy.run_auto_buy([_sanren_candidate()], dry_run=False)
            assert len(out) == 1
            assert out[0]["verdict"].startswith("skip_rt3_stop_recheck"), out
            # skip 通知は飛ぶが、発注経路 (禁止スタブ) には到達していない
            assert len(ab.sent) == 1 and "skip" in ab.sent[0][0]

            # フラグを外せば同じ候補が発注経路に進む (dry-run で確認、実発注なし)
            daily_predict.RT3_STOP_FLAG.unlink()
            out2 = auto_buy.run_auto_buy([_sanren_candidate()], dry_run=True)
            assert [r["verdict"] for r in out2] == ["dry_run"], out2
        finally:
            daily_predict.RT3_STOP_FLAG = orig_stop


def test_final_gate_backstop_flag_and_failclosed():
    """backstop sticky フラグ (tmp) でも再検査が skip する / 判定不能は fail-closed。"""
    import daily_predict
    with _BackstopSandbox() as bs:
        orig_stop = daily_predict.RT3_STOP_FLAG
        try:
            daily_predict.RT3_STOP_FLAG = bs.tmp / "rt3_stop.flag"
            _write_detail(bs.detail, [_detail_row(4, "rt3", vote=100, hit=200)])
            bets = _sanren_candidate()["bets"]
            assert auto_buy.rt3_final_gate_blocks(bets) is False
            # backstop sticky フラグ (tmp 内) が候補生成後に出現
            bs.flag.write_text("stop", encoding="utf-8")
            assert auto_buy.rt3_final_gate_blocks(bets) is True
            bs.flag.unlink()
            # 台帳が消えて判定不能 → fail-closed
            bs.detail.unlink()
            assert auto_buy.rt3_final_gate_blocks(bets) is True
            # 三連系を含まない bets は再検査対象外 (複勝は止めない)
            assert auto_buy.rt3_final_gate_blocks(
                [{"type": "fns", "cars": [5], "amount": 300}]) is False
            assert auto_buy.rt3_final_gate_blocks([]) is False
            assert auto_buy.rt3_final_gate_blocks(None) is False
        finally:
            daily_predict.RT3_STOP_FLAG = orig_stop


# ─── ② 壊れた台帳 (金額セル不正) → backstop 完全 fail-closed ───────────

def test_backstop_corrupt_amount_cell_fail_closed():
    """三連系行の金額セルが 1 つでも不正なら部分集計を返さず fail-closed。
    sticky フラグ書き出し・メール通知 (真の閾値割れのみ) は発火しない。"""
    with _BackstopSandbox() as sb:
        # 正常行に混じって 1 行だけ vote_amount が壊れている
        # (旧実装はこの行を 0 円扱いで無視 → 過小集計で購入許可し得た)
        _write_detail(sb.detail, [
            _detail_row(4, "rt3", vote=100, hit=200, race_no=1),
            {"date": "2026-07-01", "place_code": 4, "race_no": 2,
             "order_id": "broken", "bet_type_code": "rt3",
             "vote_amount": "12,000円", "hit_amount": 0,
             "henkan_amount": 0, "tokubarai_amount": 0},
        ])
        ev = backstop.evaluate_backstop()
        assert ev["profit"] is None, ev
        assert "金額セル不正" in (ev["error"] or ""), ev
        assert ev["breached"] is False
        # 購入ゲートは fail-closed で停止
        assert backstop.backstop_blocks_purchase() is True
        # 真の発動とは区別: フラグも通知も出さない
        out = backstop.enforce_backstop()
        assert out["active"] is False and out["newly_triggered"] is False
        assert not sb.flag.exists() and sb.sent == []


def test_backstop_corrupt_variants_fail_closed():
    """空セル・非数値・負値・NaN、いずれも不正として fail-closed。"""
    bad_values = ["", "abc", "-100", "nan"]
    for bad in bad_values:
        with _BackstopSandbox() as sb:
            _write_detail(sb.detail, [
                {"date": "2026-07-01", "place_code": 4, "race_no": 1,
                 "order_id": "x", "bet_type_code": "rf3",
                 "vote_amount": 100, "hit_amount": bad,
                 "henkan_amount": 0, "tokubarai_amount": 0},
            ])
            ev = backstop.evaluate_backstop()
            assert ev["profit"] is None, (bad, ev)
            assert backstop.backstop_blocks_purchase() is True, bad
    # 三連系外 (fns) の壊れたセルは三連系集計に影響しない (fail-open のまま)
    with _BackstopSandbox() as sb:
        _write_detail(sb.detail, [
            _detail_row(4, "rt3", vote=100, hit=200, race_no=1),
            {"date": "2026-07-01", "place_code": 2, "race_no": 2,
             "order_id": "y", "bet_type_code": "fns",
             "vote_amount": "broken", "hit_amount": "broken",
             "henkan_amount": 0, "tokubarai_amount": 0},
        ])
        ev = backstop.evaluate_backstop()
        assert ev["profit"] == 100 and ev["breached"] is False
        assert backstop.backstop_blocks_purchase() is False


def test_backstop_missing_required_header_fail_closed():
    """必須ヘッダ (vote_amount 等) の欠落 = 台帳フォーマット異常 → fail-closed。"""
    with _BackstopSandbox() as sb:
        sb.detail.write_text(
            "date,place_code,race_no,bet_type_code,hit_amount\n"
            "2026-07-01,4,1,rt3,0\n",
            encoding="utf-8")
        ev = backstop.evaluate_backstop()
        assert ev["profit"] is None, ev
        assert "必須ヘッダ欠落" in (ev["error"] or ""), ev
        assert backstop.backstop_blocks_purchase() is True
        out = backstop.enforce_backstop()
        assert out["active"] is False
        assert not sb.flag.exists() and sb.sent == []


def test_backstop_healthy_ledger_still_passes():
    """厳格化しても健全な台帳 (本番フォーマット) は False のまま (回帰確認)。"""
    with _BackstopSandbox() as sb:
        _write_detail(sb.detail, [
            _detail_row(4, "rt3", vote=100, hit=1080, race_no=1),
            _detail_row(6, "rf3", vote=300, hit=0, race_no=2),
            _detail_row(2, "fns", vote=500, hit=700, race_no=3),  # 三連系外
        ])
        ev = backstop.evaluate_backstop()
        assert ev["profit"] == 680 and ev["n_rows"] == 2
        assert backstop.backstop_blocks_purchase() is False


# ─── ③ 投票後クラッシュ (WAIT_ABANDONED) → skip + 警告 ────────────────

def _child_code(name: str, wait_ms: int) -> str:
    """子プロセス: mutex を取得して Release せず即死 (abandoned 化)。"""
    return (
        "import ctypes, os, sys\n"
        "from ctypes import wintypes\n"
        "k32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]\n"
        "k32.CreateMutexW.restype = wintypes.HANDLE\n"
        "k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]\n"
        "k32.WaitForSingleObject.restype = wintypes.DWORD\n"
        f"h = k32.CreateMutexW(None, False, {name!r})\n"
        f"rc = k32.WaitForSingleObject(h, {wait_ms})\n"
        "print(f'ACQ:{rc}', flush=True)\n"
        "os._exit(0)\n"
    )


def test_abandoned_mutex_sticky_stop_until_human_clears():
    """WAIT_ABANDONED 受領 → sticky フラグ書き出し + 全候補 skip + 警告通知。
    次回 run **も** skip_abandoned_pending で停止し、人間のフラグ明示削除後
    だけ通常復帰する (2026-07-12 Codex再々検証の要求仕様)。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    with _AutoBuySandbox() as ab:
        name = ab.mutex_name
        locked_calls: list = []
        orig_locked = auto_buy._run_auto_buy_locked
        # 親が未所有ハンドルでオブジェクトを生存させ、子の即死で abandoned 化
        k32 = auto_buy._kernel32()
        keepalive = k32.CreateMutexW(None, False, name)
        assert keepalive, "keepalive ハンドル作成"
        try:
            auto_buy._run_auto_buy_locked = (
                lambda c, n, d: locked_calls.append(len(c)) or [])
            proc = subprocess.run(
                [sys.executable, "-c", _child_code(name, 5000)],
                timeout=30, capture_output=True, text=True)
            assert proc.returncode == 0 and "ACQ:0" in proc.stdout, \
                f"子プロセスが mutex を実取得した (stdout={proc.stdout.strip()!r})"

            # ── run 1: abandoned 受領 → skip + sticky フラグ + 警告通知 ──
            cands = [_sanren_candidate(),
                     {"race_date": "2099-01-01", "place_code": 6,
                      "venue": "sanyou", "venue_jp": "山陽", "race_no": 8,
                      "car_no": 1, "ev": 2.0,
                      "bets": [{"type": "fns", "cars": [1], "amount": 300}],
                      "amount": 300}]
            out = auto_buy.run_auto_buy(cands, dry_run=False)
            assert [r["verdict"] for r in out] == \
                ["skip_abandoned_lock", "skip_abandoned_lock"], out
            assert locked_calls == [], "abandoned 時は発注本体に入らない"
            assert len(ab.sent) == 1
            assert "先行プロセス異常終了" in ab.sent[0][0]
            assert "重複投票" in ab.sent[0][1]
            assert "全券種" in ab.sent[0][1]
            # sticky フラグが書かれ、検知時刻・mutex名・照合手順を含む
            flag = ab.tmp / "abandoned_lock_stop.flag"
            assert flag.exists(), "発注結果不明フラグが書き出されている"
            ftext = flag.read_text(encoding="utf-8")
            assert "detected_at=" in ftext and f"mutex={name}" in ftext
            assert "投票履歴" in ftext and "削除" in ftext
            assert auto_buy.abandoned_stop_active() is True

            # ロック自体は正常解放済み (mutex ではなくフラグが発注を止める)
            got: dict[str, bool] = {}

            def _try_after() -> None:
                lk = auto_buy._acquire_lock(wait_sec=0, name=name)
                got["ok"] = lk is not None
                got["abandoned"] = bool(lk and lk[2])
                if lk is not None:
                    auto_buy._release_lock(lk)

            t = threading.Thread(target=_try_after)
            t.start(); t.join(timeout=15)
            assert got.get("ok") is True, "abandoned 処理後もロックは取得可能"
            assert got.get("abandoned") is False, \
                "正常解放後の取得は abandoned 扱いにならない"

            # ── run 2: フラグ残存中は次回 run も停止 (発注本体に入らない) ──
            out2 = auto_buy.run_auto_buy(cands, dry_run=False)
            assert [r["verdict"] for r in out2] == \
                ["skip_abandoned_pending", "skip_abandoned_pending"], out2
            assert locked_calls == [], "フラグ残存中は発注本体に入らない"
            # 通知は日次1回: 検知時に通知済みマークが立つため再送しない
            assert len(ab.sent) == 1, ab.sent

            # ── run 3: 人間がフラグを明示削除 → 通常復帰 (発注本体へ) ──
            flag.unlink()
            assert auto_buy.abandoned_stop_active() is False
            out3 = auto_buy.run_auto_buy(cands, dry_run=False)
            assert out3 == [] and locked_calls == [2], \
                "フラグ削除後だけ通常の発注経路 (stub) に復帰する"
        finally:
            auto_buy._run_auto_buy_locked = orig_locked
            k32.CloseHandle(keepalive)


def test_abandoned_pending_flag_blocks_and_notifies_daily_once():
    """フラグが既に存在する状態 (別プロセスの検知や再起動後) でも入口で停止。
    残存リマインドは日次1回のみ。buy_app 用ゲート abandoned_stop_active() も
    同じフラグを見る (全券種ブロック)。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    with _AutoBuySandbox() as ab:
        locked_calls: list = []
        orig_locked = auto_buy._run_auto_buy_locked
        try:
            auto_buy._run_auto_buy_locked = (
                lambda c, n, d: locked_calls.append(len(c)) or [])
            # 人間未照合のフラグが残っている状況を直接作る
            (ab.tmp / "abandoned_lock_stop.flag").write_text(
                "detected_at=2099-01-01T00:00:00\n", encoding="utf-8")
            assert auto_buy.abandoned_stop_active() is True  # buy_app ゲート

            # 複勝のみの候補 (三連系なし) でも全券種停止
            fns_only = [{"race_date": "2099-01-01", "place_code": 2,
                         "venue": "kawaguchi", "venue_jp": "川口", "race_no": 3,
                         "car_no": 2, "ev": 1.5,
                         "bets": [{"type": "fns", "cars": [2], "amount": 300}],
                         "amount": 300}]
            out = auto_buy.run_auto_buy(fns_only, dry_run=False)
            assert [r["verdict"] for r in out] == ["skip_abandoned_pending"], out
            assert locked_calls == []
            assert len(ab.sent) == 1
            assert "発注結果不明フラグ残存" in ab.sent[0][0]
            assert "skip_abandoned_pending" in ab.sent[0][1]

            # 同日 2 回目の run はリマインドを再送しない (日次1回)
            out2 = auto_buy.run_auto_buy(fns_only, dry_run=False)
            assert [r["verdict"] for r in out2] == ["skip_abandoned_pending"]
            assert len(ab.sent) == 1, "残存リマインドは日次1回のみ"

            # 人間の明示削除で復帰
            (ab.tmp / "abandoned_lock_stop.flag").unlink()
            assert auto_buy.abandoned_stop_active() is False
            out3 = auto_buy.run_auto_buy(fns_only, dry_run=False)
            assert out3 == [] and locked_calls == [1]
        finally:
            auto_buy._run_auto_buy_locked = orig_locked


def test_acquire_lock_returns_abandoned_flag():
    """_acquire_lock の戻り値 3-tuple: 通常取得は abandoned=False。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    name = f"Global\\AutoRacingAITest_{uuid.uuid4().hex[:8]}"
    lock = auto_buy._acquire_lock(wait_sec=5, name=name)
    assert lock is not None and len(lock) == 3
    assert lock[2] is False, "通常取得は abandoned=False"
    auto_buy._release_lock(lock)


# ─── ④ 並行 run 競合窓: mutex 取得後の abandoned フラグ再検査 ──────────────

def test_flag_created_during_mutex_wait_rechecked_after_acquire():
    """Codex再々々検証の主題: 入口 (mutex 取得前) 検査だけでは閉じない競合窓。

    シーケンス:
      1. run B が入口でフラグ不存在を確認 → mutex 待ちに入る
      2. run A が WAIT_ABANDONED を受領し sticky フラグを作成
      3. run A が mutex を正常解放
      4. run B が WAIT_OBJECT_0 で正常取得 (abandoned=False)

    旧実装では 4 の後に再検査がなく発注してしまう。本テストは _acquire_lock を
    差し替えて「取得の瞬間にフラグが既に作られている (= A が待機中に作成済み)」
    状況を作り、run B が mutex 取得後の再検査で skip_abandoned_pending となり
    発注本体 (_run_auto_buy_locked) に一切入らないことを検証する。
    実 mutex は使わず、フラグ作成タイミングだけを制御する (全 OS で実行可)。"""
    with _AutoBuySandbox() as ab:
        flag = auto_buy.ABANDONED_STOP_FLAG
        assert not flag.exists(), "入口検査時点ではフラグ不存在"

        released: list = []
        orig_locked = auto_buy._run_auto_buy_locked
        orig_acquire = auto_buy._acquire_lock
        orig_release = auto_buy._release_lock
        try:
            def _fake_acquire(*a, **kw):
                # run B が mutex を正常取得する「瞬間」に、run A が待機中に
                # 作成済みだった sticky フラグが既に存在している状況を模擬
                flag.write_text("detected_at=2099-01-01T00:00:00\n",
                                encoding="utf-8")
                return ("k32_stub", "handle_stub", False)  # abandoned=False

            auto_buy._acquire_lock = _fake_acquire
            auto_buy._release_lock = lambda lk: released.append(lk)
            # 発注本体は禁止スタブ: 再検査が効かず入るとこのテストは失敗する
            auto_buy._run_auto_buy_locked = _forbidden_execute

            cands = [
                _sanren_candidate(),
                {"race_date": "2099-01-01", "place_code": 2,
                 "venue": "kawaguchi", "venue_jp": "川口", "race_no": 3,
                 "car_no": 2, "ev": 1.5,
                 "bets": [{"type": "fns", "cars": [2], "amount": 300}],
                 "amount": 300}]
            out = auto_buy.run_auto_buy(cands, dry_run=False)
            assert [r["verdict"] for r in out] == \
                ["skip_abandoned_pending", "skip_abandoned_pending"], out
            # finally でロックは正常解放されている (取得後再検査でも解放漏れなし)
            assert released == [("k32_stub", "handle_stub", False)], released
            assert len(ab.sent) == 1 and "残存" in ab.sent[0][0], ab.sent
        finally:
            auto_buy._run_auto_buy_locked = orig_locked
            auto_buy._acquire_lock = orig_acquire
            auto_buy._release_lock = orig_release


def test_no_flag_after_acquire_proceeds_normally():
    """回帰: 取得後もフラグが無ければ従来どおり発注本体へ進む (誤停止しない)。"""
    with _AutoBuySandbox() as ab:
        assert not auto_buy.ABANDONED_STOP_FLAG.exists()
        locked_calls: list = []
        orig_locked = auto_buy._run_auto_buy_locked
        orig_acquire = auto_buy._acquire_lock
        orig_release = auto_buy._release_lock
        try:
            auto_buy._acquire_lock = lambda *a, **kw: ("k32", "h", False)
            auto_buy._release_lock = lambda lk: None
            auto_buy._run_auto_buy_locked = (
                lambda c, n, d: locked_calls.append(len(c)) or [])
            out = auto_buy.run_auto_buy([_sanren_candidate()], dry_run=False)
            assert out == [] and locked_calls == [1], (out, locked_calls)
        finally:
            auto_buy._run_auto_buy_locked = orig_locked
            auto_buy._acquire_lock = orig_acquire
            auto_buy._release_lock = orig_release


# ─── ⑤ execute_purchase 入口ゲート (全経路の最終防御) ──────────────────────

def test_execute_purchase_entry_gate_fail_closed():
    """execute_purchase 入口ゲート: 全券種は abandoned フラグ、三連系は
    backstop_blocks_purchase() (フラグ存在 OR 台帳異常 OR 閾値超過) で発注を
    止める。判定不能も fail-closed。auto_buy 経由・手動UI 経由・直CLI の全経路が
    ここに集約される最終防御点。② 直CLI経路で台帳異常 → 停止 も検証する。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import execute_purchase as ep

    fns = [{"type": "fns", "cars": [5], "amount": 300}]
    sanren = [{"type": "rt3", "cars": [5, 6, 7], "amount": 100}]

    ab_tmp = Path(tempfile.mkdtemp(prefix="ep_gate_"))
    ab_flag = ab_tmp / "abandoned_lock_stop.flag"
    orig_ab = auto_buy.ABANDONED_STOP_FLAG
    with _BackstopSandbox() as bs:
        try:
            auto_buy.ABANDONED_STOP_FLAG = ab_flag
            # 健全な台帳 (閾値内) → backstop ゲート通過
            _write_detail(bs.detail, [_detail_row(4, "rt3", vote=100, hit=200)])

            # フラグ無し・健全台帳 → 全券種通す (誤停止しない)
            assert ep._stop_flags_block(fns) == (False, None)
            assert ep._stop_flags_block(sanren) == (False, None)

            # abandoned フラグ → 複勝でも三連系でも停止 (全券種)
            ab_flag.write_text("x", encoding="utf-8")
            assert ep._stop_flags_block(fns)[0] is True
            assert ep._stop_flags_block(sanren)[0] is True
            ab_flag.unlink()

            # rt3_backstop_stop.flag 存在 → 三連系のみ停止、複勝は通す
            bs.flag.write_text("x", encoding="utf-8")
            blk_s, why_s = ep._stop_flags_block(sanren)
            assert blk_s is True and "バックストップ" in (why_s or ""), \
                (blk_s, why_s)
            assert ep._stop_flags_block(fns) == (False, None)
            bs.flag.unlink()

            # ② 直CLI経路で台帳異常 (金額セル不正・フラグ無し) →
            #    backstop_blocks_purchase() 経由で三連系停止 (旧 backstop_active
            #    のフラグ存在だけ見る実装では迂回できた穴を塞ぐ)
            _write_detail(bs.detail, [
                {"date": "2026-07-01", "place_code": 4, "race_no": 1,
                 "order_id": "broken", "bet_type_code": "rt3",
                 "vote_amount": "壊れた", "hit_amount": 0,
                 "henkan_amount": 0, "tokubarai_amount": 0}])
            assert not bs.flag.exists()  # フラグは無い
            assert backstop.backstop_blocks_purchase() is True  # 台帳ゲート=True
            blk_c, _why_c = ep._stop_flags_block(sanren)
            assert blk_c is True, "台帳異常で execute_purchase 入口も停止すべき"
            # 台帳異常でも複勝 (三連系外) は止めない
            assert ep._stop_flags_block(fns) == (False, None)
            _write_detail(bs.detail, [_detail_row(4, "rt3", vote=100, hit=200)])

            # abandoned 判定が例外 → fail-closed
            orig_fn = auto_buy.abandoned_stop_active
            try:
                def _boom():
                    raise RuntimeError("boom")
                auto_buy.abandoned_stop_active = _boom
                blk, why = ep._stop_flags_block(fns)
                assert blk is True and "fail-closed" in (why or ""), (blk, why)
            finally:
                auto_buy.abandoned_stop_active = orig_fn
        finally:
            auto_buy.ABANDONED_STOP_FLAG = orig_ab


# ─── ⑥ 実投票クリック直前の停止フラグ再検査 (execute_purchase Step 8) ────────

class _FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    async def count(self):
        # 全削除ボタンは「無い」(カート空) 扱い、他は存在扱い
        return 0 if "全削除" in self.selector else 1

    async def is_disabled(self):
        # 「投票する」ボタンの is_disabled await 中に副作用を発火できる
        # (別プロセスが await 待ち中に abandoned フラグを作る TOCTOU の再現)
        if "投票する" in self.selector and self.page.vote_is_disabled_hook:
            self.page.vote_is_disabled_hook()
        return False

    async def click(self, timeout=None):
        self.page.clicks.append(self.selector)
        if "投票確認へ" in self.selector:
            self.page.confirmed = True
            self.page.url = self.page.confirm_url


class _FakePage:
    """execute_buy が Step 8 (実投票クリック) 直前まで進むだけの最小 fake。
    実クリック関数 (locator('投票する').click) が呼ばれたか clicks で観測する。"""

    def __init__(self, confirm_text: str, confirm_url: str):
        self.url = ""
        self.clicks: list[str] = []
        self.confirmed = False
        self._confirm_text = confirm_text
        self.confirm_url = confirm_url
        self.vote_is_disabled_hook = None  # click 前 await の副作用注入口

    def locator(self, selector):
        return _FakeLocator(self, selector)

    async def goto(self, url, **kw):
        self.url = url

    async def evaluate(self, script):
        # 確認遷移前 (カートクリア中) は空 → 「0組」扱い、遷移後は確認テキスト
        return self._confirm_text if self.confirmed else ""

    def on(self, event, cb):
        pass

    async def screenshot(self, **kw):
        pass


class _FakeContext:
    async def close(self):
        pass


class _FakeAsyncPWCM:
    async def __aenter__(self):
        return object()  # pw (使わない: _launch_context を差し替える)

    async def __aexit__(self, *a):
        return False


async def _acoro(value):
    return value


def _drive_execute_buy(fake_page, ab_flag: Path, tmp: Path,
                       bets: list[dict]):
    """execute_buy を全 fake ブラウザ (playwright/_launch_context/券種ヘルパ) と
    ROOT 一時 dir 差替で Step 8 まで駆動し、送出された RuntimeError を返す
    (無ければ None)。auto_buy.ABANDONED_STOP_FLAG は ab_flag に差し替える。
    実 data/ には一切書き込まない。"""
    import asyncio
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import execute_purchase as ep
    import playwright.async_api as _pw_api

    fake_ctx = _FakeContext()
    orig = {
        "ROOT": ep.ROOT,
        "_launch_context": ep._launch_context,
        "_normalize_bettype": ep._normalize_bettype,
        "_select_cars": ep._select_cars,
        "_set_amount_units": ep._set_amount_units,
        "_add_to_sheet": ep._add_to_sheet,
        "async_playwright": _pw_api.async_playwright,
        "ABANDONED_STOP_FLAG": auto_buy.ABANDONED_STOP_FLAG,
    }
    try:
        ep.ROOT = tmp
        auto_buy.ABANDONED_STOP_FLAG = ab_flag

        async def _fake_launch(pw):
            return fake_ctx, fake_page
        ep._launch_context = _fake_launch
        ep._normalize_bettype = lambda page, tab: _acoro(True)
        ep._select_cars = lambda page, bt, cars: _acoro(None)
        ep._set_amount_units = lambda page, amt: _acoro(True)
        ep._add_to_sheet = lambda page: _acoro(None)
        _pw_api.async_playwright = lambda: _FakeAsyncPWCM()

        try:
            asyncio.run(ep.execute_buy(
                "2099-01-01", 4, 7, dry_run=False, bets=bets))
            return None
        except RuntimeError as e:
            return e
    finally:
        ep.ROOT = orig["ROOT"]
        ep._launch_context = orig["_launch_context"]
        ep._normalize_bettype = orig["_normalize_bettype"]
        ep._select_cars = orig["_select_cars"]
        ep._set_amount_units = orig["_set_amount_units"]
        ep._add_to_sheet = orig["_add_to_sheet"]
        _pw_api.async_playwright = orig["async_playwright"]
        auto_buy.ABANDONED_STOP_FLAG = orig["ABANDONED_STOP_FLAG"]


_CONFIRM_URL = ("https://vote.autorace.jp/vote/confirm"
                "?vel_code=004&race_num=7")
_CONFIRM_TEXT = (
    "浜松 7R\n投票数 1組\n合計購入額 300円\n複勝 5 号\n"
    "ポイント残高\n1,000pt\n払戻金残高\n500円\n")


def _new_click_tmp():
    tmp = Path(tempfile.mkdtemp(prefix="ep_click_"))
    (tmp / "data").mkdir()  # screenshot/txt の書き出し先 (実 data/ を汚さない)
    return tmp, tmp / "abandoned_lock_stop.flag"


def test_click_precheck_blocks_when_flag_created_before_click():
    """入口通過後・実クリック直前に abandoned フラグが立つと「投票する」click は
    呼ばれず abort する (buy_app/直CLI は Auto mutex 非保持なので入口〜クリックの
    間に別 run がフラグを作れる)。ブラウザは全て fake、クリック関数の未発火を観測。"""
    tmp, ab_flag = _new_click_tmp()
    fake_page = _FakePage(_CONFIRM_TEXT, _CONFIRM_URL)
    # :769 検査時点から既にフラグがある状況 (最初の再検査で捕捉)
    ab_flag.write_text("detected_at=2099-01-01T00:00:00\n", encoding="utf-8")

    raised = _drive_execute_buy(
        fake_page, ab_flag, tmp, [{"type": "fns", "cars": [5], "amount": 300}])

    assert raised is not None, "停止フラグ下でも例外なく発注に進んでしまった"
    assert "実投票クリック直前" in str(raised), str(raised)
    # 「投票する」click は一度も呼ばれていない (確認へ click は起きてよい)
    assert not any("投票する" in s for s in fake_page.clicks), fake_page.clicks
    assert any("投票確認へ" in s for s in fake_page.clicks), \
        "テスト前提: 確認画面までは到達している"


def test_click_precheck_blocks_when_flag_created_during_awaits():
    """Codex第5R: :769 の再検査は通過し、その後の await (count / is_disabled)
    中に別プロセスが abandoned フラグを生成 → click **直前**の最終再検査で捕捉し
    「投票する」click は呼ばれず abort する (await 待ち中の TOCTOU を原子化)。
    is_disabled の await 副作用でフラグ生成タイミングを再現する。"""
    tmp, ab_flag = _new_click_tmp()
    fake_page = _FakePage(_CONFIRM_TEXT, _CONFIRM_URL)
    # :769 検査時点ではフラグ無し (通過)。「投票する」is_disabled await の
    # 副作用で「別プロセスが今フラグを作った」を再現する。
    fake_page.vote_is_disabled_hook = lambda: ab_flag.write_text(
        "detected_at=2099-01-01T00:00:00\n", encoding="utf-8")
    assert not ab_flag.exists()

    raised = _drive_execute_buy(
        fake_page, ab_flag, tmp, [{"type": "fns", "cars": [5], "amount": 300}])

    assert raised is not None, "await 中のフラグ生成を捕捉できず発注に進んだ"
    # click 直前の最終再検査で捕捉 (inner try に包まれ "投票する click 失敗" で
    # 再送出されるが、"click 直前" を含み、実 click は起きていない)
    assert "click 直前" in str(raised), str(raised)
    assert ab_flag.exists(), "await 副作用でフラグが生成されているはず"
    assert not any("投票する" in s for s in fake_page.clicks), fake_page.clicks
    assert any("投票確認へ" in s for s in fake_page.clicks), \
        "テスト前提: 確認画面までは到達している"


# ─── ⑦ 購入ゲート (プロセス間 TOCTOU の完全原子化, Codex第6R) ───────────────

def test_click_aborts_when_purchase_gate_unavailable():
    """購入ゲートが取得不能 (None) → 検査〜click の原子性を保証できないため
    click せず fail-closed で abort (buy_app/直CLI/auto_buy 全経路の最終防御)。"""
    tmp, ab_flag = _new_click_tmp()  # フラグ無し: ゲート起因の中止を明確化
    fake_page = _FakePage(_CONFIRM_TEXT, _CONFIRM_URL)
    orig_gate = auto_buy.acquire_purchase_gate
    try:
        auto_buy.acquire_purchase_gate = lambda *a, **kw: None
        raised = _drive_execute_buy(
            fake_page, ab_flag, tmp,
            [{"type": "fns", "cars": [5], "amount": 300}])
    finally:
        auto_buy.acquire_purchase_gate = orig_gate
    assert raised is not None, "ゲート取得不能でも発注に進んでしまった"
    assert "購入ゲート" in str(raised), str(raised)
    assert not any("投票する" in s for s in fake_page.clicks), fake_page.clicks


def test_purchase_gate_mutual_exclusion():
    """購入ゲート保持中は別スレッド (別プロセス相当) が同ゲートを取得できず、
    解放後に取得できる。これが「フラグ生成」と「最終検査→click」を同一排他
    区間に入れて原子化する土台になる。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    name = f"Global\\AutoRacingAITestGate_{uuid.uuid4().hex[:8]}"
    holder_ready = threading.Event()
    release_holder = threading.Event()
    res: dict = {}

    def _holder():
        g = auto_buy.acquire_purchase_gate(wait_sec=5, name=name)
        res["holder_got"] = g is not None
        holder_ready.set()
        release_holder.wait(5)
        auto_buy.release_purchase_gate(g)

    th = threading.Thread(target=_holder)
    th.start()
    try:
        assert holder_ready.wait(5)
        assert res.get("holder_got") is True, "holder がゲートを取得できていない"
        # 保持中は別スレッド (main) が即時取得できない (wait 0 → None)
        g_blocked = auto_buy.acquire_purchase_gate(wait_sec=0, name=name)
        assert g_blocked is None, "ゲート保持中に別スレッドが取得できてはいけない"
    finally:
        release_holder.set()
        th.join(5)
    # 解放後は取得できる
    g_after = auto_buy.acquire_purchase_gate(wait_sec=5, name=name)
    assert g_after is not None, "解放後は取得できる"
    auto_buy.release_purchase_gate(g_after)


def test_no_deadlock_run_mutex_then_purchase_gate():
    """デッドロック回避: ネスト順 run mutex → 購入ゲート の一方向。run mutex
    保持中に別スレッドがゲートを保持していても、解放後にゲートを取得でき
    ハングしない (click 側は run mutex を取らないため循環待ちが生じない)。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    run_name = f"Global\\AutoRacingAITest_{uuid.uuid4().hex[:8]}"
    gate_name = f"Global\\AutoRacingAITestGate_{uuid.uuid4().hex[:8]}"
    holder_ready = threading.Event()
    proceed = threading.Event()

    def _gate_holder():
        g = auto_buy.acquire_purchase_gate(wait_sec=5, name=gate_name)
        holder_ready.set()
        proceed.wait(5)  # main が gate 待ちに入る直前に解放
        auto_buy.release_purchase_gate(g)

    th = threading.Thread(target=_gate_holder)
    th.start()
    assert holder_ready.wait(5)
    # run mutex を先に取得 (ネストの外側)
    run_lock = auto_buy._acquire_lock(wait_sec=5, name=run_name)
    assert run_lock is not None and run_lock[2] is False
    try:
        proceed.set()  # gate_holder がゲートを解放
        # run mutex 保持中にゲートを取得 (循環しないのでデッドロックせず取れる)
        gate = auto_buy.acquire_purchase_gate(wait_sec=10, name=gate_name)
        assert gate is not None, \
            "run mutex 保持中でも gate 取得はデッドロックしない"
        auto_buy.release_purchase_gate(gate)
    finally:
        auto_buy._release_lock(run_lock)
        th.join(5)


def test_purchase_gate_ex_distinguishes_timeout_vs_broken():
    """Codex第8R: _acquire_purchase_gate_ex は「timeout(競合)」と「broken」を
    区別する。実 mutex を別スレッドで保持させ、競合が GATE_TIMEOUT (broken でない)
    と分類されること、解放後は GATE_OK になることを確認する。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    name = f"Global\\AutoRacingAITestGate_{uuid.uuid4().hex[:8]}"
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def _holder():
        g = auto_buy.acquire_purchase_gate(wait_sec=10, name=name)
        holder_ready.set()
        release_holder.wait(10)
        auto_buy.release_purchase_gate(g)

    th = threading.Thread(target=_holder)
    th.start()
    try:
        assert holder_ready.wait(5)
        # 競合中: wait 0 → GATE_TIMEOUT (broken ではない)
        status, gate = auto_buy._acquire_purchase_gate_ex(0, name=name)
        assert status == auto_buy.GATE_TIMEOUT and gate is None, (status, gate)
    finally:
        release_holder.set()
        th.join(5)
    # 解放後: GATE_OK
    status2, gate2 = auto_buy._acquire_purchase_gate_ex(2, name=name)
    assert status2 == auto_buy.GATE_OK and gate2 is not None, status2
    auto_buy.release_purchase_gate(gate2)


def _patch_gate_ex(seq):
    """`_acquire_purchase_gate_ex` を (status, gate) の列 seq を順に返す関数に
    差し替える (最後の要素で以後固定)。呼び出し回数を返り値に記録する。"""
    calls = {"n": 0}

    def _fake_ex(*a, **kw):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]
    return _fake_ex, calls


def test_generator_broken_does_not_write_but_ok_writes():
    """Codex第8R: 生成側は購入ゲート **GATE_OK のときだけ** フラグを書く。
    - GATE_BROKEN(mutex 生成不可) → 排他外 write せず、フラグ未生成 (逃げ道廃止)
    - GATE_OK → ゲート保持下で write + 解放
    _acquire_purchase_gate_ex を差し替えて status を制御 (子プロセス不要・全 OS 可)。"""
    with _AutoBuySandbox() as ab:
        flag = auto_buy.ABANDONED_STOP_FLAG
        cands = [_sanren_candidate()]
        orig_acquire = auto_buy._acquire_lock
        orig_release = auto_buy._release_lock
        orig_ex = auto_buy._acquire_purchase_gate_ex
        orig_relgate = auto_buy.release_purchase_gate
        orig_locked = auto_buy._run_auto_buy_locked
        try:
            auto_buy._acquire_lock = lambda *a, **kw: ("k32", "h", True)
            auto_buy._release_lock = lambda lk: None
            auto_buy._run_auto_buy_locked = _forbidden_execute
            released: list = []
            auto_buy.release_purchase_gate = lambda g: released.append(g)

            # (b) GATE_BROKEN → 非write・未生成・release 呼ばない
            fake_ex, _ = _patch_gate_ex([(auto_buy.GATE_BROKEN, None)])
            auto_buy._acquire_purchase_gate_ex = fake_ex
            assert not flag.exists()
            out = auto_buy.run_auto_buy(cands, dry_run=False)
            assert [r["verdict"] for r in out] == ["skip_abandoned_lock"], out
            assert not flag.exists(), "broken 時に排他外 write してはいけない"
            assert released == [], "broken 経路では release も呼ばない"
            assert len(ab.sent) == 1 and "ゲート" in ab.sent[0][1], ab.sent

            # (a) GATE_OK → 保持下で write + 解放
            ab.sent.clear()
            released.clear()
            fake_gate = ("k32gate", "hgate")
            fake_ex2, calls2 = _patch_gate_ex([(auto_buy.GATE_OK, fake_gate)])
            auto_buy._acquire_purchase_gate_ex = fake_ex2
            out2 = auto_buy.run_auto_buy(cands, dry_run=False)
            assert [r["verdict"] for r in out2] == ["skip_abandoned_lock"], out2
            assert flag.exists(), "GATE_OK 時はフラグを書く"
            assert calls2["n"] == 1 and released == [fake_gate], \
                (calls2, released)
        finally:
            auto_buy._acquire_lock = orig_acquire
            auto_buy._release_lock = orig_release
            auto_buy._acquire_purchase_gate_ex = orig_ex
            auto_buy.release_purchase_gate = orig_relgate
            auto_buy._run_auto_buy_locked = orig_locked


def test_generator_timeout_retries_then_writes():
    """Codex第8R: timeout(競合) は諦めず再試行し続け、取得できたらゲート下で write。
    _acquire_purchase_gate_ex が TIMEOUT を数回返した後 OK を返すよう差し替え、
    再試行回数と「取得後にのみ write」を検証する (排他外 write なし)。"""
    with _AutoBuySandbox() as ab:
        flag = auto_buy.ABANDONED_STOP_FLAG
        cands = [_sanren_candidate()]
        orig_acquire = auto_buy._acquire_lock
        orig_release = auto_buy._release_lock
        orig_ex = auto_buy._acquire_purchase_gate_ex
        orig_relgate = auto_buy.release_purchase_gate
        orig_locked = auto_buy._run_auto_buy_locked
        try:
            auto_buy._acquire_lock = lambda *a, **kw: ("k32", "h", True)
            auto_buy._release_lock = lambda lk: None
            auto_buy._run_auto_buy_locked = _forbidden_execute
            released: list = []
            auto_buy.release_purchase_gate = lambda g: released.append(g)

            fake_gate = ("k32gate", "hgate")
            fake_ex, calls = _patch_gate_ex([
                (auto_buy.GATE_TIMEOUT, None),
                (auto_buy.GATE_TIMEOUT, None),
                (auto_buy.GATE_OK, fake_gate)])
            auto_buy._acquire_purchase_gate_ex = fake_ex
            assert not flag.exists()
            out = auto_buy.run_auto_buy(cands, dry_run=False)
            assert [r["verdict"] for r in out] == ["skip_abandoned_lock"], out
            assert calls["n"] == 3, f"competition を諦めず再試行するはず: {calls}"
            assert flag.exists(), "取得後にフラグを書く"
            assert released == [fake_gate], released
        finally:
            auto_buy._acquire_lock = orig_acquire
            auto_buy._release_lock = orig_release
            auto_buy._acquire_purchase_gate_ex = orig_ex
            auto_buy.release_purchase_gate = orig_relgate
            auto_buy._run_auto_buy_locked = orig_locked


def test_generator_total_timeout_last_resort_writes():
    """Codex第8R 最後の砦: 総上限を超えても競合が解消しない異常時のみ、
    best-effort でフラグを書き後続購入を停止する (この click 単体の原子性は
    既に達成不能だが sticky halt を残す)。総上限を極小にして到達を再現。"""
    with _AutoBuySandbox() as ab:
        flag = auto_buy.ABANDONED_STOP_FLAG
        cands = [_sanren_candidate()]
        orig_acquire = auto_buy._acquire_lock
        orig_release = auto_buy._release_lock
        orig_ex = auto_buy._acquire_purchase_gate_ex
        orig_total = auto_buy.PURCHASE_GATE_TOTAL_WAIT_SEC
        orig_locked = auto_buy._run_auto_buy_locked
        try:
            auto_buy._acquire_lock = lambda *a, **kw: ("k32", "h", True)
            auto_buy._release_lock = lambda lk: None
            auto_buy._run_auto_buy_locked = _forbidden_execute
            auto_buy.PURCHASE_GATE_TOTAL_WAIT_SEC = 0.2  # 総上限を極小化
            # 常に TIMEOUT (競合が永久に解消しない異常事態を模擬)
            fake_ex, calls = _patch_gate_ex([(auto_buy.GATE_TIMEOUT, None)])
            auto_buy._acquire_purchase_gate_ex = fake_ex
            assert not flag.exists()
            out = auto_buy.run_auto_buy(cands, dry_run=False)
            assert [r["verdict"] for r in out] == ["skip_abandoned_lock"], out
            assert calls["n"] >= 1
            # 最後の砦で best-effort write されている
            assert flag.exists(), "総上限超過時は best-effort でフラグを書く"
            assert len(ab.sent) == 1 and "best-effort" in ab.sent[0][1], ab.sent
        finally:
            auto_buy._acquire_lock = orig_acquire
            auto_buy._release_lock = orig_release
            auto_buy._acquire_purchase_gate_ex = orig_ex
            auto_buy.PURCHASE_GATE_TOTAL_WAIT_SEC = orig_total
            auto_buy._run_auto_buy_locked = orig_locked


def test_generator_waits_for_gate_no_out_of_lock_write():
    """Codex第7R 原子性: click 側 (別スレッド) がゲート保持中は生成側は待機し、
    その間フラグを書かない (排他外 write が起きない)。解放後にゲート下で書く。
    実ゲート mutex を使い、生成側は別スレッドでブロックさせて観測する。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    with _AutoBuySandbox() as ab:
        flag = auto_buy.ABANDONED_STOP_FLAG
        gate_name = ab.gate_name
        orig_acquire = auto_buy._acquire_lock
        orig_release = auto_buy._release_lock
        orig_locked = auto_buy._run_auto_buy_locked

        holder_ready = threading.Event()
        release_holder = threading.Event()

        def _holder():
            g = auto_buy.acquire_purchase_gate(wait_sec=15, name=gate_name)
            holder_ready.set()
            release_holder.wait(15)
            auto_buy.release_purchase_gate(g)

        th = threading.Thread(target=_holder)
        th.start()
        assert holder_ready.wait(5), "holder がゲートを取得できていない"
        try:
            auto_buy._acquire_lock = lambda *a, **kw: ("k32", "h", True)
            auto_buy._release_lock = lambda lk: None
            auto_buy._run_auto_buy_locked = _forbidden_execute

            gen_done = threading.Event()
            gen_out: dict = {}

            def _gen():
                gen_out["out"] = auto_buy.run_auto_buy(
                    [_sanren_candidate()], dry_run=False)
                gen_done.set()

            gt = threading.Thread(target=_gen)
            gt.start()
            # 生成側はゲート待ちでブロック → この間フラグは書かれない
            assert not gen_done.wait(1.0), \
                "ゲート保持中に生成側が完了してしまった (待機していない)"
            assert not flag.exists(), \
                "ゲート保持中に排他外 write してはいけない"
            # 解放 → 生成側がゲート取得して write (デッドロックせず完了)
            release_holder.set()
            assert gen_done.wait(20), "解放後も生成側が完了しない (デッドロック?)"
            assert flag.exists(), "解放後はゲート保持下でフラグを書く"
            assert [r["verdict"] for r in gen_out["out"]] == \
                ["skip_abandoned_lock"], gen_out["out"]
        finally:
            release_holder.set()
            auto_buy._acquire_lock = orig_acquire
            auto_buy._release_lock = orig_release
            auto_buy._run_auto_buy_locked = orig_locked
            th.join(5)


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
