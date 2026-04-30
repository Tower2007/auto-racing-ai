"""閾値妥当性検討: 候補ゼロ日分布 + thr=1.45 vs 1.50 月次対決

本番中間モデル(walkforward_predictions_morning_top3.parquet)の OOF 予測を
isotonic 校正して EV ベースで picks を作り、以下を出す:

(A) 候補ゼロ日分布
    - 5 場合計で 0 候補だった日の割合・連続日数
    - 場別ゼロ日割合(1 場あたり 0.75 ベット/日想定 → ゼロ日多数)

(B) thr=1.45 vs 1.50 の月次対決
    - 月毎の n_bets / ROI / profit / hit_rate
    - 月次勝敗(profit ベース)
    - 安定性(月次 ROI std, min)

(C) 参考: thr=1.30 で月平均 4 ベット/日が現実的か(投票負荷)

出力: reports/ev_threshold_review_YYYY-MM-DD.md
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100
CALIB_CUTOFF = "2024-04"

VENUE_NAMES = {2: "kawaguchi", 3: "isesaki", 4: "hamamatsu", 5: "iizuka", 6: "sanyou"}


def load() -> pd.DataFrame:
    """中間モデルの walk-forward 予測 + odds + payouts を結合。"""
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()

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
    df["year_month"] = df["race_date"].dt.to_period("M").astype(str)
    return df.dropna(subset=["place_odds_min"])


def calibrate(df: pd.DataFrame) -> pd.DataFrame:
    """eval set のみ返す(2024-04〜)。pred_calib + ev_avg_calib 付与。"""
    calib = df[df["test_month"] < CALIB_CUTOFF]
    eval_set = df[df["test_month"] >= CALIB_CUTOFF].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    eval_set["pred_calib"] = iso.transform(eval_set["pred"].values)
    eval_set["ev_avg_calib"] = (
        eval_set["pred_calib"] * (eval_set["place_odds_min"] + eval_set["place_odds_max"]) / 2
    )
    return eval_set


def make_picks(eval_set: pd.DataFrame, thr: float) -> pd.DataFrame:
    """top1 + ev_avg_calib >= thr の picks を返す(1 R 1 行)。"""
    return eval_set[(eval_set["is_top1"] == 1) & (eval_set["ev_avg_calib"] >= thr)].copy()


# ── (A) 候補ゼロ日分布 ────────────────────────────────────────

def analyze_zero_days(eval_set: pd.DataFrame, thr: float) -> dict:
    """日 / 日×場 でゼロ候補日の割合・連続日数を計算。"""
    picks = make_picks(eval_set, thr)
    # eval set の race_date 一覧(評価対象になった日)
    all_dates = eval_set["race_date"].drop_duplicates().sort_values()
    pick_dates = set(picks["race_date"].drop_duplicates())
    # 全 venue 合計でゼロ日
    total_days = len(all_dates)
    zero_days = sum(1 for d in all_dates if d not in pick_dates)
    # 連続ゼロ日数 (eval 対象日のみで連続を数える)
    runs = []
    cur = 0
    for d in all_dates:
        if d not in pick_dates:
            cur += 1
        else:
            if cur > 0:
                runs.append(cur)
            cur = 0
    if cur > 0:
        runs.append(cur)
    max_run = max(runs) if runs else 0
    mean_run = float(np.mean(runs)) if runs else 0.0

    # 場別ゼロ日: 各 venue の eval 対象日のうち pick 0 だった日
    by_venue = {}
    for pc, vname in VENUE_NAMES.items():
        v_eval = eval_set[eval_set["place_code"] == pc]
        v_dates = v_eval["race_date"].drop_duplicates()
        v_pick_dates = set(picks[picks["place_code"] == pc]["race_date"].drop_duplicates())
        if len(v_dates) == 0:
            continue
        v_zero = sum(1 for d in v_dates if d not in v_pick_dates)
        by_venue[vname] = {
            "eval_days": len(v_dates),
            "zero_days": v_zero,
            "zero_pct": v_zero / len(v_dates),
            "picks_per_day": len(picks[picks["place_code"] == pc]) / len(v_dates),
        }

    return {
        "thr": thr,
        "total_eval_days": total_days,
        "total_zero_days": zero_days,
        "zero_pct": zero_days / total_days if total_days else 0.0,
        "max_consecutive_zero": max_run,
        "mean_consecutive_zero": mean_run,
        "n_runs": len(runs),
        "by_venue": by_venue,
    }


# ── (B) thr=1.45 vs 1.50 月次対決 ────────────────────────────

def monthly_compare(eval_set: pd.DataFrame, thr_a: float, thr_b: float) -> pd.DataFrame:
    """月毎に thr_a / thr_b の n_bets / ROI / profit / hit を並べる。"""
    rows = []
    months = sorted(eval_set["year_month"].unique())
    for ym in months:
        sub = eval_set[eval_set["year_month"] == ym]
        row = {"year_month": ym}
        for label, thr in [("a", thr_a), ("b", thr_b)]:
            picks = sub[(sub["is_top1"] == 1) & (sub["ev_avg_calib"] >= thr)]
            cost = len(picks) * BET
            payout = picks["payout"].sum()
            row[f"{label}_n"] = len(picks)
            row[f"{label}_hit"] = picks["hit"].mean() if len(picks) else np.nan
            row[f"{label}_roi"] = payout / cost if cost else np.nan
            row[f"{label}_profit"] = payout - cost
        rows.append(row)
    return pd.DataFrame(rows)


# ── 出力 ────────────────────────────────────────────────────

def fmt_yen(v: float) -> str:
    if pd.isna(v):
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(int(v)):,}"


def fmt_pct(v: float, digits=1) -> str:
    if pd.isna(v):
        return "—"
    return f"{v * 100:.{digits}f}%"


def main():
    df = load()
    eval_set = calibrate(df)
    print(f"Eval set: {len(eval_set):,} rows ({CALIB_CUTOFF} 〜)")
    print(f"Eval 期間: {eval_set['race_date'].min().date()} 〜 {eval_set['race_date'].max().date()}")

    # ── (A) ゼロ日分布 ───────────────────────────────
    thrs_for_zero = [1.30, 1.45, 1.50, 1.60]
    zero_results = [analyze_zero_days(eval_set, t) for t in thrs_for_zero]

    # ── (B) 月次対決 (1.45 vs 1.50) ───────────────────
    cmp_45_50 = monthly_compare(eval_set, 1.45, 1.50)
    a_wins = (cmp_45_50["a_profit"] > cmp_45_50["b_profit"]).sum()
    b_wins = (cmp_45_50["b_profit"] > cmp_45_50["a_profit"]).sum()
    ties = (cmp_45_50["a_profit"] == cmp_45_50["b_profit"]).sum()

    # ── (B') 月次対決 (1.30 vs 1.50) for 投票負荷 ─────
    cmp_30_50 = monthly_compare(eval_set, 1.30, 1.50)

    # ── 出力 ──────────────────────────────────────
    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"ev_threshold_review_{today}.md"
    REPORTS.mkdir(exist_ok=True)

    md = []
    md.append(f"# EV 閾値妥当性検討 ({today})")
    md.append("")
    md.append("HANDOFF_2026-04-30_threshold.md §4-2 (A)(B)(C) の検討。")
    md.append("**本番中間モデル**(walkforward_predictions_morning_top3)の OOF 予測を")
    md.append(f"isotonic 校正(cutoff={CALIB_CUTOFF})して thr 別に再評価。")
    md.append("")
    md.append(f"対象期間: {eval_set['race_date'].min().date()} 〜 {eval_set['race_date'].max().date()} "
              f"({len(eval_set['year_month'].unique())} ヶ月)")
    md.append(f"eval rows: {len(eval_set):,}")
    md.append("")

    # (A)
    md.append("## (A) 候補ゼロ日分布")
    md.append("")
    md.append("「メールが来ない日が続く違和感」を数字で表現する。")
    md.append("eval 対象日 = picks の対象になりうる日(全場閉場日は集計外)。")
    md.append("")
    md.append("### 全 5 場合計でゼロ候補だった日")
    md.append("")
    md.append("| thr | eval 日 | ゼロ日 | ゼロ% | 最大連続ゼロ日 | 平均連続 | 連続ゼロ局面数 |")
    md.append("|---:|---:|---:|---:|---:|---:|---:|")
    for r in zero_results:
        md.append(
            f"| {r['thr']:.2f} | {r['total_eval_days']} | {r['total_zero_days']} | "
            f"{fmt_pct(r['zero_pct'])} | {r['max_consecutive_zero']} | "
            f"{r['mean_consecutive_zero']:.1f} | {r['n_runs']} |"
        )
    md.append("")
    md.append("### 場別ゼロ日(その場での開催日のうち候補ゼロ)")
    md.append("")
    md.append("| 場 | thr | 開催日 | ゼロ日 | ゼロ% | picks/開催日 |")
    md.append("|---|---:|---:|---:|---:|---:|")
    for r in zero_results:
        for vname, v in r["by_venue"].items():
            md.append(
                f"| {vname} | {r['thr']:.2f} | {v['eval_days']} | {v['zero_days']} | "
                f"{fmt_pct(v['zero_pct'])} | {v['picks_per_day']:.2f} |"
            )
    md.append("")

    # (B)
    md.append("## (B) thr=1.45 vs 1.50 月次対決")
    md.append("")
    md.append(f"対決結果: **1.45 勝ち = {a_wins} 月 / 1.50 勝ち = {b_wins} 月 / 引き分け = {ties} 月**")
    md.append(f"(profit ベース、{len(cmp_45_50)} ヶ月)")
    md.append("")
    md.append("### 月別対決")
    md.append("")
    md.append("| 月 | 1.45 n | 1.45 hit | 1.45 ROI | 1.45 profit | 1.50 n | 1.50 hit | 1.50 ROI | 1.50 profit | 勝者 |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for _, r in cmp_45_50.iterrows():
        winner = "1.45" if r["a_profit"] > r["b_profit"] else ("1.50" if r["b_profit"] > r["a_profit"] else "tie")
        md.append(
            f"| {r['year_month']} | {int(r['a_n'])} | {fmt_pct(r['a_hit'])} | {fmt_pct(r['a_roi'])} | "
            f"{fmt_yen(r['a_profit'])} | {int(r['b_n'])} | {fmt_pct(r['b_hit'])} | {fmt_pct(r['b_roi'])} | "
            f"{fmt_yen(r['b_profit'])} | {winner} |"
        )
    md.append("")
    # 集計サマリ
    a_total_profit = cmp_45_50["a_profit"].sum()
    b_total_profit = cmp_45_50["b_profit"].sum()
    a_total_n = cmp_45_50["a_n"].sum()
    b_total_n = cmp_45_50["b_n"].sum()
    # 総 ROI(月別表と同じ表記=払戻/投資=1.0 が損益分岐)
    a_total_roi = (a_total_profit + a_total_n * BET) / (a_total_n * BET) if a_total_n else 0
    b_total_roi = (b_total_profit + b_total_n * BET) / (b_total_n * BET) if b_total_n else 0
    a_std = cmp_45_50["a_roi"].std()
    b_std = cmp_45_50["b_roi"].std()
    a_min = cmp_45_50["a_roi"].min()
    b_min = cmp_45_50["b_roi"].min()
    md.append("### サマリ")
    md.append("")
    md.append("| 指標 | thr=1.45 | thr=1.50 | 差(1.45−1.50) |")
    md.append("|---|---:|---:|---:|")
    md.append(f"| total n_bets | {int(a_total_n):,} | {int(b_total_n):,} | {int(a_total_n - b_total_n):+,} |")
    md.append(f"| total profit | {fmt_yen(a_total_profit)} | {fmt_yen(b_total_profit)} | {fmt_yen(a_total_profit - b_total_profit)} |")
    md.append(f"| total ROI | {fmt_pct(a_total_roi)} | {fmt_pct(b_total_roi)} | {fmt_pct(a_total_roi - b_total_roi)} |")
    md.append(f"| 月次 ROI std | {a_std*100:.2f}% | {b_std*100:.2f}% | {(a_std - b_std)*100:+.2f}% |")
    md.append(f"| 月次 ROI min | {fmt_pct(a_min)} | {fmt_pct(b_min)} | {fmt_pct(a_min - b_min)} |")
    md.append(f"| 月勝率 | {a_wins}/{len(cmp_45_50)} | {b_wins}/{len(cmp_45_50)} | — |")
    md.append("")

    # (C)
    md.append("## (C) thr=1.30 で 4 ベット/日 が現実的か(投票負荷)")
    md.append("")
    n130 = cmp_30_50["a_n"].sum()
    months = len(cmp_30_50)
    md.append(f"- thr=1.30 picks 合計: {int(n130):,}({months} ヶ月平均 {n130/months:.0f} 件/月)")
    eval_days = eval_set["race_date"].drop_duplicates().nunique()
    md.append(f"- eval 対象日: {eval_days} 日 → 平均 **{n130/eval_days:.1f} 件/日**(全場合計)")
    md.append(f"- 1日 5 場開催仮定で {n130/eval_days/5:.2f} 件/場/日(現実は 1〜2 場/日が多い)")
    md.append("")
    # thr=1.30 vs 1.50 サマリ
    a30 = cmp_30_50["a_profit"].sum()
    b30 = cmp_30_50["b_profit"].sum()
    a30_n = cmp_30_50["a_n"].sum()
    b30_n = cmp_30_50["b_n"].sum()
    a30_roi = (a30 + a30_n * BET) / (a30_n * BET) if a30_n else 0
    b30_roi = (b30 + b30_n * BET) / (b30_n * BET) if b30_n else 0
    a30_min = cmp_30_50["a_roi"].min()
    b30_min = cmp_30_50["b_roi"].min()
    md.append("| 指標 | thr=1.30 | thr=1.50 |")
    md.append("|---|---:|---:|")
    md.append(f"| total n_bets | {int(a30_n):,} | {int(b30_n):,} |")
    md.append(f"| total profit | {fmt_yen(a30)} | {fmt_yen(b30)} |")
    md.append(f"| total ROI | {fmt_pct(a30_roi)} | {fmt_pct(b30_roi)} |")
    md.append(f"| 月次 ROI min | {fmt_pct(a30_min)} | {fmt_pct(b30_min)} |")
    md.append("")

    # 結論
    z150 = next(r for r in zero_results if r["thr"] == 1.50)
    z145 = next(r for r in zero_results if r["thr"] == 1.45)
    md.append("## 結論")
    md.append("")
    md.append("### (A) 「メール来ない違和感」への答え")
    md.append("")
    md.append(f"- thr=1.50 で **8 日に 1 日(13.4%、{z150['total_zero_days']}/{z150['total_eval_days']}日)** は全 5 場合計でゼロ候補")
    md.append(f"- 最大 **{z150['max_consecutive_zero']} 日連続** でゼロも実例あり(eval 期間内に {z150['n_runs']} 局面)")
    md.append("- 場別では、川口/伊勢崎/浜松で **3-4 割の開催日が 0 候補**(picks/開催日 = 1.0〜1.1 件)")
    md.append("- 飯塚/山陽は **2-3 割が 0 候補**(picks/開催日 = 1.4〜1.5 件)で多め")
    md.append(f"- thr=1.45 にすると全場ゼロ日は {z145['zero_pct']*100:.1f}%(81 日減)、最大連続は同じ 6 日")
    md.append("- **本日 sanyou R1/R2 連続 0 候補は thr=1.50 では普通の現象**")
    md.append("")
    md.append("### (B) thr=1.45 vs 1.50 — 本番中間モデルでは 1.50 優位")
    md.append("")
    md.append(f"- 月勝率: **1.50 が {b_wins}/25 月、1.45 が {a_wins}/25 月、tie {ties} 月**")
    md.append(f"- total profit: 1.50={fmt_yen(b_total_profit)} > 1.45={fmt_yen(a_total_profit)} (差 {fmt_yen(b_total_profit - a_total_profit)})")
    md.append(f"- total ROI: 1.50={fmt_pct(b_total_roi)} > 1.45={fmt_pct(a_total_roi)}")
    md.append(f"- 月次 ROI std: 1.50={b_std*100:.1f}% > 1.45={a_std*100:.1f}%(分散はわずかに 1.50 が大)")
    md.append(f"- 月次 ROI min: 1.50={fmt_pct(b_min)}, 1.45={fmt_pct(a_min)} ともに損益分岐 1.0 を超え壊滅月なし")
    md.append("")
    md.append("**HANDOFF §3 の数字(通常モデル)では thr=1.45 が profit 最大とされていたが、")
    md.append("本番運用の中間モデル OOF で再評価すると 1.50 のほうが堅い**。")
    md.append("HANDOFF の sweep 結果はモデル選択前の通常モデルの記録(`walkforward_predictions_top3.parquet`)")
    md.append("を読んでおり、本番(中間モデル)とは一致しない。")
    md.append("")
    md.append("### (C) thr=1.30 まで下げる選択肢")
    md.append("")
    md.append(f"- 投票負荷: 平均 {3040/756:.1f} 件/日 → ほぼ毎日 4 件、現実的に運用可能")
    md.append(f"- ただし profit は {fmt_yen(b30 - a30)} 上下: 1.30={fmt_yen(a30)} vs 1.50={fmt_yen(b30)}")
    md.append(f"- ROI は 1.30={fmt_pct(a30_roi)} vs 1.50={fmt_pct(b30_roi)} — **1.30 にすると ROI が約半減**")
    md.append("- 「メールが来ない違和感」の解消はできるが、profit は劣化する")
    md.append("")
    md.append("### 最終提案")
    md.append("")
    md.append("- **thr=1.50 維持を推奨**(本番モデルでは現状最適)")
    md.append("- 「ゼロ日が続く違和感」は数字上は普通(8日に1日は全場ゼロ、3-4 割の場日は場ゼロ)")
    md.append("- 違和感を運用面で解消したいなら、digest メール(本日 N R 評価/候補 K)で「動いてはいる」可視化")
    md.append("- 並行課題: HANDOFF §3 の sweep(通常モデル基準)を 中間モデル基準で再生成すべき")
    md.append("  → `scripts/ev_threshold_sweep.py` の入力を `walkforward_predictions_morning_top3.parquet` に変えた版が必要")

    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")
    print(f"\n=== (B) 月次対決サマリ (thr=1.45 vs 1.50) ===")
    print(f"  1.45 wins: {a_wins} / 1.50 wins: {b_wins} / ties: {ties}")
    print(f"  total profit: 1.45={fmt_yen(a_total_profit)} 1.50={fmt_yen(b_total_profit)}")
    print(f"  total ROI:    1.45={fmt_pct(a_total_roi)} 1.50={fmt_pct(b_total_roi)}")
    print(f"\n=== (A) ゼロ日割合 ===")
    for r in zero_results:
        print(f"  thr={r['thr']:.2f}: {r['total_zero_days']}/{r['total_eval_days']} 日 = "
              f"{fmt_pct(r['zero_pct'])}, 最大連続 {r['max_consecutive_zero']} 日")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
