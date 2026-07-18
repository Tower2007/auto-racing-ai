"""採用ゲート v2: ROI 劣化拒否権 (ROI veto) — 8PJ 艦隊共通仕様の移植版.

目的:
  精度ゲート (_should_adopt の同一val AUC 公平比較) が「採用」判定を出した
  候補モデルに対し、「標準買い目の ROI が champion から大幅かつ有意に
  劣化していないか」を最終チェックし、劣化時のみ採用を拒否 (veto) する。
  精度ゲートの主判定・鮮度オーバーライド・best_iter サニティは一切変えない
  (本モジュールは追加の拒否権のみを持ち、採用を後押しすることはない)。

全 PJ 共通仕様 (2026-07。参照実装: hokkaido-keiba-ai ml/roi_gate.py):
  - 発動タイミング: 精度ゲートが採用判定を出した後の最終チェック。
    精度ゲート NG なら ROI 計算不要 (呼び出し側でスキップ)。
  - 計算: 精度ゲートと同一 val 窓・同一レース集合上で champion / candidate の
    「標準買い目」ROI をペア計算する。本 PJ の標準買い目は
    「各レース予測スコア top1 の単勝を 1 点 100 円、payouts.csv (bet_type=tns)
    の確定払戻で精算」(呼び出し側 ml/train_production.py の _roi_veto 参照)。
  - 判定 (3 値 + スキップ):
      n_bets < MIN_BETS(200)              -> SKIP  (記録のみ)
      dROI = candidate - champion [pt]
      レース単位ペアブートストラップ (N_BOOTSTRAP=2000, seed 固定) で 95% CI
      dROI <= -10pt かつ CI 上限 < 0      -> NG    (拒否権発動)
      dROI <= -5pt                        -> WARN  (採用は通すが警告)
      それ以外                            -> OK
  - フェイルセーフ: 例外・データ欠損は verdict="ERROR" を返すだけで raise
    しない。呼び出し側は ERROR を WARN 相当として記録し、精度ゲートの判定の
    まま進める (ROI ゲートの不具合で再学習パイプラインを止めない)。

他 PJ への移植方法:
  このファイルを丸ごとコピーし、呼び出し側で以下の DataFrame を組んで
  evaluate_roi_gate() に渡すだけ (本モジュールは pandas/numpy 以外に依存しない):

    列                  内容
    ------------------  ------------------------------------------------------
    race_keys (可変)    レースを一意にする列 (例: race_date, place_code, race_no)
    p_champion          現行モデルの予測スコア (行 = 出走単位)
    p_candidate         候補モデルの予測スコア (同一行集合)
    is_win              買い目的中フラグ (0/1)。単勝なら「1着か」
    odds                的中時払戻倍率 (単勝確定オッズ)。払戻金しかない PJ は
                        payout / stake を渡せば同じ (100 円単勝なら payout/100)

  ・買い目ロジックが top1 単勝以外の PJ は、「1 行 = 1 買い目候補」になるよう
    整形して is_win / odds をその券種の値にすれば流用可 (レース内 argmax 選択)。
  ・レース内の予測が全て NaN の行しかないレース、選ばれた買い目の odds / is_win
    が NaN のレースはペア比較から除外される (両モデルとも同一レース集合で比較)。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

# ─── 共通仕様の既定値 (全 PJ で揃える) ──────────────────────────────────
MIN_BETS = 200          # これ未満は SKIP (小標本では判定しない)
NG_DELTA_PT = -10.0     # dROI がこれ以下 (かつ CI 上限 < 0) で NG
WARN_DELTA_PT = -5.0    # dROI がこれ以下で WARN
N_BOOTSTRAP = 2000      # ペアブートストラップ反復数
BOOTSTRAP_SEED = 20260719  # 乱数 seed (再現性のため固定)
CI_ALPHA = 0.05         # 95% CI


def _race_pair_returns(
    d: pd.DataFrame,
    race_keys: list[str],
    champion_col: str,
    candidate_col: str,
    win_col: str,
    odds_col: str,
) -> pd.DataFrame:
    """レースごとに champion / candidate の買い目 (レース内 argmax) を選び、
    100 円あたり払戻倍率 (odds * is_win) をペアで返す。

    どちらかの買い目が精算不能 (odds / is_win 欠損) のレースは落とす
    (同一レース集合上のペア比較を守るため)。
    """
    d = d.copy()
    d[odds_col] = pd.to_numeric(d[odds_col], errors="coerce")
    d[win_col] = pd.to_numeric(d[win_col], errors="coerce")
    # 予測が NaN の行は選択対象外 (全行 NaN のレースは dropna で消える)
    d = d.dropna(subset=[champion_col, candidate_col])
    if d.empty:
        return pd.DataFrame(columns=["champ_ret", "cand_ret"])

    grp = d.groupby(race_keys, sort=False)
    champ_pick = d.loc[grp[champion_col].idxmax()]
    cand_pick = d.loc[grp[candidate_col].idxmax()]
    races = pd.DataFrame({
        "champ_ret": (champ_pick[odds_col] * champ_pick[win_col]).to_numpy(),
        "cand_ret": (cand_pick[odds_col] * cand_pick[win_col]).to_numpy(),
    })
    return races.dropna().reset_index(drop=True)


def evaluate_roi_gate(
    df: pd.DataFrame,
    *,
    race_keys: Sequence[str] = ("race_date", "place_code", "race_no"),
    champion_col: str = "p_champion",
    candidate_col: str = "p_candidate",
    win_col: str = "is_win",
    odds_col: str = "odds",
    min_bets: int = MIN_BETS,
    ng_delta_pt: float = NG_DELTA_PT,
    warn_delta_pt: float = WARN_DELTA_PT,
    n_bootstrap: int = N_BOOTSTRAP,
    seed: int = BOOTSTRAP_SEED,
) -> dict:
    """champion vs candidate の標準買い目 ROI ペア比較 → verdict dict (純関数)。

    raise しない (フェイルセーフ)。例外は verdict="ERROR" に畳んで返す。

    Returns (dict):
      verdict        "OK" | "WARN" | "NG" | "SKIP" | "ERROR"
      reason         判定理由 (日本語 1 行)
      roi_champion   champion の ROI [%] (100 = 収支トントン)。計算不能時 None
      roi_candidate  candidate の ROI [%]
      roi_delta_pt   candidate - champion [pt]
      roi_ci_low     dROI 95% CI 下限 [pt] (ペアブートストラップ)
      roi_ci_high    dROI 95% CI 上限 [pt]
      n_bets         判定に使った買い目数 (= 精算可能レース数、各モデル 1 点/レース)
    """
    out: dict = {
        "verdict": "ERROR", "reason": "",
        "roi_champion": None, "roi_candidate": None, "roi_delta_pt": None,
        "roi_ci_low": None, "roi_ci_high": None, "n_bets": 0,
    }
    try:
        race_keys = list(race_keys)
        need = race_keys + [champion_col, candidate_col, win_col, odds_col]
        missing = [c for c in need if c not in df.columns]
        if missing:
            raise ValueError(f"必要列が欠損: {missing}")

        races = _race_pair_returns(
            df, race_keys, champion_col, candidate_col, win_col, odds_col)
        n = len(races)
        out["n_bets"] = int(n)
        if n < min_bets:
            out["verdict"] = "SKIP"
            out["reason"] = f"n_bets={n} < {min_bets} (小標本のため判定せず)"
            if n == 0:
                return out
            # 記録用に点推定だけは残す (判定には使わない)

        champ_ret = races["champ_ret"].to_numpy(dtype=float)
        cand_ret = races["cand_ret"].to_numpy(dtype=float)
        roi_champ = float(champ_ret.mean() * 100.0)
        roi_cand = float(cand_ret.mean() * 100.0)
        delta_pt = roi_cand - roi_champ

        # レース単位のペアブートストラップ (同一 resample を両モデルに適用)
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, n, size=(n_bootstrap, n))
        deltas = (cand_ret[idx].mean(axis=1) - champ_ret[idx].mean(axis=1)) * 100.0
        ci_low = float(np.percentile(deltas, 100 * (CI_ALPHA / 2)))
        ci_high = float(np.percentile(deltas, 100 * (1 - CI_ALPHA / 2)))

        out.update({
            "roi_champion": round(roi_champ, 2),
            "roi_candidate": round(roi_cand, 2),
            "roi_delta_pt": round(delta_pt, 2),
            "roi_ci_low": round(ci_low, 2),
            "roi_ci_high": round(ci_high, 2),
        })
        if out["verdict"] == "SKIP":
            return out

        stat = (f"dROI={delta_pt:+.2f}pt (champ {roi_champ:.1f}% -> "
                f"cand {roi_cand:.1f}%), 95%CI [{ci_low:+.2f}, {ci_high:+.2f}], "
                f"n_bets={n}")
        if delta_pt <= ng_delta_pt and ci_high < 0:
            out["verdict"] = "NG"
            out["reason"] = f"ROI 大幅かつ有意に劣化: {stat}"
        elif delta_pt <= warn_delta_pt:
            out["verdict"] = "WARN"
            out["reason"] = f"ROI 劣化 (許容内): {stat}"
        else:
            out["verdict"] = "OK"
            out["reason"] = stat
        return out
    except Exception as e:  # フェイルセーフ: ROI ゲートの不具合で採用を止めない
        out["verdict"] = "ERROR"
        out["reason"] = f"ROI 計算失敗 ({type(e).__name__}: {e})"
        return out
