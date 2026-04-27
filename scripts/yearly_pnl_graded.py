"""重賞のみで年度別 P&L を再計算

依存ファイル:
- data/walkforward_predictions_top3.parquet  (ml/walkforward.py --save-predictions で生成)
- data/race_meetings.csv  (scripts/fetch_grades.py で生成)
- data/payouts.csv

戦略:
- 重賞 = grade_code in {1=SG, 2=GⅠ, 3=GⅡ}
- 各レースで予測 top-1 車を 100 円購入(単勝・複勝)
- 重賞のみフィルタ → 年度別 P&L
- 比較として「全レース」「重賞以外」の数字も並べる
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"

GRADE_LABELS = {0: "普通", 1: "SG", 2: "GⅠ", 3: "GⅡ", 4: "その他"}
STAKES_GRADED = {1, 2, 3}  # SG/GⅠ/GⅡ = 重賞


def load_predictions() -> pd.DataFrame:
    df = pd.read_parquet(DATA / "walkforward_predictions_top3.parquet")
    df["race_date"] = pd.to_datetime(df["race_date"])
    df["place_code"] = df["place_code"].astype(int)
    df["race_no"] = df["race_no"].astype(int)
    df["car_no"] = df["car_no"].astype(int)
    return df


def load_meetings() -> pd.DataFrame:
    df = pd.read_csv(DATA / "race_meetings.csv")
    df["race_date"] = pd.to_datetime(df["race_date"])
    df["place_code"] = df["place_code"].astype(int)
    df["grade_code"] = pd.to_numeric(df["grade_code"], errors="coerce")
    return df


def load_payouts() -> pd.DataFrame:
    df = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    df["race_date"] = pd.to_datetime(df["race_date"])
    return df


def compute_pnl(picks: pd.DataFrame, payouts: pd.DataFrame, label: str) -> pd.DataFrame:
    """各レース予測 top-1 車を購入したと仮定して年度別 P&L を出す。"""
    if len(picks) == 0:
        return pd.DataFrame()

    tns = payouts[payouts["bet_type"] == "tns"][["race_date", "place_code", "race_no", "car_no_1", "refund"]]
    fns = payouts[payouts["bet_type"] == "fns"][["race_date", "place_code", "race_no", "car_no_1", "refund"]]
    # 同着重複対策: (race × car) で集約して合計払戻
    tns = tns.groupby(["race_date", "place_code", "race_no", "car_no_1"], as_index=False)["refund"].sum()
    fns = fns.groupby(["race_date", "place_code", "race_no", "car_no_1"], as_index=False)["refund"].sum()

    BET = 100

    # 単勝: pick の car が tns の winner と一致するか
    win = picks.merge(
        tns.rename(columns={"car_no_1": "car_no", "refund": "win_payout"}),
        on=["race_date", "place_code", "race_no", "car_no"], how="left",
    )
    win["win_payout"] = win["win_payout"].fillna(0)
    win["win_hit"] = (win["win_payout"] > 0).astype(int)

    # 複勝: pick の car が fns に存在するか
    place = picks.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "place_payout"}),
        on=["race_date", "place_code", "race_no", "car_no"], how="left",
    )
    place["place_payout"] = place["place_payout"].fillna(0)
    place["place_hit"] = (place["place_payout"] > 0).astype(int)

    # この時点で win/place は picks と同じ行数のはず
    assert len(win) == len(picks), f"win len mismatch: {len(win)} vs {len(picks)}"
    assert len(place) == len(picks), f"place len mismatch: {len(place)} vs {len(picks)}"

    picks = picks.assign(
        win_hit=win["win_hit"].values, win_payout=win["win_payout"].values,
        place_hit=place["place_hit"].values, place_payout=place["place_payout"].values,
    )
    picks["year"] = picks["race_date"].dt.year

    g = picks.groupby("year").agg(
        n_bets=("car_no", "size"),
        win_hits=("win_hit", "sum"),
        win_payout=("win_payout", "sum"),
        place_hits=("place_hit", "sum"),
        place_payout=("place_payout", "sum"),
    ).reset_index()
    g["cost"] = g["n_bets"] * BET
    g["win_profit"] = g["win_payout"] - g["cost"]
    g["place_profit"] = g["place_payout"] - g["cost"]
    g["win_roi"] = g["win_payout"] / g["cost"]
    g["place_roi"] = g["place_payout"] / g["cost"]
    g["win_hit_rate"] = g["win_hits"] / g["n_bets"]
    g["place_hit_rate"] = g["place_hits"] / g["n_bets"]
    g["scope"] = label

    # 全期間
    total_cost = g["cost"].sum()
    total = pd.DataFrame([{
        "year": "All",
        "n_bets": g["n_bets"].sum(),
        "win_hits": g["win_hits"].sum(),
        "win_payout": g["win_payout"].sum(),
        "place_hits": g["place_hits"].sum(),
        "place_payout": g["place_payout"].sum(),
        "cost": total_cost,
        "win_profit": g["win_payout"].sum() - total_cost,
        "place_profit": g["place_payout"].sum() - total_cost,
        "win_roi": g["win_payout"].sum() / total_cost if total_cost else 0,
        "place_roi": g["place_payout"].sum() / total_cost if total_cost else 0,
        "win_hit_rate": g["win_hits"].sum() / g["n_bets"].sum() if g["n_bets"].sum() else 0,
        "place_hit_rate": g["place_hits"].sum() / g["n_bets"].sum() if g["n_bets"].sum() else 0,
        "scope": label,
    }])
    return pd.concat([g, total], ignore_index=True)


def fmt_yen(v: float) -> str:
    return f"¥{int(v):,}"


def render_block(pnl: pd.DataFrame, header: str) -> list[str]:
    if pnl.empty:
        return [f"### {header}", "", "(対象データなし)", ""]

    rows = []
    for _, r in pnl.iterrows():
        rows.append({
            "年": r["year"],
            "ベット数": f"{int(r['n_bets']):,}",
            "投資": fmt_yen(r["cost"]),
            "単勝命中率": f"{r['win_hit_rate']*100:.1f}%",
            "単勝回収": fmt_yen(r["win_payout"]),
            "単勝損益": fmt_yen(r["win_profit"]),
            "単勝ROI": f"{r['win_roi']*100:.1f}%",
            "複勝命中率": f"{r['place_hit_rate']*100:.1f}%",
            "複勝回収": fmt_yen(r["place_payout"]),
            "複勝損益": fmt_yen(r["place_profit"]),
            "複勝ROI": f"{r['place_roi']*100:.1f}%",
        })
    table = pd.DataFrame(rows).to_markdown(index=False)
    return [f"### {header}", "", table, ""]


def main() -> None:
    preds = load_predictions()
    meetings = load_meetings()
    payouts = load_payouts()

    # 各 race × car の予測 → race ごとに top-1 prediction の car を選ぶ
    grp_keys = ["race_date", "place_code", "race_no"]
    picks = preds.loc[preds.groupby(grp_keys)["pred"].idxmax()].copy()
    picks = picks[grp_keys + ["car_no", "pred"]]

    # meeting 情報を join (grade)
    picks = picks.merge(
        meetings[["race_date", "place_code", "grade_code", "grade_name"]],
        on=["race_date", "place_code"], how="left",
    )

    # フィルタ別 P&L
    pnl_all = compute_pnl(picks, payouts, "全レース")
    pnl_graded = compute_pnl(
        picks[picks["grade_code"].isin(STAKES_GRADED)], payouts, "重賞のみ (SG+GⅠ+GⅡ)",
    )
    pnl_nongraded = compute_pnl(
        picks[~picks["grade_code"].isin(STAKES_GRADED) | picks["grade_code"].isna()],
        payouts, "重賞以外",
    )

    # grade 別の breakdown
    by_grade_rows = []
    for gc in [1, 2, 3, 4, 0]:
        sub = picks[picks["grade_code"] == gc]
        if len(sub) == 0:
            continue
        pnl = compute_pnl(sub, payouts, f"grade={gc}")
        if pnl.empty:
            continue
        # All 行のみ取る
        last = pnl[pnl["year"] == "All"].iloc[0]
        by_grade_rows.append({
            "グレード": GRADE_LABELS.get(gc, str(gc)),
            "ベット数": f"{int(last['n_bets']):,}",
            "投資": fmt_yen(last["cost"]),
            "単勝命中率": f"{last['win_hit_rate']*100:.1f}%",
            "単勝損益": fmt_yen(last["win_profit"]),
            "単勝ROI": f"{last['win_roi']*100:.1f}%",
            "複勝命中率": f"{last['place_hit_rate']*100:.1f}%",
            "複勝損益": fmt_yen(last["place_profit"]),
            "複勝ROI": f"{last['place_roi']*100:.1f}%",
        })
    by_grade = pd.DataFrame(by_grade_rows)

    # 重賞のレース数(参考)
    n_graded = (picks["grade_code"].isin(STAKES_GRADED)).sum()
    n_total = len(picks)

    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"yearly_pnl_graded_{today}.md"
    REPORTS.mkdir(exist_ok=True)
    md = [
        f"# 年度別 P&L: 重賞フィルタ ({today})",
        "",
        f"対象: walk-forward 49 ヶ月の予測(target=top3) × 各レース予測 top-1 車を 100 円購入。",
        "",
        f"## 概要",
        "",
        f"- 全レース: **{n_total:,}** 件",
        f"- 重賞 (SG+GⅠ+GⅡ): **{n_graded:,}** 件 ({n_graded/n_total*100:.1f}%)",
        f"- それ以外: **{n_total - n_graded:,}** 件",
        "",
        "## グレード別 全期間 P&L",
        "",
        by_grade.to_markdown(index=False) if not by_grade.empty else "(データなし)",
        "",
        "## 年度別 P&L",
        "",
    ]
    md += render_block(pnl_all, "(A) 全レース(参考)")
    md += render_block(pnl_graded, "(B) 重賞のみ(SG + GⅠ + GⅡ)")
    md += render_block(pnl_nongraded, "(C) 重賞以外")

    out.write_text("\n".join(md), encoding="utf-8")

    print()
    print(f"Total picks: {n_total:,}")
    print(f"Graded picks (SG/GⅠ/GⅡ): {n_graded:,}  ({n_graded/n_total*100:.1f}%)")
    print()
    print("=== Grade-level (All-time) ===")
    print(by_grade.to_string(index=False) if not by_grade.empty else "(empty)")
    print()
    print(f"Report saved: {out}")


if __name__ == "__main__":
    main()
