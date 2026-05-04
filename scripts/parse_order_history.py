"""vote.autorace.jp /mypage/order の HTML を CSV に変換。

リスト画面 (1 R = 1 行) を解析。各行から以下を抽出:
- date (YYYY-MM-DD)
- place_code (2..6)
- place_name (kawaguchi 等 ASCII)
- race_no (int)
- bet_amount (int 円)  # "Npt" 表記、1pt=1円
- refund_amount (int 円)
- profit (refund - bet)

使い方:
    python scripts/parse_order_history.py data/sample_order.html
        → 標準出力に CSV を出す
    python scripts/parse_order_history.py data/sample_order.html -o data/bet_history_2026-05-04.csv
        → CSV ファイル出力
    python scripts/parse_order_history.py data/orders/*.html -o data/bet_history.csv
        → 複数 HTML をまとめて 1 CSV に追記 (重複排除あり)
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

# vel_code → (place_code, ascii name)
VEL_CODE_MAP: dict[str, tuple[int, str]] = {
    "002": (2, "kawaguchi"),
    "003": (3, "isesaki"),
    "004": (4, "hamamatsu"),
    "005": (5, "iizuka"),
    "006": (6, "sanyou"),
}

# detail URL パターン (1 件分のアンカー)
# 例: /mypage/order/detail?open_day=2026-05-04&vel_code=002&race_num=7&from=...&to=...
RE_DETAIL_LINK = re.compile(
    r'href="[^"]*/mypage/order/detail\?'
    r'open_day=(\d{4}-\d{2}-\d{2})'
    r'&(?:amp;)?vel_code=(\d{3})'
    r'&(?:amp;)?race_num=(\d+)'
    r'[^"]*"'
    # 同じ <a> 内の本文に 購入/払戻 が出る
    r'.*?購入.*?<dd[^>]*>\s*([0-9,]+)\s*pt\s*</dd>'
    r'.*?払戻.*?<dd[^>]*>\s*([0-9,]+)\s*円\s*</dd>',
    re.DOTALL,
)


def parse_html(html: str) -> list[dict]:
    """HTML を解析して 1 R = 1 dict のリストを返す。"""
    rows: list[dict] = []
    for m in RE_DETAIL_LINK.finditer(html):
        date_str, vel_code, race_num, bet_str, refund_str = m.groups()
        if vel_code not in VEL_CODE_MAP:
            # 想定外場コード → スキップ (将来的に船橋等が再開した場合の安全策)
            continue
        place_code, place_name = VEL_CODE_MAP[vel_code]
        bet = int(bet_str.replace(",", ""))
        refund = int(refund_str.replace(",", ""))
        rows.append({
            "date": date_str,
            "place_code": place_code,
            "place_name": place_name,
            "race_no": int(race_num),
            "bet_amount": bet,
            "refund_amount": refund,
            "profit": refund - bet,
        })
    return rows


def merge_dedup(existing: list[dict], new: list[dict]) -> list[dict]:
    """date+place_code+race_no キーで重複排除 (新しい値で上書き)。"""
    key = lambda r: (r["date"], r["place_code"], r["race_no"])
    merged: dict = {key(r): r for r in existing}
    for r in new:
        merged[key(r)] = r
    return sorted(merged.values(), key=lambda r: (r["date"], r["place_code"], r["race_no"]))


def load_existing_csv(path: Path) -> list[dict]:
    """既存 CSV があれば読み込み (重複排除のため)。"""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        out = []
        for row in reader:
            out.append({
                "date": row["date"],
                "place_code": int(row["place_code"]),
                "place_name": row["place_name"],
                "race_no": int(row["race_no"]),
                "bet_amount": int(row["bet_amount"]),
                "refund_amount": int(row["refund_amount"]),
                "profit": int(row["profit"]),
            })
        return out


def write_csv(rows: list[dict], path: Path | None) -> None:
    """CSV 書き出し (path=None なら stdout)。"""
    fieldnames = [
        "date", "place_code", "place_name", "race_no",
        "bet_amount", "refund_amount", "profit",
    ]
    if path is None:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def summarize(rows: list[dict]) -> str:
    """日次サマリ文字列を返す (進捗確認用)。"""
    by_date: dict[str, dict] = {}
    for r in rows:
        d = by_date.setdefault(r["date"], {"bet": 0, "refund": 0, "n": 0})
        d["bet"] += r["bet_amount"]
        d["refund"] += r["refund_amount"]
        d["n"] += 1
    lines = ["", "[日次サマリ]"]
    lines.append(f"{'date':12s} {'n':>4s} {'bet':>10s} {'refund':>10s} {'profit':>10s} {'roi':>7s}")
    total_bet = total_refund = total_n = 0
    for d in sorted(by_date.keys()):
        s = by_date[d]
        roi = s["refund"] / s["bet"] * 100 if s["bet"] else 0
        lines.append(
            f"{d:12s} {s['n']:>4d} {s['bet']:>10,d} {s['refund']:>10,d} "
            f"{s['refund'] - s['bet']:>+10,d} {roi:>6.1f}%"
        )
        total_bet += s["bet"]
        total_refund += s["refund"]
        total_n += s["n"]
    if len(by_date) > 1:
        roi = total_refund / total_bet * 100 if total_bet else 0
        lines.append(
            f"{'TOTAL':12s} {total_n:>4d} {total_bet:>10,d} {total_refund:>10,d} "
            f"{total_refund - total_bet:>+10,d} {roi:>6.1f}%"
        )
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("html_files", nargs="+", help="HTML ファイル (複数指定可)")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="出力 CSV パス (省略時 stdout)。既存があれば差分マージ。")
    p.add_argument("--summary", action="store_true",
                   help="stderr に日次サマリを出す")
    args = p.parse_args()

    new_rows: list[dict] = []
    for path_str in args.html_files:
        path = Path(path_str)
        if not path.exists():
            print(f"warn: {path} not found, skip", file=sys.stderr)
            continue
        html = path.read_text(encoding="utf-8")
        rows = parse_html(html)
        print(f"{path}: {len(rows)} rows", file=sys.stderr)
        new_rows.extend(rows)

    existing = load_existing_csv(args.output) if args.output else []
    merged = merge_dedup(existing, new_rows)

    write_csv(merged, args.output)
    if args.output:
        print(f"wrote {args.output} ({len(merged)} rows total)", file=sys.stderr)
    if args.summary or args.output:
        print(summarize(merged), file=sys.stderr)


if __name__ == "__main__":
    main()
