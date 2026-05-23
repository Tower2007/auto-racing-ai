"""LEAD_MIN 別の EV 維持率測定 (Antigravity 2026-05-23 提案 #3)。

発火時 EV ≥ thr が確定時 EV ≥ thr をどれだけ維持しているか、
LEAD_MIN の世代 (10/5/2/4 分前) ごとに分解して測定する。

データソース:
  - data/odds_snapshots.csv  (発火時 snap: pred_calib + ev_avg_calib)
  - data/odds_summary.csv    (確定時 closing odds)
  - data/payouts.csv         (実 hit/payout: fns 複勝のみ)

LEAD_MIN 履歴 (git log より):
  - 〜 2026-05-01 11:21:  10 分前
  - 〜 2026-05-14 03:57:   5 分前
  - 〜 2026-05-17 14:03:   2 分前 (試行)
  - 〜 現在:               4 分前 (Antigravity 要求の評価対象)

使い方:
  python scripts/ev_persistence_by_leadmin.py
  python scripts/ev_persistence_by_leadmin.py --thr 1.50
  python scripts/ev_persistence_by_leadmin.py --since 2026-05-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100

# LEAD_MIN 切替境界 (UTC ナイーブ JST)。新しい境界が来たら ↓ に追記
LEAD_MIN_BOUNDARIES = [
    (pd.Timestamp("2026-05-17 14:03"), 4),
    (pd.Timestamp("2026-05-14 03:57"), 2),
    (pd.Timestamp("2026-05-01 11:21"), 5),
    (pd.Timestamp("1970-01-01"),       10),  # それ以前
]


def infer_lead_min(captured_at) -> int:
    """captured_at から運用中だった LEAD_MIN を逆算。"""
    if pd.isna(captured_at):
        return -1
    ts = pd.to_datetime(captured_at)
    for boundary, lead in LEAD_MIN_BOUNDARIES:
        if ts >= boundary:
            return lead
    return -1


def load_data(since: str | None):
    snap_path = DATA / "odds_snapshots.csv"
    if not snap_path.exists() or snap_path.stat().st_size == 0:
        print("[error] data/odds_snapshots.csv が存在しないか空", file=sys.stderr)
        sys.exit(1)
    snap = pd.read_csv(snap_path, low_memory=False)
    snap["race_date"] = pd.to_datetime(snap["race_date"])
    snap["captured_at"] = pd.to_datetime(snap["captured_at"], errors="coerce")
    if since:
        snap = snap[snap["race_date"] >= pd.to_datetime(since)]

    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    odds = odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]].rename(
        columns={"place_odds_min": "close_min", "place_odds_max": "close_max"}
    )

    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()
    fns = fns.rename(columns={"car_no_1": "car_no", "refund": "payout"})

    return snap, odds, fns


def summarize_era(label: str, df: pd.DataFrame, thr: float) -> None:
    """LEAD_MIN era ごとに 1 行サマリを出力。"""
    if df.empty:
        print(f"  {label:14s}: no data")
        return
    # pred-top1 のみ
    df = df[df["pred_rank"] == 1].copy()
    if df.empty:
        print(f"  {label:14s}: pred-top1 該当なし")
        return
    df = df.dropna(subset=["close_min", "close_max"]).copy()
    if df.empty:
        print(f"  {label:14s}: close odds 取り込み待ち")
        return

    df["close_ev"] = df["pred_calib"] * (df["close_min"] + df["close_max"]) / 2
    df["ev_diff"] = df["close_ev"] - df["ev_avg_calib"]

    snap_hi = df[df["ev_avg_calib"] >= thr]
    n_snap = len(snap_hi)
    if n_snap == 0:
        print(
            f"  {label:14s}: n_top1={len(df):3d}  "
            f"snap EV≥{thr} 0 件 (drift 平均 {df['ev_diff'].mean():+.3f})"
        )
        return
    still_hi = (snap_hi["close_ev"] >= thr).sum()
    persistence = still_hi / n_snap * 100
    drift_mean = snap_hi["ev_diff"].mean()
    drift_med = snap_hi["ev_diff"].median()

    # 実成績 (payout 列があれば)
    extra = ""
    if "payout" in snap_hi.columns:
        snap_hi_pay = snap_hi.dropna(subset=["payout"]).copy()
        snap_hi_pay["payout"] = snap_hi_pay["payout"].fillna(0).astype(int)
        n_eval = len(snap_hi_pay)
        if n_eval > 0:
            hits = (snap_hi_pay["payout"] > 0).sum()
            cost = n_eval * BET
            pay = int(snap_hi_pay["payout"].sum())
            roi = pay / cost * 100 if cost else 0
            extra = f"  | hit {hits}/{n_eval} ({hits/n_eval*100:.0f}%)  ROI {roi:.1f}%"

    print(
        f"  {label:14s}: n_top1={len(df):3d}  "
        f"snap≥{thr} {n_snap:3d}件  維持率 {still_hi:3d}/{n_snap} ({persistence:5.1f}%)  "
        f"drift μ={drift_mean:+.3f} med={drift_med:+.3f}{extra}"
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--thr", type=float, default=1.50)
    p.add_argument("--since", type=str, default=None,
                   help="YYYY-MM-DD 以降に絞る")
    args = p.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    snap, odds, fns = load_data(args.since)
    print(f"=== EV 維持率 (LEAD_MIN 別) thr={args.thr} ===")
    print(f"対象期間: {snap['race_date'].min().date()} 〜 {snap['race_date'].max().date()}")
    print(f"snap 行数: {len(snap)} ({snap.groupby(RACE_KEY).ngroups} R)")
    print()

    # 確定済 R のみ (fns が存在する R)
    snap_keys = snap[RACE_KEY].drop_duplicates()
    confirmed = fns.merge(snap_keys, on=RACE_KEY, how="inner")[RACE_KEY].drop_duplicates()
    if confirmed.empty:
        print("[info] 確定済 R が 0 — daily_ingest 待ち")
        return
    eval_df = snap.merge(confirmed, on=RACE_KEY, how="inner")
    eval_df = eval_df.merge(odds, on=RACE_KEY + ["car_no"], how="left")
    eval_df = eval_df.merge(fns, on=RACE_KEY + ["car_no"], how="left")
    eval_df["payout"] = eval_df["payout"].fillna(0)

    # LEAD_MIN era タグ
    eval_df["lead_min"] = eval_df["captured_at"].apply(infer_lead_min)

    print(f"  | snap≥thr | 維持率 (close でも EV≥thr) | drift μ/med | hit/ROI |")
    print(f"  {'-' * 92}")
    # 各 era ごとに集計
    for boundary, lead in LEAD_MIN_BOUNDARIES:
        sub = eval_df[eval_df["lead_min"] == lead]
        if sub.empty:
            continue
        period_start = sub["captured_at"].min().date() if pd.notna(sub["captured_at"].min()) else "?"
        period_end = sub["captured_at"].max().date() if pd.notna(sub["captured_at"].max()) else "?"
        label = f"LEAD={lead}min"
        print(f"  [{label}] {period_start} 〜 {period_end}")
        summarize_era("  ", sub, args.thr)
        print()

    # 全期間サマリ
    print(f"  [ALL] (LEAD_MIN 区別なし)")
    summarize_era("  ", eval_df, args.thr)

    # Antigravity の問いに対する要約コメント
    print()
    print("=== 解釈ガイド (Antigravity 2026-05-23 提案 #3) ===")
    print("  維持率: snap で EV≥thr だった top1 picks のうち、確定 odds でも EV≥thr 維持率")
    print("  drift: close_ev - snap_ev の平均/中央値。負ならオッズ低下 (本命化)、正なら上昇 (穴化)")
    print("  → 4 分前への戻しで維持率が 2 分前に近づいてれば回復、5 分前より低ければ drift 残り")


if __name__ == "__main__":
    main()
