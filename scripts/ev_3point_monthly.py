"""3点BUY 戦略の月次安定性検証

ev_3point_buy.py で見つかった thr=1.45 と thr=1.80 について、
eval 期間(月リスト後半半分)を月次に分解して以下を表示:
  - 月別 race 数 / ベット数
  - 月別 複勝/3連単/3連複/合算 ROI
  - 月次 ≥ 100% 月数
  - 標準偏差・最大・最小
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


def load_eval_picks() -> tuple[pd.DataFrame, list[str]]:
    """eval 用 picks と eval_months(後半半分の月リスト)を返す。"""
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])

    df = preds.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]],
        on=RACE_KEY + ["car_no"], how="left",
    ).dropna(subset=["place_odds_min"])
    df["pred_rank"] = df.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)

    months = sorted(df["test_month"].unique())
    if len(months) < 2:
        raise SystemExit("test_month が 2 ヶ月未満で校正/評価分割できません")
    half = len(months) // 2
    calib_months = months[:half]
    eval_months = months[half:]
    print(f"[calib] {calib_months[0]} - {calib_months[-1]} ({len(calib_months)} months)")
    print(f"[eval ] {eval_months[0]} - {eval_months[-1]} ({len(eval_months)} months)")

    calib = df[df["test_month"].isin(calib_months)]
    eval_df = df[df["test_month"].isin(eval_months)].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    eval_df["pred_calib"] = iso.transform(eval_df["pred"].values)
    eval_df["ev_avg_calib"] = eval_df["pred_calib"] * (
        eval_df["place_odds_min"] + eval_df["place_odds_max"]
    ) / 2

    sub = eval_df[eval_df["pred_rank"] <= 3].copy()
    sub["pred_rank"] = sub["pred_rank"].astype(int)
    pivoted = sub.pivot_table(
        index=RACE_KEY + ["test_month"], columns="pred_rank",
        values="car_no", aggfunc="first",
    )
    pivoted.columns = [f"pick{c}" for c in pivoted.columns]
    pivoted = pivoted.reset_index()
    top1 = eval_df[eval_df["pred_rank"] == 1][
        RACE_KEY + ["pred_calib", "ev_avg_calib"]
    ].drop_duplicates(subset=RACE_KEY)
    return pivoted.merge(top1, on=RACE_KEY, how="left"), eval_months


def load_payouts() -> dict[str, pd.DataFrame]:
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    return {
        "fns": pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
            .groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum(),
        "rt3": pay[pay["bet_type"] == "rt3"][RACE_KEY + ["car_no_1", "car_no_2", "car_no_3", "refund"]]
            .groupby(RACE_KEY + ["car_no_1", "car_no_2", "car_no_3"], as_index=False)["refund"].sum(),
        "rf3": pay[pay["bet_type"] == "rf3"][RACE_KEY + ["car_no_1", "car_no_2", "car_no_3", "refund"]]
            .groupby(RACE_KEY + ["car_no_1", "car_no_2", "car_no_3"], as_index=False)["refund"].sum(),
    }


def attach_payouts(picks: pd.DataFrame, payouts: dict[str, pd.DataFrame]) -> pd.DataFrame:
    df = picks.dropna(subset=["pick1", "pick2", "pick3"]).copy()
    df["pick1"] = df["pick1"].astype(int)
    df["pick2"] = df["pick2"].astype(int)
    df["pick3"] = df["pick3"].astype(int)

    fns = payouts["fns"].rename(columns={"car_no_1": "pick1", "refund": "fns_payout"})
    df = df.merge(fns, on=RACE_KEY + ["pick1"], how="left")
    df["fns_payout"] = df["fns_payout"].fillna(0)

    rt3 = payouts["rt3"].rename(columns={
        "car_no_1": "pick1", "car_no_2": "pick2", "car_no_3": "pick3",
        "refund": "rt3_payout",
    })
    df = df.merge(rt3, on=RACE_KEY + ["pick1", "pick2", "pick3"], how="left")
    df["rt3_payout"] = df["rt3_payout"].fillna(0)

    rf3 = payouts["rf3"].copy()
    sorted_rf3 = pd.DataFrame(
        np.sort(rf3[["car_no_1", "car_no_2", "car_no_3"]].values, axis=1),
        index=rf3.index, columns=["a", "b", "c"],
    )
    rf3["car_set"] = list(zip(sorted_rf3["a"], sorted_rf3["b"], sorted_rf3["c"]))
    rf3 = rf3.groupby(RACE_KEY + ["car_set"], as_index=False)["refund"].sum()
    rf3 = rf3.rename(columns={"refund": "rf3_payout"})

    pick_set = pd.DataFrame(
        np.sort(df[["pick1", "pick2", "pick3"]].values, axis=1),
        index=df.index, columns=["a", "b", "c"],
    )
    df["car_set"] = list(zip(pick_set["a"], pick_set["b"], pick_set["c"]))
    df = df.merge(rf3, on=RACE_KEY + ["car_set"], how="left")
    df["rf3_payout"] = df["rf3_payout"].fillna(0)
    return df


def monthly_breakdown(df: pd.DataFrame, thr: float) -> pd.DataFrame:
    sub = df[df["ev_avg_calib"] >= thr]
    if sub.empty:
        return pd.DataFrame()
    g = sub.groupby("test_month").agg(
        n=("ev_avg_calib", "size"),
        fns_payout=("fns_payout", "sum"),
        rt3_payout=("rt3_payout", "sum"),
        rf3_payout=("rf3_payout", "sum"),
    ).reset_index()
    g["fns_stake"] = g["n"] * BET
    g["rt3_stake"] = g["n"] * BET
    g["rf3_stake"] = g["n"] * BET
    g["total_stake"] = g["n"] * BET * 3
    g["total_payout"] = g["fns_payout"] + g["rt3_payout"] + g["rf3_payout"]
    g["fns_roi"] = g["fns_payout"] / g["fns_stake"]
    g["rt3_roi"] = g["rt3_payout"] / g["rt3_stake"]
    g["rf3_roi"] = g["rf3_payout"] / g["rf3_stake"]
    g["combined_roi"] = g["total_payout"] / g["total_stake"]
    g["combined_profit"] = g["total_payout"] - g["total_stake"]
    return g


def summarize(monthly: pd.DataFrame, label: str) -> dict:
    if monthly.empty:
        return {"label": label, "n_months": 0}
    return {
        "label": label,
        "n_months": len(monthly),
        "races_total": int(monthly["n"].sum()),
        "races_per_month_mean": float(monthly["n"].mean()),
        "fns_roi_mean": float(monthly["fns_roi"].mean()),
        "fns_months_ge1": int((monthly["fns_roi"] >= 1.0).sum()),
        "rt3_roi_mean": float(monthly["rt3_roi"].mean()),
        "rt3_months_ge1": int((monthly["rt3_roi"] >= 1.0).sum()),
        "rt3_roi_std": float(monthly["rt3_roi"].std()),
        "rt3_roi_min": float(monthly["rt3_roi"].min()),
        "rt3_roi_max": float(monthly["rt3_roi"].max()),
        "rf3_roi_mean": float(monthly["rf3_roi"].mean()),
        "rf3_months_ge1": int((monthly["rf3_roi"] >= 1.0).sum()),
        "rf3_roi_std": float(monthly["rf3_roi"].std()),
        "rf3_roi_min": float(monthly["rf3_roi"].min()),
        "rf3_roi_max": float(monthly["rf3_roi"].max()),
        "combined_roi_mean": float(monthly["combined_roi"].mean()),
        "combined_roi_std": float(monthly["combined_roi"].std()),
        "combined_months_ge1": int((monthly["combined_roi"] >= 1.0).sum()),
        "combined_roi_min": float(monthly["combined_roi"].min()),
        "combined_roi_max": float(monthly["combined_roi"].max()),
        "total_profit": float(monthly["combined_profit"].sum()),
    }


def render_monthly_table(monthly: pd.DataFrame) -> str:
    if monthly.empty:
        return "(候補なし)"
    show = monthly.copy()
    show["combined_profit"] = show["combined_profit"].apply(lambda v: f"¥{int(v):+,}")
    for c in ["fns_roi", "rt3_roi", "rf3_roi", "combined_roi"]:
        show[c] = (show[c] * 100).round(1).astype(str) + "%"
    return show[["test_month", "n", "fns_roi", "rt3_roi", "rf3_roi",
                 "combined_roi", "combined_profit"]].to_markdown(index=False)


def main():
    picks, eval_months = load_eval_picks()
    payouts = load_payouts()
    df = attach_payouts(picks, payouts)
    print(f"Eval set with payouts: {len(df):,} races")

    results = {}
    for thr in [1.45, 1.50, 1.80, 2.00]:
        monthly = monthly_breakdown(df, thr)
        s = summarize(monthly, f"thr={thr}")
        results[thr] = (monthly, s)

    print()
    print("=" * 80)
    for thr, (monthly, s) in results.items():
        if not monthly.empty:
            print(f"\n=== thr={thr} 月次サマリ ===")
            print(f"  月数 {s['n_months']}, race 計 {s['races_total']} (月平均 {s['races_per_month_mean']:.1f})")
            print(f"  複勝   ROI mean={s['fns_roi_mean']*100:.2f}%   ≥100% 月: {s['fns_months_ge1']}/{s['n_months']}")
            print(f"  3連単  ROI mean={s['rt3_roi_mean']*100:.2f}% std={s['rt3_roi_std']*100:.2f}% "
                  f"min={s['rt3_roi_min']*100:.1f}% max={s['rt3_roi_max']*100:.1f}% ≥100% 月: {s['rt3_months_ge1']}/{s['n_months']}")
            print(f"  3連複  ROI mean={s['rf3_roi_mean']*100:.2f}% std={s['rf3_roi_std']*100:.2f}% "
                  f"min={s['rf3_roi_min']*100:.1f}% max={s['rf3_roi_max']*100:.1f}% ≥100% 月: {s['rf3_months_ge1']}/{s['n_months']}")
            print(f"  合算   ROI mean={s['combined_roi_mean']*100:.2f}% std={s['combined_roi_std']*100:.2f}% "
                  f"min={s['combined_roi_min']*100:.1f}% max={s['combined_roi_max']*100:.1f}% ≥100% 月: {s['combined_months_ge1']}/{s['n_months']}")
            print(f"  total_profit: ¥{int(s['total_profit']):+,}")

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_3point_monthly_{today}.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# 3点BUY 月次安定性検証 ({today})",
        "",
        f"対象: walk-forward eval {len(eval_months)} ヶ月"
        f"({eval_months[0]} 〜 {eval_months[-1]})",
        "",
        "## サマリ",
        "",
        "| thr | 月数 | races/月 | 複勝平均 | 3連単平均 | 3連単 std | 3連複平均 | 3連複 std | 合算平均 | 合算 std | 合算≥100% | 累計利益 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for thr, (monthly, s) in results.items():
        if monthly.empty:
            continue
        md.append(
            f"| {thr} | {s['n_months']} | {s['races_per_month_mean']:.1f} | "
            f"{s['fns_roi_mean']*100:.1f}% | {s['rt3_roi_mean']*100:.1f}% | "
            f"{s['rt3_roi_std']*100:.1f}% | {s['rf3_roi_mean']*100:.1f}% | "
            f"{s['rf3_roi_std']*100:.1f}% | {s['combined_roi_mean']*100:.1f}% | "
            f"{s['combined_roi_std']*100:.1f}% | "
            f"{s['combined_months_ge1']}/{s['n_months']} | "
            f"¥{int(s['total_profit']):+,} |"
        )
    md.append("")

    for thr, (monthly, s) in results.items():
        if monthly.empty:
            continue
        md.append(f"## thr={thr} 月次明細")
        md.append("")
        md.append(render_monthly_table(monthly))
        md.append("")

    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
