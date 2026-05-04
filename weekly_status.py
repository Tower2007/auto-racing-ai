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
import subprocess
import sys
from pathlib import Path

import pandas as pd

from gmail_notify import send_email

# 通知候補監査(オプショナル: data/daily_predict_picks.csv が無ければ skip)
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
try:
    from picks_audit import (
        load_picks as _audit_load_picks,
        attach_results as _audit_attach,
        filter_period as _audit_filter,
        render_text as _audit_render_text,
        render_html as _audit_render_html,
    )
    _AUDIT_AVAILABLE = True
except Exception:
    _AUDIT_AVAILABLE = False

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


def check_bet_history_health(days: int = 7) -> dict:
    """bet_history 死活監視(Codex R6 提案)。

    4 項目を返す:
      - last_date: bet_history.csv の最新 race_date
      - recent_r_count: 直近 N 日に bet_history へ記録された R 数
      - log_last_success: fetch_order_history.log の最終 END exit=0 時刻
      - missing_picks: 直近 N 日で daily_predict_picks にあるが
        bet_history に対応行が無い (date, place_code, race_no) の件数
        (= 推奨が出たのに購入記録が無いケース、取りこぼし監視)
    """
    out: dict = {
        "last_date": None,
        "recent_r_count": 0,
        "log_last_success": None,
        "missing_picks": 0,
        "missing_picks_details": [],
        "warnings": [],
    }

    bh_p = DATA / "bet_history.csv"
    if bh_p.exists() and bh_p.stat().st_size > 0:
        bh = pd.read_csv(bh_p)
        if not bh.empty and "date" in bh.columns:
            bh["date"] = pd.to_datetime(bh["date"], errors="coerce")
            bh = bh.dropna(subset=["date"])
            if not bh.empty:
                out["last_date"] = bh["date"].max().date().isoformat()
                cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days)
                recent = bh[bh["date"] >= cutoff]
                out["recent_r_count"] = int(len(recent))
    else:
        out["warnings"].append("bet_history.csv が無い")

    log_p = DATA / "fetch_order_history.log"
    if log_p.exists():
        try:
            with open(log_p, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-200:]
            for line in reversed(tail):
                m = re.search(r"=== (\S+) END exit=0 ===", line)
                if m:
                    out["log_last_success"] = m.group(1)
                    break
        except Exception as e:
            out["warnings"].append(f"fetch_order_history.log 読込失敗: {e}")
    else:
        out["warnings"].append("fetch_order_history.log が無い")

    # 推奨 vs 購入: picks にあって bet_history に無い件数
    picks_p = DATA / "daily_predict_picks.csv"
    if picks_p.exists() and picks_p.stat().st_size > 0 and bh_p.exists():
        try:
            picks = pd.read_csv(picks_p)
            picks["race_date"] = pd.to_datetime(picks["race_date"], errors="coerce")
            picks = picks.dropna(subset=["race_date"])
            cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days)
            picks = picks[picks["race_date"] >= cutoff]
            if not picks.empty:
                bh_local = pd.read_csv(bh_p)
                bh_local["date"] = pd.to_datetime(bh_local["date"], errors="coerce")
                bh_keys = set(
                    zip(
                        bh_local["date"].dt.date.astype(str),
                        bh_local["place_code"].astype(int),
                        bh_local["race_no"].astype(int),
                    )
                )
                pick_keys = picks[["race_date", "place_code", "race_no"]].drop_duplicates()
                # has_result = race_results にデータあり(= 当日が過ぎている)
                res_p = DATA / "race_results.csv"
                if res_p.exists():
                    res = pd.read_csv(res_p, usecols=["race_date", "place_code"]).drop_duplicates()
                    res["race_date"] = pd.to_datetime(res["race_date"])
                    res_set = set(zip(res["race_date"].dt.date, res["place_code"].astype(int)))
                else:
                    res_set = set()
                missing = []
                for _, row in pick_keys.iterrows():
                    d = row["race_date"].date()
                    pc = int(row["place_code"])
                    rn = int(row["race_no"])
                    # 結果未確定の R は判定保留
                    if (d, pc) not in res_set:
                        continue
                    if (d.isoformat(), pc, rn) not in bh_keys:
                        missing.append((d.isoformat(), pc, rn))
                out["missing_picks"] = len(missing)
                out["missing_picks_details"] = missing[:10]
        except Exception as e:
            out["warnings"].append(f"missing picks 計算失敗: {e}")

    return out


def check_schtasks_health() -> dict:
    """schtasks LastRunResult 監視(Codex R6 提案)。

    Autorace* タスクの最終実行結果を取得し、0 以外を異常として返す。
    Windows 環境のみ動作、それ以外では空 dict を返す。
    """
    out: dict = {"tasks": [], "ng_count": 0, "warnings": []}
    if sys.platform != "win32":
        return out
    try:
        # /v 詳細表示 + /fo CSV で機械可読化
        proc = subprocess.run(
            ["schtasks", "/query", "/fo", "CSV", "/v"],
            capture_output=True, text=True, timeout=30,
            encoding="cp932", errors="replace",
        )
        if proc.returncode != 0:
            out["warnings"].append(f"schtasks 失敗 rc={proc.returncode}")
            return out
        from io import StringIO
        df = pd.read_csv(StringIO(proc.stdout), low_memory=False)
        # 列名は LANG により変わるので厳密一致せず substring で探す
        name_col = next((c for c in df.columns if "TaskName" in c or "タスク名" in c), None)
        result_col = next((c for c in df.columns if "Last Result" in c or "前回の結果" in c), None)
        last_run_col = next((c for c in df.columns if "Last Run" in c or "前回の実行" in c), None)
        if not name_col or not result_col:
            out["warnings"].append("schtasks 列名解析失敗")
            return out
        df = df[df[name_col].astype(str).str.contains("Autorace", case=False, na=False)]
        # Dyn_* one-shot は数が多く、定常監視は親タスクのみ対象にする
        keep_prefixes = (
            "AutoraceDailyIngest",
            "AutoraceDynamicScheduler",
            "AutoraceWeeklyRetrain",
            "AutoraceWeeklyStatus",
            "AutoraceDailyOrderHistory",
            "AutoraceMorningPredict",
            "AutoraceNoonPredict",
            "AutoraceEveningPredict",
        )
        for _, row in df.iterrows():
            raw_name = str(row[name_col]).strip().lstrip("\\")
            if not raw_name.startswith(keep_prefixes):
                continue
            try:
                rc = int(row[result_col])
            except (ValueError, TypeError):
                rc = -1
            last_run = str(row[last_run_col]) if last_run_col else ""
            entry = {"name": raw_name, "result": rc, "last_run": last_run}
            out["tasks"].append(entry)
            if rc != 0 and rc != 267011:  # 267011 = まだ実行されていない (allowed)
                out["ng_count"] += 1
    except FileNotFoundError:
        out["warnings"].append("schtasks 未検出")
    except Exception as e:
        out["warnings"].append(f"schtasks 例外: {e}")
    return out


def render_health_text(bh: dict, st: dict) -> str:
    lines = []
    lines.append("【🩺 死活監視】")
    if bh.get("last_date"):
        lines.append(f"  bet_history 最終日: {bh['last_date']}  / 直近 7 日 R 数: {bh['recent_r_count']}")
    else:
        lines.append("  bet_history: データなし")
    lines.append(f"  fetch_order_history 最終成功: {bh.get('log_last_success') or '不明'}")
    miss = bh.get("missing_picks", 0)
    if miss > 0:
        lines.append(f"  ⚠️ 推奨済 / 購入記録なし: {miss} R(取りこぼし or 手動スキップ)")
        for d, pc, rn in bh.get("missing_picks_details", []):
            lines.append(f"     - {d} 場{pc} R{rn}")
    else:
        lines.append("  ✅ 推奨済 → 全件 bet_history と整合")
    for w in bh.get("warnings", []):
        lines.append(f"  ⚠️ {w}")

    if st.get("tasks"):
        lines.append("")
        lines.append(f"  schtasks Autorace* {len(st['tasks'])} 件 / NG {st['ng_count']} 件")
        for t in st["tasks"]:
            mark = "✅" if t["result"] == 0 else ("⏳" if t["result"] == 267011 else "🔴")
            lines.append(f"    {mark} {t['name']:34s} rc={t['result']:>6} last_run={t['last_run']}")
    for w in st.get("warnings", []):
        lines.append(f"  ⚠️ schtasks: {w}")
    return "\n".join(lines)


def render_health_html(bh: dict, st: dict) -> str:
    BORDER = '"border-collapse:collapse; border-color:#bbb; font-family:Arial,sans-serif; font-size:13px;"'
    TH = '"background:#e8e8e8; padding:6px 10px; border:1px solid #bbb; text-align:center;"'
    TD = '"padding:6px 10px; border:1px solid #ddd; text-align:left;"'
    TD_R = '"padding:6px 10px; border:1px solid #ddd; text-align:right;"'

    miss = bh.get("missing_picks", 0)
    health_color = "#2e7d32" if (miss == 0 and st.get("ng_count", 0) == 0) else "#c62828"
    parts = [f'<h3 style="color:{health_color}; margin:18px 0 8px 0;">🩺 死活監視</h3>']
    parts.append(f'<table border="1" cellpadding="6" cellspacing="0" style={BORDER}>')
    parts.append(f'<tr><th style={TH}>項目</th><th style={TH}>値</th></tr>')
    parts.append(
        f'<tr><td style={TD}>bet_history 最終日</td>'
        f'<td style={TD_R}>{bh.get("last_date") or "(なし)"}</td></tr>'
    )
    parts.append(
        f'<tr style="background:#fafafa;"><td style={TD}>直近 7 日 R 数</td>'
        f'<td style={TD_R}>{bh.get("recent_r_count", 0)}</td></tr>'
    )
    parts.append(
        f'<tr><td style={TD}>fetch_order_history 最終成功</td>'
        f'<td style={TD_R}>{bh.get("log_last_success") or "不明"}</td></tr>'
    )
    miss_color = "#c62828" if miss > 0 else "#2e7d32"
    parts.append(
        f'<tr style="background:#fafafa;"><td style={TD}>推奨済 / 購入記録なし</td>'
        f'<td style={TD_R}><b style="color:{miss_color}">{miss} R</b></td></tr>'
    )
    parts.append("</table>")
    if bh.get("missing_picks_details"):
        parts.append('<ul style="margin:6px 0 0 18px; color:#c62828; font-size:12px;">')
        for d, pc, rn in bh["missing_picks_details"]:
            parts.append(f"<li>{d} 場{pc} R{rn}</li>")
        parts.append("</ul>")

    if st.get("tasks"):
        parts.append('<h4 style="margin:14px 0 6px 0;">schtasks 状態</h4>')
        parts.append(f'<table border="1" cellpadding="6" cellspacing="0" style={BORDER}>')
        parts.append(
            f'<tr><th style={TH}>task</th><th style={TH}>rc</th>'
            f'<th style={TH}>last_run</th></tr>'
        )
        for i, t in enumerate(st["tasks"]):
            alt = ' style="background:#fafafa;"' if i % 2 == 1 else ""
            rc = t["result"]
            mark = "✅" if rc == 0 else ("⏳" if rc == 267011 else "🔴")
            color = "#2e7d32" if rc == 0 else ("#888" if rc == 267011 else "#c62828")
            parts.append(
                f"<tr{alt}>"
                f'<td style={TD}>{mark} {t["name"]}</td>'
                f'<td style={TD_R}><span style="color:{color}">{rc}</span></td>'
                f'<td style={TD_R}>{t["last_run"]}</td>'
                f"</tr>"
            )
        parts.append("</table>")
    for w in (bh.get("warnings", []) + ["schtasks: " + w for w in st.get("warnings", [])]):
        parts.append(f'<p style="color:#e65100; margin:4px 0; font-size:12px;">⚠️ {w}</p>')
    return "\n".join(parts)


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

    # 死活監視(Codex R6 提案: bet_history + schtasks)
    try:
        bh_health = check_bet_history_health(args.days)
        st_health = check_schtasks_health()
        text += "\n\n" + render_health_text(bh_health, st_health)
        health_html = render_health_html(bh_health, st_health)
        html = html.replace(
            '<hr style="border:none;',
            health_html + '\n<hr style="border:none;',
            1,
        )
    except Exception as e:
        text += f"\n\n(死活監視スキップ: {e})"

    # 全期間累積成績(daily_predict と同じソース・フォーマット)
    try:
        from daily_predict import (
            cumulative_performance,
            render_cumulative_text,
            render_cumulative_html,
        )
        perf = cumulative_performance()
        if perf and perf["n_total"] > 0:
            text += "\n\n" + render_cumulative_text(perf)
            cum_html = render_cumulative_html(perf)
            html = html.replace(
                '<hr style="border:none;',
                cum_html + '\n<hr style="border:none;',
                1,
            )
    except Exception as e:
        text += f"\n\n(累積成績スキップ: {e})"

    # 通知候補監査(直近 args.days 日 詳細)
    if _AUDIT_AVAILABLE:
        try:
            picks = _audit_load_picks()
            audit = _audit_attach(picks)
            audit = _audit_filter(audit, args.days)
            text += "\n\n" + _audit_render_text(audit, args.days)
            # HTML: footer hr の前に挿入
            audit_html = _audit_render_html(audit, args.days)
            html = html.replace(
                '<hr style="border:none;',
                audit_html + '\n<hr style="border:none;',
                1,
            )
        except Exception as e:
            text += f"\n\n(picks 監査スキップ: {e})"

    # 推奨 vs 購入 整合監査(Codex R6 提案・優先 2)
    try:
        from reconcile_recommendations_vs_bets import (
            build_summary as _rec_build,
            render_text as _rec_text,
            render_html as _rec_html,
        )
        rec = _rec_build(args.days)
        text += "\n\n" + _rec_text(rec)
        rec_html = _rec_html(rec)
        html = html.replace(
            '<hr style="border:none;',
            rec_html + '\n<hr style="border:none;',
            1,
        )
    except Exception as e:
        text += f"\n\n(推奨 vs 購入 監査スキップ: {e})"

    # 実購入損益サマリ (bet_history.csv ベース)
    try:
        from bet_history_summary import build_summary as _bh_build
        bh = _bh_build(args.days)
        if bh is not None:
            bh_text, bh_html = bh
            text += "\n\n" + bh_text
            html = html.replace(
                '<hr style="border:none;',
                bh_html + '\n<hr style="border:none;',
                1,
            )
    except Exception as e:
        text += f"\n\n(実購入損益スキップ: {e})"

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
