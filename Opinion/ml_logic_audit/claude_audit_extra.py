"""Claude による独立追加監査(Codex 監査の補完)

Codex 監査(2026-05-06)が covered:
  - キー重複(clean)
  - target 整合性(special races 26+13)
  - this_year_win_count vs recalc(exact 10.7%、diff -103 まで観測)
  - OOF 再集計(ROI 132.9%, n=2014)
  - production_meta inspection

Claude が追加で見る角度:
  1. race_stats の temporal leakage (90d/180d 統計が「as of race date」か「latest snapshot」か)
  2. total_win_count 単調性 (同選手で時系列順に増加するか?減少なら closing-style snapshot 疑惑)
  3. Codex が指摘した this_year_win_count 巨大 diff の意味の追究
  4. NaN 系特徴 と target の相関(drop bias)
  5. categorical 列の coverage gap(訓練に出ない車番/場/レース番号がテストに出るか?)
  6. odds_summary の closing-style 確認 (1番人気 win_odds と target_top3 hit rate の乖離)
"""

from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = ROOT / "Opinion" / "ml_logic_audit" / "claude_audit_results.md"

RACE_KEY = ["race_date", "place_code", "race_no"]
CAR_KEY = RACE_KEY + ["car_no"]


def section(title: str) -> list[str]:
    return [f"\n## {title}\n"]


def check_total_win_count_monotonicity() -> list[str]:
    """同選手で race_date 昇順に並べたとき、total_win_count は単調非減少のはず。

    減少が観測されたら、各レコードが「そのレース時点の累積」ではなく
    「データ取得時点の累積」(後日 snapshot)である疑い → temporal leakage
    """
    s = pd.read_csv(DATA / "race_stats.csv", low_memory=False, dtype={"player_code": str})
    s["race_date"] = pd.to_datetime(s["race_date"])
    # 同選手 / 同日 / 同レース でユニーク化(出走毎に 1 行のはず)
    s = s.sort_values(["player_code", "race_date", "race_no"]).reset_index(drop=True)
    s["prev_total"] = s.groupby("player_code")["total_win_count"].shift(1)
    s["delta"] = s["total_win_count"] - s["prev_total"]
    decrease = s[s["delta"] < 0].copy()
    n_pairs = int(s["prev_total"].notna().sum())
    n_dec = len(decrease)
    n_inc_skip = int((s["delta"] > 1).sum())  # +2 以上 = 出走と出走の間に複数勝ったように見える(他場混入は普通)
    lines = section("1. total_win_count 単調性検査")
    lines.append("**仮説**: 同選手の `total_win_count` は時系列で単調非減少のはず。")
    lines.append("減少が出る = 各行が「そのレース時点」ではなく「後日 snapshot」の疑い。")
    lines.append("")
    lines.append(f"- 比較対象ペア数(同一選手の連続出走): {n_pairs:,}")
    lines.append(f"- **減少ペア数 (delta < 0)**: {n_dec:,} ({n_dec/n_pairs*100:.3f}%)")
    lines.append(f"- 参考: 増加 +2 以上のペア数: {n_inc_skip:,}")
    if n_dec > 0:
        lines.append("")
        lines.append("**減少サンプル(先頭 10 行)**:")
        cols = ["player_code", "race_date", "place_code", "race_no", "prev_total", "total_win_count", "delta"]
        lines.append(decrease[cols].head(10).to_markdown(index=False))
    return lines


def check_this_year_reset() -> list[str]:
    """this_year_win_count がいつリセットされるか調査。

    名前は「今年の勝ち数」だが、Codex の差分 -103 を見ると年初に 0 にリセット
    されないか?年末の値が 17 で、データから再計算した同年勝数が 120
    → API は別定義(直近◯ヶ月? or リセット日が異なる?)
    """
    s = pd.read_csv(DATA / "race_stats.csv", low_memory=False, dtype={"player_code": str})
    s["race_date"] = pd.to_datetime(s["race_date"])
    s = s.sort_values(["player_code", "race_date"]).reset_index(drop=True)
    # 同選手で month=1 (1月) のレース時の this_year_win_count 平均
    s["month"] = s["race_date"].dt.month
    s["year"] = s["race_date"].dt.year
    by_month = s.groupby("month")["this_year_win_count"].agg(["mean", "median", "max"]).round(2)
    lines = section("2. this_year_win_count のリセット調査")
    lines.append("**仮説**: 「今年の勝ち数」なら 1 月で 0、12 月で max のはず。")
    lines.append("Codex の差分 -103 (API=17 vs recalc=120) はこの想定と合わない。")
    lines.append("")
    lines.append("**月別の `this_year_win_count` 分布**:")
    lines.append(by_month.to_markdown())
    # Codex が見つけた選手 3307, 2025-12-31 を再現
    target = s[(s["player_code"] == "3307") & (s["year"] == 2025)]
    if not target.empty:
        head = target[["race_date", "race_no", "this_year_win_count", "total_win_count"]].head(3)
        tail = target[["race_date", "race_no", "this_year_win_count", "total_win_count"]].tail(3)
        lines.append("")
        lines.append("**選手 3307 の 2025 年 (年初/年末)**:")
        lines.append("年初:")
        lines.append(head.to_markdown(index=False))
        lines.append("年末:")
        lines.append(tail.to_markdown(index=False))
    return lines


def check_total_win_count_progression() -> list[str]:
    """total_win_count は 通算勝ち数。同選手の年初〜年末で「年内増分」を見る。

    年内増分が 100 を超える選手がいれば「this_year_win_count 17」は破綻。
    """
    s = pd.read_csv(DATA / "race_stats.csv", low_memory=False, dtype={"player_code": str})
    s["race_date"] = pd.to_datetime(s["race_date"])
    s["year"] = s["race_date"].dt.year
    s = s.sort_values(["player_code", "race_date", "race_no"]).reset_index(drop=True)
    by_player_year = s.groupby(["player_code", "year"]).agg(
        first_total=("total_win_count", "first"),
        last_total=("total_win_count", "last"),
        last_this_year=("this_year_win_count", "last"),
        n_races=("race_date", "size"),
    ).reset_index()
    by_player_year["yearly_inc"] = by_player_year["last_total"] - by_player_year["first_total"]
    by_player_year["mismatch"] = by_player_year["yearly_inc"] - by_player_year["last_this_year"]
    big = by_player_year.sort_values("mismatch", ascending=False).head(10)
    lines = section("3. total_win_count 年内増分 vs this_year_win_count")
    lines.append("**仮説**: 1 年での `total_win_count` 増分 ≒ `last_this_year_win_count`")
    lines.append("(年末の最終 race の this_year で、その年の勝数が確定するはず)")
    lines.append("")
    lines.append("**最大 mismatch top10**:")
    lines.append(big.to_markdown(index=False))
    avg_mis = by_player_year["mismatch"].abs().mean()
    lines.append("")
    lines.append(f"- 平均 |mismatch|: {avg_mis:.2f}")
    return lines


def check_drop_nan_bias() -> list[str]:
    """ml_features.parquet で NaN が多い行と target の相関。

    NaN を学習で drop するなら、NaN 行が target 偏っていればバイアス。
    """
    f = pd.read_parquet(DATA / "ml_features.parquet")
    f = f[(f["is_absent"] == 0) & (f["finished"] == 1)].copy()
    f["nan_count"] = f.isna().sum(axis=1)
    by_nan = f.groupby("nan_count").agg(
        n=("target_top3", "size"),
        hit_rate=("target_top3", "mean"),
    ).reset_index().head(15)
    lines = section("4. NaN 数と target_top3 の相関")
    lines.append("**仮説**: NaN 行(=データ欠損)は新人や復帰直後で hit rate が偏る可能性。")
    lines.append("")
    lines.append("**nan_count 別の hit rate**:")
    lines.append(by_nan.to_markdown(index=False))
    return lines


def check_categorical_coverage() -> list[str]:
    """walk-forward の最初の月 vs 最後の月で categorical の値域が変わるか。"""
    f = pd.read_parquet(DATA / "ml_features.parquet")
    f["race_date"] = pd.to_datetime(f["race_date"])
    f["year_month"] = f["race_date"].dt.to_period("M").astype(str)
    f = f[(f["is_absent"] == 0) & (f["finished"] == 1)].copy()
    months = sorted(f["year_month"].unique())
    first_set = f[f["year_month"] == months[0]]
    last_set = f[f["year_month"] == months[-1]]
    lines = section("5. categorical 列の coverage 変化")
    lines.append(f"**最古月** ({months[0]}) vs **最新月** ({months[-1]}) の値域比較:")
    lines.append("")
    cats = ["place_code", "race_no", "car_no", "rank_class", "graduation_code"]
    rows = []
    for c in cats:
        if c not in f.columns:
            continue
        s_first = set(first_set[c].dropna().unique())
        s_last = set(last_set[c].dropna().unique())
        only_first = s_first - s_last
        only_last = s_last - s_first
        rows.append({
            "col": c,
            "n_first": len(s_first),
            "n_last": len(s_last),
            "only_first": str(sorted(only_first))[:60],
            "only_last": str(sorted(only_last))[:60],
        })
    lines.append(pd.DataFrame(rows).to_markdown(index=False))
    return lines


def check_odds_winning_rate() -> list[str]:
    """1番人気 (win_odds 最小) の hit rate vs win_odds 最大の hit rate。

    closing odds なら 1番人気 hit rate がかなり高く、odds 通りの implied
    確率に近いはず。発火時 odds なら、より分散があるはず。
    """
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    res = pd.read_csv(DATA / "race_results.csv", low_memory=False)
    res["race_date"] = pd.to_datetime(res["race_date"])
    res["target_top3"] = ((res["order"] >= 1) & (res["order"] <= 3)).astype(int)
    res["target_win"] = (res["order"] == 1).astype(int)

    df = odds.merge(res[CAR_KEY + ["target_win", "target_top3"]], on=CAR_KEY, how="inner")
    df["win_rank"] = df.groupby(RACE_KEY)["win_odds"].rank(method="min")
    by_rank = df.groupby("win_rank").agg(
        n=("target_win", "size"),
        win_hit=("target_win", "mean"),
        top3_hit=("target_top3", "mean"),
        win_odds_avg=("win_odds", "mean"),
    ).reset_index().round(4)
    lines = section("6. win_odds rank と hit rate の関係 (closing-style 確認)")
    lines.append("**仮説**: 完全な closing odds なら 1番人気の win_hit ≒ 1/win_odds_avg。")
    lines.append("")
    lines.append("**win_rank 別の hit rate vs implied prob**:")
    by_rank["implied_win_prob"] = (1 / by_rank["win_odds_avg"]).round(4)
    by_rank["overround_factor"] = (by_rank["win_hit"] / by_rank["implied_win_prob"]).round(3)
    lines.append(by_rank.to_markdown(index=False))
    return lines


def check_temporal_split_in_walkforward() -> list[str]:
    """walk-forward 予測 parquet で同じ (race_date, place, race, car) の
    pred が複数の test_month から出ていないか?(本来 1 つだけのはず)"""
    p = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    dup_per_car = p.duplicated(CAR_KEY).sum()
    test_months = sorted(p["test_month"].unique())
    p["race_date_month"] = pd.to_datetime(p["race_date"]).dt.to_period("M").astype(str)
    mismatch = p[p["race_date_month"] != p["test_month"]]
    lines = section("7. walk-forward 予測の test_month 整合性")
    lines.append(f"- 予測 parquet rows: {len(p):,}")
    lines.append(f"- CAR_KEY 重複: {dup_per_car:,}")
    lines.append(f"- race_date.month と test_month が不一致: {len(mismatch):,}")
    if len(mismatch):
        lines.append("**警告: race_date と test_month の月不一致 →  leakage 疑惑**")
        lines.append(mismatch.head(10).to_markdown(index=False))
    return lines


def check_categorical_encoding_consistency() -> list[str]:
    """LightGBM の categorical_feature='auto' では、object → category 変換が
    モデルごとに行われる。walkforward は月毎に再訓練するので、各月で
    category codes が違う可能性がある。pred 時のエンコードが train と一致するか?

    ここでは間接的に: pred parquet の pred 値の月別分布が極端に変わらないかで確認。
    """
    p = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    by_month = p.groupby("test_month")["pred"].agg(["mean", "std", "min", "max", "size"]).round(4)
    lines = section("8. 月別 pred 分布の連続性 (categorical encoding の安定性間接確認)")
    lines.append(by_month.to_markdown())
    lines.append("")
    lines.append("**観点**: mean/std が月で大きくジャンプすると、categorical の符号化")
    lines.append("が月毎に変わって意味が壊れている可能性。")
    return lines


def main() -> None:
    lines = [
        "# Claude 独立追加監査結果",
        "",
        "Codex 監査(2026-05-06)を補完する追加角度。メインプロジェクトは未変更。",
        "",
        "監査項目:",
        "1. total_win_count 単調性 (temporal leakage の最重要 sanity)",
        "2. this_year_win_count のリセット挙動調査",
        "3. total_win_count 年内増分 vs this_year_win_count 整合性",
        "4. NaN 数と target_top3 の相関 (drop bias)",
        "5. categorical 値域の月変化",
        "6. win_odds rank と hit rate (closing-style 確認)",
        "7. walk-forward 予測の test_month 整合性",
        "8. 月別 pred 分布の連続性",
    ]
    for fn in [
        check_total_win_count_monotonicity,
        check_this_year_reset,
        check_total_win_count_progression,
        check_drop_nan_bias,
        check_categorical_coverage,
        check_odds_winning_rate,
        check_temporal_split_in_walkforward,
        check_categorical_encoding_consistency,
    ]:
        try:
            lines.extend(fn())
        except Exception as e:
            lines.extend([f"\n## {fn.__name__}\n", f"**FAILED**: {e}"])
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Written: {OUT}")


if __name__ == "__main__":
    main()
