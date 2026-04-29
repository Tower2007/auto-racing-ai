"""伊勢崎 2026-04 開催の日次 P&L 表(thr=1.45 採用時)

戦略: top-1 + ev_avg_calib ≥ 1.45 で複勝 100 円ベット
キャリブレーション: 前半 24ヶ月 (test_month < 2024-04) で isotonic fit
"""

from __future__ import annotations

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
THR = 1.45


def main():
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

    # 伊勢崎 2026-04 抽出
    target = preds[
        (preds["place_code"] == 3)
        & (preds["race_date"] >= "2026-04-01")
        & (preds["race_date"] <= "2026-04-30")
    ].copy()
    target["pred_calib"] = iso.transform(target["pred"].values)

    # オッズ・払戻 join
    target = target.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max", "win_odds"]],
        on=RACE_KEY + ["car_no"], how="left",
    )
    target["ev_avg_calib"] = target["pred_calib"] * (
        target["place_odds_min"] + target["place_odds_max"]
    ) / 2

    # top-1 抽出
    target["pred_rank"] = target.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)
    candidates = target[(target["pred_rank"] == 1) & target["ev_avg_calib"].notna()].copy()

    # thr 適用
    bets = candidates[candidates["ev_avg_calib"] >= THR].copy()

    # 払戻 join
    bets = bets.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "payout"}),
        on=RACE_KEY + ["car_no"], how="left",
    )
    bets["payout"] = bets["payout"].fillna(0)
    bets["hit"] = (bets["payout"] > 0).astype(int)
    bets["profit"] = bets["payout"] - BET

    # 日次サマリ
    daily = bets.groupby("race_date").agg(
        n_bets=("hit", "size"),
        n_hits=("hit", "sum"),
        cost=("hit", lambda s: len(s) * BET),
        payout=("payout", "sum"),
    ).reset_index()
    daily["profit"] = daily["payout"] - daily["cost"]
    daily["roi"] = daily["payout"] / daily["cost"]
    daily["hit_rate"] = daily["n_hits"] / daily["n_bets"]

    # 全 race-day を index に(伊勢崎が開催した日でベット 0 だった日も表示)
    all_iseseki_days = candidates.drop_duplicates("race_date")["race_date"].sort_values().reset_index(drop=True)
    daily = pd.DataFrame({"race_date": all_iseseki_days}).merge(daily, on="race_date", how="left").fillna(0)
    for c in ["n_bets", "n_hits", "cost"]:
        daily[c] = daily[c].astype(int)

    # 当日見送り race 数も
    skipped = candidates.groupby("race_date").size().reset_index(name="cand_total")
    daily = daily.merge(skipped, on="race_date", how="left").fillna(0)
    daily["skipped"] = (daily["cand_total"] - daily["n_bets"]).astype(int)

    # 合計行
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
    }])
    out_df = pd.concat([daily, total], ignore_index=True)

    # ベット内訳テーブル
    bet_detail = bets[[
        "race_date", "race_no", "car_no", "pred", "pred_calib",
        "place_odds_min", "place_odds_max", "ev_avg_calib", "win_odds", "payout", "hit",
    ]].sort_values(["race_date", "race_no"]).reset_index(drop=True)

    # 表示整形
    def fmt_yen(v):
        sign = "-" if v < 0 else ""
        return f"{sign}¥{abs(int(v)):,}"

    out_df_disp = out_df.copy()
    out_df_disp["roi"] = (out_df_disp["roi"] * 100).round(2).astype(str) + "%"
    out_df_disp["hit_rate"] = (out_df_disp["hit_rate"] * 100).round(1).astype(str) + "%"
    for c in ["cost", "payout", "profit"]:
        out_df_disp[c] = out_df_disp[c].apply(fmt_yen)
    # race_date を見やすく
    out_df_disp["race_date"] = out_df_disp["race_date"].apply(
        lambda v: v.strftime("%Y-%m-%d (%a)") if hasattr(v, "strftime") else v
    )
    out_df_disp = out_df_disp[[
        "race_date", "cand_total", "n_bets", "skipped",
        "n_hits", "hit_rate", "cost", "payout", "profit", "roi",
    ]].rename(columns={
        "cand_total": "全R", "n_bets": "ベット", "skipped": "見送り",
        "n_hits": "命中", "hit_rate": "命中率",
        "cost": "投資", "payout": "回収", "profit": "損益", "roi": "ROI",
    })

    bet_detail_disp = bet_detail.copy()
    bet_detail_disp["pred"] = bet_detail_disp["pred"].round(3)
    bet_detail_disp["pred_calib"] = bet_detail_disp["pred_calib"].round(3)
    bet_detail_disp["place_odds_min"] = bet_detail_disp["place_odds_min"].round(2)
    bet_detail_disp["place_odds_max"] = bet_detail_disp["place_odds_max"].round(2)
    bet_detail_disp["ev_avg_calib"] = bet_detail_disp["ev_avg_calib"].round(3)
    bet_detail_disp["win_odds"] = bet_detail_disp["win_odds"].round(1)
    bet_detail_disp["hit"] = bet_detail_disp["hit"].map({1: "✓", 0: "—"})
    bet_detail_disp["payout"] = bet_detail_disp["payout"].apply(fmt_yen)
    bet_detail_disp["race_date"] = bet_detail_disp["race_date"].dt.strftime("%m/%d")
    bet_detail_disp = bet_detail_disp.rename(columns={
        "race_no": "R", "car_no": "車", "place_odds_min": "min",
        "place_odds_max": "max", "ev_avg_calib": "EV",
        "win_odds": "単勝", "payout": "払戻",
    })

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"daily_pnl_isesaki_apr2026.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# 伊勢崎 2026-04 日次 P&L (thr=1.45 採用時)",
        "",
        f"**戦略**: 各レースで予測 top-1 車のうち `ev_avg_calib ≥ {THR}` の場合のみ 複勝 100 円ベット",
        f"**校正**: isotonic regression (前半 24ヶ月 fit)",
        "",
        "## 1. 日次サマリ",
        "",
        out_df_disp.to_markdown(index=False),
        "",
        "凡例: 全R=その日の総レース数 / ベット=実購入したレース数 / 見送り=EV不足で買わなかった数",
        "",
        "## 2. 個別ベット明細",
        "",
        bet_detail_disp.to_markdown(index=False),
        "",
        f"※ EV={THR} 以上のみ表示。pred_calib は校正後の予測確率(P(top3))",
    ]
    out.write_text("\n".join(md), encoding="utf-8")
    print(out_df_disp.to_string(index=False))
    print()
    print(bet_detail_disp.to_string(index=False))
    print(f"\nReport: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
