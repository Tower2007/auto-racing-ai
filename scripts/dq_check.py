"""
データ品質チェックスクリプト

backfill 完了後に data/*.csv を一通り検査して、reports/data_quality_<date>.md
にサマリを書き出す。

使い方: python scripts/dq_check.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

VENUE_NAMES = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
RACE_KEY = ["race_date", "place_code", "race_no"]
ENTRY_KEY = RACE_KEY + ["car_no"]


def load_all() -> dict[str, pd.DataFrame]:
    csvs = ["race_entries", "race_stats", "race_results", "odds_summary", "payouts", "race_laps"]
    out = {}
    for name in csvs:
        path = DATA_DIR / f"{name}.csv"
        df = pd.read_csv(path, dtype={"player_code": str}, low_memory=False)
        df["race_date"] = pd.to_datetime(df["race_date"])
        out[name] = df
    return out


def section_row_counts(dfs: dict[str, pd.DataFrame]) -> str:
    out = ["## 1. 行数とサイズ\n", "| CSV | 行数 | サイズ (MB) |", "|---|---:|---:|"]
    for name, df in dfs.items():
        size_mb = (DATA_DIR / f"{name}.csv").stat().st_size / 1024 / 1024
        out.append(f"| {name} | {len(df):,} | {size_mb:.1f} |")
    return "\n".join(out) + "\n"


def section_date_coverage(dfs: dict[str, pd.DataFrame]) -> str:
    out = ["## 2. 日付カバレッジ\n"]
    df = dfs["race_entries"]
    dmin, dmax = df["race_date"].min(), df["race_date"].max()
    days_total = (dmax - dmin).days + 1
    out.append(f"- 期間: {dmin.date()} 〜 {dmax.date()} ({days_total} 日)")
    out.append("")
    out.append("### 場別 race-day 数(出走表ベース)")
    out.append("")
    out.append("| 場 | race-day 数 | 最古 | 最新 |")
    out.append("|---|---:|---|---|")
    for code, name in VENUE_NAMES.items():
        sub = df[df["place_code"] == code]
        if len(sub) == 0:
            out.append(f"| {name} (pc={code}) | 0 | - | - |")
            continue
        unique_days = sub["race_date"].nunique()
        out.append(
            f"| {name} (pc={code}) | {unique_days} | "
            f"{sub['race_date'].min().date()} | {sub['race_date'].max().date()} |"
        )
    out.append("")
    return "\n".join(out)


def section_cross_consistency(dfs: dict[str, pd.DataFrame]) -> str:
    out = ["## 3. ファイル間整合性 (race_id レベル)\n"]
    out.append("race_id = (race_date, place_code, race_no)")
    out.append("")

    sets: dict[str, set] = {}
    for name in ["race_entries", "race_results", "odds_summary"]:
        df = dfs[name]
        sets[name] = set(map(tuple, df[RACE_KEY].drop_duplicates().to_numpy()))

    out.append("| 集合 | race_id 数 |")
    out.append("|---|---:|")
    for name, s in sets.items():
        out.append(f"| {name} | {len(s):,} |")
    out.append("")

    only_results = sets["race_results"] - sets["race_entries"]
    only_odds = sets["odds_summary"] - sets["race_entries"]
    only_entries = sets["race_entries"] - sets["race_results"]
    common_ro = sets["race_results"] & sets["odds_summary"]
    out.append("### 差分")
    out.append("")
    out.append(f"- results にあって entries に無い race_id: **{len(only_results)}**")
    out.append(f"- odds にあって entries に無い race_id: **{len(only_odds)}**")
    out.append(f"- entries にあって results に無い race_id: **{len(only_entries)}**")
    out.append(f"- results ∩ odds の race_id: **{len(common_ro):,}**")
    out.append("")

    if only_results or only_odds:
        out.append("### entries に無い race_id 一覧(最大 20 件)")
        out.append("")
        out.append("| source | race_date | place_code | race_no |")
        out.append("|---|---|---:|---:|")
        sample = list(only_results | only_odds)[:20]
        for rid in sample:
            d, pc, rn = rid
            src = []
            if rid in only_results:
                src.append("results")
            if rid in only_odds:
                src.append("odds")
            out.append(f"| {','.join(src)} | {pd.Timestamp(d).date()} | {pc} | {rn} |")
        out.append("")
    return "\n".join(out)


def section_duplicates(dfs: dict[str, pd.DataFrame]) -> str:
    out = ["## 4. 重複検知\n"]
    checks = [
        ("race_entries", ENTRY_KEY),
        ("race_stats", ENTRY_KEY),
        ("race_results", ENTRY_KEY),
        ("odds_summary", ENTRY_KEY),
        ("race_laps", RACE_KEY + ["lap_no", "car_no"]),
    ]
    out.append("| CSV | キー | 重複行数 |")
    out.append("|---|---|---:|")
    for name, key in checks:
        df = dfs[name]
        dup = df.duplicated(subset=key).sum()
        out.append(f"| {name} | {','.join(key)} | {int(dup)} |")
    out.append("")

    # payouts: 同じ (race_id, bet_type, car_no_1, car_no_2, car_no_3) は重複であってはならないが、
    # ワイドや複勝は同じ bet_type に複数行(順位違い等)があるので、組合せキーで見る
    pay = dfs["payouts"]
    pay_key = RACE_KEY + ["bet_type", "car_no_1", "car_no_2", "car_no_3"]
    pay_dup = pay.duplicated(subset=pay_key).sum()
    out.append(f"※ payouts: ({','.join(pay_key)}) 重複 = {int(pay_dup)} 行")
    out.append("")
    return "\n".join(out)


def section_null_rates(dfs: dict[str, pd.DataFrame]) -> str:
    out = ["## 5. 主要列の NULL 率\n"]
    targets = {
        "race_entries": ["car_no", "player_code", "handicap", "trial_run_time", "rank", "rate2", "rate3"],
        "race_results": ["order", "race_time", "st", "trial_time", "accident_code", "foul_code"],
        "odds_summary": ["win_odds", "place_odds_min", "place_odds_max"],
        "payouts": ["refund", "pop", "refund_votes", "car_no_1", "car_no_2", "car_no_3"],
        "race_laps": ["lap_no", "rank"],
    }
    for name, cols in targets.items():
        df = dfs[name]
        n = len(df)
        out.append(f"### {name} (n={n:,})")
        out.append("")
        out.append("| 列 | NULL 数 | NULL 率 |")
        out.append("|---|---:|---:|")
        for c in cols:
            if c not in df.columns:
                out.append(f"| {c} | (列なし) | - |")
                continue
            nulls = int(df[c].isna().sum())
            pct = nulls / n * 100 if n else 0
            out.append(f"| {c} | {nulls:,} | {pct:.2f}% |")
        out.append("")
    return "\n".join(out)


def section_finish_distribution(dfs: dict[str, pd.DataFrame]) -> str:
    df = dfs["race_results"]
    out = ["## 6. 着順 / 失格 分布\n"]
    n = len(df)
    out.append(f"総結果行: {n:,}")
    out.append("")
    out.append("### 着順分布")
    out.append("")
    counts = df["order"].value_counts(dropna=False).sort_index()
    out.append("| order | 件数 | 比率 |")
    out.append("|---:|---:|---:|")
    for k, v in counts.items():
        label = "NULL (失格/欠車)" if pd.isna(k) else int(k)
        out.append(f"| {label} | {int(v):,} | {v/n*100:.2f}% |")
    out.append("")

    out.append("### accident_code 分布(NULL 以外)")
    out.append("")
    acc = df[df["accident_code"].notna()].groupby(["accident_code", "accident_name"]).size().sort_values(ascending=False)
    out.append("| code | name | 件数 |")
    out.append("|---|---|---:|")
    for (code, name), v in acc.items():
        out.append(f"| {code} | {name} | {int(v):,} |")
    out.append("")

    out.append("### foul_code 分布(NULL 以外)")
    out.append("")
    foul = df[df["foul_code"].notna()].groupby("foul_code").size().sort_values(ascending=False)
    out.append("| foul_code | 件数 |")
    out.append("|---|---:|")
    for code, v in foul.items():
        out.append(f"| {code} | {int(v):,} |")
    out.append("")
    return "\n".join(out)


def section_odds_anomalies(dfs: dict[str, pd.DataFrame]) -> str:
    df = dfs["odds_summary"]
    out = ["## 7. オッズ異常値\n"]
    n = len(df)
    out.append(f"odds_summary 行数: {n:,}\n")

    win = df["win_odds"]
    out.append("### win_odds 統計")
    out.append("")
    out.append(f"- 非 NULL 件数: {win.notna().sum():,}")
    out.append(f"- NULL: {win.isna().sum():,}")
    out.append(f"- 0.0 (要注意): **{(win == 0).sum():,}**")
    out.append(f"- 中央値: {win.median():.2f}")
    out.append(f"- 平均: {win.mean():.2f}")
    out.append(f"- 最大: {win.max():.2f}")
    out.append(f"- ≥ 100: {(win >= 100).sum():,}")
    out.append(f"- ≥ 500: {(win >= 500).sum():,}")
    out.append(f"- ≥ 1000: {(win >= 1000).sum():,}")
    out.append("")

    out.append("### place_odds 範囲チェック")
    out.append("")
    bad = (df["place_odds_min"] > df["place_odds_max"]).sum()
    out.append(f"- place_odds_min > place_odds_max(逆転): {int(bad):,}")
    out.append(f"- place_odds_min が NULL: {df['place_odds_min'].isna().sum():,}")
    out.append(f"- place_odds_max が NULL: {df['place_odds_max'].isna().sum():,}")
    out.append("")
    return "\n".join(out)


def section_lap_consistency(dfs: dict[str, pd.DataFrame]) -> str:
    laps = dfs["race_laps"]
    results = dfs["race_results"]
    out = ["## 8. 周回データ整合性\n"]

    races_in_results = set(map(tuple, results[RACE_KEY].drop_duplicates().to_numpy()))
    races_in_laps = set(map(tuple, laps[RACE_KEY].drop_duplicates().to_numpy()))

    out.append(f"- race_results に存在する race_id: {len(races_in_results):,}")
    out.append(f"- race_laps に存在する race_id: {len(races_in_laps):,}")
    out.append(f"- laps が無い race_id: **{len(races_in_results - races_in_laps):,}**")
    out.append(f"- laps はあるが results に無い race_id: {len(races_in_laps - races_in_results):,}")
    out.append("")

    lap_counts = laps.groupby(RACE_KEY)["lap_no"].nunique()
    out.append("### レースあたりの lap 数分布")
    out.append("")
    out.append("| lap 数 | 件数 |")
    out.append("|---:|---:|")
    for k, v in lap_counts.value_counts().sort_index().items():
        out.append(f"| {int(k)} | {int(v):,} |")
    out.append("")
    return "\n".join(out)


def section_payout_balance(dfs: dict[str, pd.DataFrame]) -> str:
    pay = dfs["payouts"]
    out = ["## 9. 払戻データ整合性\n"]

    out.append("### 券種別行数")
    out.append("")
    out.append("| bet_type | bet_name | 行数 |")
    out.append("|---|---|---:|")
    for (bt, bn), v in pay.groupby(["bet_type", "bet_name"]).size().sort_values(ascending=False).items():
        out.append(f"| {bt} | {bn} | {int(v):,} |")
    out.append("")

    out.append("### refund 異常値")
    out.append("")
    out.append(f"- refund NULL: {pay['refund'].isna().sum():,}")
    out.append(f"- refund = 0: {(pay['refund'] == 0).sum():,}")
    out.append(f"- refund ≥ 100,000 (10万円): {(pay['refund'] >= 100000).sum():,}")
    out.append(f"- refund 中央値: {pay['refund'].median():.0f}")
    out.append(f"- refund 最大値: {pay['refund'].max():.0f}")
    out.append("")
    return "\n".join(out)


def main() -> None:
    REPORTS_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    out_path = REPORTS_DIR / f"data_quality_{today}.md"

    print("Loading CSVs...", file=sys.stderr)
    dfs = load_all()
    print(f"Loaded {len(dfs)} CSVs", file=sys.stderr)

    sections = [
        f"# データ品質レポート ({today})\n",
        f"対象: `data/*.csv`(backfill 2021-04-26 〜 2026-04-26、完了 2026-04-27)\n",
        section_row_counts(dfs),
        section_date_coverage(dfs),
        section_cross_consistency(dfs),
        section_duplicates(dfs),
        section_null_rates(dfs),
        section_finish_distribution(dfs),
        section_odds_anomalies(dfs),
        section_lap_consistency(dfs),
        section_payout_balance(dfs),
    ]
    report = "\n".join(sections)
    out_path.write_text(report, encoding="utf-8")
    print(f"Wrote {out_path}", file=sys.stderr)
    print(f"Report size: {out_path.stat().st_size / 1024:.1f} KB", file=sys.stderr)


if __name__ == "__main__":
    main()
