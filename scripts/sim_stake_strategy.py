"""複勝 ステーク戦略 バックテスト・シミュレータ (2026-06-16)。

目的: 複勝(fns) top-1 戦略に対し、賭け金の振り方 (フラット / マーチンゲール 等)
を過去データで比較する。**シミュレーション専用** — 本番投票はしない。

データ:
  walk-forward OOF 予測 (walkforward_predictions_morning_top3.parquet、~4年/51ヶ月)
  から各レースの予測 top-1 車を選び、odds_summary で複勝オッズ、payouts で実払戻を
  結合して「実際に賭けていたら」の払戻倍率列を時系列で作る。

戦略:
  flat        : 毎回 base 固定
  martingale  : 負けたら ×factor、勝ったら base にリセット (チケット上限で頭打ち)
  anti        : 勝ったら ×factor、負けたら base (逆マーチン)

評価:
  単一走査 (実際の時系列順) + モンテカルロ (順序シャッフル N 回) で
  損益分布・最大ドローダウン・破産確率・チケット上限到達を出す。
  マーチンは順序依存が強いので MC の破産確率が本質。

使い方:
  python scripts/sim_stake_strategy.py                       # 既定(flat vs martingale)
  python scripts/sim_stake_strategy.py --base 100 --ev-thr 1.5 --mc 2000
  python scripts/sim_stake_strategy.py --bankroll 22000 --cap-ticket 1000 --factor 2
"""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def build_pick_stream(ev_thr: float) -> pd.DataFrame:
    """予測 top-1 複勝ピックの時系列を作る。
    返り値: race ごと 1 行、列 [race_date, place_code, race_no, car_no,
            pred_calib, odds_mid, ev, mult] (mult=実払戻倍率, 0=外れ)。
    """
    wf = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    wf["race_date"] = pd.to_datetime(wf["race_date"])

    # isotonic 校正器 (live と同じ) で pred -> pred_calib
    try:
        with open(DATA / "production_calib.pkl", "rb") as f:
            iso = pickle.load(f)
        wf["pred_calib"] = iso.transform(wf["pred"].values)
    except Exception:
        wf["pred_calib"] = wf["pred"]

    # 各レースの予測 top-1 車
    idx = wf.groupby(["race_date", "place_code", "race_no"])["pred_calib"].idxmax()
    picks = wf.loc[idx, ["race_date", "place_code", "race_no", "car_no",
                         "pred_calib", "target_top3"]].copy()

    # 複勝オッズ結合
    od = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    od["race_date"] = pd.to_datetime(od["race_date"])
    od = od[["race_date", "place_code", "race_no", "car_no",
             "place_odds_min", "place_odds_max"]]
    picks = picks.merge(od, on=["race_date", "place_code", "race_no", "car_no"],
                        how="left")
    picks["odds_mid"] = (picks["place_odds_min"] + picks["place_odds_max"]) / 2
    picks["ev"] = picks["pred_calib"] * picks["odds_mid"]

    # 実払戻 (payouts の fns、当該車)
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][
        ["race_date", "place_code", "race_no", "car_no_1", "refund"]
    ].rename(columns={"car_no_1": "car_no"})
    picks = picks.merge(fns, on=["race_date", "place_code", "race_no", "car_no"],
                        how="left")
    picks["mult"] = (picks["refund"].fillna(0.0) / 100.0)  # 倍率, 0=外れ

    picks = picks.dropna(subset=["odds_mid", "ev"])
    if ev_thr > 0:
        # ⚠️ 注意: ここで使う odds_summary の複勝オッズは実払戻より系統的に過大
        #   (的中車で odds中点 ~3.9 vs 実払戻 ~2.2、midpoint過大 + late money前)。
        #   EV フィルタはこの盛れたオッズで選別するため backtest ROI が
        #   楽観に出る (実弾 ~100% に対し EV>=1.5 backtest は ~135%)。
        #   ステーク戦略の比較は --ev-thr 0 (素の予測top1) の honest 基準で見ること。
        print("[warn] EV フィルタは odds_summary の過大オッズ依存で楽観バイアス。"
              "honest 評価は --ev-thr 0 を使うこと。", file=sys.stderr)
        picks = picks[picks["ev"] >= ev_thr]
    picks = picks.sort_values(["race_date", "place_code", "race_no"]).reset_index(drop=True)
    return picks


def simulate(mult: np.ndarray, strategy: str, base: int, factor: float,
             cap_ticket: int, bankroll: int) -> dict:
    """1 走査。戻り値に損益・最大賭け金・DD・破産・上限到達回数。"""
    stake = base
    bal = 0.0
    min_bal = 0.0
    maxstake = 0
    cap_hits = 0
    ruin = False
    for x in mult:
        bet = min(stake, cap_ticket)
        if stake > cap_ticket:
            cap_hits += 1
        maxstake = max(maxstake, bet)
        bal -= bet
        if bal < min_bal:
            min_bal = bal
        if -bal > bankroll:
            ruin = True
        won = x > 0
        if won:
            bal += bet * x
        # 次の賭け金
        if strategy == "flat":
            stake = base
        elif strategy == "martingale":
            stake = base if won else int(stake * factor)
        elif strategy == "anti":
            stake = int(stake * factor) if won else base
        else:
            raise ValueError(strategy)
    roi = (bal + sum(min(s, cap_ticket) for s in [base])) and None  # placeholder
    return {"pnl": bal, "max_stake": maxstake, "cap_hits": cap_hits,
            "max_dd": min_bal, "ruin": ruin}


def longest_loss_streak(mult: np.ndarray) -> int:
    s = mx = 0
    for x in mult:
        if x == 0:
            s += 1; mx = max(mx, s)
        else:
            s = 0
    return mx


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", type=int, default=100)
    ap.add_argument("--factor", type=float, default=2.0, help="倍率(マーチン)")
    ap.add_argument("--cap-ticket", type=int, default=1000, help="1券種上限¥")
    ap.add_argument("--bankroll", type=int, default=22000, help="破産判定残高¥")
    ap.add_argument("--ev-thr", type=float, default=1.5, help="EV閾値(0で無効)")
    ap.add_argument("--mc", type=int, default=2000, help="モンテカルロ順序シャッフル回数")
    ap.add_argument("--strategies", default="flat,martingale",
                    help="カンマ区切り: flat,martingale,anti")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    picks = build_pick_stream(args.ev_thr)
    mult = picks["mult"].values
    n = len(mult)
    hit = (mult > 0).mean()
    win = mult[mult > 0]

    print(f"=== 複勝 top-1 ピック列 ({picks['race_date'].min().date()} 〜 "
          f"{picks['race_date'].max().date()}, EV>={args.ev_thr}) ===")
    print(f"レース数 {n:,} / 的中率 {hit*100:.1f}% / 的中時倍率 平均{win.mean():.2f} "
          f"中央{np.median(win):.2f} / 2.0倍未満 {(win<2.0).mean()*100:.0f}%")
    print(f"最長連敗(実順序) {longest_loss_streak(mult)}連敗 / "
          f"単純ROI {mult.sum()/n*100:.1f}%")
    print()

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    rng = np.random.default_rng(args.seed)

    for strat in strategies:
        base_run = simulate(mult, strat, args.base, args.factor,
                            args.cap_ticket, args.bankroll)
        # モンテカルロ: 順序シャッフル
        pnls = np.empty(args.mc)
        ruins = 0
        max_stakes = np.empty(args.mc)
        for i in range(args.mc):
            perm = rng.permutation(mult)
            r = simulate(perm, strat, args.base, args.factor,
                         args.cap_ticket, args.bankroll)
            pnls[i] = r["pnl"]
            ruins += int(r["ruin"])
            max_stakes[i] = r["max_stake"]
        print(f"--- {strat} (base¥{args.base}, factor×{args.factor}) ---")
        print(f"  実順序: 損益 {base_run['pnl']:+,.0f} / 最大賭け金 ¥{base_run['max_stake']:,} "
              f"/ 上限到達 {base_run['cap_hits']}回 / 最大DD ¥{base_run['max_dd']:,.0f}"
              + (" / ★破産" if base_run['ruin'] else ""))
        print(f"  MC({args.mc}回): 損益 中央{np.median(pnls):+,.0f} "
              f"[5%{np.percentile(pnls,5):+,.0f} / 95%{np.percentile(pnls,95):+,.0f}] "
              f"/ ★破産確率 {ruins/args.mc*100:.1f}% "
              f"/ 最大賭け金 中央¥{np.median(max_stakes):,.0f}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
