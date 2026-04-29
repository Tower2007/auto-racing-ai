"""3点BUY policy の上振れ依存度分析

各 policy の月次 P&L を出し、「最良月 N 個を除外した場合に利益がどうなるか」を
比較する。利益の大半が一握りの max 月に依存しているなら、過去再現性が低い。

ev_3point_policy_sim.py の policy 定義と評価関数を再利用。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ev_3point_buy import load_eval_set, load_payouts  # noqa: E402
from ev_3point_policy_sim import (  # noqa: E402
    POLICIES, PLACE_NAMES, build_picks_with_payouts,
)

THR = 1.45  # 主要分析対象。docで利益最大点として推奨される閾値


def monthly_pnl(picks: pd.DataFrame, policy: dict[int, set[str]], thr: float) -> pd.DataFrame:
    """月別 stake/payout/profit を返す。"""
    sub = picks[picks["ev_avg_calib"] >= thr].copy()
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
    monthly = sub.groupby("test_month").agg(
        m_stake=("stake", "sum"),
        m_payout=("payout", "sum"),
    ).reset_index()
    monthly["m_profit"] = monthly["m_payout"] - monthly["m_stake"]
    monthly["m_roi"] = monthly["m_payout"] / monthly["m_stake"]
    monthly = monthly.sort_values("test_month").reset_index(drop=True)
    return monthly


def outlier_analysis(monthly: pd.DataFrame) -> dict:
    """最良月 N 個除外時の利益 / 月次 std / 連敗最長 を計算。"""
    sorted_by_profit = monthly.sort_values("m_profit", ascending=False).reset_index(drop=True)
    total_profit = float(monthly["m_profit"].sum())
    total_stake = float(monthly["m_stake"].sum())
    total_payout = float(monthly["m_payout"].sum())

    drop_results = {}
    for n in [0, 1, 2, 3, 5]:
        if n >= len(sorted_by_profit):
            continue
        kept = sorted_by_profit.iloc[n:]
        s = float(kept["m_stake"].sum())
        p = float(kept["m_payout"].sum())
        pr = float(kept["m_profit"].sum())
        drop_results[f"drop_top{n}"] = {
            "n_months": int(len(kept)),
            "stake": int(s),
            "payout": int(p),
            "profit": int(pr),
            "roi": p / s if s else 0.0,
        }

    # 連敗最長(月単位、m_profit < 0 が連続した最長期間)
    longest_lose_streak = 0
    cur = 0
    for v in monthly["m_profit"].values:
        if v < 0:
            cur += 1
            longest_lose_streak = max(longest_lose_streak, cur)
        else:
            cur = 0
    # 累計ドローダウン(時系列で見た最大ドローダウン金額)
    cum = monthly["m_profit"].cumsum().values
    peak = 0
    max_dd = 0
    for v in cum:
        peak = max(peak, v)
        max_dd = min(max_dd, v - peak)

    return {
        "total_profit": int(total_profit),
        "total_stake": int(total_stake),
        "total_payout": int(total_payout),
        "n_months": int(len(monthly)),
        "monthly_profit_std": float(monthly["m_profit"].std()),
        "monthly_profit_min": float(monthly["m_profit"].min()),
        "monthly_profit_max": float(monthly["m_profit"].max()),
        "longest_lose_streak_months": int(longest_lose_streak),
        "max_drawdown": int(max_dd),
        "drop_results": drop_results,
        "top3_months": sorted_by_profit.head(3).to_dict("records"),
        "worst3_months": sorted_by_profit.tail(3).to_dict("records"),
    }


def main():
    eval_df = load_eval_set()
    payouts = load_payouts()
    picks = build_picks_with_payouts(eval_df, payouts)
    print(f"Eval set picks: {len(picks):,}")
    print(f"Threshold: {THR}")
    print()

    results = {}
    for name, policy in POLICIES.items():
        monthly = monthly_pnl(picks, policy, THR)
        if monthly.empty:
            continue
        results[name] = (monthly, outlier_analysis(monthly))

    # コンソール出力: サマリ
    print("=" * 110)
    print(f"{'policy':<22} {'profit':>10} {'std':>9} {'min月':>9} {'max月':>9} "
          f"{'-top1':>10} {'-top3':>10} {'連敗':>5} {'最大DD':>10}")
    print("-" * 110)
    rows_summary = []
    for name, (monthly, oa) in results.items():
        rows_summary.append({
            "policy": name,
            "profit": oa["total_profit"],
            "std": oa["monthly_profit_std"],
            "min_m": oa["monthly_profit_min"],
            "max_m": oa["monthly_profit_max"],
            "drop1": oa["drop_results"].get("drop_top1", {}).get("profit"),
            "drop3": oa["drop_results"].get("drop_top3", {}).get("profit"),
            "lose_streak": oa["longest_lose_streak_months"],
            "max_dd": oa["max_drawdown"],
        })
        print(
            f"{name:<22} ¥{oa['total_profit']:>+9,} ¥{oa['monthly_profit_std']:>+8,.0f} "
            f"¥{oa['monthly_profit_min']:>+8,.0f} ¥{oa['monthly_profit_max']:>+8,.0f} "
            f"¥{oa['drop_results'].get('drop_top1', {}).get('profit', 0):>+9,} "
            f"¥{oa['drop_results'].get('drop_top3', {}).get('profit', 0):>+9,} "
            f"{oa['longest_lose_streak_months']:>5} "
            f"¥{oa['max_drawdown']:>+9,}"
        )

    # 利益降順で並べ替えて主要 3 policy の月別表示
    rows_summary.sort(key=lambda r: r["profit"], reverse=True)
    print()
    print("=== Top 3 policy 月別 P&L ===")
    for r in rows_summary[:3]:
        name = r["policy"]
        monthly, oa = results[name]
        print(f"\n--- {name} (累計 ¥{oa['total_profit']:+,}) ---")
        for _, m in monthly.iterrows():
            bar = "█" * max(0, int(m["m_profit"] / 2000))
            sign = "+" if m["m_profit"] >= 0 else "-"
            print(
                f"  {m['test_month']}  stake=¥{int(m['m_stake']):>7,}  "
                f"payout=¥{int(m['m_payout']):>7,}  "
                f"profit={sign}¥{abs(int(m['m_profit'])):>7,}  "
                f"ROI={m['m_roi']*100:>6.1f}%  {bar}"
            )

    # レポート出力
    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_3point_outlier_analysis_{today}.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# 3点BUY policy 上振れ依存度分析 ({today})",
        "",
        f"**閾値**: thr = {THR}(`ev_3point_policy_sim.py` の主要評価点)",
        "",
        "**狙い**: 利益最大の月を除外した時に、policy ごとの利益がどう変化するか。",
        "上振れ依存度が高ければ「過去 max 月の偶然」で支えられている = 過去再現性低い。",
        "",
        "## サマリ",
        "",
        "| policy | 累計利益 | 月利益 std | min月 | max月 | -top1除外 | -top3除外 | "
        "連敗最長 | 最大DD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows_summary:
        md.append(
            f"| {r['policy']} | ¥{r['profit']:+,} | ¥{r['std']:+,.0f} | "
            f"¥{int(r['min_m']):+,} | ¥{int(r['max_m']):+,} | "
            f"¥{r['drop1']:+,} | ¥{r['drop3']:+,} | "
            f"{r['lose_streak']} 月 | ¥{r['max_dd']:+,} |"
        )
    md.append("")
    md.append("**読み方**:")
    md.append("- **-top1除外**: 一番儲かった月を抜いたときの累計利益。これが激減するなら")
    md.append("  「単月の上振れ依存」")
    md.append("- **連敗最長**: 月利益<0 が連続した最長期間")
    md.append("- **最大DD**: 累計利益曲線でピークから最も落ち込んだ金額")
    md.append("")

    md.append("## Policy 別 月別 P&L 詳細(全 policy)")
    md.append("")
    for name, (monthly, oa) in results.items():
        md.append(f"### {name} (累計 ¥{oa['total_profit']:+,})")
        md.append("")
        md.append("| test_month | stake | payout | profit | ROI |")
        md.append("|---|---:|---:|---:|---:|")
        for _, m in monthly.iterrows():
            md.append(
                f"| {m['test_month']} | ¥{int(m['m_stake']):,} | "
                f"¥{int(m['m_payout']):,} | ¥{int(m['m_profit']):+,} | "
                f"{m['m_roi']*100:.1f}% |"
            )
        md.append("")
        md.append(f"**top3 月**: " + ", ".join(
            f"{m['test_month']} (¥{int(m['m_profit']):+,})"
            for m in oa["top3_months"]
        ))
        md.append("")
        md.append(f"**worst3 月**: " + ", ".join(
            f"{m['test_month']} (¥{int(m['m_profit']):+,})"
            for m in oa["worst3_months"]
        ))
        md.append("")

    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
