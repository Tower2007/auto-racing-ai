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
    today = dt.date.today().isoformat()
    css = """
    <style>
      body { font-family: sans-serif; font-size: 14px; color: #222; line-height: 1.55; }
      h2 { color: #c62828; border-left: 4px solid #c62828; padding-left: 8px; }
      table { border-collapse: collapse; margin: 8px 0; }
      th, td { border: 1px solid #ccc; padding: 4px 10px; font-size: 13px; text-align: right; }
      th { background: #eee; }
      td.left { text-align: left; }
      .ok { color: #388e3c; font-weight: bold; }
      .ng { color: #888; }
      .summary-card { background: #f5f5f5; padding: 10px 14px; border-radius: 6px; margin: 8px 0; }
      .error-box { background: #fff3e0; padding: 8px 12px; border-left: 4px solid #ff9800; font-family: monospace; font-size: 12px; white-space: pre-wrap; }
    </style>
    """
    ok_count = sum(1 for d in days if d["status"] == "OK")
    fail_count = len(days) - ok_count
    overall_ok = (fail_count <= 1 and not errors)  # autorace は開催が無い日が頻繁
    status_badge = ("🟢 正常" if overall_ok else "🟡 要確認" if fail_count <= 3 else "🔴 異常")

    parts = [css]
    parts.append(f"<h1>📊 auto-racing-ai 週次ステータス {today} &nbsp; <span>{status_badge}</span></h1>")

    # データサマリー
    rows_summary = "".join(
        f"<tr><td class='left'>{f}</td><td>{c:,}</td></tr>"
        for f, c in summary["counts"].items()
    )
    parts.append(f"""
    <div class="summary-card">
      <h2 style="margin-top:0">データサマリー</h2>
      <table>
        <tr><th class='left'>ファイル</th><th>行数</th></tr>
        {rows_summary}
        <tr><td class='left'>合計サイズ</td><td>{summary['total_size_mb']:.1f} MB</td></tr>
        <tr><td class='left'>期間</td><td>{summary['oldest_date']} 〜 {summary['latest_date']}</td></tr>
      </table>
    </div>
    """)

    # 直近 N 日 場別
    parts.append(f"<h2>直近{len(days)}日の収集状況(OK {ok_count} / NO_DATA {fail_count})</h2>")
    venue_th = "".join(f"<th>{n}</th>" for n in VENUE_NAMES.values())
    rows_html = []
    for d in days:
        cls = "ok" if d["status"] == "OK" else "ng"
        per_venue = "".join(f"<td>{d['per_venue'][n] or '—'}</td>" for n in VENUE_NAMES.values())
        rows_html.append(
            f"<tr><td class='left'>{d['date']}</td>{per_venue}"
            f"<td class='{cls}'>{d['race_count']}</td></tr>"
        )
    parts.append(f"""
    <table>
      <tr><th class='left'>日付</th>{venue_th}<th>合計</th></tr>
      {''.join(rows_html)}
    </table>
    """)

    if errors:
        parts.append(f"<h2>⚠️ daily_ingest エラー(直近 {min(len(errors), 10)} 件)</h2>")
        parts.append("<div class='error-box'>")
        for e in errors[-10:]:
            parts.append(e + "<br>")
        parts.append("</div>")
    else:
        parts.append("<h2>✅ エラーなし</h2><p>直近のログにエラーは記録されていません。</p>")

    parts.append("<hr><p style='color:#999;font-size:11px'>auto-racing-ai daily ingest watchdog</p>")
    return "\n".join(parts)


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
