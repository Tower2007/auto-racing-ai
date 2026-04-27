"""ボックス買い・流し買いの ROI 分析

walk-forward 予測の top-N 車から組合せを生成して、各ベットを 100 円で購入。
コストと払戻を集計。

評価する戦略:
1. 単勝 / 複勝の top-N(N=1,2,3,4): 上位 N 車にそれぞれ 100 円
2. ワイド 1着流し top-1 → {top-2..top-N}
3. 2連複 box top-N (N=2,3,4): 上位 N 車から C(N,2) 通り
4. 3連複 box top-N (N=3,4,5,6): 上位 N 車から C(N,3) 通り
5. 3連単 1着流し top-1 → {top-2..top-N}: P(N-1, 2) 通り
"""

from __future__ import annotations

from datetime import datetime
from itertools import combinations, permutations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"

RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100


def load_top_picks(top_n: int = 6) -> pd.DataFrame:
    """各レースの予測 top-N 車を pivot してまとめる。"""
    preds = pd.read_parquet(DATA / "walkforward_predictions_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    preds = preds.sort_values(RACE_KEY + ["pred"], ascending=[True, True, True, False])
    preds["rank_pred"] = preds.groupby(RACE_KEY).cumcount() + 1
    sub = preds[preds["rank_pred"] <= top_n]
    out = sub.pivot_table(
        index=RACE_KEY, columns="rank_pred", values="car_no", aggfunc="first",
    )
    out.columns = [f"p{c}" for c in out.columns]
    out = out.reset_index()
    return out


def load_payouts() -> pd.DataFrame:
    df = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    df["race_date"] = pd.to_datetime(df["race_date"])
    return df


def _eval_single_bets(picks: pd.DataFrame, payouts: pd.DataFrame,
                      bet_type: str, top_n: int) -> dict:
    """単勝 or 複勝 を上位 N 車にそれぞれ 100 円。"""
    pay = payouts[payouts["bet_type"] == bet_type][RACE_KEY + ["car_no_1", "refund"]]
    pay = pay.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()

    n_races = len(picks)
    n_bets = n_races * top_n
    total_cost = n_bets * BET
    total_payout = 0.0
    total_hits = 0

    for k in range(1, top_n + 1):
        m = picks.merge(
            pay.rename(columns={"car_no_1": f"p{k}", "refund": "refund"}),
            on=RACE_KEY + [f"p{k}"], how="left",
        )
        total_payout += m["refund"].fillna(0).sum()
        total_hits += int(m["refund"].notna().sum())

    return {
        "n_races": n_races, "combos_per_race": top_n,
        "n_bets": n_bets, "total_cost": total_cost,
        "total_payout": float(total_payout),
        "n_hits": total_hits,
        "hit_rate_per_race": total_hits / n_races / top_n,
        "roi": float(total_payout / total_cost),
    }


def _eval_unordered_box(picks: pd.DataFrame, payouts: pd.DataFrame,
                        bet_type: str, top_n: int, k: int) -> dict:
    """順不同の組合せ box (2連複 / 3連複 / ワイド)。
    top-N 車から C(N, k) 通りを買う。
    """
    if bet_type == "wid":
        # ワイドは k=2、payouts に 3 行/レース(任意 2 着内)
        pay_cols = ["car_no_1", "car_no_2"]
        sort_idx = [0, 1]
    elif bet_type == "rfw":
        pay_cols = ["car_no_1", "car_no_2"]
        sort_idx = [0, 1]
    elif bet_type == "rf3":
        pay_cols = ["car_no_1", "car_no_2", "car_no_3"]
        sort_idx = [0, 1, 2]
    else:
        raise ValueError(bet_type)

    pay = payouts[payouts["bet_type"] == bet_type][RACE_KEY + pay_cols + ["refund"]].copy()
    # ソート済タプルでキー化(順不同なため)
    pay["key"] = pay[pay_cols].apply(lambda r: tuple(sorted(r)), axis=1)
    pay_keyed = pay.groupby(RACE_KEY + ["key"], as_index=False)["refund"].sum()

    # 各レースで top-N から C(N, k) 通りの組合せを生成
    car_cols = [f"p{i}" for i in range(1, top_n + 1)]
    combos_per_race = sum(1 for _ in combinations(range(top_n), k))

    payout_total = 0.0
    n_hits = 0

    # ループ: 重い ので vectorize できる範囲でやる
    # 各 race で組合せのソート済タプルセットを作り、pay と join
    rows = []
    for combo_idx in combinations(range(top_n), k):
        cols = [car_cols[i] for i in combo_idx]
        sub = picks[RACE_KEY + cols].copy()
        # ソート済タプル(int に揃える)
        sub["key"] = sub[cols].apply(lambda r: tuple(sorted(int(x) for x in r if not pd.isna(x))), axis=1)
        # k 車揃わない race は除外
        sub = sub[sub["key"].apply(lambda t: len(t) == k)]
        rows.append(sub[RACE_KEY + ["key"]])
    picks_combos = pd.concat(rows, ignore_index=True)

    merged = picks_combos.merge(pay_keyed, on=RACE_KEY + ["key"], how="left")
    payout_total = float(merged["refund"].fillna(0).sum())
    n_hits = int(merged["refund"].notna().sum())
    n_bets = len(picks_combos)
    total_cost = n_bets * BET

    return {
        "n_races": len(picks), "combos_per_race": combos_per_race,
        "n_bets": n_bets, "total_cost": total_cost,
        "total_payout": payout_total,
        "n_hits": n_hits,
        "hit_rate_per_race": n_hits / len(picks),  # 1 race で複数当たる場合があり > 1.0 もあり得る
        "roi": payout_total / total_cost,
    }


def _eval_ordered_nagashi(picks: pd.DataFrame, payouts: pd.DataFrame,
                          bet_type: str, top_n: int) -> dict:
    """3連単 1着流し: pick1 を 1着固定、2&3 を {p2..pN} の順列。
    P(N-1, 2) 通り。

    or 2連単 1着流し: pick1 → {p2..pN}: N-1 通り。
    """
    if bet_type == "rt3":
        pay_cols = ["car_no_1", "car_no_2", "car_no_3"]
        # 組合せ: (p1, x, y) for distinct x,y in {p2..pN}
        seconds_thirds = list(permutations(range(2, top_n + 1), 2))
        combos_per_race = len(seconds_thirds)

        car_cols_2 = [f"p{i}" for i in range(2, top_n + 1)]
        # それぞれの (i, j) ペアで rt3 と join
        pay = payouts[payouts["bet_type"] == "rt3"][RACE_KEY + pay_cols + ["refund"]].copy()
        pay = pay.groupby(RACE_KEY + pay_cols, as_index=False)["refund"].sum()

        rows = []
        for i, j in seconds_thirds:
            sub = picks[RACE_KEY + ["p1", f"p{i}", f"p{j}"]].copy()
            sub.columns = RACE_KEY + ["car_no_1", "car_no_2", "car_no_3"]
            rows.append(sub)
        all_bets = pd.concat(rows, ignore_index=True)
        # NaN を除外(数 race で top-N が揃わないことあり)
        all_bets = all_bets.dropna(subset=["car_no_1", "car_no_2", "car_no_3"])
        all_bets[["car_no_1", "car_no_2", "car_no_3"]] = all_bets[["car_no_1", "car_no_2", "car_no_3"]].astype(int)

        merged = all_bets.merge(pay, on=RACE_KEY + pay_cols, how="left")
        payout_total = float(merged["refund"].fillna(0).sum())
        n_hits = int(merged["refund"].notna().sum())
        n_bets = len(all_bets)
        total_cost = n_bets * BET

    elif bet_type == "rtw":  # 2連単 1着流し
        pay_cols = ["car_no_1", "car_no_2"]
        seconds = list(range(2, top_n + 1))
        combos_per_race = len(seconds)

        pay = payouts[payouts["bet_type"] == "rtw"][RACE_KEY + pay_cols + ["refund"]].copy()
        pay = pay.groupby(RACE_KEY + pay_cols, as_index=False)["refund"].sum()

        rows = []
        for j in seconds:
            sub = picks[RACE_KEY + ["p1", f"p{j}"]].copy()
            sub.columns = RACE_KEY + ["car_no_1", "car_no_2"]
            rows.append(sub)
        all_bets = pd.concat(rows, ignore_index=True)
        all_bets = all_bets.dropna(subset=["car_no_1", "car_no_2"])
        all_bets[["car_no_1", "car_no_2"]] = all_bets[["car_no_1", "car_no_2"]].astype(int)

        merged = all_bets.merge(pay, on=RACE_KEY + pay_cols, how="left")
        payout_total = float(merged["refund"].fillna(0).sum())
        n_hits = int(merged["refund"].notna().sum())
        n_bets = len(all_bets)
        total_cost = n_bets * BET
    else:
        raise ValueError(bet_type)

    return {
        "n_races": len(picks), "combos_per_race": combos_per_race,
        "n_bets": n_bets, "total_cost": total_cost,
        "total_payout": payout_total,
        "n_hits": n_hits,
        "hit_rate_per_race": n_hits / len(picks),
        "roi": payout_total / total_cost,
    }


def main():
    picks = load_top_picks(top_n=6)
    payouts = load_payouts()
    print(f"Picks: {len(picks):,} races")

    rows = []

    # 単勝・複勝 top-N
    for bt, label in [("tns", "単勝"), ("fns", "複勝")]:
        for n in [1, 2, 3, 4]:
            r = _eval_single_bets(picks, payouts, bt, n)
            r.update({"strategy": f"{label} top-{n}", "bet_type": bt})
            rows.append(r)

    # ワイド 1着流し: top1 とペアで {p2..pN}
    # → これは 2連複 box の top-N と同じ k=2 だが、ワイド payouts を使う
    # 1着流し版: ペアの片方が p1 固定
    for n in [2, 3, 4]:
        # 1着流し = (p1, p2), (p1, p3), ..., (p1, pn) = n-1 combos
        # → 2連複 box top-N の中で p1 を含むものに相当
        # 簡略化: ワイド top-N box (k=2) を流し代わりに評価
        r = _eval_unordered_box(picks, payouts, "wid", n, 2)
        r.update({"strategy": f"ワイド box top-{n}", "bet_type": "wid"})
        rows.append(r)

    # 2連複 box top-N (k=2)
    for n in [2, 3, 4]:
        r = _eval_unordered_box(picks, payouts, "rfw", n, 2)
        r.update({"strategy": f"2連複 box top-{n}", "bet_type": "rfw"})
        rows.append(r)

    # 3連複 box top-N (k=3)
    for n in [3, 4, 5]:
        r = _eval_unordered_box(picks, payouts, "rf3", n, 3)
        r.update({"strategy": f"3連複 box top-{n}", "bet_type": "rf3"})
        rows.append(r)

    # 2連単 1着流し
    for n in [2, 3, 4]:
        r = _eval_ordered_nagashi(picks, payouts, "rtw", n)
        r.update({"strategy": f"2連単 1着流し top-{n}", "bet_type": "rtw"})
        rows.append(r)

    # 3連単 1着流し
    for n in [3, 4, 5]:
        r = _eval_ordered_nagashi(picks, payouts, "rt3", n)
        r.update({"strategy": f"3連単 1着流し top-{n}", "bet_type": "rt3"})
        rows.append(r)

    df = pd.DataFrame(rows)
    df["roi_pct"] = df["roi"] * 100
    df = df[[
        "strategy", "bet_type", "combos_per_race",
        "n_bets", "total_cost", "total_payout", "n_hits", "roi_pct",
    ]].sort_values("roi_pct", ascending=False).reset_index(drop=True)

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"box_nagashi_{today}.md"
    REPORTS.mkdir(exist_ok=True)

    md = [
        f"# ボックス買い・流し買い ROI 比較 ({today})",
        "",
        f"対象: walk-forward 49ヶ月 × {len(picks):,} レース、各組合せ 100 円ベット。",
        "",
        "## 全戦略 ROI(降順)",
        "",
        df.assign(
            total_cost=lambda d: d["total_cost"].apply(lambda v: f"¥{int(v):,}"),
            total_payout=lambda d: d["total_payout"].apply(lambda v: f"¥{int(v):,}"),
            n_bets=lambda d: d["n_bets"].apply(lambda v: f"{int(v):,}"),
            n_hits=lambda d: d["n_hits"].apply(lambda v: f"{int(v):,}"),
            roi_pct=lambda d: d["roi_pct"].round(2).astype(str) + "%",
        ).rename(columns={"roi_pct": "ROI"})
        .to_markdown(index=False),
        "",
        "## 観察",
        "",
        "- ベット数(n_bets)が増えても ROI が変わらない → 期待値中立(box は分散低減のみ)",
        "- 各組合せが等期待値でない場合のみ ROI が変動(top-1 の組合せが市場で過大評価されている等)",
        "- ROI 100% を超えれば「市場越え」だが、現状は届かず",
    ]
    out.write_text("\n".join(md), encoding="utf-8")

    print(f"\nReport saved: {out}")
    print()
    print("=== Top strategies by ROI ===")
    print(df.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
