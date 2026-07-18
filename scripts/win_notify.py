"""当選通知 (2026-07-18 導入)。

背景: これまでシステムが送るのは「投票完了」通知だけで、**当たったことを知らせる
通知が無かった**。三連系一本化で的中率が 18% に落ちてから当たりの実感が消え、
運用の楽しさが失われていた (ユーザー要望)。

購入履歴取得 (AutoraceFetchOrderHistory, 毎日 02:30) の直後に走らせ、対象日の
的中を集計して「🎯 当たりました」メールを送る。的中ゼロの日は送らない
(ハズレ通知はノイズなので出さない = 当たった時だけ嬉しい設計)。

使い方:
  python scripts/win_notify.py                 # 前日分 (02:30 実行を想定)
  python scripts/win_notify.py --date 2026-07-18
  python scripts/win_notify.py --date 2026-07-18 --dry-run   # 送信せず内容表示
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DETAIL_CSV = ROOT / "data" / "bet_history_detail.csv"
VENUE_JP = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
# 券種ごとの演出 (当たりの"重み"で絵文字を変える)
BET_EMOJI = {"複勝": "🎯", "三連複": "🎊", "三連単": "🏆"}


def load_hits(target: dt.date) -> pd.DataFrame:
    """対象日の的中行を返す (払戻 > 0)。"""
    if not (DETAIL_CSV.exists() and DETAIL_CSV.stat().st_size > 0):
        return pd.DataFrame()
    df = pd.read_csv(DETAIL_CSV)
    if "date" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("hit_amount", "henkan_amount", "tokubarai_amount", "vote_amount"):
        if c not in df.columns:
            df[c] = 0
    df["refund"] = (df["hit_amount"].fillna(0) + df["henkan_amount"].fillna(0)
                    + df["tokubarai_amount"].fillna(0))
    day = df[df["date"].dt.date == target]
    return day[day["refund"] > 0].copy(), day


def build_message(target: dt.date, hits: pd.DataFrame,
                  day_all: pd.DataFrame) -> tuple[str, str]:
    """(subject, body) を組み立てる。"""
    total_ref = int(hits["refund"].sum())
    day_inv = int(day_all["vote_amount"].fillna(0).sum())
    day_ref = int(day_all["refund"].sum())
    day_profit = day_ref - day_inv
    n_hits = len(hits)
    biggest = int(hits["refund"].max())

    # 件名は「当たった感」を最優先 (最高払戻を出す)
    head = "🏆" if biggest >= 5000 else ("🎊" if biggest >= 1000 else "🎯")
    subject = (f"[autorace] {head} 的中 {n_hits}件 / 払戻 ¥{total_ref:,} "
               f"({target:%m-%d})")

    lines = [f"【{target:%Y-%m-%d} の的中】", ""]
    for _, r in hits.sort_values("refund", ascending=False).iterrows():
        label = str(r.get("bet_type_label", "?"))
        emo = BET_EMOJI.get(label, "🎯")
        venue = VENUE_JP.get(int(r.get("place_code", 0)), "?")
        deme = str(r.get("pack_deme", "?"))
        ref = int(r["refund"])
        vote = int(r.get("vote_amount", 0) or 0)
        mult = f" ({ref / vote:.1f}倍)" if vote else ""
        lines.append(f"  {emo} {venue} R{int(r.get('race_no', 0))} "
                     f"{label} {deme} → ¥{ref:,}{mult}")

    if biggest >= 5000:
        lines += ["", "💥 特大配当！今夜は祝勝会だ！"]
    elif biggest >= 1000:
        lines += ["", "✨ 会心の的中！"]

    lines += [
        "",
        "─" * 28,
        f"当日: 投資 ¥{day_inv:,} / 払戻 ¥{day_ref:,} / "
        f"収支 {'+' if day_profit >= 0 else ''}¥{day_profit:,}",
        f"的中 {n_hits} / {len(day_all)} 件",
    ]
    return subject, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (既定: 前日)")
    ap.add_argument("--dry-run", action="store_true", help="送信せず内容表示")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.date:
        target = dt.date.fromisoformat(args.date)
    else:
        target = dt.date.today() - dt.timedelta(days=1)

    hits, day_all = load_hits(target)
    if day_all.empty:
        print(f"[win_notify] {target}: 購入なし — 通知しない")
        return 0
    if hits.empty:
        print(f"[win_notify] {target}: 的中なし ({len(day_all)}件購入) — 通知しない")
        return 0

    subject, body = build_message(target, hits, day_all)
    if args.dry_run:
        print("--- DRY RUN ---")
        print(subject)
        print()
        print(body)
        return 0

    try:
        from gmail_notify import send_email
        send_email(subject=subject, body=body)
        print(f"[win_notify] 送信: {subject}")
    except Exception as e:
        print(f"[win_notify] 送信失敗: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
