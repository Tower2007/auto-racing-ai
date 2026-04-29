"""朝オッズ取得タイミング調査(時刻スイープテスト)

Task Scheduler から指定時刻に呼ばれて、特定場の Odds API を叩いて
「いつオッズが取れるか」をログに記録する。

使い方:
  python scripts/odds_availability_test.py <place_code> <race_date> <race_no1,race_no2,...>
  例: python scripts/odds_availability_test.py 5 2026-04-29 1,6,12

出力: data/odds_availability_log.csv に追記(冪等)
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.client import AutoraceClient

LOG_FILE = ROOT / "data" / "odds_availability_log.csv"
COLUMNS = [
    "timestamp", "place_code", "race_date", "race_no",
    "result", "has_odds", "tns_non_zero_cars",
    "sales_updateDate", "tns_salesCount", "fns_salesCount",
    "errors",
]


def ensure_header():
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 0:
        return
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(COLUMNS)


def check(client: AutoraceClient, place_code: int, race_date: str, race_no: int) -> dict:
    ts = datetime.now().isoformat(timespec="seconds")
    rec = {
        "timestamp": ts,
        "place_code": place_code,
        "race_date": race_date,
        "race_no": race_no,
        "result": "", "has_odds": False, "tns_non_zero_cars": 0,
        "sales_updateDate": "", "tns_salesCount": "", "fns_salesCount": "",
        "errors": "",
    }
    try:
        r = client.get_odds(place_code=place_code, race_date=race_date, race_no=race_no)
        rec["result"] = r.get("result", "")
        body = r.get("body", {})
        if isinstance(body, dict):
            tns = body.get("tnsOddsList", {}) or {}
            non_zero = sum(
                1 for v in tns.values()
                if isinstance(v, str) and v not in ("", "0.0", "0", None)
            )
            rec["tns_non_zero_cars"] = non_zero
            rec["has_odds"] = non_zero > 0
            sales = body.get("salesInfo", {}) or {}
            rec["sales_updateDate"] = sales.get("updateDate", "")
            tns_s = sales.get("tns") or {}
            fns_s = sales.get("fns") or {}
            rec["tns_salesCount"] = tns_s.get("salesCount", "") if isinstance(tns_s, dict) else ""
            rec["fns_salesCount"] = fns_s.get("salesCount", "") if isinstance(fns_s, dict) else ""
        else:
            rec["errors"] = "body is not dict"
        if r.get("errors"):
            rec["errors"] = str(r.get("errors"))[:200]
    except Exception as e:
        rec["errors"] = f"exception: {e!r}"[:200]
    return rec


def main():
    if len(sys.argv) < 4:
        print("Usage: odds_availability_test.py <place_code> <race_date> <race_no_csv>")
        print("Example: odds_availability_test.py 5 2026-04-29 1,6,12")
        sys.exit(1)

    place_code = int(sys.argv[1])
    race_date = sys.argv[2]
    race_nos = [int(x.strip()) for x in sys.argv[3].split(",") if x.strip()]

    ensure_header()
    client = AutoraceClient()
    rows = []
    for rn in race_nos:
        rec = check(client, place_code, race_date, rn)
        rows.append(rec)
        print(f"  pc={place_code} {race_date} R{rn}: result={rec['result']} "
              f"has_odds={rec['has_odds']} tns_non_zero={rec['tns_non_zero_cars']} "
              f"updateDate={rec['sales_updateDate']}")

    with open(LOG_FILE, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writerows(rows)
    print(f"Appended {len(rows)} rows to {LOG_FILE}")


if __name__ == "__main__":
    main()
