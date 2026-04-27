"""券種別 ROI 比較

walk-forward 予測の top-N 車を組合せにして、各券種を 1 点買いした場合の ROI を計算。

戦略(各レース 100 円ベット):
- 単勝 (tns): top-1 予測
- 複勝 (fns): top-1 予測
- 2連単 (rtw): top-1 → top-2 (順序あり)
- 2連複 (rfw): {top-1, top-2}
- ワイド (wid): {top-1, top-2}
- 3連単 (rt3): top-1 → top-2 → top-3
- 3連複 (rf3): {top-1, top-2, top-3}

信頼度閾値別にも見る。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"

RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100


def load_top3_picks() -> pd.DataFrame:
    preds = pd.read_parquet(DATA / "walkforward_predictions_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    preds = preds.sort_values(RACE_KEY + ["pred"], ascending=[True, True, True, False])
    preds["rank_pred"] = preds.groupby(RACE_KEY).cumcount() + 1

    # 各レースの top-1..3 を pivot
    sub = preds[preds["rank_pred"] <= 3]
    out = sub.pivot_table(
        index=RACE_KEY, columns="rank_pred",
        values=["car_no", "pred"], aggfunc="first",
    )
    out.columns = [f"{a}{b}" for a, b in out.columns]
    out = out.reset_index()
    # car_no1, car_no2, car_no3, pred1, pred2, pred3 が揃う
    out = out.rename(columns={"car_no1": "pick1", "car_no2": "pick2", "car_no3": "pick3"})
    return out


def load_payouts() -> pd.DataFrame:
    df = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    df["race_date"] = pd.to_datetime(df["race_date"])
    return df


def evaluate_bet(picks: pd.DataFrame, payouts: pd.DataFrame, bet_type: str, label: str) -> dict:
    """各レースで 1 点買い、bet_type ごとに hit/payout を集計。"""
    pay = payouts[payouts["bet_type"] == bet_type].copy()

    if bet_type == "tns":
        # pick1 == car_no_1
        df = picks.merge(
            pay[RACE_KEY + ["car_no_1", "refund"]].groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum(),
            left_on=RACE_KEY + ["pick1"], right_on=RACE_KEY + ["car_no_1"], how="left",
        )
        df["hit"] = df["refund"].notna()
        df["payout"] = df["refund"].fillna(0)

    elif bet_type == "fns":
        df = picks.merge(
            pay[RACE_KEY + ["car_no_1", "refund"]].groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum(),
            left_on=RACE_KEY + ["pick1"], right_on=RACE_KEY + ["car_no_1"], how="left",
        )
        df["hit"] = df["refund"].notna()
        df["payout"] = df["refund"].fillna(0)

    elif bet_type == "rtw":  # 2連単 順序あり: pick1=1st, pick2=2nd
        m = picks.merge(
            pay[RACE_KEY + ["car_no_1", "car_no_2", "refund"]],
            on=RACE_KEY, how="left",
        )
        # pick1 == car_no_1 AND pick2 == car_no_2
        m["match"] = (m["pick1"] == m["car_no_1"]) & (m["pick2"] == m["car_no_2"])
        df = m.groupby(RACE_KEY + ["pick1", "pick2"], as_index=False).apply(
            lambda g: pd.Series({
                "hit": bool(g["match"].any()),
                "payout": g.loc[g["match"], "refund"].sum() if g["match"].any() else 0.0,
            })
        ).reset_index(drop=True)
        # 空白回でも残すために picks と再結合
        df = picks.merge(df, on=RACE_KEY + ["pick1", "pick2"], how="left")
        df["hit"] = df["hit"].fillna(False).astype(bool)
        df["payout"] = df["payout"].fillna(0)

    elif bet_type == "rfw":  # 2連複: {pick1, pick2}
        m = picks.merge(
            pay[RACE_KEY + ["car_no_1", "car_no_2", "refund"]],
            on=RACE_KEY, how="left",
        )
        # 順不同 一致
        s_pick = pd.DataFrame({"a": m[["pick1", "pick2"]].min(axis=1), "b": m[["pick1", "pick2"]].max(axis=1)})
        s_pay = pd.DataFrame({"a": m[["car_no_1", "car_no_2"]].min(axis=1), "b": m[["car_no_1", "car_no_2"]].max(axis=1)})
        m["match"] = (s_pick["a"] == s_pay["a"]) & (s_pick["b"] == s_pay["b"])
        df = picks.merge(
            m.groupby(RACE_KEY + ["pick1", "pick2"], as_index=False).apply(
                lambda g: pd.Series({
                    "hit": bool(g["match"].any()),
                    "payout": g.loc[g["match"], "refund"].sum() if g["match"].any() else 0.0,
                })
            ).reset_index(drop=True),
            on=RACE_KEY + ["pick1", "pick2"], how="left",
        )
        df["hit"] = df["hit"].fillna(False).astype(bool)
        df["payout"] = df["payout"].fillna(0)

    elif bet_type == "wid":  # ワイド: {pick1, pick2} が 3着以内 2 車
        m = picks.merge(
            pay[RACE_KEY + ["car_no_1", "car_no_2", "refund"]],
            on=RACE_KEY, how="left",
        )
        s_pick = pd.DataFrame({"a": m[["pick1", "pick2"]].min(axis=1), "b": m[["pick1", "pick2"]].max(axis=1)})
        s_pay = pd.DataFrame({"a": m[["car_no_1", "car_no_2"]].min(axis=1), "b": m[["car_no_1", "car_no_2"]].max(axis=1)})
        m["match"] = (s_pick["a"] == s_pay["a"]) & (s_pick["b"] == s_pay["b"])
        df = picks.merge(
            m.groupby(RACE_KEY + ["pick1", "pick2"], as_index=False).apply(
                lambda g: pd.Series({
                    "hit": bool(g["match"].any()),
                    "payout": g.loc[g["match"], "refund"].sum() if g["match"].any() else 0.0,
                })
            ).reset_index(drop=True),
            on=RACE_KEY + ["pick1", "pick2"], how="left",
        )
        df["hit"] = df["hit"].fillna(False).astype(bool)
        df["payout"] = df["payout"].fillna(0)

    elif bet_type == "rt3":  # 3連単 順序あり
        m = picks.merge(
            pay[RACE_KEY + ["car_no_1", "car_no_2", "car_no_3", "refund"]],
            on=RACE_KEY, how="left",
        )
        m["match"] = (m["pick1"] == m["car_no_1"]) & (m["pick2"] == m["car_no_2"]) & (m["pick3"] == m["car_no_3"])
        df = picks.merge(
            m.groupby(RACE_KEY + ["pick1", "pick2", "pick3"], as_index=False).apply(
                lambda g: pd.Series({
                    "hit": bool(g["match"].any()),
                    "payout": g.loc[g["match"], "refund"].sum() if g["match"].any() else 0.0,
                })
            ).reset_index(drop=True),
            on=RACE_KEY + ["pick1", "pick2", "pick3"], how="left",
        )
        df["hit"] = df["hit"].fillna(False).astype(bool)
        df["payout"] = df["payout"].fillna(0)

    elif bet_type == "rf3":  # 3連複 順不同
        m = picks.merge(
            pay[RACE_KEY + ["car_no_1", "car_no_2", "car_no_3", "refund"]],
            on=RACE_KEY, how="left",
        )
        # ソート済 set 比較
        sp = pd.DataFrame(np.sort(m[["pick1", "pick2", "pick3"]].values, axis=1), index=m.index, columns=["a", "b", "c"])
        sa = pd.DataFrame(np.sort(m[["car_no_1", "car_no_2", "car_no_3"]].values, axis=1), index=m.index, columns=["a", "b", "c"])
        m["match"] = (sp["a"] == sa["a"]) & (sp["b"] == sa["b"]) & (sp["c"] == sa["c"])
        df = picks.merge(
            m.groupby(RACE_KEY + ["pick1", "pick2", "pick3"], as_index=False).apply(
                lambda g: pd.Series({
                    "hit": bool(g["match"].any()),
                    "payout": g.loc[g["match"], "refund"].sum() if g["match"].any() else 0.0,
                })
            ).reset_index(drop=True),
            on=RACE_KEY + ["pick1", "pick2", "pick3"], how="left",
        )
        df["hit"] = df["hit"].fillna(False).astype(bool)
        df["payout"] = df["payout"].fillna(0)
    else:
        raise ValueError(bet_type)

    n = len(df)
    return {
        "bet_type": bet_type,
        "label": label,
        "n_bets": n,
        "hit_rate": float(df["hit"].mean()),
        "n_hits": int(df["hit"].sum()),
        "total_cost": n * BET,
        "total_payout": float(df["payout"].sum()),
        "roi": float(df["payout"].sum() / (n * BET)),
        "max_payout": float(df["payout"].max()),
    }, df


def main():
    picks = load_top3_picks()
    payouts = load_payouts()

    BET_TYPES = [
        ("tns", "単勝"),
        ("fns", "複勝"),
        ("rtw", "2連単"),
        ("rfw", "2連複"),
        ("wid", "ワイド"),
        ("rt3", "3連単"),
        ("rf3", "3連複"),
    ]

    # 全レース
    print(f"Picks: {len(picks):,}")
    rows_all = []
    detail_dfs = {}
    for bt, label in BET_TYPES:
        m, df = evaluate_bet(picks, payouts, bt, label)
        rows_all.append(m)
        detail_dfs[bt] = df
        print(f"  {label} ({bt}): n={m['n_bets']:,} hit={m['hit_rate']*100:.2f}% ROI={m['roi']*100:.2f}% max_payout={m['max_payout']:,.0f}")

    df_all = pd.DataFrame(rows_all)

    # 信頼度閾値別 ROI(top-1 信頼度を使う)
    print("\n=== 信頼度閾値別 (pred1) ===")
    threshold_rows = []
    for thr in [0.0, 0.7, 0.8, 0.9, 0.94]:
        sub = picks[picks["pred1"] >= thr]
        if len(sub) < 100:
            continue
        for bt, label in BET_TYPES:
            m, _ = evaluate_bet(sub, payouts, bt, label)
            m["pred1_thr"] = thr
            threshold_rows.append(m)
    df_thr = pd.DataFrame(threshold_rows)

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"bet_type_analysis_{today}.md"
    REPORTS.mkdir(exist_ok=True)

    md = [
        f"# 券種別 ROI 比較 ({today})",
        "",
        f"対象: walk-forward 49ヶ月 × {len(picks):,} レース。",
        "戦略: 各レースで予測 top-N 車の 1 点買い (100 円)、券種ごとに集計。",
        "",
        "## 全レース 全券種 ROI",
        "",
        df_all.assign(
            hit_rate=lambda d: (d["hit_rate"] * 100).round(2).astype(str) + "%",
            roi=lambda d: (d["roi"] * 100).round(2).astype(str) + "%",
            total_cost=lambda d: d["total_cost"].apply(lambda v: f"¥{int(v):,}"),
            total_payout=lambda d: d["total_payout"].apply(lambda v: f"¥{int(v):,}"),
            max_payout=lambda d: d["max_payout"].apply(lambda v: f"¥{int(v):,}"),
        )[["bet_type", "label", "n_bets", "hit_rate", "n_hits", "total_cost", "total_payout", "roi", "max_payout"]]
        .to_markdown(index=False),
        "",
        "## 信頼度 (pred1) 閾値別 × 券種 ROI",
        "",
    ]

    pivot = df_thr.pivot_table(index="pred1_thr", columns="label", values="roi") * 100
    pivot = pivot.round(2).astype(str) + "%"
    pivot = pivot.reset_index()
    md += [
        pivot.to_markdown(index=False),
        "",
        "## 注記",
        "",
        "- 各券種で「予測 top-N 車を 1 点買い」のシンプル戦略",
        "- 流し買い・ボックス買いは未評価(コスト計算が複雑、別途分析要)",
        "- 高配当券種 (3連単 等) はサンプルサイズが小さいときの分散が大きい",
    ]
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport saved: {out}")


if __name__ == "__main__":
    main()
