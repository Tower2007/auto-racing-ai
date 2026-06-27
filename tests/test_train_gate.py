"""品質ゲート _should_adopt の単体テスト (2026-06-26 公平比較版)。

run: python tests/test_train_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ml.train_production import (  # noqa: E402
    _should_adopt, ADOPT_TOL_AUC, STALE_DAYS, STALE_TOL_AUC,
    TARGET_DEFINITION_VERSION,
)

OLD = {
    "trained_at": "2026-04-29T15:00:00",
    "target_definition_version": TARGET_DEFINITION_VERSION,
    "train_metrics": {"valid_auc": 0.8266, "best_iteration": 271},
}


def _m(auc, best_iter=200):
    return {"valid_auc": auc, "best_iteration": best_iter, "valid_logloss": 0.5}


def test_no_old_model():
    v, _ = _should_adopt(_m(0.80), None, None, None)
    assert v == "OK"


def test_force():
    v, _ = _should_adopt(_m(0.10), OLD, 0.99, 1, force=True)
    assert v == "OK"


def test_fair_candidate_better():
    # 同一val で候補 > 現役 → OK (旧ゲートなら record0.8266 比較で NG だった)
    v, r = _should_adopt(_m(0.8235), OLD, 0.8230, 5)
    assert v == "OK" and "公平比較" in r


def test_fair_within_tolerance():
    # 候補がわずかに下だが許容内 → OK
    v, _ = _should_adopt(_m(0.8230 - ADOPT_TOL_AUC + 0.0005), OLD, 0.8230, 5)
    assert v == "OK"


def test_fair_worse_fresh_model_NG():
    # 許容超で劣り、モデルが新しい(鮮度オーバーライド対象外) → NG
    v, _ = _should_adopt(_m(0.8230 - 0.02), OLD, 0.8230, model_age_days=5)
    assert v == "NG"


def test_staleness_override():
    # 許容超で劣るが STALE_DAYS 超 かつ STALE_TOL 以内 → WARN 採用
    auc = 0.8230 - (ADOPT_TOL_AUC + STALE_TOL_AUC) / 2  # 0.003〜0.010 の間
    v, r = _should_adopt(_m(auc), OLD, 0.8230, model_age_days=STALE_DAYS + 10)
    assert v == "WARN" and "鮮度" in r


def test_staleness_override_too_bad_NG():
    # 鮮度超でも STALE_TOL を超えて劣る → NG
    v, _ = _should_adopt(_m(0.8230 - 0.05), OLD, 0.8230,
                         model_age_days=STALE_DAYS + 10)
    assert v == "NG"


def test_degenerate_best_iter_NG():
    # best_iter が現役の40%未満 → 同一val で勝っていても無条件 NG
    v, r = _should_adopt(_m(0.99, best_iter=50), OLD, 0.80, 5)
    assert v == "NG" and "best_iteration" in r


def test_fallback_no_incumbent_rescore():
    # 再採点不能(None)時は record 比較に fallback、許容内なら OK
    v, r = _should_adopt(_m(0.8266 - ADOPT_TOL_AUC + 0.0005), OLD, None, 5)
    assert v == "OK" and "fallback" in r


if __name__ == "__main__":
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
            print(f"  ERROR {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
