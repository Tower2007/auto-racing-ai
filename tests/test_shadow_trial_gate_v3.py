"""試走ありシャドー (ml/shadow_trial.py) と採用ゲート v3 (ml/train_production.py) のテスト (2026-11-12)。

重点: シャドーが無い・壊れている・特徴量が欠ける・予測が例外・書けない、のどれでも
本番の予測値 (pred / pred_calib) が変わらず、例外が外に出ないこと。
合成データのみ (実データ・実モデル・ネットワーク不要)。
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

from ml import shadow_trial as st  # noqa: E402
from ml import train_production as tp  # noqa: E402


def _align(df, meta):
    """daily_predict.align_features と同じ挙動の簡易版 (テスト用)。"""
    out = pd.DataFrame(index=df.index)
    for c in meta["feature_columns"]:
        out[c] = df[c] if c in df.columns else np.nan
    return out


def _race(trial=(3.40, 3.41, 3.43), race_no=1):
    n = len(trial)
    return pd.DataFrame({
        "race_date": pd.to_datetime(["2026-11-01"] * n), "place_code": [3] * n,
        "race_no": [race_no] * n, "car_no": list(range(1, n + 1)),
        "trial_run_time": list(trial), "win_odds": [2.0, 5.0, 9.0][:n],
        "pred": [0.7, 0.4, 0.2][:n], "pred_calib": [0.69, 0.41, 0.2][:n],
    })


@pytest.fixture()
def tiny_model(tmp_path):
    import lightgbm as lgb
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"trial_run_time": rng.normal(3.4, 0.05, 400), "win_odds": rng.uniform(1, 20, 400)})
    y = (rng.random(400) < 0.4).astype(int)
    b = lgb.train({"objective": "binary", "verbosity": -1, "num_leaves": 4}, lgb.Dataset(X, y), 5)
    mp, jp = tmp_path / "m.lgb", tmp_path / "m.json"
    b.save_model(str(mp))
    jp.write_text(json.dumps({"feature_columns": ["trial_run_time", "win_odds"],
                              "trained_at": "2026-11-12T00:00:00"}), encoding="utf-8")
    st._CACHE.update({"mtime": None, "model": None, "meta": None})
    return mp, jp


def test_record_ok_and_prod_unchanged(tmp_path, tiny_model):
    mp, jp = tiny_model
    f = _race()
    before = f.copy()
    r = st.record([f, _race(race_no=2)], _align, model_path=mp, meta_path=jp,
                  log_path=tmp_path / "log.csv")
    assert r["status"] == "ok" and r["n_races"] == 2 and r["elapsed_ms"] < 2000
    pd.testing.assert_frame_equal(f, before)               # 本番の frame は不変
    log = pd.read_csv(tmp_path / "log.csv")
    assert list(log.columns) == st.LOG_FIELDS and len(log) == 6
    assert log["pred_shadow"].notna().all() and set(log["trial_status"]) == {"ok"}


@pytest.mark.parametrize("case", ["no_model", "broken", "missing_feature", "predict_raises", "unwritable"])
def test_record_never_raises_and_keeps_prod(tmp_path, tiny_model, case):
    mp, jp = tiny_model
    f = _race(trial=(3.4, None, 0.0))
    before = f.copy()
    log = tmp_path / "log.csv"
    align = _align
    if case == "no_model":
        mp = tmp_path / "none.lgb"
    elif case == "broken":
        mp.write_text("this is not a model", encoding="utf-8")
    elif case == "missing_feature":
        jp.write_text(json.dumps({"feature_columns": ["no_such_col"]}), encoding="utf-8")
    elif case == "predict_raises":
        def align(df, meta):
            raise KeyError("feature missing")
    elif case == "unwritable":
        log = tmp_path  # ディレクトリ = 書けない
    r = st.record([f], align, model_path=mp, meta_path=jp, log_path=log)
    pd.testing.assert_frame_equal(f, before)
    assert isinstance(r, dict) and "elapsed_ms" in r
    if case == "no_model":
        assert r["status"] == "no_model"
    if case == "broken":
        assert r["status"].startswith("model_load_failed")
    if case == "predict_raises":
        assert r["status"].startswith("predict_failed")
    if case == "unwritable":
        assert r["status"].startswith("record_failed")
    if case in ("no_model", "broken", "predict_raises"):
        lg = pd.read_csv(log)
        assert set(lg["trial_status"]) == {"partial"} and lg["pred_shadow"].isna().all()


def test_trial_status():
    assert st.trial_status(_race()) == "ok"
    assert st.trial_status(_race(trial=(3.4, None, 3.5))) == "partial"
    assert st.trial_status(_race(trial=(None, None, None))) == "none"
    assert st.trial_status(_race().drop(columns=["trial_run_time"])) == "none"


def _write_eval_fixture(tmp_path, n_weeks=4):
    rows, res = [], []
    rng = np.random.default_rng(1)
    for w in range(n_weeks):
        for r in range(10):
            day = (pd.Timestamp("2026-10-05") + pd.Timedelta(days=7 * w + r % 5)).date().isoformat()
            order = rng.permutation(8) + 1
            for c in range(8):
                top3 = order[c] <= 3
                rows.append({"race_date": day, "place_code": 2, "race_no": r + 1, "car_no": c + 1,
                             "pred_prod": 0.6 if top3 else 0.3, "pred_calib_prod": 0.6 if top3 else 0.3,
                             "pred_shadow": 0.8 if top3 else 0.2, "trial_run_time": 3.4,
                             "trial_status": "ok" if r < 9 else "none",
                             "shadow_trained_at": "x", "captured_at": "x", "elapsed_ms": 50})
                res.append({"race_date": day, "place_code": 2, "race_no": r + 1, "car_no": c + 1,
                            "order": order[c]})
    pd.DataFrame(rows).to_csv(tmp_path / "log.csv", index=False)
    pd.DataFrame(res).to_csv(tmp_path / "res.csv", index=False)


def test_evaluate_and_weekly_lines(tmp_path):
    _write_eval_fixture(tmp_path, n_weeks=4)
    r = st.evaluate(tmp_path / "log.csv", tmp_path / "res.csv")
    assert r["n_logged_races"] == 40 and abs(r["trial_ok_rate"] - 0.9) < 1e-9
    assert r["n_eval_races"] == 36 and r["ll_shadow"] < r["ll_prod"]
    L = st.weekly_lines(r)
    assert "試走の取得率" in L[0] and "90.0%" in L[0]
    assert "本番化の判断材料が揃った" in L[1]
    _write_eval_fixture(tmp_path, n_weeks=2)
    L2 = st.weekly_lines(st.evaluate(tmp_path / "log.csv", tmp_path / "res.csv"))
    assert "2/4 週" in L2[1]
    assert "記録なし" in st.weekly_lines({"n_logged_races": 0})[0]


def test_shadow_exclude_only_restores_trial():
    ex = st.shadow_exclude()
    assert not (set(st.TRIAL_COLS) & ex)
    assert (tp.BASE_EXCLUDE | tp.MORNING_EXCLUDE) - set(st.TRIAL_COLS) == ex
    assert "ai_expect_code" in ex and "trial_retry_code" in ex


# ---------------------------------------------------------------- 採用ゲート v3
OLD = {"trained_at": "2026-11-01T03:00:00",
       "target_definition_version": tp.TARGET_DEFINITION_VERSION,
       "train_metrics": {"best_iteration": 200}}


def test_v3_rules():
    ch = {"auc": 0.818, "logloss": 0.5090}
    m = {"best_iteration": 180}
    assert tp._should_adopt_v3({"auc": 0.818, "logloss": 0.5100}, ch, m, OLD, 7)[0] == "OK"
    v, r = tp._should_adopt_v3({"auc": 0.818, "logloss": 0.5120}, ch, m, OLD, 7)
    assert v == "NG" and "logloss" in r
    v, r = tp._should_adopt_v3({"auc": 0.818, "logloss": 0.5120}, ch, m, OLD, tp.STALE_DAYS + 1)
    assert v == "WARN" and "鮮度" in r
    assert tp._should_adopt_v3({"auc": 0.9, "logloss": 0.40}, ch, {"best_iteration": 10}, OLD, 7)[0] == "NG"
    assert tp._should_adopt_v3({"auc": 0.5, "logloss": 0.9}, ch, m, OLD, 7, force=True)[0] == "OK"
    assert tp._should_adopt_v3({"auc": 0.5, "logloss": 0.9}, ch, m, None, 7)[0] == "OK"


def test_champion_train_end():
    dates = pd.Series(pd.date_range("2025-01-01", "2026-01-01", freq="D"))
    assert tp.champion_train_end({"train_metrics": {"train_date_max": "2025-10-01"}}, dates) == "2025-10-01"
    end = tp.champion_train_end({"data_date_range": {"max": "2025-12-31"}, "train_metrics": {}}, dates)
    assert "2025-11-20" <= end < "2025-12-31"
    assert tp.champion_train_end(None, dates) is None


def test_train_full_v3_split(monkeypatch):
    rng = np.random.default_rng(0)
    n = 2000
    dates = pd.Series(pd.to_datetime("2025-01-01") + pd.to_timedelta(np.arange(n) // 5, unit="D"))
    X = pd.DataFrame({"a": rng.normal(size=n), "b": rng.normal(size=n)})
    y = pd.Series((X["a"] + rng.normal(size=n) > 0).astype(int))
    model, m, X_es, y_es, X_cmp, y_cmp = tp.train_full_v3(X, y, dates)
    assert m["n_train"] + m["n_val"] + m["n_cmp"] == n
    assert dates[X_cmp.index].min() > dates[X_es.index].max()
    assert m["train_date_max"] < m["cmp_start"] and 0.5 < m["cmp_auc"] <= 1


def test_place_payout_frame():
    d = pd.DataFrame({"race_date": ["2026-11-01"] * 4 + ["2026-11-02"] * 2, "place_code": [2] * 6,
                      "race_no": [1] * 4 + [1] * 2, "car_no": [1, 2, 3, 4, 1, 2]})
    pay = pd.DataFrame({"race_date": ["2026-11-01"] * 4, "place_code": [2] * 4, "race_no": [1] * 4,
                        "bet_type": ["fns", "fns", "fns", "tns"], "car_no_1": [1, 3, 4, 1],
                        "refund": [110, 250, 400, 180]})
    f = tp.place_payout_frame(d, pay)
    r1 = f[f["race_date"] == pd.Timestamp("2026-11-01")].set_index("car_no")
    assert r1.loc[1, "is_win"] == 1 and abs(r1.loc[1, "odds"] - 1.1) < 1e-9
    assert r1.loc[2, "is_win"] == 0 and r1.loc[4, "odds"] == 4.0
    assert f[f["race_date"] == pd.Timestamp("2026-11-02")]["odds"].isna().all()  # 払戻なし → 除外


def test_history_header_v3_columns():
    assert tp.HISTORY_HEADER[-8:] == ["gate_mode", "cmp_start", "cmp_n", "cand_ll_cmp",
                                      "champ_ll_cmp", "cand_auc_cmp", "champ_auc_cmp", "roi_bet_type"]


def test_weekly_retrain_optional_step_failure_keeps_rc(monkeypatch):
    import scripts.weekly_retrain as wr
    calls = []
    monkeypatch.setattr(wr, "run_step", lambda *a, **k: calls.append(a[0]) or True)
    monkeypatch.setattr(wr, "run_optional_step", lambda *a, **k: calls.append(a[0]) or False)
    monkeypatch.setattr(wr, "_log", lambda m: None)
    monkeypatch.setattr(sys, "argv", ["weekly_retrain.py"])
    assert wr.main() == 0
    assert calls[-1] == "ml.shadow_trial" and "ml.train_production" in calls


def test_daily_predict_hook_is_guarded():
    """発火経路の追加は try/except で囲まれ、購入処理 (auto_buy) より後にある。"""
    src = (ROOT / "daily_predict.py").read_text(encoding="utf-8")
    i_hook = src.index("_st.record(shadow_frames, align_features)")
    i_buy = src.index("auto_buy.run_auto_buy(candidates)")
    assert i_buy < i_hook
    block = src[src.rindex("try:", 0, i_hook):i_hook + 400]
    assert "except Exception" in block
