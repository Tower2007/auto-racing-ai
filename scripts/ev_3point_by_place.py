"""3点BUY 戦略の場別 (place_code) 探索

ev_3point_buy.py と同じ eval set を使い、場 × thr のマトリクスで
複勝/3連単/3連複/合算 ROI を比較。
特定の場で edge が偏っているか(または山陽除外で全体結果が改善されるか)を確認。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ev_3point_buy import load_eval_set, get_top3_per_race, load_payouts, evaluate_3point  # noqa: E402

PLACE_NAMES = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
THRESHOLDS = [0.0, 1.30, 1.45, 1.50, 1.80, 2.00]


def evaluate_by_place(picks: pd.DataFrame, payouts: dict, thr: float) -> pd.DataFrame:
    rows = []
    sub_all = picks[picks["ev_avg_calib"] >= thr].dropna(subset=["pick1", "pick2", "pick3"])
    for place_code, name in PLACE_NAMES.items():
        sub = sub_all[sub_all["place_code"] == place_code]
        if len(sub) == 0:
            continue
        r = evaluate_3point(sub, payouts)
        if r is None:
            continue
        rows.append({
            "place_code": place_code,
            "place": name,
            "n_races": r["n_races"],
            "fns_roi": r["fns"]["roi"] * 100,
            "fns_hit": r["fns"]["hit_rate"] * 100,
            "rt3_roi": r["rt3"]["roi"] * 100,
            "rt3_hit": r["rt3"]["hit_rate"] * 100,
            "rf3_roi": r["rf3"]["roi"] * 100,
            "rf3_hit": r["rf3"]["hit_rate"] * 100,
            "combined_roi": r["combined_roi"] * 100,
            "combined_profit": r["combined_profit"],
        })
    # 全場合算
    if len(sub_all) > 0:
        r = evaluate_3point(sub_all, payouts)
        if r is not None:
            rows.append({
                "place_code": 0,
                "place": "全場",
                "n_races": r["n_races"],
                "fns_roi": r["fns"]["roi"] * 100,
                "fns_hit": r["fns"]["hit_rate"] * 100,
                "rt3_roi": r["rt3"]["roi"] * 100,
                "rt3_hit": r["rt3"]["hit_rate"] * 100,
                "rf3_roi": r["rf3"]["roi"] * 100,
                "rf3_hit": r["rf3"]["hit_rate"] * 100,
                "combined_roi": r["combined_roi"] * 100,
                "combined_profit": r["combined_profit"],
            })
    return pd.DataFrame(rows)


def render_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "(候補なし)\n"
    lines = []
    lines.append(f"{'場':<6} {'race':>5} "
                 f"{'fns_roi':>8} {'fns_hit':>8} "
                 f"{'rt3_roi':>8} {'rt3_hit':>8} "
                 f"{'rf3_roi':>8} {'rf3_hit':>8} "
                 f"{'comb_roi':>9} {'profit':>11}")
    lines.append("-" * 90)
    for _, r in df.iterrows():
        lines.append(
            f"{r['place']:<6} {int(r['n_races']):>5} "
            f"{r['fns_roi']:>7.1f}% {r['fns_hit']:>7.1f}% "
            f"{r['rt3_roi']:>7.1f}% {r['rt3_hit']:>7.1f}% "
            f"{r['rf3_roi']:>7.1f}% {r['rf3_hit']:>7.1f}% "
            f"{r['combined_roi']:>8.1f}% ¥{int(r['combined_profit']):>+9,}"
        )
    return "\n".join(lines)


def main():
    eval_df = load_eval_set()
    picks = get_top3_per_race(eval_df)
    payouts = load_payouts()
    print(f"Eval set picks: {len(picks):,}")
    print()

    all_results: dict[float, pd.DataFrame] = {}
    for thr in THRESHOLDS:
        df = evaluate_by_place(picks, payouts, thr)
        all_results[thr] = df
        print(f"=== thr={thr:.2f} ===")
        print(render_table(df))
        print()

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_3point_by_place_{today}.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# 3点BUY 戦略 場別探索 ({today})",
        "",
        "**戦略**: 複勝 top-1 の `ev_avg_calib >= thr` で選別 → 複勝/3連単/3連複 を 100 円ずつ。",
        "**eval 期間**: walk-forward predictions の後半半分(動的 half-split)。",
        "",
    ]
    for thr in THRESHOLDS:
        df = all_results[thr]
        md.append(f"## thr={thr:.2f}")
        md.append("")
        md.append(
            f"| 場 | races | 複勝ROI | 複勝的中 | 3単ROI | 3単的中 | 3複ROI | 3複的中 | 合算ROI | 合算利益 |"
        )
        md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for _, r in df.iterrows():
            md.append(
                f"| {r['place']} | {int(r['n_races'])} | "
                f"{r['fns_roi']:.1f}% | {r['fns_hit']:.1f}% | "
                f"{r['rt3_roi']:.1f}% | {r['rt3_hit']:.1f}% | "
                f"{r['rf3_roi']:.1f}% | {r['rf3_hit']:.1f}% | "
                f"{r['combined_roi']:.1f}% | ¥{int(r['combined_profit']):+,} |"
            )
        md.append("")
    md.append("## 観察")
    md.append("")
    md.append("- 場ごとに combined_roi が大きく違うか確認(同一 thr で偏りがあれば場フィルタの価値あり)。")
    md.append("- 山陽(6) を除外することで全場合算 ROI が改善される thr 帯を特定。")
    md.append("- レース数が極端に少ない (<30) 場は ROI 信頼区間が広いので参考程度に扱う。")
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"Report: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
