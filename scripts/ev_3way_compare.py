"""3 モデル EV-based ROI 比較

直前(オッズ・試走あり) vs 中間(オッズあり・試走なし) vs 前日(両方なし)
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100
CALIB_CUTOFF = "2024-04"


def evaluate(name: str, parquet: str, odds: pd.DataFrame, fns: pd.DataFrame) -> dict:
    df = pd.read_parquet(DATA / parquet)
    df["race_date"] = pd.to_datetime(df["race_date"])
    df = df.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]],
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
    df = df.dropna(subset=["place_odds_min"])

    calib = df[df["test_month"] < CALIB_CUTOFF]
    eval_set = df[df["test_month"] >= CALIB_CUTOFF].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    eval_set["pred_calib"] = iso.transform(eval_set["pred"].values)
    eval_set["ev_avg_calib"] = eval_set["pred_calib"] * (
        eval_set["place_odds_min"] + eval_set["place_odds_max"]
    ) / 2

    auc = roc_auc_score(eval_set["target_top3"], eval_set["pred_calib"])

    rows = []
    for thr in [1.00, 1.10, 1.20, 1.30, 1.45, 1.50, 1.80, 2.00]:
        sub = eval_set[(eval_set["is_top1"] == 1) & (eval_set["ev_avg_calib"] >= thr)]
        if len(sub) == 0:
            rows.append({"thr": thr, "n_bets": 0, "hit_rate": 0, "roi": 0, "profit": 0})
            continue
        cost = len(sub) * BET
        payout = sub["payout"].sum()
        rows.append({
            "thr": thr,
            "n_bets": int(len(sub)),
            "hit_rate": float(sub["hit"].mean()),
            "roi": float(payout / cost),
            "profit": float(payout - cost),
        })
    return {"name": name, "auc": auc, "summary": pd.DataFrame(rows)}


def main():
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()

    models = [
        ("直前", "walkforward_predictions_top3.parquet"),
        ("中間", "walkforward_predictions_morning_top3.parquet"),
        ("前日", "walkforward_predictions_preday_top3.parquet"),
    ]
    results = [evaluate(name, parq, odds, fns) for name, parq in models]

    print("=== AUC (calibrated, eval set 25mo) ===")
    for r in results:
        print(f"  {r['name']}: {r['auc']:.4f}")
    print()
    for r in results:
        print(f"=== {r['name']}モデル top1 + ev_avg_calib ===")
        print(r["summary"].assign(
            roi=lambda d: (d["roi"] * 100).round(2),
            hit_rate=lambda d: (d["hit_rate"] * 100).round(1),
        ).to_string(index=False))
        print()

    # 一覧表(ROI)
    pivot = pd.DataFrame({
        "thr": results[0]["summary"]["thr"],
    })
    for r in results:
        pivot[f"{r['name']}_ROI%"] = (r["summary"]["roi"] * 100).round(2).values
    for r in results:
        pivot[f"{r['name']}_n"] = r["summary"]["n_bets"].values
    for r in results:
        pivot[f"{r['name']}_利益"] = r["summary"]["profit"].astype(int).values

    print("=== サマリ ===")
    print(pivot.to_string(index=False))

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_3way_compare_{today}.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# 3 モデル EV 戦略比較 ({today})",
        "",
        f"対象: walk-forward 25ヶ月 eval (2024-04 〜 2026-04)",
        "",
        "## AUC(calibrated)",
        "",
        "| モデル | 試走 | オッズ | AUC |",
        "|---|:---:|:---:|---:|",
    ]
    md.append(f"| 直前 | ✓ | ✓ | {results[0]['auc']:.4f} |")
    md.append(f"| 中間 | ✕ | ✓ | {results[1]['auc']:.4f} |")
    md.append(f"| 前日 | ✕ | ✕ | {results[2]['auc']:.4f} |")
    md.append("")

    md.append("## ROI / ベット数 / 利益(top-1 + ev_avg_calib)")
    md.append("")
    md.append(pivot.to_markdown(index=False))
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
