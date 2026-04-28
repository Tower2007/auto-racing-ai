"""EV-based 戦略の徹底検証

ev_selection.py で得た「ROI > 100%」が本物か、複数の角度から audit する。
チェック項目:
  A. Walk-forward leakage 検証
     - parquet の test_month と race_date の整合
     - test month 予測は当月以前のデータで作られているか
  B. オッズ pre-race vs post-race(API 仕様確認)
  C. 「ev_min が保守的すぎ → ROI > 100% になっただけ」効果の分離
     - hit 時の実払戻 / place_odds_min の比率
     - ev_avg ベース・実払戻ベースの真の ROI
  D. pred のキャリブレーション(pred ビン → 実 hit_rate)
  E. ランダム選択(n マッチング)との比較
  F. 直近 2025-2026 のみでの再現確認(完全 out-of-sample)
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100


def load() -> pd.DataFrame:
    preds = pd.read_parquet(DATA / "walkforward_predictions_top3.parquet")
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
    df["is_top1"] = (df["pred_rank"] == 1).astype(int)
    df["ev_min"] = df["pred"] * df["place_odds_min"]
    df["ev_max"] = df["pred"] * df["place_odds_max"]
    df["ev_avg"] = df["pred"] * (df["place_odds_min"] + df["place_odds_max"]) / 2
    df["realized_odds"] = df["payout"] / BET  # = 実 fns 倍率(0 or 1.0+)
    df["year"] = df["race_date"].dt.year
    df["year_month"] = df["race_date"].dt.to_period("M").astype(str)
    return df.dropna(subset=["place_odds_min"])


# =============== A. Walk-forward 整合性チェック ===============

def check_walkforward(df: pd.DataFrame) -> dict:
    """test_month と race_date が同月に属しているか。"""
    df = df.copy()
    df["race_ym"] = df["race_date"].dt.to_period("M").astype(str)
    mismatch = (df["race_ym"] != df["test_month"]).sum()
    return {
        "total_predictions": int(len(df)),
        "race_month != test_month": int(mismatch),
        "test_month の最古": df["test_month"].min(),
        "test_month の最新": df["test_month"].max(),
    }


# =============== C. EV_min の保守性検証 ===============

def check_payout_realization(df: pd.DataFrame) -> pd.DataFrame:
    """hit 時に実際に得た payout が place_odds_min の何倍か。"""
    hit = df[df["hit"] == 1].copy()
    hit["realized_div_min"] = hit["realized_odds"] / hit["place_odds_min"]
    hit["realized_div_avg"] = hit["realized_odds"] / ((hit["place_odds_min"] + hit["place_odds_max"]) / 2)
    hit["realized_div_max"] = hit["realized_odds"] / hit["place_odds_max"]

    # ビン分け
    hit["min_bin"] = pd.cut(
        hit["place_odds_min"],
        bins=[0, 1.0, 1.2, 1.5, 2.0, 3.0, 5.0, 100.0],
        right=False,
    )
    g = hit.groupby("min_bin", observed=True).agg(
        n=("realized_odds", "size"),
        mean_realized=("realized_odds", "mean"),
        mean_min=("place_odds_min", "mean"),
        mean_avg=(("place_odds_max"), lambda s: ((hit.loc[s.index, "place_odds_min"] + s) / 2).mean()),
        mean_max=("place_odds_max", "mean"),
    ).reset_index()
    g["realized_vs_min"] = g["mean_realized"] / g["mean_min"]
    g["realized_vs_avg"] = g["mean_realized"] / g["mean_avg"]
    return g


def check_true_ev_strategy(df: pd.DataFrame) -> pd.DataFrame:
    """EV_avg ベースの戦略 ROI(payout 中央値ベースの真の EV)。"""
    rows = []
    for thr in [1.00, 1.05, 1.10, 1.20]:
        for top1, label in [(True, "top1"), (False, "all")]:
            sub = df[df["ev_avg"] >= thr]
            if top1:
                sub = sub[sub["is_top1"] == 1]
            if len(sub) == 0:
                continue
            cost = len(sub) * BET
            payout = sub["payout"].sum()
            rows.append({
                "ev_metric": "avg",
                "thr": thr,
                "scope": label,
                "n_bets": len(sub),
                "hit_rate": sub["hit"].mean(),
                "roi": payout / cost,
            })
    return pd.DataFrame(rows)


# =============== D. pred キャリブレーション ===============

def check_pred_calibration(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["pred_bin"] = pd.cut(
        df["pred"],
        bins=[0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.01],
        right=False,
    )
    g = df.groupby("pred_bin", observed=True).agg(
        n=("hit", "size"),
        n_hits=("hit", "sum"),
        pred_mean=("pred", "mean"),
        hit_rate=("hit", "mean"),
    ).reset_index()
    g["pred_vs_hit_diff"] = g["pred_mean"] - g["hit_rate"]
    return g


# =============== E. ランダム選択との比較 ===============

def random_baseline(df: pd.DataFrame, n_target: int, seed: int = 42) -> dict:
    """同じ n を全 bet からランダム抽出した時の ROI(複数試行で分布)。"""
    rng = np.random.default_rng(seed)
    bootstrap = []
    for _ in range(20):
        idx = rng.choice(len(df), size=n_target, replace=False)
        sub = df.iloc[idx]
        cost = len(sub) * BET
        bootstrap.append(sub["payout"].sum() / cost)
    return {
        "n_target": n_target,
        "boot_mean": float(np.mean(bootstrap)),
        "boot_std": float(np.std(bootstrap)),
        "boot_min": float(np.min(bootstrap)),
        "boot_max": float(np.max(bootstrap)),
    }


# =============== F. 直近 out-of-sample 再現 ===============

def recent_only_check(df: pd.DataFrame) -> dict:
    """2025-10 以降だけで EV_min ≥ 1.0 の ROI を計算(完全 out-of-sample 風)。"""
    sub = df[df["race_date"] >= "2025-10-01"]
    rows = []
    for thr in [1.00, 1.05, 1.10, 1.20]:
        for top1, label in [(True, "top1"), (False, "all")]:
            ss = sub[sub["ev_min"] >= thr]
            if top1:
                ss = ss[ss["is_top1"] == 1]
            cost = len(ss) * BET
            payout = ss["payout"].sum()
            rows.append({
                "thr": thr,
                "scope": label,
                "n_bets": len(ss),
                "roi": payout / cost if cost else 0,
            })
    return pd.DataFrame(rows)


# =============== Main ===============

def main():
    df = load()
    print(f"Loaded {len(df):,} (race, car) rows")

    # A
    a = check_walkforward(df)
    print("\n=== A. Walk-forward 整合性 ===")
    for k, v in a.items():
        print(f"  {k}: {v}")

    # C
    c1 = check_payout_realization(df)
    print("\n=== C. hit 時の実払戻 vs 推定値 ===")
    print(c1.to_string(index=False))

    c2 = check_true_ev_strategy(df)
    print("\n=== C. EV_avg ベース戦略の ROI(真の EV) ===")
    print(c2.to_string(index=False))

    # D
    d = check_pred_calibration(df)
    print("\n=== D. pred キャリブレーション(predicted vs actual hit_rate) ===")
    print(d.to_string(index=False))

    # E
    print("\n=== E. ランダム選択ベースライン(同じ n) ===")
    sub_top1_1 = df[(df["is_top1"] == 1) & (df["ev_min"] >= 1.0)]
    e_top1 = random_baseline(df, n_target=len(sub_top1_1))
    e_top1["actual_roi"] = float(sub_top1_1["payout"].sum() / (len(sub_top1_1) * BET))
    print(f"  top1, ev_min≥1.0 (n={len(sub_top1_1):,}):")
    for k, v in e_top1.items():
        print(f"    {k}: {v:.4f}" if isinstance(v, float) else f"    {k}: {v}")

    # F
    print("\n=== F. 2025-10 以降のみ(直近 out-of-sample) ===")
    f = recent_only_check(df)
    print(f.to_string(index=False))

    # MD レポート
    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_audit_{today}.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# EV-based 戦略の徹底検証 ({today})",
        "",
        "## A. Walk-forward 整合性",
        "",
        f"- 予測総数: {a['total_predictions']:,}",
        f"- race_date と test_month が同月でない件数: **{a['race_month != test_month']}**",
        f"- test_month 範囲: {a['test_month の最古']} 〜 {a['test_month の最新']}",
        "",
        "→ ミスマッチが 0 なら、各予測は当月以前のデータで訓練されたモデルから出力されている(walk-forward が正しい)。",
        "",
        "## C-1. hit 時の実払戻 vs place_odds_min",
        "",
        "**core 検証**: place_odds_min は「最低保証払戻」。実 hit 時の payout が min の何倍かで、"
        "「ev_min ≥ 1.0 で ROI > 100% になったのは保守的閾値のため」かを判定。",
        "",
        c1.assign(
            mean_realized=lambda d: d["mean_realized"].round(3),
            mean_min=lambda d: d["mean_min"].round(3),
            mean_avg=lambda d: d["mean_avg"].round(3),
            mean_max=lambda d: d["mean_max"].round(3),
            realized_vs_min=lambda d: d["realized_vs_min"].round(3),
            realized_vs_avg=lambda d: d["realized_vs_avg"].round(3),
        ).to_markdown(index=False),
        "",
        "## C-2. EV_avg ベース戦略の ROI",
        "",
        "place_odds の平均値ベース(min/max の中央)で EV を計算した時の ROI。",
        "もし真の edge がなければ ROI ≒ 100% になるはず。",
        "",
        c2.assign(
            hit_rate=lambda d: (d["hit_rate"] * 100).round(2).astype(str) + "%",
            roi=lambda d: (d["roi"] * 100).round(2).astype(str) + "%",
            n_bets=lambda d: d["n_bets"].apply(lambda v: f"{int(v):,}"),
        ).to_markdown(index=False),
        "",
        "## D. pred キャリブレーション",
        "",
        "predicted P(top3) と実 hit_rate の対応。乖離があれば overfitting / leakage の疑い。",
        "",
        d.assign(
            pred_mean=lambda d_: d_["pred_mean"].round(4),
            hit_rate=lambda d_: d_["hit_rate"].round(4),
            pred_vs_hit_diff=lambda d_: d_["pred_vs_hit_diff"].round(4),
        ).to_markdown(index=False),
        "",
        "## E. ランダム選択ベースライン",
        "",
        f"- top1, ev_min≥1.0 と同じ n={e_top1['n_target']:,} をランダム抽出した 20 試行の ROI:",
        f"  - 平均: {e_top1['boot_mean']*100:.2f}%",
        f"  - 範囲: {e_top1['boot_min']*100:.2f}% 〜 {e_top1['boot_max']*100:.2f}%",
        f"- 実際の戦略 ROI: **{e_top1['actual_roi']*100:.2f}%**",
        f"- 差(戦略 - ランダム平均): **+{(e_top1['actual_roi'] - e_top1['boot_mean'])*100:.2f}pt**",
        "",
        "→ 差が大きいほど真のシグナル、小さければ偶然の選択効果。",
        "",
        "## F. 直近 2025-10 以降のみ(完全 out-of-sample 風)",
        "",
        f.assign(
            roi=lambda d: (d["roi"] * 100).round(2).astype(str) + "%",
            n_bets=lambda d: d["n_bets"].apply(lambda v: f"{int(v):,}"),
        ).to_markdown(index=False),
        "",
        "→ 直近期間でも ROI > 100% が維持されていれば再現性高い。"
        "極端に下がっていれば過去の周期効果やデータ rot の可能性。",
    ]
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
