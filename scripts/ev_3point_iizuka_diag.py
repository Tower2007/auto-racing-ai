"""飯塚の 3連単(rt3)弱体化が構造かノイズか切り分け

同じ eval set + thr=1.45 の picks を場別 × 月別に分解し、rt3 ROI/hit_rate を確認。
- 飯塚が「全月で平均的に低い」=構造的弱さ
- 飯塚が「数ヶ月だけ凹みで他は普通」=外れ値ノイズ
- ついでに浜松の高 rt3 ROI が外れ値依存か(月別 max - 全体平均) も合わせて見る
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ev_3point_buy import load_eval_set, get_top3_per_race, load_payouts, evaluate_3point  # noqa: E402

PLACE_NAMES = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
THR = 1.45


def attach_payout_flags(picks: pd.DataFrame, payouts: dict) -> pd.DataFrame:
    """全 picks に rt3_payout / rf3_payout / fns_payout を付与(thr フィルタ前)。"""
    full = evaluate_3point(picks, payouts)  # ignored; we re-do per-place below
    return full  # placeholder, not used


def evaluate_place_month(sub: pd.DataFrame, payouts: dict) -> dict | None:
    if sub.empty:
        return None
    return evaluate_3point(sub, payouts)


def _print_place_summary(df: pd.DataFrame, col: str) -> None:
    print(f"{'場':<6} {'months':>6} {'mean':>7} {'median':>7} {'std':>7} "
          f"{'min':>7} {'max':>8} {'>=100%':>7}")
    print("-" * 70)
    for name in PLACE_NAMES.values():
        d = df[df["place"] == name]
        if d.empty:
            continue
        n_ge1 = int((d[col] >= 100).sum())
        print(f"{name:<6} {len(d):>6} "
              f"{d[col].mean():>6.1f}% "
              f"{d[col].median():>6.1f}% "
              f"{d[col].std():>6.1f}% "
              f"{d[col].min():>6.1f}% "
              f"{d[col].max():>7.1f}% "
              f"{n_ge1:>4}/{len(d):<2}")


def main():
    eval_df = load_eval_set()
    picks = get_top3_per_race(eval_df)
    payouts = load_payouts()

    # test_month を picks に付与(get_top3_per_race は test_month 落とすので再 merge)
    tm = eval_df[["race_date", "place_code", "race_no", "test_month"]].drop_duplicates()
    picks = picks.merge(tm, on=["race_date", "place_code", "race_no"], how="left")

    sel = picks[picks["ev_avg_calib"] >= THR].dropna(subset=["pick1", "pick2", "pick3"])
    print(f"thr={THR} eligible picks: {len(sel):,}")
    print()

    rows = []
    for place_code, name in PLACE_NAMES.items():
        sub_place = sel[sel["place_code"] == place_code]
        for month, grp in sub_place.groupby("test_month"):
            r = evaluate_place_month(grp, payouts)
            if r is None:
                continue
            rows.append({
                "place": name,
                "month": month,
                "n": r["n_races"],
                "rt3_roi": r["rt3"]["roi"] * 100,
                "rt3_hit": r["rt3"]["hit_rate"] * 100,
                "rf3_roi": r["rf3"]["roi"] * 100,
                "fns_roi": r["fns"]["roi"] * 100,
            })
    df = pd.DataFrame(rows)

    # 場別 rt3 統計サマリ
    print(f"=== 場別 月次 rt3 統計 (thr={THR}) ===")
    _print_place_summary(df, "rt3_roi")
    print()

    # 場別 rf3 統計サマリ
    print(f"=== 場別 月次 rf3 統計 (thr={THR}) ===")
    _print_place_summary(df, "rf3_roi")
    print()

    # 飯塚の月次明細
    print("=== 飯塚 月次明細 (thr=1.45) ===")
    iz = df[df["place"] == "飯塚"].sort_values("month")
    print(f"{'month':<8} {'n':>4} {'rt3_roi':>8} {'rt3_hit':>8} "
          f"{'rf3_roi':>8} {'fns_roi':>8}")
    print("-" * 55)
    for _, r in iz.iterrows():
        print(f"{r['month']:<8} {int(r['n']):>4} "
              f"{r['rt3_roi']:>7.1f}% {r['rt3_hit']:>7.1f}% "
              f"{r['rf3_roi']:>7.1f}% {r['fns_roi']:>7.1f}%")
    print()

    # 浜松の月次明細(高 ROI が外れ値依存か)
    print("=== 浜松 月次明細 (thr=1.45) ===")
    hm = df[df["place"] == "浜松"].sort_values("month")
    print(f"{'month':<8} {'n':>4} {'rt3_roi':>8} {'rt3_hit':>8} "
          f"{'rf3_roi':>8} {'fns_roi':>8}")
    print("-" * 55)
    for _, r in hm.iterrows():
        print(f"{r['month']:<8} {int(r['n']):>4} "
              f"{r['rt3_roi']:>7.1f}% {r['rt3_hit']:>7.1f}% "
              f"{r['rf3_roi']:>7.1f}% {r['fns_roi']:>7.1f}%")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
