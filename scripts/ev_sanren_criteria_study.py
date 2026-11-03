"""三連系 (rt3/rf3) 購入基準の再検討スタディ (read-only、data/ には書かない)。

発火時点で入手可能な情報のみ (odds_snapshots / odds_combo_snapshots) を使い、
TRAIN (<=2026-08-31) で確率モデル・閾値を決め、TEST (2026-09-01〜) で評価する。

実行: python scripts/ev_sanren_criteria_study.py
      python scripts/ev_sanren_criteria_study.py --train-end 2026-11-30 --test-start 2026-12-01
出力: reports/sanren_criteria_study_<今日>.md (--out で変更可)
初回: 2026-11-03 (reports/sanren_criteria_study_2026-11-03.md)。判定は「据え置き」。
次回: 2027-02 頃、三連単の的中数が溜まってから同じ手順で再検証。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.strategy_config import FIRE_EV_DISCOUNT, THREE_POINT_POLICY_PAIRS  # noqa: E402

DATA = ROOT / "data"  # read-only
_ap = argparse.ArgumentParser(description="三連系 購入基準スタディ (read-only)")
_ap.add_argument("--train-end", default="2026-08-31", help="TRAIN 最終日 (含む)")
_ap.add_argument("--test-start", default="2026-09-01", help="TEST 開始日")
_ap.add_argument("--out", default=None,
                 help="レポート出力先 (既定 reports/sanren_criteria_study_<今日>.md)")
_args = _ap.parse_args()
OUT = (Path(_args.out) if _args.out
       else ROOT / "reports" / f"sanren_criteria_study_{dt.date.today().isoformat()}.md")

TRAIN_END = _args.train_end
TEST_START = _args.test_start
FIRE_DISCOUNT = FIRE_EV_DISCOUNT
# 現役ポリシーの (場, 券種) は strategy_config が正本 (場コードは文字列で照合)
POLICY_PAIRS = {(str(pc), bt) for pc, bt in THREE_POINT_POLICY_PAIRS}
K_GRID = [1.0, 1.1, 1.2, 1.3, 1.5, 2.0]
MIN_TRAIN_N = 60
BANDS = {
    "rf3": [(1, 3), (3, 5), (5, 8), (8, 15), (15, np.inf)],
    "rt3": [(1, 5), (5, 10), (10, 20), (20, 50), (50, np.inf)],
}
KEYS = ["race_date", "place_code", "race_no"]
lines: list[str] = []


def emit(s: str = "") -> None:
    lines.append(s)
    print(s)


def logit(p):
    p = np.clip(p, 0.01, 0.99)
    return np.log(p / (1 - p))


# ---------------------------------------------------------------- 読み込み
def load_snapshots():
    s = pd.read_csv(DATA / "odds_snapshots.csv")
    s["place_code"] = s["place_code"].astype(int)
    n_raw = s.groupby(KEYS).ngroups
    last = s.groupby(KEYS)["captured_at"].transform("max")
    s = s[s["captured_at"] == last].copy()
    s = s.drop_duplicates(KEYS + ["car_no"], keep="last")
    return s, n_raw


def per_race_table(s: pd.DataFrame) -> pd.DataFrame:
    """レース単位: top4 の pred_calib、top3 の車番、top1 の fns 生EV。"""
    s = s.sort_values(KEYS + ["pred_calib", "car_no"], ascending=[True] * 3 + [False, True])
    s["r"] = s.groupby(KEYS).cumcount()
    s["place_mid"] = (s["place_odds_min"] + s["place_odds_max"]) / 2
    top = s[s["r"] < 4]
    w = top.pivot_table(index=KEYS, columns="r", values=["pred_calib", "car_no"], aggfunc="first")
    w.columns = [f"{a}{b}" for a, b in w.columns]
    w = w.reset_index()
    t1 = s[s["r"] == 0][KEYS + ["place_mid"]].rename(columns={"place_mid": "mid1"})
    w = w.merge(t1, on=KEYS)
    w = w.dropna(subset=["pred_calib0", "pred_calib1", "pred_calib2", "pred_calib3"]).copy()
    for i in range(4):
        w[f"car_no{i}"] = w[f"car_no{i}"].astype(int)
    w["fns_raw_ev"] = w["pred_calib0"] * w["mid1"]
    w["place_code"] = w["place_code"].astype(int)
    w["race_date"] = w["race_date"].astype(str)
    return w


def load_payouts() -> dict:
    p = pd.read_csv(DATA / "payouts.csv")
    p = p[p["bet_type"].isin(["rt3", "rf3"])].copy()
    p["place_code"] = p["place_code"].astype(int)
    p["race_date"] = p["race_date"].astype(str)
    out = {}
    for bt in ["rt3", "rf3"]:
        q = p[p["bet_type"] == bt].dropna(subset=["refund"]).copy()
        for c in ["car_no_1", "car_no_2", "car_no_3"]:
            q[c] = q[c].astype(int)
        if bt == "rf3":
            arr = np.sort(q[["car_no_1", "car_no_2", "car_no_3"]].values, axis=1)
            q[["car_no_1", "car_no_2", "car_no_3"]] = arr
        q = q.drop_duplicates(KEYS + ["car_no_1", "car_no_2", "car_no_3"])
        out[bt] = q[KEYS + ["car_no_1", "car_no_2", "car_no_3", "refund"]]
    return out


def load_combo():
    c = pd.read_csv(
        DATA / "odds_combo_snapshots.csv",
        usecols=KEYS + ["bet_type", "car_no_1", "car_no_2", "car_no_3", "odds", "captured_at"],
    )
    c = c[c["bet_type"].isin(["rt3", "rf3"])].copy()
    c["place_code"] = c["place_code"].astype(int)
    c["race_date"] = c["race_date"].astype(str)
    out, chk = {}, {}
    for bt in ["rt3", "rf3"]:
        q = c[c["bet_type"] == bt].copy()
        last = q.groupby(KEYS)["captured_at"].transform("max")
        q = q[q["captured_at"] == last].dropna(subset=["car_no_1", "car_no_2", "car_no_3"])
        for k in ["car_no_1", "car_no_2", "car_no_3"]:
            q[k] = q[k].astype(int)
        if bt == "rf3":
            asc = ((q["car_no_1"] < q["car_no_2"]) & (q["car_no_2"] < q["car_no_3"])).mean()
            chk["rf3_sorted_ratio"] = float(asc)
            arr = np.sort(q[["car_no_1", "car_no_2", "car_no_3"]].values, axis=1)
            q[["car_no_1", "car_no_2", "car_no_3"]] = arr
        q = q.drop_duplicates(KEYS + ["car_no_1", "car_no_2", "car_no_3"])
        out[bt] = q[KEYS + ["car_no_1", "car_no_2", "car_no_3", "odds"]]
        chk[f"{bt}_races_with_board"] = int(q.groupby(KEYS).ngroups)
    return out, chk


# ---------------------------------------------------------------- 結果結合
def attach_outcome(w: pd.DataFrame, bt: str, pay: pd.DataFrame, settled_keys: set) -> pd.DataFrame:
    """w に bet_type ごとの hit / ret(¥100 当たり払戻) / settled を付ける。"""
    d = w.copy()
    trio = d[["car_no0", "car_no1", "car_no2"]].values
    if bt == "rf3":
        trio = np.sort(trio, axis=1)
    d["b1"], d["b2"], d["b3"] = trio[:, 0], trio[:, 1], trio[:, 2]
    m = d.merge(
        pay.rename(columns={"car_no_1": "b1", "car_no_2": "b2", "car_no_3": "b3"}),
        on=KEYS + ["b1", "b2", "b3"], how="left",
    )
    m["hit"] = m["refund"].notna().astype(int)
    m["ret"] = m["refund"].fillna(0.0)
    m["settled"] = [(a, b, c) in settled_keys for a, b, c in zip(m.race_date, m.place_code, m.race_no)]
    return m


def attach_odds(m: pd.DataFrame, bt: str, board: pd.DataFrame) -> pd.DataFrame:
    b = board.rename(columns={"car_no_1": "b1", "car_no_2": "b2", "car_no_3": "b3", "odds": f"odds_{bt}"})
    return m.merge(b, on=KEYS + ["b1", "b2", "b3"], how="left")


# ---------------------------------------------------------------- 統計
def boot_ci(ret: np.ndarray, reps=5000, seed=0):
    n = len(ret)
    if n == 0:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    out = np.empty(reps)
    chunk = max(1, int(2e7 // n))
    i = 0
    while i < reps:
        r = min(chunk, reps - i)
        idx = rng.integers(0, n, size=(r, n))
        out[i:i + r] = ret[idx].sum(axis=1) / (100.0 * n) * 100
        i += r
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def cell(df: pd.DataFrame, mask, days: int, ci: bool):
    sub = df[mask]
    n = len(sub)
    if n == 0:
        return dict(n=0, hits=0, hit=np.nan, roi=np.nan, profit=0, perday=0, lo=np.nan, hi=np.nan)
    ret = sub["ret"].values
    hits = int(sub["hit"].sum())
    roi = ret.sum() / (100 * n) * 100
    lo, hi = boot_ci(ret) if ci else (np.nan, np.nan)
    return dict(n=n, hits=hits, hit=hits / n * 100, roi=roi, profit=int(ret.sum() - 100 * n),
                perday=n / days if days else np.nan, lo=lo, hi=hi)


def fmt_row(label, tr, te):
    def f(c, ci):
        if c["n"] == 0:
            return "0 | - | - | - | - | -" + (" | -" if ci else "")
        s = f"{c['n']} | {c['hits']} | {c['hit']:.1f} | {c['roi']:.1f} | {c['profit']:+,} | {c['perday']:.2f}"
        if ci:
            s += f" | [{c['lo']:.0f}, {c['hi']:.0f}]"
        return s
    return f"| {label} | {f(tr, False)} | {f(te, True)} |"


TABLE_HEAD = (
    "| ルール | TRAIN n | hits | hit% | ROI% | 損益¥ | 本/日 | TEST n | hits | hit% | ROI% | 損益¥ | 本/日 | ROI 95%CI |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
)


def analyze(df_all: pd.DataFrame, bt: str, uni_label: str):
    """df_all: bet_type bt のユニバース (settled & combo odds あり & 対象 place)。"""
    tr = df_all[df_all["race_date"] <= TRAIN_END]
    te = df_all[df_all["race_date"] >= TEST_START]
    dtr, dte = tr["race_date"].nunique(), te["race_date"].nunique()
    od = f"odds_{bt}"

    def masks(d):
        ev = d["fns_raw_ev"]
        return {
            "R1 全レース買い": pd.Series(True, index=d.index),
            f"R0 現行 (EV×{FIRE_DISCOUNT}>=1.80)": ev * FIRE_DISCOUNT >= 1.80,
            "R0raw (EV>=1.80)": ev >= 1.80,
        }

    res = {}
    mte = masks(te)
    for name, fn in masks(tr).items():
        res[name] = (cell(tr, fn, dtr, False), cell(te, mte[name], dte, True))
    grid = {}
    for k in K_GRID:
        grid[k] = (cell(tr, tr[f"ov_{bt}"] >= k, dtr, False), cell(te, te[f"ov_{bt}"] >= k, dte, True))
    elig = [(k, g[0]["roi"]) for k, g in grid.items() if g[0]["n"] >= MIN_TRAIN_N and not np.isnan(g[0]["roi"])]
    ksel = max(elig, key=lambda x: x[1])[0] if elig else None
    bands = []
    for lo, hi in BANDS[bt]:
        lab = f"odds [{lo},{'inf' if np.isinf(hi) else hi})"
        mt = (tr[od] >= lo) & (tr[od] < hi)
        me = (te[od] >= lo) & (te[od] < hi)
        bands.append((lab, cell(tr, mt, dtr, False), cell(te, me, dte, True)))
    r4 = None
    if ksel is not None:
        ev_t = tr["fns_raw_ev"] * FIRE_DISCOUNT >= 1.80
        ev_e = te["fns_raw_ev"] * FIRE_DISCOUNT >= 1.80
        r4 = (cell(tr, ev_t & (tr[f"ov_{bt}"] >= ksel), dtr, False),
              cell(te, ev_e & (te[f"ov_{bt}"] >= ksel), dte, True))

    emit(f"#### {bt} / {uni_label}  (TRAIN {dtr}日 {len(tr)}R, TEST {dte}日 {len(te)}R)")
    emit()
    emit(TABLE_HEAD)
    for name, (a, b) in res.items():
        emit(fmt_row(name, a, b))
    if ksel is not None:
        emit(fmt_row(f"R2 overlay>={ksel} **(TRAINで選択)**", *grid[ksel]))
        emit(fmt_row(f"R4 = R0 ∩ overlay>={ksel}", *r4))
    else:
        emit(f"| R2/R4 | TRAIN n>={MIN_TRAIN_N} を満たす k なし |")
    emit()
    emit(f"R2 k グリッド (TEST 結果は選択に不使用・透明性のため掲載。TRAIN 選択 k = {ksel})")
    emit()
    emit(TABLE_HEAD)
    for k, g in grid.items():
        emit(fmt_row(f"overlay>={k}" + (" ←選択" if k == ksel else ""), *g))
    emit()
    emit("R3 fire時点コンボオッズ帯 (記述的)")
    emit()
    emit(TABLE_HEAD)
    for lab, a, b in bands:
        emit(fmt_row(lab, a, b))
    emit()
    return ksel


def calib_table(d: pd.DataFrame, col: str, hitcol: str, nb: int, title: str):
    d = d.dropna(subset=[col]).copy()
    d["bin"] = pd.qcut(d[col].rank(method="first"), nb, labels=False)
    g = d.groupby("bin").agg(n=(col, "size"), mean_p=(col, "mean"), actual=(hitcol, "mean"), hits=(hitcol, "sum"))
    emit(f"**{title}**")
    emit()
    emit("| 分位 | n | 平均P̂ | 実際 hit率 | hits |\n|---|---|---|---|---|")
    for i, r in g.iterrows():
        emit(f"| {i + 1} | {int(r.n)} | {r.mean_p:.4f} | {r.actual:.4f} | {int(r.hits)} |")
    emit(f"全体: 平均P̂={d[col].mean():.4f} 実際={d[hitcol].mean():.4f} (差 {d[col].mean() - d[hitcol].mean():+.4f})")
    emit()


# ---------------------------------------------------------------- main
def main():
    from sklearn.linear_model import LogisticRegression

    emit("# 三連系 購入基準スタディ (2026-11-03)")
    emit()
    emit(f"スクリプト: `scripts/ev_sanren_criteria_study.py` (read-only)。発火時点情報のみ、TRAIN<={TRAIN_END} で学習、TEST={TEST_START}〜。")
    emit()

    snaps, n_raw = load_snapshots()
    w = per_race_table(snaps)
    pay = load_payouts()
    board, chk = load_combo()

    settled = {bt: set(zip(pay[bt].race_date, pay[bt].place_code, pay[bt].race_no)) for bt in ["rt3", "rf3"]}

    full = {}
    for bt in ["rt3", "rf3"]:
        m = attach_outcome(w, bt, pay[bt], settled[bt])
        full[bt] = attach_odds(m, bt, board[bt])
    base = full["rf3"].copy()
    rt_hit = full["rt3"][KEYS + ["hit", "settled"]].rename(columns={"hit": "hit_rt3", "settled": "settled_rt3"})
    base = base.merge(rt_hit, on=KEYS, how="left").rename(columns={"hit": "hit_rf3", "settled": "settled_rf3"})
    base["settled_rt3"] = base["settled_rt3"].fillna(False).astype(bool)

    for i in range(4):
        base[f"x{i}"] = logit(base[f"pred_calib{i}"].values)
    xc = [f"x{i}" for i in range(4)]
    trn = base[(base["race_date"] <= TRAIN_END) & base["settled_rf3"]]
    lr = LogisticRegression(C=1e10, max_iter=2000)
    lr.fit(trn[xc].values, trn["hit_rf3"].values)
    base["p_rf3"] = lr.predict_proba(base[xc].values)[:, 1]
    trn_rt = trn[trn["settled_rt3"] & (trn["hit_rf3"] == 1)]
    q = trn_rt["hit_rt3"].mean()
    base["p_rt3"] = base["p_rf3"] * q

    emit("## 0. サニティチェック")
    emit()
    emit(f"- odds_snapshots: 生レース数 {n_raw} / 最新 capture のみ {snaps.groupby(KEYS).ngroups} / top4 揃う {len(w)} "
         f"/ 期間 {w.race_date.min()}〜{w.race_date.max()}")
    emit(f"- payouts 決済済レース (NaN除外): rt3 {len(settled['rt3'])}, rf3 {len(settled['rf3'])} (payouts 最終日 "
         f"{max(d for d, _, _ in settled['rf3'])})")
    emit(f"- odds_combo_snapshots 盤面レース数: rt3 {chk['rt3_races_with_board']}, rf3 {chk['rf3_races_with_board']}")
    emit(f"- **rf3 盤面の車番昇順チェック**: car_no_1<car_no_2<car_no_3 の割合 = {chk['rf3_sorted_ratio']:.4f}"
         + (" (全行昇順。結合前に念のため sort 済)" if chk['rf3_sorted_ratio'] == 1 else " (非昇順あり→sort で正規化)"))
    emit(f"- ロジスティック係数 (logit p1..p4): {np.round(lr.coef_[0], 3).tolist()}, 切片 {lr.intercept_[0]:.3f}  (TRAIN {len(trn)}R)")
    emit(f"- q (TRAIN: rf3的中のうち a→b→c 順序一致の割合) = {q:.4f} (n_rf3hit={len(trn_rt)})")
    allset = base[base["settled_rf3"]]
    emit(f"- top3(pred_calib) rf3 的中率: 全期間 {allset.hit_rf3.mean():.4f} (n={len(allset)}); "
         f"TRAIN {allset[allset.race_date <= TRAIN_END].hit_rf3.mean():.4f}, TEST {allset[allset.race_date >= TEST_START].hit_rf3.mean():.4f}")
    allr = base[base["settled_rt3"]]
    emit(f"- rt3 的中率: 全期間 {allr.hit_rt3.mean():.4f} (n={len(allr)})")

    universes = {}
    for bt in ["rt3", "rf3"]:
        m = full[bt].merge(base[KEYS + ["p_rf3", "p_rt3"]], on=KEYS, how="left")
        m[f"ov_{bt}"] = m[f"p_{bt}"] * m[f"odds_{bt}"]
        universes[bt] = m
        te_all = m[(m.race_date >= TEST_START) & m.settled]
        te_pool = m[(m.race_date >= TEST_START)]
        board_races = set(zip(board[bt].race_date, board[bt].place_code, board[bt].race_no))
        hasb = np.array([(a, b, c) in board_races for a, b, c in zip(te_all.race_date, te_all.place_code, te_all.race_no)])
        te_hasboard = te_all[hasb]
        emit(f"- [{bt}] TEST 決済済 {len(te_all)}R のうち 盤面なし(レース単位) {int((~hasb).sum())}R、"
             f"盤面ありだが top3 コンボのオッズ欠損 {int(te_hasboard[f'odds_{bt}'].isna().sum())}R "
             f"(TEST 未決済/payout 無し {len(te_pool) - len(te_all)}R は除外済)")
        tr_all = m[(m.race_date <= TRAIN_END) & (m.race_date >= "2026-06-10") & m.settled]
        emit(f"- [{bt}] TRAIN (06-10〜08-31) 決済済 {len(tr_all)}R のうち オッズ欠損 {int(tr_all[f'odds_{bt}'].isna().sum())}R")
        h = m[(m.hit == 1) & m[f"odds_{bt}"].notna()]
        ratio = h["ret"] / (h[f"odds_{bt}"] * 100)
        emit(f"- [{bt}] 的中レースで 確定払戻 / (発火時オッズ×100) の中央値 {ratio.median():.2f}, 平均 {ratio.mean():.2f} (n={len(h)})")
    emit()
    emit("注: 以降の評価ユニバースは「決済済 かつ 発火時コンボオッズあり」のレース (R1 を含む全ルールで同一母集団)。"
         "TRAIN 評価は盤面が存在する 2026-06-10 以降のみ。")
    emit()

    emit("## 1. 確率モデルの TEST キャリブレーション")
    emit()
    te_base = base[(base.race_date >= TEST_START) & base["settled_rf3"]]
    calib_table(te_base, "p_rf3", "hit_rf3", 10, f"rf3 P̂ 十分位 (TEST, 全場 n={len(te_base)})")
    te_pol = te_base[te_base.place_code.isin([3, 4, 6])]
    calib_table(te_pol, "p_rf3", "hit_rf3", 5, f"rf3 P̂ 五分位 (TEST, 場 3/4/6 n={len(te_pol)})")
    te_b3 = base[(base.race_date >= TEST_START) & base["settled_rt3"]].copy()
    calib_table(te_b3, "p_rt3", "hit_rt3", 5, f"rt3 P̂ 五分位 (TEST, 全場 n={len(te_b3)}) ※rt3 的中は希少")
    emit("場別 (TEST) 平均P̂_rf3 vs 実際:")
    emit()
    emit("| 場 | n | 平均P̂ | 実際 |\n|---|---|---|---|")
    for pc, g in te_base.groupby("place_code"):
        emit(f"| {pc} | {len(g)} | {g.p_rf3.mean():.4f} | {g.hit_rf3.mean():.4f} |")
    emit()

    emit("## 2. ルール比較 (主結果: 現行ポリシー対象の場×券種)")
    emit()
    for bt in ["rf3", "rt3"]:
        places = sorted({int(p) for p, b in POLICY_PAIRS if b == bt})
        m = universes[bt]
        d = m[m.settled & m[f"odds_{bt}"].notna() & m.place_code.isin(places)].copy()
        emit(f"### {bt} — ポリシー場 {places}")
        emit()
        analyze(d, bt, f"場 {places}")
    emit("## 3. (exploratory) 全場 2-6")
    emit()
    for bt in ["rf3", "rt3"]:
        m = universes[bt]
        d = m[m.settled & m[f"odds_{bt}"].notna()].copy()
        emit(f"### {bt} — 全場 2-6 (探索用、k は全場 TRAIN で再選択)")
        emit()
        analyze(d, bt, "全場 2-6")

    emit("## 4. 補助: fns_raw_ev と三連系 ROI の関係 (全期間・全場・盤面あり)")
    emit()
    for bt in ["rf3", "rt3"]:
        m = universes[bt]
        d = m[m.settled & m[f"odds_{bt}"].notna()]
        corr = d[["fns_raw_ev", f"ov_{bt}"]].corr(method="spearman").iloc[0, 1]
        emit(f"- [{bt}] Spearman(fns_raw_ev, overlay) = {corr:.3f} (n={len(d)})")
        emit()
        emit("| fns_raw_ev 帯 | n | hit% | ROI% |\n|---|---|---|---|")
        for lo, hi in [(0, 1.2), (1.2, 1.5), (1.5, 1.8), (1.8, 2.2), (2.2, 99)]:
            g = d[(d.fns_raw_ev >= lo) & (d.fns_raw_ev < hi)]
            if len(g):
                emit(f"| [{lo},{hi}) | {len(g)} | {g.hit.mean() * 100:.1f} | {g.ret.sum() / (100 * len(g)) * 100:.1f} |")
        emit()

    emit("## 5. 注意点 (caveats)")
    emit()
    emit("- TRAIN の三連系評価は盤面が存在する 2026-06-10〜08-31 のみ (約 2.7 ヶ月)。P̂ のロジスティック学習自体は 04-30〜08-31 の全 snapshot を使用。")
    emit("- TRAIN の overlay は P̂ を同じ TRAIN で学習しているため in-sample (特徴 4 個・数千レースなので過学習は小さいが、k 選択は楽観的)。")
    emit("- rt3 は的中が希少で高配当に依存するため ROI の分散が極めて大きい。bootstrap CI は広く、点推定の差の大半はノイズ圏内。")
    emit("- 評価した (ルール×券種×k×帯) の数が多く、多重比較の問題がある。TEST の「有望そうな」セルは偶然の可能性を排除できない。")
    emit("- bootstrap はレース単位 iid 前提。日次・場内の相関や時期の非定常性 (モデル週次再学習、pred_calib 分布変化) は考慮していない。")
    emit("- ROI は発火時オッズではなく **確定払戻** で計算。overlay 判定は発火時オッズ×P̂ なので、確定オッズが発火時より低下する傾向があれば優位は実現しにくい。購入による自己オッズ変動は未考慮。")
    emit("- R0/R0raw は fns_raw_ev (top-1 複勝) を全期間に一様適用。実運用の購入履歴 (実弾 ROI ~80%) とは母集団が異なる。")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nsaved: {OUT}")


if __name__ == "__main__":
    main()
