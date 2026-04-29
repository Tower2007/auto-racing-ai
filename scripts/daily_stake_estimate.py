"""baseline_fns_only thr=1.50 における 1 日あたり pick 数 / stake の分布

過去 25mo の eval データから「1 日に何件の予想 pick が出たか」を集計。
ベット額をスケールした時の必要軍資金感覚を出す。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ev_3point_buy import load_eval_set  # noqa: E402

THR = 1.50


def main():
    eval_df = load_eval_set()
    # baseline = top-1 のみ thr で選別
    top1 = eval_df[eval_df["pred_rank"] == 1].copy()
    picks = top1[top1["ev_avg_calib"] >= THR].copy()
    print(f"全期間 picks: {len(picks):,}  (期間 {picks['race_date'].min().date()} 〜 {picks['race_date'].max().date()})")
    print()

    # 日別 pick 数
    daily = picks.groupby("race_date").size().rename("n_picks").reset_index()
    n_days_with_pick = len(daily)
    n_total_days = (picks["race_date"].max() - picks["race_date"].min()).days + 1
    print(f"pick が出た日数: {n_days_with_pick:,} / 全期間 {n_total_days:,} 日")
    print(f"pick 0 件の日: {n_total_days - n_days_with_pick:,} 日")
    print()

    print("=== 日別 pick 数の分布(pick > 0 の日のみ)===")
    print(f"  mean : {daily['n_picks'].mean():.2f} 件/日")
    print(f"  median: {daily['n_picks'].median():.0f} 件/日")
    print(f"  p75 : {daily['n_picks'].quantile(0.75):.0f} 件/日")
    print(f"  p90 : {daily['n_picks'].quantile(0.90):.0f} 件/日")
    print(f"  p95 : {daily['n_picks'].quantile(0.95):.0f} 件/日")
    print(f"  p99 : {daily['n_picks'].quantile(0.99):.0f} 件/日")
    print(f"  max : {daily['n_picks'].max()} 件/日 ({daily.loc[daily['n_picks'].idxmax(), 'race_date'].date()})")
    print()

    # ベット額別の必要資金
    print("=== bet 額別 1 日の軍資金目安 ===")
    print(f"{'bet/pick':>10} {'mean日':>10} {'p75日':>10} {'p90日':>10} {'p95日':>10} {'max日':>10}")
    for bet in [100, 500, 1000, 5000, 10000]:
        print(
            f"¥{bet:>8,} "
            f"¥{int(daily['n_picks'].mean() * bet):>8,} "
            f"¥{int(daily['n_picks'].quantile(0.75) * bet):>8,} "
            f"¥{int(daily['n_picks'].quantile(0.90) * bet):>8,} "
            f"¥{int(daily['n_picks'].quantile(0.95) * bet):>8,} "
            f"¥{int(daily['n_picks'].max() * bet):>8,}"
        )
    print()

    # 月別 stake (連敗月の最大投資額を見る)
    picks["month"] = picks["race_date"].dt.to_period("M")
    monthly = picks.groupby("month").size().rename("n_picks").reset_index()
    print("=== 月別 pick 数 ===")
    print(f"  mean : {monthly['n_picks'].mean():.0f} 件/月")
    print(f"  max  : {monthly['n_picks'].max()} 件/月 ({monthly.loc[monthly['n_picks'].idxmax(), 'month']})")
    print(f"  min  : {monthly['n_picks'].min()} 件/月 ({monthly.loc[monthly['n_picks'].idxmin(), 'month']})")

    print()
    print("=== bet 額別 1 ヶ月の総投資 ===")
    print(f"{'bet/pick':>10} {'mean月':>12} {'max月':>12}")
    for bet in [100, 500, 1000, 5000, 10000]:
        print(
            f"¥{bet:>8,} "
            f"¥{int(monthly['n_picks'].mean() * bet):>10,} "
            f"¥{int(monthly['n_picks'].max() * bet):>10,}"
        )


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
