"""3点BUY policy シミュレータ

「場ごとに買う券種を変える policy」を複数定義し、合算 ROI と月次安定性を比較。
ev_3point_buy.py の評価データを再利用。

policy 例:
  - baseline_fns_only: 全場 複勝のみ(現行 Phase A)
  - sanyo_full: 山陽のみ 複勝+3連単+3連複, 他場 複勝
  - sanyo_rf3: 山陽のみ 複勝+3連複, 他場 複勝
  - ex_iizuka: 飯塚 複勝のみ, 他場 複勝+3連単+3連複
  - all_3types: 全場 複勝+3連単+3連複(リファレンス)
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ev_3point_buy import load_eval_set, get_top3_per_race, load_payouts  # noqa: E402

PLACE_NAMES = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
PLACE_CODES = list(PLACE_NAMES.keys())

# 場別 policy 定義(set[bet_type], bet_type ∈ {"fns","rt3","rf3"})
POLICIES: dict[str, dict[int, set[str]]] = {
    "baseline_fns_only": {pc: {"fns"} for pc in PLACE_CODES},
    "sanyo_full": {
        2: {"fns"}, 3: {"fns"}, 4: {"fns"}, 5: {"fns"},
        6: {"fns", "rt3", "rf3"},
    },
    "sanyo_rf3": {
        2: {"fns"}, 3: {"fns"}, 4: {"fns"}, 5: {"fns"},
        6: {"fns", "rf3"},
    },
    "sanyo_rt3": {
        2: {"fns"}, 3: {"fns"}, 4: {"fns"}, 5: {"fns"},
        6: {"fns", "rt3"},
    },
    "ex_iizuka_full": {
        2: {"fns", "rt3", "rf3"}, 3: {"fns", "rt3", "rf3"},
        4: {"fns", "rt3", "rf3"}, 5: {"fns"},
        6: {"fns", "rt3", "rf3"},
    },
    "ex_iizuka_rf3": {
        2: {"fns", "rf3"}, 3: {"fns", "rf3"}, 4: {"fns", "rf3"},
        5: {"fns"}, 6: {"fns", "rf3"},
    },
    "all_rf3_only_extra": {pc: {"fns", "rf3"} for pc in PLACE_CODES},
    "all_3types": {pc: {"fns", "rt3", "rf3"} for pc in PLACE_CODES},
}

THRESHOLDS = [1.30, 1.45, 1.50, 1.80]


def build_picks_with_payouts(
    eval_df: pd.DataFrame, payouts: dict[str, pd.DataFrame]
) -> pd.DataFrame:
    """各 race の top-1〜3 + 各券種の払戻をひとつの DataFrame にまとめる。"""
    picks = get_top3_per_race(eval_df)

    # eval_df から test_month を join(月次評価用)
    months_map = (
        eval_df[RACE_KEY + ["test_month"]]
        .drop_duplicates(subset=RACE_KEY)
    )
    picks = picks.merge(months_map, on=RACE_KEY, how="left")

    picks = picks.dropna(subset=["pick1", "pick2", "pick3"]).copy()
    picks["pick1"] = picks["pick1"].astype(int)
    picks["pick2"] = picks["pick2"].astype(int)
    picks["pick3"] = picks["pick3"].astype(int)

    # 複勝(pick1)
    fns = payouts["fns"].rename(columns={"car_no_1": "pick1", "refund": "fns_payout"})
    picks = picks.merge(fns, on=RACE_KEY + ["pick1"], how="left")
    picks["fns_payout"] = picks["fns_payout"].fillna(0).astype(float)

    # 3連単(pick1->pick2->pick3)
    rt3 = payouts["rt3"].rename(columns={
        "car_no_1": "pick1", "car_no_2": "pick2", "car_no_3": "pick3",
        "refund": "rt3_payout",
    })
    picks = picks.merge(rt3, on=RACE_KEY + ["pick1", "pick2", "pick3"], how="left")
    picks["rt3_payout"] = picks["rt3_payout"].fillna(0).astype(float)

    # 3連複({pick1, pick2, pick3} 順不同)
    rf3 = payouts["rf3"].copy()
    rf3_sorted = pd.DataFrame(
        np.sort(rf3[["car_no_1", "car_no_2", "car_no_3"]].values, axis=1),
        index=rf3.index, columns=["a", "b", "c"],
    )
    rf3["car_set"] = list(zip(rf3_sorted["a"], rf3_sorted["b"], rf3_sorted["c"]))
    rf3 = (
        rf3[RACE_KEY + ["car_set", "refund"]]
        .rename(columns={"refund": "rf3_payout"})
        .groupby(RACE_KEY + ["car_set"], as_index=False)["rf3_payout"].sum()
    )

    pick_sorted = pd.DataFrame(
        np.sort(picks[["pick1", "pick2", "pick3"]].values, axis=1),
        index=picks.index, columns=["a", "b", "c"],
    )
    picks["car_set"] = list(zip(pick_sorted["a"], pick_sorted["b"], pick_sorted["c"]))
    picks = picks.merge(rf3, on=RACE_KEY + ["car_set"], how="left")
    picks["rf3_payout"] = picks["rf3_payout"].fillna(0).astype(float)

    return picks


def evaluate_policy(
    picks: pd.DataFrame, policy: dict[int, set[str]], thr: float
) -> dict:
    """policy に従って stake/payout を集計、合算 ROI + 月次統計を返す。"""
    sub = picks[picks["ev_avg_calib"] >= thr].copy()
    if sub.empty:
        return None

    sub["stake"] = 0
    sub["payout"] = 0.0
    for pc, bet_types in policy.items():
        mask = sub["place_code"] == pc
        if not mask.any():
            continue
        for bt in bet_types:
            sub.loc[mask, "stake"] += BET
            sub.loc[mask, "payout"] += sub.loc[mask, f"{bt}_payout"]

    sub = sub[sub["stake"] > 0]
    if sub.empty:
        return None

    stake = float(sub["stake"].sum())
    payout = float(sub["payout"].sum())
    roi = payout / stake if stake else 0.0
    profit = payout - stake

    monthly = sub.groupby("test_month").agg(
        m_stake=("stake", "sum"),
        m_payout=("payout", "sum"),
    )
    monthly["m_roi"] = monthly["m_payout"] / monthly["m_stake"]
    months_total = int((monthly["m_stake"] > 0).sum())
    months_winning = int((monthly["m_roi"] >= 1.0).sum())
    monthly_min_roi = float(monthly["m_roi"].min())
    monthly_max_roi = float(monthly["m_roi"].max())
    monthly_median_roi = float(monthly["m_roi"].median())

    # 場別内訳(fns/rt3/rf3 個別 ROI も)
    place_breakdown = []
    for pc, name in PLACE_NAMES.items():
        ps = sub[sub["place_code"] == pc]
        if ps.empty:
            continue
        bet_types = sorted(policy.get(pc, set()))
        place_breakdown.append({
            "place": name,
            "bets": "+".join(bet_types) if bet_types else "-",
            "n_races": int(len(ps)),
            "stake": int(ps["stake"].sum()),
            "payout": int(ps["payout"].sum()),
            "roi": float(ps["payout"].sum() / ps["stake"].sum())
                   if ps["stake"].sum() else 0.0,
            "profit": int(ps["payout"].sum() - ps["stake"].sum()),
        })

    return {
        "thr": thr,
        "n_races_selected": int(len(sub)),
        "stake": int(stake),
        "payout": int(payout),
        "roi": roi,
        "profit": int(profit),
        "monthly_total": months_total,
        "monthly_winning": months_winning,
        "monthly_winning_rate": months_winning / months_total if months_total else 0,
        "monthly_median_roi": monthly_median_roi,
        "monthly_min_roi": monthly_min_roi,
        "monthly_max_roi": monthly_max_roi,
        "place_breakdown": place_breakdown,
    }


def render_summary_table(rows: list[dict]) -> str:
    if not rows:
        return "(結果なし)"
    lines = []
    lines.append(
        f"{'policy':<22} {'thr':>5} {'races':>6} {'stake':>9} "
        f"{'roi':>7} {'profit':>10} {'mon_win':>9} {'med_roi':>8}"
    )
    lines.append("-" * 90)
    for r in rows:
        lines.append(
            f"{r['policy']:<22} {r['thr']:>5.2f} {r['n_races_selected']:>6} "
            f"¥{r['stake']:>8,} {r['roi']*100:>6.1f}% ¥{r['profit']:>+9,} "
            f"{r['monthly_winning']:>3}/{r['monthly_total']:<3} "
            f"({r['monthly_winning_rate']*100:>3.0f}%) "
            f"{r['monthly_median_roi']*100:>7.1f}%"
        )
    return "\n".join(lines)


def main():
    eval_df = load_eval_set()
    payouts = load_payouts()
    picks = build_picks_with_payouts(eval_df, payouts)
    print(f"Eval set picks (after dropna+payout-merge): {len(picks):,}")
    print(f"test_month range: {picks['test_month'].min()} - {picks['test_month'].max()}")
    print()

    summary_rows = []
    detailed: list[tuple[str, dict]] = []

    for policy_name, policy in POLICIES.items():
        print(f"=== {policy_name} ===")
        for thr in THRESHOLDS:
            r = evaluate_policy(picks, policy, thr)
            if r is None:
                continue
            r["policy"] = policy_name
            summary_rows.append(r)
            detailed.append((policy_name, r))
            print(
                f"  thr={thr:.2f}  races={r['n_races_selected']:>4}  "
                f"stake=¥{r['stake']:>7,}  ROI={r['roi']*100:>6.1f}%  "
                f"profit=¥{r['profit']:>+9,}  "
                f"monthly={r['monthly_winning']}/{r['monthly_total']} "
                f"({r['monthly_winning_rate']*100:>3.0f}%)  "
                f"med_roi={r['monthly_median_roi']*100:.1f}%"
            )
        print()

    print()
    print("=== Summary (sorted by profit, thr=1.45) ===")
    s = [r for r in summary_rows if abs(r["thr"] - 1.45) < 1e-6]
    s.sort(key=lambda r: r["profit"], reverse=True)
    print(render_summary_table(s))
    print()

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_3point_policy_sim_{today}.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# 3点BUY policy シミュレーション ({today})",
        "",
        "**目的**: 場別に買う券種を変える policy を複数比較し、Phase A 取り込み案を決める。",
        "",
        "**eval 期間**: walk-forward predictions の後半半分。各 race の top-1 EV が",
        "thr 以上の race のみ選別、policy[place] にある bet_type について 100 円ベット。",
        "",
        "## Policy 定義",
        "",
        "| policy | 川口 | 伊勢崎 | 浜松 | 飯塚 | 山陽 |",
        "|---|---|---|---|---|---|",
    ]
    for name, pol in POLICIES.items():
        row = [name]
        for pc in PLACE_CODES:
            row.append("+".join(sorted(pol.get(pc, set()))) or "-")
        md.append("| " + " | ".join(row) + " |")
    md.append("")

    for thr in THRESHOLDS:
        sub = [r for r in summary_rows if abs(r["thr"] - thr) < 1e-6]
        sub.sort(key=lambda r: r["profit"], reverse=True)
        md.append(f"## thr={thr:.2f}")
        md.append("")
        md.append(
            "| policy | races | stake | payout | ROI | profit | "
            "月次≥100% | median月ROI | min月ROI | max月ROI |"
        )
        md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in sub:
            md.append(
                f"| {r['policy']} | {r['n_races_selected']} | "
                f"¥{r['stake']:,} | ¥{r['payout']:,} | "
                f"{r['roi']*100:.1f}% | ¥{r['profit']:+,} | "
                f"{r['monthly_winning']}/{r['monthly_total']} "
                f"({r['monthly_winning_rate']*100:.0f}%) | "
                f"{r['monthly_median_roi']*100:.1f}% | "
                f"{r['monthly_min_roi']*100:.1f}% | "
                f"{r['monthly_max_roi']*100:.1f}% |"
            )
        md.append("")

    # 推奨 policy(thr=1.45 利益最大) の場別内訳
    md.append("## 推奨 policy 候補(thr=1.45 利益降順 上位 3)場別内訳")
    md.append("")
    s = [r for r in summary_rows if abs(r["thr"] - 1.45) < 1e-6]
    s.sort(key=lambda r: r["profit"], reverse=True)
    for r in s[:3]:
        md.append(f"### {r['policy']} (利益 ¥{r['profit']:+,})")
        md.append("")
        md.append("| 場 | 券種 | races | stake | payout | ROI | profit |")
        md.append("|---|---|---:|---:|---:|---:|---:|")
        for pb in r["place_breakdown"]:
            md.append(
                f"| {pb['place']} | {pb['bets']} | {pb['n_races']} | "
                f"¥{pb['stake']:,} | ¥{pb['payout']:,} | "
                f"{pb['roi']*100:.1f}% | ¥{pb['profit']:+,} |"
            )
        md.append("")

    md.append("## 観察ポイント")
    md.append("")
    md.append("- **profit** だけでなく **monthly ≥100% 達成率** と **median 月 ROI** で")
    md.append("  運用安定性を見る。median 80% 未満は外れ値依存の疑い。")
    md.append("- **min 月 ROI** が極端に低い policy は連敗月の振れ幅が大きい。")
    md.append("- 利益最大 ≠ 採用すべき policy(運用負荷と振れ幅のバランス)。")
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"Report: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
