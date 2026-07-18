"""採用ゲート v2: ROI 劣化拒否権 (ml/roi_gate.py) の回帰テスト.

合成データで NG / WARN / OK / SKIP / ERROR の 5 経路と、seed 再現性・
欠損レース除外・retrain_history.csv のヘッダ移行 (後方互換) を検証する。
ネットワーク・実データ不要。

Usage:
    python -m unittest tests.test_roi_gate -v
    python tests/test_roi_gate.py
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ml.roi_gate import evaluate_roi_gate  # noqa: E402


def _make_df(n_races: int, champ_ret: float | list, cand_ret: float | list,
             special: dict | None = None) -> pd.DataFrame:
    """1 レース 2 車の合成データを作る。

    champ (p_champion 最大) は車 1、cand (p_candidate 最大) は車 2 を選ぶ。
    車 i の (odds * is_win) が指定リターンになるよう odds/is_win を組む
    (検証用に両車 is_win=1 を許す。roi_gate は的中判定を再計算しないため問題ない)。
    special: {race_idx: (champ_ret, cand_ret)} で一部レースだけ上書き。
    """
    rows = []
    for r in range(n_races):
        cr, ar = champ_ret, cand_ret
        if special and r in special:
            cr, ar = special[r]
        # 車1 = champion の買い目, 車2 = candidate の買い目
        rows.append({"race_date": "2026-06-01", "place_code": 2, "race_no": r,
                     "p_champion": 0.9, "p_candidate": 0.1,
                     "is_win": 1 if cr > 0 else 0,
                     "odds": cr if cr > 0 else 5.0})
        rows.append({"race_date": "2026-06-01", "place_code": 2, "race_no": r,
                     "p_champion": 0.1, "p_candidate": 0.9,
                     "is_win": 1 if ar > 0 else 0,
                     "odds": ar if ar > 0 else 5.0})
    return pd.DataFrame(rows)


class RoiGateVerdictTests(unittest.TestCase):
    def test_ok_identical_models(self):
        """champion = candidate 相当 (同一リターン) -> dROI=0 で OK."""
        df = _make_df(250, champ_ret=2.0, cand_ret=2.0)
        res = evaluate_roi_gate(df)
        self.assertEqual(res["verdict"], "OK")
        self.assertEqual(res["n_bets"], 250)
        self.assertAlmostEqual(res["roi_delta_pt"], 0.0)
        self.assertAlmostEqual(res["roi_champion"], 200.0)

    def test_warn_moderate_drop(self):
        """dROI = -7pt (有意) -> NG 閾値 -10pt 未満なので WARN 止まり."""
        df = _make_df(250, champ_ret=1.07, cand_ret=1.00)
        res = evaluate_roi_gate(df)
        self.assertEqual(res["verdict"], "WARN")
        self.assertAlmostEqual(res["roi_delta_pt"], -7.0, places=6)
        self.assertLess(res["roi_ci_high"], 0)

    def test_ng_large_significant_drop(self):
        """dROI = -120pt かつ全レース一様 (CI 上限 < 0) -> 拒否権発動 NG."""
        df = _make_df(250, champ_ret=1.2, cand_ret=0.0)
        res = evaluate_roi_gate(df)
        self.assertEqual(res["verdict"], "NG")
        self.assertAlmostEqual(res["roi_delta_pt"], -120.0, places=6)
        self.assertLess(res["roi_ci_high"], 0)

    def test_warn_large_but_insignificant_drop(self):
        """dROI = -12pt だが少数レース由来で CI 上限 >= 0 -> NG にせず WARN."""
        special = {i: (11.0, 1.0) for i in (10, 100, 200)}  # 3 レースだけ大差
        df = _make_df(250, champ_ret=1.0, cand_ret=1.0, special=special)
        res = evaluate_roi_gate(df)
        self.assertAlmostEqual(res["roi_delta_pt"], -12.0, places=6)
        self.assertGreaterEqual(res["roi_ci_high"], 0)
        self.assertEqual(res["verdict"], "WARN")

    def test_skip_small_sample(self):
        """n_bets < 200 -> SKIP (点推定は記録される)."""
        df = _make_df(50, champ_ret=1.2, cand_ret=0.0)  # 中身は NG 級でも判定しない
        res = evaluate_roi_gate(df)
        self.assertEqual(res["verdict"], "SKIP")
        self.assertEqual(res["n_bets"], 50)
        self.assertIsNotNone(res["roi_delta_pt"])

    def test_skip_empty(self):
        """列は揃っているが 0 行 (対象レースなし) -> SKIP."""
        df = _make_df(1, champ_ret=1.0, cand_ret=1.0).iloc[0:0]
        res = evaluate_roi_gate(df)
        self.assertEqual(res["verdict"], "SKIP")
        self.assertEqual(res["n_bets"], 0)

    def test_error_missing_column(self):
        """必要列欠損 -> raise せず verdict=ERROR (フェイルセーフ)."""
        df = _make_df(250, champ_ret=1.0, cand_ret=1.0).drop(columns=["odds"])
        res = evaluate_roi_gate(df)
        self.assertEqual(res["verdict"], "ERROR")
        self.assertIn("odds", res["reason"])

    def test_error_non_dataframe(self):
        res = evaluate_roi_gate(None)  # type: ignore[arg-type]
        self.assertEqual(res["verdict"], "ERROR")


class RoiGateMechanicsTests(unittest.TestCase):
    def test_seed_reproducibility(self):
        """seed 固定で CI が完全一致する (再現性)."""
        rng = np.random.default_rng(7)
        special = {i: (float(rng.uniform(0, 5)), float(rng.uniform(0, 5)))
                   for i in range(0, 250, 3)}
        df = _make_df(250, champ_ret=1.0, cand_ret=1.0, special=special)
        r1 = evaluate_roi_gate(df)
        r2 = evaluate_roi_gate(df)
        self.assertEqual(r1["roi_ci_low"], r2["roi_ci_low"])
        self.assertEqual(r1["roi_ci_high"], r2["roi_ci_high"])

    def test_nan_odds_race_dropped_pairwise(self):
        """どちらかの買い目の odds が NaN のレースはペアごと除外される."""
        df = _make_df(210, champ_ret=2.0, cand_ret=2.0)
        # race_no=0 の車2 (candidate の買い目) の odds を NaN に
        m = (df["race_no"] == 0) & (df["p_candidate"] == 0.9)
        df.loc[m, "odds"] = np.nan
        res = evaluate_roi_gate(df)
        self.assertEqual(res["n_bets"], 209)
        self.assertEqual(res["verdict"], "OK")

    def test_custom_race_keys_and_columns(self):
        """競技非依存: 列名・レースキーを差し替えても動く (他 PJ 移植の形)."""
        df = _make_df(250, champ_ret=1.5, cand_ret=1.5).rename(columns={
            "p_champion": "pc", "p_candidate": "pn",
            "is_win": "hit", "odds": "pay_mult", "race_no": "rid",
        })
        res = evaluate_roi_gate(
            df, race_keys=["race_date", "rid"],
            champion_col="pc", candidate_col="pn",
            win_col="hit", odds_col="pay_mult")
        self.assertEqual(res["verdict"], "OK")
        self.assertEqual(res["n_bets"], 250)


class HistoryMigrationTests(unittest.TestCase):
    """retrain_history.csv への roi_* 列追加が旧行を壊さないこと (後方互換)。

    weekly_status.py の check_model_freshness は retrain_history.csv を
    pd.read_csv で読むため、ラグド CSV (行ごとに列数不一致) にしないことが必須。
    """

    OLD_HEADER = [
        "timestamp", "verdict", "reason",
        "valid_auc", "valid_logloss", "best_iteration",
        "target_definition_version",
    ]

    def test_append_migrates_old_header(self):
        from ml import train_production as tp

        with tempfile.TemporaryDirectory() as tmp:
            hist = Path(tmp) / "retrain_history.csv"
            with open(hist, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(self.OLD_HEADER)
                w.writerow(["2026-05-24T03:00:15", "NG", "旧行",
                            0.8234, 0.5036, 183, 1])
            with mock.patch.object(tp, "HISTORY_PATH", hist):
                tp._append_retrain_history(
                    "OK", "新行",
                    {"valid_auc": 0.8207, "valid_logloss": 0.5, "best_iteration": 202},
                    roi={"verdict": "OK", "roi_delta_pt": 1.23, "n_bets": 250},
                )
            out = pd.read_csv(hist)  # ラグド CSV ならここで落ちる
            self.assertEqual(list(out.columns), tp.HISTORY_HEADER)
            self.assertEqual(len(out), 2)
            self.assertTrue(pd.isna(out.loc[0, "roi_verdict"]))  # 旧行は空欄
            self.assertEqual(out.loc[0, "verdict"], "NG")        # 旧行の既知列は保全
            self.assertEqual(out.loc[1, "roi_verdict"], "OK")
            self.assertEqual(out.loc[1, "roi_n_bets"], 250)

    def test_append_new_file_uses_new_header(self):
        from ml import train_production as tp

        with tempfile.TemporaryDirectory() as tmp:
            hist = Path(tmp) / "retrain_history.csv"
            with mock.patch.object(tp, "HISTORY_PATH", hist):
                tp._append_retrain_history(
                    "OK", "初回", {"valid_auc": 0.8},
                    roi={"verdict": "SKIP"})
            out = pd.read_csv(hist)
            self.assertEqual(list(out.columns), tp.HISTORY_HEADER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
