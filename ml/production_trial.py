"""試走入り本番モデル (第 2 系統) — 学習・校正・採用ゲート・推論の切替 (2026-11-14)
=======================================================================
利用者の指示「オートは試走も入れて」(2026-11-13) を受けた実装。設計は親の決定どおり:

- **2 系統**: 既存の `data/production_*` (試走なし) はそのまま残し、フォールバックとして使い続ける。
  試走入りは別のファイル群 `data/production_trial_{model.lgb,calib.pkl,meta.json}` に、本番と同じ手順
  (同じデータ・分割 train_full_v3・パラメータ・isotonic 校正・採用ゲート v3) で学習・保存する。
  特徴量は試走シャドーと同じ定義 (本番の除外から試走関連 6 列だけを戻す。ai_expect_code は除外のまま)。
- **校正**: 本番と同じく walk-forward の OOF 予測 (このモジュールの walkforward が
  `data/walkforward_predictions_trial_top3.parquet` を作る) に isotonic を当てはめる。
- **切替**: `src/strategy_config.PRODUCTION_USE_TRIAL` (既定 False)。オフの間は推論で何もしない
  (`apply_trial_model` は入力をそのまま返す) ので、本番の予測値・EV・発注は従来と同じ。
- **推論**: オンで、その R の試走が全車揃っている (trial_status == ok) ときだけ、試走入りモデル + その校正で
  pred / pred_calib を置き換える。試走欠け・ファイル無し/破損・特徴量不足・予測例外は **R 単位で**
  試走なしにフォールバックする (R の中でモデルを混ぜない)。どちらを使ったか・理由は frame.attrs に残し、
  daily_predict が購入処理の後に `data/model_used_preds.csv` へ記録する (既存 CSV の列は変えない)。
- **週次再学習**: scripts/weekly_retrain.py の任意ステップ。失敗しても試走なし系統 (フォールバック) の
  学習・採用には影響しない。採用ゲート v3 は試走入り系統の現役 (production_trial_*) と比べる。

pythonw 起動でも落ちないよう print は使わない。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import pickle
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODEL_PATH = DATA / "production_trial_model.lgb"
CALIB_PATH = DATA / "production_trial_calib.pkl"
META_PATH = DATA / "production_trial_meta.json"
PREV_META_PATH = DATA / "production_trial_meta.prev.json"
REJECTED_META_PATH = DATA / "production_trial_meta.rejected.json"
WF_PATH = DATA / "walkforward_predictions_trial_top3.parquet"
HISTORY_PATH = DATA / "retrain_history_trial.csv"
USED_LOG = DATA / "model_used_preds.csv"
USED_FIELDS = ["race_date", "place_code", "race_no", "car_no", "model_used", "fallback_reason",
               "pred_trial", "pred_calib_trial", "pred_notrial", "pred_calib_notrial",
               "trial_status", "trial_model_trained_at", "captured_at"]

logger = logging.getLogger(__name__)
_CACHE: dict = {}


def use_trial_enabled() -> bool:
    """切替スイッチ (src/strategy_config.PRODUCTION_USE_TRIAL)。読めなければ False (安全側)。"""
    try:
        from src import strategy_config as sc
        return bool(getattr(sc, "PRODUCTION_USE_TRIAL", False))
    except Exception:
        return False


# ---------------------------------------------------------------- walk-forward (校正の元)
def walkforward(min_train_months: int = 12, out_path: Path | None = None) -> Path:
    """walkforward_morning と同じ手順で、試走入りの OOF 予測を作る。"""
    from ml import shadow_trial as st
    from ml import walkforward_morning as wm
    df = wm.load()
    X, y, kept, _cols = st.prepare(df)
    months = sorted(kept["year_month"].unique())
    frames = []
    for tm in months[min_train_months:]:
        is_tr = (kept["year_month"] < tm).values
        is_te = (kept["year_month"] == tm).values
        d_tr = kept.loc[is_tr, "race_date"]
        is_val = (d_tr >= d_tr.quantile(0.9)).values
        X_trf, y_trf = X[is_tr], y[is_tr]
        model = wm._train(X_trf[~is_val], y_trf[~is_val], X_trf[is_val], y_trf[is_val])
        te = kept.loc[is_te, ["race_date", "place_code", "race_no", "car_no", "target_top3"]].copy()
        te["pred"] = model.predict(X[is_te], num_iteration=model.best_iteration)
        te["test_month"] = tm
        frames.append(te)
        logger.info("[trial wf] %s n=%d best_iter=%d", tm, len(te), model.best_iteration)
    out = Path(out_path or WF_PATH)
    pd.concat(frames, ignore_index=True).to_parquet(out, index=False)
    return out


def fit_calibration(pq: Path | None = None):
    """本番 (train_production.fit_calibration) と同じ方法: 全 OOF で isotonic、honest 分割で評価。"""
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import log_loss, roc_auc_score
    df = pd.read_parquet(pq or WF_PATH)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(df["pred"].values, df["target_top3"].values)
    cutoff = "2024-04"
    cal, ev = df[df["test_month"] < cutoff], df[df["test_month"] >= cutoff]
    honest = {"cutoff": cutoff}
    if len(cal) and len(ev):
        ih = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        ih.fit(cal["pred"].values, cal["target_top3"].values)
        pe = ih.transform(ev["pred"].values)
        honest.update({"n_calib": int(len(cal)), "n_eval": int(len(ev)),
                       "raw_logloss": float(log_loss(ev["target_top3"], ev["pred"].clip(1e-6, 1 - 1e-6))),
                       "calib_logloss": float(log_loss(ev["target_top3"], np.clip(pe, 1e-6, 1 - 1e-6))),
                       "raw_auc": float(roc_auc_score(ev["target_top3"], ev["pred"])),
                       "calib_auc": float(roc_auc_score(ev["target_top3"], pe))})
    return iso, {"n_calib": int(len(df)), "honest_split": honest}


# ---------------------------------------------------------------- 学習 + 採用ゲート v3
def _champion_on(X, y, meta: dict | None) -> dict | None:
    import lightgbm as lgb
    from sklearn.metrics import log_loss, roc_auc_score
    if meta is None or not MODEL_PATH.exists() or len(X) == 0:
        return None
    try:
        b = lgb.Booster(model_file=str(MODEL_PATH))
        p = b.predict(X[meta["feature_columns"]])
        return {"auc": float(roc_auc_score(y, p)), "logloss": float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6)))}
    except Exception as e:  # noqa: BLE001
        logger.warning("試走入り現役の採点に失敗: %s", e)
        return None


def _roi_veto(model, meta: dict | None, X_cmp, df_cmp) -> dict:
    import lightgbm as lgb
    from ml import train_production as tp
    from ml.roi_gate import evaluate_roi_gate
    try:
        if meta is None or not MODEL_PATH.exists():
            return {"verdict": "SKIP", "reason": "試走入り現役なし (初回) のため ROI 比較不能"}
        champ = lgb.Booster(model_file=str(MODEL_PATH))
        d = df_cmp[tp.ROI_RACE_KEYS + ["car_no"]].copy().reset_index(drop=True)
        d["p_champion"] = champ.predict(X_cmp[meta["feature_columns"]])
        d["p_candidate"] = model.predict(X_cmp, num_iteration=model.best_iteration)
        pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
        return evaluate_roi_gate(tp.place_payout_frame(d, pay), race_keys=tp.ROI_RACE_KEYS)
    except Exception as e:  # noqa: BLE001
        return {"verdict": "ERROR", "reason": f"ROI 拒否権 実行失敗 ({type(e).__name__}: {e})"}


def _append_history(row: dict) -> None:
    from ml import train_production as tp
    try:
        new = not HISTORY_PATH.exists()
        with HISTORY_PATH.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(tp.HISTORY_HEADER)
            w.writerow([row.get(k, "") for k in tp.HISTORY_HEADER])
    except Exception as e:  # noqa: BLE001
        logger.warning("retrain_history_trial 追記失敗: %s", e)


def _load_meta() -> dict | None:
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8")) if META_PATH.exists() else None
    except Exception:
        return None


def train(force: bool = False) -> dict:
    """試走入り系統を学習し、試走入り現役と v3 で比べて採用なら保存する。結果 dict を返す。"""
    from ml import shadow_trial as st
    from ml import train_production as tp
    df = tp.load_features()
    X, y, kept, cols = st.prepare(df)
    model, m, X_es, y_es, X_cmp, y_cmp = tp.train_full_v3(X, y, kept["race_date"])
    iso, calib = fit_calibration()
    old = _load_meta()
    age = tp._model_age_days(old)
    champ_end = tp.champion_train_end(old, kept["race_date"])
    champ = _champion_on(X_cmp, y_cmp, old) if (champ_end and champ_end < m["cmp_start"]) else None
    extra = {"cmp_start": m["cmp_start"], "cmp_n": m["n_cmp"], "cand_ll_cmp": round(m["cmp_logloss"], 6),
             "cand_auc_cmp": round(m["cmp_auc"], 5), "roi_bet_type": "fns"}
    if old is None:
        verdict, reason = "OK", "試走入り系統の初回採用"
        extra["gate_mode"] = "first"
    elif champ is not None:
        extra.update({"gate_mode": "v3_compare_window", "champ_ll_cmp": round(champ["logloss"], 6),
                      "champ_auc_cmp": round(champ["auc"], 5)})
        verdict, reason = tp._should_adopt_v3({"auc": m["cmp_auc"], "logloss": m["cmp_logloss"]},
                                              champ, m, old, age, force=force)
    else:
        extra["gate_mode"] = "v3_unavailable"
        verdict, reason = ("OK", "--force") if force else (
            "WARN", f"試走入り現役を比較窓で採点できない (現役学習終端 {champ_end}) — 採用して要確認")
    roi = {"verdict": "SKIP", "reason": "精度ゲート NG"} if verdict == "NG" else \
        _roi_veto(model, old, X_cmp, kept.loc[X_cmp.index])
    if roi.get("verdict") == "NG" and not force:
        verdict, reason = "NG", f"[ROI拒否権] {roi.get('reason', '')}"
    _append_history({"timestamp": dt.datetime.now().isoformat(timespec="seconds"), "verdict": verdict,
                     "reason": reason, "valid_auc": m["valid_auc"], "valid_logloss": m["valid_logloss"],
                     "best_iteration": m["best_iteration"],
                     "target_definition_version": tp.TARGET_DEFINITION_VERSION,
                     **tp._roi_history_fields(roi), **extra})
    meta = {
        "trained_at": dt.datetime.now().isoformat(timespec="seconds"),
        "system": "trial", "target": "target_top3",
        "target_definition_version": tp.TARGET_DEFINITION_VERSION,
        "n_features": len(cols), "feature_columns": cols,
        "categorical": [c for c in tp.CATEGORICAL if c in cols],
        "data_date_range": {"min": kept["race_date"].min().date().isoformat(),
                            "max": kept["race_date"].max().date().isoformat()},
        "train_metrics": m, "calib_metrics": calib,
        "quality_gate_verdict": verdict, "adoption_reason": reason, "roi_gate": roi,
    }
    if verdict == "NG":
        REJECTED_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"adopted": False, **meta}
    if META_PATH.exists():
        shutil.copy2(META_PATH, PREV_META_PATH)
    for path, writer in ((MODEL_PATH, lambda p: model.save_model(str(p))),
                         (CALIB_PATH, lambda p: p.write_bytes(pickle.dumps(iso)))):
        tmp = path.with_suffix(path.suffix + ".tmp")
        writer(tmp)
        tmp.replace(path)
    tmp = META_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(META_PATH)
    return {"adopted": True, **meta}


# ---------------------------------------------------------------- 推論の切替
def _load_trial():
    import lightgbm as lgb
    mt = tuple(p.stat().st_mtime for p in (MODEL_PATH, CALIB_PATH, META_PATH))
    c = _CACHE.get("trial")
    if c is None or c[0] != mt:
        with CALIB_PATH.open("rb") as f:
            iso = pickle.load(f)
        c = (mt, lgb.Booster(model_file=str(MODEL_PATH)), iso,
             json.loads(META_PATH.read_text(encoding="utf-8")))
        _CACHE["trial"] = c
    return c[1], c[2], c[3]


def apply_trial_model(feat: pd.DataFrame, align_features, *, enabled: bool | None = None) -> pd.DataFrame:
    """フラグがオンで R の試走が全車揃うとき、pred / pred_calib を試走入り系統の値で置き換える。

    feat: 試走なし本番で pred / pred_calib を計算済みの 1 R 分。オフなら何もせずそのまま返す。
    置き換えは R 単位 (全車まとめて)。失敗したら feat の値は触らずに返す (フォールバック)。
    attrs: model_used ("trial" / "notrial")、fallback_reason、notrial_pred / notrial_calib (試走なしの値)、
    trial_pred / trial_calib (試走入りの値。計算できた場合)、trial_model_trained_at。
    """
    on = use_trial_enabled() if enabled is None else enabled
    if not on:
        return feat
    from ml import shadow_trial as st
    feat.attrs["model_used"] = "notrial"
    feat.attrs["notrial_pred"] = feat["pred"].to_numpy(copy=True)
    feat.attrs["notrial_calib"] = feat["pred_calib"].to_numpy(copy=True)
    try:
        status = st.trial_status(feat)
        feat.attrs["trial_status"] = status
        if not all(p.exists() for p in (MODEL_PATH, CALIB_PATH, META_PATH)):
            feat.attrs["fallback_reason"] = "試走入りモデル/校正ファイルなし"
            return feat
        model, iso, meta = _load_trial()
        X = align_features(feat.copy(), meta)
        p = np.asarray(model.predict(X), dtype=float)
        pc = np.asarray(iso.transform(p), dtype=float)
        if len(p) != len(feat) or not np.all(np.isfinite(p)) or not np.all(np.isfinite(pc)):
            feat.attrs["fallback_reason"] = "試走入りの予測が不正 (長さ/NaN)"
            return feat
        feat.attrs["trial_pred"], feat.attrs["trial_calib"] = p, pc
        feat.attrs["trial_model_trained_at"] = meta.get("trained_at", "")
        if status != "ok":
            feat.attrs["fallback_reason"] = f"試走が揃っていない ({status})"
            return feat
        feat["pred"] = p
        feat["pred_calib"] = pc
        feat.attrs["model_used"] = "trial"
        feat.attrs["fallback_reason"] = ""
        return feat
    except Exception as e:  # noqa: BLE001 — どんな失敗でも試走なしの値のまま
        feat.attrs["fallback_reason"] = f"例外 {type(e).__name__}: {e}"[:200]
        return feat


def notrial_view(feat: pd.DataFrame) -> pd.DataFrame:
    """シャドー記録用: pred / pred_calib を試走なし本番の値に戻した写し (列の意味を保つ)。"""
    if feat.attrs.get("model_used") != "trial":
        return feat
    v = feat.copy()
    v["pred"] = feat.attrs["notrial_pred"]
    v["pred_calib"] = feat.attrs["notrial_calib"]
    return v


def record_used(frames: list[pd.DataFrame], *, log_path: Path | None = None) -> dict:
    """R ごとにどちらを使ったかと、両系統の予測を記録 (フラグオンの R だけ。購入処理の後に呼ぶ)。"""
    t0 = time.perf_counter()
    out = {"n_races": 0, "status": "ok"}
    try:
        from ml import shadow_trial as st
        rows = []
        now = dt.datetime.now().isoformat(timespec="seconds")
        for feat in frames or []:
            if feat is None or feat.empty or "model_used" not in feat.attrs:
                continue
            a = feat.attrs
            n = len(feat)
            tp_, tc_ = a.get("trial_pred", [np.nan] * n), a.get("trial_calib", [np.nan] * n)
            for i, (_, r) in enumerate(feat.iterrows()):
                rows.append({
                    "race_date": pd.Timestamp(r.get("race_date")).date().isoformat(),
                    "place_code": int(r.get("place_code")), "race_no": int(r.get("race_no")),
                    "car_no": int(r.get("car_no")), "model_used": a["model_used"],
                    "fallback_reason": a.get("fallback_reason", ""),
                    "pred_trial": st._f(tp_[i]), "pred_calib_trial": st._f(tc_[i]),
                    "pred_notrial": st._f(a["notrial_pred"][i]),
                    "pred_calib_notrial": st._f(a["notrial_calib"][i]),
                    "trial_status": a.get("trial_status", ""),
                    "trial_model_trained_at": a.get("trial_model_trained_at", ""), "captured_at": now})
            out["n_races"] += 1
        if rows:
            p = Path(log_path or USED_LOG)
            new = not p.exists() or p.stat().st_size == 0
            with p.open("a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=USED_FIELDS)
                if new:
                    w.writeheader()
                w.writerows(rows)
    except Exception as e:  # noqa: BLE001
        out["status"] = f"record_failed: {type(e).__name__}: {e}"
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


# ---------------------------------------------------------------- 週次
def evaluate(log_path: Path | None = None, results_path: Path | None = None) -> dict:
    p = Path(log_path or USED_LOG)
    res: dict = {"n_races": 0}
    if not p.exists():
        return res
    k = ["race_date", "place_code", "race_no"]
    lg = pd.read_csv(p).drop_duplicates(k + ["car_no"], keep="first")
    races = lg.drop_duplicates(k)
    res.update({"n_races": int(len(races)), "n_trial": int((races["model_used"] == "trial").sum()),
                "n_fallback": int((races["model_used"] != "trial").sum()),
                "date_min": str(races["race_date"].min()), "date_max": str(races["race_date"].max())})
    rp = Path(results_path or (DATA / "race_results.csv"))
    if not rp.exists():
        return res
    rr = pd.read_csv(rp, usecols=k + ["car_no", "order"])
    d = lg.merge(rr[rr["race_date"] >= res["date_min"]], on=k + ["car_no"], how="inner")
    d = d[d["pred_calib_trial"].notna() & d["pred_calib_notrial"].notna()]
    d = d[d.groupby(k)["order"].transform(lambda s: (s <= 3).sum()) >= 3]
    if d.empty:
        return res
    y = (d["order"] <= 3).astype(float).to_numpy()

    def ll(c):
        q = np.clip(d[c].astype(float).to_numpy(), 1e-6, 1 - 1e-6)
        return float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))
    res.update({"n_eval_races": int(d.groupby(k).ngroups), "ll_trial": ll("pred_calib_trial"),
                "ll_notrial": ll("pred_calib_notrial")})
    return res


def weekly_lines(res: dict | None = None) -> list[str]:
    try:
        if not use_trial_enabled():
            return ["本番モデル = 試走なし (PRODUCTION_USE_TRIAL=False。試走入り系統は学習のみ)"]
        res = res if res is not None else evaluate()
        if not res.get("n_races"):
            return ["本番モデル = 試走入り (PRODUCTION_USE_TRIAL=True)、記録はまだありません"]
        s = (f"本番モデル = 試走入り ({res['date_min']}〜{res['date_max']}: {res['n_races']} R 中 "
             f"試走入り {res['n_trial']} R・フォールバック {res['n_fallback']} R)")
        if res.get("n_eval_races"):
            s += (f" / 同じ {res['n_eval_races']} R の 3 着内 logloss (校正後) 試走入り {res['ll_trial']:.4f}"
                  f" ・ 試走なし {res['ll_notrial']:.4f}")
        return [s]
    except Exception as e:  # noqa: BLE001
        return [f"本番モデルの系統: 集計失敗 ({type(e).__name__})"]


def main() -> int:
    ap = argparse.ArgumentParser(description="試走入り本番モデル (第 2 系統) の walk-forward・学習・採用")
    ap.add_argument("--skip-walkforward", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t0 = time.time()
    if not args.skip_walkforward:
        walkforward()
        logger.info("trial walk-forward done %.0fs", time.time() - t0)
    r = train(force=args.force)
    logger.info("trial system: adopted=%s verdict=%s reason=%s (%.0fs)", r["adopted"],
                r["quality_gate_verdict"], r["adoption_reason"], time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
