"""期待値ベースの選別 (EV-based selection) で複勝 ROI を改善する試み

walk-forward 予測 + odds_summary から各 (race, car) の期待値を計算:
  EV = pred(top3) × place_odds × 100  (= 100 円ベットあたりの期待回収額)

EV > 100 のベットだけ買えば理論上 ROI > 100% になるはず。
実データで検証する。

評価戦略:
  S-EV-min-top1: top-1 pick の EV(=pred × place_odds_min)が 100 超なら買う
  S-EV-avg-top1: top-1 pick の EV(=pred × place_odds 中央値)が 100 超なら買う
  S-EV-min-all : 全 car で EV>100 のものを全部買う
  S-EV-avg-all : 全 car で EV(中央値ベース)>100 のものを全部買う

EV 閾値スイープも実施(1.00 / 1.05 / 1.10 / 1.20)。
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


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    preds = pd.read_parquet(DATA / "walkforward_predictions_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])

    # join: predictions × odds (place_odds_min/max)
    df = preds.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]],
        on=RACE_KEY + ["car_no"], how="left",
    )
    # 期待値計算
    df["ev_min"] = df["pred"] * df["place_odds_min"]
    df["ev_max"] = df["pred"] * df["place_odds_max"]
    df["ev_avg"] = df["pred"] * (df["place_odds_min"] + df["place_odds_max"]) / 2

    # top-1 フラグ
    df["pred_rank"] = df.groupby(RACE_KEY)["pred"].rank(method="min", ascending=False)
    df["is_top1"] = (df["pred_rank"] == 1).astype(int)
    return df, odds


def load_payouts() -> pd.DataFrame:
    df = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    df["race_date"] = pd.to_datetime(df["race_date"])
    fns = df[df["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()
    return fns


def evaluate(picks: pd.DataFrame, fns: pd.DataFrame, label: str) -> dict:
    if len(picks) == 0:
        return {"strategy": label, "n_bets": 0, "hit_rate": 0, "roi": 0,
                "cost": 0, "payout": 0, "profit": 0}
    m = picks.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "payout"}),
        on=RACE_KEY + ["car_no"], how="left",
    )
    m["payout"] = m["payout"].fillna(0)
    m["hit"] = (m["payout"] > 0).astype(int)
    cost = len(m) * BET
    payout = m["payout"].sum()
    return {
        "strategy": label,
        "n_bets": int(len(m)),
        "n_hits": int(m["hit"].sum()),
        "hit_rate": float(m["hit"].mean()),
        "cost": int(cost),
        "payout": float(payout),
        "profit": float(payout - cost),
        "roi": float(payout / cost),
    }


def yearly_breakdown(picks: pd.DataFrame, fns: pd.DataFrame) -> pd.DataFrame:
    if len(picks) == 0:
        return pd.DataFrame()
    m = picks.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "payout"}),
        on=RACE_KEY + ["car_no"], how="left",
    )
    m["payout"] = m["payout"].fillna(0)
    m["hit"] = (m["payout"] > 0).astype(int)
    m["year"] = m["race_date"].dt.year
    g = m.groupby("year").agg(
        n_bets=("hit", "size"),
        n_hits=("hit", "sum"),
        payout=("payout", "sum"),
    ).reset_index()
    g["cost"] = g["n_bets"] * BET
    g["profit"] = g["payout"] - g["cost"]
    g["roi"] = g["payout"] / g["cost"]
    g["hit_rate"] = g["n_hits"] / g["n_bets"]
    # All
    total_cost = g["cost"].sum()
    total = pd.DataFrame([{
        "year": "All",
        "n_bets": g["n_bets"].sum(),
        "n_hits": g["n_hits"].sum(),
        "payout": g["payout"].sum(),
        "cost": total_cost,
        "profit": g["payout"].sum() - total_cost,
        "roi": (g["payout"].sum() / total_cost) if total_cost else 0,
        "hit_rate": (g["n_hits"].sum() / g["n_bets"].sum()) if g["n_bets"].sum() else 0,
    }])
    return pd.concat([g, total], ignore_index=True)


def fmt_yen(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(int(v)):,}"


def main():
    df, _ = load_data()
    fns = load_payouts()

    # 欠車 (place_odds_min が NaN) は自然に除外される
    df = df.dropna(subset=["place_odds_min", "place_odds_max"])
    print(f"Total (race, car) with odds: {len(df):,}")
    print(f"  EV_min mean: {df['ev_min'].mean():.4f}")
    print(f"  EV_min > 1.00: {(df['ev_min'] > 1.0).sum():,}  ({(df['ev_min'] > 1.0).mean()*100:.2f}%)")
    print(f"  EV_min > 1.05: {(df['ev_min'] > 1.05).sum():,}  ({(df['ev_min'] > 1.05).mean()*100:.2f}%)")
    print(f"  EV_avg > 1.00: {(df['ev_avg'] > 1.0).sum():,}  ({(df['ev_avg'] > 1.0).mean()*100:.2f}%)")

    # === EV 閾値別 主要戦略 ===
    summaries = []
    thresholds = [1.00, 1.02, 1.05, 1.10, 1.15, 1.20, 1.30, 1.50]

    # S-EV-min-top1
    for thr in thresholds:
        picks = df[(df["is_top1"] == 1) & (df["ev_min"] >= thr)]
        r = evaluate(picks, fns, f"top1, ev_min≥{thr:.2f}")
        r["thr"] = thr
        r["variant"] = "top1_min"
        summaries.append(r)

    # S-EV-avg-top1
    for thr in thresholds:
        picks = df[(df["is_top1"] == 1) & (df["ev_avg"] >= thr)]
        r = evaluate(picks, fns, f"top1, ev_avg≥{thr:.2f}")
        r["thr"] = thr
        r["variant"] = "top1_avg"
        summaries.append(r)

    # S-EV-min-all
    for thr in thresholds:
        picks = df[df["ev_min"] >= thr]
        r = evaluate(picks, fns, f"all_cars, ev_min≥{thr:.2f}")
        r["thr"] = thr
        r["variant"] = "all_min"
        summaries.append(r)

    # S-EV-avg-all
    for thr in thresholds:
        picks = df[df["ev_avg"] >= thr]
        r = evaluate(picks, fns, f"all_cars, ev_avg≥{thr:.2f}")
        r["thr"] = thr
        r["variant"] = "all_avg"
        summaries.append(r)

    s_df = pd.DataFrame(summaries)
    s_df["roi_pct"] = (s_df["roi"] * 100).round(2)

    # ピボット: variant × thr
    pivot_roi = s_df.pivot(index="thr", columns="variant", values="roi_pct")
    pivot_n = s_df.pivot(index="thr", columns="variant", values="n_bets")

    # 最良戦略を年度別に展開
    best_idx = s_df["roi"].idxmax()
    best = s_df.loc[best_idx]
    print(f"\n=== Best strategy ===")
    print(f"  {best['strategy']}: ROI={best['roi']*100:.2f}% (n={best['n_bets']:,})")

    # 最良戦略の picks を再作成して年度別に
    if best["variant"] == "top1_min":
        best_picks = df[(df["is_top1"] == 1) & (df["ev_min"] >= best["thr"])]
    elif best["variant"] == "top1_avg":
        best_picks = df[(df["is_top1"] == 1) & (df["ev_avg"] >= best["thr"])]
    elif best["variant"] == "all_min":
        best_picks = df[df["ev_min"] >= best["thr"]]
    else:
        best_picks = df[df["ev_avg"] >= best["thr"]]
    best_yearly = yearly_breakdown(best_picks, fns)

    # ベースライン S3 (pred>=0.94, top1) との比較も再計算
    s3_picks = df[(df["is_top1"] == 1) & (df["pred"] >= 0.94)]
    s3_yearly = yearly_breakdown(s3_picks, fns)
    s3_summary = evaluate(s3_picks, fns, "S3 baseline (pred>=0.94, top1)")

    # MD レポート
    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_selection_{today}.md"
    REPORTS.mkdir(exist_ok=True)

    md = [
        f"# 期待値ベース選別 (EV-based) 検証 ({today})",
        "",
        f"対象: walk-forward 49ヶ月 × {len(df):,} (race, car) 行(欠車除く)",
        "",
        "## 1. 期待値分布(参考)",
        "",
        f"- EV_min(pred × place_odds_min)平均: {df['ev_min'].mean():.4f}",
        f"- EV_min > 1.00 件数: {(df['ev_min'] > 1.0).sum():,} ({(df['ev_min'] > 1.0).mean()*100:.2f}%)",
        f"- EV_avg > 1.00 件数: {(df['ev_avg'] > 1.0).sum():,} ({(df['ev_avg'] > 1.0).mean()*100:.2f}%)",
        "",
        "## 2. 戦略 × 閾値 マトリクス",
        "",
        "### ROI(%)",
        "",
        pivot_roi.reset_index().rename(columns={"thr": "EV閾値"}).to_markdown(index=False),
        "",
        "### ベット数",
        "",
        pivot_n.reset_index().rename(columns={"thr": "EV閾値"}).to_markdown(index=False),
        "",
        "## 3. 最良戦略 詳細",
        "",
        f"**{best['strategy']}**: ROI = **{best['roi']*100:.2f}%**",
        f"- n_bets={best['n_bets']:,}, hit_rate={best['hit_rate']*100:.1f}%",
        f"- 投資 ¥{int(best['cost']):,} / 回収 ¥{int(best['payout']):,} / 損益 {fmt_yen(best['profit'])}",
        "",
        "### 年度別 P&L",
        "",
    ]
    if not best_yearly.empty:
        best_yearly_disp = best_yearly.copy()
        best_yearly_disp["コスト"] = best_yearly_disp["cost"].apply(fmt_yen)
        best_yearly_disp["回収"] = best_yearly_disp["payout"].apply(fmt_yen)
        best_yearly_disp["損益"] = best_yearly_disp["profit"].apply(fmt_yen)
        best_yearly_disp["ROI"] = (best_yearly_disp["roi"] * 100).round(2).astype(str) + "%"
        best_yearly_disp["命中率"] = (best_yearly_disp["hit_rate"] * 100).round(1).astype(str) + "%"
        best_yearly_disp["ベット数"] = best_yearly_disp["n_bets"].apply(lambda v: f"{int(v):,}")
        md.append(best_yearly_disp[["year", "ベット数", "命中率", "コスト", "回収", "損益", "ROI"]]
                  .to_markdown(index=False))
        md.append("")

    md += [
        "## 4. ベースライン比較(S3: 複勝 top-1 + pred≥0.94)",
        "",
        f"S3 全期間: ROI {s3_summary['roi']*100:.2f}% ({s3_summary['n_bets']:,} ベット, "
        f"損益 {fmt_yen(s3_summary['profit'])})",
        "",
    ]
    if not s3_yearly.empty:
        s3_disp = s3_yearly.copy()
        s3_disp["ROI"] = (s3_disp["roi"] * 100).round(2).astype(str) + "%"
        s3_disp["損益"] = s3_disp["profit"].apply(fmt_yen)
        s3_disp["ベット数"] = s3_disp["n_bets"].apply(lambda v: f"{int(v):,}")
        md.append(s3_disp[["year", "ベット数", "ROI", "損益"]].to_markdown(index=False))
        md.append("")

    md += [
        "## 5. 解釈",
        "",
        "- EV_min ≥ X は「最低保証払戻」ベースの保守的判定(分母が固定 1.0× の本命を弾く)",
        "- EV_avg ≥ X は中央値ベース",
        "- all_cars 系は同レースで複数車購入することがあり、機会数を稼ぐ",
        "- top1 系は 1 レース 1 ベット、機会少なく分散小",
    ]
    out.write_text("\n".join(md), encoding="utf-8")

    # コンソール簡易表示
    print(f"\n=== ROI matrix (variant × threshold) ===")
    print(pivot_roi.to_string())
    print(f"\n=== n_bets matrix ===")
    print(pivot_n.to_string())
    print(f"\nReport saved: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
