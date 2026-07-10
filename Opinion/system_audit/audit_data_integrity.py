"""2026-07 全体監査用の読み取り専用データ整合性チェック。

実行:
    .venv\\Scripts\\python.exe Opinion/system_audit/audit_data_integrity.py

データ・モデル・運用ログを変更しない。出力を監査メモに転記するための補助。
"""

from __future__ import annotations

import json
from pathlib import Path

import lightgbm as lgb
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"


def read_csv(name: str) -> pd.DataFrame | None:
    path = DATA / name
    if not path.exists():
        print(f"MISSING {name}")
        return None
    try:
        df = pd.read_csv(path)
        print(f"OK {name}: rows={len(df):,} cols={len(df.columns)}")
        return df
    except Exception as exc:
        print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        return None


def key_report(name: str, df: pd.DataFrame | None, key: list[str]) -> None:
    if df is None or not set(key).issubset(df.columns):
        return
    nulls = int(df[key].isna().any(axis=1).sum())
    dupes = int(df.duplicated(key, keep=False).sum())
    print(f"KEY {name}: null_key_rows={nulls:,} duplicate_rows={dupes:,}")


def main() -> None:
    print("=== core CSVs ===")
    entries = read_csv("race_entries.csv")
    stats = read_csv("race_stats.csv")
    results = read_csv("race_results.csv")
    payouts = read_csv("payouts.csv")
    odds = read_csv("odds_summary.csv")
    laps = read_csv("race_laps.csv")

    car_key = ["race_date", "place_code", "race_no", "car_no"]
    race_key = ["race_date", "place_code", "race_no"]
    for name, df in [("entries", entries), ("stats", stats), ("results", results), ("odds", odds)]:
        key_report(name, df, car_key)
    key_report("payouts", payouts, race_key + ["bet_type", "car_no_1", "car_no_2", "car_no_3"])
    key_report("laps", laps, race_key + ["lap_no", "car_no"])

    print("=== core joins / ranges ===")
    if entries is not None:
        for name, df in [("stats", stats), ("results", results), ("odds", odds)]:
            if df is None:
                continue
            left = entries[car_key].drop_duplicates()
            right = df[car_key].drop_duplicates()
            missing = len(left.merge(right, on=car_key, how="left", indicator=True).query("_merge == 'left_only'"))
            extra = len(right.merge(left, on=car_key, how="left", indicator=True).query("_merge == 'left_only'"))
            print(f"JOIN entries->{name}: missing_right={missing:,} extra_right={extra:,}")
        dates = pd.to_datetime(entries["race_date"], errors="coerce")
        print(f"DATE entries: min={dates.min().date()} max={dates.max().date()} bad={dates.isna().sum():,}")

    if results is not None:
        order = pd.to_numeric(results.get("order"), errors="coerce")
        print(
            "RESULTS order: "
            f"null={order.isna().sum():,} top3={(order.between(1, 3)).sum():,} "
            f"out_of_range={(order.notna() & ~order.between(1, 8)).sum():,}"
        )
    if odds is not None:
        for col in ["win_odds", "place_odds_min", "place_odds_max"]:
            if col not in odds:
                continue
            values = pd.to_numeric(odds[col], errors="coerce")
            print(f"ODDS {col}: null={values.isna().sum():,} nonpositive={(values <= 0).sum():,}")
        lo = pd.to_numeric(odds.get("place_odds_min"), errors="coerce")
        hi = pd.to_numeric(odds.get("place_odds_max"), errors="coerce")
        print(f"ODDS fns min>max={(lo > hi).sum():,}")
    if payouts is not None:
        fns = payouts[payouts.get("bet_type").eq("fns")]
        print(f"PAYOUTS fns: rows={len(fns):,} races={len(fns[race_key].drop_duplicates()):,}")

    print("=== production artifacts ===")
    meta_path = DATA / "production_meta.json"
    model_path = DATA / "production_model.lgb"
    if meta_path.exists() and model_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        model = lgb.Booster(model_file=str(model_path))
        meta_features = meta.get("feature_columns", [])
        model_features = model.feature_name()
        print(
            "MODEL meta: "
            f"trained_at={meta.get('trained_at')} target={meta.get('target')} "
            f"target_definition_version={meta.get('target_definition_version')}"
        )
        print(
            "MODEL features: "
            f"meta={len(meta_features)} model={len(model_features)} "
            f"same_order={meta_features == model_features}"
        )
    else:
        print("MISSING production_model or production_meta")

    feat_path = DATA / "ml_features.parquet"
    if feat_path.exists():
        feat = pd.read_parquet(feat_path, columns=["race_date", "place_code", "race_no", "car_no", "is_absent", "finished"])
        finished = feat[(feat["is_absent"] == 0) & (feat["finished"] == 1)]
        print(
            "FEATURES train-filter: "
            f"rows={len(finished):,} "
            f"date_max={pd.to_datetime(feat['race_date']).max().date()}"
        )
        if entries is not None and results is not None:
            joined = entries[car_key + ["absent"]].merge(
                results[car_key + ["order"]], on=car_key, how="inner", validate="one_to_one"
            )
            expected = joined[joined["absent"].isna() & joined["order"].notna()]
            feat_key = finished[car_key].copy()
            feat_key["race_date"] = pd.to_datetime(feat_key["race_date"]).dt.strftime("%Y-%m-%d")
            expected_key = expected[car_key].copy()
            expected_key["race_date"] = expected_key["race_date"].astype(str)
            missing_expected = len(expected_key.merge(feat_key, on=car_key, how="left", indicator=True).query("_merge == 'left_only'"))
            unexpected = len(feat_key.merge(expected_key, on=car_key, how="left", indicator=True).query("_merge == 'left_only'"))
            print(
                "FEATURES finished contract: "
                f"expected_order_notna={len(expected):,} missing={missing_expected:,} unexpected={unexpected:,}"
            )

    print("=== operational logs ===")
    picks = read_csv("daily_predict_picks.csv")
    if picks is not None:
        key_report("daily_predict_picks", picks, car_key)
        sent = pd.to_datetime(picks.get("sent_at"), errors="coerce")
        print(f"PICKS sent_at: bad={sent.isna().sum():,} min={sent.min()} max={sent.max()}")
    snap = read_csv("odds_snapshots.csv")
    if snap is not None:
        key_report("odds_snapshots", snap, car_key)
    prerace = read_csv("odds_combo_prerace.csv")
    if prerace is not None:
        pr_key = race_key + ["bet_type", "car_no_1", "car_no_2", "car_no_3", "target_offset_min"]
        key_report("odds_combo_prerace", prerace, pr_key)
        print(f"PRERACE offsets={sorted(prerace['target_offset_min'].dropna().unique().tolist())}")

    history = read_csv("bet_history.csv")
    detail = read_csv("bet_history_detail.csv")
    if history is not None:
        hkey = ["date", "place_code", "race_no"]
        key_report("bet_history", history, hkey)
        for col in ["bet_amount", "refund_amount", "profit"]:
            values = pd.to_numeric(history.get(col), errors="coerce")
            print(f"BET_HISTORY {col}: null={values.isna().sum():,}")
    if detail is not None:
        dkey = ["date", "place_code", "race_no", "order_id", "bet_type_code", "pack_deme"]
        key_report("bet_history_detail", detail, dkey)


if __name__ == "__main__":
    main()
