"""(B) DQ 追加調査:
  - lap データ無し race_id (353 件) の年月・場・accident 分布
  - odds NULL (3,805 行) の年月・場分布

reports/dq_followup_<date>.md に書き出す。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

VENUE_NAMES = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
RACE_KEY = ["race_date", "place_code", "race_no"]


def load_csvs() -> dict[str, pd.DataFrame]:
    out = {}
    for name in ["race_results", "race_laps", "odds_summary", "race_entries"]:
        df = pd.read_csv(DATA_DIR / f"{name}.csv", dtype={"player_code": str}, low_memory=False)
        df["race_date"] = pd.to_datetime(df["race_date"])
        out[name] = df
    return out


def section_lap_missing(dfs: dict[str, pd.DataFrame]) -> str:
    out = ["## 1. lap データが無い race の分布\n"]

    results = dfs["race_results"]
    laps = dfs["race_laps"]

    races_in_results = results[RACE_KEY].drop_duplicates()
    races_in_laps = laps[RACE_KEY].drop_duplicates()

    merged = races_in_results.merge(
        races_in_laps.assign(_has_lap=1), on=RACE_KEY, how="left"
    )
    missing = merged[merged["_has_lap"].isna()].drop(columns=["_has_lap"])
    out.append(f"- lap 欠損 race 数: **{len(missing):,}** / 全 {len(races_in_results):,} ({len(missing)/len(races_in_results)*100:.2f}%)\n")

    # 年月別
    missing["year_month"] = missing["race_date"].dt.to_period("M").astype(str)
    by_ym = missing.groupby("year_month").size().sort_index()

    out.append("### 年月別 lap 欠損 race 数(上位 20)\n")
    out.append("| 年月 | 欠損 race 数 |")
    out.append("|---|---:|")
    for ym, v in by_ym.sort_values(ascending=False).head(20).items():
        out.append(f"| {ym} | {int(v)} |")
    out.append("")

    out.append("### 場別 lap 欠損 race 数\n")
    out.append("| 場 | 欠損数 |")
    out.append("|---|---:|")
    for code, name in VENUE_NAMES.items():
        n = (missing["place_code"] == code).sum()
        out.append(f"| {name} (pc={code}) | {int(n)} |")
    out.append("")

    # 欠損 race の results 側で accident_code が異常に多いか?
    missing_keys = set(map(tuple, missing[RACE_KEY].to_numpy()))
    results_in_missing = results[
        results.set_index(RACE_KEY).index.isin(missing_keys)
    ]
    out.append("### lap 欠損 race の結果側 accident_code 分布\n")
    out.append(f"対象 race の総 result 行: {len(results_in_missing):,}")
    out.append("")
    n_accident = results_in_missing["accident_code"].notna().sum()
    out.append(f"- accident_code が NULL でない件数: {int(n_accident):,} ({n_accident/len(results_in_missing)*100:.1f}%)")
    out.append(f"- 比較: 全データの accident NULL 以外率: {results['accident_code'].notna().mean()*100:.2f}%")
    out.append("")

    # 着順が NULL の率(失格率)
    n_order_null = results_in_missing["order"].isna().sum()
    out.append(f"- 着順 NULL 率: {n_order_null/len(results_in_missing)*100:.1f}% (全データ: {results['order'].isna().mean()*100:.2f}%)")
    out.append("")

    # 年で分けて、古い時期に偏ってるか
    out.append("### 年別 race 数 vs lap 欠損数\n")
    out.append("| 年 | 全 race | lap 欠損 | 欠損率 |")
    out.append("|---|---:|---:|---:|")
    for year in sorted(races_in_results["race_date"].dt.year.unique()):
        total = (races_in_results["race_date"].dt.year == year).sum()
        miss = (missing["race_date"].dt.year == year).sum()
        rate = miss / total * 100 if total else 0
        out.append(f"| {year} | {int(total):,} | {int(miss):,} | {rate:.2f}% |")
    out.append("")

    return "\n".join(out)


def section_odds_null(dfs: dict[str, pd.DataFrame]) -> str:
    out = ["## 2. odds が NULL の行の分布\n"]
    odds = dfs["odds_summary"]
    null_odds = odds[odds["win_odds"].isna()].copy()
    out.append(f"- win_odds NULL 行: **{len(null_odds):,}** / 全 {len(odds):,} ({len(null_odds)/len(odds)*100:.2f}%)\n")

    # 年月別
    null_odds["year_month"] = null_odds["race_date"].dt.to_period("M").astype(str)
    out.append("### 年月別 NULL 行数(上位 20)\n")
    out.append("| 年月 | 件数 |")
    out.append("|---|---:|")
    for ym, v in null_odds.groupby("year_month").size().sort_values(ascending=False).head(20).items():
        out.append(f"| {ym} | {int(v)} |")
    out.append("")

    # 場別
    out.append("### 場別 NULL 行数\n")
    out.append("| 場 | NULL 件数 | 全行数 | NULL 率 |")
    out.append("|---|---:|---:|---:|")
    for code, name in VENUE_NAMES.items():
        n_null = (null_odds["place_code"] == code).sum()
        n_total = (odds["place_code"] == code).sum()
        rate = n_null / n_total * 100 if n_total else 0
        out.append(f"| {name} (pc={code}) | {int(n_null):,} | {int(n_total):,} | {rate:.2f}% |")
    out.append("")

    # 同じ車両が entries に存在するか?
    entries = dfs["race_entries"]
    entries_keys = set(map(tuple, entries[RACE_KEY + ["car_no"]].to_numpy()))
    null_keys = list(map(tuple, null_odds[RACE_KEY + ["car_no"]].to_numpy()))
    in_entries = sum(1 for k in null_keys if k in entries_keys)
    out.append(f"### 整合性: NULL odds の car_no が entries に存在する率\n")
    out.append(f"- {in_entries:,} / {len(null_keys):,} ({in_entries/len(null_keys)*100:.2f}%)")
    out.append(f"  → 残り {len(null_keys)-in_entries:,} は entries にも無い(欠車・出走取消の可能性大)")
    out.append("")

    # entries 側に absent (欠車) フラグが立っているか?
    null_with_entries = null_odds.merge(entries[RACE_KEY + ["car_no", "absent", "trial_run_time"]], on=RACE_KEY + ["car_no"], how="left")
    n_absent = null_with_entries["absent"].notna().sum()
    n_no_trial = null_with_entries["trial_run_time"].isna().sum()
    out.append(f"- NULL odds 行のうち entries.absent != NULL: {int(n_absent):,}")
    out.append(f"- NULL odds 行のうち entries.trial_run_time が NULL: {int(n_no_trial):,}")
    out.append("")

    # 年別
    out.append("### 年別 odds NULL 率\n")
    out.append("| 年 | 全行数 | NULL 件数 | NULL 率 |")
    out.append("|---|---:|---:|---:|")
    for year in sorted(odds["race_date"].dt.year.unique()):
        total = (odds["race_date"].dt.year == year).sum()
        nul = (null_odds["race_date"].dt.year == year).sum()
        rate = nul / total * 100 if total else 0
        out.append(f"| {year} | {int(total):,} | {int(nul):,} | {rate:.2f}% |")
    out.append("")
    return "\n".join(out)


def main() -> None:
    REPORTS_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    out_path = REPORTS_DIR / f"dq_followup_{today}.md"

    print("Loading CSVs...", file=sys.stderr)
    dfs = load_csvs()

    sections = [
        f"# DQ 追加調査レポート ({today})\n",
        "lap 欠損 353 race と odds NULL 3,805 行の原因切り分け。\n",
        section_lap_missing(dfs),
        section_odds_null(dfs),
    ]
    out_path.write_text("\n".join(sections), encoding="utf-8")
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
