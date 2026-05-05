"""ML予想ロジックの軽量監査。

メインプロジェクトは変更せず、既存 data/*.csv/parquet を読み取り、
結果を Opinion/ml_logic_audit/audit_results.md にだけ書く。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = ROOT / "Opinion" / "ml_logic_audit" / "audit_results.md"
RACE_KEY = ["race_date", "place_code", "race_no"]
CAR_KEY = RACE_KEY + ["car_no"]
CALIB_CUTOFF = "2024-04"
THR = 1.50


def _load_csv(name: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / name, low_memory=False)
    if "race_date" in df.columns:
        df["race_date"] = pd.to_datetime(df["race_date"])
    return df


def duplicate_report() -> list[str]:
    lines = ["## キー重複チェック", ""]
    specs = [
        ("race_entries.csv", CAR_KEY),
        ("race_stats.csv", CAR_KEY),
        ("odds_summary.csv", CAR_KEY),
        ("race_results.csv", CAR_KEY),
        ("ml_features.parquet", CAR_KEY),
    ]
    for name, key in specs:
        if name.endswith(".parquet"):
            df = pd.read_parquet(DATA / name)
            df["race_date"] = pd.to_datetime(df["race_date"])
        else:
            df = _load_csv(name)
        dup = int(df.duplicated(key).sum())
        lines.append(f"- `{name}` key={key}: rows={len(df):,}, duplicated={dup:,}")
    return lines


def target_integrity_report() -> list[str]:
    feat = pd.read_parquet(DATA / "ml_features.parquet")
    feat["race_date"] = pd.to_datetime(feat["race_date"])
    active = feat[(feat["is_absent"] == 0) & (feat["finished"] == 1)].copy()
    by_race = active.groupby(RACE_KEY).agg(
        n_cars=("car_no", "size"),
        top3=("target_top3", "sum"),
        wins=("target_win", "sum"),
    ).reset_index()
    bad_top3 = by_race[by_race["top3"] != np.minimum(3, by_race["n_cars"])]
    bad_wins = by_race[by_race["wins"] != 1]
    lines = ["", "## target 整合性", ""]
    lines.append(f"- finished/non-absent races: {len(by_race):,}")
    lines.append(f"- top3 数が期待値と違う R: {len(bad_top3):,}")
    lines.append(f"- 1着数が 1 ではない R: {len(bad_wins):,}")
    if len(bad_top3):
        lines.append("")
        lines.append("top3 異常サンプル:")
        lines.append(bad_top3.head(10).to_markdown(index=False))
    if len(bad_wins):
        lines.append("")
        lines.append("win 異常サンプル:")
        lines.append(bad_wins.head(10).to_markdown(index=False))
    return lines


def prior_stat_sanity_report() -> list[str]:
    """API の this_year_win_count / win_count_90d が未来込みでないかを粗く見る。"""
    feat = pd.read_parquet(DATA / "ml_features.parquet")
    feat["race_date"] = pd.to_datetime(feat["race_date"])
    feat["player_code"] = feat["player_code"].astype(str).str.zfill(4)
    feat = feat[(feat["finished"] == 1) & (feat["is_absent"] == 0)].copy()
    feat["year"] = feat["race_date"].dt.year

    base = feat[[
        *CAR_KEY,
        "player_code",
        "year",
        "target_win",
        "this_year_win_count",
        "win_count_90d",
    ]].copy()
    base = base.sort_values(["player_code", "race_date", "race_no"])

    # 同一年・同選手の「そのレース前までの勝数」。
    base["calc_prior_year_wins"] = (
        base.groupby(["player_code", "year"])["target_win"].cumsum()
        - base["target_win"]
    )

    # dataset が年初から揃う 2022 年以降だけを見る。
    chk = base[base["year"] >= 2022].copy()
    chk["year_win_diff"] = chk["this_year_win_count"] - chk["calc_prior_year_wins"]
    exact = float((chk["year_win_diff"] == 0).mean()) if len(chk) else float("nan")
    near = float(chk["year_win_diff"].abs().le(1).mean()) if len(chk) else float("nan")

    # 90日勝数も簡易再計算。API定義差が出やすいので参考扱い。
    parts = []
    for _, g in base.groupby("player_code", sort=False):
        g = g.sort_values(["race_date", "race_no"]).copy()
        dates = g["race_date"].to_numpy()
        wins = g["target_win"].to_numpy()
        vals = []
        for i, d in enumerate(dates):
            lo = pd.Timestamp(d) - pd.Timedelta(days=90)
            mask = (dates < d) & (dates >= np.datetime64(lo))
            vals.append(int(wins[:i][mask[:i]].sum()))
        g["calc_prior_90d_wins"] = vals
        parts.append(g)
    chk90 = pd.concat(parts, ignore_index=True)
    chk90 = chk90[chk90["year"] >= 2022].copy()
    chk90["win90_diff"] = chk90["win_count_90d"] - chk90["calc_prior_90d_wins"]
    exact90 = float((chk90["win90_diff"] == 0).mean()) if len(chk90) else float("nan")
    near90 = float(chk90["win90_diff"].abs().le(1).mean()) if len(chk90) else float("nan")

    lines = ["", "## 過去成績特徴の未来情報サニティ", ""]
    lines.append(
        "- `this_year_win_count` vs データ内で再計算した同年・レース前勝数 "
        f"(2022+): exact={exact:.3f}, ±1={near:.3f}, n={len(chk):,}"
    )
    lines.append(
        "- `win_count_90d` vs データ内で再計算した直近90日・レース前勝数 "
        f"(参考): exact={exact90:.3f}, ±1={near90:.3f}, n={len(chk90):,}"
    )
    worst = chk.reindex(chk["year_win_diff"].abs().sort_values(ascending=False).index).head(10)
    lines.append("")
    lines.append("this_year_win_count 差分が大きいサンプル:")
    lines.append(worst[[
        "race_date", "place_code", "race_no", "player_code",
        "this_year_win_count", "calc_prior_year_wins", "year_win_diff",
    ]].to_markdown(index=False))
    lines.append("")
    lines.append(
        "注: API側の集計定義とローカル再計算は完全一致しない可能性があるため、"
        "ここはリーク確定ではなく、強い疑いを探す検査。"
    )
    return lines


def oof_prediction_report() -> list[str]:
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = _load_csv("odds_summary.csv")
    payouts = _load_csv("payouts.csv")
    fns = payouts[payouts["bet_type"] == "fns"][
        RACE_KEY + ["car_no_1", "refund"]
    ].rename(columns={"car_no_1": "car_no", "refund": "fns_refund"})

    dup = int(preds.duplicated(CAR_KEY).sum())
    months = sorted(preds["test_month"].unique())
    df = preds.merge(
        odds[RACE_KEY + ["car_no", "place_odds_min", "place_odds_max"]],
        on=CAR_KEY,
        how="left",
    ).dropna(subset=["place_odds_min", "place_odds_max"])
    calib = df[df["test_month"] < CALIB_CUTOFF]
    ev = df[df["test_month"] >= CALIB_CUTOFF].copy()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    ev["pred_calib"] = iso.transform(ev["pred"].values)
    ev["ev_avg_calib"] = ev["pred_calib"] * (
        ev["place_odds_min"] + ev["place_odds_max"]
    ) / 2
    ev["pred_rank"] = ev.groupby(RACE_KEY)["pred_calib"].rank(method="min", ascending=False)
    picks = ev[(ev["pred_rank"] == 1) & (ev["ev_avg_calib"] >= THR)].copy()
    picks = picks.merge(fns, on=CAR_KEY, how="left")
    picks["payout"] = picks["fns_refund"].fillna(0)
    roi = picks["payout"].sum() / (len(picks) * 100) if len(picks) else float("nan")
    hit = (picks["payout"] > 0).mean() if len(picks) else float("nan")

    lines = ["", "## OOF 予測・EV評価の再集計", ""]
    lines.append(f"- OOF rows={len(preds):,}, months={months[0]}..{months[-1]}, duplicated CAR_KEY={dup:,}")
    lines.append(f"- calib cutoff={CALIB_CUTOFF}, calib rows={len(calib):,}, eval rows={len(ev):,}")
    lines.append(f"- baseline top1 fns EV>={THR:.2f}: n={len(picks):,}, ROI={roi*100:.1f}%, hit={hit*100:.1f}%")
    by_month = picks.groupby("test_month").agg(
        n=("payout", "size"),
        roi=("payout", lambda s: s.sum() / (len(s) * 100)),
        hit=("payout", lambda s: (s > 0).mean()),
    ).reset_index()
    lines.append("")
    lines.append("月次サンプル:")
    lines.append(by_month.head(10).to_markdown(index=False))
    lines.append("")
    lines.append("月次末尾:")
    lines.append(by_month.tail(10).to_markdown(index=False))
    return lines


def production_meta_report() -> list[str]:
    meta_path = DATA / "production_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    features = meta.get("feature_columns", [])
    suspicious = [
        c for c in features
        if any(tok in c.lower() for tok in ["odds", "expect", "trial", "order", "target", "refund", "payout"])
    ]
    lines = ["", "## production meta", ""]
    lines.append(f"- trained_at: {meta.get('trained_at')}")
    lines.append(f"- data range: {meta.get('data_date_range')}")
    lines.append(f"- n_features: {meta.get('n_features')}")
    lines.append(f"- leak-sensitive feature names: {suspicious}")
    lines.append("")
    lines.append("train_metrics:")
    lines.append("```json")
    lines.append(json.dumps(meta.get("train_metrics", {}), ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("calib_metrics:")
    lines.append("```json")
    lines.append(json.dumps(meta.get("calib_metrics", {}), ensure_ascii=False, indent=2))
    lines.append("```")
    return lines


def main() -> None:
    lines = [
        "# MLロジック軽量監査結果",
        "",
        "- 目的: 予想ロジックのリーク/過大評価/測定バグの当たりを付ける",
        "- 制約: メインプロジェクトは未変更。既存データ読み取りのみ。再学習なし。",
    ]
    for fn in [
        duplicate_report,
        target_integrity_report,
        prior_stat_sanity_report,
        oof_prediction_report,
        production_meta_report,
    ]:
        lines.extend(fn())
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
