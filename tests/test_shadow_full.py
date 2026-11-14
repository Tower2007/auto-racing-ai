"""全期間シャドー (ml/shadow_full.py) のテスト (2026-11-13)。

重点: モデルが無い・壊れている・特徴量が欠ける・予測が例外・書き込めない、のどれでも本番の予測値
(pred / pred_calib) が変わらず例外が外に出ないこと。前向き評価は記録した行だけで比べること。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ml import shadow_full as sf  # noqa: E402


def _align(df, meta):
    out = pd.DataFrame(index=df.index)
    for c in meta["feature_columns"]:
        out[c] = df[c] if c in df.columns else np.nan
    return out


def _race(race_no=1, trial=(3.40, 3.41, 3.43)):
    n = len(trial)
    return pd.DataFrame({
        "race_date": pd.to_datetime(["2026-11-01"] * n), "place_code": [3] * n,
        "race_no": [race_no] * n, "car_no": list(range(1, n + 1)),
        "trial_run_time": list(trial), "win_odds": [2.0, 5.0, 9.0][:n],
        "pred": [0.7, 0.4, 0.2][:n], "pred_calib": [0.69, 0.41, 0.2][:n],
    })


@pytest.fixture()
def models(tmp_path, monkeypatch):
    import lightgbm as lgb
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"trial_run_time": rng.normal(3.4, 0.05, 400), "win_odds": rng.uniform(1, 20, 400)})
    y = (rng.random(400) < 0.4).astype(int)
    paths = {}
    for kind, cols in (("full", ["win_odds"]), ("full_trial", ["trial_run_time", "win_odds"])):
        b = lgb.train({"objective": "binary", "verbosity": -1, "num_leaves": 4}, lgb.Dataset(X[cols], y), 5)
        mp, jp = tmp_path / f"{kind}.lgb", tmp_path / f"{kind}.json"
        b.save_model(str(mp))
        jp.write_text(json.dumps({"feature_columns": cols, "trained_at": "2026-11-13T00:00:00"}),
                      encoding="utf-8")
        paths[kind] = (mp, jp)
    monkeypatch.setattr(sf, "MODELS", paths)
    sf._CACHE.clear()
    return paths


def test_record_ok_and_prod_unchanged(tmp_path, models):
    f = _race()
    before = f.copy()
    r = sf.record([f, _race(2)], _align, log_path=tmp_path / "log.csv")
    assert r["status"] == "ok" and r["n_races"] == 2
    pd.testing.assert_frame_equal(f, before)
    lg = pd.read_csv(tmp_path / "log.csv")
    assert list(lg.columns) == sf.LOG_FIELDS and len(lg) == 6
    assert lg[["pred_full", "pred_full_trial"]].notna().all().all()


@pytest.mark.parametrize("case", ["no_model", "broken", "missing_feature", "predict_raises", "unwritable"])
def test_record_never_raises(tmp_path, models, case):
    f = _race(trial=(3.4, None, 0.0))
    before = f.copy()
    log = tmp_path / "log.csv"
    align = _align
    mp, jp = models["full"]
    if case == "no_model":
        mp.unlink()
    elif case == "broken":
        mp.write_text("not a model", encoding="utf-8")
    elif case == "missing_feature":
        jp.write_text(json.dumps({"feature_columns": ["nope"]}), encoding="utf-8")
    elif case == "predict_raises":
        def align(df, meta):
            raise KeyError("x")
    elif case == "unwritable":
        log = tmp_path
    r = sf.record([f], align, log_path=log)
    pd.testing.assert_frame_equal(f, before)
    assert "elapsed_ms" in r
    expect = {"no_model": "no_model:full", "broken": "model_load_failed:full",
              "predict_raises": "predict_failed", "unwritable": "record_failed"}
    if case in expect:
        assert r["status"].startswith(expect[case])
    if case in ("no_model", "broken"):
        lg = pd.read_csv(log)
        assert lg["pred_full"].isna().all() and lg["pred_full_trial"].notna().all()


def _fixture(tmp_path, n_weeks):
    k = ["race_date", "place_code", "race_no"]
    full, trial, res = [], [], []
    rng = np.random.default_rng(2)
    for w in range(n_weeks):
        for r in range(8):
            day = (pd.Timestamp("2026-11-16") + pd.Timedelta(days=7 * w + r % 4)).date().isoformat()
            order = rng.permutation(8) + 1
            for c in range(8):
                t = order[c] <= 3
                base = {"race_date": day, "place_code": 5, "race_no": r + 1, "car_no": c + 1}
                full.append({**base, "pred_prod": 0.55 if t else 0.35, "pred_full": 0.65 if t else 0.3,
                             "pred_full_trial": 0.8 if t else 0.2, "trial_status": "ok",
                             "full_trained_at": "x", "full_trial_trained_at": "x",
                             "captured_at": "x", "elapsed_ms": 90})
                trial.append({**base, "pred_shadow": 0.7 if t else 0.25})
                res.append({**base, "order": order[c]})
    pd.DataFrame(full).to_csv(tmp_path / "full.csv", index=False)
    pd.DataFrame(trial).to_csv(tmp_path / "trial.csv", index=False)
    pd.DataFrame(res).to_csv(tmp_path / "res.csv", index=False)


def test_evaluate_forward_only_and_lines(tmp_path):
    _fixture(tmp_path, 4)
    r = sf.evaluate(tmp_path / "full.csv", tmp_path / "trial.csv", tmp_path / "res.csv")
    assert r["n_eval_races"] == 32 and r["n_weeks"] == 4
    assert r["ll_full_trial"] < r["ll_shadow"] < r["ll_full"] < r["ll_prod"]
    L = sf.weekly_lines(r)
    assert "本番" in L[0] and "全期間" in L[0] and "判断材料が揃った" in L[0]
    _fixture(tmp_path, 1)
    assert "1/4 週" in sf.weekly_lines(sf.evaluate(tmp_path / "full.csv", tmp_path / "trial.csv",
                                                    tmp_path / "res.csv"))[0]
    assert "記録なし" in sf.weekly_lines({"n_logged_races": 0})[0]


def test_wiring_weekly_retrain_and_daily_predict():
    import scripts.weekly_retrain as wr
    assert ("ml.shadow_full" in [m for m, *_ in wr.OPTIONAL_STEPS])
    assert "ml.shadow_full" not in [m for m, *_ in wr.STEPS]   # 本番の必須ステップには入れない
    src = (ROOT / "daily_predict.py").read_text(encoding="utf-8")
    i_hook = src.index("_sf.record(_sv, align_features)")
    assert src.index("auto_buy.run_auto_buy(candidates)") < i_hook
    block = src[src.rindex("try:", 0, i_hook):i_hook + 400]
    assert "except Exception" in block


def test_params_match_production_training():
    """複写したパラメータが本番 (train_full_v3) と食い違っていないこと。"""
    import inspect
    from ml import train_production as tp
    src = inspect.getsource(tp.train_full_v3)
    for k, v in sf.PARAMS.items():
        if k in ("objective", "verbosity", "seed"):
            continue
        assert f'"{k}": {v}' in src, k
