"""試走入り本番モデル (第 2 系統, ml/production_trial.py) の切替テスト (2026-11-14)。

重点: フラグがオフなら従来と完全に同じ / オンで試走が揃えば試走入りを使う / 試走欠け・モデル無し・
破損・例外は R 単位でフォールバックし R の中でモデルが混ざらない / EV・候補の作り方は pred_calib の
出どころ以外変わらない / 週次再学習で片方の失敗がもう片方に波及しない。
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ml import production_trial as pt  # noqa: E402


def _align(df, meta):
    out = pd.DataFrame(index=df.index)
    for c in meta["feature_columns"]:
        out[c] = df[c] if c in df.columns else np.nan
    return out


def _race(trial=(3.40, 3.41, 3.43, 3.45)):
    n = len(trial)
    return pd.DataFrame({
        "race_date": pd.to_datetime(["2026-11-01"] * n), "place_code": [3] * n, "race_no": [5] * n,
        "car_no": list(range(1, n + 1)), "trial_run_time": list(trial),
        "win_odds": [2.0, 5.0, 9.0, 20.0][:n],
        "pred": [0.70, 0.40, 0.20, 0.10][:n], "pred_calib": [0.69, 0.41, 0.20, 0.10][:n],
    })


class _Iso:
    """isotonic の代わり (単調・決定的)。"""
    def transform(self, p):
        return np.clip(np.asarray(p) * 0.9 + 0.02, 0, 1)


class _BadIso:
    """壊れた校正 (NaN を返す)。"""
    def transform(self, p):
        return np.full(len(p), np.nan)


@pytest.fixture()
def trial_files(tmp_path, monkeypatch):
    import lightgbm as lgb
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"trial_run_time": rng.normal(3.4, 0.05, 500), "win_odds": rng.uniform(1, 20, 500)})
    y = ((X["win_odds"] < 6) ^ (rng.random(500) < 0.2)).astype(int)
    b = lgb.train({"objective": "binary", "verbosity": -1, "num_leaves": 4}, lgb.Dataset(X, y), 10)
    mp, cp, jp = tmp_path / "m.lgb", tmp_path / "c.pkl", tmp_path / "m.json"
    b.save_model(str(mp))
    cp.write_bytes(pickle.dumps(_Iso()))
    jp.write_text(json.dumps({"feature_columns": ["trial_run_time", "win_odds"],
                              "trained_at": "2026-11-14T00:00:00"}), encoding="utf-8")
    monkeypatch.setattr(pt, "MODEL_PATH", mp)
    monkeypatch.setattr(pt, "CALIB_PATH", cp)
    monkeypatch.setattr(pt, "META_PATH", jp)
    pt._CACHE.clear()
    return mp, cp, jp


def test_flag_is_read_from_strategy_config(monkeypatch):
    """スイッチは src/strategy_config.PRODUCTION_USE_TRIAL をそのまま読む (2026-11-14 から True)。
    False に戻せば即座にオフになる。"""
    from src import strategy_config as sc
    assert isinstance(sc.PRODUCTION_USE_TRIAL, bool)
    monkeypatch.setattr(sc, "PRODUCTION_USE_TRIAL", False)
    assert pt.use_trial_enabled() is False
    monkeypatch.setattr(sc, "PRODUCTION_USE_TRIAL", True)
    assert pt.use_trial_enabled() is True


def test_off_returns_identical(trial_files):
    f = _race()
    before = f.copy()
    out = pt.apply_trial_model(f, _align, enabled=False)
    assert out is f
    pd.testing.assert_frame_equal(out, before)
    assert "model_used" not in out.attrs


def test_on_uses_trial_for_whole_race(trial_files):
    f = _race()
    before = f.copy()
    out = pt.apply_trial_model(f, _align, enabled=True)
    assert out.attrs["model_used"] == "trial" and out.attrs["fallback_reason"] == ""
    assert not np.allclose(out["pred"], before["pred"])           # 全車が試走入りの値
    np.testing.assert_allclose(out["pred_calib"], _Iso().transform(out["pred"]))
    np.testing.assert_allclose(out.attrs["notrial_pred"], before["pred"])
    v = pt.notrial_view(out)                                       # シャドー用の写しは試走なしの値
    pd.testing.assert_series_equal(v["pred"], before["pred"])
    pd.testing.assert_series_equal(v["pred_calib"], before["pred_calib"])


@pytest.mark.parametrize("case", ["trial_missing", "no_file", "broken", "missing_feature",
                                  "predict_raises", "calib_nan"])
def test_on_falls_back_per_race(trial_files, case):
    mp, cp, jp = trial_files
    f = _race(trial=(3.4, None, 3.43, 3.45)) if case == "trial_missing" else _race()
    before = f.copy()
    align = _align
    if case == "no_file":
        cp.unlink()
    elif case == "broken":
        mp.write_text("not a model", encoding="utf-8")
    elif case == "missing_feature":
        jp.write_text(json.dumps({"feature_columns": ["no_such"]}), encoding="utf-8")
    elif case == "predict_raises":
        def align(df, meta):
            raise KeyError("feature")
    elif case == "calib_nan":
        cp.write_bytes(pickle.dumps(_BadIso()))
    out = pt.apply_trial_model(f, align, enabled=True)
    assert out.attrs["model_used"] == "notrial" and out.attrs["fallback_reason"]
    pd.testing.assert_frame_equal(out[["pred", "pred_calib"]], before[["pred", "pred_calib"]])


def _ev_candidates(feat):
    """daily_predict の EV・候補の作り方 (pred_calib × 複勝平均 × 割引、top1 = pred 最大)。"""
    from src.strategy_config import FIRE_EV_DISCOUNT
    f = feat.copy()
    f["place_odds_min"], f["place_odds_max"] = [1.1, 1.8, 3.0, 6.0], [1.5, 2.6, 5.0, 9.0]
    f["ev"] = f["pred_calib"] * (f["place_odds_min"] + f["place_odds_max"]) / 2 * FIRE_EV_DISCOUNT
    f["rank"] = f["pred"].rank(method="min", ascending=False)
    return f


def test_ev_construction_unchanged_except_source(trial_files):
    off = _ev_candidates(pt.apply_trial_model(_race(), _align, enabled=False))
    on_feat = pt.apply_trial_model(_race(), _align, enabled=True)
    on = _ev_candidates(on_feat)
    # 同じ式: EV は pred_calib から作られ、出どころの違いだけが差になる
    from src.strategy_config import FIRE_EV_DISCOUNT
    np.testing.assert_allclose(on["ev"], on_feat["pred_calib"] * (on["place_odds_min"] + on["place_odds_max"])
                               / 2 * FIRE_EV_DISCOUNT)
    np.testing.assert_allclose(off["ev"], _race()["pred_calib"] * (off["place_odds_min"] + off["place_odds_max"])
                               / 2 * FIRE_EV_DISCOUNT)


def test_record_used_and_weekly(tmp_path, trial_files, monkeypatch):
    a = pt.apply_trial_model(_race(), _align, enabled=True)
    b = pt.apply_trial_model(_race(trial=(3.4, None, 3.4, 3.4)), _align, enabled=True)
    b["race_no"] = 6
    off = pt.apply_trial_model(_race(), _align, enabled=False)       # オフの R は記録しない
    r = pt.record_used([a, b, off], log_path=tmp_path / "u.csv")
    assert r["n_races"] == 2 and r["status"] == "ok"
    lg = pd.read_csv(tmp_path / "u.csv")
    assert list(lg.columns) == pt.USED_FIELDS
    assert set(lg.groupby("race_no")["model_used"].nunique()) == {1}   # R の中で混ざらない
    assert lg.groupby("race_no")["model_used"].first().to_dict() == {5: "trial", 6: "notrial"}
    res = pd.DataFrame({"race_date": ["2026-11-01"] * 8, "place_code": [3] * 8,
                        "race_no": [5] * 4 + [6] * 4, "car_no": [1, 2, 3, 4] * 2,
                        "order": [1, 2, 3, 4, 1, 2, 3, 4]})
    res.to_csv(tmp_path / "res.csv", index=False)
    ev = pt.evaluate(tmp_path / "u.csv", tmp_path / "res.csv")
    assert ev["n_races"] == 2 and ev["n_trial"] == 1 and ev["n_fallback"] == 1
    assert ev["n_eval_races"] == 2
    monkeypatch.setattr(pt, "use_trial_enabled", lambda: True)
    assert "フォールバック 1 R" in pt.weekly_lines(ev)[0]
    monkeypatch.setattr(pt, "use_trial_enabled", lambda: False)
    assert "試走なし" in pt.weekly_lines(ev)[0]


def test_record_used_never_raises(tmp_path, trial_files):
    a = pt.apply_trial_model(_race(), _align, enabled=True)
    r = pt.record_used([a], log_path=tmp_path)    # ディレクトリ = 書けない
    assert r["status"].startswith("record_failed")


def test_weekly_retrain_isolation(monkeypatch):
    import scripts.weekly_retrain as wr
    # 試走入り (任意) の失敗は rc=0 のまま、試走なし系統の必須ステップは全部走る
    calls = []
    monkeypatch.setattr(wr, "run_step", lambda m, *a, **k: calls.append(m) or True)
    monkeypatch.setattr(wr, "run_optional_step", lambda m, *a, **k: calls.append(m) or (m != "ml.production_trial"))
    monkeypatch.setattr(wr, "_log", lambda m: None)
    monkeypatch.setattr(sys, "argv", ["weekly_retrain.py"])
    assert wr.main() == 0
    assert calls.index("ml.train_production") < calls.index("ml.production_trial")
    assert "ml.production_trial" not in [m for m, *_ in wr.STEPS]
    # 試走なし系統 (必須) が失敗したら全体が失敗、試走入りも走らない
    calls.clear()
    monkeypatch.setattr(wr, "run_step", lambda m, *a, **k: calls.append(m) or (m != "ml.train_production"))
    assert wr.main() == 1
    assert "ml.production_trial" not in calls


def test_daily_predict_wiring():
    src = (ROOT / "daily_predict.py").read_text(encoding="utf-8")
    i_pred = src.index('feat["pred_calib"] = iso.transform(feat["pred"].values)')
    i_apply = src.index("_pt.apply_trial_model(feat, align_features)")
    i_ev = src.index('ev_raw = feat["pred_calib"]')
    assert i_pred < i_apply < i_ev                         # 校正の後・EV の前で置き換える
    blk = src[src.rindex("try:", 0, i_apply):i_apply + 900]
    assert "except Exception" in blk
    i_rec = src.index("_pt.record_used(shadow_frames)")
    assert src.index("auto_buy.run_auto_buy(candidates)") < i_rec   # 記録は購入処理の後
