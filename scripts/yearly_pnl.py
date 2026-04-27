"""年度別 収支・ROI 集計

data/walkforward_top3.csv (月次) → 年度別に集計して md レポート出力。

戦略:
- 各レース予測 top-1 車を 100 円購入(単勝・複勝それぞれ)
- 月次 ROI を bets 数で重み付けして年度集計
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "walkforward_top3.csv"
REPORTS = ROOT / "reports"


def main() -> None:
    df = pd.read_csv(DATA)
    df["year"] = df["month"].str[:4].astype(int)

    BET = 100  # 1 ベット 100 円

    # 月次の絶対値に展開
    df["win_cost"] = df["win_n_bets"] * BET
    df["win_revenue"] = df["win_roi"] * df["win_cost"]
    df["win_hits"] = (df["win_hit_rate"] * df["win_n_bets"]).round().astype(int)

    df["place_cost"] = df["place_n_bets"] * BET
    df["place_revenue"] = df["place_roi"] * df["place_cost"]
    df["place_hits"] = (df["place_hit_rate"] * df["place_n_bets"]).round().astype(int)

    # 年度集計
    g = df.groupby("year").agg(
        months=("month", "count"),
        n_bets=("win_n_bets", "sum"),
        win_hits=("win_hits", "sum"),
        win_cost=("win_cost", "sum"),
        win_revenue=("win_revenue", "sum"),
        place_hits=("place_hits", "sum"),
        place_cost=("place_cost", "sum"),
        place_revenue=("place_revenue", "sum"),
    ).reset_index()
    g["win_profit"] = g["win_revenue"] - g["win_cost"]
    g["place_profit"] = g["place_revenue"] - g["place_cost"]
    g["win_roi"] = g["win_revenue"] / g["win_cost"]
    g["place_roi"] = g["place_revenue"] / g["place_cost"]
    g["win_hit_rate"] = g["win_hits"] / g["n_bets"]
    g["place_hit_rate"] = g["place_hits"] / g["n_bets"]

    # 全期間
    total = pd.Series({
        "year": "All",
        "months": int(g["months"].sum()),
        "n_bets": int(g["n_bets"].sum()),
        "win_hits": int(g["win_hits"].sum()),
        "win_cost": int(g["win_cost"].sum()),
        "win_revenue": int(g["win_revenue"].sum()),
        "place_hits": int(g["place_hits"].sum()),
        "place_cost": int(g["place_cost"].sum()),
        "place_revenue": int(g["place_revenue"].sum()),
        "win_profit": int(g["win_profit"].sum()),
        "place_profit": int(g["place_profit"].sum()),
        "win_roi": g["win_revenue"].sum() / g["win_cost"].sum(),
        "place_roi": g["place_revenue"].sum() / g["place_cost"].sum(),
        "win_hit_rate": g["win_hits"].sum() / g["n_bets"].sum(),
        "place_hit_rate": g["place_hits"].sum() / g["n_bets"].sum(),
    })

    # 表示用フォーマット
    def fmt_yen(v: float) -> str:
        return f"{int(v):,}"

    def render(row) -> dict:
        return {
            "年": row["year"],
            "月数": int(row["months"]),
            "ベット数": fmt_yen(row["n_bets"]),
            "投資額": f"¥{fmt_yen(row['win_cost'])}",
            "単勝命中": fmt_yen(row["win_hits"]),
            "単勝命中率": f"{row['win_hit_rate']*100:.1f}%",
            "単勝回収": f"¥{fmt_yen(row['win_revenue'])}",
            "単勝損益": f"¥{fmt_yen(row['win_profit'])}",
            "単勝ROI": f"{row['win_roi']*100:.1f}%",
            "複勝命中": fmt_yen(row["place_hits"]),
            "複勝命中率": f"{row['place_hit_rate']*100:.1f}%",
            "複勝回収": f"¥{fmt_yen(row['place_revenue'])}",
            "複勝損益": f"¥{fmt_yen(row['place_profit'])}",
            "複勝ROI": f"{row['place_roi']*100:.1f}%",
        }

    rows = [render(r) for _, r in g.iterrows()]
    rows.append(render(total))
    table = pd.DataFrame(rows)

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"yearly_pnl_{today}.md"
    REPORTS.mkdir(exist_ok=True)

    md_lines = [
        f"# 年度別 収支・ROI({today})",
        "",
        "対象: walk-forward 月次評価結果(target=top3、49 ヶ月分)を年度に集計。",
        "戦略: 各レースで予測 top-1 車を 100 円購入(単勝 1点、複勝 1点 を別々に集計)。",
        "",
        "## 注記",
        "",
        "- 2022 年は 4 月(walk-forward 開始月)〜12 月の 9 ヶ月分",
        "- 2026 年は 1 月〜4 月の 4 ヶ月分",
        "- 2023〜2025 年は通年(12 ヶ月)",
        "- ROI = 回収額 / 投資額(1.0 = 100% = 損益ゼロ)",
        "",
        "## 年度別 収支",
        "",
        table.to_markdown(index=False),
        "",
        "## 単勝ベット視点(1 レース 100 円ベット、的中時は単勝オッズ × 100 円が戻る)",
        "",
        f"- 5 年分(49 ヶ月)で **¥{fmt_yen(total['win_cost'])}** を投資、回収 **¥{fmt_yen(total['win_revenue'])}**、",
        f"  **損失 ¥{fmt_yen(-total['win_profit'])}**(ROI {total['win_roi']*100:.2f}%)",
        f"- 命中率 {total['win_hit_rate']*100:.1f}% は控除率 30% を埋めるには低すぎる",
        "",
        "## 複勝ベット視点(1 レース 100 円ベット、的中時は複勝オッズ × 100 円が戻る)",
        "",
        f"- 5 年分で **¥{fmt_yen(total['place_cost'])}** を投資、回収 **¥{fmt_yen(total['place_revenue'])}**、",
        f"  **損失 ¥{fmt_yen(-total['place_profit'])}**(ROI {total['place_roi']*100:.2f}%)",
        f"- 命中率 {total['place_hit_rate']*100:.1f}% で控除率に肉薄するも到達せず",
    ]
    out.write_text("\n".join(md_lines), encoding="utf-8")

    # コンソール表示
    print()
    print("=== 年度別 収支・ROI(walk-forward 49ヶ月) ===\n")
    print(table.to_string(index=False))
    print()
    print(f"Report saved: {out}")


if __name__ == "__main__":
    main()
