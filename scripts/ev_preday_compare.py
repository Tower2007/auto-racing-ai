"""前日モデル(オッズ・試走なし) vs 直前モデル の EV-based ROI 比較

両モデルの予測 parquet に同じ EV 計算 + キャリブレーションを適用、
threshold スイープで ROI を比べる。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score, log_loss

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100
CALIB_CUTOFF = "2024-04"


def load_predictions(name: str) -> pd.DataFrame:
    df = pd.read_parquet(DATA / f"walkforward_predictions_{name}_top3.parquet")
    df["race_date"] = pd.to_datetime(df["race_date"])
    return df


def load_odds_payouts() -> tuple[pd.DataFrame, pd.DataFrame]:
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()
    return odds, fns


def evaluate(preds: pd.DataFrame, odds: pd.DataFrame, fns: pd.DataFrame, label: str) -> dict:
    df = preds.merge(
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

    # キャリブレーション(前半 24mo fit、後半 25mo eval)
    calib = df[df["test_month"] < CALIB_CUTOFF]
    eval_set = df[df["test_month"] >= CALIB_CUTOFF].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    eval_set["pred_calib"] = iso.transform(eval_set["pred"].values)
    eval_set["ev_avg_calib"] = eval_set["pred_calib"] * (
        eval_set["place_odds_min"] + eval_set["place_odds_max"]
    ) / 2

    # 全体 AUC
    auc_all = roc_auc_score(eval_set["target_top3"], eval_set["pred"])
    auc_calib = roc_auc_score(eval_set["target_top3"], eval_set["pred_calib"])

    # 閾値別 ROI
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
            "cost": int(cost),
            "payout": float(payout),
        })

    summary = pd.DataFrame(rows)
    summary["model"] = label
    return {
        "label": label,
        "auc_eval": auc_all,
        "auc_calib": auc_calib,
        "n_eval_rows": len(eval_set),
        "summary": summary,
    }


def main():
    odds, fns = load_odds_payouts()

    # 直前モデル(既存): file は walkforward_predictions_top3.parquet
    full_preds = pd.read_parquet(DATA / "walkforward_predictions_top3.parquet")
    full_preds["race_date"] = pd.to_datetime(full_preds["race_date"])
    res_full = evaluate(full_preds, odds, fns, "直前モデル(オッズ・試走あり)")

    # 前日モデル: walkforward_predictions_preday_top3.parquet
    preday_preds = load_predictions("preday")
    res_preday = evaluate(preday_preds, odds, fns, "前日モデル(オッズ・試走なし)")

    print(f"=== AUC 比較(eval set 25ヶ月) ===")
    print(f"  直前: raw={res_full['auc_eval']:.4f}, calib={res_full['auc_calib']:.4f}")
    print(f"  前日: raw={res_preday['auc_eval']:.4f}, calib={res_preday['auc_calib']:.4f}")
    print(f"  AUC 差: {res_full['auc_eval'] - res_preday['auc_eval']:.4f}")

    print()
    print("=== 直前モデル: top1 + ev_avg_calib スイープ ===")
    print(res_full["summary"].assign(
        roi_pct=lambda d: (d["roi"] * 100).round(2),
        hit_pct=lambda d: (d["hit_rate"] * 100).round(1),
    )[["thr", "n_bets", "hit_pct", "roi_pct", "profit"]].to_string(index=False))

    print()
    print("=== 前日モデル: top1 + ev_avg_calib スイープ ===")
    print(res_preday["summary"].assign(
        roi_pct=lambda d: (d["roi"] * 100).round(2),
        hit_pct=lambda d: (d["hit_rate"] * 100).round(1),
    )[["thr", "n_bets", "hit_pct", "roi_pct", "profit"]].to_string(index=False))

    # MD
    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_preday_compare_{today}.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# 前日モデル vs 直前モデル EV 戦略比較 ({today})",
        "",
        "## AUC(25ヶ月 eval set)",
        "",
        f"| モデル | raw AUC | calibrated AUC |",
        f"|---|---:|---:|",
        f"| 直前(オッズ・試走あり)| {res_full['auc_eval']:.4f} | {res_full['auc_calib']:.4f} |",
        f"| 前日(オッズ・試走なし)| {res_preday['auc_eval']:.4f} | {res_preday['auc_calib']:.4f} |",
        f"| **差** | **-{res_full['auc_eval'] - res_preday['auc_eval']:.4f}** | - |",
        "",
        "## ROI 比較(top-1 + ev_avg_calib)",
        "",
        "### 直前モデル",
        "",
        res_full["summary"].assign(
            roi=lambda d: (d["roi"] * 100).round(2).astype(str) + "%",
            hit_rate=lambda d: (d["hit_rate"] * 100).round(1).astype(str) + "%",
            profit=lambda d: d["profit"].apply(lambda v: f"¥{int(v):,}"),
            n_bets=lambda d: d["n_bets"].apply(lambda v: f"{int(v):,}"),
        )[["thr", "n_bets", "hit_rate", "roi", "profit"]].to_markdown(index=False),
        "",
        "### 前日モデル",
        "",
        res_preday["summary"].assign(
            roi=lambda d: (d["roi"] * 100).round(2).astype(str) + "%",
            hit_rate=lambda d: (d["hit_rate"] * 100).round(1).astype(str) + "%",
            profit=lambda d: d["profit"].apply(lambda v: f"¥{int(v):,}"),
            n_bets=lambda d: d["n_bets"].apply(lambda v: f"{int(v):,}"),
        )[["thr", "n_bets", "hit_rate", "roi", "profit"]].to_markdown(index=False),
        "",
        "## 結論",
        "",
        "前日のみで予測してオッズが当日確定した時点で EV ベース選別すれば、",
        "前日でも市場越えできるか?",
    ]
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
