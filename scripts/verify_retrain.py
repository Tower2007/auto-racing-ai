"""再学習後検証スクリプト (2026-05-02 レビュー指摘 #2 対応の事後確認用)

確認項目:
  1. ml_features.parquet の過去 race_date 行で finished=0 が混入していないこと
     (今回の修正で finished の意味が「結果取得済」に変わり、過去データは全 1 のはず)
  2. 週次再学習 (日曜 03:00) 後の production_meta.json の valid_auc / logloss が
     再学習前 (data/production_meta.prev.json として手動 snapshot 済) から
     大きく劣化していないこと

使い方:
  python scripts/verify_retrain.py

異常時は exit code 1、正常時は 0。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

AUC_DROP_THR = 0.01      # 0.01 (= 1pt) 以上の AUC 低下で警告
LOGLOSS_RISE_THR = 0.01  # 0.01 以上の logloss 上昇で警告


def check_finished_regression() -> tuple[bool, str]:
    feat = DATA / "ml_features.parquet"
    if not feat.exists():
        return False, f"NOT FOUND: {feat}"
    df = pd.read_parquet(feat, columns=["race_date", "finished", "is_absent"])
    df["race_date"] = pd.to_datetime(df["race_date"])
    today = pd.Timestamp.today().normalize()

    past = df[df["race_date"] < today]
    bad = past[past["finished"] == 0]
    if len(bad) == 0:
        return True, f"OK (past rows: {len(past):,}, finished=0: 0)"
    return False, f"REGRESSION: {len(bad):,} past rows have finished=0 (expected 0)"


def check_metrics_change() -> tuple[bool, str]:
    cur_p = DATA / "production_meta.json"
    prev_p = DATA / "production_meta.prev.json"
    if not cur_p.exists():
        return False, f"NOT FOUND: {cur_p}"
    if not prev_p.exists():
        return False, f"NOT FOUND: {prev_p} (再学習前 snapshot が無い)"

    cur = json.loads(cur_p.read_text(encoding="utf-8"))
    prev = json.loads(prev_p.read_text(encoding="utf-8"))

    if cur.get("trained_at") == prev.get("trained_at"):
        return False, (
            f"NO RETRAIN: trained_at unchanged ({cur.get('trained_at')}). "
            "AutoraceWeeklyRetrain has not run yet?"
        )

    def _metric(d: dict, key: str) -> float | None:
        v = d.get(key)
        if v is not None:
            return v
        # ネスト構造の場合 (validation_metrics 等) を探索
        for vv in d.values():
            if isinstance(vv, dict) and key in vv:
                return vv[key]
        return None

    cur_auc = _metric(cur, "valid_auc")
    prev_auc = _metric(prev, "valid_auc")
    cur_ll = _metric(cur, "valid_logloss")
    prev_ll = _metric(prev, "valid_logloss")

    msgs = [
        f"trained_at: {prev.get('trained_at')} -> {cur.get('trained_at')}",
        f"n_train: {_metric(prev, 'n_train')} -> {_metric(cur, 'n_train')}",
        f"valid_auc: {prev_auc:.4f} -> {cur_auc:.4f} (diff {cur_auc - prev_auc:+.4f})",
        f"valid_logloss: {prev_ll:.4f} -> {cur_ll:.4f} (diff {cur_ll - prev_ll:+.4f})",
    ]

    issues = []
    if cur_auc - prev_auc < -AUC_DROP_THR:
        issues.append(f"AUC dropped >= {AUC_DROP_THR}")
    if cur_ll - prev_ll > LOGLOSS_RISE_THR:
        issues.append(f"logloss rose >= {LOGLOSS_RISE_THR}")

    if issues:
        return False, "DEGRADATION: " + "; ".join(issues) + "\n  " + "\n  ".join(msgs)
    return True, "OK\n  " + "\n  ".join(msgs)


def main() -> int:
    print("=" * 60)
    print("auto-racing-ai post-retrain verification")
    print("=" * 60)

    ok1, msg1 = check_finished_regression()
    print(f"\n[1] features.parquet finished flag regression check")
    print(f"    {msg1}")

    ok2, msg2 = check_metrics_change()
    print(f"\n[2] production model metrics change")
    print(f"    {msg2}")

    print()
    if ok1 and ok2:
        print("RESULT: OK")
        return 0
    print("RESULT: ISSUES FOUND (see messages above)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
