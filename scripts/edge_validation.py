"""真の edge があるかの厳密検証

以下を比較:
  1. pred-top1 + EV>=1.50 (現本番戦略) — 主張: ROI 132%
  2. ランダム picks (1 R から 1 車を一様無作為) — 期待: ROI ≈ 控除率 (70-75%)
  3. pred-top1 のみ (EV 閾値なし) — 期待: 中間
  4. EV>=1.50 のみ (pred 順位無関係) — 期待: max-EV 的、ROI 不安定
  5. 最低 EV 車 (= 安全本命) — 期待: 控除率に近い

加えて 25 ヶ月を 5 期間に分けて pred-top1 EV>=1.50 のサブサンプル ROI を
出し、安定性 (期間ごとのバラつき) を測定。

これで「結果論ではないか」「edge は本物か」が定量的に判定できる。
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
BET = 100
THR = 1.50

np.random.seed(42)


def load():
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()

    df = preds.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]],
        on=RACE_KEY + ["car_no"], how="left",
    )
    df = df.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "payout"}),
        on=RACE_KEY + ["car_no"], how="left",
    )
    df["payout"] = df["payout"].fillna(0).astype(int)
    df["hit"] = (df["payout"] > 0).astype(int)
    df["pred_rank"] = df.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)
    df["ym"] = df["race_date"].dt.to_period("M").astype(str)
    return df.dropna(subset=["place_odds_min"])


def calibrate(df, cutoff="2024-04"):
    calib = df[df["test_month"] < cutoff]
    ev = df[df["test_month"] >= cutoff].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    ev["pred_calib"] = iso.transform(ev["pred"].values)
    ev["ev_calib"] = ev["pred_calib"] * (ev["place_odds_min"] + ev["place_odds_max"]) / 2
    ev["ev_rank"] = ev.groupby(RACE_KEY)["ev_calib"].rank(method="min", ascending=False)
    return ev


def stats(label, picks):
    if picks.empty:
        print(f"  {label:42s}: no picks")
        return None
    n = len(picks); cost = n * BET
    pay = int(picks["payout"].sum()); pf = pay - cost
    hit = picks["hit"].mean()
    roi = pay / cost
    return dict(label=label, n=n, hit=hit, pf=pf, roi=roi)


def fmt(s):
    if s is None:
        return ""
    return (f"  {s['label']:42s}: n={s['n']:5d} hit={s['hit']*100:5.1f}% "
            f"profit={s['pf']:+7d} ROI={s['roi']*100:5.1f}%")


def main():
    df = load()
    ev = calibrate(df)
    print(f"Eval set: {ev['race_date'].min().date()} ~ {ev['race_date'].max().date()}, "
          f"{ev['ym'].nunique()} months, {ev.groupby(RACE_KEY).ngroups:,} races, {len(ev):,} car-rows")
    print()

    # ── 戦略比較 ──
    print("=== 戦略 5 種比較 (eval 25mo) ===")
    rows = []
    rows.append(stats("(本命) pred-top1 EV>=1.50",
                      ev[(ev["pred_rank"] == 1) & (ev["ev_calib"] >= THR)]))
    rows.append(stats("(参考) pred-top1 全件 (閾値なし)",
                      ev[ev["pred_rank"] == 1]))
    rows.append(stats("(参考) EV>=1.50 全車 (max-EV)",
                      ev[ev["ev_calib"] >= THR]))
    rows.append(stats("(対照) ランダム 1 R 1 車 (シード=42)",
                      ev.groupby(RACE_KEY).apply(lambda g: g.sample(1, random_state=42)).reset_index(drop=True)))
    rows.append(stats("(対照) pred 最下位 1 車 (反予想)",
                      ev[ev["pred_rank"] == ev.groupby(RACE_KEY)["pred_rank"].transform("max")]))
    rows.append(stats("(対照) pred-top1 EV<1.50 (本命の閾値以下)",
                      ev[(ev["pred_rank"] == 1) & (ev["ev_calib"] < THR)]))
    for r in rows:
        if r:
            print(fmt(r))
    print()
    print("解釈:")
    print(f"  - pari-mutuel 控除率 ~25-30% → 完全ランダム picks の理論 ROI ~70-75%")
    print(f"  - ランダム picks の実測 ROI が ~70-75% に近ければ控除率の検証が成立")
    print(f"  - 本命戦略の ROI が控除率を有意に超えれば真の edge が存在")
    print()

    # ── サブサンプル安定性 (25ヶ月を 5 期間に分割) ──
    print("=== サブサンプル安定性: 5 期間別 pred-top1 EV>=1.50 ROI ===")
    all_picks = ev[(ev["pred_rank"] == 1) & (ev["ev_calib"] >= THR)].copy()
    all_picks["period"] = pd.qcut(all_picks["race_date"].rank(method="first"),
                                   q=5, labels=["P1", "P2", "P3", "P4", "P5"])
    print(f"{'期間':<6} {'開始':<12} {'終了':<12} {'n':>5} {'hit%':>7} {'profit':>10} {'ROI':>7}")
    for p, sub in all_picks.groupby("period", observed=True):
        n = len(sub); hit = sub["hit"].mean(); pf = int(sub["payout"].sum() - n*BET)
        roi = sub["payout"].sum() / (n*BET)
        d_min = sub["race_date"].min().date(); d_max = sub["race_date"].max().date()
        print(f"{str(p):<6} {str(d_min):<12} {str(d_max):<12} {n:5d} {hit*100:6.1f}% "
              f"{pf:+10d} {roi*100:6.1f}%")
    print()
    print("解釈: 5 期間とも ROI > 100% なら安定 edge。1-2 期間でも < 100% なら期間依存疑い")
    print()

    # ── ランダム picks の bootstrap (100 回反復で ROI 分布) ──
    print("=== ランダム picks bootstrap (100 シード × 1R1車) ===")
    rois = []
    for seed in range(100):
        rng = np.random.RandomState(seed)
        sample = ev.groupby(RACE_KEY).apply(
            lambda g: g.iloc[rng.randint(0, len(g))]
        ).reset_index(drop=True)
        rois.append(sample["payout"].sum() / (len(sample)*BET))
    rois = np.array(rois)
    print(f"  100 シードでの ROI: mean={rois.mean()*100:.1f}%, "
          f"std={rois.std()*100:.1f}%, "
          f"min={rois.min()*100:.1f}%, max={rois.max()*100:.1f}%")
    print(f"  本命戦略 ROI 132.5% は random distribution の何σ?")
    z = (1.325 - rois.mean()) / rois.std()
    print(f"  → z-score = {z:+.2f} (|z|>2 なら有意、>3 なら強く有意)")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
