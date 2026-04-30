"""ハイブリッド戦略検証: 複勝 pred-top1 + 3連複/単 EV-top3 box

ユーザー提案 (2026-04-30): "複勝は pred、3連は max-EV で買い目を提案"

3連 max-EV を「ev_avg_calib top-3 を box 1 点 (rf3) or 6 点 (rt3)」と定義し、
walk-forward eval set 25 ヶ月で複数 policy を比較。

事前オッズが保存されていないため全 56/336 通りからの max-EV 選別は不可。
箱買いに限定した近似戦略。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100
THR = 1.50
CALIB_CUTOFF = "2024-04"


def load_eval():
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    df = preds.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]],
        on=RACE_KEY + ["car_no"], how="left",
    ).dropna(subset=["place_odds_min"])

    calib = df[df["test_month"] < CALIB_CUTOFF]
    ev = df[df["test_month"] >= CALIB_CUTOFF].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    ev["pred_calib"] = iso.transform(ev["pred"].values)
    ev["ev_calib"] = ev["pred_calib"] * (ev["place_odds_min"] + ev["place_odds_max"]) / 2
    ev["pred_rank"] = ev.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)
    ev["ev_rank"] = ev.groupby(RACE_KEY)["ev_calib"].rank(method="min", ascending=False)
    ev["ym"] = ev["race_date"].dt.to_period("M").astype(str)
    return ev


def load_payouts():
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])

    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()
    fns = fns.rename(columns={"car_no_1": "car_no", "refund": "payout_fns"})

    rf3 = pay[pay["bet_type"] == "rf3"][
        RACE_KEY + ["car_no_1", "car_no_2", "car_no_3", "refund"]
    ].dropna()
    rf3["actual_set"] = rf3.apply(
        lambda r: tuple(sorted([int(r.car_no_1), int(r.car_no_2), int(r.car_no_3)])),
        axis=1,
    )
    rf3 = rf3[RACE_KEY + ["actual_set", "refund"]].rename(
        columns={"refund": "payout_rf3"}
    )

    rt3 = pay[pay["bet_type"] == "rt3"][
        RACE_KEY + ["car_no_1", "car_no_2", "car_no_3", "refund"]
    ].dropna()
    rt3["actual_seq_set"] = rt3.apply(
        lambda r: tuple(sorted([int(r.car_no_1), int(r.car_no_2), int(r.car_no_3)])),
        axis=1,
    )
    rt3 = rt3[RACE_KEY + ["actual_seq_set", "refund"]].rename(
        columns={"refund": "payout_rt3"}
    )

    return fns, rf3, rt3


def race_picks(ev: pd.DataFrame, rank_col: str) -> pd.DataFrame:
    """rank_col 上位 3 車を sorted tuple として race per row 化。"""
    sub = ev[ev[rank_col] <= 3].copy()
    sub["car_no"] = sub["car_no"].astype(int)
    grp = sub.groupby(RACE_KEY)["car_no"].apply(
        lambda x: tuple(sorted(x.tolist()))
    ).reset_index().rename(columns={"car_no": "pick"})
    grp = grp[grp["pick"].apply(len) == 3]
    # race-level top1_ev (for thresholding)
    top1_ev = ev[ev["ev_rank"] == 1][RACE_KEY + ["ev_calib"]].rename(
        columns={"ev_calib": "top1_ev"}
    )
    grp = grp.merge(top1_ev, on=RACE_KEY, how="left")
    return grp


def eval_strategy(picks: pd.DataFrame, payout_col: str, cost_per_race: int,
                   actual_col: str, payouts_df: pd.DataFrame) -> pd.DataFrame:
    df = picks.merge(payouts_df, on=RACE_KEY, how="left")
    df["hit"] = (df["pick"] == df[actual_col]).astype(int)
    df["payout"] = np.where(df["hit"] == 1, df[payout_col].fillna(0), 0).astype(int)
    df["cost"] = cost_per_race
    df["profit"] = df["payout"] - df["cost"]
    df["ym"] = df["race_date"].dt.to_period("M").astype(str)
    return df


def summary(name: str, df: pd.DataFrame):
    n = len(df)
    cost = df["cost"].sum()
    payout = df["payout"].sum()
    profit = payout - cost
    hit_rate = df["hit"].mean() if n else 0
    avg_pay_hit = df[df["hit"] == 1]["payout"].mean() if hit_rate > 0 else 0
    max_pay = df["payout"].max() if n else 0
    roi = payout / cost if cost else 0

    big_cut = 1000
    big = df[df["payout"] >= big_cut]
    rest = df[df["payout"] < big_cut]
    pf_big = int(big["payout"].sum() - big["cost"].sum())
    pf_rest = int(rest["payout"].sum() - rest["cost"].sum())

    monthly = df.groupby("ym").agg(
        n=("payout", "size"), pay=("payout", "sum"), cost=("cost", "sum")
    ).reset_index()
    monthly["pf"] = monthly["pay"] - monthly["cost"]
    monthly["roi"] = monthly["pay"] / monthly["cost"]
    months_pos = (monthly["pf"] > 0).sum()
    n_months = len(monthly)
    worst_month = monthly["pf"].min()
    worst_ym = monthly.loc[monthly["pf"].idxmin(), "ym"] if n_months else "—"
    best_month = monthly["pf"].max()

    # 連敗
    losses = (df["payout"] == 0).astype(int).values
    runs, cur = [], 0
    for v in losses:
        if v == 1:
            cur += 1
        else:
            if cur > 0:
                runs.append(cur)
            cur = 0
    if cur > 0:
        runs.append(cur)
    max_run = max(runs) if runs else 0

    return {
        "name": name, "n": n, "cost": int(cost), "payout": int(payout),
        "profit": int(profit), "roi": roi, "hit": hit_rate,
        "avg_pay_hit": int(avg_pay_hit) if avg_pay_hit else 0,
        "max_pay": int(max_pay), "pf_big_ge1k": pf_big, "pf_rest_lt1k": pf_rest,
        "months_pos": int(months_pos), "n_months": int(n_months),
        "worst_pf": int(worst_month), "worst_ym": worst_ym,
        "best_pf": int(best_month), "max_loss_run": max_run,
    }


def main():
    ev = load_eval()
    fns, rf3, rt3 = load_payouts()
    print(f"Eval: {ev['race_date'].min().date()} ~ {ev['race_date'].max().date()}, "
          f"{ev['ym'].nunique()} months")

    # picks
    pred_top3 = race_picks(ev, "pred_rank")
    ev_top3 = race_picks(ev, "ev_rank")

    # ── A0: 複勝 pred-top1 + EV>=THR (現本番) ──
    a0 = ev[(ev["pred_rank"] == 1) & (ev["ev_calib"] >= THR)].copy()
    a0 = a0.merge(fns, on=RACE_KEY + ["car_no"], how="left")
    a0["payout"] = a0["payout_fns"].fillna(0).astype(int)
    a0["cost"] = BET
    a0["hit"] = (a0["payout"] > 0).astype(int)
    a0["profit"] = a0["payout"] - a0["cost"]
    a0["ym"] = a0["race_date"].dt.to_period("M").astype(str)

    # ── B1: rf3 EV-top3 box, 無条件 ──
    b1 = eval_strategy(ev_top3, "payout_rf3", BET, "actual_set", rf3)

    # ── B2: rf3 EV-top3 box, top1_EV>=THR ──
    b2 = eval_strategy(
        ev_top3[ev_top3["top1_ev"] >= THR], "payout_rf3", BET, "actual_set", rf3
    )

    # ── B3: rf3 pred-top3 box, top1_EV>=THR (対照) ──
    b3 = eval_strategy(
        pred_top3[pred_top3["top1_ev"] >= THR], "payout_rf3", BET, "actual_set", rf3
    )

    # ── C: rt3 EV-top3 box 6 点, top1_EV>=THR ──
    c = eval_strategy(
        ev_top3[ev_top3["top1_ev"] >= THR], "payout_rt3", BET * 6, "actual_seq_set", rt3
    )

    # ── H: ハイブリッド A0 + B2 (R が両方該当時のみ A0+B2、A0 のみは A0、B2 のみは B2) ──
    a0_min = a0[RACE_KEY + ["payout", "cost", "hit"]].rename(
        columns={"payout": "payout_a", "cost": "cost_a", "hit": "hit_a"}
    )
    b2_min = b2[RACE_KEY + ["payout", "cost", "hit"]].rename(
        columns={"payout": "payout_b", "cost": "cost_b", "hit": "hit_b"}
    )
    h = a0_min.merge(b2_min, on=RACE_KEY, how="outer").fillna(0)
    h["payout"] = (h["payout_a"] + h["payout_b"]).astype(int)
    h["cost"] = (h["cost_a"] + h["cost_b"]).astype(int)
    h["hit"] = ((h["hit_a"] + h["hit_b"]) > 0).astype(int)
    h["race_date"] = pd.to_datetime(h["race_date"])
    h["ym"] = h["race_date"].dt.to_period("M").astype(str)
    h["profit"] = h["payout"] - h["cost"]

    rows = [
        summary("A0 fns pred-top1 EV>=1.5 (baseline)", a0),
        summary("B1 rf3 EV-top3 box (all races)", b1),
        summary("B2 rf3 EV-top3 box (top1_EV>=1.5)", b2),
        summary("B3 rf3 pred-top3 box (top1_EV>=1.5) [既不採用]", b3),
        summary("C  rt3 EV-top3 box 6pts (top1_EV>=1.5)", c),
        summary("H  ハイブリッド A0+B2 (ユーザー提案)", h),
    ]

    # ── 出力 ──
    today = datetime.now().strftime("%Y-%m-%d")
    REPORTS.mkdir(exist_ok=True)
    md = []
    md.append(f"# ハイブリッド戦略検証 ({today}): 複勝=pred / 3連=max-EV box")
    md.append("")
    md.append(f"Eval set: {ev['race_date'].min().date()} ~ {ev['race_date'].max().date()}, "
              f"{ev['ym'].nunique()} months. thr={THR}, BET=¥{BET}")
    md.append("")
    md.append("## 1. 全体サマリ")
    md.append("")
    md.append("| Strategy | n | hit% | cost | payout | profit | ROI | 月勝率 | worst月 | best月 | 連敗max | big(≥¥1k)寄与 | rest(<¥1k) |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        md.append(
            f"| {r['name']} | {r['n']:,} | {r['hit']*100:.1f}% | "
            f"¥{r['cost']:,} | ¥{r['payout']:,} | "
            f"**¥{r['profit']:+,}** | {r['roi']*100:.1f}% | "
            f"{r['months_pos']}/{r['n_months']} | "
            f"¥{r['worst_pf']:+,} ({r['worst_ym']}) | "
            f"¥{r['best_pf']:+,} | {r['max_loss_run']} | "
            f"¥{r['pf_big_ge1k']:+,} | ¥{r['pf_rest_lt1k']:+,} |"
        )
    md.append("")

    md.append("## 2. ハイブリッド H = A0 + B2 の月次")
    md.append("")
    monthly_h = h.groupby("ym").agg(
        n=("payout", "size"), pay=("payout", "sum"), cost=("cost", "sum"),
        hit=("hit", "sum"),
    ).reset_index()
    monthly_h["pf"] = monthly_h["pay"] - monthly_h["cost"]
    monthly_h["roi"] = monthly_h["pay"] / monthly_h["cost"] * 100
    md.append("| 月 | bets | hits | cost | payout | profit | ROI |")
    md.append("|---|---:|---:|---:|---:|---:|---:|")
    for _, m in monthly_h.iterrows():
        md.append(
            f"| {m['ym']} | {int(m['n'])} | {int(m['hit'])} | "
            f"¥{int(m['cost']):,} | ¥{int(m['pay']):,} | "
            f"**¥{int(m['pf']):+,}** | {m['roi']:.1f}% |"
        )

    out = REPORTS / f"ev_hybrid_compare_{today}.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}\n")

    print("=== Summary ===")
    for r in rows:
        print(f"  {r['name']:55s}  n={r['n']:5d}  hit={r['hit']*100:5.1f}%  "
              f"profit={r['profit']:+7d}  ROI={r['roi']*100:5.1f}%  "
              f"月勝={r['months_pos']}/{r['n_months']}  worst={r['worst_pf']:+6d}  "
              f"big¥1k+={r['pf_big_ge1k']:+7d}  rest={r['pf_rest_lt1k']:+7d}  "
              f"連敗={r['max_loss_run']}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
