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
import json
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

try:
    from ehi_monitor import calculate_ehi
    _EHI_AVAILABLE = True
except Exception:
    _EHI_AVAILABLE = False

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


def _check_bh_csv_freshness(out: dict, bh_p: Path, today: pd.Timestamp, days: int) -> None:
    """bet_history.csv の存在・最終取得日を検査 (check_bet_history_health の下請け)。"""
    if not (bh_p.exists() and bh_p.stat().st_size > 0):
        out["alerts"].append(("WARN", "bet_history.csv が存在しない"))
    else:
        bh = pd.read_csv(bh_p)
        if not bh.empty and "date" in bh.columns:
            bh["date"] = pd.to_datetime(bh["date"], errors="coerce")
            bh = bh.dropna(subset=["date"])
            if not bh.empty:
                last = bh["date"].max()
                out["last_date"] = last.date().isoformat()
                cutoff = today - pd.Timedelta(days=days)
                out["recent_r_count"] = int(len(bh[bh["date"] >= cutoff]))
                # 最終取得日 > today-2 → WARN
                if last < today - pd.Timedelta(days=2):
                    out["alerts"].append((
                        "WARN", f"bet_history 最終日が古い ({out['last_date']}, today-2 超え)"
                    ))


def _check_fetch_order_log(out: dict) -> None:
    """fetch_order_history.log の最終成功・NG パターンを検査 (check_bet_history_health の下請け)。"""
    log_p = DATA / "fetch_order_history.log"
    if not log_p.exists():
        out["alerts"].append(("WARN", "fetch_order_history.log が存在しない"))
    else:
        try:
            with open(log_p, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-2000:]
            # 最終成功時刻
            for line in reversed(tail):
                m = re.search(r"=== (\S+) END exit=0 ===", line)
                if m:
                    out["log_last_success"] = m.group(1)
                    break
            if out["log_last_success"]:
                try:
                    last_ok = pd.to_datetime(out["log_last_success"])
                    if last_ok < pd.Timestamp.now() - pd.Timedelta(hours=48):
                        out["alerts"].append((
                            "WARN", f"fetch_order_history 最終成功 > 48h 前 ({out['log_last_success']})"
                        ))
                except Exception:
                    pass
            else:
                out["alerts"].append(("WARN", "fetch_order_history 成功記録が無い"))

            # R8: error 検出は「最後の START 以降」のブロック内のみ。
            # 過去の解決済みエラーで永遠に NG になる問題を回避。
            last_start_idx = None
            for i in range(len(tail) - 1, -1, -1):
                if re.search(r"=== \S+ START\b", tail[i]):
                    last_start_idx = i
                    break
            if last_start_idx is None:
                # START マーカが無い古い形式 → 直近 200 行を fallback
                last_block = "".join(tail[-200:])
            else:
                last_block = "".join(tail[last_start_idx:])

            ng_patterns = [
                (r"\bauth(?:entication)?\s*fail", "認証失敗"),
                (r"\b401\b", "401 Unauthorized"),
                (r"\b403\b", "403 Forbidden"),
                (r"cookie.*(expir|invalid|missing|失効|期限切れ)", "cookie 失効"),
                (r"login.*(fail|require|必要)", "ログイン要求"),
                (r"^Traceback", "Python traceback"),
                (r"\bERROR\b", "ERROR ログ"),
            ]
            seen = set()
            for pat, label in ng_patterns:
                if re.search(pat, last_block, re.IGNORECASE | re.MULTILINE) and label not in seen:
                    seen.add(label)
                    out["alerts"].append((
                        "NG", f"fetch_order_history.log (最後の START 以降): {label} を検出"
                    ))
        except Exception as e:
            out["alerts"].append(("WARN", f"fetch_order_history.log 読込失敗: {e}"))


def _check_missing_picks(out: dict, bh_p: Path, today: pd.Timestamp, days: int) -> None:
    """推奨 (picks) にあって bet_history に無い R を集計 (check_bet_history_health の下請け)。"""
    # 推奨 vs 購入: picks にあって bet_history に無い件数(R7: NG にせず INFO)
    picks_p = DATA / "daily_predict_picks.csv"
    if picks_p.exists() and picks_p.stat().st_size > 0 and bh_p.exists():
        try:
            picks = pd.read_csv(picks_p)
            picks["race_date"] = pd.to_datetime(picks["race_date"], errors="coerce")
            picks = picks.dropna(subset=["race_date"])
            cutoff = today - pd.Timedelta(days=days)
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
                res_p = DATA / "race_results.csv"
                if res_p.exists():
                    res = pd.read_csv(res_p, usecols=["race_date", "place_code"]).drop_duplicates()
                    res["race_date"] = pd.to_datetime(res["race_date"])
                    res_set = set(zip(res["race_date"].dt.date, res["place_code"].astype(int)))
                else:
                    res_set = set()
                missing = []
                # 日別の集計: その日に推奨が居て bet が 1 件でもあれば「購入あり」、
                # 推奨が居て bet が 0 件なら「全 R 購入なし」と扱う(R8: 連続日数判定用)
                bh_dates = set(bh_local["date"].dt.date.astype(str)) if not bh_local.empty else set()
                picks_per_day: dict[str, int] = {}
                bought_per_day: dict[str, int] = {}
                for _, row in pick_keys.iterrows():
                    d = row["race_date"].date()
                    pc = int(row["place_code"])
                    rn = int(row["race_no"])
                    if (d, pc) not in res_set:
                        continue
                    d_str = d.isoformat()
                    picks_per_day[d_str] = picks_per_day.get(d_str, 0) + 1
                    if (d_str, pc, rn) in bh_keys:
                        bought_per_day[d_str] = bought_per_day.get(d_str, 0) + 1
                    else:
                        missing.append((d_str, pc, rn))
                out["missing_picks"] = len(missing)
                out["missing_picks_details"] = missing[:10]

                # R8: 「推奨はあったが bet が 0」の日が連続している数を数える
                # 直近の推奨があった日(picks_per_day のキー)を新しい順に並べ、
                # bought_per_day == 0 の連続を数える
                sorted_days = sorted(picks_per_day.keys(), reverse=True)
                no_bet_streak = 0
                for d_str in sorted_days:
                    if bought_per_day.get(d_str, 0) == 0:
                        no_bet_streak += 1
                    else:
                        break
                out["no_bet_streak_days"] = no_bet_streak

                if missing:
                    if no_bet_streak >= 3:
                        out["alerts"].append((
                            "WARN",
                            f"推奨あり / 購入 0 が {no_bet_streak} 日連続 "
                            f"(計 {len(missing)} R)— 運用継続意思を確認"
                        ))
                    else:
                        out["alerts"].append((
                            "INFO",
                            f"推奨済 / 購入記録なし {len(missing)} R "
                            f"(連続 {no_bet_streak} 日、ユーザー不在 or 手動 skip の可能性)"
                        ))
        except Exception as e:
            out["alerts"].append(("WARN", f"missing picks 計算失敗: {e}"))


def check_bet_history_health(days: int = 7) -> dict:
    """bet_history 死活監視(Codex R6 + R7 提案)。

    R7 で WARN/NG 分類を追加:
      - bet_history.csv 不在            → WARN
      - 最終取得日 > today-2            → WARN
      - 推奨あり/購入 0                 → INFO(NG ではない、ユーザー不在の可能性)
      - log 最終成功 > 48h              → WARN
      - log に error / 認証失敗 / cookie → NG
    """
    out: dict = {
        "last_date": None,
        "recent_r_count": 0,
        "log_last_success": None,
        "missing_picks": 0,
        "missing_picks_details": [],
        "alerts": [],   # [(level, msg)] level ∈ {INFO, WARN, NG}
    }
    today = pd.Timestamp.now().normalize()
    bh_p = DATA / "bet_history.csv"

    _check_bh_csv_freshness(out, bh_p, today, days)
    _check_fetch_order_log(out)
    _check_missing_picks(out, bh_p, today, days)

    # 全体ステータス決定: NG > WARN > OK
    levels = [a[0] for a in out["alerts"]]
    if "NG" in levels:
        out["status"] = "NG"
    elif "WARN" in levels:
        out["status"] = "WARN"
    else:
        out["status"] = "OK"
    return out


# ─── 三連系 (rt3+rf3) 機械的停止基準 (docs/ev_strategy_findings.md 2026-05-31) ──

RT3_STOP_FLAG = DATA / "rt3_stop.flag"   # 存在すると daily_predict/auto_buy が三連系購入を停止
RT3_BET_TYPES = ("rt3", "rf3")
RT3_PLACES = (4, 6)                      # 浜松・山陽
RT3_EVAL_MIN_N = 10                      # n<10 は損益をノイズ扱い (評価開始しない)
RT3_EARLY_WINDOW_N = 30                  # ① 初期下振れ停止の評価窓 (n<=30)
RT3_EARLY_ROI_FLOOR = 0.50               # ① ROI < 50% で停止
RT3_ABS_LOSS_STOP = -5000                # ② 絶対損失 ≤ -¥5000 で停止


def check_3point_health() -> dict:
    """三連系まとめ買い (浜松・山陽 rt3+rf3) の機械的停止基準を評価。

    停止条件 (docs/ev_strategy_findings.md 2026-05-31):
      ① 初期下振れ: 10<=n<=30 で 累積 ROI < 50%
      ② 絶対損失:  三連系累積損失 ≤ -¥5,000 (n 問わずリアルタイム)
      ③ drift 逆転: live ROI が backtest の 30% 未満を 30 picks 連続 (n>=30 必要)
      ④ 失格増加:  浜松・山陽 失格率が 6ヶ月平均の 1.5 倍超が 2ヶ月連続
    ①② を concrete 評価し、いずれか発動で stop。③④ は n / データ不足時は「未評価」。

    n<10 は評価開始せず (個別 R 損益はノイズ規律)。
    発動時は RT3_STOP_FLAG を書き出し、daily_predict/auto_buy が三連系購入を停止
    (複勝・参考メール表示は継続)。
    """
    out: dict = {
        "n_picks": 0, "invest": 0, "payout": 0, "profit": 0, "roi": None,
        "conditions": [],          # [(id, level, msg)]
        "triggered": False, "stop_reason": None,
        "flag_exists": RT3_STOP_FLAG.exists(),
    }
    detail_p = DATA / "bet_history_detail.csv"
    if not (detail_p.exists() and detail_p.stat().st_size > 0):
        out["conditions"].append(("-", "INFO", "bet_history_detail.csv なし — 三連系未評価"))
        return out
    try:
        df = pd.read_csv(detail_p)
    except Exception as e:
        out["conditions"].append(("-", "WARN", f"detail 読込失敗: {e}"))
        return out

    need = {"bet_type_code", "place_code", "vote_amount", "hit_amount",
            "date", "race_no"}
    if not need.issubset(df.columns):
        out["conditions"].append(("-", "INFO", "detail に必要列なし — 三連系未評価"))
        return out

    sub = df[df["bet_type_code"].isin(RT3_BET_TYPES)
             & df["place_code"].astype(int).isin(RT3_PLACES)].copy()
    if sub.empty:
        out["conditions"].append(("-", "INFO", "三連系の購入実績なし — 未評価"))
        return out

    out["n_picks"] = int(
        sub[["date", "place_code", "race_no"]].drop_duplicates().shape[0])
    out["invest"] = int(sub["vote_amount"].fillna(0).astype(float).sum())
    out["payout"] = int(sub["hit_amount"].fillna(0).astype(float).sum())
    out["profit"] = out["payout"] - out["invest"]
    out["roi"] = (out["payout"] / out["invest"]) if out["invest"] > 0 else None

    n = out["n_picks"]
    roi = out["roi"]

    # ② 絶対損失停止 (n 問わず)
    if out["profit"] <= RT3_ABS_LOSS_STOP:
        out["conditions"].append((
            "②", "NG",
            f"絶対損失停止: 累積損失 ¥{out['profit']:,} ≤ ¥{RT3_ABS_LOSS_STOP:,}"))
        out["triggered"] = True
        out["stop_reason"] = f"②絶対損失 ¥{out['profit']:,}"

    # 評価開始は n>=10
    if n < RT3_EVAL_MIN_N:
        out["conditions"].append((
            "①", "INFO",
            f"n={n} < {RT3_EVAL_MIN_N} → 初期下振れ評価は保留 (ノイズ規律)"))
    else:
        # ① 初期下振れ停止 (10<=n<=30 で ROI<50%)
        if n <= RT3_EARLY_WINDOW_N:
            if roi is not None and roi < RT3_EARLY_ROI_FLOOR:
                out["conditions"].append((
                    "①", "NG",
                    f"初期下振れ停止: n={n} 累積 ROI {roi*100:.0f}% "
                    f"< {RT3_EARLY_ROI_FLOOR*100:.0f}%"))
                out["triggered"] = True
                if not out["stop_reason"]:
                    out["stop_reason"] = f"①初期下振れ ROI {roi*100:.0f}%"
            else:
                out["conditions"].append((
                    "①", "OK",
                    f"初期下振れ OK: n={n} ROI {roi*100:.0f}% "
                    f">= {RT3_EARLY_ROI_FLOOR*100:.0f}%"))
        else:
            out["conditions"].append((
                "①", "INFO", f"n={n} > {RT3_EARLY_WINDOW_N} → ①窓外 (③へ移行)"))

    # ③ drift 逆転 (n>=30 必要、live vs close EV は別途。現状は未評価)
    if n < RT3_EARLY_WINDOW_N:
        out["conditions"].append((
            "③", "INFO", f"drift 逆転: n={n} < {RT3_EARLY_WINDOW_N} で未評価"))
    else:
        out["conditions"].append((
            "③", "INFO",
            "drift 逆転: live/close EV 比較は未実装 (要 odds_snapshots 連携)"))

    # ④ 失格率増加 (浜松・山陽、月次 6ヶ月平均比) — best-effort
    try:
        dq = _check_3point_dq_rate()
        out["conditions"].append(dq)
        if dq[1] == "NG":
            out["triggered"] = True
            if not out["stop_reason"]:
                out["stop_reason"] = "④失格率増加"
    except Exception as e:
        out["conditions"].append(("④", "INFO", f"失格率評価スキップ: {e}"))

    return out


def _check_3point_dq_rate() -> tuple:
    """④ 浜松・山陽の失格率が 6ヶ月平均の 1.5 倍超の月が 2ヶ月連続か。

    race_results.csv の accident_code (非空=事故/失格/欠車) を月次集計。
    返り値: (id, level, msg)
    """
    res_p = DATA / "race_results.csv"
    if not res_p.exists():
        return ("④", "INFO", "race_results.csv なし — 失格率未評価")
    cols = pd.read_csv(res_p, nrows=0).columns
    acc_col = next((c for c in ("accident_code", "accidentCode") if c in cols), None)
    if acc_col is None:
        return ("④", "INFO", "accident 列なし — 失格率未評価")
    df = pd.read_csv(res_p, usecols=["race_date", "place_code", acc_col])
    df = df[df["place_code"].astype(int).isin(RT3_PLACES)].copy()
    if df.empty:
        return ("④", "INFO", "浜松・山陽の結果なし — 失格率未評価")
    df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
    df = df.dropna(subset=["race_date"])
    df["ym"] = df["race_date"].dt.to_period("M")
    df["is_dq"] = df[acc_col].notna() & (df[acc_col].astype(str).str.strip() != "")
    monthly = df.groupby("ym").agg(n=("is_dq", "size"), dq=("is_dq", "sum"))
    monthly["rate"] = monthly["dq"] / monthly["n"].clip(lower=1)
    if len(monthly) < 7:
        return ("④", "INFO", f"月数 {len(monthly)} < 7 で 6ヶ月平均比 未評価")
    monthly = monthly.sort_index()
    recent2 = monthly.iloc[-2:]
    base6 = monthly.iloc[-8:-2]["rate"].mean()
    if base6 <= 0:
        return ("④", "INFO", "6ヶ月平均失格率 0 — 比較不能")
    over = (recent2["rate"] > base6 * 1.5).all()
    msg = (f"失格率 直近2ヶ月 {recent2['rate'].mean()*100:.1f}% vs "
           f"6ヶ月平均 {base6*100:.1f}% (x1.5={base6*1.5*100:.1f}%)")
    return ("④", "NG" if over else "OK", msg)


def write_rt3_stop_flag(reason: str) -> None:
    """三連系購入の停止フラグを書き出す (daily_predict/auto_buy が参照)。"""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        RT3_STOP_FLAG.write_text(
            f"stopped_at={dt.datetime.now().isoformat(timespec='seconds')}\n"
            f"reason={reason}\n"
            f"# このファイルがあると三連系まとめ買い (rt3+rf3) を停止します。\n"
            f"# 複勝と参考メール表示は継続。再開するにはこのファイルを削除。\n",
            encoding="utf-8")
    except Exception as e:
        print(f"[weekly_status] rt3_stop.flag 書き込み失敗: {e}", file=sys.stderr)


def render_3point_text(tp: dict) -> str:
    if not tp:
        return ""
    lines = ["", "【🎯 三連系まとめ買い 停止基準監視 (浜松・山陽 rt3+rf3)】"]
    if tp.get("flag_exists"):
        lines.append("  🛑 停止フラグ ON (data/rt3_stop.flag) — 三連系購入は現在停止中")
    roi = tp.get("roi")
    lines.append(
        f"  n={tp['n_picks']} / 投資 ¥{tp['invest']:,} / 払戻 ¥{tp['payout']:,} "
        f"/ 損益 ¥{tp['profit']:,} / ROI "
        + (f"{roi*100:.0f}%" if roi is not None else "—"))
    for cid, level, msg in tp.get("conditions", []):
        mark = {"NG": "🔴", "WARN": "🟡", "OK": "🟢", "INFO": "ℹ️"}.get(level, "")
        lines.append(f"    {mark} [{cid}] {msg}")
    if tp.get("triggered"):
        lines.append(f"  🔴 停止発動: {tp['stop_reason']} → RT3 購入停止 "
                     f"(複勝継続)。再開は data/rt3_stop.flag 削除。")
    return "\n".join(lines)


def render_3point_html(tp: dict) -> str:
    if not tp:
        return ""
    roi = tp.get("roi")
    flag_banner = (
        '<p style="color:#c62828; font-weight:bold; margin:4px 0;">'
        '🛑 停止フラグ ON (data/rt3_stop.flag) — 三連系購入は停止中</p>'
        if tp.get("flag_exists") else "")
    rows = []
    for cid, level, msg in tp.get("conditions", []):
        color = {"NG": "#c62828", "WARN": "#e65100",
                 "OK": "#2e7d32", "INFO": "#666"}.get(level, "#444")
        rows.append(f'<li style="color:{color}"><b>[{cid}] {level}</b>: {msg}</li>')
    trig = (
        f'<p style="color:#c62828; font-weight:bold;">🔴 停止発動: '
        f'{tp["stop_reason"]} → RT3 購入停止 (複勝継続)。'
        f'再開は data/rt3_stop.flag 削除。</p>' if tp.get("triggered") else "")
    return (
        '<h4 style="margin:14px 0 4px 0; font-size:13px;">'
        '🎯 三連系まとめ買い 停止基準監視 (浜松・山陽 rt3+rf3)</h4>'
        + flag_banner
        + f'<p style="margin:2px 0; font-size:12px;">'
        f'n={tp["n_picks"]} / 投資 ¥{tp["invest"]:,} / 払戻 ¥{tp["payout"]:,} '
        f'/ 損益 <b>¥{tp["profit"]:,}</b> / ROI '
        + (f'<b>{roi*100:.0f}%</b>' if roi is not None else "—") + '</p>'
        + '<ul style="margin:4px 0 0 18px; font-size:12px;">'
        + "".join(rows) + '</ul>' + trig)


def check_schtasks_health() -> dict:
    """schtasks LastRunResult 監視(Codex R6 + R7 提案)。

    Autorace* タスクの最終実行結果を取得し、0 以外を異常として返す。
    R7: 列名ローカライズ対策として CSV 全体を data/schtasks_snapshot.csv に
    保存し、後から人手で確認できるようにする。
    """
    out: dict = {"tasks": [], "ng_count": 0, "warnings": [], "snapshot_path": None}
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

        # R7: 列名は LANG により変わるので、substring で対応 + 両方探す
        def find_col(*needles: str) -> str | None:
            for c in df.columns:
                for n in needles:
                    if n.lower() in c.lower():
                        return c
            return None

        name_col = find_col("TaskName", "タスク名")
        result_col = find_col("Last Result", "前回の結果")
        last_run_col = find_col("Last Run", "前回の実行")

        if not name_col or not result_col:
            out["warnings"].append(
                f"schtasks 列名解析失敗 (cols={list(df.columns)[:5]}...)"
            )
            return out

        df = df[df[name_col].astype(str).str.contains("Autorace", case=False, na=False)]

        # R7: Autorace* 全件の raw snapshot を保存(検証用)
        try:
            snap_path = DATA / "schtasks_snapshot.csv"
            df.to_csv(snap_path, index=False, encoding="utf-8-sig")
            out["snapshot_path"] = str(snap_path.relative_to(ROOT))
        except Exception as e:
            out["warnings"].append(f"schtasks snapshot 保存失敗: {e}")

        # Dyn_* one-shot は数が多く、定常監視は親タスクのみ対象にする
        keep_prefixes = (
            "AutoraceDailyIngest",
            "AutoraceDynamicScheduler",
            "AutoraceFetchOrderHistory",
            "AutoraceDailyOrderHistory",  # 旧名互換
            "AutoraceWeeklyRetrain",
            "AutoraceWeeklyStatus",
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


def check_ngrok_health(days: int = 7) -> dict:
    """ngrok トンネル起動の死活監視 (Antigravity 2026-05-23 提案)。

    daily_predict.log から直近 N 日の ngrok 起動結果を集計:
      - 成功: "ngrok tunnel: https://" / "ngrok tunnel URL:" / "reusing existing ngrok"
      - 失敗: "ngrok tunnel start failed" / "ngrok unavailable" /
              "NGROK_AUTHTOKEN not found" / "ngrok command not found" /
              "ngrok process exited" / "tunnel URL not available"

    判定:
      - 試行 0      → INFO (購入推奨が出てない期間)
      - 全試行失敗  → NG (ngrok 設定要確認 → サイレント死リスク顕在化)
      - 一部失敗    → WARN (失敗率付き)
      - 全成功      → OK
    """
    out: dict = {
        "attempts": 0,
        "successes": 0,
        "failures": 0,
        "failure_reasons": [],  # [(timestamp, reason), ...] 直近 5 件
        "last_success_at": None,
        "last_failure_at": None,
        "alerts": [],
    }
    log_p = DATA / "daily_predict.log"
    if not log_p.exists():
        out["alerts"].append(("INFO", "daily_predict.log が存在しない"))
        return out

    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    success_patterns = [
        r"ngrok tunnel:\s*https?://",
        r"ngrok tunnel URL:\s*https?://",
        r"reusing existing ngrok tunnel",
    ]
    failure_patterns = [
        (r"ngrok tunnel start failed", "tunnel start 失敗 (LAN fallback)"),
        (r"ngrok unavailable", "ngrok 例外で利用不可"),
        (r"NGROK_AUTHTOKEN not found", "authtoken 未設定"),
        (r"ngrok command not found", "ngrok コマンド未発見"),
        (r"ngrok process exited", "ngrok プロセス異常終了"),
        (r"tunnel URL not available", "URL 取得タイムアウト (30s)"),
    ]

    try:
        with open(log_p, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-20000:]  # 直近 20k 行 (1 ヶ月分余裕)
    except Exception as e:
        out["alerts"].append(("WARN", f"daily_predict.log 読み込み失敗: {e}"))
        return out

    for line in tail:
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not m:
            continue
        try:
            ts = pd.to_datetime(m.group(1))
        except Exception:
            continue
        if ts < cutoff:
            continue
        # 成功判定先 (失敗 fallback ログより優先)
        matched_success = False
        for sp in success_patterns:
            if re.search(sp, line):
                out["successes"] += 1
                if out["last_success_at"] is None or ts > pd.to_datetime(out["last_success_at"]):
                    out["last_success_at"] = ts.isoformat()
                matched_success = True
                break
        if matched_success:
            continue
        for fp, reason in failure_patterns:
            if re.search(fp, line):
                out["failures"] += 1
                out["failure_reasons"].append((ts.isoformat(), reason))
                if out["last_failure_at"] is None or ts > pd.to_datetime(out["last_failure_at"]):
                    out["last_failure_at"] = ts.isoformat()
                break

    # 失敗理由は直近 5 件のみ保持 (メールを膨らませないため)
    out["failure_reasons"] = out["failure_reasons"][-5:]
    out["attempts"] = out["successes"] + out["failures"]

    if out["attempts"] == 0:
        out["alerts"].append(("INFO", "ngrok 起動試行なし (購入推奨が出てない期間)"))
    elif out["successes"] == 0:
        out["alerts"].append((
            "NG",
            f"ngrok 全試行失敗 ({out['failures']} 件 / 直近 {days} 日)"
        ))
    elif out["failures"] > 0:
        rate = out["failures"] / out["attempts"] * 100
        out["alerts"].append((
            "WARN",
            f"ngrok 失敗率 {rate:.0f}% ({out['failures']}/{out['attempts']} 件 / 直近 {days} 日)"
        ))
    return out


def check_model_freshness(st_health: dict | None = None) -> dict:
    """本番モデルの塩漬けリスク監視 (Antigravity 2026-05-23 提案 #2、
    2026-05-23 修正で「再学習未実行」と「連続却下」を区別)。

    schtasks の AutoraceWeeklyRetrain.last_run と trained_at を比較し、
    retrain_status を 3 状態に分類:
      - "rejected"     : 再学習は走ってるが採用見送り (本当の塩漬け)
      - "cron_inactive": 再学習 cron が走ってない / 失敗
      - "fresh"        : 直近で trained_at 更新 (採用済)

    判定:
      - 14 日以内: OK
      - 21 日超 (cron_inactive)  → WARN: cron 障害の可能性
      - 21 日超 (rejected) 連続却下>=2 → WARN: 塩漬け監視継続
      - 35 日超 OR 連続却下>=4 → NG: 概念ドリフト/cron 障害深刻
    """
    out: dict = {
        "model_trained_at": None,
        "model_age_days": None,
        "current_verdict": None,
        "consecutive_rejections": 0,
        "last_attempt_at": None,
        "last_attempt_verdict": None,
        "history_available": False,
        "weekly_retrain_last_run": None,
        "retrain_status": "unknown",
        "alerts": [],
    }
    meta_p = DATA / "production_meta.json"
    if not meta_p.exists():
        out["alerts"].append(("WARN", "production_meta.json が無い"))
        return out
    try:
        with open(meta_p, "r", encoding="utf-8") as f:
            meta = json.load(f)
        trained = pd.to_datetime(meta.get("trained_at"))
        out["model_trained_at"] = trained.isoformat() if pd.notna(trained) else None
        if pd.notna(trained):
            age = (pd.Timestamp.now() - trained).days
            out["model_age_days"] = age
        out["current_verdict"] = meta.get("quality_gate_verdict")  # 旧モデルだと None
    except Exception as e:
        out["alerts"].append(("WARN", f"production_meta.json 読み込み失敗: {e}"))

    # retrain_history.csv から連続却下数をカウント (採用日以降の試行のみ対象)
    hist_p = DATA / "retrain_history.csv"
    if hist_p.exists():
        out["history_available"] = True
        try:
            hist = pd.read_csv(hist_p)
            if not hist.empty and "timestamp" in hist.columns:
                hist["timestamp"] = pd.to_datetime(hist["timestamp"], errors="coerce")
                hist = hist.dropna(subset=["timestamp"]).sort_values("timestamp")
                # 採用日 (model_trained_at) 以降の試行
                if out["model_trained_at"]:
                    trained_ts = pd.to_datetime(out["model_trained_at"])
                    after = hist[hist["timestamp"] > trained_ts]
                else:
                    after = hist
                if not after.empty:
                    last_row = after.iloc[-1]
                    out["last_attempt_at"] = last_row["timestamp"].isoformat()
                    out["last_attempt_verdict"] = str(last_row.get("verdict", ""))
                    # 連続却下: 直近から OK が出るまで遡る (NG/WARN を却下扱い)
                    count = 0
                    for _, row in after[::-1].iterrows():
                        if str(row.get("verdict", "")) in ("NG", "WARN"):
                            count += 1
                        else:
                            break
                    out["consecutive_rejections"] = count
        except Exception as e:
            out["alerts"].append(("WARN", f"retrain_history.csv 読み込み失敗: {e}"))

    # schtasks の AutoraceWeeklyRetrain last_run と比較して状態判別
    weekly_retrain_last: pd.Timestamp | None = None
    if st_health and st_health.get("tasks"):
        for task in st_health["tasks"]:
            if task.get("name", "").startswith("AutoraceWeeklyRetrain"):
                lr = task.get("last_run", "")
                if lr:
                    try:
                        weekly_retrain_last = pd.to_datetime(lr)
                    except Exception:
                        pass
                break
    if weekly_retrain_last is not None:
        out["weekly_retrain_last_run"] = weekly_retrain_last.isoformat()

    trained_ts = (pd.to_datetime(out["model_trained_at"])
                  if out["model_trained_at"] else None)
    if trained_ts is None:
        out["retrain_status"] = "unknown"
    elif weekly_retrain_last is None:
        out["retrain_status"] = "schtasks_unavailable"  # 判定保留
    elif weekly_retrain_last > trained_ts:
        # 再学習が走ってる (last_run > trained_at) のに採用更新されてない
        # = 採用見送り (本当の塩漬け)
        out["retrain_status"] = "rejected"
    else:
        # 再学習 cron 自体が走ってない or last_run が trained_at 以前
        # (= 採用後 retrain 未実施 = 翌週まで待ち)
        out["retrain_status"] = "fresh" if (out.get("model_age_days") or 0) < 8 else "cron_inactive"

    # 判定
    age = out.get("model_age_days")
    rej = out.get("consecutive_rejections", 0)
    status = out["retrain_status"]
    if age is not None:
        if status == "cron_inactive" and age >= 35:
            out["alerts"].append((
                "NG",
                f"再学習 cron 異常 {age} 日: WeeklyRetrain.last_run="
                f"{out['weekly_retrain_last_run']} ≤ trained_at — schtasks 要確認"
            ))
        elif status == "rejected" and (age >= 35 or rej >= 4):
            out["alerts"].append((
                "NG",
                f"モデル塩漬け {age} 日 / 連続却下 {rej} 回 — "
                f"再学習は走ってるが品質ゲート連続 NG = 概念ドリフトの可能性、要再検討"
            ))
        elif status == "cron_inactive" and age >= 21:
            out["alerts"].append((
                "WARN",
                f"再学習 cron 不調 {age} 日: WeeklyRetrain.last_run="
                f"{out['weekly_retrain_last_run']} ≤ trained_at — cron 障害の可能性"
            ))
        elif status == "rejected" and (age >= 21 or rej >= 2):
            out["alerts"].append((
                "WARN",
                f"モデル塩漬け {age} 日 / 連続却下 {rej} 回 — "
                f"再学習は走ってるが採用見送り継続中"
            ))
        elif status == "schtasks_unavailable" and age >= 21:
            out["alerts"].append((
                "WARN",
                f"モデル {age} 日経過、schtasks 情報なしで cron 状態不明 (要 schtasks 確認)"
            ))
        elif age >= 14 and not out["history_available"]:
            out["alerts"].append((
                "INFO",
                f"モデル経過 {age} 日 / retrain_history 未蓄積 "
                f"(retrain_status={status})"
            ))
    return out


def _overall_health(bh: dict, st: dict, ehi: dict | None = None,
                    ng: dict | None = None, mf: dict | None = None) -> str:
    """bet_history / schtasks / EHI / ngrok / model_freshness の異常を集約。

    R9 で warnings 握り潰しバグ修正、2026-05-23 で ngrok + model_freshness
    追加 (Antigravity 提案)。
    """
    if bh.get("status") == "NG":
        return "NG"
    if any(lv == "NG" for lv, _ in bh.get("alerts", [])):
        return "NG"
    if ehi and ehi.get("status") == "DANGER":
        return "NG"
    if ng and any(lv == "NG" for lv, _ in ng.get("alerts", [])):
        return "NG"
    if mf and any(lv == "NG" for lv, _ in mf.get("alerts", [])):
        return "NG"
    if bh.get("status") == "WARN":
        return "WARN"
    if st.get("ng_count", 0) > 0:
        return "WARN"
    if st.get("warnings"):
        return "WARN"
    if ehi and ehi.get("status") == "WARNING":
        return "WARN"
    if ng and any(lv == "WARN" for lv, _ in ng.get("alerts", [])):
        return "WARN"
    if mf and any(lv == "WARN" for lv, _ in mf.get("alerts", [])):
        return "WARN"
    return "OK"


def render_health_text(bh: dict, st: dict, ehi: dict | None = None,
                       ng: dict | None = None, mf: dict | None = None) -> str:
    lines = []
    status_emoji = {"OK": "🟢", "WARN": "🟡", "NG": "🔴"}
    overall = _overall_health(bh, st, ehi, ng, mf)
    lines.append(f"【🩺 死活監視】 {status_emoji.get(overall, '')} {overall}")
    
    if ehi and ehi.get("ehi") is not None:
        lines.append(
            f"  Edge Health Index (7d): {ehi['ehi']} {ehi['emoji']} {ehi['status']} "
            f"(n={ehi.get('n_races', 0)})"
        )
    elif ehi and ehi.get("status") == "NO_DATA":
        lines.append(f"  Edge Health Index (7d): {ehi.get('message', 'No data')}")

    if bh.get("last_date"):
        lines.append(f"  bet_history 最終日: {bh['last_date']}  / 直近 7 日 R 数: {bh['recent_r_count']}")
    else:
        lines.append("  bet_history: データなし")
    lines.append(f"  fetch_order_history 最終成功: {bh.get('log_last_success') or '不明'}")
    # R7 の alerts(level + msg)
    for level, msg in bh.get("alerts", []):
        prefix = {"NG": "🔴 NG", "WARN": "🟡 WARN", "INFO": "ℹ️  INFO"}.get(level, level)
        lines.append(f"  {prefix}: {msg}")
    # R9: 明細は overall != OK もしくは streak ≥ 3 のときだけ表示
    show_details = (overall != "OK") or (bh.get("no_bet_streak_days", 0) >= 3)
    if show_details and bh.get("missing_picks_details"):
        for d, pc, rn in bh["missing_picks_details"]:
            lines.append(f"     - {d} 場{pc} R{rn}")
    elif bh.get("missing_picks", 0) > 0:
        lines.append(
            f"     (明細省略: 全体 OK のため。詳細は scripts/reconcile_recommendations_vs_bets.py)"
        )
    if not bh.get("alerts"):
        lines.append("  ✅ 全項目正常")

    if st.get("tasks"):
        lines.append("")
        lines.append(f"  schtasks Autorace* {len(st['tasks'])} 件 / NG {st['ng_count']} 件")
        for t in st["tasks"]:
            mark = "✅" if t["result"] == 0 else ("⏳" if t["result"] == 267011 else "🔴")
            lines.append(f"    {mark} {t['name']:34s} rc={t['result']:>6} last_run={t['last_run']}")
        if st.get("snapshot_path"):
            lines.append(f"  (raw snapshot: {st['snapshot_path']})")
    for w in st.get("warnings", []):
        lines.append(f"  ⚠️ schtasks: {w}")

    # ngrok 死活監視セクション (Antigravity 2026-05-23 提案)
    if ng is not None:
        lines.append("")
        lines.append(
            f"  ngrok トンネル (購入推奨時のみ起動): "
            f"成功 {ng.get('successes', 0)} / 失敗 {ng.get('failures', 0)} "
            f"/ 試行 {ng.get('attempts', 0)}"
        )
        if ng.get("last_success_at"):
            lines.append(f"    最終成功: {ng['last_success_at']}")
        if ng.get("last_failure_at"):
            lines.append(f"    最終失敗: {ng['last_failure_at']}")
        for level, msg in ng.get("alerts", []):
            prefix = {"NG": "🔴 NG", "WARN": "🟡 WARN", "INFO": "ℹ️  INFO"}.get(level, level)
            lines.append(f"    {prefix}: {msg}")
        # 失敗理由詳細 (直近 5 件) は WARN/NG 時のみ
        if ng.get("failures", 0) > 0 and ng.get("failure_reasons"):
            for ts, reason in ng["failure_reasons"]:
                lines.append(f"      - {ts}  {reason}")

    # モデル塩漬けリスク (Antigravity 2026-05-23 提案 #2)
    if mf is not None:
        lines.append("")
        age = mf.get("model_age_days")
        rej = mf.get("consecutive_rejections", 0)
        status_label = {
            "rejected": "📛 連続却下 (再学習は走ってるが採用見送り)",
            "cron_inactive": "⚠️ 再学習 cron 未実行 (schtasks 障害の可能性)",
            "fresh": "✅ 直近採用済",
            "schtasks_unavailable": "❓ schtasks 情報なし",
            "unknown": "❓ trained_at 不明",
        }.get(mf.get("retrain_status", "unknown"), "?")
        lines.append(
            f"  本番モデル塩漬け監視: "
            f"trained_at={mf.get('model_trained_at', '不明')}"
            f"{' (' + str(age) + ' 日経過)' if age is not None else ''}"
        )
        lines.append(f"    状態: {status_label} / 連続却下 {rej} 回")
        if mf.get("weekly_retrain_last_run"):
            lines.append(
                f"    AutoraceWeeklyRetrain 直近実行: {mf['weekly_retrain_last_run']}"
            )
        if mf.get("last_attempt_at"):
            lines.append(
                f"    直近の再学習 attempt (history): {mf['last_attempt_at']} "
                f"(verdict={mf.get('last_attempt_verdict', '?')})"
            )
        if not mf.get("history_available"):
            lines.append(
                f"    (retrain_history.csv 未蓄積 — 次回 train_production 実行から記録開始)"
            )
        for level, msg in mf.get("alerts", []):
            prefix = {"NG": "🔴 NG", "WARN": "🟡 WARN", "INFO": "ℹ️  INFO"}.get(level, level)
            lines.append(f"    {prefix}: {msg}")

    return "\n".join(lines)


def render_health_html(bh: dict, st: dict, ehi: dict | None = None,
                       ng: dict | None = None, mf: dict | None = None) -> str:
    BORDER = '"border-collapse:collapse; border-color:#bbb; font-family:Arial,sans-serif; font-size:13px;"'
    TH = '"background:#e8e8e8; padding:6px 10px; border:1px solid #bbb; text-align:center;"'
    TD = '"padding:6px 10px; border:1px solid #ddd; text-align:left;"'
    TD_R = '"padding:6px 10px; border:1px solid #ddd; text-align:right;"'

    miss = bh.get("missing_picks", 0)
    overall = _overall_health(bh, st, ehi, ng, mf)
    status_color = {"OK": "#2e7d32", "WARN": "#e65100", "NG": "#c62828"}.get(overall, "#444")
    status_emoji = {"OK": "🟢", "WARN": "🟡", "NG": "🔴"}.get(overall, "")
    parts = [
        f'<h3 style="color:{status_color}; margin:18px 0 8px 0;">'
        f'🩺 死活監視 <span style="font-size:13px;">{status_emoji} {overall}</span></h3>'
    ]
    # R7 alerts(WARN/NG/INFO 一覧)
    if bh.get("alerts"):
        parts.append('<ul style="margin:0 0 8px 18px; font-size:12px;">')
        for level, msg in bh["alerts"]:
            color = {"NG": "#c62828", "WARN": "#e65100", "INFO": "#666"}.get(level, "#444")
            parts.append(f'<li style="color:{color}"><b>{level}</b>: {msg}</li>')
        parts.append("</ul>")
    parts.append(f'<table border="1" cellpadding="6" cellspacing="0" style={BORDER}>')
    parts.append(f'<tr><th style={TH}>項目</th><th style={TH}>値</th></tr>')
    
    if ehi and ehi.get("ehi") is not None:
        ehi_val = ehi["ehi"]
        ehi_color = {"HEALTHY": "#2e7d32", "WARNING": "#e65100", "DANGER": "#c62828"}.get(ehi["status"], "#444")
        parts.append(
            f'<tr><td style={TD}>Edge Health Index (7d)</td>'
            f'<td style={TD_R}><b style="color:{ehi_color}">{ehi_val}</b> '
            f'({ehi["emoji"]} {ehi["status"]})</td></tr>'
        )

    parts.append(
        f'<tr style="background:#fafafa;"><td style={TD}>bet_history 最終日</td>'
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
    # R9: 明細は overall != OK もしくは streak ≥ 3 のときだけ表示
    show_details = (overall != "OK") or (bh.get("no_bet_streak_days", 0) >= 3)
    if show_details and bh.get("missing_picks_details"):
        parts.append('<ul style="margin:6px 0 0 18px; color:#c62828; font-size:12px;">')
        for d, pc, rn in bh["missing_picks_details"]:
            parts.append(f"<li>{d} 場{pc} R{rn}</li>")
        parts.append("</ul>")
    elif miss > 0:
        parts.append(
            '<p style="color:#888; font-size:11px; margin:4px 0;">'
            '(明細省略: 全体 OK のため。詳細は <code>scripts/reconcile_recommendations_vs_bets.py</code>)</p>'
        )

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
    for w in ["schtasks: " + w for w in st.get("warnings", [])]:
        parts.append(f'<p style="color:#e65100; margin:4px 0; font-size:12px;">⚠️ {w}</p>')
    if st.get("snapshot_path"):
        parts.append(
            f'<p style="color:#888; font-size:11px; margin:4px 0;">'
            f'raw snapshot: {st["snapshot_path"]}</p>'
        )

    _append_ngrok_html(parts, ng)
    _append_model_freshness_html(parts, mf)
    return "\n".join(parts)


def _append_ngrok_html(parts: list[str], ng: dict | None) -> None:
    """ngrok 死活監視セクションを parts に追記 (render_health_html の下請け)。"""
    # ngrok 死活監視 (Antigravity 2026-05-23 提案)
    if ng is not None:
        parts.append(
            f'<h4 style="margin:14px 0 4px 0; font-size:13px;">'
            f'🌐 ngrok トンネル (購入推奨時のみ起動)</h4>'
        )
        parts.append(
            f'<p style="margin:2px 0; font-size:12px;">'
            f'成功 <b>{ng.get("successes", 0)}</b> / '
            f'失敗 <b style="color:#c62828">{ng.get("failures", 0)}</b> / '
            f'試行 {ng.get("attempts", 0)}'
            f'</p>'
        )
        if ng.get("last_success_at") or ng.get("last_failure_at"):
            parts.append('<ul style="margin:2px 0 0 18px; font-size:11px; color:#666;">')
            if ng.get("last_success_at"):
                parts.append(f'<li>最終成功: {ng["last_success_at"]}</li>')
            if ng.get("last_failure_at"):
                parts.append(f'<li>最終失敗: {ng["last_failure_at"]}</li>')
            parts.append("</ul>")
        if ng.get("alerts"):
            parts.append('<ul style="margin:4px 0 0 18px; font-size:12px;">')
            for level, msg in ng["alerts"]:
                color = {"NG": "#c62828", "WARN": "#e65100", "INFO": "#666"}.get(level, "#444")
                parts.append(f'<li style="color:{color}"><b>{level}</b>: {msg}</li>')
            parts.append("</ul>")
        if ng.get("failures", 0) > 0 and ng.get("failure_reasons"):
            parts.append(
                '<p style="margin:4px 0 2px 0; font-size:11px; color:#888;">'
                '失敗理由 (直近 5 件):</p>'
            )
            parts.append('<ul style="margin:0 0 0 18px; font-size:11px; color:#666;">')
            for ts, reason in ng["failure_reasons"]:
                parts.append(f'<li>{ts}  {reason}</li>')
            parts.append("</ul>")


def _append_model_freshness_html(parts: list[str], mf: dict | None) -> None:
    """モデル塩漬け監視セクションを parts に追記 (render_health_html の下請け)。"""
    # モデル塩漬けリスク (Antigravity 2026-05-23 提案 #2)
    if mf is not None:
        age = mf.get("model_age_days")
        rej = mf.get("consecutive_rejections", 0)
        status_label = {
            "rejected": "📛 連続却下 (再学習は走ってるが採用見送り)",
            "cron_inactive": "⚠️ 再学習 cron 未実行",
            "fresh": "✅ 直近採用済",
            "schtasks_unavailable": "❓ schtasks 情報なし",
            "unknown": "❓ trained_at 不明",
        }.get(mf.get("retrain_status", "unknown"), "?")
        parts.append(
            f'<h4 style="margin:14px 0 4px 0; font-size:13px;">'
            f'🧠 本番モデル塩漬け監視</h4>'
        )
        parts.append(
            f'<p style="margin:2px 0; font-size:12px;">'
            f'trained_at: <b>{mf.get("model_trained_at", "不明")}</b>'
            + (f' (<b>{age}</b> 日経過)' if age is not None else '')
            + f'</p>'
        )
        parts.append(
            f'<p style="margin:2px 0; font-size:12px;">'
            f'状態: {status_label} / 連続却下 '
            f'<b style="color:{"#c62828" if rej >= 2 else "#444"}">{rej}</b> 回'
            f'</p>'
        )
        if mf.get("weekly_retrain_last_run"):
            parts.append(
                f'<p style="margin:2px 0; font-size:11px; color:#666;">'
                f'AutoraceWeeklyRetrain 直近実行: {mf["weekly_retrain_last_run"]}'
                f'</p>'
            )
        if mf.get("last_attempt_at"):
            parts.append(
                f'<p style="margin:2px 0; font-size:11px; color:#666;">'
                f'直近 attempt (history): {mf["last_attempt_at"]} '
                f'(verdict={mf.get("last_attempt_verdict", "?")})'
                f'</p>'
            )
        if not mf.get("history_available"):
            parts.append(
                f'<p style="margin:2px 0; font-size:11px; color:#888;">'
                f'(retrain_history.csv 未蓄積 — 次回 train_production 実行から記録開始)'
                f'</p>'
            )
        if mf.get("alerts"):
            parts.append('<ul style="margin:4px 0 0 18px; font-size:12px;">')
            for level, msg in mf["alerts"]:
                color = {"NG": "#c62828", "WARN": "#e65100", "INFO": "#666"}.get(level, "#444")
                parts.append(f'<li style="color:{color}"><b>{level}</b>: {msg}</li>')
            parts.append("</ul>")


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


def _insert_before_footer(html: str, section_html: str) -> str:
    """フッター <hr> の直前にセクション HTML を 1 回だけ挿入する。"""
    return html.replace(
        '<hr style="border:none;',
        section_html + '\n<hr style="border:none;',
        1,
    )


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

    # 死活監視(Codex R6 提案: bet_history + schtasks + EHI、
    # Antigravity 2026-05-23 提案: ngrok + model_freshness)
    bh_health: dict = {}
    st_health: dict = {}
    ehi_health: dict | None = None
    ng_health: dict | None = None
    mf_health: dict | None = None
    try:
        if _EHI_AVAILABLE:
            ehi_health = calculate_ehi(7)

        bh_health = check_bet_history_health(args.days)
        st_health = check_schtasks_health()
        ng_health = check_ngrok_health(args.days)
        # st_health を渡すことで「再学習未実行」と「連続却下」を区別
        mf_health = check_model_freshness(st_health)
        text += "\n\n" + render_health_text(
            bh_health, st_health, ehi_health, ng_health, mf_health
        )
        health_html = render_health_html(
            bh_health, st_health, ehi_health, ng_health, mf_health
        )
        html = _insert_before_footer(html, health_html)
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
            html = _insert_before_footer(html, cum_html)
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
            html = _insert_before_footer(html, audit_html)
        except Exception as e:
            text += f"\n\n(picks 監査スキップ: {e})"

    # 推奨 vs 購入 整合監査(R6 詳細 → R7 で件数のみに圧縮)
    # 詳細は scripts/reconcile_recommendations_vs_bets.py --days N --csv-out で確認
    try:
        from reconcile_recommendations_vs_bets import (
            build_summary as _rec_build,
            render_compact_text as _rec_text,
            render_compact_html as _rec_html,
        )
        rec = _rec_build(args.days)
        text += "\n\n" + _rec_text(rec)
        rec_html = _rec_html(rec)
        html = _insert_before_footer(html, rec_html)
    except Exception as e:
        text += f"\n\n(推奨 vs 購入 監査スキップ: {e})"

    # 実購入損益サマリ (bet_history.csv ベース)
    try:
        from bet_history_summary import build_summary as _bh_build
        bh = _bh_build(args.days)
        if bh is not None:
            bh_text, bh_html = bh
            text += "\n\n" + bh_text
            html = _insert_before_footer(html, bh_html)
    except Exception as e:
        text += f"\n\n(実購入損益スキップ: {e})"

    # 三連系実弾 判定指標 (2026-07-26 Go/No-Go: 100R / ROI>120% を維持できるか)
    # bet_history.csv には券種列が無いため、券種付き detail から三連系のみ分離集計。
    try:
        from bet_history_summary import build_sanren_section as _sanren_build
        sanren = _sanren_build(args.days)
        if sanren is not None:
            sanren_text, sanren_html = sanren
            text += "\n\n" + sanren_text
            html = _insert_before_footer(html, sanren_html)
    except Exception as e:
        text += f"\n\n(三連系判定指標スキップ: {e})"

    # 三連系まとめ買い 停止基準監視 + 発動時 kill-switch
    tp_health: dict = {}
    try:
        tp_health = check_3point_health()
        text += "\n\n" + render_3point_text(tp_health)
        tp_html = render_3point_html(tp_health)
        html = _insert_before_footer(html, tp_html)
        if tp_health.get("triggered") and not tp_health.get("flag_exists"):
            write_rt3_stop_flag(tp_health.get("stop_reason", "stop"))
            text += "\n  → data/rt3_stop.flag を書き出しました (三連系購入を停止)。"
    except Exception as e:
        text += f"\n\n(三連系停止基準スキップ: {e})"

    print(text)

    if args.no_email:
        return

    today = dt.date.today().isoformat()
    ok_count = sum(1 for d in days if d["status"] == "OK")
    fail_count = len(days) - ok_count
    if errors:
        ingest_status = "🔴NG"
    elif fail_count >= 4:
        ingest_status = "🟡WARN"
    else:
        ingest_status = "🟢OK"

    # R9: subject に health overall(bet_history + schtasks + EHI)を反映
    # 上で計算した bh_health / st_health / ehi_health を再利用
    try:
        if bh_health or st_health or ehi_health:
            health_overall = _overall_health(bh_health, st_health, ehi_health)
        else:
            health_overall = "?"
    except Exception:
        health_overall = "?"
    health_emoji = {"OK": "🟢", "WARN": "🟡", "NG": "🔴", "?": "❔"}.get(
        health_overall, "❔"
    )
    rt3_tag = ""
    if tp_health.get("triggered"):
        rt3_tag = " 🛑RT3停止"
    elif tp_health.get("flag_exists"):
        rt3_tag = " 🛑RT3停止中"
    subject = (
        f"[autorace] 週次 {today} ingest={ingest_status} "
        f"health={health_emoji}{health_overall} (OK {ok_count}/{len(days)})"
        f"{rt3_tag}"
    )

    send_email(subject=subject, body=text, html=html)


if __name__ == "__main__":
    main()
