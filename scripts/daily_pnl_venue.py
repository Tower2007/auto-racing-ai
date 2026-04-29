"""指定場 × 期間の日次 P&L(thr=1.45 採用時)

使い方:
  python scripts/daily_pnl_venue.py 3 2025-10-29 2026-04-28
  → 伊勢崎 (pc=3) の指定期間
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100
CALIB_CUTOFF = "2024-04"

VENUE_NAMES = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("place_code", type=int, help="2=川口/3=伊勢崎/4=浜松/5=飯塚/6=山陽")
    p.add_argument("start_date", type=str, help="YYYY-MM-DD")
    p.add_argument("end_date", type=str, help="YYYY-MM-DD")
    p.add_argument("--thr", type=float, default=1.45)
    args = p.parse_args()

    venue = VENUE_NAMES.get(args.place_code, str(args.place_code))

    preds = pd.read_parquet(DATA / "walkforward_predictions_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()

    # キャリブレーション fit
    calib = preds[preds["test_month"] < CALIB_CUTOFF]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)

    target = preds[
        (preds["place_code"] == args.place_code)
        & (preds["race_date"] >= args.start_date)
        & (preds["race_date"] <= args.end_date)
    ].copy()
    target["pred_calib"] = iso.transform(target["pred"].values)
    target = target.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]],
        on=RACE_KEY + ["car_no"], how="left",
    )
    target["ev_avg_calib"] = target["pred_calib"] * (
        target["place_odds_min"] + target["place_odds_max"]
    ) / 2

    target["pred_rank"] = target.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)
    candidates = target[(target["pred_rank"] == 1) & target["ev_avg_calib"].notna()].copy()
    bets = candidates[candidates["ev_avg_calib"] >= args.thr].copy()

    bets = bets.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "payout"}),
        on=RACE_KEY + ["car_no"], how="left",
    )
    bets["payout"] = bets["payout"].fillna(0)
    bets["hit"] = (bets["payout"] > 0).astype(int)
    bets["profit"] = bets["payout"] - BET

    daily = bets.groupby("race_date").agg(
        n_bets=("hit", "size"),
        n_hits=("hit", "sum"),
        cost=("hit", lambda s: len(s) * BET),
        payout=("payout", "sum"),
    ).reset_index()
    daily["profit"] = daily["payout"] - daily["cost"]
    daily["roi"] = daily["payout"] / daily["cost"]
    daily["hit_rate"] = daily["n_hits"] / daily["n_bets"]

    # candidates 全 race-day を index に
    all_days = candidates.drop_duplicates("race_date")["race_date"].sort_values().reset_index(drop=True)
    daily = pd.DataFrame({"race_date": all_days}).merge(daily, on="race_date", how="left").fillna(0)
    for c in ["n_bets", "n_hits", "cost"]:
        daily[c] = daily[c].astype(int)

    skipped = candidates.groupby("race_date").size().reset_index(name="cand_total")
    daily = daily.merge(skipped, on="race_date", how="left").fillna(0)
    daily["cand_total"] = daily["cand_total"].astype(int)
    daily["skipped"] = (daily["cand_total"] - daily["n_bets"]).astype(int)

    # 累積収支
    daily = daily.sort_values("race_date").reset_index(drop=True)
    daily["cum_cost"] = daily["cost"].cumsum()
    daily["cum_payout"] = daily["payout"].cumsum()
    daily["cum_profit"] = daily["cum_payout"] - daily["cum_cost"]
    daily["cum_roi"] = daily["cum_payout"] / daily["cum_cost"].replace(0, 1)

    # 合計
    total_cost = daily["cost"].sum()
    total = pd.DataFrame([{
        "race_date": "合計",
        "cand_total": int(daily["cand_total"].sum()),
        "n_bets": int(daily["n_bets"].sum()),
        "n_hits": int(daily["n_hits"].sum()),
        "cost": int(total_cost),
        "payout": float(daily["payout"].sum()),
        "profit": float(daily["payout"].sum() - total_cost),
        "roi": float(daily["payout"].sum() / total_cost) if total_cost else 0,
        "hit_rate": float(daily["n_hits"].sum() / daily["n_bets"].sum()) if daily["n_bets"].sum() else 0,
        "skipped": int(daily["skipped"].sum()),
        "cum_cost": int(total_cost),
        "cum_payout": float(daily["payout"].sum()),
        "cum_profit": float(daily["payout"].sum() - total_cost),
        "cum_roi": float(daily["payout"].sum() / total_cost) if total_cost else 0,
    }])
    out_df = pd.concat([daily, total], ignore_index=True)

    # 月次集計も出す
    daily_only = daily.copy()
    daily_only["ym"] = pd.to_datetime(daily_only["race_date"]).dt.to_period("M").astype(str)
    monthly = daily_only.groupby("ym").agg(
        opens=("race_date", "size"),
        cand_total=("cand_total", "sum"),
        n_bets=("n_bets", "sum"),
        n_hits=("n_hits", "sum"),
        cost=("cost", "sum"),
        payout=("payout", "sum"),
    ).reset_index()
    monthly["profit"] = monthly["payout"] - monthly["cost"]
    monthly["roi"] = monthly["payout"] / monthly["cost"].replace(0, 1)
    monthly["hit_rate"] = monthly["n_hits"] / monthly["n_bets"].replace(0, 1)

    # 表示整形
    def fmt_yen(v):
        sign = "-" if v < 0 else ""
        return f"{sign}¥{abs(int(v)):,}"

    out_disp = out_df.copy()
    out_disp["roi"] = (out_disp["roi"] * 100).round(2).astype(str) + "%"
    out_disp["hit_rate"] = (out_disp["hit_rate"] * 100).round(0).astype(int).astype(str) + "%"
    out_disp["cum_roi"] = (out_disp["cum_roi"] * 100).round(1).astype(str) + "%"
    for c in ["cost", "payout", "profit", "cum_profit"]:
        out_disp[c] = out_disp[c].apply(fmt_yen)
    out_disp["race_date"] = out_disp["race_date"].apply(
        lambda v: v.strftime("%Y-%m-%d (%a)") if hasattr(v, "strftime") else v
    )
    out_disp = out_disp[[
        "race_date", "cand_total", "n_bets", "n_hits", "hit_rate",
        "cost", "payout", "profit", "roi", "cum_profit", "cum_roi",
    ]].rename(columns={
        "cand_total": "全R", "n_bets": "ベット", "n_hits": "命中", "hit_rate": "命中率",
        "cost": "投資", "payout": "回収", "profit": "損益", "roi": "ROI",
        "cum_profit": "累計損益", "cum_roi": "累計ROI",
    })

    monthly_disp = monthly.copy()
    monthly_disp["roi"] = (monthly_disp["roi"] * 100).round(2).astype(str) + "%"
    monthly_disp["hit_rate"] = (monthly_disp["hit_rate"] * 100).round(0).astype(int).astype(str) + "%"
    for c in ["cost", "payout", "profit"]:
        monthly_disp[c] = monthly_disp[c].apply(fmt_yen)
    monthly_disp = monthly_disp.rename(columns={
        "ym": "年月", "opens": "開催日", "cand_total": "全R",
        "n_bets": "ベット", "n_hits": "命中", "hit_rate": "命中率",
        "cost": "投資", "payout": "回収", "profit": "損益", "roi": "ROI",
    })

    today_str = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"daily_pnl_{venue}_{args.start_date}_to_{args.end_date}.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# {venue} 日次 P&L: {args.start_date} 〜 {args.end_date}",
        "",
        f"**戦略**: top-1 + ev_avg_calib ≥ {args.thr} で複勝 100 円ベット",
        f"**校正**: isotonic regression (前半 24ヶ月 fit)",
        "",
        "## 月次サマリ",
        "",
        monthly_disp.to_markdown(index=False),
        "",
        "## 日次明細",
        "",
        out_disp.to_markdown(index=False),
    ]
    out.write_text("\n".join(md), encoding="utf-8")

    print("\n=== 月次 ===")
    print(monthly_disp.to_string(index=False))
    print("\n=== 日次 ===")
    print(out_disp.to_string(index=False))
    print(f"\nReport: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
