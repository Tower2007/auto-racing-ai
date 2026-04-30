"""戦略比較: pred-top1 vs max-EV vs all-cars

本日 (2026-04-30) のライブで「pred 2 位の方が EV 高くて、結局その車が来た」事例を
ユーザーが観測。20 R サンプルで複勝 ROI が pred-top1=96.0% / max-EV=147.0%。
構造的勝ちパターンか確認するため、eval set 25 ヶ月で正式 walk-forward 比較。

3 戦略:
  (A) pred-top1: pred_calib 1位 + EV>=thr → 1 R 0〜1 ベット (現本番)
  (B) max-EV   : EV>=thr の車のうち EV 最大 → 1 R 0〜1 ベット (top1 縛り無し)
  (C) all-cars : EV>=thr の全車 → 1 R 0〜8 ベット

評価期間: 2024-04 〜 2026-04 (25 ヶ月、isotonic 校正後)。

出力: reports/ev_strategy_compare_YYYY-MM-DD.md

使い方:
  python scripts/ev_strategy_compare.py
  python scripts/ev_strategy_compare.py --thr 1.50  # 閾値変更
"""

from __future__ import annotations

import argparse
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
CALIB_CUTOFF = "2024-04"


def load() -> pd.DataFrame:
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
    df["payout"] = df["payout"].fillna(0)
    df["hit"] = (df["payout"] > 0).astype(int)
    df["pred_rank"] = df.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)
    df["year_month"] = df["race_date"].dt.to_period("M").astype(str)
    return df.dropna(subset=["place_odds_min"])


def calibrate(df: pd.DataFrame) -> pd.DataFrame:
    calib = df[df["test_month"] < CALIB_CUTOFF]
    eval_set = df[df["test_month"] >= CALIB_CUTOFF].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    eval_set["pred_calib"] = iso.transform(eval_set["pred"].values)
    eval_set["ev_avg_calib"] = (
        eval_set["pred_calib"] * (eval_set["place_odds_min"] + eval_set["place_odds_max"]) / 2
    )
    eval_set["ev_rank"] = eval_set.groupby(RACE_KEY)["ev_avg_calib"].rank(
        method="min", ascending=False)
    eval_set["is_top1_pred"] = (eval_set["pred_rank"] == 1).astype(int)
    eval_set["is_top1_ev"] = (eval_set["ev_rank"] == 1).astype(int)
    return eval_set


def make_picks(eval_set: pd.DataFrame, strategy: str, thr: float) -> pd.DataFrame:
    if strategy == "pred_top1":
        return eval_set[(eval_set["is_top1_pred"] == 1) & (eval_set["ev_avg_calib"] >= thr)]
    if strategy == "max_ev":
        return eval_set[(eval_set["is_top1_ev"] == 1) & (eval_set["ev_avg_calib"] >= thr)]
    if strategy == "all_cars":
        return eval_set[eval_set["ev_avg_calib"] >= thr]
    raise ValueError(strategy)


def aggregate(picks: pd.DataFrame) -> dict:
    n = len(picks)
    cost = n * BET
    payout = picks["payout"].sum()
    profit = payout - cost
    roi = payout / cost if cost else 0
    hit_rate = picks["hit"].mean() if n else 0
    monthly = picks.groupby("year_month").agg(n=("payout", "size"), p=("payout", "sum")).reset_index()
    monthly["cost"] = monthly["n"] * BET
    monthly["roi"] = monthly["p"] / monthly["cost"]
    monthly["profit"] = monthly["p"] - monthly["cost"]
    return {
        "n": n, "cost": cost, "payout": payout, "profit": profit,
        "roi": roi, "hit_rate": hit_rate,
        "month_min_roi": monthly["roi"].min() if len(monthly) else 0,
        "month_std_roi": monthly["roi"].std() if len(monthly) > 1 else 0,
        "month_ge_1": int((monthly["roi"] >= 1.0).sum()),
        "n_months": len(monthly),
        "monthly": monthly,
    }


def fmt_yen(v: float) -> str:
    if pd.isna(v): return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(int(v)):,}"


def fmt_pct(v: float) -> str:
    if pd.isna(v): return "—"
    return f"{v * 100:.1f}%"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--thr", type=float, default=1.50)
    args = p.parse_args()
    thr = args.thr

    df = load()
    eval_set = calibrate(df)
    print(f"Eval set: {len(eval_set):,} rows ({eval_set['race_date'].min().date()} 〜 {eval_set['race_date'].max().date()}, {len(eval_set['year_month'].unique())} ヶ月)")

    strategies = [
        ("pred_top1", "pred-top1 (現本番)"),
        ("max_ev",    "max-EV (top1 縛り無し)"),
        ("all_cars",  "all-cars (該当全車)"),
    ]
    results = {}
    for s, label in strategies:
        picks = make_picks(eval_set, s, thr)
        results[s] = (label, aggregate(picks))

    # 月次 wins (pred_top1 vs max_ev)
    m_top1 = results["pred_top1"][1]["monthly"][["year_month", "profit"]].rename(columns={"profit": "p_top1"})
    m_ev   = results["max_ev"][1]["monthly"][["year_month", "profit"]].rename(columns={"profit": "p_ev"})
    cmp_te = m_top1.merge(m_ev, on="year_month", how="outer").fillna(0)
    top1_wins = (cmp_te["p_top1"] > cmp_te["p_ev"]).sum()
    ev_wins   = (cmp_te["p_ev"] > cmp_te["p_top1"]).sum()
    ties      = (cmp_te["p_top1"] == cmp_te["p_ev"]).sum()

    # ── 出力 ──
    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_strategy_compare_{today}.md"
    REPORTS.mkdir(exist_ok=True)
    md = []
    md.append(f"# EV 戦略比較: pred-top1 vs max-EV vs all-cars ({today})")
    md.append("")
    md.append(f"トリガー: 2026-04-30 ライブで R7 isesaki にて pred 2 位の車 (EV 2.37) が 1 着、")
    md.append(f"本日 20 R で max-EV 戦略が ROI 147% (vs pred-top1 96%) と短期サンプルで上回ったため、")
    md.append(f"eval set 25 ヶ月で正式比較。")
    md.append("")
    md.append(f"**閾値 thr = {thr:.2f}** / 中間モデル (walkforward_predictions_morning_top3) /")
    md.append(f"isotonic 校正 (cutoff={CALIB_CUTOFF}) / 1 R 100 円 / 複勝 (fns)")
    md.append("")

    md.append("## 1. 全体サマリ")
    md.append("")
    md.append("| 戦略 | n_bets | hit% | 投資 | 払戻 | 収支 | ROI | 月次 std | 月次 min | 月勝率 |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s, _ in strategies:
        label, r = results[s]
        md.append(
            f"| {label} | {r['n']:,} | {fmt_pct(r['hit_rate'])} | "
            f"{fmt_yen(r['cost'])} | {fmt_yen(r['payout'])} | "
            f"**{fmt_yen(r['profit'])}** | {r['roi']*100:.1f}% | "
            f"{r['month_std_roi']*100:.1f}% | {fmt_pct(r['month_min_roi'])} | "
            f"{r['month_ge_1']}/{r['n_months']} |"
        )
    md.append("")

    md.append("## 2. pred-top1 vs max-EV 月次対決")
    md.append("")
    md.append(f"対決結果: **pred-top1 勝ち = {top1_wins} 月 / max-EV 勝ち = {ev_wins} 月 / 引き分け = {ties} 月**")
    md.append("")
    md.append("| 月 | pred-top1 收支 | max-EV 收支 | 差 (max-EV − pred-top1) | 勝者 |")
    md.append("|---|---:|---:|---:|---:|")
    for _, row in cmp_te.iterrows():
        diff = row["p_ev"] - row["p_top1"]
        winner = "pred-top1" if row["p_top1"] > row["p_ev"] else (
            "max-EV" if row["p_ev"] > row["p_top1"] else "tie")
        md.append(
            f"| {row['year_month']} | {fmt_yen(row['p_top1'])} | {fmt_yen(row['p_ev'])} | "
            f"{fmt_yen(diff)} | {winner} |"
        )
    md.append("")

    # 結論
    top1_total = results["pred_top1"][1]["profit"]
    ev_total   = results["max_ev"][1]["profit"]
    all_total  = results["all_cars"][1]["profit"]
    md.append("## 3. 結論")
    md.append("")
    md.append(f"### 総合判定")
    if ev_total > top1_total and ev_wins > top1_wins:
        verdict = f"**max-EV が pred-top1 より優位**: 月勝率 {ev_wins}/{ev_wins+top1_wins+ties}, 総 profit {fmt_yen(ev_total)} > {fmt_yen(top1_total)}"
        md.append(f"- ⭐ {verdict}")
        md.append(f"- 戦略変更を検討する価値あり。ただし採用前に分散・最大連敗・場別偏りを追加検証推奨。")
    elif top1_total > ev_total and top1_wins > ev_wins:
        verdict = f"**pred-top1 維持が妥当**: 月勝率 {top1_wins}/{top1_wins+ev_wins+ties}, 総 profit {fmt_yen(top1_total)} > {fmt_yen(ev_total)}"
        md.append(f"- 🟢 {verdict}")
        md.append(f"- 本日のサンプルは偶然。本番ロジックそのまま継続。")
    else:
        md.append(f"- 🟡 結論曖昧。月勝率 (pred-top1 {top1_wins} vs max-EV {ev_wins}) と")
        md.append(f"  総 profit (pred-top1 {fmt_yen(top1_total)} vs max-EV {fmt_yen(ev_total)}) の方向が一致しない。")
        md.append(f"  追加検証 (場別、季節別、最大連敗) で判断。")
    md.append("")
    md.append(f"### all-cars 戦略について")
    md.append(f"- n_bets {results['all_cars'][1]['n']:,} で profit {fmt_yen(all_total)}, ROI {results['all_cars'][1]['roi']*100:.1f}%")
    md.append(f"- 投票負荷が現実的か別途検討 (1 R 平均 {results['all_cars'][1]['n']/results['pred_top1'][1]['n_months']/30:.1f} 件/日)")
    md.append("")
    md.append(f"### 朝の email 問題との関連")
    md.append(f"- 仮に max-EV に切替えると 1 日通知頻度が変わる")
    md.append(f"- pred-top1 の月平均: {results['pred_top1'][1]['n']/results['pred_top1'][1]['n_months']:.0f} 件/月")
    md.append(f"- max-EV の月平均: {results['max_ev'][1]['n']/results['max_ev'][1]['n_months']:.0f} 件/月")
    md.append(f"- digest メールの優先度がさらに上がる (数が増えると個別通知が埋もれる)")

    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")
    print(f"\n=== 全体サマリ (thr={thr:.2f}) ===")
    for s, _ in strategies:
        label, r = results[s]
        print(f"  {label:30s}: n={r['n']:>5,}, profit={fmt_yen(r['profit']):>10s}, "
              f"ROI={r['roi']*100:6.2f}%, hit={r['hit_rate']*100:5.2f}%, "
              f"月勝率={r['month_ge_1']}/{r['n_months']}")
    print(f"\n=== 月次対決 (pred-top1 vs max-EV) ===")
    print(f"  pred-top1 勝: {top1_wins} 月")
    print(f"  max-EV 勝   : {ev_wins} 月")
    print(f"  引き分け    : {ties} 月")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
