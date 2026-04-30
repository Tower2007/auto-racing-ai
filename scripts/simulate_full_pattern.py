"""1 日 1 場の全レース×全 7 券種シミュレーション(Markdown 出力)

仕様:
- 入力: 日付 + 場所 (例: 2026-04-29 iizuka または 5)
- 出力: 12 R 分の買い目を Markdown で出力。1 R で 5 券種 × ¥100 = ¥500 投資
  (二車連 rfw / 二車単 rtw は除外)
- 選定: pred_calib(中間モデル isotonic 校正)の top1〜top3 を使い回す
  単勝/複勝: top1
  ワイド: top1 と top2 (順序なし、車番昇順)
  三連複: top1, top2, top3 (順序なし、車番昇順)
  三連単: top1 → top2 → top3 (順序あり)
- オッズ: 単勝・複勝は odds_summary.csv から表示、連単系は記録なし「-」
- 結果: payouts.csv の bet_type/car_no を参照し、買い目と一致すれば的中・払戻

券種コード:
  tns=単勝, fns=複勝, wid=ワイド, rtw=二車単, rfw=二車連, rt3=三連単, rf3=三連複

使い方:
  python scripts/simulate_full_pattern.py 2026-04-29 5
  python scripts/simulate_full_pattern.py 2026-04-29 iizuka
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RACE_KEY = ["race_date", "place_code", "race_no"]
BET = 100
CALIB_CUTOFF = "2024-04"

VENUE_NAMES = {2: "kawaguchi", 3: "isesaki", 4: "hamamatsu", 5: "iizuka", 6: "sanyou"}
NAME_TO_PC = {v: k for k, v in VENUE_NAMES.items()}

BET_LABELS = {
    "tns": "単勝",
    "fns": "複勝",
    "wid": "ワイド",
    "rfw": "二車連",
    "rtw": "二車単",
    "rf3": "三連複",
    "rt3": "三連単",
}
BET_ORDER = ["tns", "fns", "wid", "rf3", "rt3"]  # 二車連(rfw)・二車単(rtw)は除外


def fmt_yen(v: float) -> str:
    if pd.isna(v):
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(int(v)):,}"


def fmt_combo(bt: str, cars: list[int]) -> str:
    """車番リストを bet_type に応じた表記に。順序あり=「→」、なし=「-」"""
    if bt in ("tns", "fns"):
        return str(cars[0])
    if bt in ("rtw", "rt3"):
        return "→".join(str(c) for c in cars)
    # wid, rfw, rf3: 昇順表記
    return "-".join(str(c) for c in sorted(cars))


def make_picks_for_race(top_cars: list[int]) -> dict[str, list[int]]:
    """top1, top2, top3 から各券種の車番を決める。"""
    t1 = top_cars[0]
    t2 = top_cars[1] if len(top_cars) >= 2 else None
    t3 = top_cars[2] if len(top_cars) >= 3 else None
    return {
        "tns": [t1],
        "fns": [t1],
        "wid": [t1, t2] if t2 else [],
        "rfw": [t1, t2] if t2 else [],
        "rtw": [t1, t2] if t2 else [],
        "rf3": [t1, t2, t3] if (t2 and t3) else [],
        "rt3": [t1, t2, t3] if (t2 and t3) else [],
    }


def check_hit(bt: str, picked: list[int], pay_rows: pd.DataFrame) -> tuple[bool, float]:
    """payouts の該当 bet_type 行を見て、picked 買い目が当たってるか判定。
    戻り値: (hit, refund)"""
    if not picked or pay_rows.empty:
        return False, 0.0
    if bt == "tns":
        match = pay_rows[pay_rows["car_no_1"] == picked[0]]
    elif bt == "fns":
        match = pay_rows[pay_rows["car_no_1"] == picked[0]]
    elif bt == "wid":
        s = sorted(picked)
        # wid は car_no_1<car_no_2 順で記録
        match = pay_rows[
            ((pay_rows["car_no_1"] == s[0]) & (pay_rows["car_no_2"] == s[1])) |
            ((pay_rows["car_no_1"] == s[1]) & (pay_rows["car_no_2"] == s[0]))
        ]
    elif bt == "rfw":
        s = sorted(picked)
        match = pay_rows[
            ((pay_rows["car_no_1"] == s[0]) & (pay_rows["car_no_2"] == s[1])) |
            ((pay_rows["car_no_1"] == s[1]) & (pay_rows["car_no_2"] == s[0]))
        ]
    elif bt == "rtw":
        # 順序あり
        match = pay_rows[
            (pay_rows["car_no_1"] == picked[0]) & (pay_rows["car_no_2"] == picked[1])
        ]
    elif bt == "rf3":
        s = sorted(picked)
        # rf3 は車番昇順で記録
        match = pay_rows[
            (pay_rows["car_no_1"] == s[0]) &
            (pay_rows["car_no_2"] == s[1]) &
            (pay_rows["car_no_3"] == s[2])
        ]
    elif bt == "rt3":
        match = pay_rows[
            (pay_rows["car_no_1"] == picked[0]) &
            (pay_rows["car_no_2"] == picked[1]) &
            (pay_rows["car_no_3"] == picked[2])
        ]
    else:
        return False, 0.0
    if match.empty:
        return False, 0.0
    return True, float(match["refund"].sum())


def parse_venue(s: str) -> int:
    """場名 or 数字 → place_code"""
    if s.isdigit():
        return int(s)
    if s in NAME_TO_PC:
        return NAME_TO_PC[s]
    raise ValueError(f"場名が不明: {s} (有効: {list(NAME_TO_PC.keys())} or 2-6)")


def load_data():
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])

    # 校正用 (2024-04 以前で fit)
    calib = preds[preds["test_month"] < CALIB_CUTOFF]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(calib["pred"].values, calib["target_top3"].values)
    preds["pred_calib"] = iso.transform(preds["pred"].values)
    return preds, odds, pay


def main():
    p = argparse.ArgumentParser()
    p.add_argument("date", help="対象日 (YYYY-MM-DD)")
    p.add_argument("venue", help="場 (kawaguchi/isesaki/hamamatsu/iizuka/sanyou or 2-6)")
    args = p.parse_args()

    target_date = pd.Timestamp(args.date)
    pc = parse_venue(args.venue)
    venue = VENUE_NAMES[pc]

    preds, odds, pay = load_data()

    # 当日のレース一覧
    day_preds = preds[(preds["race_date"] == target_date) & (preds["place_code"] == pc)]
    if day_preds.empty:
        print(f"対象データなし: {target_date.date()} {venue} (place_code={pc})")
        print(f"OOF データの範囲: {preds['race_date'].min().date()} 〜 {preds['race_date'].max().date()}")
        return
    races = sorted(day_preds["race_no"].unique())

    # 当日の odds と payouts
    day_odds = odds[(odds["race_date"] == target_date) & (odds["place_code"] == pc)]
    day_pay = pay[(pay["race_date"] == target_date) & (pay["place_code"] == pc)]

    # ── レース毎にピックを作成 ──
    md = []
    md.append(f"# 全購入パターン シミュレーション: {target_date.date()} {venue}")
    md.append("")
    md.append(f"**戦略**: 中間モデル(isotonic 校正)の top1〜top3 を 5 券種に割り付け、各 ¥{BET}")
    md.append("(二車連・二車単は除外、1 R で ¥500 投資)")
    md.append("**入力データ**: walkforward_predictions_morning_top3 + odds_summary + payouts(全て事後)")
    md.append("")
    md.append("選定ルール:")
    md.append("- 単勝・複勝: top1")
    md.append("- ワイド: top1 + top2 (車番昇順)")
    md.append("- 三連複: top1+top2+top3 (車番昇順)")
    md.append("- 三連単: top1→top2→top3 (順序あり)")
    md.append("")

    grand_cost = 0
    grand_refund = 0
    by_bet = {bt: {"hit": 0, "n": 0, "cost": 0, "refund": 0} for bt in BET_ORDER}

    for r in races:
        race_preds = day_preds[day_preds["race_no"] == r].sort_values("pred_calib", ascending=False)
        top_cars = race_preds["car_no"].tolist()[:3]

        # オッズ参照(単勝・複勝)
        race_odds = day_odds[day_odds["race_no"] == r]
        win_odds_map = dict(zip(race_odds["car_no"], race_odds["win_odds"]))
        place_min_map = dict(zip(race_odds["car_no"], race_odds["place_odds_min"]))
        place_max_map = dict(zip(race_odds["car_no"], race_odds["place_odds_max"]))

        # 払戻参照
        race_pay = day_pay[day_pay["race_no"] == r]

        picks = make_picks_for_race(top_cars)

        md.append(f"## R{r}")
        md.append("")
        # 上位 3 車の予測値表示
        md.append("**予測 top 3**")
        md.append("")
        md.append("| 車 | pred_calib | 単勝 | 複勝 |")
        md.append("|---:|---:|---:|---:|")
        for _, rp in race_preds.head(3).iterrows():
            cn = int(rp["car_no"])
            wn = win_odds_map.get(cn)
            pmn = place_min_map.get(cn)
            pmx = place_max_map.get(cn)
            md.append(
                f"| {cn} | {rp['pred_calib']:.3f} | "
                f"{f'{wn:.1f}' if pd.notna(wn) else '—'} | "
                f"{f'{pmn:.1f}-{pmx:.1f}' if pd.notna(pmn) else '—'} |"
            )
        md.append("")
        md.append("**買い目** (¥100 × 5 券種 = ¥500 投資)")
        md.append("")
        md.append("| 券種 | 買い目 | オッズ | 結果 | 払戻 |")
        md.append("|---|---|---:|---:|---:|")

        race_cost = 0
        race_refund = 0
        for bt in BET_ORDER:
            label = BET_LABELS[bt]
            picked = picks.get(bt, [])
            if not picked:
                md.append(f"| {label} | — | — | — | — |")
                continue
            combo = fmt_combo(bt, picked)
            # オッズ表示 (tns/fns のみ)
            if bt == "tns":
                od = win_odds_map.get(picked[0])
                od_s = f"{od:.1f}" if pd.notna(od) else "—"
            elif bt == "fns":
                pmn = place_min_map.get(picked[0])
                pmx = place_max_map.get(picked[0])
                od_s = f"{pmn:.1f}-{pmx:.1f}" if pd.notna(pmn) else "—"
            else:
                od_s = "—"
            # 的中判定
            bt_pay = race_pay[race_pay["bet_type"] == bt]
            hit, refund = check_hit(bt, picked, bt_pay)
            mark = "○" if hit else "✗"
            md.append(f"| {label} | {combo} | {od_s} | {mark} | {fmt_yen(refund)} |")
            race_cost += BET
            race_refund += refund
            by_bet[bt]["n"] += 1
            by_bet[bt]["cost"] += BET
            by_bet[bt]["refund"] += refund
            if hit:
                by_bet[bt]["hit"] += 1

        md.append("")
        md.append(f"R{r} 小計: 投資 {fmt_yen(race_cost)} / 払戻 {fmt_yen(race_refund)} / "
                  f"収支 **{fmt_yen(race_refund - race_cost)}**")
        md.append("")

        grand_cost += race_cost
        grand_refund += race_refund

    # ── サマリ ──
    md.append("## 1 日サマリ")
    md.append("")
    md.append(f"- 投資合計: **{fmt_yen(grand_cost)}** ({grand_cost // BET} 点)")
    md.append(f"- 払戻合計: **{fmt_yen(grand_refund)}**")
    md.append(f"- **収支: {fmt_yen(grand_refund - grand_cost)}** "
              f"(ROI {grand_refund/grand_cost*100:.1f}%)" if grand_cost else "")
    md.append("")
    md.append("### 券種別")
    md.append("")
    md.append("| 券種 | 買 | 当 | hit% | 投資 | 払戻 | 収支 | ROI |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for bt in BET_ORDER:
        s = by_bet[bt]
        if s["n"] == 0:
            continue
        hit_rate = s["hit"] / s["n"] * 100
        roi = s["refund"] / s["cost"] * 100 if s["cost"] else 0
        md.append(
            f"| {BET_LABELS[bt]} | {s['n']} | {s['hit']} | {hit_rate:.1f}% | "
            f"{fmt_yen(s['cost'])} | {fmt_yen(s['refund'])} | "
            f"**{fmt_yen(s['refund'] - s['cost'])}** | {roi:.1f}% |"
        )
    md.append("")

    out = REPORTS / f"sim_full_{target_date.date()}_{venue}.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")
    print(f"投資 {fmt_yen(grand_cost)} → 払戻 {fmt_yen(grand_refund)} / 収支 {fmt_yen(grand_refund - grand_cost)} "
          f"({grand_refund/grand_cost*100:.1f}%)")
    print()
    for bt in BET_ORDER:
        s = by_bet[bt]
        if s["n"] == 0:
            continue
        roi = s["refund"] / s["cost"] * 100 if s["cost"] else 0
        print(f"  {BET_LABELS[bt]:6s}: {s['hit']}/{s['n']} 的中, "
              f"投資 {fmt_yen(s['cost'])}, 払戻 {fmt_yen(s['refund'])}, "
              f"収支 {fmt_yen(s['refund'] - s['cost'])} ({roi:.1f}%)")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
