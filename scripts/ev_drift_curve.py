"""EV drift カーブ測定 (2026-08-08 導入)。

背景 (2026-08-07 夜次反省ログ):
    「発火EV→確定EV で閾値 1.5 を跨いだレースが 4/6 件」
    = 発走 4 分前に EV>=1.50 と判断して発注した pick の 2/3 が、
      確定オッズでは EV<1.50 に落ちていた。実質は期待値のない賭けを
      打っている疑いがあり、累積 ROI 88.3% (274R) と整合的。

このスクリプトは data/odds_ts/*.jsonl (2026-07-05 〜 蓄積中、
発走 -60/-30/-15/-8/-4/-3/+2 分の 7 時点スナップショット) から
「各時点の複勝オッズが確定 (+2 分) に対して何倍だったか」を測り、
発火時 EV に掛けるべき割引係数を推定する。

なぜ odds_ts 単独で EV の話ができるか:
    EV(t) = pred_calib * (fns_min(t) + fns_max(t)) / 2
    pred_calib は同一レース・同一車なら時点によらず一定。よって
    EV(t) / EV(+2) = odds_avg(t) / odds_avg(+2)
    となり、モデル出力を結合しなくても EV の比率は複勝オッズ比だけで
    determinable。閾値跨ぎの実件数だけは pred が要るので、
    --snapshots 指定時のみ odds_snapshots.csv を結合して追加集計する。

CLAUDE.md「数値判断のチェックポイント」に従い、--self-test で
合成データによる sanity check を実行できる (ROI 検算と同じ趣旨)。

使い方:
  python scripts/ev_drift_curve.py
  python scripts/ev_drift_curve.py --since 2026-07-20
  python scripts/ev_drift_curve.py --thr 1.50 --snapshots   # 閾値跨ぎ実測も
  python scripts/ev_drift_curve.py --self-test              # sanity check のみ
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TS_DIR = DATA / "odds_ts"
RACE_KEY = ["race_date", "place_code", "race_no"]

BASE_OFFSET = 2       # 確定とみなす offset (+2 分 = 締切後プール)
FIRE_OFFSET = -4      # 現行 LEAD_MIN (dynamic_scheduler.LEAD_MIN)

# daily_predict.py と同一の異常オッズ判定 (EV 59.28 バグ対策と同じ基準)
ODDS_MAX_CAP = 50.0
ODDS_RATIO_CAP = 20.0
ODDS_MIN_FLOOR = 1.1


def _clean_odds(v) -> float:
    """オッズ文字列 → float。'--' や欠測は NaN。"""
    if v is None:
        return float("nan")
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else float("nan")
    s = str(v).strip().replace(",", "")
    if not s or s in {"--", "-", "***", "0", "0.0"}:
        return float("nan")
    try:
        f = float(s)
    except ValueError:
        return float("nan")
    return f if f > 0 else float("nan")


def is_anomalous(odds_min: pd.Series, odds_max: pd.Series) -> pd.Series:
    """daily_predict と同じ 3 条件で異常オッズを検出。"""
    return (
        (odds_max > ODDS_MAX_CAP)
        | ((odds_min > 0) & (odds_max / odds_min > ODDS_RATIO_CAP))
        | ((odds_min < ODDS_MIN_FLOOR) & (odds_max < ODDS_MIN_FLOOR))
    )


def extract_rows(rec: dict) -> list[dict]:
    """1 スナップショット (jsonl 1 行) → 車ごとの複勝オッズ行。"""
    body = rec.get("body") or {}
    if not isinstance(body, dict):
        return []
    fns = body.get("fnsOddsList")
    if not isinstance(fns, dict):
        return []
    players = body.get("playerList")
    if not isinstance(players, list):
        return []

    out = []
    for p in players:
        if not isinstance(p, dict):
            continue
        car_no = p.get("carNo")
        if car_no is None:
            continue
        entry = fns.get(str(car_no))
        if not isinstance(entry, dict):
            continue
        out.append({
            "race_date": rec.get("race_date"),
            "place_code": rec.get("place_code"),
            "race_no": rec.get("race_no"),
            "offset_min": rec.get("offset_min"),
            "car_no": int(car_no),
            "odds_min": _clean_odds(entry.get("min")),
            "odds_max": _clean_odds(entry.get("max")),
        })
    return out


def load_odds_ts(since: str | None, verbose: bool = True) -> pd.DataFrame:
    """odds_ts/*.jsonl をストリーミングで読み、複勝オッズだけ抽出。

    body は全券種 (rt3 は 336 通り) を含み 1 ヶ月で数百 MB になるため、
    行ごとに必要な列だけ取り出して捨てる。
    """
    if not TS_DIR.exists():
        print(f"[error] {TS_DIR} が存在しない (このPCに odds_ts が未同期)",
              file=sys.stderr)
        sys.exit(1)

    files = sorted(TS_DIR.glob("*.jsonl"))
    if since:
        files = [f for f in files if f.stem >= since]
    if not files:
        print("[error] 対象 jsonl が 0 件", file=sys.stderr)
        sys.exit(1)

    rows: list[dict] = []
    n_lines = n_bad = 0
    for i, path in enumerate(files, 1):
        if verbose:
            print(f"  [{i}/{len(files)}] {path.name} ...", end="", flush=True)
        before = len(rows)
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    n_lines += 1
                    try:
                        rec = json.loads(line)
                    except Exception:
                        n_bad += 1
                        continue
                    rows.extend(extract_rows(rec))
        except Exception as e:
            print(f" read failed: {e}")
            continue
        if verbose:
            print(f" +{len(rows) - before} rows")

    if verbose:
        print(f"  -> {n_lines} snapshots, {len(rows)} car-rows"
              f"{f' ({n_bad} unparsable)' if n_bad else ''}")

    df = pd.DataFrame(rows)
    if df.empty:
        print("[error] 抽出行が 0 (fnsOddsList が空?)", file=sys.stderr)
        sys.exit(1)
    df["odds_avg"] = (df["odds_min"] + df["odds_max"]) / 2
    df.loc[is_anomalous(df["odds_min"], df["odds_max"]), "odds_avg"] = np.nan
    return df


def build_ratio_table(df: pd.DataFrame) -> pd.DataFrame:
    """各 (race, car, offset) を同一車の +2 分値とペアリングし比率を付ける。

    比率 = odds_avg(t) / odds_avg(+2)
         = EV(t) / EV(+2)   (pred_calib が約分されるため)
    1 を超える = 発火時のほうがオッズが高い = EV を過大評価していた。
    """
    key = RACE_KEY + ["car_no"]
    base = df[df["offset_min"] == BASE_OFFSET][key + ["odds_avg"]].rename(
        columns={"odds_avg": "close_avg"}
    )
    # 同一キーの重複スナップは念のため中央値に畳む (通常は 1 件)
    base = base.groupby(key, as_index=False)["close_avg"].median()

    merged = df[df["offset_min"] != BASE_OFFSET].merge(base, on=key, how="inner")
    merged = merged.dropna(subset=["odds_avg", "close_avg"])
    merged = merged[merged["close_avg"] > 0]
    merged["ratio"] = merged["odds_avg"] / merged["close_avg"]

    # 人気順 (確定オッズの昇順) を付与。pred-top1 は概ね人気上位に集中するため、
    # 「favorite」= 確定複勝オッズ最小の車 を pred-top1 の代理指標として使う。
    # 順位はレース単位の確定オッズだけで決める (offset ごとの欠測で順位が
    # ブレると「最人気車」の定義が時点によって変わってしまうため)。
    fav = base.dropna(subset=["close_avg"]).copy()
    fav["fav_rank"] = fav.groupby(RACE_KEY)["close_avg"].rank(method="min").astype(int)
    merged = merged.merge(fav[key + ["fav_rank"]], on=key, how="left")
    return merged


def print_curve(ratio: pd.DataFrame, label: str) -> None:
    """offset ごとの比率分布を 1 行ずつ出力。"""
    print(f"\n[{label}]")
    print(f"  {'offset':>8} {'n':>6} {'odds_avg':>9} {'ratio_mu':>9} "
          f"{'ratio_med':>10} {'>1.0率':>8}")
    print(f"  {'-' * 56}")
    for off in sorted(ratio["offset_min"].unique()):
        sub = ratio[ratio["offset_min"] == off]
        if sub.empty:
            continue
        over = (sub["ratio"] > 1.0).mean() * 100
        print(f"  {off:+7d}m {len(sub):6d} {sub['odds_avg'].mean():9.3f} "
              f"{sub['ratio'].mean():9.4f} {sub['ratio'].median():10.4f} "
              f"{over:7.1f}%")
    # 基準行: 対象となった (race, car) のユニーク集合における確定オッズ平均。
    # offset 行と違い重複カウントしないので、上の odds_avg 列と直接比較できる。
    uniq = ratio.drop_duplicates(subset=RACE_KEY + ["car_no"])
    print(f"  {'+2m 基準':>7}  {len(uniq):6d} {uniq['close_avg'].mean():9.3f} "
          f"{1.0:9.4f} {1.0:10.4f} {'--':>8}")


def print_correction(ratio: pd.DataFrame, thr: float) -> None:
    """発火 offset における割引係数と実効閾値を出す (本スクリプトの結論部)。"""
    fire = ratio[ratio["offset_min"] == FIRE_OFFSET]
    print(f"\n=== 発火時 EV の割引補正 (offset={FIRE_OFFSET:+d}min) ===")
    if fire.empty:
        print(f"  offset={FIRE_OFFSET} のペアが 0 件 — 補正係数を出せない")
        return

    for scope, sub in (("全車", fire), ("最人気車のみ", fire[fire["fav_rank"] == 1])):
        if sub.empty:
            print(f"  {scope:12s}: データなし")
            continue
        med = sub["ratio"].median()
        mu = sub["ratio"].mean()
        disc = 1.0 / med if med > 0 else float("nan")
        eff_thr = thr * med
        print(f"  {scope:12s} n={len(sub):5d}  "
              f"ratio med={med:.4f} mu={mu:.4f}")
        print(f"  {'':12s}   -> 発火 EV に x{disc:.4f} を掛けると確定 EV の中央値推定")
        print(f"  {'':12s}   -> thr={thr:.2f} を維持したいなら発火時 thr={eff_thr:.4f} 相当に引き上げ")


def print_threshold_crossing(ratio: pd.DataFrame, thr: float) -> None:
    """odds_snapshots.csv があれば、実発火 pick の閾値跨ぎ率を実測する。"""
    snap_path = DATA / "odds_snapshots.csv"
    print(f"\n=== 閾値跨ぎ実測 (thr={thr}) ===")
    if not snap_path.exists() or snap_path.stat().st_size == 0:
        print("  data/odds_snapshots.csv が無いためスキップ (--snapshots は家 PC で)")
        return

    snap = pd.read_csv(snap_path, low_memory=False)
    need = set(RACE_KEY + ["car_no", "pred_calib", "ev_avg_calib"])
    if not need.issubset(snap.columns):
        print(f"  odds_snapshots.csv に必要列が無い (要 {sorted(need)})")
        return
    snap = snap[snap.get("pred_rank", 1) == 1] if "pred_rank" in snap.columns else snap
    snap = snap[RACE_KEY + ["car_no", "pred_calib", "ev_avg_calib"]].copy()
    snap["race_date"] = snap["race_date"].astype(str)

    r = ratio[ratio["offset_min"] == FIRE_OFFSET].copy()
    r["race_date"] = r["race_date"].astype(str)
    m = r.merge(snap, on=RACE_KEY + ["car_no"], how="inner")
    if m.empty:
        print("  odds_ts と odds_snapshots の突合が 0 件 (期間が重なっていない可能性)")
        return

    m["close_ev"] = m["pred_calib"] * m["close_avg"]
    fired = m[m["ev_avg_calib"] >= thr]
    if fired.empty:
        print(f"  発火 (EV>={thr}) 該当が 0 件 / 突合 {len(m)} 件")
        return
    kept = (fired["close_ev"] >= thr).sum()
    print(f"  突合 {len(m)} 件 / 発火 {len(fired)} 件")
    print(f"  確定でも EV>={thr} を維持: {kept}/{len(fired)} "
          f"({kept / len(fired) * 100:.1f}%)")
    print(f"  跨ぎ (維持できず): {len(fired) - kept}/{len(fired)} "
          f"({(1 - kept / len(fired)) * 100:.1f}%)")
    print(f"  確定 EV の平均 {fired['close_ev'].mean():.3f} "
          f"(発火時 {fired['ev_avg_calib'].mean():.3f})")


def self_test() -> int:
    """合成データで比率計算を検算する (CLAUDE.md の sanity check 推奨に対応)。

    仕込む事実:
      - car1: 発火 -4min で avg 3.0、確定 +2min で avg 2.0 → ratio 1.5
      - car2: 発火 -4min で avg 1.0、確定 +2min で avg 2.0 → ratio 0.5
      - car3: 確定側が異常オッズ (min=max=1.0) → NaN 化され除外されるべき
    """
    print("=== self-test (合成データ) ===")
    rows = []
    for off, (a1, a2) in {(-4): ((2.5, 3.5), (0.8, 1.2)),
                          2: ((1.5, 2.5), (1.5, 2.5))}.items():
        rows += [
            {"race_date": "2026-07-01", "place_code": 4, "race_no": 1,
             "offset_min": off, "car_no": 1, "odds_min": a1[0], "odds_max": a1[1]},
            {"race_date": "2026-07-01", "place_code": 4, "race_no": 1,
             "offset_min": off, "car_no": 2, "odds_min": a2[0], "odds_max": a2[1]},
            {"race_date": "2026-07-01", "place_code": 4, "race_no": 1,
             "offset_min": off, "car_no": 3, "odds_min": 1.0, "odds_max": 1.0},
        ]
    df = pd.DataFrame(rows)
    df["odds_avg"] = (df["odds_min"] + df["odds_max"]) / 2
    df.loc[is_anomalous(df["odds_min"], df["odds_max"]), "odds_avg"] = np.nan

    ratio = build_ratio_table(df)
    ok = True

    got = ratio[ratio["car_no"] == 1]["ratio"]
    exp = 3.0 / 2.0
    if len(got) != 1 or abs(got.iloc[0] - exp) > 1e-9:
        print(f"  [NG] car1 ratio 期待 {exp} 実際 {list(got)}")
        ok = False
    else:
        print(f"  [OK] car1 ratio = {got.iloc[0]:.4f} (期待 {exp})")

    got = ratio[ratio["car_no"] == 2]["ratio"]
    exp = 1.0 / 2.0
    if len(got) != 1 or abs(got.iloc[0] - exp) > 1e-9:
        print(f"  [NG] car2 ratio 期待 {exp} 実際 {list(got)}")
        ok = False
    else:
        print(f"  [OK] car2 ratio = {got.iloc[0]:.4f} (期待 {exp})")

    if (ratio["car_no"] == 3).any():
        print("  [NG] car3 (異常オッズ) が除外されていない")
        ok = False
    else:
        print("  [OK] car3 (min=max=1.0) は異常判定で除外")

    # 人気順: 確定 avg が car2=2.0, car1=2.0 で同値、car3 は除外
    print(f"  [--] fav_rank 分布: {sorted(ratio['fav_rank'].unique())}")

    print(f"\n  self-test: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", type=str, default=None, help="YYYY-MM-DD 以降の jsonl のみ")
    p.add_argument("--thr", type=float, default=1.50, help="EV 閾値 (default 1.50)")
    p.add_argument("--snapshots", action="store_true",
                   help="odds_snapshots.csv を結合して閾値跨ぎを実測")
    p.add_argument("--self-test", action="store_true", help="合成データで検算のみ")
    args = p.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.self_test:
        return self_test()

    print("=== EV drift カーブ (odds_ts) ===")
    print("読み込み中...")
    df = load_odds_ts(args.since)

    dates = sorted(df["race_date"].dropna().unique())
    n_race = df.groupby(RACE_KEY).ngroups
    print(f"\n対象: {dates[0]} 〜 {dates[-1]} / {len(dates)} 日 / {n_race} レース")
    print(f"offset 別スナップ数: "
          f"{dict(df.groupby('offset_min')['car_no'].count().sort_index())}")

    ratio = build_ratio_table(df)
    if ratio.empty:
        print("[error] +2min とペアリングできた行が 0 件", file=sys.stderr)
        return 1

    print_curve(ratio, "全車")
    print_curve(ratio[ratio["fav_rank"] == 1], "最人気車のみ (pred-top1 の代理)")
    print_correction(ratio, args.thr)
    if args.snapshots:
        print_threshold_crossing(ratio, args.thr)

    print("\n=== 読み方 ===")
    print("  ratio = odds_avg(t) / odds_avg(+2min) = EV(t) / EV(確定)")
    print("  ratio > 1 : 発火時のほうがオッズが高い = EV を過大評価していた")
    print("              (締切直前に金が集まりオッズが下がる = 本命化)")
    print("  ratio < 1 : 発火時のほうが低い = EV を過小評価 (穴化)")
    print("  '>1.0率' が 50% を大きく超えるなら、drift は偶然でなく系統的バイアス。")
    print("  その場合は上の割引係数を daily_predict の EV に掛けるか、")
    print("  発火時 thr を実効値まで引き上げるのが直接的な対処。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
