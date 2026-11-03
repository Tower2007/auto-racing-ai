"""daily_predict.candidate_mail_decision の単体テスト (2026-11-03)。

自動発注が全候補を処理した R では買い候補メールを送らず、自動発注が動かなかった /
処理しきれなかった時だけ手動購入用に送ることを確認する。
`python tests/test_candidate_mail.py` でも実行可能。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daily_predict import candidate_mail_decision  # noqa: E402


def _v(*verdicts):
    return [{"race": f"hamamatsu_R{i + 1}", "verdict": v} for i, v in enumerate(verdicts)]


def _decide(**kw):
    base = dict(n_picks=2, auto_buy_enabled=True, dry_run=False, send_always=False,
                n_candidates=2, verdicts=_v("executed", "executed"), auto_buy_error=False)
    base.update(kw)
    return candidate_mail_decision(**base)


def test_all_executed_suppresses():
    send, _ = _decide()
    assert send is False


def test_guard_skip_and_duplicate_still_suppress():
    # 日次上限などの意図した見送り / 二重発注防止は AUTO-BUY メールがあるので送らない
    send, _ = _decide(verdicts=_v("skip_daily_cap (spent 3000 + 300 > 3000)", "skip_duplicate"))
    assert send is False


def test_failed_sends_fallback():
    send, reason = _decide(verdicts=_v("executed", "failed"))
    assert send is True and "failed" in reason


def test_skip_lock_sends_fallback():
    send, reason = _decide(verdicts=_v("skip_lock", "skip_lock"))
    assert send is True and "skip_lock" in reason


def test_exception_sends_fallback():
    send, _ = _decide(verdicts=None, auto_buy_error=True)
    assert send is True


def test_missing_verdicts_sends():
    send, _ = _decide(verdicts=_v("executed"))
    assert send is True


def test_pick_not_in_candidates_sends():
    # 複勝 OFF 等で自動発注の対象外になった候補がある時は情報メールを残す
    send, _ = _decide(n_candidates=1, verdicts=_v("executed"))
    assert send is True


def test_disabled_dry_run_and_override_send():
    assert _decide(auto_buy_enabled=False)[0] is True
    assert _decide(dry_run=True)[0] is True
    assert _decide(send_always=True)[0] is True


def test_no_picks_defers_to_suppress_noresult():
    send, _ = _decide(n_picks=0, n_candidates=0, verdicts=[])
    assert send is True


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
