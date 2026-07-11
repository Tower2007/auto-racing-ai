"""2026-07-11 監査 P2 + Codex 追加指摘の単体テスト。

対象:
  1. 三連系 全場・全期間 絶対損失バックストップ (src/backstop.py)
     - -¥10,000 で発動 / 未満では発動しない / sticky (データ改善でも解除されない)
     - 発動時に通知が飛ぶ (monkeypatch、実メールは送らない)
     - daily_predict.rt3_buy_active() がバックストップフラグで False になる
  2. kill-switch 現役監視の場×券種 厳密一致 (weekly_status.check_3point_health)
     - 廃止済みペア (伊勢崎 rt3) が現役集計から除外される
  3. ingest manifest の hard-kill 穴 (ingest_day.ingest_one_day)
     - 取込開始時に partial 行が先に書かれ、プロセス kill 相当
       (KeyboardInterrupt) でも manifest=partial が残る
     - 正常完了 (no_race) では最終行が優先される

発注経路 (execute_purchase / run_auto_buy) には一切到達しない。
pytest があれば `pytest tests/test_backstop_and_scope.py`、
無くても `python tests/test_backstop_and_scope.py` で実行可能。
"""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.backstop as backstop  # noqa: E402
from src.strategy_config import (  # noqa: E402
    THREE_POINT_BACKSTOP_LOSS_YEN,
    THREE_POINT_POLICY_PAIRS,
)

DETAIL_FIELDS = ["date", "place_code", "place_name", "race_no", "order_id",
                 "created_at", "bet_type_code", "bet_type_label", "pack_deme",
                 "pack_votes", "vote_amount", "hit_amount", "henkan_amount",
                 "tokubarai_amount"]


def _write_detail(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=DETAIL_FIELDS)
        w.writeheader()
        for r in rows:
            base = {k: "" for k in DETAIL_FIELDS}
            base.update(r)
            w.writerow(base)


def _detail_row(pc: int, bt: str, vote: int, hit: int, race_no: int = 1,
                date: str = "2026-07-01") -> dict:
    return {"date": date, "place_code": pc, "race_no": race_no,
            "order_id": f"o{pc}{bt}{race_no}", "bet_type_code": bt,
            "vote_amount": vote, "hit_amount": hit,
            "henkan_amount": 0, "tokubarai_amount": 0}


class _BackstopSandbox:
    """src.backstop のパス・通知を tmp に差し替える (try/finally 用)。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="backstop_test_"))
        self.detail = self.tmp / "bet_history_detail.csv"
        self.flag = self.tmp / "rt3_backstop_stop.flag"
        self.sent: list[tuple[str, str]] = []
        self._orig = (backstop.DETAIL_CSV, backstop.BACKSTOP_FLAG,
                      backstop._send_notify)

    def __enter__(self):
        backstop.DETAIL_CSV = self.detail
        backstop.BACKSTOP_FLAG = self.flag
        backstop._send_notify = lambda subject, body: self.sent.append(
            (subject, body))
        return self

    def __exit__(self, *exc):
        (backstop.DETAIL_CSV, backstop.BACKSTOP_FLAG,
         backstop._send_notify) = self._orig
        return False


def test_backstop_constant_and_policy_pairs():
    assert THREE_POINT_BACKSTOP_LOSS_YEN == -10_000
    # 正本ペア: 伊勢崎は rf3 のみ (rt3 は廃止済みペアとして含まれない)
    assert (3, "rf3") in THREE_POINT_POLICY_PAIRS
    assert (3, "rt3") not in THREE_POINT_POLICY_PAIRS
    assert (4, "rt3") in THREE_POINT_POLICY_PAIRS
    assert (6, "rf3") in THREE_POINT_POLICY_PAIRS
    assert (5, "rf3") not in THREE_POINT_POLICY_PAIRS  # 飯塚は除外済み


def test_backstop_not_triggered_below_threshold():
    with _BackstopSandbox() as sb:
        # 全場累積 -9,900 (> -10,000) → 発動しない
        _write_detail(sb.detail, [
            _detail_row(5, "rf3", vote=10_000, hit=100, race_no=1),
            _detail_row(2, "fns", vote=999, hit=0, race_no=2),  # 三連系外は無視
        ])
        ev = backstop.evaluate_backstop()
        assert ev["profit"] == -9_900 and ev["breached"] is False
        out = backstop.enforce_backstop()
        assert out["active"] is False and out["newly_triggered"] is False
        assert not sb.flag.exists()
        assert sb.sent == []


def test_backstop_triggers_at_threshold_and_is_sticky():
    with _BackstopSandbox() as sb:
        # ちょうど -10,000 → 発動 (<= 判定)
        _write_detail(sb.detail, [
            _detail_row(5, "rf3", vote=6_000, hit=0, race_no=1),
            _detail_row(4, "rt3", vote=5_000, hit=1_000, race_no=2),
        ])
        out = backstop.enforce_backstop()
        assert out["profit"] == -10_000
        assert out["active"] is True and out["newly_triggered"] is True
        assert sb.flag.exists()
        assert len(sb.sent) == 1 and "バックストップ" in sb.sent[0][0]
        flag_text = sb.flag.read_text(encoding="utf-8")
        assert "sticky" in flag_text and "人間" in flag_text

        # sticky: 損益がプラスに転じても (場の廃止/追加・データ差し替えでも)
        # フラグがある限り停止のまま。再通知・再書き込みもしない。
        _write_detail(sb.detail, [
            _detail_row(4, "rt3", vote=100, hit=99_999, race_no=3),
        ])
        out2 = backstop.enforce_backstop()
        assert out2["active"] is True and out2["newly_triggered"] is False
        assert len(sb.sent) == 1  # 再送なし
        assert backstop.backstop_active() is True

        # 解除は人間のフラグ削除のみ
        sb.flag.unlink()
        out3 = backstop.enforce_backstop()
        assert out3["active"] is False


def test_backstop_eval_failure_fail_closed():
    """2026-07-12 Codex艦隊監査 P2-2: 履歴読取失敗は fail-closed。

    - enforce_backstop() は「発動」とは扱わない (sticky フラグ書き出し・通知なし)
    - ただし購入ゲート backstop_blocks_purchase() は True (三連系購入スキップ)
    """
    with _BackstopSandbox() as sb:
        # detail が存在しない → 未評価
        out = backstop.enforce_backstop()
        assert out["active"] is False and out["profit"] is None
        assert out["error"]
        assert not sb.flag.exists() and sb.sent == []   # 真の発動と区別
        # 購入ゲートは fail-closed で停止
        assert backstop.backstop_blocks_purchase() is True
        # 壊れた detail (集計不能) でも fail-closed
        sb.detail.write_bytes(b"\xff\xfe\x00broken")
        assert backstop.backstop_blocks_purchase() is True


def test_backstop_blocks_purchase_states():
    """購入ゲートの4状態: 健全 → False / 閾値割れ (フラグ未書出) → True /
    フラグ存在 → True / 読取不能 → True (fail-closed)。"""
    with _BackstopSandbox() as sb:
        # 健全 (閾値内)
        _write_detail(sb.detail, [_detail_row(4, "rt3", vote=100, hit=200)])
        assert backstop.backstop_blocks_purchase() is False
        # 閾値割れ: enforce (フラグ書き出し) 前でも購入は止まる
        _write_detail(sb.detail, [_detail_row(4, "rt3", vote=20_000, hit=0)])
        assert backstop.backstop_blocks_purchase() is True
        assert not sb.flag.exists()   # ゲートはフラグを書かない (enforce の仕事)
        # フラグ存在 (sticky): detail が健全でも停止
        _write_detail(sb.detail, [_detail_row(4, "rt3", vote=100, hit=200)])
        sb.flag.write_text("x", encoding="utf-8")
        assert backstop.backstop_blocks_purchase() is True


def test_rt3_buy_active_gated_by_backstop_flag():
    import daily_predict
    with _BackstopSandbox() as sb:
        orig_stop = daily_predict.RT3_STOP_FLAG
        try:
            daily_predict.RT3_STOP_FLAG = sb.tmp / "rt3_stop.flag"  # 無し
            # P2-2: detail 不在は fail-closed → まず健全な detail を置く
            _write_detail(sb.detail, [_detail_row(4, "rt3", vote=100, hit=200)])
            assert daily_predict.rt3_buy_active() is True
            sb.flag.write_text("test", encoding="utf-8")  # backstop flag ON
            assert daily_predict.rt3_buy_active() is False
            sb.flag.unlink()
            assert daily_predict.rt3_buy_active() is True
            # P2-2: detail が読めない → fail-closed で False
            sb.detail.unlink()
            assert daily_predict.rt3_buy_active() is False
            _write_detail(sb.detail, [_detail_row(4, "rt3", vote=100, hit=200)])
            assert daily_predict.rt3_buy_active() is True
            # 既存 kill-switch フラグも従来通り効く
            daily_predict.RT3_STOP_FLAG.write_text("x", encoding="utf-8")
            assert daily_predict.rt3_buy_active() is False
        finally:
            daily_predict.RT3_STOP_FLAG = orig_stop


def test_check_3point_health_strict_pair_scope():
    import weekly_status
    with _BackstopSandbox() as sb:
        tmp_data = sb.tmp / "wsdata"
        tmp_data.mkdir()
        # 現役監視の入力: 伊勢崎 rt3 (廃止済みペア, +1,280) を混ぜる
        _write_detail(tmp_data / "bet_history_detail.csv", [
            _detail_row(3, "rt3", vote=700, hit=1_980, race_no=1),   # 除外対象
            _detail_row(3, "rf3", vote=1_000, hit=1_440, race_no=2),
            _detail_row(4, "rt3", vote=100, hit=0, race_no=3),
        ])
        # backstop 側 (⑤) も同じ tmp detail を見る (全場: +1,620 → OK)
        backstop.DETAIL_CSV = tmp_data / "bet_history_detail.csv"
        orig = (weekly_status.DATA, weekly_status.RT3_STOP_FLAG)
        try:
            weekly_status.DATA = tmp_data
            weekly_status.RT3_STOP_FLAG = tmp_data / "rt3_stop.flag"
            out = weekly_status.check_3point_health()
        finally:
            weekly_status.DATA, weekly_status.RT3_STOP_FLAG = orig
        # 伊勢崎 rt3 (vote 700 / hit 1980) が現役集計から除外されている
        assert out["invest"] == 1_100, out
        assert out["payout"] == 1_440, out
        assert out["profit"] == 340, out
        assert out["n_picks"] == 2, out
        assert out["triggered"] is False
        # 条件⑤ (全場バックストップ) が OK で入っている
        c5 = [c for c in out["conditions"] if c[0] == "⑤"]
        assert len(c5) == 1 and c5[0][1] == "OK", out["conditions"]
        assert not sb.flag.exists() and sb.sent == []


def test_ingest_manifest_partial_written_before_fetch():
    """取込開始マーカ: R1 取得中にプロセス kill 相当が起きても partial が残る。"""
    import ingest_day

    class _KilledClient:
        def get_program(self, *a, **kw):
            raise KeyboardInterrupt  # except Exception では捕まらない = kill 相当

    tmp = Path(tempfile.mkdtemp(prefix="ingest_manifest_test_"))
    orig_manifest = ingest_day.MANIFEST_CSV
    orig_has = ingest_day.has_race_day
    try:
        ingest_day.MANIFEST_CSV = tmp / "ingest_manifest.csv"
        ingest_day.has_race_day = lambda *a, **kw: False
        interrupted = False
        try:
            ingest_day.ingest_one_day(_KilledClient(), 4, "2099-01-01")
        except KeyboardInterrupt:
            interrupted = True
        assert interrupted
        # 完了行は書けていないが、開始マーカの partial が残っている
        assert ingest_day.manifest_status(4, "2099-01-01") == "partial"
    finally:
        ingest_day.MANIFEST_CSV = orig_manifest
        ingest_day.has_race_day = orig_has


def test_ingest_manifest_final_status_overrides_start_marker():
    """正常系: 開始マーカ partial の後に完了行 (no_race) が優先される。
    2026-07-12 P2-1: no_race は RaceRefund の裏取り (0件) がある時のみ。"""
    import ingest_day

    class _NoRaceClient:
        def get_program(self, *a, **kw):
            return {"body": []}  # 開催なし

        def get_race_refund(self, *a, **kw):
            return {"body": []}  # 払戻も0件 = 開催なしの裏取り

    tmp = Path(tempfile.mkdtemp(prefix="ingest_manifest_test2_"))
    orig_manifest = ingest_day.MANIFEST_CSV
    orig_has = ingest_day.has_race_day
    try:
        ingest_day.MANIFEST_CSV = tmp / "ingest_manifest.csv"
        ingest_day.has_race_day = lambda *a, **kw: False
        counts = ingest_day.ingest_one_day(_NoRaceClient(), 4, "2099-01-02")
        assert counts == {}
        assert ingest_day.manifest_status(4, "2099-01-02") == "no_race"
        # manifest には partial(開始) → no_race(完了) の 2 行がある
        with open(ingest_day.MANIFEST_CSV, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert [r["status"] for r in rows] == ["partial", "no_race"]
        # 次回は skip される
        again = ingest_day.ingest_one_day(_NoRaceClient(), 4, "2099-01-02")
        assert again == {"skipped": True}
    finally:
        ingest_day.MANIFEST_CSV = orig_manifest
        ingest_day.has_race_day = orig_has


def test_ingest_empty_r1_without_corroboration_is_partial():
    """2026-07-12 Codex艦隊監査 P2-1: Program R1 の空応答だけでは no_race に
    しない。RaceRefund で裏取りできない (失敗 or 払戻あり) 場合は partial の
    まま次回再取得に委ねる。"""
    import ingest_day

    class _RefundFailsClient:
        def get_program(self, *a, **kw):
            return {"body": []}   # 空応答 (一時障害かもしれない)

        def get_race_refund(self, *a, **kw):
            raise RuntimeError("API down")

    class _RefundContradictsClient:
        def get_program(self, *a, **kw):
            return {"body": []}   # 空応答

        def get_race_refund(self, *a, **kw):
            return {"body": [{"raceNo": 1}]}   # 払戻あり = 実は開催日

    tmp = Path(tempfile.mkdtemp(prefix="ingest_empty_r1_test_"))
    orig_manifest = ingest_day.MANIFEST_CSV
    orig_has = ingest_day.has_race_day
    try:
        ingest_day.MANIFEST_CSV = tmp / "ingest_manifest.csv"
        ingest_day.has_race_day = lambda *a, **kw: False
        counts = ingest_day.ingest_one_day(_RefundFailsClient(), 4, "2099-02-01")
        assert counts == {}
        assert ingest_day.manifest_status(4, "2099-02-01") == "partial"
        counts = ingest_day.ingest_one_day(_RefundContradictsClient(), 4, "2099-02-02")
        assert counts == {}
        assert ingest_day.manifest_status(4, "2099-02-02") == "partial"
    finally:
        ingest_day.MANIFEST_CSV = orig_manifest
        ingest_day.has_race_day = orig_has


def test_ingest_midloop_empty_with_payout_marks_partial():
    """2026-07-12 Codex艦隊監査 P2-1: ループ途中の空応答 (払戻実績のある R) を
    「カード終わり」と誤認して ok と記録しない — partial に倒す。"""
    import ingest_day

    class _MidloopEmptyClient:
        """R1 は正常、R2 は空応答 (だが払戻あり = 取りこぼし)、R3〜 は本当に無い。"""

        def get_program(self, pc, date, race_no):
            if race_no == 1:
                return {"body": {"playerList": [{"carNo": 1}]}}
            return {"body": []}

        def get_race_refund(self, *a, **kw):
            return {"body": [{"raceNo": 1}, {"raceNo": 2}]}

        def get_odds(self, *a, **kw):
            return {"body": []}       # list = オッズなし (行は書かない)

        def get_race_result(self, *a, **kw):
            return {"body": []}       # list = 結果なし (行は書かない)

    tmp = Path(tempfile.mkdtemp(prefix="ingest_midloop_test_"))
    orig = {k: getattr(ingest_day, k) for k in (
        "MANIFEST_CSV", "has_race_day", "append_rows",
        "parse_program_entries", "parse_program_stats", "parse_payouts")}
    try:
        ingest_day.MANIFEST_CSV = tmp / "ingest_manifest.csv"
        ingest_day.has_race_day = lambda *a, **kw: False
        ingest_day.append_rows = lambda name, rows: len(rows)  # 実CSVに書かない
        ingest_day.parse_program_entries = lambda *a, **kw: [{"r": 1}]
        ingest_day.parse_program_stats = lambda *a, **kw: []
        ingest_day.parse_payouts = lambda *a, **kw: []
        counts = ingest_day.ingest_one_day(_MidloopEmptyClient(), 4, "2099-03-01")
        # R2 の取りこぼしが errors としてカウントされ、日は partial (ok にしない)
        assert ingest_day.manifest_status(4, "2099-03-01") == "partial"
        assert counts.get("entries") == 1   # R1 は取り込まれている
    finally:
        for k, v in orig.items():
            setattr(ingest_day, k, v)


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
