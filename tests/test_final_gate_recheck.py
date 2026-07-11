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
        self._orig: dict = {}

    def __enter__(self):
        self._orig = {k: getattr(auto_buy, k) for k in (
            "AUTO_BUY_ENABLED", "LOCK_WAIT_SEC", "STATE_FILE", "DATA",
            "BET_HISTORY_CSV", "ABANDONED_STOP_FLAG", "_notify",
            "_run_execute_purchase", "_mutex_name", "check_guards")}
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
