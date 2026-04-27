"""信頼度ベースのセレクション・サイジング分析

walkforward の per-race 予測 (data/walkforward_predictions_top3.parquet) を使い、
予測信頼度 (pred) ごとに ROI がどう変化するかを分析。

確認事項:
1. 高信頼ベットだけ買えば ROI ≥ 1.0 になる閾値はあるか?
2. 「市場との乖離 (edge = pred - implied_prob)」で見ると優位性はあるか?
3. 累積 ROI (top-N 信頼度で買った場合) のカーブ

出力: reports/sizing_analysis_<date>.md
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"


def load_picks_with_payouts() -> pd.DataFrame:
    """各レースの top-1 prediction pick + 単勝/複勝の払戻情報を 1 行にまとめる。"""
    preds = pd.read_parquet(DATA / "walkforward_predictions_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])

    # 各レースで pred 最大の car を pick
    grp_keys = ["race_date", "place_code", "race_no"]
    picks = preds.loc[preds.groupby(grp_keys)["pred"].idxmax()].copy()
    picks = picks[grp_keys + ["car_no", "pred"]].reset_index(drop=True)

    # 払戻 join (同着 dedupe)
    payouts = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    payouts["race_date"] = pd.to_datetime(payouts["race_date"])
    tns = payouts[payouts["bet_type"] == "tns"][grp_keys + ["car_no_1", "refund"]]
    fns = payouts[payouts["bet_type"] == "fns"][grp_keys + ["car_no_1", "refund"]]
    tns = tns.groupby(grp_keys + ["car_no_1"], as_index=False)["refund"].sum()
    fns = fns.groupby(grp_keys + ["car_no_1"], as_index=False)["refund"].sum()

    picks = picks.merge(
        tns.rename(columns={"car_no_1": "car_no", "refund": "win_payout"}),
        on=grp_keys + ["car_no"], how="left",
    )
    picks = picks.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "place_payout"}),
        on=grp_keys + ["car_no"], how="left",
    )
    picks["win_payout"] = picks["win_payout"].fillna(0)
    picks["place_payout"] = picks["place_payout"].fillna(0)
    picks["win_hit"] = (picks["win_payout"] > 0).astype(int)
    picks["place_hit"] = (picks["place_payout"] > 0).astype(int)

    # オッズ情報も付ける(market implied prob 計算用)
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    picks = picks.merge(
        odds[grp_keys + ["car_no", "win_odds", "place_odds_min", "place_odds_max"]],
        on=grp_keys + ["car_no"], how="left",
    )
    return picks


def bucket_by_pred(picks: pd.DataFrame, edges: list[float]) -> pd.DataFrame:
    """pred の閾値で bin を切って ROI を集計。"""
    rows = []
    for lo in edges:
        sub = picks[picks["pred"] >= lo]
        if len(sub) == 0:
            continue
        rows.append({
            "pred>=": lo,
            "n": len(sub),
            "n_pct": len(sub) / len(picks),
            "win_hit_rate": sub["win_hit"].mean(),
            "win_roi": sub["win_payout"].sum() / (len(sub) * 100),
            "place_hit_rate": sub["place_hit"].mean(),
            "place_roi": sub["place_payout"].sum() / (len(sub) * 100),
        })
    return pd.DataFrame(rows)


def bucket_by_decile(picks: pd.DataFrame) -> pd.DataFrame:
    """pred の 10 分位で bin を切る。"""
    picks = picks.copy()
    picks["decile"] = pd.qcut(picks["pred"], 10, labels=False, duplicates="drop")
    rows = []
    for d, sub in picks.groupby("decile"):
        rows.append({
            "decile": int(d),
            "pred_min": sub["pred"].min(),
            "pred_max": sub["pred"].max(),
            "n": len(sub),
            "win_hit_rate": sub["win_hit"].mean(),
            "win_roi": sub["win_payout"].sum() / (len(sub) * 100),
            "place_hit_rate": sub["place_hit"].mean(),
            "place_roi": sub["place_payout"].sum() / (len(sub) * 100),
        })
    return pd.DataFrame(rows).sort_values("decile", ascending=False).reset_index(drop=True)


def edge_analysis(picks: pd.DataFrame) -> pd.DataFrame:
    """市場 implied prob (= 1/win_odds) と pred の乖離 (edge) で見る。

    edge > 0 → モデルが市場より高く評価 = 期待値プラスの可能性
    """
    picks = picks.copy()
    picks["market_p"] = 1.0 / picks["win_odds"]
    picks["edge"] = picks["pred"] - picks["market_p"]
    # market_p は 単勝の implied だが pred は top3 なので比較スケールが違う
    # 代替: market_p_top3 = 1 - (1 - 1/win_odds_each_car) 全車積でもよいが
    #       簡易に「pred - 1/win_odds」のまま使う(モデルのオッズ越え判定として)
    picks_sorted = picks.dropna(subset=["edge"]).sort_values("edge", ascending=False)
    edges = [-1.0, -0.5, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.5]
    rows = []
    for e in edges:
        sub = picks_sorted[picks_sorted["edge"] >= e]
        if len(sub) == 0:
            continue
        rows.append({
            "edge>=": e,
            "n": len(sub),
            "win_hit_rate": sub["win_hit"].mean(),
            "win_roi": sub["win_payout"].sum() / (len(sub) * 100),
            "place_hit_rate": sub["place_hit"].mean(),
            "place_roi": sub["place_payout"].sum() / (len(sub) * 100),
        })
    return pd.DataFrame(rows)


def best_threshold(picks: pd.DataFrame) -> dict:
    """pred の閾値を細かく振って ROI 最大点を探す。"""
    thresholds = np.arange(0.30, 0.95, 0.01)
    rows = []
    for t in thresholds:
        sub = picks[picks["pred"] >= t]
        if len(sub) < 100:  # サンプルサイズが小さすぎると不安定
            continue
        rows.append({
            "thr": float(t),
            "n": len(sub),
            "win_roi": sub["win_payout"].sum() / (len(sub) * 100),
            "place_roi": sub["place_payout"].sum() / (len(sub) * 100),
        })
    df = pd.DataFrame(rows)
    return {
        "best_win_thr": df.loc[df["win_roi"].idxmax(), "thr"],
        "best_win_roi": df["win_roi"].max(),
        "best_win_n": int(df.loc[df["win_roi"].idxmax(), "n"]),
        "best_place_thr": df.loc[df["place_roi"].idxmax(), "thr"],
        "best_place_roi": df["place_roi"].max(),
        "best_place_n": int(df.loc[df["place_roi"].idxmax(), "n"]),
        "scan_df": df,
    }


def fmt(v, w=8):
    return f"{v:.4f}" if isinstance(v, float) else str(v)


def main():
    picks = load_picks_with_payouts()
    print(f"Total picks (1 per race): {len(picks):,}")
    print(f"Date range: {picks['race_date'].min().date()} ~ {picks['race_date'].max().date()}")

    # 1. pred 閾値別
    by_threshold = bucket_by_pred(picks, [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
    # 2. pred 10 分位
    by_decile = bucket_by_decile(picks)
    # 3. edge 分析
    by_edge = edge_analysis(picks)
    # 4. 最適閾値
    best = best_threshold(picks)

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"sizing_analysis_{today}.md"
    REPORTS.mkdir(exist_ok=True)

    md = [
        f"# 信頼度ベース セレクション分析 ({today})",
        "",
        f"対象: walk-forward 49ヶ月 × {len(picks):,} 件の top-1 pick",
        "",
        "## 結論",
        "",
        "### 1. 信頼度(pred)閾値別 ROI",
        "",
        by_threshold.assign(
            n_pct=lambda d: (d["n_pct"] * 100).round(1).astype(str) + "%",
            win_hit_rate=lambda d: (d["win_hit_rate"] * 100).round(1).astype(str) + "%",
            win_roi=lambda d: (d["win_roi"] * 100).round(1).astype(str) + "%",
            place_hit_rate=lambda d: (d["place_hit_rate"] * 100).round(1).astype(str) + "%",
            place_roi=lambda d: (d["place_roi"] * 100).round(1).astype(str) + "%",
        ).to_markdown(index=False),
        "",
        "### 2. 信頼度 10 分位",
        "",
        by_decile.assign(
            pred_min=lambda d: d["pred_min"].round(3),
            pred_max=lambda d: d["pred_max"].round(3),
            win_hit_rate=lambda d: (d["win_hit_rate"] * 100).round(1).astype(str) + "%",
            win_roi=lambda d: (d["win_roi"] * 100).round(1).astype(str) + "%",
            place_hit_rate=lambda d: (d["place_hit_rate"] * 100).round(1).astype(str) + "%",
            place_roi=lambda d: (d["place_roi"] * 100).round(1).astype(str) + "%",
        ).to_markdown(index=False),
        "",
        "### 3. 市場との乖離 (edge = pred - 1/win_odds) 分析",
        "",
        "edge > 0 はモデルが市場より高く評価しているケース(理論的には期待値プラス候補)。",
        "",
        by_edge.assign(
            win_hit_rate=lambda d: (d["win_hit_rate"] * 100).round(1).astype(str) + "%",
            win_roi=lambda d: (d["win_roi"] * 100).round(1).astype(str) + "%",
            place_hit_rate=lambda d: (d["place_hit_rate"] * 100).round(1).astype(str) + "%",
            place_roi=lambda d: (d["place_roi"] * 100).round(1).astype(str) + "%",
        ).to_markdown(index=False),
        "",
        "### 4. 最適 pred 閾値スキャン",
        "",
        f"- 単勝 ROI 最大: pred ≥ **{best['best_win_thr']:.2f}** で ROI = **{best['best_win_roi']*100:.2f}%** (n={best['best_win_n']:,})",
        f"- 複勝 ROI 最大: pred ≥ **{best['best_place_thr']:.2f}** で ROI = **{best['best_place_roi']*100:.2f}%** (n={best['best_place_n']:,})",
        "",
        "閾値スキャン詳細(主要点):",
        "",
        best["scan_df"].iloc[::5].assign(
            win_roi=lambda d: (d["win_roi"] * 100).round(2).astype(str) + "%",
            place_roi=lambda d: (d["place_roi"] * 100).round(2).astype(str) + "%",
        ).to_markdown(index=False),
    ]

    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport saved: {out}")
    print()
    print("=== 信頼度閾値別 ROI ===")
    print(by_threshold.to_string(index=False))
    print()
    print("=== 信頼度 10 分位 ===")
    print(by_decile.to_string(index=False))
    print()
    print(f"Best win threshold: pred>={best['best_win_thr']:.2f}, "
          f"ROI={best['best_win_roi']*100:.2f}%, n={best['best_win_n']:,}")
    print(f"Best place threshold: pred>={best['best_place_thr']:.2f}, "
          f"ROI={best['best_place_roi']*100:.2f}%, n={best['best_place_n']:,}")


if __name__ == "__main__":
    main()
