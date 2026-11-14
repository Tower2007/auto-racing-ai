"""全期間 (直近まで) で学習したシャドーモデル — 学習・発火時の記録・週次評価 (2026-11-13)
=======================================================================
背景 (精度検証 2026-11-12 の発見):
  本番 (ml/train_production.py) は日付の 90% 分位より前の行だけで学習し、末尾 10% は早期打ち切り
  (と v3 の比較窓) にしか使わない。推論用に全データで学ぶ「再学習」の工程が無いため、直近 約 6 か月が
  モデルに入っていない (11/08 版: 学習 2021-04-26〜2026-05-04 / 263,735 行、末尾 2026-05-05〜11-06 /
  29,454 行は早期打ち切りのみ)。CHANGELOG・コメントに「意図的に取り置く」記述は無く、初版 (2026-04-29,
  17dc238) の「末尾 10% を validation にして early stopping」のまま。hokkaido は best_iteration 本数で
  全データ再学習した推論用フルモデルを別に持つ (weekly_retrain.py の MODEL_FULL_PATH)。

方針 (親の決定): 本番の学習方法は変えない。全期間版をシャドーとして並走させ、前向きの記録だけで比べる。
  - 木の本数: 本番と同じ手順 (train_full_v3 の早期打ち切り) で決まった best_iteration をそのまま使う。
    擬似実験 (学習終端をずらして全期間再学習) で、行数比で増やした本数 (×約 1.11) との差は
    logloss ±0.0001 と無く、hokkaido と同じ「best_iteration そのまま」を採る。
  - 2 本: 全期間版 (本番と同じ特徴量) と 試走あり×全期間版。保存先は本番・試走シャドーとも別ファイル。
  - 記録は data/shadow_full_preds.csv (別ファイル。既存の shadow_trial_preds.csv の読み手を壊さない)。
  - 全期間版は過去の窓を学習済みなので、過去データでの比較はできない。**前向きに記録した分だけ**で比べる。
  購入判断・EV・発注・本番の pred / pred_calib には一切使わない。pythonw 起動でも落ちないよう print は使わない。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODELS = {
    "full": (DATA / "shadow_full_model.lgb", DATA / "shadow_full_meta.json"),
    "full_trial": (DATA / "shadow_full_trial_model.lgb", DATA / "shadow_full_trial_meta.json"),
}
LOG_PATH = DATA / "shadow_full_preds.csv"
LOG_FIELDS = ["race_date", "place_code", "race_no", "car_no", "pred_prod", "pred_full",
              "pred_full_trial", "trial_status", "full_trained_at", "full_trial_trained_at",
              "captured_at", "elapsed_ms"]
WEEKS_FOR_DECISION = 4

# train_production.train_full_v3 と同じパラメータ (本番の学習手順は変えないため複写。変えるときは両方)
PARAMS = {"objective": "binary", "learning_rate": 0.05, "num_leaves": 63,
          "feature_fraction": 0.85, "bagging_fraction": 0.85, "bagging_freq": 5,
          "min_data_in_leaf": 50, "verbosity": -1, "seed": 42}

logger = logging.getLogger(__name__)
_CACHE: dict = {}


# ---------------------------------------------------------------- 学習
def _train_one(kind: str, df: pd.DataFrame) -> dict:
    import lightgbm as lgb
    from ml import shadow_trial as st
    from ml import train_production as tp
    prep = tp.prepare if kind == "full" else st.prepare
    X, y, kept, cols = prep(df)
    dates = kept["race_date"]
    _m, metrics, *_ = tp.train_full_v3(X, y, dates)       # 本数を決めるだけ (本番と同じ手順)
    rounds = max(int(metrics["best_iteration"]), 1)
    model = lgb.train(PARAMS, lgb.Dataset(X, label=y, categorical_feature="auto"),
                      num_boost_round=rounds)
    mp, jp = MODELS[kind]
    tmp = mp.with_suffix(".lgb.tmp")
    model.save_model(str(tmp))
    tmp.replace(mp)
    meta = {
        "trained_at": dt.datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "purpose": "shadow only (全期間で学習)。購入判断・EV・発注には使わない",
        "feature_columns": cols,
        "categorical": [c for c in tp.CATEGORICAL if c in cols],
        "num_boost_round": rounds,
        "rounds_rule": "train_full_v3 の best_iteration をそのまま (行数比の増量はしない)",
        "train_rows": int(len(X)),
        "data_date_range": {"min": dates.min().date().isoformat(), "max": dates.max().date().isoformat()},
        "split_model_metrics": metrics,
    }
    tmp = jp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(jp)
    return meta


def train_and_save(kinds=("full", "full_trial")) -> dict:
    """全期間版を学習して保存。1 本の失敗で他を止めない。{kind: meta or エラー文字列}。"""
    from ml import train_production as tp
    df = tp.load_features()
    out = {}
    for k in kinds:
        try:
            out[k] = _train_one(k, df)
        except Exception as e:  # noqa: BLE001
            out[k] = f"{type(e).__name__}: {e}"
            logger.warning("shadow_full %s 学習失敗: %s", k, e)
    return out


# ---------------------------------------------------------------- 発火時の記録
def _load(kind: str):
    import lightgbm as lgb
    mp, jp = MODELS[kind]
    if not (mp.exists() and jp.exists()):
        return None, None
    mt = (mp.stat().st_mtime, jp.stat().st_mtime)
    c = _CACHE.get(kind)
    if c is None or c[0] != mt:
        c = (mt, lgb.Booster(model_file=str(mp)), json.loads(jp.read_text(encoding="utf-8")))
        _CACHE[kind] = c
    return c[1], c[2]


def record(frames: list[pd.DataFrame], align_features, *, log_path: Path | None = None) -> dict:
    """本番の予測済み frames に全期間版 2 本の予測を並べて記録する。例外は外に出さない。"""
    from ml import shadow_trial as st
    t0 = time.perf_counter()
    out = {"n_races": 0, "elapsed_ms": 0.0, "status": "ok"}
    try:
        log_path = Path(log_path or LOG_PATH)
        models = {}
        for k in MODELS:
            try:
                m, meta = _load(k)
                if m is None:
                    out["status"] = f"no_model:{k}"
                else:
                    models[k] = (m, meta)
            except Exception as e:  # noqa: BLE001
                out["status"] = f"model_load_failed:{k}:{type(e).__name__}"
        rows = []
        now = dt.datetime.now().isoformat(timespec="seconds")
        for feat in frames or []:
            if feat is None or feat.empty:
                continue
            t1 = time.perf_counter()
            preds = {}
            for k in MODELS:
                p = np.full(len(feat), np.nan)
                if k in models:
                    try:
                        p = models[k][0].predict(align_features(feat.copy(), models[k][1]))
                    except Exception as e:  # noqa: BLE001
                        out["status"] = f"predict_failed:{k}:{type(e).__name__}"
                preds[k] = p
            ms = (time.perf_counter() - t1) * 1000
            ts = st.trial_status(feat)
            for i, (_, r) in enumerate(feat.iterrows()):
                rows.append({
                    "race_date": pd.Timestamp(r.get("race_date")).date().isoformat(),
                    "place_code": int(r.get("place_code")), "race_no": int(r.get("race_no")),
                    "car_no": int(r.get("car_no")), "pred_prod": st._f(r.get("pred")),
                    "pred_full": st._f(preds["full"][i]), "pred_full_trial": st._f(preds["full_trial"][i]),
                    "trial_status": ts,
                    "full_trained_at": (models.get("full", (None, {}))[1] or {}).get("trained_at", ""),
                    "full_trial_trained_at": (models.get("full_trial", (None, {}))[1] or {}).get("trained_at", ""),
                    "captured_at": now, "elapsed_ms": round(ms, 1),
                })
            out["n_races"] += 1
        if rows:
            new = not log_path.exists() or log_path.stat().st_size == 0
            with log_path.open("a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
                if new:
                    w.writeheader()
                w.writerows(rows)
    except Exception as e:  # noqa: BLE001
        out["status"] = f"record_failed: {type(e).__name__}: {e}"
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


# ---------------------------------------------------------------- 週次評価 (前向きの記録だけ)
def evaluate(log_path: Path | None = None, trial_log_path: Path | None = None,
             results_path: Path | None = None) -> dict:
    """前向きに記録した同じ行で 本番 / 試走シャドー / 全期間 / 試走×全期間 の logloss。"""
    from ml import shadow_trial as st
    log_path = Path(log_path or LOG_PATH)
    trial_log_path = Path(trial_log_path or st.LOG_PATH)
    results_path = Path(results_path or (DATA / "race_results.csv"))
    res: dict = {"n_logged_races": 0}
    if not log_path.exists():
        return res
    k = ["race_date", "place_code", "race_no"]
    lg = pd.read_csv(log_path).drop_duplicates(k + ["car_no"], keep="first")
    if lg.empty:
        return res
    res["n_logged_races"] = int(lg.drop_duplicates(k).shape[0])
    if trial_log_path.exists():
        tl = pd.read_csv(trial_log_path, usecols=k + ["car_no", "pred_shadow"]).drop_duplicates(
            k + ["car_no"], keep="first")
        lg = lg.merge(tl, on=k + ["car_no"], how="left")
    else:
        lg["pred_shadow"] = np.nan
    if not results_path.exists():
        return res
    rr = pd.read_csv(results_path, usecols=k + ["car_no", "order"])
    rr = rr[rr["race_date"] >= lg["race_date"].min()]
    d = lg.merge(rr, on=k + ["car_no"], how="inner")
    cols = ["pred_prod", "pred_shadow", "pred_full", "pred_full_trial"]
    d = d[(d["trial_status"] == "ok") & d[cols].notna().all(axis=1)]
    d = d[d.groupby(k)["order"].transform(lambda s: (s <= 3).sum()) >= 3]
    if d.empty:
        res["n_eval_races"] = 0
        return res
    y = (d["order"] <= 3).astype(float).to_numpy()

    def ll(c):
        p = np.clip(d[c].astype(float).to_numpy(), 1e-6, 1 - 1e-6)
        return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    weeks = pd.to_datetime(d["race_date"]).dt.to_period("W-SUN").astype(str)
    res.update({"n_eval_races": int(d.groupby(k).ngroups), "n_eval_rows": int(len(d)),
                "eval_date_min": str(d["race_date"].min()), "eval_date_max": str(d["race_date"].max()),
                "n_weeks": int(weeks.nunique()),
                **{f"ll_{c.replace('pred_', '')}": ll(c) for c in cols}})
    return res


def weekly_lines(res: dict | None = None) -> list[str]:
    try:
        res = res if res is not None else evaluate()
        if not res.get("n_logged_races"):
            return ["全期間シャドー: 記録なし (発火時の記録は daily_predict から)"]
        if not res.get("n_eval_races"):
            return [f"全期間シャドー: 記録 {res['n_logged_races']} R、結果確定済みで 4 本とも揃う R はまだありません"]
        line = (f"前向きの同じ {res['n_eval_races']} R ({res['eval_date_min']}〜{res['eval_date_max']}、"
                f"試走あり R のみ、3 着内 logloss・校正前): 試走なし本番系統 {res['ll_prod']:.4f} / 試走シャドー "
                f"{res['ll_shadow']:.4f} / 全期間 {res['ll_full']:.4f} / 試走×全期間 {res['ll_full_trial']:.4f}")
        if res["n_weeks"] >= WEEKS_FOR_DECISION:
            line += f" — {res['n_weeks']} 週分たまり、判断材料が揃った (本番化は利用者の判断)"
        else:
            line += f" — {res['n_weeks']}/{WEEKS_FOR_DECISION} 週"
        return [line]
    except Exception as e:  # noqa: BLE001
        return [f"全期間シャドー: 集計失敗 ({type(e).__name__})"]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = train_and_save()
    for k, v in out.items():
        if isinstance(v, dict):
            logger.info("shadow_full %s saved: rounds=%s rows=%s data %s",
                        k, v["num_boost_round"], v["train_rows"], v["data_date_range"])
        else:
            logger.warning("shadow_full %s failed: %s", k, v)
    return 0 if all(isinstance(v, dict) for v in out.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
