"""任意月の全購入パターン シミュレーション集計

simulate_full_pattern.py のロジックを 1 ヶ月分(全日×全場)で回して
日別/場別/券種別の収支を Markdown で出力。

使い方:
  python scripts/simulate_full_pattern_monthly.py 2026-04
  python scripts/simulate_full_pattern_monthly.py 2026-03 2026-04   # 期間
  python scripts/simulate_full_pattern_monthly.py 2025-04 2026-04 --venue isesaki  # 場限定
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

VENUE_NAMES = {2: "kawaguchi", 3: "isesaki", 4: "hamamatsu", 5: "iizuka", 6: "sanyou"}
NAME_TO_PC = {v: k for k, v in VENUE_NAMES.items()}

BET_LABELS = {
    "tns": "単勝",
    "fns": "複勝",
    "wid": "ワイド",
    "rf3": "三連複",
    "rt3": "三連単",
}
BET_ORDER = ["tns", "fns", "wid", "rf3", "rt3"]


def fmt_yen(v: float) -> str:
    if pd.isna(v):
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(int(v)):,}"


def make_picks(top_cars: list[int]) -> dict[str, list[int]]:
    t1 = top_cars[0]
    t2 = top_cars[1] if len(top_cars) >= 2 else None
    t3 = top_cars[2] if len(top_cars) >= 3 else None
    return {
        "tns": [t1],
        "fns": [t1],
        "wid": [t1, t2] if t2 else [],
        "rf3": [t1, t2, t3] if (t2 and t3) else [],
        "rt3": [t1, t2, t3] if (t2 and t3) else [],
    }


def check_hit(bt: str, picked: list[int], pay_rows: pd.DataFrame) -> tuple[bool, float]:
    if not picked or pay_rows.empty:
        return False, 0.0
    if bt in ("tns", "fns"):
        match = pay_rows[pay_rows["car_no_1"] == picked[0]]
    elif bt == "wid":
        s = sorted(picked)
        match = pay_rows[
            ((pay_rows["car_no_1"] == s[0]) & (pay_rows["car_no_2"] == s[1])) |
            ((pay_rows["car_no_1"] == s[1]) & (pay_rows["car_no_2"] == s[0]))
        ]
    elif bt == "rf3":
        s = sorted(picked)
        match = pay_rows[
            (pay_rows["car_no_1"] == s[0]) &
            (pay_rows["car_no_2"] == s[1]) &
            (pay_rows["car_no_3"] == s[2])
        ]
    elif bt == "rt3":
        match = pay_rows[
            (pay_rows["car_no_1"] == picked[0]) &
            (pay_rows["car_no_2"] == picked[1]) &
            (pay_rows["car_no_3"] == picked[2])
        ]
    else:
        return False, 0.0
    if match.empty:
        return False, 0.0
    return True, float(match["refund"].sum())


def parse_period(start: str, end: str | None) -> tuple[pd.Timestamp, pd.Timestamp]:
    s = pd.Period(start, freq="M") if len(start) == 7 else pd.Timestamp(start)
    if end is None:
        e = s
    else:
        e = pd.Period(end, freq="M") if len(end) == 7 else pd.Timestamp(end)
    if isinstance(s, pd.Period):
        sd = s.to_timestamp(how="start")
    else:
        sd = s
    if isinstance(e, pd.Period):
        ed = e.to_timestamp(how="end").normalize()
    else:
        ed = e
    return sd, ed


def main():
    p = argparse.ArgumentParser()
    p.add_argument("start", help="開始月/日 (例: 2026-04, 2026-04-01)")
    p.add_argument("end", nargs="?", default=None, help="終了月/日 (省略時 = start 単月)")
    p.add_argument("--venue", default=None,
                   help="場名 or place_code で限定 (例: isesaki, 3)")
    args = p.parse_args()

    start_date, end_date = parse_period(args.start, args.end)
    venue_pc = None
    if args.venue:
        if args.venue.isdigit():
            venue_pc = int(args.venue)
        elif args.venue in NAME_TO_PC:
            venue_pc = NAME_TO_PC[args.venue]
        else:
            raise ValueError(f"場名が不明: {args.venue}")
    venue_suffix = f"_{VENUE_NAMES[venue_pc]}" if venue_pc else ""
    period_label = f"{start_date.date()}_to_{end_date.date()}{venue_suffix}"
    print(f"対象期間: {start_date.date()} 〜 {end_date.date()}"
          + (f" / 場: {VENUE_NAMES[venue_pc]} (pc={venue_pc})" if venue_pc else ""))

    # データロード
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])

    # 校正 (2024-04 以前で fit)
    calib = preds[preds["test_month"] < CALIB_CUTOFF]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    preds["pred_calib"] = iso.transform(preds["pred"].values)

    # 期間で絞る
    period_mask = (preds["race_date"] >= start_date) & (preds["race_date"] <= end_date)
    if venue_pc is not None:
        period_mask = period_mask & (preds["place_code"] == venue_pc)
    period_preds = preds[period_mask].copy()
    pay_mask = (pay["race_date"] >= start_date) & (pay["race_date"] <= end_date)
    if venue_pc is not None:
        pay_mask = pay_mask & (pay["place_code"] == venue_pc)
    period_pay = pay[pay_mask]

    if period_preds.empty:
        print("対象データなし")
        return

    # ── レース毎にピック作成 + 結果計算 ──
    rows = []  # 各レース 1 行(全券種合算)
    bt_rows = []  # 各レース×各券種

    for (rd, pc, r), grp in period_preds.groupby(RACE_KEY):
        grp_sorted = grp.sort_values("pred_calib", ascending=False)
        top_cars = grp_sorted["car_no"].tolist()[:3]
        picks = make_picks(top_cars)
        race_pay = period_pay[
            (period_pay["race_date"] == rd) &
            (period_pay["place_code"] == pc) &
            (period_pay["race_no"] == r)
        ]
        race_cost = 0
        race_refund = 0
        for bt in BET_ORDER:
            picked = picks.get(bt, [])
            if not picked:
                continue
            bt_pay = race_pay[race_pay["bet_type"] == bt]
            hit, refund = check_hit(bt, picked, bt_pay)
            race_cost += BET
            race_refund += refund
            bt_rows.append({
                "race_date": rd, "place_code": pc, "race_no": r,
                "bet_type": bt, "hit": int(hit), "cost": BET, "refund": refund,
            })
        rows.append({
            "race_date": rd, "place_code": pc, "race_no": r,
            "cost": race_cost, "refund": race_refund,
            "profit": race_refund - race_cost,
        })

    df = pd.DataFrame(rows)
    btdf = pd.DataFrame(bt_rows)
    df["venue"] = df["place_code"].map(VENUE_NAMES)
    btdf["venue"] = btdf["place_code"].map(VENUE_NAMES)

    # ── 集計 ──
    n_races = len(df)
    total_cost = df["cost"].sum()
    total_refund = df["refund"].sum()
    total_profit = total_refund - total_cost
    total_roi = total_refund / total_cost if total_cost else 0

    # 日別
    daily = df.groupby("race_date").agg(
        n=("cost", "size"), cost=("cost", "sum"),
        refund=("refund", "sum"),
    ).reset_index()
    daily["profit"] = daily["refund"] - daily["cost"]
    daily["cum_profit"] = daily["profit"].cumsum()
    daily["roi"] = daily["refund"] / daily["cost"]

    # 場別
    by_venue = df.groupby("venue").agg(
        n=("cost", "size"), cost=("cost", "sum"),
        refund=("refund", "sum"),
    ).reset_index()
    by_venue["profit"] = by_venue["refund"] - by_venue["cost"]
    by_venue["roi"] = by_venue["refund"] / by_venue["cost"]

    # 券種別
    by_bet = btdf.groupby("bet_type").agg(
        n=("cost", "size"), hit=("hit", "sum"),
        cost=("cost", "sum"), refund=("refund", "sum"),
    ).reset_index()
    by_bet["profit"] = by_bet["refund"] - by_bet["cost"]
    by_bet["roi"] = by_bet["refund"] / by_bet["cost"]
    by_bet["hit_rate"] = by_bet["hit"] / by_bet["n"]
    by_bet["label"] = by_bet["bet_type"].map(BET_LABELS)
    by_bet = by_bet.set_index("bet_type").reindex(BET_ORDER).reset_index()

    # 場×券種クロス
    cross = btdf.groupby(["venue", "bet_type"]).agg(
        n=("cost", "size"), hit=("hit", "sum"),
        cost=("cost", "sum"), refund=("refund", "sum"),
    ).reset_index()
    cross["profit"] = cross["refund"] - cross["cost"]
    cross["roi"] = cross["refund"] / cross["cost"]

    # ── Markdown 出力 ──
    md = []
    md.append(f"# 全購入パターン 月次シミュレーション ({start_date.date()} 〜 {end_date.date()})")
    md.append("")
    md.append(f"**対象**: {n_races} レース({len(daily)} 開催日 × 平均 "
              f"{n_races/len(daily):.1f} R/日)")
    md.append("**戦略**: 中間モデル top1〜top3 を 5 券種に割り付け(二車連・二車単 除外)")
    md.append(f"**1 R 投資**: ¥{BET * len(BET_ORDER)} (¥{BET} × {len(BET_ORDER)} 券種)")
    md.append("")

    md.append("## 全体サマリ")
    md.append("")
    md.append(f"- レース数: **{n_races}**")
    md.append(f"- 投資合計: **{fmt_yen(total_cost)}**")
    md.append(f"- 払戻合計: **{fmt_yen(total_refund)}**")
    md.append(f"- **収支: {fmt_yen(total_profit)}** (ROI **{total_roi*100:.1f}%**)")
    md.append("")

    md.append("## 券種別収支")
    md.append("")
    md.append("| 券種 | 買 | 当 | hit% | 投資 | 払戻 | 収支 | ROI |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for _, r in by_bet.iterrows():
        md.append(
            f"| {r['label']} | {int(r['n'])} | {int(r['hit'])} | "
            f"{r['hit_rate']*100:.1f}% | {fmt_yen(r['cost'])} | "
            f"{fmt_yen(r['refund'])} | **{fmt_yen(r['profit'])}** | "
            f"{r['roi']*100:.1f}% |"
        )
    md.append("")

    md.append("## 場別収支")
    md.append("")
    md.append("| 場 | レース | 投資 | 払戻 | 収支 | ROI |")
    md.append("|---|---:|---:|---:|---:|---:|")
    for _, r in by_venue.sort_values("profit", ascending=False).iterrows():
        md.append(
            f"| {r['venue']} | {int(r['n'])} | {fmt_yen(r['cost'])} | "
            f"{fmt_yen(r['refund'])} | **{fmt_yen(r['profit'])}** | "
            f"{r['roi']*100:.1f}% |"
        )
    md.append("")

    md.append("## 場×券種 クロス収支(ROI)")
    md.append("")
    pivot = cross.pivot(index="venue", columns="bet_type", values="roi") * 100
    pivot = pivot.reindex(columns=BET_ORDER)
    md.append("| 場 | " + " | ".join(BET_LABELS[b] for b in BET_ORDER) + " |")
    md.append("|---|" + "|".join(["---:"] * len(BET_ORDER)) + "|")
    for v in pivot.index:
        cells = []
        for b in BET_ORDER:
            val = pivot.loc[v, b]
            cells.append(f"{val:.0f}%" if pd.notna(val) else "—")
        md.append(f"| {v} | " + " | ".join(cells) + " |")
    md.append("")

    md.append("## 日別収支(累計)")
    md.append("")
    md.append("| 日 | レース | 投資 | 払戻 | 当日収支 | 累計収支 | ROI |")
    md.append("|---|---:|---:|---:|---:|---:|---:|")
    for _, r in daily.iterrows():
        md.append(
            f"| {r['race_date'].date()} | {int(r['n'])} | "
            f"{fmt_yen(r['cost'])} | {fmt_yen(r['refund'])} | "
            f"{fmt_yen(r['profit'])} | **{fmt_yen(r['cum_profit'])}** | "
            f"{r['roi']*100:.0f}% |"
        )
    md.append("")

    out = REPORTS / f"sim_full_monthly_{period_label}.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")
    print(f"\n=== 全体 ===")
    print(f"  {n_races} レース, 投資 {fmt_yen(total_cost)} → 払戻 {fmt_yen(total_refund)}")
    print(f"  収支 {fmt_yen(total_profit)} (ROI {total_roi*100:.1f}%)")
    print(f"\n=== 券種別 ===")
    for _, r in by_bet.iterrows():
        print(f"  {r['label']:6s}: {int(r['hit'])}/{int(r['n'])} hit ({r['hit_rate']*100:.1f}%), "
              f"収支 {fmt_yen(r['profit'])} ({r['roi']*100:.1f}%)")
    print(f"\n=== 場別 ===")
    for _, r in by_venue.sort_values("profit", ascending=False).iterrows():
        print(f"  {r['venue']:10s}: {int(r['n']):3d} R, "
              f"収支 {fmt_yen(r['profit']):>10s} ({r['roi']*100:.1f}%)")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
