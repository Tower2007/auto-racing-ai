"""
週次ステータスレポート
-----------------------------------------------------------------------
直近 1 週間のデータ収集状況を Gmail で送信。月曜朝などに定期実行。

送信内容:
  - 直近 7 日の収集ステータス(日別、場別、レース数)
  - データ全体サマリー(行数・期間・容量)
  - 最新の収集日
  - daily_ingest.log の末尾エラー抜粋

使い方:
  python weekly_status.py                # 直近 7 日
  python weekly_status.py --days 14
  python weekly_status.py --no-email     # コンソール表示のみ(テスト)
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path

import pandas as pd

from gmail_notify import send_email

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOG_FILE = DATA / "daily_ingest.log"

VENUE_NAMES = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}

CSV_FILES = [
    "race_entries.csv",
    "race_stats.csv",
    "race_results.csv",
    "race_laps.csv",
    "odds_summary.csv",
    "payouts.csv",
]


def get_db_summary() -> dict:
    """各 CSV の行数と期間を取得"""
    counts = {}
    for f in CSV_FILES:
        p = DATA / f
        if not p.exists():
            counts[f] = 0
            continue
        # 行数: 速い方法
        with open(p, "rb") as fp:
            counts[f] = sum(1 for _ in fp) - 1  # ヘッダを除く

    # サイズ
    total_size = sum((DATA / f).stat().st_size for f in CSV_FILES if (DATA / f).exists())

    # 期間 (race_entries から)
    p_entries = DATA / "race_entries.csv"
    if p_entries.exists():
        df = pd.read_csv(p_entries, usecols=["race_date"])
        df["race_date"] = pd.to_datetime(df["race_date"])
        oldest = df["race_date"].min().date().isoformat()
        latest = df["race_date"].max().date().isoformat()
    else:
        oldest = latest = None

    return {
        "counts": counts,
        "total_size_mb": total_size / 1024 / 1024,
        "oldest_date": oldest,
        "latest_date": latest,
    }


def get_recent_days_status(days: int) -> list[dict]:
    """直近 N 日の場別レース数。"""
    p = DATA / "race_entries.csv"
    if not p.exists():
        return []
    df = pd.read_csv(p, usecols=["race_date", "place_code", "race_no"])
    df["race_date"] = pd.to_datetime(df["race_date"]).dt.date.astype(str)

    today = dt.date.today()
    result = []
    for i in range(1, days + 1):
        d = (today - dt.timedelta(days=i)).isoformat()
        sub = df[df["race_date"] == d]
        per_venue = {}
        for pc, name in VENUE_NAMES.items():
            n_races = sub[sub["place_code"] == pc].drop_duplicates(["race_no"]).shape[0]
            per_venue[name] = n_races
        total_races = sum(per_venue.values())
        result.append({
            "date": d,
            "race_count": total_races,
            "per_venue": per_venue,
            "status": "OK" if total_races > 0 else "NO DATA",
        })
    result.reverse()  # 古い順に
    return result


def get_log_tail(n: int = 500) -> list[str]:
    if not LOG_FILE.exists():
        return ["(ログファイルなし)"]
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return [l.rstrip() for l in lines[-n:]]


def extract_errors(log_lines: list[str]) -> list[str]:
    """ERROR / fail=N(N>0) を含む行を抽出"""
    out = []
    for l in log_lines:
        if "ERROR" in l:
            out.append(l)
            continue
        m = re.search(r"error=(\d+)", l)
        if m and int(m.group(1)) > 0:
            out.append(l)
    return out


def render_text(summary: dict, days: list[dict], errors: list[str]) -> str:
    lines = []
    today = dt.date.today().isoformat()
    lines.append(f"📊 auto-racing-ai 週次ステータス ({today})")
    lines.append("=" * 60)
    lines.append("")
    lines.append("【データサマリー】")
    for f, c in summary["counts"].items():
        lines.append(f"  {f:22s} {c:>10,} 行")
    lines.append(f"  合計サイズ: {summary['total_size_mb']:.1f} MB")
    lines.append(f"  期間: {summary['oldest_date']} 〜 {summary['latest_date']}")
    lines.append("")

    ok_count = sum(1 for d in days if d["status"] == "OK")
    fail_count = len(days) - ok_count
    lines.append(f"【直近{len(days)}日の収集状況】 OK={ok_count} / NO_DATA={fail_count}")
    header = f"  {'date':12s}" + "".join(f" {n:>4s}" for n in VENUE_NAMES.values()) + " 計"
    lines.append(header)
    for d in days:
        v_str = "".join(f" {d['per_venue'][n]:>4d}" for n in VENUE_NAMES.values())
        flag = "✅" if d["status"] == "OK" else "—"
        lines.append(f"  {d['date']} {v_str} {d['race_count']:>3d} {flag}")
    lines.append("")

    if errors:
        lines.append(f"【⚠️ daily_ingest エラー(直近 {len(errors)} 件)】")
        for e in errors[-10:]:
            lines.append(f"  {e}")
    else:
        lines.append("【エラー】 なし ✅")
    lines.append("")
    lines.append("-- auto-racing-ai daily ingest watchdog --")
    return "\n".join(lines)


def render_html(summary: dict, days: list[dict], errors: list[str]) -> str:
    """Email クライアント(Gmail 等)で剥がされない inline-style 版。"""
    today = dt.date.today().isoformat()
    ok_count = sum(1 for d in days if d["status"] == "OK")
    fail_count = len(days) - ok_count
    overall_ok = (fail_count <= 1 and not errors)
    status_badge = ("🟢 正常" if overall_ok else "🟡 要確認" if fail_count <= 3 else "🔴 異常")

    # 共通スタイル(全部インライン)
    TBL = ('border="1" cellpadding="6" cellspacing="0" '
           'style="border-collapse:collapse; border-color:#bbb; '
           'font-family:Arial,sans-serif; font-size:13px;"')
    TH = ('style="background:#e8e8e8; text-align:left; padding:6px 10px; '
          'font-weight:bold; border:1px solid #bbb;"')
    TH_R = ('style="background:#e8e8e8; text-align:right; padding:6px 10px; '
            'font-weight:bold; border:1px solid #bbb;"')
    TD_L = 'style="text-align:left; padding:6px 10px; border:1px solid #ddd;"'
    TD_R = 'style="text-align:right; padding:6px 10px; border:1px solid #ddd;"'
    ROW_ALT = 'style="background:#fafafa;"'

    parts = []
    parts.append(
        '<div style="font-family:Arial,sans-serif; font-size:14px; color:#222; line-height:1.55; max-width:720px;">'
    )
    parts.append(
        f'<h2 style="color:#c62828; margin:0 0 12px 0; padding-bottom:6px; border-bottom:2px solid #c62828;">'
        f'📊 auto-racing-ai 週次ステータス &nbsp; <span style="color:#222; font-weight:normal;">{today}</span> '
        f'&nbsp; <span style="font-weight:normal;">{status_badge}</span></h2>'
    )

    # データサマリー
    parts.append('<h3 style="color:#444; margin:18px 0 8px 0;">データサマリー</h3>')
    parts.append(f'<table {TBL}>')
    parts.append(f'<tr><th {TH}>ファイル</th><th {TH_R}>行数</th></tr>')
    for i, (f, c) in enumerate(summary["counts"].items()):
        alt = ROW_ALT if i % 2 == 1 else ""
        parts.append(f'<tr {alt}><td {TD_L}>{f}</td><td {TD_R}>{c:,}</td></tr>')
    parts.append(
        f'<tr {ROW_ALT}><td {TD_L}><b>合計サイズ</b></td>'
        f'<td {TD_R}><b>{summary["total_size_mb"]:.1f} MB</b></td></tr>'
    )
    parts.append(
        f'<tr><td {TD_L}><b>期間</b></td>'
        f'<td {TD_R}><b>{summary["oldest_date"]} 〜 {summary["latest_date"]}</b></td></tr>'
    )
    parts.append('</table>')

    # 直近 N 日 場別マトリクス
    parts.append(
        f'<h3 style="color:#444; margin:18px 0 8px 0;">'
        f'直近{len(days)}日の収集状況 '
        f'<span style="font-weight:normal; color:#666; font-size:13px;">'
        f'(OK {ok_count} / NO_DATA {fail_count})</span></h3>'
    )
    parts.append(f'<table {TBL}>')
    venue_th = "".join(f'<th {TH_R}>{n}</th>' for n in VENUE_NAMES.values())
    parts.append(f'<tr><th {TH}>日付</th>{venue_th}<th {TH_R}>合計</th><th {TH}></th></tr>')
    for i, d in enumerate(days):
        alt = ROW_ALT if i % 2 == 1 else ""
        venue_cells = "".join(
            f'<td {TD_R}>{d["per_venue"][n]}</td>' if d["per_venue"][n] > 0
            else f'<td {TD_R} style="text-align:right; padding:6px 10px; border:1px solid #ddd; color:#bbb;">—</td>'
            for n in VENUE_NAMES.values()
        )
        if d["status"] == "OK":
            total_cell = f'<td {TD_R} style="text-align:right; padding:6px 10px; border:1px solid #ddd; color:#2e7d32; font-weight:bold;">{d["race_count"]}</td>'
            flag = '<td {} style="text-align:center; padding:6px 10px; border:1px solid #ddd;">✅</td>'.format(TD_L.replace('style="', 'style="text-align:center; '))
        else:
            total_cell = f'<td {TD_R} style="text-align:right; padding:6px 10px; border:1px solid #ddd; color:#999;">0</td>'
            flag = f'<td {TD_L} style="text-align:center; padding:6px 10px; border:1px solid #ddd; color:#999;">—</td>'
        parts.append(f'<tr {alt}><td {TD_L}>{d["date"]}</td>{venue_cells}{total_cell}{flag}</tr>')
    parts.append('</table>')

    # エラー
    if errors:
        parts.append(f'<h3 style="color:#e65100; margin:18px 0 8px 0;">⚠️ daily_ingest エラー(直近 {min(len(errors), 10)} 件)</h3>')
        parts.append(
            '<pre style="background:#fff3e0; padding:10px 12px; border-left:4px solid #ff9800; '
            'font-family:Consolas,monospace; font-size:12px; white-space:pre-wrap; overflow-x:auto;">'
        )
        for e in errors[-10:]:
            parts.append(_html_escape(e))
        parts.append('</pre>')
    else:
        parts.append(
            '<h3 style="color:#2e7d32; margin:18px 0 8px 0;">✅ エラーなし</h3>'
            '<p style="margin:0 0 8px 0; color:#555;">直近のログにエラーは記録されていません。</p>'
        )

    parts.append(
        '<hr style="border:none; border-top:1px solid #ddd; margin:18px 0 8px 0;">'
        '<p style="color:#999; font-size:11px; margin:0;">auto-racing-ai daily ingest watchdog</p>'
    )
    parts.append('</div>')
    return "\n".join(parts)


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "<br>")


def main() -> None:
    # Windows console (cp932) で絵文字が落ちないようにする
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--no-email", action="store_true")
    args = p.parse_args()

    summary = get_db_summary()
    days = get_recent_days_status(args.days)
    log_lines = get_log_tail(500)
    errors = extract_errors(log_lines)

    text = render_text(summary, days, errors)
    html = render_html(summary, days, errors)

    print(text)

    if args.no_email:
        return

    today = dt.date.today().isoformat()
    ok_count = sum(1 for d in days if d["status"] == "OK")
    fail_count = len(days) - ok_count
    if errors:
        status = "🔴NG"
    elif fail_count >= 4:
        status = "🟡WARN"
    else:
        status = "🟢OK"
    subject = f"[autorace] 週次 {today} {status} (OK {ok_count}/{len(days)})"

    send_email(subject=subject, body=text, html=html)


if __name__ == "__main__":
    main()
