"""任意期間の本番戦略シミュレーション + 収支表

本番運用条件:
- 中間モデル(walkforward_predictions_morning_top3.parquet)
- isotonic 校正(cutoff=2024-04)
- top1 + ev_avg_calib >= 1.50
- 複勝 fns、1 R 100 円固定

使い方:
  python scripts/simulate_2026_mar_apr.py                    # default 2026-03〜04
  python scripts/simulate_2026_mar_apr.py 2025-10-01 2026-04-30
  python scripts/simulate_2026_mar_apr.py 2025-10 2026-04    # 月指定もOK

出力:
- reports/simulate_<start>_to_<end>.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100
CALIB_CUTOFF = "2024-04"
THR = 1.50

VENUE_NAMES = {2: "kawaguchi", 3: "isesaki", 4: "hamamatsu", 5: "iizuka", 6: "sanyou"}


def normalize_date(s: str, end: bool = False) -> str:
    """'2025-10' → '2025-10-01' (start) or '2025-10-31' (end)。'YYYY-MM-DD' はそのまま。"""
    if len(s) == 7:  # YYYY-MM
        if end:
            return (pd.Period(s, freq="M").to_timestamp(how="end")).date().isoformat()
        return f"{s}-01"
    return s


def load_and_pick(start_date: str, end_date: str):
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()

    df = preds.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max", "win_odds"]],
        on=RACE_KEY + ["car_no"], how="left",
    )
    df = df.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "payout"}),
        on=RACE_KEY + ["car_no"], how="left",
    )
    df["payout"] = df["payout"].fillna(0)
    df["hit"] = (df["payout"] > 0).astype(int)
    df["pred_rank"] = df.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)
    df = df.dropna(subset=["place_odds_min"])

    # 校正: 2024-04 より前の OOF 予測で fit
    calib = df[df["test_month"] < CALIB_CUTOFF]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    df["pred_calib"] = iso.transform(df["pred"].values)
    df["ev_avg_calib"] = (
        df["pred_calib"] * (df["place_odds_min"] + df["place_odds_max"]) / 2
    )

    # 期間 + top1 + EV>=1.50 で picks 抽出
    target_period = (df["race_date"] >= start_date) & (df["race_date"] <= end_date)
    picks = df[target_period & (df["pred_rank"] == 1) & (df["ev_avg_calib"] >= THR)].copy()
    picks["venue"] = picks["place_code"].map(VENUE_NAMES)
    picks["profit"] = picks["payout"] - BET
    return picks.sort_values(["race_date", "place_code", "race_no"]).reset_index(drop=True)


def fmt_yen(v: float) -> str:
    if pd.isna(v):
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(int(v)):,}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("start", nargs="?", default="2026-03-01",
                   help="開始日 'YYYY-MM-DD' or 'YYYY-MM' (default: 2026-03-01)")
    p.add_argument("end", nargs="?", default="2026-04-30",
                   help="終了日 'YYYY-MM-DD' or 'YYYY-MM' (default: 2026-04-30)")
    args = p.parse_args()
    start_date = normalize_date(args.start, end=False)
    end_date = normalize_date(args.end, end=True)

    picks = load_and_pick(start_date, end_date)
    print(f"期間指定: {start_date} 〜 {end_date}")
    if picks.empty:
        print("picks が 0 件です。期間を確認してください。")
        return
    print(f"全 picks: {len(picks)} 件")
    print(f"実データ範囲: {picks['race_date'].min().date()} 〜 {picks['race_date'].max().date()}")

    # ── サマリ ──
    n = len(picks)
    cost = n * BET
    payout = picks["payout"].sum()
    profit = payout - cost
    roi = (payout / cost) if cost else 0
    hit = picks["hit"].mean() if n else 0

    # 月別
    picks["year_month"] = picks["race_date"].dt.to_period("M").astype(str)
    monthly = picks.groupby("year_month").agg(
        n=("payout", "size"),
        hit=("hit", "mean"),
        cost=("payout", lambda s: len(s) * BET),
        payout=("payout", "sum"),
    ).reset_index()
    monthly["profit"] = monthly["payout"] - monthly["cost"]
    monthly["roi"] = monthly["payout"] / monthly["cost"]

    # 場別
    by_venue = picks.groupby("venue").agg(
        n=("payout", "size"),
        hit=("hit", "mean"),
        cost=("payout", lambda s: len(s) * BET),
        payout=("payout", "sum"),
    ).reset_index()
    by_venue["profit"] = by_venue["payout"] - by_venue["cost"]
    by_venue["roi"] = by_venue["payout"] / by_venue["cost"]

    # 日別
    daily = picks.groupby("race_date").agg(
        n=("payout", "size"),
        hits=("hit", "sum"),
        payout=("payout", "sum"),
    ).reset_index()
    daily["cost"] = daily["n"] * BET
    daily["profit"] = daily["payout"] - daily["cost"]
    daily["cum_profit"] = daily["profit"].cumsum()
    daily["roi"] = daily["payout"] / daily["cost"]

    # ── レポート出力 ──
    period_label = f"{picks['race_date'].min().date()}_to_{picks['race_date'].max().date()}"
    md = []
    md.append(f"# シミュレーション収支表 ({picks['race_date'].min().date()} 〜 {picks['race_date'].max().date()})")
    md.append("")
    md.append("**戦略**: 中間モデル + top1 + ev_avg_calib >= 1.50 + 複勝(fns) / 1 R 100 円")
    md.append(f"**期間**: {picks['race_date'].min().date()} 〜 {picks['race_date'].max().date()}")
    md.append(f"**データ**: walkforward_predictions_morning_top3 の OOF 予測 + odds_summary + payouts")
    md.append("")

    md.append("## 全体サマリ")
    md.append("")
    md.append(f"- ベット数: **{n}** 件")
    md.append(f"- 投資合計: **{fmt_yen(cost)}**")
    md.append(f"- 払戻合計: **{fmt_yen(payout)}**")
    md.append(f"- **収支: {fmt_yen(profit)}** (ROI **{roi*100:.1f}%**)")
    md.append(f"- 的中率: {hit*100:.1f}%")
    md.append("")

    md.append("## 月別収支")
    md.append("")
    md.append("| 月 | n | hit% | 投資 | 払戻 | 収支 | ROI |")
    md.append("|---|---:|---:|---:|---:|---:|---:|")
    for _, r in monthly.iterrows():
        md.append(
            f"| {r['year_month']} | {int(r['n'])} | {r['hit']*100:.1f}% | "
            f"{fmt_yen(r['cost'])} | {fmt_yen(r['payout'])} | "
            f"**{fmt_yen(r['profit'])}** | {r['roi']*100:.1f}% |"
        )
    md.append("")

    md.append("## 場別収支")
    md.append("")
    md.append("| 場 | n | hit% | 投資 | 払戻 | 収支 | ROI |")
    md.append("|---|---:|---:|---:|---:|---:|---:|")
    for _, r in by_venue.sort_values("profit", ascending=False).iterrows():
        md.append(
            f"| {r['venue']} | {int(r['n'])} | {r['hit']*100:.1f}% | "
            f"{fmt_yen(r['cost'])} | {fmt_yen(r['payout'])} | "
            f"**{fmt_yen(r['profit'])}** | {r['roi']*100:.1f}% |"
        )
    md.append("")

    md.append("## 日別収支(累計)")
    md.append("")
    md.append("| 日 | n | hits | 投資 | 払戻 | 当日収支 | 累計収支 | ROI |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for _, r in daily.iterrows():
        md.append(
            f"| {r['race_date'].date()} | {int(r['n'])} | {int(r['hits'])} | "
            f"{fmt_yen(r['cost'])} | {fmt_yen(r['payout'])} | "
            f"{fmt_yen(r['profit'])} | **{fmt_yen(r['cum_profit'])}** | "
            f"{r['roi']*100:.0f}% |"
        )
    md.append("")

    md.append("## 全 picks 一覧(参考)")
    md.append("")
    md.append("| 日 | 場 | R | 車 | pred_calib | EV | min | max | win_odds | 払戻 | 結果 |")
    md.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for _, r in picks.iterrows():
        result = "○" if r["hit"] else "✗"
        md.append(
            f"| {r['race_date'].date()} | {r['venue']} | {int(r['race_no'])} | "
            f"{int(r['car_no'])} | {r['pred_calib']:.3f} | {r['ev_avg_calib']:.2f} | "
            f"{r['place_odds_min']:.1f} | {r['place_odds_max']:.1f} | "
            f"{r['win_odds']:.1f} | {fmt_yen(r['payout'])} | {result} |"
        )
    md.append("")

    out = REPORTS / f"simulate_{period_label}.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")
    print(f"\n=== サマリ ===")
    print(f"  ベット数: {n}, 投資 {fmt_yen(cost)}, 払戻 {fmt_yen(payout)}")
    print(f"  収支 {fmt_yen(profit)} (ROI {roi*100:.1f}%, 的中 {hit*100:.1f}%)")
    print(f"\n=== 月別 ===")
    for _, r in monthly.iterrows():
        print(f"  {r['year_month']}: n={int(r['n'])}, profit={fmt_yen(r['profit'])}, "
              f"ROI={r['roi']*100:.1f}%, hit={r['hit']*100:.1f}%")
    print(f"\n=== 場別 ===")
    for _, r in by_venue.sort_values("profit", ascending=False).iterrows():
        print(f"  {r['venue']:10s}: n={int(r['n']):3d}, profit={fmt_yen(r['profit']):>10s}, "
              f"ROI={r['roi']*100:.1f}%, hit={r['hit']*100:.1f}%")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
