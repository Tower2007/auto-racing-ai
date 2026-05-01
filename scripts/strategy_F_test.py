"""戦略 F 検証: pred-top1 = 市場本命 (pop1) + EV 閾値緩和

仮説: pred-top1 が市場本命 (= win_odds 最低車) と一致する R に限定すれば、
人気1位車には数百~数千票集まるので、自分のベットがプールを歪めにくい。

代わりに edge は減るので EV 閾値を緩めて picks 数を確保。

複数 (pop1 一致 ON/OFF) × (EV 閾値 1.10-2.00) で realized ROI を比較。
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
TAKERATE = 0.17


def load():
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
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max", "win_odds"]],
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
    df["win_odds_rank"] = df.groupby(RACE_KEY)["win_odds"].rank(method="min", ascending=True)
    df = df.dropna(subset=["place_odds_min"])
    return df


def calibrate(df):
    calib = df[df["test_month"] < "2024-04"]
    ev = df[df["test_month"] >= "2024-04"].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    ev["pred_calib"] = iso.transform(ev["pred"].values)
    ev["ev_calib"] = ev["pred_calib"] * (
        ev["place_odds_min"] + ev["place_odds_max"]
    ) / 2
    return ev


def realistic_payout(picks: pd.DataFrame, bet: int) -> tuple[int, int, float]:
    """正確な公式: new_per_100 = (R*T + b*(1-r)/3) / (T + b/100)"""
    my_t = bet / 100
    hits = picks[picks["hit"] == 1]
    valid = hits[hits["refund_votes"] > 0]
    invalid = hits[hits["refund_votes"] == 0]
    R = valid["payout"]
    T = valid["refund_votes"]
    new_per_100 = (R * T + bet * (1 - TAKERATE) / 3) / (T + my_t)
    realized = (bet / 100) * new_per_100
    invalid_pay = (invalid["payout"] / 100 * bet).sum() if len(invalid) else 0
    total_pay = int(realized.sum() + invalid_pay)
    invest = len(picks) * bet
    return total_pay, invest, total_pay - invest


def main():
    df = load()
    ev = calibrate(df)
    print(f"Eval set: {ev['race_date'].min().date()} ~ {ev['race_date'].max().date()}, "
          f"{ev.groupby(RACE_KEY).ngroups:,} races")
    print(f"前提: 票数中央値の指標として refund_votes (post-race) を使用")
    print()

    # 戦略 A: pred-top1 のみ (pop1 一致縛りなし) — 現本番
    # 戦略 F: pred-top1 ∩ win_odds_rank=1 (市場本命と一致)
    print(f"{'戦略':30s}  {'閾値':>6s}  {'n':>5s}  {'hit%':>6s}  "
          f"{'avgVotes':>9s}  {'¥100 ROI':>10s}  {'¥500 ROI':>10s}  {'¥1000 ROI':>11s}  {'¥2000 ROI':>11s}")
    for thr in [1.10, 1.20, 1.30, 1.50, 1.70, 2.00]:
        for label, mask_fn in [
            ("A pred-top1 (現本番)", lambda d: d["pred_rank"] == 1),
            ("F pred-top1 ∩ pop1一致", lambda d: (d["pred_rank"] == 1) & (d["win_odds_rank"] == 1)),
        ]:
            picks = ev[mask_fn(ev) & (ev["ev_calib"] >= thr)].copy()
            if picks.empty:
                continue
            n = len(picks)
            hit = picks["hit"].mean() * 100
            hits = picks[picks["hit"] == 1]
            avg_votes = hits["refund_votes"].mean() if len(hits) else 0
            line = f"{label:30s}  {thr:>5.2f}  {n:5d}  {hit:5.1f}%  {avg_votes:8.0f}"
            for bet in [100, 500, 1000, 2000]:
                pay, inv, _ = realistic_payout(picks, bet)
                roi = pay / inv * 100 if inv else 0
                line += f"  {roi:9.1f}%"
            print(line)
        print()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
