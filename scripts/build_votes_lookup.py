"""場×R 番号別の typical 複勝票数を計算し data/expected_votes.csv に保存。

直近 180 日の payouts.csv から (place_code, race_no) → median refund_votes
(= winning 複勝チケット数) を集計。daily_predict / Streamlit がこれを参照
してベット推奨額を動的に提示する。

ベット額 → odds 低下率の関係:
  new_odds / old_odds ≈ old_votes / (old_votes + new_tickets)
  10% 低下許容 → max_tickets = 0.111 * old_votes → max_yen = max * 100
   5% 低下許容 → max_tickets = 0.053 * old_votes

月次 cron で再生成推奨 (場の規模感は時期で変わる)。

使い方:
  python scripts/build_votes_lookup.py
  python scripts/build_votes_lookup.py --days 90  # 直近 90 日に限定
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=180, help="直近 N 日に限定 (default 180)")
    args = p.parse_args()

    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"].copy()
    cutoff = fns["race_date"].max() - pd.Timedelta(days=args.days)
    recent = fns[fns["race_date"] >= cutoff]
    print(f"集計期間: {cutoff.date()} ~ {fns['race_date'].max().date()} ({args.days} 日)")
    print(f"  fns 行数: {len(recent):,}")

    g = recent.groupby(["place_code", "race_no"])["refund_votes"].agg(
        ["median", "mean", "count"]
    ).reset_index()
    g.columns = ["place_code", "race_no", "votes_median", "votes_mean", "n_obs"]
    # 推奨額 (10% 低下許容、100 円単位)
    g["rec_yen_10pct"] = (g["votes_median"] * 0.111 * 100).round(-2).clip(lower=0).astype(int)
    g["rec_yen_5pct"] = (g["votes_median"] * 0.053 * 100).round(-2).clip(lower=0).astype(int)

    out = DATA / "expected_votes.csv"
    g.to_csv(out, index=False)
    print(f"保存: {out}  ({len(g)} 行 = 場 × R 番号 組合せ)")
    print()
    print("場別 平均推奨額 (10%低下まで):")
    by_place = g.groupby("place_code")[["votes_median", "rec_yen_10pct", "rec_yen_5pct"]].mean().round(0).astype(int)
    print(by_place.to_string())


if __name__ == "__main__":
    main()
