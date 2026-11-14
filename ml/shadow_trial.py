"""試走ありの直前モデル (シャドー) — 学習・発火時の記録・週次評価 (2026-11-12)
=======================================================================
背景 (統合監視 精度検証 2026-11-12 Auto C/E):
  本番モデル (ml/train_production.py) は MORNING_EXCLUDE で試走タイム関連を学習から外している。
  発火は発走 3〜4 分前で、odds_ts (7/5〜11/12, 2,779 R) では T-4 / T-3 の時点で全車の試走タイムが
  揃っていたレースが 99.5%。walk-forward (2022-04〜2026-04, 217,578 行) では試走ありが
  logloss 0.5108 / なし 0.5200 (毎年一貫して -0.009)。前向き 4 か月の簡易加算でも -0.0037。

本モジュール (シャドー = 記録だけ。購入判断・EV・発注・本番の pred / pred_calib には一切使わない):
  - 学習: 本番と同じデータ・同じ分割・同じパラメータで、試走関連だけを特徴量に戻したモデルを
    data/shadow_trial_model.lgb / data/shadow_trial_meta.json に保存する (本番の production_* は触らない)。
    週次再学習 (scripts/weekly_retrain.py) の最後の任意ステップ。失敗しても本番の採用に影響しない。
  - 記録: daily_predict の発火で、本番の予測と並べて data/shadow_trial_preds.csv に追記する。
    R ごとに試走タイムが発火時点で取れていたか (trial_status = ok / partial / none) も記録する。
    呼び出しは購入処理の後。例外はすべて握りつぶしてログに残す。
  - 評価: 同じ R (試走 ok・結果確定) で 本番 pred (校正前) と シャドー pred の logloss を比べ、
    試走の取得率と合わせて週次メールに 1 行ずつ。4 週分たまったら「本番化の判断材料が揃った」。
    本番化そのものは利用者の判断。

pythonw 起動でも落ちないよう print は使わない。
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
MODEL_PATH = DATA / "shadow_trial_model.lgb"
META_PATH = DATA / "shadow_trial_meta.json"
LOG_PATH = DATA / "shadow_trial_preds.csv"

TRIAL_COLS = ["trial_run_time", "has_trial", "trial_diff_min", "trial_diff_mean",
              "race_trial_min", "race_trial_mean"]
LOG_FIELDS = ["race_date", "place_code", "race_no", "car_no", "pred_prod", "pred_calib_prod",
              "pred_shadow", "trial_run_time", "trial_status", "shadow_trained_at",
              "captured_at", "elapsed_ms"]
WEEKS_FOR_DECISION = 4

logger = logging.getLogger(__name__)
_CACHE: dict = {"mtime": None, "model": None, "meta": None}


# ---------------------------------------------------------------- 学習
def shadow_exclude() -> set[str]:
    """本番の除外 (BASE ∪ MORNING) から試走関連だけを戻した除外集合。"""
    from ml import train_production as tp
    return (tp.BASE_EXCLUDE | tp.MORNING_EXCLUDE) - set(TRIAL_COLS)


def prepare(df: pd.DataFrame, target_col: str = "target_top3"):
    from ml import train_production as tp
    mask = (df["is_absent"] == 0) & (df["finished"] == 1)
    df = df[mask].copy()
    feature_cols = sorted(c for c in df.columns if c not in shadow_exclude())
    X = df[feature_cols].copy()
    for c in tp.CATEGORICAL:
        if c in X.columns:
            X[c] = X[c].astype("category")
    for c in X.select_dtypes(include=["object", "string"]).columns:
        X[c] = X[c].astype("category")
    y = df[target_col].astype(int)
    return X, y, df, feature_cols


def train_and_save(model_path: Path | None = None, meta_path: Path | None = None) -> dict:
    """本番と同じ分割 (train_production.train_full) でシャドーを学習し保存する。meta を返す。"""
    import lightgbm as lgb
    from sklearn.metrics import log_loss, roc_auc_score
    from ml import train_production as tp
    model_path = Path(model_path or MODEL_PATH)
    meta_path = Path(meta_path or META_PATH)
    df = tp.load_features()
    X, y, kept, cols = prepare(df)
    # 本番 (採用ゲート v3) と同じ分割: 学習 < q0.90 / 早期終了 q0.90〜0.95 / 比較 q0.95〜
    model, metrics, _X_es, _y_es, X_cmp, y_cmp = tp.train_full_v3(X, y, kept["race_date"])
    # 比較用の窓で本番 (現役) モデルを採点し直して並べる (記録用。本番の学習終端より後か注意)
    prod = {}
    try:
        pmeta = json.loads((tp.DATA / "production_meta.json").read_text(encoding="utf-8"))
        booster = lgb.Booster(model_file=str(tp.DATA / "production_model.lgb"))
        pv = booster.predict(X_cmp[pmeta["feature_columns"]])
        prod = {"cmp_auc": float(roc_auc_score(y_cmp, pv)),
                "cmp_logloss": float(log_loss(y_cmp, np.clip(pv, 1e-6, 1 - 1e-6))),
                "trained_at": pmeta.get("trained_at"),
                "production_train_end": tp.champion_train_end(pmeta, kept["race_date"]),
                "note": ("本番の学習終端が比較窓の開始より前なら公平な比較。後なら本番に有利 (in-sample)")}
    except Exception as e:  # noqa: BLE001
        prod = {"error": f"{type(e).__name__}: {e}"}
    tmp = model_path.with_suffix(".lgb.tmp")
    model.save_model(str(tmp))
    tmp.replace(model_path)
    meta = {
        "trained_at": dt.datetime.now().isoformat(timespec="seconds"),
        "purpose": "shadow only (試走あり直前モデル)。購入判断・EV・発注には使わない",
        "feature_columns": cols,
        "categorical": [c for c in tp.CATEGORICAL if c in cols],
        "trial_features": TRIAL_COLS,
        "data_date_range": {"min": kept["race_date"].min().date().isoformat(),
                            "max": kept["race_date"].max().date().isoformat()},
        "train_metrics": metrics,
        "production_on_same_val": prod,
    }
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(meta_path)
    return meta


# ---------------------------------------------------------------- 発火時の記録
def _load(model_path: Path, meta_path: Path):
    import lightgbm as lgb
    mt = (model_path.stat().st_mtime, meta_path.stat().st_mtime)
    if _CACHE["mtime"] != mt:
        _CACHE["model"] = lgb.Booster(model_file=str(model_path))
        _CACHE["meta"] = json.loads(meta_path.read_text(encoding="utf-8"))
        _CACHE["mtime"] = mt
    return _CACHE["model"], _CACHE["meta"]


def trial_status(feat: pd.DataFrame) -> str:
    """発火時点の試走タイムの取得状況: ok (全車) / partial / none。"""
    if "trial_run_time" not in feat.columns or feat.empty:
        return "none"
    t = pd.to_numeric(feat["trial_run_time"], errors="coerce")
    ok = (t > 0).fillna(False)
    if ok.all():
        return "ok"
    return "partial" if ok.any() else "none"


def record(frames: list[pd.DataFrame], align_features, *, model_path: Path | None = None,
           meta_path: Path | None = None, log_path: Path | None = None) -> dict:
    """本番の予測済み frames (predict_race の戻り) にシャドーの予測を並べて記録する。

    align_features: daily_predict.align_features (列合わせ・カテゴリ化を本番と同じにする)。
    例外は外に出さない (呼び出し側も try で包むが二重に守る)。本番の frames は変更しない。
    戻り値: {"n_races", "elapsed_ms", "status"} (計測とテスト用)。
    """
    t0 = time.perf_counter()
    out = {"n_races": 0, "elapsed_ms": 0.0, "status": "ok"}
    try:
        model_path = Path(model_path or MODEL_PATH)
        meta_path = Path(meta_path or META_PATH)
        log_path = Path(log_path or LOG_PATH)
        model = meta = None
        if model_path.exists() and meta_path.exists():
            try:
                model, meta = _load(model_path, meta_path)
            except Exception as e:  # noqa: BLE001
                out["status"] = f"model_load_failed: {type(e).__name__}"
        else:
            out["status"] = "no_model"
        rows = []
        now = dt.datetime.now().isoformat(timespec="seconds")
        for feat in frames or []:
            if feat is None or feat.empty:
                continue
            t1 = time.perf_counter()
            st = trial_status(feat)
            ps = np.full(len(feat), np.nan)
            if model is not None:
                try:
                    ps = model.predict(align_features(feat.copy(), meta))
                except Exception as e:  # noqa: BLE001 — 特徴量欠け等。本番には影響させない
                    out["status"] = f"predict_failed: {type(e).__name__}"
            ms = (time.perf_counter() - t1) * 1000
            for i, (_, r) in enumerate(feat.iterrows()):
                rows.append({
                    "race_date": pd.Timestamp(r.get("race_date")).date().isoformat(),
                    "place_code": int(r.get("place_code")), "race_no": int(r.get("race_no")),
                    "car_no": int(r.get("car_no")),
                    "pred_prod": _f(r.get("pred")), "pred_calib_prod": _f(r.get("pred_calib")),
                    "pred_shadow": _f(ps[i]), "trial_run_time": _f(r.get("trial_run_time")),
                    "trial_status": st,
                    "shadow_trained_at": (meta or {}).get("trained_at", ""),
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


def _f(v) -> str:
    try:
        x = float(v)
        return "" if not np.isfinite(x) else f"{x:.6f}"
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------- 週次評価
def evaluate(log_path: Path | None = None, results_path: Path | None = None) -> dict:
    """同じ R (試走 ok・結果確定・両方の予測あり) で 本番 pred と シャドー pred の logloss。"""
    log_path = Path(log_path or LOG_PATH)
    results_path = Path(results_path or (DATA / "race_results.csv"))
    res: dict = {"n_logged_races": 0}
    if not log_path.exists():
        return res
    lg = pd.read_csv(log_path)
    if lg.empty:
        return res
    k = ["race_date", "place_code", "race_no"]
    lg = lg.drop_duplicates(k + ["car_no"], keep="first")   # 同じ R の再発火は初回だけ
    races = lg.drop_duplicates(k)
    res["n_logged_races"] = int(len(races))
    res["trial_ok_rate"] = float((races["trial_status"] == "ok").mean())
    res["trial_counts"] = races["trial_status"].value_counts().to_dict()
    res["date_min"], res["date_max"] = str(races["race_date"].min()), str(races["race_date"].max())
    if not results_path.exists():
        return res
    rr = pd.read_csv(results_path, usecols=k + ["car_no", "order"])
    rr = rr[rr["race_date"] >= res["date_min"]]
    d = lg.merge(rr, on=k + ["car_no"], how="inner")
    d = d[(d["trial_status"] == "ok") & d["pred_shadow"].notna() & d["pred_prod"].notna()]
    d = d[d.groupby(k)["order"].transform(lambda s: (s <= 3).sum()) >= 3]
    if d.empty:
        res["n_eval_races"] = 0
        return res
    y = (d["order"] <= 3).astype(float).to_numpy()

    def ll(p):
        p = np.clip(p.astype(float).to_numpy(), 1e-6, 1 - 1e-6)
        return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    weeks = pd.to_datetime(d["race_date"]).dt.to_period("W-SUN").astype(str)
    res.update({
        "n_eval_races": int(d.groupby(k).ngroups), "n_eval_rows": int(len(d)),
        "eval_date_min": str(d["race_date"].min()), "eval_date_max": str(d["race_date"].max()),
        "ll_prod": ll(d["pred_prod"]), "ll_shadow": ll(d["pred_shadow"]),
        "ll_prod_calib": ll(d["pred_calib_prod"]) if d["pred_calib_prod"].notna().all() else None,
        "n_weeks": int(weeks.nunique()),
        "elapsed_ms_median": float(pd.to_numeric(lg["elapsed_ms"], errors="coerce").median()),
    })
    return res


def weekly_lines(res: dict | None = None) -> list[str]:
    """週次メール用 (text/HTML 共通)。失敗しても例外を出さない。"""
    try:
        res = res if res is not None else evaluate()
        if not res.get("n_logged_races"):
            return ["試走ありシャドー: 記録なし (発火時の記録は daily_predict から。モデル未学習なら"
                    " 予測は空で試走の取得状況だけ記録)"]
        c = res.get("trial_counts", {})
        L = [f"試走の取得率 (発火時点、{res['date_min']}〜{res['date_max']}): "
             f"{res['trial_ok_rate'] * 100:.1f}% ({c.get('ok', 0)}/{res['n_logged_races']} R、"
             f"一部欠け {c.get('partial', 0)} R・なし {c.get('none', 0)} R)"]
        if res.get("n_eval_races"):
            line = (f"試走ありシャドー vs 試走なし本番系統 (同じ {res['n_eval_races']} R / "
                    f"{res['eval_date_min']}〜{res['eval_date_max']}、3 着内の logloss): "
                    f"試走なし系統 {res['ll_prod']:.4f} → シャドー {res['ll_shadow']:.4f} "
                    f"(差 {res['ll_shadow'] - res['ll_prod']:+.4f}、低いほど良い。どちらも校正前)")
            if res["n_weeks"] >= WEEKS_FOR_DECISION:
                line += f" — {res['n_weeks']} 週分たまり、本番化の判断材料が揃った (判断は利用者)"
            else:
                line += f" — {res['n_weeks']}/{WEEKS_FOR_DECISION} 週"
            L.append(line)
        else:
            L.append("試走ありシャドー vs 試走なし本番系統: 結果確定済みで比較できる R はまだありません")
        return L
    except Exception as e:  # noqa: BLE001
        return [f"試走ありシャドー: 集計失敗 ({type(e).__name__})"]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    meta = train_and_save()
    logger.info("shadow trial model saved: cmp_auc=%.4f cmp_logloss=%.4f (比較窓 %s〜, 本番: %s)",
                meta["train_metrics"]["cmp_auc"], meta["train_metrics"]["cmp_logloss"],
                meta["train_metrics"]["cmp_start"], meta.get("production_on_same_val"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
