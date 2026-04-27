"""meeting (開催) ごとに gradeCode を取得し data/race_meetings.csv に保存。

各 (place_code, race_date) について /race_info/Player API を呼ぶが、
レスポンスの periodStartDate / periodEndDate を見て同じ meeting に属する
他の race_date は API 呼び出しをスキップ(無駄なコールを避ける)。

実測ペース: 1 meeting ≒ 1 API call、5 場 × 5 年 ≒ 1,250 meetings × 0.5s = 約 10 分。

出力: data/race_meetings.csv
  columns: place_code, race_date, period_start_date, period_end_date,
           grade_code, grade_name, title

冪等: 既存ファイルがあれば再開せず差分のみ取得。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.client import AutoraceClient

DATA = ROOT / "data"
OUTPUT = DATA / "race_meetings.csv"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_race_dates() -> pd.DataFrame:
    """race_entries.csv から (place_code, race_date) のユニーク一覧を作る。"""
    df = pd.read_csv(DATA / "race_entries.csv", usecols=["race_date", "place_code"])
    pairs = df.drop_duplicates().reset_index(drop=True)
    pairs["race_date"] = pd.to_datetime(pairs["race_date"]).dt.strftime("%Y-%m-%d")
    pairs = pairs.sort_values(["place_code", "race_date"]).reset_index(drop=True)
    return pairs


def load_existing() -> dict[tuple[int, str], dict]:
    """既存 race_meetings.csv をロード(再開用)。"""
    if not OUTPUT.exists():
        return {}
    df = pd.read_csv(OUTPUT)
    out = {}
    for _, r in df.iterrows():
        out[(int(r["place_code"]), r["race_date"])] = r.to_dict()
    return out


def main() -> None:
    pairs = load_race_dates()
    existing = load_existing()
    logger.info("Total (place, date) pairs: %d, already cached: %d",
                len(pairs), len(existing))

    if not existing:
        # ヘッダ行を初期化
        OUTPUT.parent.mkdir(exist_ok=True)
        with open(OUTPUT, "w", encoding="utf-8") as f:
            f.write("place_code,race_date,period_start_date,period_end_date,grade_code,grade_name,title\n")

    client = AutoraceClient()
    n_api = 0
    n_skip = 0

    # meeting 期間内の other race_dates を覆うためのキャッシュ
    # (place_code, periodStart) → set of race_dates already covered
    meeting_seen: dict[tuple[int, str], dict] = {}

    for i, row in pairs.iterrows():
        place = int(row["place_code"])
        date = str(row["race_date"])
        key = (place, date)

        if key in existing:
            n_skip += 1
            continue

        # 同 meeting でカバー済みか?
        covered = False
        for (mp, _), meta in meeting_seen.items():
            if mp != place:
                continue
            ps, pe = meta["period_start_date"], meta["period_end_date"]
            if ps <= date <= pe:
                # 既知 meeting に該当
                rec = {
                    "place_code": place, "race_date": date,
                    "period_start_date": ps, "period_end_date": pe,
                    "grade_code": meta["grade_code"], "grade_name": meta["grade_name"],
                    "title": meta["title"],
                }
                _append_row(rec)
                existing[key] = rec
                covered = True
                break
        if covered:
            continue

        # API コール
        try:
            resp = client.get_players(place_code=place, race_date=date)
        except Exception as e:
            logger.error("API error for %s/%s: %s", place, date, e)
            continue

        if resp.get("result") != "Success" or not resp.get("body"):
            logger.warning("No body for %s/%s: %s", place, date, resp.get("errors"))
            # それでも空エントリで埋めて再呼び出しを避ける
            rec = {
                "place_code": place, "race_date": date,
                "period_start_date": "", "period_end_date": "",
                "grade_code": "", "grade_name": "", "title": "",
            }
            _append_row(rec)
            existing[key] = rec
            continue

        body = resp["body"][0] if isinstance(resp["body"], list) else resp["body"]
        ps = body.get("periodStartDate", "")
        pe = body.get("periodEndDate", "")
        gc = body.get("gradeCode", "")
        gn = body.get("gradeName", "")
        title = body.get("title", "")

        rec = {
            "place_code": place, "race_date": date,
            "period_start_date": ps, "period_end_date": pe,
            "grade_code": gc, "grade_name": gn, "title": title,
        }
        _append_row(rec)
        existing[key] = rec
        meeting_seen[(place, ps)] = rec
        n_api += 1

        if n_api % 50 == 0:
            logger.info("API calls: %d, cached skips: %d, processed: %d/%d",
                        n_api, n_skip, i + 1, len(pairs))

    logger.info("Done. API calls: %d, in-period skips: %d, prior-cache skips: %d",
                n_api, len(existing) - n_api - n_skip, n_skip)
    logger.info("Output: %s", OUTPUT)


def _append_row(rec: dict) -> None:
    line = ",".join(str(rec.get(c, "")).replace(",", " ") for c in [
        "place_code", "race_date", "period_start_date", "period_end_date",
        "grade_code", "grade_name", "title",
    ])
    with open(OUTPUT, "a", encoding="utf-8") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    main()
