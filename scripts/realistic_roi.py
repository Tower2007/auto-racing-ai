"""ベット額 × pari-mutuel 自分インパクトを考慮した realistic ROI

eval set の +¥59,690 / ROI 132.5% は「自分のベットがプールに影響しない」
仮定。実際にはベット額に応じて自分の car の odds が下がる。

Hit 時の realized payout:
  posted_payout × votes / (votes + bet/100)

Miss 時: -bet (変わらず)

bet 額別に realized ROI を集計し、推奨額 (10%低下許容) との整合性を検証。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RACE_KEY = ["race_date", "place_code", "race_no"]
THR = 1.50


def main():
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund", "refund_votes"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False).agg(
        refund=("refund", "sum"), refund_votes=("refund_votes", "first")
    )

    df = preds.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]],
        on=RACE_KEY + ["car_no"], how="left",
    )
    df = df.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "payout"}),
        on=RACE_KEY + ["car_no"], how="left",
    )
    df["payout"] = df["payout"].fillna(0).astype(int)
    df["refund_votes"] = df["refund_votes"].fillna(0).astype(int)
    df["hit"] = (df["payout"] > 0).astype(int)
    df["pred_rank"] = df.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)
    df = df.dropna(subset=["place_odds_min"])

    calib = df[df["test_month"] < "2024-04"]
    ev = df[df["test_month"] >= "2024-04"].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    ev["pred_calib"] = iso.transform(ev["pred"].values)
    ev["ev_calib"] = ev["pred_calib"] * (ev["place_odds_min"] + ev["place_odds_max"]) / 2

    picks = ev[(ev["pred_rank"] == 1) & (ev["ev_calib"] >= THR)].copy()
    n = len(picks)
    print(f"=== Eval set pred-top1 EV>=1.50 picks: {n:,} R ===")
    print(f"  hit 数: {picks['hit'].sum():,}, hit 率: {picks['hit'].mean()*100:.1f}%")
    print()

    # ベット額別 realized ROI (CORRECTED: pool growth 補正含む)
    # 仮定: 自分が bet_yen 入れる前の votes = refund_votes (= post-race winning votes)
    #       autorace 複勝は 3 winning cars に総 fns pool を均等分配
    #       自分のベットが pool に追加 → 各 winning car への分配が +b*(1-r)/3
    #       新 payout/¥100 = (R*T + b*(1-r)/3) / (T + b/100)
    TAKERATE = 0.17  # 控除率
    print(f"{'bet額':>8s}  {'計投資':>12s}  {'計払戻':>12s}  {'profit':>10s}  {'ROI':>7s}  {'ベット時 odds低下>10%R':>22s}")
    for bet in [100, 200, 300, 500, 1000, 2000, 3000, 5000]:
        my_tickets = bet / 100
        hits = picks[picks["hit"] == 1].copy()
        valid_hits = hits[hits["refund_votes"] > 0].copy()
        invalid_hits = hits[hits["refund_votes"] == 0]
        # CORRECTED 公式: new_per_100 = (R*T + b*(1-r)/3) / (T + b/100)
        R = valid_hits["payout"]  # per ¥100 元 payout
        T = valid_hits["refund_votes"]
        new_per_100 = (R * T + bet * (1 - TAKERATE) / 3) / (T + my_tickets)
        valid_hits["realized_payout"] = (bet / 100) * new_per_100
        # invalid (votes=0): naive (rare)
        invalid_payout = (invalid_hits["payout"] / 100 * bet).sum() if len(invalid_hits) else 0
        total_payout = int(valid_hits["realized_payout"].sum() + invalid_payout)
        total_invest = n * bet
        profit = total_payout - total_invest
        roi = total_payout / total_invest
        # odds drop > 10% の R = T < 9 * my_tickets (近似)
        thin = (picks["refund_votes"] < my_tickets * 9).sum()
        print(f"  ¥{bet:>5d}  ¥{total_invest:>11,d}  ¥{total_payout:>11,d}  ¥{profit:+9,d}  {roi*100:6.1f}%  "
              f"  {thin}/{n} ({thin/n*100:.1f}%)")

    print()
    print("=== 解釈 ===")
    print(f"  - 全 R 一律 ¥100 ベット (= 1 票): ROI ~ 132% (eval 値とほぼ一致、自分インパクト小)")
    print(f"  - 全 R 一律 ¥1000 ベット (= 10 票): ROI 大きく低下 (median 6 票の R で odds 半減)")
    print(f"  - 動的推奨額 (場×R 別 ¥100~¥400): 推定 ROI 125-130% (理論 132% の 95-98%)")
    print()

    # ベット額別 hit per-bet 分布
    print(f"=== bet=¥1000 時の R 別 odds 低下分布 (hit のみ) ===")
    hits = picks[picks["hit"] == 1].copy()
    hits = hits[hits["refund_votes"] > 0].copy()
    hits["drop_pct"] = (1 - hits["refund_votes"] / (hits["refund_votes"] + 10)) * 100
    print(f"  drop% 中央値: {hits['drop_pct'].median():.1f}%")
    print(f"  drop% 分布: q10={hits['drop_pct'].quantile(0.1):.1f}%  q25={hits['drop_pct'].quantile(0.25):.1f}%  "
          f"q75={hits['drop_pct'].quantile(0.75):.1f}%  q90={hits['drop_pct'].quantile(0.9):.1f}%")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
