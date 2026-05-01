"""発火時オッズスナップと確定後結果の比較解析

`data/odds_snapshots.csv` (発火時 = 発走 LEAD_MIN 分前のスナップ) と
`data/payouts.csv` (確定後の複勝払戻) を join して以下を測定:

  1. 発火時 EV>=thr 信号の hit rate / 実 ROI
  2. eval set 予測値 (132% / 65%) との乖離度
  3. 発火時 EV vs 確定後 EV の drift (odds_summary.csv ある場合)
  4. 場別 / 月別 内訳

LEAD_MIN を変えるたびに captured_at の意味が変わるので、本スクリプトは
「captured_at 時点の EV シグナル → 実結果」を直接見るだけ。

蓄積が浅くても動く設計。データ 0 件なら "no data" を表示して終了。

使い方:
  python scripts/odds_snapshot_eval.py              # 全期間
  python scripts/odds_snapshot_eval.py --thr 1.50   # 閾値変更
  python scripts/odds_snapshot_eval.py --since 2026-05-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100


def load_data(thr: float, since: str | None):
    snap_path = DATA / "odds_snapshots.csv"
    if not snap_path.exists() or snap_path.stat().st_size == 0:
        print("[error] data/odds_snapshots.csv が存在しないか空です。")
        print("        daily_predict.py の発火が 1 回も完了していない可能性。")
        sys.exit(1)
    snap = pd.read_csv(snap_path, low_memory=False)
    snap["race_date"] = pd.to_datetime(snap["race_date"])
    snap["captured_at"] = pd.to_datetime(snap["captured_at"])
    if since:
        snap = snap[snap["race_date"] >= pd.to_datetime(since)]

    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()
    fns = fns.rename(columns={"car_no_1": "car_no", "refund": "payout"})

    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    odds = odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]].rename(
        columns={"place_odds_min": "close_min", "place_odds_max": "close_max"}
    )

    return snap, fns, odds


def summarize(label: str, df: pd.DataFrame) -> None:
    if df.empty:
        print(f"  {label:30s}: no picks")
        return
    n = len(df)
    cost = n * BET
    pay = int(df["payout"].sum())
    profit = pay - cost
    hit = df["hit"].mean()
    avg_pay_hit = int(df[df["hit"] == 1]["payout"].mean()) if hit > 0 else 0
    roi = pay / cost if cost else 0
    print(f"  {label:30s}: n={n:4d} hit={hit*100:5.1f}% pay={pay:6d} profit={profit:+6d} ROI={roi*100:5.1f}% avg_pay_hit={avg_pay_hit:5d}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--thr", type=float, default=1.50)
    p.add_argument("--since", type=str, default=None, help="YYYY-MM-DD 以降に絞る")
    args = p.parse_args()

    snap, fns, odds = load_data(args.thr, args.since)
    n_races = snap.groupby(RACE_KEY).ngroups
    print(f"=== odds_snapshots.csv 読込 ===")
    print(f"  対象期間: {snap['race_date'].min().date()} ~ {snap['race_date'].max().date()}")
    print(f"  発火 R 数: {n_races}, 行数: {len(snap)} (全 8 車含む)")
    print()

    # 1. fns 確定済の R に絞る
    snap_with_pay = snap.merge(fns, on=RACE_KEY + ["car_no"], how="left")
    has_pay = snap_with_pay.dropna(subset=["payout"])
    n_pay_races = has_pay.groupby(RACE_KEY).ngroups
    snap_with_pay["payout"] = snap_with_pay["payout"].fillna(0).astype(int)
    snap_with_pay["hit"] = (snap_with_pay["payout"] > 0).astype(int)
    print(f"  fns 確定済 R 数: {n_pay_races} / 全 {n_races} R "
          f"({n_pay_races/n_races*100:.0f}%, 残りは未走 or 払戻未取り込み)")
    print()

    if n_pay_races == 0:
        print("[info] 確定済 R が 0 — 解析できません。daily_ingest が走った後に再実行。")
        return

    # 2. 戦略別 picks 抽出
    eval_df = snap_with_pay.dropna(subset=["payout"]).copy()
    eval_df["payout"] = eval_df["payout"].astype(int)

    print(f"=== 発火時 EV>={args.thr} 信号の実成績 (n_races={n_pay_races}) ===")
    # pred-top1 + EV>=thr (現本番戦略)
    a = eval_df[(eval_df["pred_rank"] == 1) & (eval_df["ev_avg_calib"] >= args.thr)]
    summarize("A pred-top1 EV>=thr", a)
    # pred-top1 全件 (EV 閾値なし、参考)
    b = eval_df[eval_df["pred_rank"] == 1]
    summarize("B pred-top1 全件 (参考)", b)
    # EV>=thr 全車 (閾値超は何件あるか)
    c = eval_df[eval_df["ev_avg_calib"] >= args.thr]
    summarize("C EV>=thr 全車", c)
    print()

    # 3. eval set 予測値との比較
    print(f"=== eval set (25mo, closing odds) 予測値 ===")
    print(f"  pred-top1 EV>=1.50: ROI 132.5%, hit 65.3%, profit/月 ¥2,388 (¥59,690/25mo)")
    print(f"  ↑ closing odds 基準。実発火 -5min との drift を↓で測定")
    print()

    # 4. EV drift 分析 (snap EV vs closing EV、odds_summary がある R のみ)
    print(f"=== EV drift 分析 (snap EV vs closing EV、pred-top1 のみ) ===")
    drift = eval_df[eval_df["pred_rank"] == 1].merge(odds, on=RACE_KEY + ["car_no"], how="inner")
    if not drift.empty:
        drift["close_ev"] = drift["pred_calib"] * (drift["close_min"] + drift["close_max"]) / 2
        drift["ev_diff"] = drift["close_ev"] - drift["ev_avg_calib"]
        d = drift.dropna(subset=["close_ev"])
        if not d.empty:
            print(f"  対象 R: {len(d)}")
            print(f"  snap EV  平均 {d['ev_avg_calib'].mean():.3f} (±{d['ev_avg_calib'].std():.3f})")
            print(f"  close EV 平均 {d['close_ev'].mean():.3f} (±{d['close_ev'].std():.3f})")
            print(f"  drift (close - snap) 平均 {d['ev_diff'].mean():+.3f}, 中央値 {d['ev_diff'].median():+.3f}")
            print(f"  drift 分布: ", end="")
            print(f"q05={d['ev_diff'].quantile(0.05):+.2f} "
                  f"q25={d['ev_diff'].quantile(0.25):+.2f} "
                  f"q75={d['ev_diff'].quantile(0.75):+.2f} "
                  f"q95={d['ev_diff'].quantile(0.95):+.2f}")
            # 信号 persistence: snap で EV>=thr だったうち何 % が close でも EV>=thr か
            snap_high = d[d["ev_avg_calib"] >= args.thr]
            if not snap_high.empty:
                still_high = (snap_high["close_ev"] >= args.thr).sum()
                print(f"  snap EV>={args.thr} 信号の close でも EV>={args.thr} 残存率: "
                      f"{still_high}/{len(snap_high)} = {still_high/len(snap_high)*100:.1f}%")
        else:
            print("  close EV 計算可能な R なし (close odds の NaN)")
    else:
        print("  odds_summary.csv に該当する R 未取り込み (daily_ingest 待ち)")
    print()

    # 5. 場別内訳
    print(f"=== 場別 (pred-top1 EV>=thr) ===")
    if not a.empty:
        VENUE = {2: "kawaguchi", 3: "isesaki", 4: "hamamatsu", 5: "iizuka", 6: "sanyou"}
        a["venue"] = a["place_code"].map(VENUE)
        for venue, sub in a.groupby("venue"):
            summarize(f"  {venue}", sub)
    else:
        print("  (該当 R なし)")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
