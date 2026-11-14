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


# ---------------------------------------------------------------- 買い候補の並行比較 (2026-11-15)
def _bc_files(tmp_path, races):
    """races: [(race_no, {car: (p_trial, p_notrial)}, {car: (omin, omax)}, 払戻 {car: 円} or None)]
    確率は raw = 校正後 とする (単純化)。払戻 None = 結果未確定。"""
    log, snap, pay = [], [], []
    for rn, probs, odds, refund in races:
        for car, (pt_, pn_) in probs.items():
            log.append({"race_date": "2026-11-15", "place_code": 3, "race_no": rn, "car_no": car,
                        "model_used": "trial", "fallback_reason": "", "pred_trial": pt_, "pred_calib_trial": pt_,
                        "pred_notrial": pn_, "pred_calib_notrial": pn_, "trial_status": "ok",
                        "trial_model_trained_at": "", "captured_at": "x"})
            lo, hi = odds[car]
            snap.append({"race_date": "2026-11-15", "place_code": 3, "race_no": rn, "car_no": car,
                         "place_odds_min": lo, "place_odds_max": hi, "captured_at": "2026-11-15T10:00:00"})
        if refund is not None:
            pay.append({"race_date": "2026-11-15", "place_code": 3, "race_no": rn, "bet_type": "fns",
                        "car_no_1": 99, "refund": 100.0})        # 確定の印 (他の車の払戻は下で足す)
            for car, y in refund.items():
                pay.append({"race_date": "2026-11-15", "place_code": 3, "race_no": rn, "bet_type": "fns",
                            "car_no_1": car, "refund": y})
    pd.DataFrame(log).to_csv(tmp_path / "u.csv", index=False)
    pd.DataFrame(snap).to_csv(tmp_path / "s.csv", index=False)
    pd.DataFrame(pay or [{"race_date": "", "place_code": 0, "race_no": 0, "bet_type": "x",
                          "car_no_1": 0, "refund": 0}]).to_csv(tmp_path / "p.csv", index=False)
    return dict(log_path=tmp_path / "u.csv", snap_path=tmp_path / "s.csv", payouts_path=tmp_path / "p.csv",
                ledger_path=tmp_path / "none.jsonl", thr=1.5, discount=1.0)


def test_buy_compare_five_groups_and_diff(tmp_path):
    # EV = p x (min+max)/2。しきい値 1.5。車1 の平均オッズ 3.0
    o = {1: (2.0, 4.0), 2: (1.0, 1.0)}
    races = [
        # R1: 両方 車1 を本命 (0.6 → EV 1.8)。的中 (払戻 300)
        (1, {1: (0.6, 0.6), 2: (0.3, 0.3)}, o, {1: 300.0}),
        # R2: 試走入りだけが買う (入り 0.6 → 1.8 / なし 0.4 → 1.2)。車1 は払戻なし (3 着外)
        (2, {1: (0.6, 0.4), 2: (0.3, 0.3)}, o, {2: 100.0}),
        # R3: 試走なしだけが買う (なし 0.7 → 2.1 / 入り 0.4 → 1.2)。車1 的中 200
        (3, {1: (0.4, 0.7), 2: (0.3, 0.3)}, o, {1: 200.0}),
        # R4: どちらも買わない
        (4, {1: (0.3, 0.3), 2: (0.2, 0.2)}, o, {1: 150.0}),
        # R5: 試走入りが買う候補だが結果未確定 → 点数には入るが回収率・損益差には入らない
        (5, {1: (0.9, 0.3), 2: (0.1, 0.1)}, o, None),
    ]
    r = pt.evaluate_buy_compare(**_bc_files(tmp_path, races))
    assert r["n_compare"] == 5 and abs(r["same_top_rate"] - 1.0) < 1e-9
    assert (r["trial"]["n"], r["trial"]["n_settled"]) == (3, 2)           # R1, R2, R5 (未確定)
    assert (r["notrial"]["n"], r["notrial"]["n_settled"]) == (2, 2)       # R1, R3
    assert (r["both"]["n"], r["both"]["n_same_car"]) == (1, 1)
    assert (r["only_trial"]["n"], r["only_trial"]["n_settled"]) == (2, 1)  # R2, R5
    assert r["only_notrial"]["n"] == 1                                     # R3
    assert abs(r["trial"]["roi"] - 1.5) < 1e-9                             # (300 + 0) / 200
    assert abs(r["notrial"]["roi"] - 2.5) < 1e-9                           # (300 + 200) / 200
    # 損益差: R1 0、R2 入り -100、R3 なし +100 → 入り - なし = -100 - 100 = -200 円
    assert abs(r["diff"]["yen"] - (-200.0)) < 1e-9 and r["diff"]["n_races"] == 3


def test_buy_compare_excludes_anomalous_odds_and_ev_cap(tmp_path):
    p = {1: (0.6, 0.6), 2: (0.3, 0.3)}
    races = [
        (1, p, {1: (2.0, 51.0), 2: (1.0, 1.0)}, {1: 300.0}),          # max > 50
        (2, p, {1: (1.0, 21.0), 2: (1.0, 1.0)}, {1: 300.0}),          # max/min > 20
        (3, p, {1: (np.nan, np.nan), 2: (1.0, 1.0)}, {1: 300.0}),     # 本命のオッズ未公開
        (4, p, {1: (30.0, 40.0), 2: (1.0, 1.0)}, {1: 300.0}),         # EV 21 > 10 (auto_buy の異常ガード)
        (5, p, {1: (3.0, 3.0), 2: (1.0, 1.0)}, {1: 300.0}),           # 通常 (EV 1.8)
    ]
    r = pt.evaluate_buy_compare(**_bc_files(tmp_path, races))
    assert r["trial"]["n"] == 1 and r["notrial"]["n"] == 1 and r["n_compare"] == 5


def test_buy_compare_missing_probs_excluded_and_ledger(tmp_path):
    p, od = {1: (0.6, 0.6), 2: (0.3, 0.3)}, {1: (3.0, 3.0), 2: (1.0, 1.0)}
    kw = _bc_files(tmp_path, [(1, p, od, {1: 300.0}), (2, p, od, {1: 300.0})])
    lg = pd.read_csv(kw["log_path"])
    lg.loc[(lg.race_no == 2) & (lg.car_no == 2), "pred_trial"] = np.nan      # R2 は試走入りの確率が欠ける
    lg.to_csv(kw["log_path"], index=False)
    led = tmp_path / "led.jsonl"
    rows = [{"verdict": "executed", "race_date": "2026-11-15", "place_code": 3, "race_no": 1,
             "bets": "複勝 1号 ¥100 / 三連複 1=2=3 ¥100"},
            {"verdict": "executed", "race_date": "2026-11-15", "place_code": 3, "race_no": 9,
             "bets": "複勝 5号 ¥100"}]
    led.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n", encoding="utf-8")
    kw["ledger_path"] = led
    r = pt.evaluate_buy_compare(**kw)
    assert r["n_logged"] == 2 and r["n_compare"] == 1
    assert r["ledger"]["n_match"] == 1 and r["ledger"]["n_ledger"] == 1      # 対象外の R9 は数えない


def test_buy_compare_lines(tmp_path, monkeypatch):
    # 記録ゼロ (ファイルなし) → 行なし
    assert pt.buy_compare_lines(pt.evaluate_buy_compare(log_path=tmp_path / "nope.csv")) == []
    o = {1: (2.0, 4.0), 2: (1.0, 1.0)}
    p = {1: (0.6, 0.6), 2: (0.3, 0.3)}
    L = pt.buy_compare_lines(pt.evaluate_buy_compare(**_bc_files(tmp_path, [(1, p, o, {1: 300.0})])))
    assert len(L) == 3 and "小標本" in L[0] and "試走入り 1 点" in L[0] and "紙上の記録" in L[-1]
    # 結果未確定: 件数だけ
    L2 = pt.buy_compare_lines(pt.evaluate_buy_compare(**_bc_files(tmp_path, [(1, p, o, None)])))
    assert "回収率" not in L2[0] and "未確定" in L2[1]
    # 集計が例外でも weekly_lines は止まらない
    monkeypatch.setattr(pt, "use_trial_enabled", lambda: True)
    monkeypatch.setattr(pt, "evaluate_buy_compare", lambda *a, **k: 1 / 0)
    ev = {"n_races": 1, "n_trial": 1, "n_fallback": 0, "date_min": "a", "date_max": "b"}
    out = pt.weekly_lines(ev, buy={})
    assert "本番モデル" in out[0] and len(out) >= 1
    monkeypatch.setattr(pt, "evaluate", lambda *a, **k: ev)
    out2 = pt.weekly_lines()
    assert "本番モデル" in out2[0] and "集計失敗" in out2[-1]
