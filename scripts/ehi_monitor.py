"""Edge Health Index (EHI) Monitor

1番人気(複勝/単勝)のオーバーラウンド(overround_factor)を監視し、
市場の効率性(エッジの消失)をリアルタイムに検知する。

EHI = (1番人気の的中率) / (1番人気の implied 確率)
  - Healthy (🟢): < 0.80 (歴史的基準 0.76 付近)
  - Warning (🟡): 0.80 - 0.85
  - Danger  (🔴): >= 0.85 (撤退のサイン)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

RACE_KEY = ["race_date", "place_code", "race_no"]
CAR_KEY = RACE_KEY + ["car_no"]


def calculate_ehi(days: int = 7) -> dict:
    """直近 N 日間の 1 番人気オーバーラウンドを計算。"""
    odds_p = DATA / "odds_summary.csv"
    res_p = DATA / "race_results.csv"

    if not odds_p.exists() or not res_p.exists():
        return {"status": "UNKNOWN", "message": "CSV data missing", "ehi": None}

    try:
        # メモリ節約のため必要最小限の列のみ読み込み
        odds = pd.read_csv(odds_p, usecols=RACE_KEY + ["car_no", "win_odds"], low_memory=False)
        res = pd.read_csv(res_p, usecols=RACE_KEY + ["car_no", "order"], low_memory=False)

        # 直近 N 日にフィルタ
        cutoff = (pd.Timestamp.now() - pd.Timedelta(days=days)).date().isoformat()
        odds = odds[odds["race_date"] >= cutoff].copy()
        res = res[res["race_date"] >= cutoff].copy()

        if odds.empty or res.empty:
            return {"status": "NO_DATA", "message": f"No data in last {days} days", "ehi": None}

        # 1着フラグ
        res["target_win"] = (res["order"] == 1).astype(int)

        # 結合
        df = odds.merge(res, on=CAR_KEY, how="inner")
        if df.empty:
            return {"status": "NO_DATA", "message": "No matched races in last 7 days", "ehi": None}

        # 1番人気を特定 (win_odds 最小)
        df["win_rank"] = df.groupby(RACE_KEY)["win_odds"].rank(method="min")
        fav1 = df[df["win_rank"] == 1].copy()

        if fav1.empty:
            return {"status": "NO_DATA", "message": "No 1st favorites found", "ehi": None}

        # EHI 計算
        n_races = fav1.groupby(RACE_KEY).ngroups
        win_hit = fav1["target_win"].mean()
        # 1 / win_odds の平均 = 期待的中率 (implied prob)
        implied_prob = (1.0 / fav1["win_odds"]).mean()

        if implied_prob == 0:
            return {"status": "ERROR", "message": "Implied prob is zero", "ehi": None}

        ehi = win_hit / implied_prob

        # ステータス判定
        if ehi < 0.80:
            status = "HEALTHY"
            emoji = "🟢"
            color = "green"
        elif ehi < 0.85:
            status = "WARNING"
            emoji = "🟡"
            color = "orange"
        else:
            status = "DANGER"
            emoji = "🔴"
            color = "red"

        return {
            "status": status,
            "ehi": round(ehi, 3),
            "n_races": n_races,
            "win_hit": round(win_hit, 3),
            "implied_prob": round(implied_prob, 3),
            "emoji": emoji,
            "color": color,
            "days": days,
        }

    except Exception as e:
        return {"status": "ERROR", "message": str(e), "ehi": None}


if __name__ == "__main__":
    # Windows console 文字化け対策
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    res = calculate_ehi(7)
    if res["ehi"] is not None:
        print(f"Edge Health Index (7d): {res['ehi']} {res['emoji']} {res['status']}")
        print(f"  Races: {res['n_races']}, WinHit: {res['win_hit']}, Implied: {res['implied_prob']}")
    else:
        print(f"EHI Calculation Failed: {res.get('message', 'Unknown error')}")
