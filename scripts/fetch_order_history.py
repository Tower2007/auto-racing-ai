"""vote.autorace.jp の投票履歴を GraphQL 経由で取得して CSV に保存。

使い方:
    python scripts/fetch_order_history.py --from 2026-05-01 --to 2026-05-04
        # 期間内の R 単位サマリを data/bet_history.csv に追記マージ
    python scripts/fetch_order_history.py --from 2026-05-04 --to 2026-05-04 --detail
        # サマリ + 券種別詳細 (data/bet_history_detail.csv) も取得
    python scripts/fetch_order_history.py --since 7d
        # 直近 7 日 (= today-7 〜 today)

事前準備:
    .env.vote に VOTE_AUTORACE_COOKIE=... を設定。
    取得方法: vote.autorace.jp ログイン後、F12 → Network → api.autorace.jp/graphql
              → Request Headers → Cookie 値を全部コピー。
              cookie 期限切れ時はエラーで気付くので再ログインして更新。

API 仕様:
    POST https://api.autorace.jp/graphql
    Cookie 認証 (`_asc_session` 等)
    GraphQL queries:
      - myNanakakeOrderSummaries: R 単位の購入/払戻サマリ
      - myNanakakeOrders: R 内の券種別ベット詳細 (packs)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ENV_VOTE = ROOT / ".env.vote"
DEFAULT_SUMMARY_CSV = ROOT / "data" / "bet_history.csv"
DEFAULT_DETAIL_CSV = ROOT / "data" / "bet_history_detail.csv"

GRAPHQL_URL = "https://api.autorace.jp/graphql"

# vel_code (3桁文字列) → (place_code int, ASCII name)
VEL_CODE_MAP: dict[str, tuple[int, str]] = {
    "002": (2, "kawaguchi"),
    "003": (3, "isesaki"),
    "004": (4, "hamamatsu"),
    "005": (5, "iizuka"),
    "006": (6, "sanyou"),
}

# betType (GraphQL enum) → (日本語ラベル, 短縮コード)。
# 短縮コードは autorace.jp 公式 API (data/payouts.csv) と統一。
# 確認済: FUKUSHOU / SANRENTAN / SANRENFUKU
# 未確認 (推定): TANSHOU / NISHARENPUKU / NISHARENTAN / WIDE
BET_TYPE_MAP: dict[str, tuple[str, str]] = {
    "TANSHOU":      ("単勝",   "tns"),
    "FUKUSHOU":     ("複勝",   "fns"),
    "NISHARENPUKU": ("二車連", "rfw"),  # 推定 (autorace 公式: 二車連=rfw)
    "NISHARENTAN":  ("二車単", "rtw"),  # 推定 (autorace 公式: 二車単=rtw)
    "WIDE":         ("ワイド", "wid"),
    "SANRENFUKU":   ("三連複", "rf3"),
    "SANRENTAN":    ("三連単", "rt3"),
}

# GraphQL queries (HAR から抽出した本番クエリそのまま)
QUERY_SUMMARIES = """
query ($from: ISO8601Date, $to: ISO8601Date, $velCode: String, $raceNum: Int, $page: Int, $per: Int) {
  myNanakakeOrderSummaries(from: $from, to: $to, velCode: $velCode, raceNum: $raceNum, page: $page, per: $per) {
    totalCount
    orderSummaries {
      id openDay raceNum velName velCode
      spentCash spentPoints tokubaraiAmount henkanCash henkanPoints hitAmount
      __typename
    }
    __typename
  }
}
"""

QUERY_ORDERS = """
query ($openDay: ISO8601Date, $velCode: String, $raceNum: Int, $page: Int, $per: Int) {
  myNanakakeOrders(openDay: $openDay, velCode: $velCode, raceNum: $raceNum, page: $page, per: $per) {
    totalCount
    orders {
      id henkanCash henkanPoints hitAmount openDay orderMethod
      spentCash spentPoints tokubaraiAmount velName createdAt
      packs {
        packDeme betType hitAmount henkanAmount voteAmount packVotes tokubaraiAmount
        __typename
      }
      __typename
    }
    __typename
  }
}
"""


def load_cookie() -> str:
    """`.env.vote` から VOTE_AUTORACE_COOKIE 値を取得。"""
    if not ENV_VOTE.exists():
        sys.exit(f"error: {ENV_VOTE} が無い。README 参照して cookie 設定してください。")
    for line in ENV_VOTE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("VOTE_AUTORACE_COOKIE="):
            return line.split("=", 1)[1]
    sys.exit("error: VOTE_AUTORACE_COOKIE が .env.vote に無い")


def post_graphql(cookie: str, query: str, variables: dict) -> dict:
    """GraphQL POST。エラー時は例外。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, application/graphql-response+json",
        "Origin": "https://vote.autorace.jp",
        "Referer": "https://vote.autorace.jp/",
        "x-requested-with": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
        "Cookie": cookie,
    }
    payload = {"query": query, "variables": variables}
    resp = requests.post(GRAPHQL_URL, headers=headers, json=payload, timeout=30)
    if resp.status_code == 401 or resp.status_code == 403:
        sys.exit(
            f"error: {resp.status_code} 認証失敗。.env.vote の cookie が期限切れ。\n"
            "ブラウザで再ログインして cookie を更新してください。"
        )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]


def fetch_all_summaries(cookie: str, from_date: str, to_date: str,
                        per_page: int = 100) -> list[dict]:
    """期間内の全 R サマリをページング取得。"""
    page = 1
    out: list[dict] = []
    while True:
        data = post_graphql(cookie, QUERY_SUMMARIES, {
            "from": from_date, "to": to_date,
            "velCode": None, "raceNum": None,
            "page": page, "per": per_page,
        })
        result = data["myNanakakeOrderSummaries"]
        items = result["orderSummaries"]
        out.extend(items)
        total = result["totalCount"]
        print(f"  summaries page {page}: +{len(items)} (cumulative {len(out)}/{total})",
              file=sys.stderr)
        if len(out) >= total or not items:
            break
        page += 1
        time.sleep(0.3)  # 軽いペース制限
    return out


def fetch_orders(cookie: str, open_day: str, vel_code: str,
                 race_num: int, per_page: int = 50) -> list[dict]:
    """1 R 内の全オーダー取得 (券種別 packs 含む)。"""
    page = 1
    out: list[dict] = []
    while True:
        data = post_graphql(cookie, QUERY_ORDERS, {
            "openDay": open_day, "velCode": vel_code, "raceNum": race_num,
            "page": page, "per": per_page,
        })
        result = data["myNanakakeOrders"]
        items = result["orders"]
        out.extend(items)
        total = result["totalCount"]
        if len(out) >= total or not items:
            break
        page += 1
        time.sleep(0.3)
    return out


def summaries_to_rows(summaries: list[dict]) -> list[dict]:
    """API レスポンスを CSV 行 (parse_order_history.py 互換) に変換。"""
    rows: list[dict] = []
    for s in summaries:
        vel = s["velCode"]
        if vel not in VEL_CODE_MAP:
            print(f"warn: unknown vel_code={vel}, skip", file=sys.stderr)
            continue
        place_code, place_name = VEL_CODE_MAP[vel]
        bet = int(s.get("spentCash", 0)) + int(s.get("spentPoints", 0))
        # refund = 的中払戻 + 返金 (失格・取消) + 特払い
        refund = (
            int(s.get("hitAmount", 0))
            + int(s.get("henkanCash", 0))
            + int(s.get("henkanPoints", 0))
            + int(s.get("tokubaraiAmount", 0))
        )
        rows.append({
            "date": s["openDay"],
            "place_code": place_code,
            "place_name": place_name,
            "race_no": int(s["raceNum"]),
            "bet_amount": bet,
            "refund_amount": refund,
            "profit": refund - bet,
        })
    return rows


def orders_to_pack_rows(orders: list[dict], open_day: str,
                        vel_code: str, race_num: int) -> list[dict]:
    """orders → 券種別 (pack 単位) 行リスト。"""
    place_code, place_name = VEL_CODE_MAP.get(vel_code, (0, "unknown"))
    rows: list[dict] = []
    for o in orders:
        order_id = o["id"]
        created_at = o.get("createdAt", "")
        for p in o.get("packs", []):
            bt = p.get("betType", "")
            jp_label, short = BET_TYPE_MAP.get(bt, (bt, bt.lower()))
            rows.append({
                "date": open_day,
                "place_code": place_code,
                "place_name": place_name,
                "race_no": race_num,
                "order_id": order_id,
                "created_at": created_at,
                "bet_type_code": short,
                "bet_type_label": jp_label,
                "pack_deme": p.get("packDeme", ""),  # 買い目 (例: "5", "1-2-3")
                "pack_votes": int(p.get("packVotes", 0)),
                "vote_amount": int(p.get("voteAmount", 0)),
                "hit_amount": int(p.get("hitAmount", 0)),
                "henkan_amount": int(p.get("henkanAmount", 0)),
                "tokubarai_amount": int(p.get("tokubaraiAmount", 0)),
            })
    return rows


def merge_csv(path: Path, new_rows: list[dict], key_cols: list[str],
              fieldnames: list[str]) -> int:
    """既存 CSV と新規行を key_cols でマージ (重複は新規で上書き)。"""
    existing: dict = {}
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                key = tuple(row[c] for c in key_cols)
                existing[key] = row
    for r in new_rows:
        # int 値を str にして dict 形式統一
        norm = {k: (str(v) if not isinstance(v, str) else v) for k, v in r.items()}
        key = tuple(norm[c] for c in key_cols)
        existing[key] = norm
    rows = sorted(existing.values(),
                  key=lambda r: tuple(r.get(c, "") for c in key_cols))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def parse_since(s: str) -> int:
    """'7d' → 7 (days)"""
    if s.endswith("d"):
        return int(s[:-1])
    return int(s)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--from", dest="from_date", help="YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", help="YYYY-MM-DD")
    p.add_argument("--since", help="直近 N 日 (例: 7d)。--from/--to より優先")
    p.add_argument("--detail", action="store_true",
                   help="券種別 pack 詳細も取得 (各 R 1 リクエスト追加)")
    p.add_argument("--summary-csv", type=Path, default=DEFAULT_SUMMARY_CSV)
    p.add_argument("--detail-csv", type=Path, default=DEFAULT_DETAIL_CSV)
    args = p.parse_args()

    if args.since:
        days = parse_since(args.since)
        today = dt.date.today()
        from_date = (today - dt.timedelta(days=days)).isoformat()
        to_date = today.isoformat()
    else:
        if not args.from_date or not args.to_date:
            sys.exit("error: --from / --to or --since が必要")
        from_date = args.from_date
        to_date = args.to_date

    print(f"取得期間: {from_date} 〜 {to_date}", file=sys.stderr)
    cookie = load_cookie()

    print("[1] サマリ取得 ...", file=sys.stderr)
    summaries = fetch_all_summaries(cookie, from_date, to_date)
    print(f"  -> {len(summaries)} レース", file=sys.stderr)

    summary_rows = summaries_to_rows(summaries)
    n = merge_csv(
        args.summary_csv, summary_rows,
        key_cols=["date", "place_code", "race_no"],
        fieldnames=["date", "place_code", "place_name", "race_no",
                    "bet_amount", "refund_amount", "profit"],
    )
    print(f"  -> {args.summary_csv} ({n} 行)", file=sys.stderr)

    if args.detail:
        print("[2] 券種別詳細取得 ...", file=sys.stderr)
        all_packs: list[dict] = []
        for i, s in enumerate(summaries):
            orders = fetch_orders(cookie, s["openDay"], s["velCode"],
                                  int(s["raceNum"]))
            packs = orders_to_pack_rows(orders, s["openDay"], s["velCode"],
                                        int(s["raceNum"]))
            all_packs.extend(packs)
            print(f"  [{i+1}/{len(summaries)}] {s['openDay']} "
                  f"{VEL_CODE_MAP.get(s['velCode'], ('?', '?'))[1]} R{s['raceNum']}: "
                  f"{len(packs)} packs",
                  file=sys.stderr)
            time.sleep(0.3)
        n = merge_csv(
            args.detail_csv, all_packs,
            key_cols=["date", "place_code", "race_no", "order_id",
                      "bet_type_code", "pack_deme"],
            fieldnames=["date", "place_code", "place_name", "race_no",
                        "order_id", "created_at",
                        "bet_type_code", "bet_type_label",
                        "pack_deme", "pack_votes", "vote_amount",
                        "hit_amount", "henkan_amount", "tokubarai_amount"],
        )
        print(f"  -> {args.detail_csv} ({n} 行)", file=sys.stderr)

    # 期間内サマリを stderr に出す
    total_bet = sum(r["bet_amount"] for r in summary_rows)
    total_refund = sum(r["refund_amount"] for r in summary_rows)
    profit = total_refund - total_bet
    roi = total_refund / total_bet * 100 if total_bet else 0
    print(file=sys.stderr)
    print(f"[期間サマリ] {len(summary_rows)} R / "
          f"投資 ¥{total_bet:,} / 払戻 ¥{total_refund:,} / "
          f"損益 {profit:+,}円 / ROI {roi:.1f}%",
          file=sys.stderr)


if __name__ == "__main__":
    main()
