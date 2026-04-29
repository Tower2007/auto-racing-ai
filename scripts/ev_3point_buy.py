"""3点BUY 戦略検証(boat-racing-ai v0.5.0 移植)

複勝 ev_avg_calib >= thr を「歪みあり race」のシグナルとし、選別された race で
  - 複勝 (top-1)
  - 3連単 (top-1 → top-2 → top-3)
  - 3連複 ({top-1, top-2, top-3})
を同時 100 円ベット。各券種の ROI と合算 ROI を集計。

eval set: walk-forward predictions を月リストで前半/後半に分け、
前半で isotonic 校正、後半で EV 評価(boat ml_ev_strategy.py 方式)。
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


def load_eval_set() -> pd.DataFrame:
    """中間モデル予測 + place_odds + payouts を join、校正後 EV 付きの eval set。

    test_month の前半で isotonic 校正、後半で eval。境界は実データの月数で動的決定。
    """
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])

    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])

    df = preds.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]],
        on=RACE_KEY + ["car_no"], how="left",
    ).dropna(subset=["place_odds_min"])

    df["pred_rank"] = df.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)

    # 月リストを前半(校正)・後半(評価)に動的分割
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
    return eval_df


def get_top3_per_race(eval_df: pd.DataFrame) -> pd.DataFrame:
    """各レースの top-3 (rank 1-3) を pivot した DF を返す。"""
    sub = eval_df[eval_df["pred_rank"] <= 3].copy()
    sub["pred_rank"] = sub["pred_rank"].astype(int)
    pivoted = sub.pivot_table(
        index=RACE_KEY, columns="pred_rank", values="car_no", aggfunc="first",
    )
    pivoted.columns = [f"pick{c}" for c in pivoted.columns]
    pivoted = pivoted.reset_index()

    # top-1 の pred_calib / ev_avg_calib を載せる
    top1 = eval_df[eval_df["pred_rank"] == 1][
        RACE_KEY + ["pred_calib", "ev_avg_calib"]
    ].drop_duplicates(subset=RACE_KEY)
    pivoted = pivoted.merge(top1, on=RACE_KEY, how="left")
    return pivoted


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


def evaluate_3point(picks: pd.DataFrame, payouts: dict[str, pd.DataFrame]) -> dict:
    """各レースで 複勝/3連単/3連複 を 100 円ずつ買った時の合算 ROI。"""
    if picks.empty:
        return None

    # 複勝: pick1 == car_no_1
    fns = payouts["fns"].rename(columns={"car_no_1": "pick1", "refund": "fns_payout"})
    df = picks.merge(fns, on=RACE_KEY + ["pick1"], how="left")
    df["fns_payout"] = df["fns_payout"].fillna(0)
    df["fns_hit"] = (df["fns_payout"] > 0).astype(int)

    # 3連単: pick1 → pick2 → pick3
    rt3 = payouts["rt3"].rename(columns={
        "car_no_1": "pick1", "car_no_2": "pick2", "car_no_3": "pick3",
        "refund": "rt3_payout",
    })
    df = df.merge(rt3, on=RACE_KEY + ["pick1", "pick2", "pick3"], how="left")
    df["rt3_payout"] = df["rt3_payout"].fillna(0)
    df["rt3_hit"] = (df["rt3_payout"] > 0).astype(int)

    # 3連複: {pick1, pick2, pick3} (順不同)
    rf3 = payouts["rf3"].copy()
    sorted_set = pd.DataFrame(
        np.sort(rf3[["car_no_1", "car_no_2", "car_no_3"]].values, axis=1),
        index=rf3.index, columns=["a", "b", "c"],
    )
    rf3["car_set"] = list(zip(sorted_set["a"], sorted_set["b"], sorted_set["c"]))
    rf3 = rf3[RACE_KEY + ["car_set", "refund"]].rename(columns={"refund": "rf3_payout"})
    rf3 = rf3.groupby(RACE_KEY + ["car_set"], as_index=False)["rf3_payout"].sum()

    pick_set = pd.DataFrame(
        np.sort(picks[["pick1", "pick2", "pick3"]].values.astype(float), axis=1),
        index=picks.index, columns=["a", "b", "c"],
    )
    df["car_set"] = [
        (a, b, c) for a, b, c in zip(pick_set["a"], pick_set["b"], pick_set["c"])
        if not (np.isnan(a) or np.isnan(b) or np.isnan(c))
    ] if len(picks) > 0 else []
    # 上の代入は NaN 行が混じると行数ずれるので、別 merge で
    pick_set_int = pd.DataFrame(
        np.sort(picks[["pick1", "pick2", "pick3"]].fillna(0).astype(int).values, axis=1),
        index=picks.index, columns=["a", "b", "c"],
    )
    df = df.drop(columns=["car_set"], errors="ignore")
    df["car_set"] = list(zip(pick_set_int["a"], pick_set_int["b"], pick_set_int["c"]))
    df = df.merge(rf3, on=RACE_KEY + ["car_set"], how="left")
    df["rf3_payout"] = df["rf3_payout"].fillna(0)
    df["rf3_hit"] = (df["rf3_payout"] > 0).astype(int)

    n = len(df)
    fns_payout = df["fns_payout"].sum()
    rt3_payout = df["rt3_payout"].sum()
    rf3_payout = df["rf3_payout"].sum()

    return {
        "n_races": int(n),
        "stake_total": int(n * 3 * BET),  # 3点 × 100 yen
        "fns": {
            "stake": n * BET, "payout": float(fns_payout),
            "hit": int(df["fns_hit"].sum()), "hit_rate": float(df["fns_hit"].mean()),
            "roi": float(fns_payout / (n * BET)) if n else 0,
        },
        "rt3": {
            "stake": n * BET, "payout": float(rt3_payout),
            "hit": int(df["rt3_hit"].sum()), "hit_rate": float(df["rt3_hit"].mean()),
            "roi": float(rt3_payout / (n * BET)) if n else 0,
        },
        "rf3": {
            "stake": n * BET, "payout": float(rf3_payout),
            "hit": int(df["rf3_hit"].sum()), "hit_rate": float(df["rf3_hit"].mean()),
            "roi": float(rf3_payout / (n * BET)) if n else 0,
        },
        "combined_roi": float(
            (fns_payout + rt3_payout + rf3_payout) / (n * 3 * BET)
        ) if n else 0,
        "combined_payout": float(fns_payout + rt3_payout + rf3_payout),
        "combined_profit": float(fns_payout + rt3_payout + rf3_payout - n * 3 * BET),
    }


def main():
    eval_df = load_eval_set()
    print(f"Eval set: {len(eval_df):,} (race, car) rows")

    picks = get_top3_per_race(eval_df)
    print(f"Races with top-3 ranks: {len(picks):,}")

    payouts = load_payouts()

    rows = []
    for thr in [0.0, 1.00, 1.10, 1.20, 1.30, 1.45, 1.50, 1.80, 2.00]:
        sub = picks[picks["ev_avg_calib"] >= thr].dropna(subset=["pick1", "pick2", "pick3"])
        if len(sub) == 0:
            continue
        r = evaluate_3point(sub, payouts)
        if r is None:
            continue
        rows.append({
            "thr": thr,
            "n_races": r["n_races"],
            "fns_roi": r["fns"]["roi"] * 100,
            "fns_hit": r["fns"]["hit_rate"] * 100,
            "rt3_roi": r["rt3"]["roi"] * 100,
            "rt3_hit": r["rt3"]["hit_rate"] * 100,
            "rf3_roi": r["rf3"]["roi"] * 100,
            "rf3_hit": r["rf3"]["hit_rate"] * 100,
            "combined_roi": r["combined_roi"] * 100,
            "stake_total": r["stake_total"],
            "combined_profit": r["combined_profit"],
        })
    df = pd.DataFrame(rows)

    print()
    print("=== 3点BUY 戦略 ROI(複勝 + 3連単 + 3連複)===")
    show = df.copy()
    for c in ["fns_roi", "fns_hit", "rt3_roi", "rt3_hit", "rf3_roi", "rf3_hit", "combined_roi"]:
        show[c] = show[c].round(2)
    show["stake_total"] = show["stake_total"].apply(lambda v: f"¥{int(v):,}")
    show["combined_profit"] = show["combined_profit"].apply(lambda v: f"¥{int(v):+,}")
    print(show.to_string(index=False))

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_3point_buy_{today}.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# 3点BUY 戦略検証(boat-racing-ai 移植)({today})",
        "",
        "**戦略**: 複勝 top-1 の `ev_avg_calib >= thr` で「歪みあり race」を選別し、",
        "選別 race で 複勝・3連単・3連複 を 100 円ずつ(計 300 円)同時購入。",
        "",
        "## 結果",
        "",
        df.assign(
            fns_roi=lambda d: d["fns_roi"].round(2).astype(str) + "%",
            rt3_roi=lambda d: d["rt3_roi"].round(2).astype(str) + "%",
            rf3_roi=lambda d: d["rf3_roi"].round(2).astype(str) + "%",
            combined_roi=lambda d: d["combined_roi"].round(2).astype(str) + "%",
            fns_hit=lambda d: d["fns_hit"].round(1).astype(str) + "%",
            rt3_hit=lambda d: d["rt3_hit"].round(1).astype(str) + "%",
            rf3_hit=lambda d: d["rf3_hit"].round(1).astype(str) + "%",
            stake_total=lambda d: d["stake_total"].apply(lambda v: f"¥{int(v):,}"),
            combined_profit=lambda d: d["combined_profit"].apply(lambda v: f"¥{int(v):+,}"),
        ).to_markdown(index=False),
        "",
        "## 観察",
        "",
        "- 複勝のみ(既存 ev_avg_calib 閾値運用)との比較で、3連単・3連複 を",
        "  乗っければ ROI 向上するか?",
        "- boat-racing-ai では「3連単 EV合致のみ ROI 188.9%、3連複 ROI 114.3%」と報告。",
        "- auto-racing-ai でも同水準で再現するなら、Phase A メールに 3連単/3連複 候補を",
        "  追加する価値あり。逆に再現しないなら、競技構造の差(8 車 vs 6 艇)が要因。",
    ]
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
