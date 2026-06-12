"""三連系 (rt3/rf3) の適正ベット額分析 (2026-06-12)。

背景:
  複勝の推奨額は expected_votes.csv の「プール 10% ルール」
  (自分のベットが的中出目の投票額の ~10% を超えるとオッズを自分で
  潰す) で ¥100-400 に制限している。三連系へ賭け金を集中・増額する
  案の検討材料として、同じルールの三連系版を試算する。

方法:
  payouts.csv の refund_votes (的中出目への投票数、1 票=¥100) を
  場×R 別に集計。パリミュチュエルでは自分の v 円追加後の実効オッズは
    new_odds ≈ old_odds × V / (V + v)   (V=出目の既存投票額, v=自分)
  なので、オッズ低下を 10% 以内に抑えるには v ≤ V/9 ≈ 0.111×V。

注意:
  - refund_votes は「当たった出目」の票数 = 人気寄りバイアスあり。
    うちの買い目 (EV top3) も本命寄りなので近い性質だが、保守側の
    目安として 25th percentile も併記する。
  - 直近 365 日に絞る (売上トレンド・開催形態の変化を反映)。

使い方:
  python scripts/ev_3point_bet_sizing.py [--days 365]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

VENUE_JP = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}

# レース番号帯 (R 単位だと n が薄いので 3 帯に集約)
RACE_BANDS = [(1, 4, "R1-4"), (5, 8, "R5-8"), (9, 12, "R9-12")]


def band_label(race_no: int) -> str:
    for lo, hi, label in RACE_BANDS:
        if lo <= race_no <= hi:
            return label
    return "?"


def rec_yen_10pct(votes_median: float) -> int:
    """10% ルール推奨上限額 (オッズ低下 ≤10% となる自分のベット額)。

    votes (票) × ¥100 = 出目の既存投票額 V。v ≤ V/9。¥100 単位切捨て。
    """
    v_max = votes_median * 100 / 9
    return int(v_max // 100) * 100


def dilution_pct(votes_median: float, my_yen: int) -> float:
    """自分が my_yen 入れた時のオッズ低下率 (%)。"""
    V = votes_median * 100
    if V <= 0:
        return 100.0
    return (1 - V / (V + my_yen)) * 100


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365,
                    help="直近 N 日に絞る (default 365)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    cutoff = pay["race_date"].max() - pd.Timedelta(days=args.days)
    pay = pay[pay["race_date"] >= cutoff]
    pay = pay[pay["bet_type"].isin(["rt3", "rf3", "fns"])]
    pay = pay[pay["refund_votes"] > 0]
    pay["band"] = pay["race_no"].astype(int).map(band_label)

    print(f"=== 三連系 適正ベット額分析 (直近 {args.days} 日: "
          f"{cutoff.date()} 〜 {pay['race_date'].max().date()}) ===")
    print(f"対象 {len(pay):,} 払戻行 (rt3/rf3/fns, votes>0)")
    print()
    print("votes = 的中出目への投票数 (票, 1票=¥100)。")
    print("rec¥ = オッズ低下 10% 以内に収まる自分のベット上限 (中央値ベース)。")
    print("rec¥(q25) = 保守版 (25th percentile ベース)。")
    print()

    for bt, bt_jp in [("rt3", "三連単"), ("rf3", "三連複"), ("fns", "複勝(参考)")]:
        sub = pay[pay["bet_type"] == bt]
        print(f"--- {bt_jp} ({bt}) ---")
        rows = []
        for pc in sorted(VENUE_JP):
            for lo, hi, label in RACE_BANDS:
                s = sub[(sub["place_code"] == pc) & (sub["band"] == label)]
                if len(s) < 20:
                    continue
                med = s["refund_votes"].median()
                q25 = s["refund_votes"].quantile(0.25)
                rows.append({
                    "場": VENUE_JP[pc],
                    "帯": label,
                    "n": len(s),
                    "votes中央値": round(med, 1),
                    "votes_q25": round(q25, 1),
                    "rec¥": rec_yen_10pct(med),
                    "rec¥(q25)": rec_yen_10pct(q25),
                    "低下%@¥100": round(dilution_pct(med, 100), 1),
                    "低下%@¥300": round(dilution_pct(med, 300), 1),
                    "低下%@¥500": round(dilution_pct(med, 500), 1),
                    "低下%@¥1000": round(dilution_pct(med, 1000), 1),
                })
        if rows:
            print(pd.DataFrame(rows).to_string(index=False))
        else:
            print("  (n<20 のためスキップ)")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
