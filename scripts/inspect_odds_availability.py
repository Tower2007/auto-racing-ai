"""当日開催各場の race ごとの odds 公開状況を probe

各レースの odds API を叩き、tnsOddsList / fnsOddsList が dict (公開済み) か
list (未公開) かを確認、race_no × venue マトリクスで出力。

朝 8:00 / 昼 11:00 / 13:00 等で実行して、odds 公開タイミングの実態を測る用。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.client import AutoraceClient, VENUE_CODES  # noqa: E402


def probe_race(client: AutoraceClient, place_code: int, race_date: str, race_no: int) -> dict:
    """1 レース分の odds 状況を返す。"""
    try:
        r = client.get_odds(place_code, race_date, race_no)
        body = r.get("body", {})
        if isinstance(body, list):
            return {"status": "no-race"}
        tns = body.get("tnsOddsList")
        fns = body.get("fnsOddsList")
        return {
            "status": "ok",
            "tns_published": isinstance(tns, dict) and len(tns) > 0,
            "fns_published": isinstance(fns, dict) and len(fns) > 0,
            "n_cars": len(body.get("playerList", [])),
            "n_tns": len(tns) if isinstance(tns, dict) else 0,
            "n_fns": len(fns) if isinstance(fns, dict) else 0,
        }
    except Exception as e:
        return {"status": f"err: {e}"}


def main():
    client = AutoraceClient()
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%H:%M:%S")

    # Hold/Today で開催中の場を取得
    resp = client.get_today_hold()
    body = resp.get("body", {}) or {}
    today_list = body.get("today", []) if isinstance(body, dict) else []
    venues = [(int(h.get("placeCode")), h.get("liveStartTime"), h.get("liveEndTime"))
              for h in today_list if h.get("placeCode") is not None
              and str(h.get("cancelFlg")) != "1"]

    if not venues:
        print(f"[{now}] 当日開催なし")
        return

    print(f"=== odds 公開状況 ({today} {now} JST 時点) ===")
    print()

    for pc, st, et in venues:
        name = VENUE_CODES.get(pc, str(pc))
        print(f"--- {name} (pc={pc}, 発走 {st} 〜 {et}) ---")
        print(f"{'R':>3} {'cars':>5} {'tns':>6} {'fns':>6}  status")
        n_pub = 0
        for r in range(1, 13):
            res = probe_race(client, pc, today, r)
            if res["status"] == "no-race":
                print(f"{r:>3}  ----   ----   ----   no-race")
            elif res["status"].startswith("err"):
                print(f"{r:>3}  ----   ----   ----   {res['status']}")
            else:
                tns_mark = f"{res['n_tns']}" if res["tns_published"] else "✕"
                fns_mark = f"{res['n_fns']}" if res["fns_published"] else "✕"
                if res["fns_published"]:
                    n_pub += 1
                print(f"{r:>3}  {res['n_cars']:>4}   {tns_mark:>4}   {fns_mark:>4}   "
                      f"{'公開' if res['fns_published'] else '未公開'}")
        print(f"  → fns 公開済み: {n_pub} / 12 レース")
        print()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
