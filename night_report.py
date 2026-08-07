"""夜次メール + 反省ログ v2 (2026-08-07、夜次メール契約 v1 対応)。

契約: Public-Race-Management-System/docs/nightly_mail_reflection_spec.md
モデル: boat-racing-ai の night_report.py (v1 は簡易 plaintext だったが、
v2 で boat 同等の構成に増強: HTML テーブル / 券種別内訳 / レース内訳表 /
EV 乖離分析 / AI 振り返り (API 無ければテンプレ) / MD レポート保存)

構成 (メール節):
  1. 本日サマリ (暫定)     — auto_buy_state の実発注を RaceRefund API で精算
  2. 券種別 内訳           — 複勝 / 三連単 / 三連複
  3. 前日確定 + 累積        — bet_history.csv (2 段構え: 暫定→翌晩確定で補正)
  4. 発火EV vs 確定EV 乖離  — shadow_picks (fire_ev) × odds_ts +2分 (確定オッズ)。
                             auto 既知の drift 問題の日次モニタ
  5. 本日レース内訳         — 買い目 / 実着順 / 券種ごと ⭕❌⏳
  6. 振り返り              — ANTHROPIC_API_KEY があれば AI 生成、無ければテンプレ

永続化:
  - 反省の正本: logs/reflections/YYYY-MM.md (契約 §3、git 管理)
  - フルレポート MD: data/reports/night_YYYY-MM-DD.md (boat と同じ流儀)

使い方:
  python night_report.py             # 集計 + MD/反省保存 + メール送信
  python night_report.py --dry-run   # 送信せず本文表示 (保存もしない)
  python night_report.py --no-email  # 保存はするが送信しない
タスク: AutoraceNightReport 毎日 00:00 (scripts/run_night_report_hidden.vbs)

対象日の決定 (2026-08-08 変更): 22:00 実行では山陽ミッドナイト (〜23:50) の
発注が報告から漏れる (8/7 に 22:42/23:08 の 2 発が実際に漏れた) ため、
全レース確定後の 00:00 実行に変更。日付が跨いでいるので、実行時刻が
正午より前なら「前日」をレポート対象にする (00:00 発火 → 直前に終わった
開催日を報告)。--date 指定時はそちらが優先。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
STATE_FILE = DATA / "auto_buy_state.json"
BET_HISTORY = DATA / "bet_history.csv"
SHADOW_CSV = DATA / "shadow_picks.csv"
ODDS_TS_DIR = DATA / "odds_ts"
REPORT_DIR = DATA / "reports"
REFLECTION_DIR = ROOT / "logs" / "reflections"
LOG_FILE = DATA / "night_report.log"

VENUE_PC = {"kawaguchi": 2, "isesaki": 3, "hamamatsu": 4, "iizuka": 5,
            "sanyou": 6}
VENUE_JP = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
BET_JP = {"fns": "複勝", "rt3": "三連単", "rf3": "三連複"}
FNS_EV_THR = 1.50   # 複勝の購入閾値 (乖離表の「跨ぎ」判定に使う)

# 的中率の期待レンジ (テンプレ振り返りの乖離コメント用)
EXPECT_HIT = {"fns": (0.55, 0.80), "sanren": (0.05, 0.35)}

# ── HTML inline スタイル (Gmail 互換、boat と同じ流儀) ────────────────────
_S_BODY = "font-family:sans-serif;font-size:14px;color:#222;line-height:1.6;"
_S_TABLE = "border-collapse:collapse;margin:8px 0;font-size:13px;"
_S_TH = "border:1px solid #999;padding:6px 12px;background:#fdeaea;text-align:left;"
_S_TD = "border:1px solid #999;padding:6px 12px;"
_S_TD_NUM = _S_TD + "text-align:right;font-variant-numeric:tabular-nums;"
_S_H2 = "color:#c62828;border-left:4px solid #c62828;padding-left:8px;margin-top:24px;"
_S_CARD = "background:#f5f5f5;padding:12px 16px;border-radius:6px;margin:12px 0;"
_GREEN = "#388e3c"
_RED = "#d32f2f"


def setup_logging() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )


# =====================================================================
# 当日データの収集
# =====================================================================

def parse_bets_desc(desc: str) -> list[dict]:
    """auto_buy の日本語 bets 表記 (format_bets_jp が機械生成、決定的) を構造化。"""
    out = []
    for part in desc.split(" / "):
        part = part.strip()
        m = re.match(r"複勝 (\d)号 ¥([\d,]+)", part)
        if m:
            out.append({"type": "fns", "cars": [int(m.group(1))],
                        "amount": int(m.group(2).replace(",", ""))})
            continue
        m = re.match(r"三連単 (\d)→(\d)→(\d) ¥([\d,]+)", part)
        if m:
            out.append({"type": "rt3",
                        "cars": [int(m.group(i)) for i in (1, 2, 3)],
                        "amount": int(m.group(4).replace(",", ""))})
            continue
        m = re.match(r"三連複 (\d)=(\d)=(\d) ¥([\d,]+)", part)
        if m:
            out.append({"type": "rf3",
                        "cars": [int(m.group(i)) for i in (1, 2, 3)],
                        "amount": int(m.group(4).replace(",", ""))})
    return out


def load_today_executions(today: str) -> tuple[list[dict], int]:
    """当日の実発注 (verdict=executed) と発注失敗数。"""
    if not STATE_FILE.exists():
        return [], 0
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logging.warning("auto_buy_state 読込失敗: %s", e)
        return [], 0
    if st.get("date") != today:
        return [], 0
    execs, failed = [], 0
    for e in st.get("executions", []):
        v = e.get("verdict")
        if v == "failed":
            failed += 1
        if v != "executed":
            continue
        venue_short, _, rno = str(e.get("race", "")).rpartition("_R")
        pc = VENUE_PC.get(venue_short)
        if pc is None or not rno.isdigit():
            continue
        execs.append({
            "pc": pc, "race_no": int(rno),
            "venue_jp": VENUE_JP.get(pc, "?"),
            "bets": parse_bets_desc(e.get("bets", "")),
            "desc": e.get("bets", ""),
        })
    return execs, failed


def load_shadow_today(today: str) -> dict[tuple[int, int], dict]:
    """shadow_picks (発火時の top-1 pred/EV/オッズ) を {(pc, rno): row} で。"""
    out: dict[tuple[int, int], dict] = {}
    if not SHADOW_CSV.exists():
        return out
    try:
        import pandas as pd
        df = pd.read_csv(SHADOW_CSV)
        for _, r in df[df["race_date"] == today].iterrows():
            out[(int(r["place_code"]), int(r["race_no"]))] = {
                "car_no": int(r["car_no"]),
                "pred_calib": float(r["pred_calib"]),
                "fire_ev": float(r["fire_ev"]),
                "odds_min": r.get("place_odds_min"),
                "odds_max": r.get("place_odds_max"),
            }
    except Exception as e:
        logging.warning("shadow_picks 読込失敗: %s", e)
    return out


def load_final_fns_odds(today: str) -> dict[tuple[int, int], dict[int, float]]:
    """odds_ts の +2 分 (締切後 = 確定) スナップから複勝オッズ中点を取る。

    戻り値: {(pc, rno): {car: mid_odds}}
    """
    out: dict[tuple[int, int], dict[int, float]] = {}
    p = ODDS_TS_DIR / f"{today}.jsonl"
    if not p.exists():
        return out
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("offset_min") != 2:
                continue
            fns = (o.get("body") or {}).get("fnsOddsList") or {}
            mids: dict[int, float] = {}
            for car, mm in fns.items():
                try:
                    lo, hi = float(mm.get("min", 0)), float(mm.get("max", 0))
                    if lo > 0:
                        mids[int(car)] = (lo + hi) / 2
                except Exception:
                    continue
            if mids:
                out[(int(o["place_code"]), int(o["race_no"]))] = mids
    except Exception as e:
        logging.warning("odds_ts 読込失敗: %s", e)
    return out


def fetch_refunds(pcs: set[int], today: str) -> dict[tuple[int, int], dict]:
    """{(pc, race_no): refundInfo}。API 失敗場は欠損 (結果待ち扱い)。"""
    out: dict[tuple[int, int], dict] = {}
    try:
        from src.client import AutoraceClient
        client = AutoraceClient()
    except Exception as e:
        logging.warning("AutoraceClient 初期化失敗 (全レース結果待ち扱い): %s", e)
        return out
    for pc in sorted(pcs):
        try:
            body = client.get_race_refund(pc, today).get("body", [])
            if not isinstance(body, list):
                continue
            for race in body:
                ri = race.get("refundInfo")
                if ri:
                    out[(pc, int(race.get("raceNo", 0)))] = ri
        except Exception as e:
            logging.warning("RaceRefund 取得失敗 pc=%d: %s", pc, e)
    return out


def settle_bet(bet: dict, ri: dict) -> int:
    def entries(key):
        d = ri.get(key)
        return d.get("list", []) if isinstance(d, dict) else []

    if bet["type"] == "fns":
        for e in entries("fns"):
            if int(e.get("carNo", -1)) == bet["cars"][0]:
                return int(e.get("refund", 0)) * bet["amount"] // 100
    elif bet["type"] == "rt3":
        for e in entries("rt3"):
            fin = [int(e.get(k, -1)) for k in ("1thCarNo", "2thCarNo", "3thCarNo")]
            if fin == bet["cars"]:
                return int(e.get("refund", 0)) * bet["amount"] // 100
    elif bet["type"] == "rf3":
        for e in entries("rf3"):
            fin = sorted(int(e.get(k, -1)) for k in ("1thCarNo", "2thCarNo", "3thCarNo"))
            if fin == sorted(bet["cars"]):
                return int(e.get("refund", 0)) * bet["amount"] // 100
    return 0


def actual_top3(ri: dict) -> list[int] | None:
    d = ri.get("rt3")
    lst = d.get("list", []) if isinstance(d, dict) else []
    if lst:
        try:
            fin = [int(lst[0].get(k)) for k in ("1thCarNo", "2thCarNo", "3thCarNo")]
            if all(f > 0 for f in fin):
                return fin
        except Exception:
            pass
    return None


def evaluate_today(today: str) -> tuple[dict, int]:
    """当日実発注の暫定精算 + shadow/確定オッズの付与。"""
    execs, n_failed = load_today_executions(today)
    refunds = fetch_refunds({e["pc"] for e in execs}, today) if execs else {}
    shadow = load_shadow_today(today)
    final_odds = load_final_fns_odds(today)

    bt_totals = {bt: {"stake": 0, "payout": 0, "n": 0, "hits": 0, "pending": 0}
                 for bt in ("fns", "rt3", "rf3")}
    total_stake_settled = total_payout = 0
    n_pending = 0

    for ex in execs:
        key = (ex["pc"], ex["race_no"])
        ri = refunds.get(key)
        ex["pending"] = ri is None
        ex["bet_yen"] = sum(b["amount"] for b in ex["bets"])
        ex["refund_yen"] = 0
        ex["top3"] = actual_top3(ri) if ri else None

        # shadow (発火時 pred / EV) と確定オッズから final_ev
        sh = shadow.get(key)
        ex["pred_calib"] = sh["pred_calib"] if sh else None
        ex["fire_ev"] = sh["fire_ev"] if sh else None
        ex["final_ev"] = None
        if sh:
            mids = final_odds.get(key) or {}
            mid = mids.get(sh["car_no"])
            if mid:
                ex["final_ev"] = round(sh["pred_calib"] * mid, 3)

        if ri is None:
            n_pending += 1
            for b in ex["bets"]:
                b["refund"] = None
                bt_totals[b["type"]]["pending"] += 1
            continue
        for b in ex["bets"]:
            r = settle_bet(b, ri)
            b["refund"] = r
            ex["refund_yen"] += r
            t = bt_totals[b["type"]]
            t["stake"] += b["amount"]
            t["payout"] += r
            t["n"] += 1
            if r > 0:
                t["hits"] += 1
        total_stake_settled += ex["bet_yen"]
        total_payout += ex["refund_yen"]

    settled = [e for e in execs if not e["pending"]]
    profit = total_payout - total_stake_settled
    return {
        "date": today,
        "execs": execs, "n": len(execs),
        "n_settled": len(settled), "n_pending": n_pending,
        "n_hit": sum(1 for e in settled if e["refund_yen"] > 0),
        "stake": total_stake_settled, "payout": total_payout,
        "profit": profit,
        "roi": (total_payout / total_stake_settled * 100
                if total_stake_settled else 0.0),
        "by_bet_type": bt_totals,
        "stats": {
            "fns": [bt_totals["fns"]["hits"], bt_totals["fns"]["n"]],
            "sanren": [bt_totals["rt3"]["hits"] + bt_totals["rf3"]["hits"],
                       bt_totals["rt3"]["n"] + bt_totals["rf3"]["n"]],
        },
    }, n_failed


# =====================================================================
# 前日確定 + 累積 (bet_history.csv)
# =====================================================================

def confirmed_summary(target: str | None = None) -> dict | None:
    if not BET_HISTORY.exists():
        return None
    try:
        import pandas as pd
        df = pd.read_csv(BET_HISTORY)
        if df.empty:
            return None
        if target is not None:
            df = df[df["date"] == target]
            if df.empty:
                return None
        bet = int(df["bet_amount"].sum())
        refund = int(df["refund_amount"].sum())
        days = df["date"].nunique()
        daily = df.groupby("date").apply(
            lambda x: x["refund_amount"].sum() - x["bet_amount"].sum(),
            include_groups=False)
        return {"n": len(df), "bet": bet, "refund": refund,
                "profit": refund - bet,
                "roi": refund / bet * 100 if bet else 0.0,
                "days": days,
                "win_days": int((daily > 0).sum()),
                "first": str(df["date"].min()), "last": str(df["date"].max())}
    except Exception as e:
        logging.warning("bet_history 集計失敗: %s", e)
        return None


# =====================================================================
# 振り返り (Anthropic API、未設定時はテンプレ — boat と同じ 2 段構え)
# =====================================================================

def generate_reflection(today: dict, prev: dict | None, cum: dict | None) -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=ROOT / ".env", override=False)
    except Exception:
        pass
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return _template_reflection(today, prev, cum)
    try:
        return _anthropic_reflection(today, prev, cum, api_key)
    except Exception as e:
        logging.warning("Anthropic API error: %s", e)
        return _template_reflection(today, prev, cum) + f"\n(API error: {e})"


def _template_reflection(today: dict, prev: dict | None,
                         cum: dict | None) -> str:
    out = []
    if today["n"] == 0:
        out.append("本日は自動発注なし (候補が EV 閾値に届かず)。選別が正常に絞った日。")
    else:
        tone = "プラス" if today["profit"] >= 0 else "マイナス"
        out.append(
            f"本日 (暫定) {today['n_hit']}/{today['n_settled']}R 的中、"
            f"収支 ¥{today['profit']:+,} で{tone}。"
            + (f"結果待ち {today['n_pending']}R は明晩の確定で補正。"
               if today["n_pending"] else "")
        )
        for key, label in (("fns", "複勝"), ("sanren", "三連系")):
            hit, n = today["stats"][key]
            if n >= 3:
                rate = hit / n
                lo, hi = EXPECT_HIT[key]
                if rate < lo:
                    out.append(f"{label} {hit}/{n} ({rate*100:.0f}%) は期待レンジ"
                               f" {lo*100:.0f}-{hi*100:.0f}% を下振れ。"
                               f"数日続くなら EV 閾値と校正の点検を。")
                elif rate > hi:
                    out.append(f"{label} {hit}/{n} ({rate*100:.0f}%) は期待レンジ"
                               f"上振れ。翌朝の確定値で取りこぼしがないか確認。")
        # EV 乖離 (発火 vs 確定)
        drifts = [e for e in today["execs"]
                  if e.get("fire_ev") and e.get("final_ev")]
        crossed = [e for e in drifts
                   if (e["fire_ev"] >= FNS_EV_THR) != (e["final_ev"] >= FNS_EV_THR)]
        if crossed:
            out.append(f"発火EV→確定EV で閾値 {FNS_EV_THR} を跨いだレースが "
                       f"{len(crossed)}/{len(drifts)} 件。drift の既知問題、"
                       f"件数が常態化するなら発火時 EV の割引補正を検討。")
    if cum:
        if cum["roi"] >= 110:
            out.append(f"累積 ROI {cum['roi']:.1f}% でエッジ持続中。")
        elif cum["roi"] >= 100:
            out.append(f"累積 ROI {cum['roi']:.1f}% でぎりぎりプラス圏。")
        else:
            out.append(f"累積 ROI {cum['roi']:.1f}% (確定 {cum['n']}R)。"
                       f"控除率の壁の下、想定レンジ内かは週次で確認。")
    return "\n".join(out)


def _anthropic_reflection(today: dict, prev: dict | None, cum: dict | None,
                          api_key: str) -> str:
    import anthropic  # 遅延 import

    client = anthropic.Anthropic(api_key=api_key)
    races = []
    for e in today["execs"]:
        parts = [f"{e['venue_jp']} R{e['race_no']} {e['desc']}"]
        if e.get("fire_ev") is not None:
            parts.append(f"発火EV {e['fire_ev']:.2f}")
        if e.get("final_ev") is not None:
            parts.append(f"確定EV {e['final_ev']:.2f}")
        if e["pending"]:
            parts.append("結果待ち")
        else:
            parts.append(f"着順 {'-'.join(map(str, e['top3'])) if e['top3'] else '?'}")
            parts.append(f"払戻 ¥{e['refund_yen']:,}")
        races.append("- " + " / ".join(parts))

    prompt = f"""あなたは公営競技(オートレース)のデータ分析アシスタントです。
本日の自動購入結果を分析し、簡潔な振り返り(150-250字)を日本語で書いてください。

【本日 (暫定) {today['date']}】
- 発注: {today['n']}R / 的中 {today['n_hit']}/{today['n_settled']} / 結果待ち {today['n_pending']}
- 暫定収支: ¥{today['profit']:+,} (ROI {today['roi']:.1f}%)

【レース詳細】
{chr(10).join(races)}

【累積 (確定分)】
{f"- {cum['first']} 〜 {cum['last']} / {cum['n']}R / 収支 ¥{cum['profit']:+,} (ROI {cum['roi']:.1f}%)" if cum else "- なし"}

次の3点に触れてください:
1. 本日の結果の特徴 (当たり外れの傾向、EV乖離があれば言及)
2. 累積実績との比較
3. 次回への課題や注意点
絵文字は使わず、淡々と書いてください。"""

    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def persist_reflection(today_str: str, reflection: str, today: dict) -> Path:
    """logs/reflections/YYYY-MM.md に日付見出しで追記 (契約 §3、冪等)。"""
    REFLECTION_DIR.mkdir(parents=True, exist_ok=True)
    path = REFLECTION_DIR / f"{today_str[:7]}.md"
    heading = f"## {today_str}"
    if path.exists() and heading in path.read_text(encoding="utf-8"):
        logging.info("reflection 既存 (%s) — 追記スキップ (冪等)", heading)
        return path
    if not path.exists():
        path.write_text(
            f"# 夜次 反省ログ {today_str[:7]} (auto-racing-ai)\n\n"
            "夜次メール契約 v1 §3。正本はこのファイル "
            "(メール本文だけに存在する反省は禁止)。\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n{heading}\n")
        f.write(f"- 発注 {today['n']}R / 暫定 的中 {today['n_hit']}/"
                f"{today['n_settled']}R / 暫定収支 ¥{today['profit']:+,}\n")
        for ln in reflection.splitlines():
            if ln.strip():
                f.write(f"- {ln.strip()}\n")
    logging.info("reflection 追記: %s", path)
    return path


# =====================================================================
# レポート (MD + HTML)
# =====================================================================

def _bet_cell_label(b: dict) -> str:
    if b["type"] == "fns":
        return f"{b['cars'][0]}号"
    sep = "→" if b["type"] == "rt3" else "="
    return sep.join(map(str, b["cars"]))


def render_md(today: dict, n_failed: int, prev: dict | None, prev_date: str,
              cum: dict | None, reflection: str) -> str:
    lines = [f"# オートレース 夜次レポート {today['date']}", ""]
    lines.append("## 本日サマリ (暫定)")
    lines.append(f"- 発注: {today['n']}R" + (f" / 発注失敗 {n_failed}" if n_failed else ""))
    lines.append(f"- 的中: {today['n_hit']} / {today['n_settled']}R"
                 + (f" (結果待ち {today['n_pending']}R)" if today['n_pending'] else ""))
    lines.append(f"- 収支: ¥{today['profit']:+,} (暫定 ROI {today['roi']:.1f}%)")
    lines.append("- ※ 確定値は翌 02:30 取得、明晩のメールで補正")
    lines.append("")
    lines.append("## 券種別 内訳 (暫定)")
    for bt in ("fns", "rt3", "rf3"):
        v = today["by_bet_type"][bt]
        if v["n"] == 0 and v["pending"] == 0:
            continue
        roi = (v["payout"] / v["stake"] * 100) if v["stake"] else 0
        lines.append(f"- **{BET_JP[bt]}**: {v['hits']}/{v['n']} 的中, "
                     f"¥{v['stake']:,} → ¥{v['payout']:,} "
                     f"(¥{v['payout']-v['stake']:+,}, ROI {roi:.1f}%)"
                     + (f", 結果待ち {v['pending']}" if v["pending"] else ""))
    lines.append("")
    if prev:
        lines.append(f"## 前日確定 ({prev_date})")
        lines.append(f"- {prev['n']}R / ¥{prev['bet']:,} → ¥{prev['refund']:,} / "
                     f"収支 ¥{prev['profit']:+,} (ROI {prev['roi']:.1f}%)")
        lines.append("")
    if cum:
        lines.append("## 累積実績 (確定分)")
        lines.append(f"- 期間: {cum['first']} 〜 {cum['last']} ({cum['days']}日)")
        lines.append(f"- 通算: {cum['n']}R / 収支 ¥{cum['profit']:+,} "
                     f"(ROI {cum['roi']:.1f}%) / 勝日 {cum['win_days']}/{cum['days']}")
        lines.append("")
    lines.append("## 本日レース内訳")
    for e in today["execs"]:
        flag = "⏳" if e["pending"] else ("⭕" if e["refund_yen"] > 0 else "❌")
        lines.append(f"### {flag} {e['venue_jp']} R{e['race_no']}")
        lines.append(f"- 買い目: {e['desc']}")
        if e.get("pred_calib") is not None:
            ev_part = f"- ML確率 {e['pred_calib']:.2%} / 発火EV {e['fire_ev']:.2f}"
            if e.get("final_ev") is not None:
                ev_part += f" / 確定EV {e['final_ev']:.2f}"
            lines.append(ev_part)
        if e["top3"]:
            lines.append(f"- 着順: {'-'.join(map(str, e['top3']))}")
        if not e["pending"]:
            lines.append(f"- 払戻: ¥{e['refund_yen']:,}")
        lines.append("")
    lines.append("## 振り返り")
    lines.append(reflection)
    lines.append("")
    lines.append(f"(反省の正本: logs/reflections/{today['date'][:7]}.md)")
    return "\n".join(lines)


def _html_summary_card(today: dict, n_failed: int) -> list[str]:
    color = _GREEN if today["profit"] >= 0 else _RED
    h = [f'<div style="{_S_CARD}">',
         f'<h2 style="{_S_H2}margin-top:0">本日サマリ <span style="font-size:12px;'
         f'color:#666;font-weight:normal;">(暫定 — 確定は翌02:30、明晩補正)</span></h2>',
         f'<table style="{_S_TABLE}">',
         f'<tr><th style="{_S_TH}">項目</th><th style="{_S_TH}">値</th></tr>',
         f'<tr><td style="{_S_TD}">発注レース</td>'
         f'<td style="{_S_TD_NUM}">{today["n"]}'
         + (f' <span style="color:{_RED};">(失敗 {n_failed})</span>' if n_failed else '')
         + '</td></tr>',
         f'<tr><td style="{_S_TD}">的中 / 確定</td>'
         f'<td style="{_S_TD_NUM}">{today["n_hit"]} / {today["n_settled"]}'
         + (f' <span style="color:#e65100;">(結果待ち {today["n_pending"]})</span>'
            if today["n_pending"] else '')
         + '</td></tr>',
         f'<tr><td style="{_S_TD}">購入額 (確定分)</td>'
         f'<td style="{_S_TD_NUM}">¥{today["stake"]:,}</td></tr>',
         f'<tr><td style="{_S_TD}">払戻</td>'
         f'<td style="{_S_TD_NUM}">¥{today["payout"]:,}</td></tr>',
         f'<tr><td style="{_S_TD}">収支</td>'
         f'<td style="{_S_TD_NUM}color:{color};font-weight:bold;">'
         f'¥{today["profit"]:+,}</td></tr>',
         f'<tr><td style="{_S_TD}">暫定 ROI</td>'
         f'<td style="{_S_TD_NUM}color:{color};font-weight:bold;">'
         f'{today["roi"]:.1f}%</td></tr>',
         '</table></div>']
    return h


def _html_bet_type_section(today: dict) -> list[str]:
    h = [f'<h2 style="{_S_H2}">券種別 内訳 (暫定)</h2>',
         f'<table style="{_S_TABLE}">',
         f'<tr><th style="{_S_TH}">券種</th><th style="{_S_TH}">的中/件数</th>'
         f'<th style="{_S_TH}">購入</th><th style="{_S_TH}">払戻</th>'
         f'<th style="{_S_TH}">収支</th><th style="{_S_TH}">ROI</th>'
         f'<th style="{_S_TH}">待ち</th></tr>']
    for bt in ("fns", "rt3", "rf3"):
        v = today["by_bet_type"][bt]
        if v["n"] == 0 and v["pending"] == 0:
            continue
        roi = (v["payout"] / v["stake"] * 100) if v["stake"] else 0
        profit = v["payout"] - v["stake"]
        color = _GREEN if profit >= 0 else _RED
        h.append(f'<tr><td style="{_S_TD}">{BET_JP[bt]}</td>'
                 f'<td style="{_S_TD_NUM}">{v["hits"]}/{v["n"]}</td>'
                 f'<td style="{_S_TD_NUM}">¥{v["stake"]:,}</td>'
                 f'<td style="{_S_TD_NUM}">¥{v["payout"]:,}</td>'
                 f'<td style="{_S_TD_NUM}color:{color};font-weight:bold;">'
                 f'¥{profit:+,}</td>'
                 f'<td style="{_S_TD_NUM}color:{color};">{roi:.1f}%</td>'
                 f'<td style="{_S_TD_NUM}">{v["pending"] or "—"}</td></tr>')
    h.append('</table>')
    return h


def _html_confirmed_section(prev: dict | None, prev_date: str,
                            cum: dict | None) -> list[str]:
    h = [f'<div style="{_S_CARD}">',
         f'<h2 style="{_S_H2}margin-top:0">確定実績 (bet_history)</h2>',
         f'<table style="{_S_TABLE}">',
         f'<tr><th style="{_S_TH}">区分</th><th style="{_S_TH}">R</th>'
         f'<th style="{_S_TH}">収支</th><th style="{_S_TH}">ROI</th></tr>']
    if prev:
        c = _GREEN if prev["profit"] >= 0 else _RED
        h.append(f'<tr><td style="{_S_TD}">前日確定 ({prev_date})</td>'
                 f'<td style="{_S_TD_NUM}">{prev["n"]}</td>'
                 f'<td style="{_S_TD_NUM}color:{c};font-weight:bold;">'
                 f'¥{prev["profit"]:+,}</td>'
                 f'<td style="{_S_TD_NUM}">{prev["roi"]:.1f}%</td></tr>')
    if cum:
        c = _GREEN if cum["profit"] >= 0 else _RED
        h.append(f'<tr><td style="{_S_TD}">累積 {cum["first"]}〜 '
                 f'({cum["days"]}日, 勝日{cum["win_days"]})</td>'
                 f'<td style="{_S_TD_NUM}">{cum["n"]}</td>'
                 f'<td style="{_S_TD_NUM}color:{c};font-weight:bold;">'
                 f'¥{cum["profit"]:+,}</td>'
                 f'<td style="{_S_TD_NUM}">{cum["roi"]:.1f}%</td></tr>')
    h.append('</table></div>')
    return h


def _html_ev_drift_section(today: dict) -> list[str]:
    rows = [e for e in today["execs"]
            if e.get("fire_ev") is not None and e.get("final_ev") is not None]
    if not rows:
        return []
    h = [f'<h2 style="{_S_H2}">発火EV vs 確定EV 乖離</h2>',
         '<p style="font-size:12px;color:#666;">発火 (-4分) のオッズで EV 判定 → '
         '締切までの late money で確定オッズが変わると判定が逆転しうる '
         '(auto 既知の drift 問題の日次モニタ)。</p>',
         f'<table style="{_S_TABLE}">',
         f'<tr><th style="{_S_TH}">場</th><th style="{_S_TH}">R</th>'
         f'<th style="{_S_TH}">発火EV</th><th style="{_S_TH}">確定EV</th>'
         f'<th style="{_S_TH}">差</th><th style="{_S_TH}">判定</th></tr>']
    n_crossed = 0
    for e in rows:
        fe, le = e["final_ev"], e["fire_ev"]
        crossed = (le >= FNS_EV_THR) != (fe >= FNS_EV_THR)
        if crossed:
            n_crossed += 1
        flag, color = ("⚠️ 跨ぐ", _RED) if crossed else ("OK", _GREEN)
        h.append(f'<tr><td style="{_S_TD}">{e["venue_jp"]}</td>'
                 f'<td style="{_S_TD_NUM}">{e["race_no"]}</td>'
                 f'<td style="{_S_TD_NUM}">{le:.2f}</td>'
                 f'<td style="{_S_TD_NUM}">{fe:.2f}</td>'
                 f'<td style="{_S_TD_NUM}">{fe-le:+.2f}</td>'
                 f'<td style="{_S_TD}color:{color};">{flag}</td></tr>')
    h.append('</table>')
    if n_crossed:
        h.append(f'<p style="color:{_RED};font-size:12px;">⚠️ {len(rows)} レース中 '
                 f'<b>{n_crossed}</b> 件で閾値 {FNS_EV_THR} を跨ぐ乖離。要監視。</p>')
    else:
        h.append(f'<p style="color:{_GREEN};font-size:12px;">'
                 f'✅ 閾値を跨ぐ乖離なし。</p>')
    return h


def _html_race_table(today: dict) -> list[str]:
    h = [f'<h2 style="{_S_H2}">本日レース内訳</h2>',
         f'<table style="{_S_TABLE}">',
         f'<tr><th style="{_S_TH}">場</th><th style="{_S_TH}">R</th>'
         f'<th style="{_S_TH}">ML確率/EV</th><th style="{_S_TH}">着順</th>'
         f'<th style="{_S_TH}">複勝</th><th style="{_S_TH}">三連単</th>'
         f'<th style="{_S_TH}">三連複</th></tr>']

    def cell(ex: dict, bt: str) -> str:
        b = next((x for x in ex["bets"] if x["type"] == bt), None)
        if b is None:
            return f'<td style="{_S_TD}color:#999;">—</td>'
        label = _bet_cell_label(b)
        if ex["pending"]:
            return (f'<td style="{_S_TD}color:#e65100;font-size:11px;">'
                    f'⏳ {label}</td>')
        hit = (b.get("refund") or 0) > 0
        icon, color = ("⭕", _GREEN) if hit else ("❌", _RED)
        return (f'<td style="{_S_TD}color:{color};font-size:11px;">'
                f'{icon} {label}<br>¥{b.get("refund") or 0:,}</td>')

    for e in today["execs"]:
        if e.get("pred_calib") is not None:
            ml = f'{e["pred_calib"]:.0%} / {e["fire_ev"]:.2f}'
        else:
            ml = "—"
        top3 = "-".join(map(str, e["top3"])) if e["top3"] else ("⏳" if e["pending"] else "?")
        h.append(f'<tr><td style="{_S_TD}">{e["venue_jp"]}</td>'
                 f'<td style="{_S_TD_NUM}">{e["race_no"]}</td>'
                 f'<td style="{_S_TD_NUM}font-size:11px;">{ml}</td>'
                 f'<td style="{_S_TD_NUM}">{top3}</td>'
                 f'{cell(e, "fns")}{cell(e, "rt3")}{cell(e, "rf3")}</tr>')
    h.append('</table>')
    return h


def _html_reflection_section(reflection: str) -> list[str]:
    return [f'<h2 style="{_S_H2}">振り返り</h2>',
            '<div style="background:#fff8e1;padding:12px 16px;border-radius:6px;'
            'border-left:4px solid #fbc02d;white-space:pre-wrap;">',
            reflection,
            '</div>',
            '<p style="color:#999;font-size:11px;">反省の正本: '
            'logs/reflections/YYYY-MM.md</p>']


def render_html(today: dict, n_failed: int, prev: dict | None, prev_date: str,
                cum: dict | None, reflection: str) -> str:
    flag = "🟢" if today["roi"] >= 100 else "🔴"
    h = [f'<div style="{_S_BODY}">',
         f'<h1>🌙 オートレース 夜次レポート {today["date"]} {flag}</h1>']
    h += _html_summary_card(today, n_failed)
    h += _html_bet_type_section(today)
    h += _html_confirmed_section(prev, prev_date, cum)
    h += _html_ev_drift_section(today)
    h += _html_race_table(today)
    h += _html_reflection_section(reflection)
    h.append('<hr><p style="color:#999;font-size:11px">'
             'auto-racing-ai night report</p>')
    h.append('</div>')
    return "\n".join(h)


# =====================================================================
# main
# =====================================================================

def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (既定: 今日)")
    ap.add_argument("--dry-run", action="store_true",
                    help="送信も保存もせず本文表示")
    ap.add_argument("--no-email", action="store_true",
                    help="MD/反省は保存するが送信しない")
    args = ap.parse_args()

    if args.date:
        today_str = args.date
    else:
        now = dt.datetime.now()
        # 00:00 発火 (正午前の実行) は「直前に終わった開催日 = 前日」を報告する
        target = now.date() - dt.timedelta(days=1) if now.hour < 12 else now.date()
        today_str = target.isoformat()
    prev_date = (dt.date.fromisoformat(today_str) - dt.timedelta(days=1)).isoformat()
    logging.info("=== night_report start: %s ===", today_str)

    today, n_failed = evaluate_today(today_str)
    prev = confirmed_summary(prev_date)
    cum = confirmed_summary(None)

    if today["n"] == 0 and prev is None:
        logging.info("当日発注なし・前日確定なし — メールなし (契約 §1 非開催)")
        return 0

    reflection = generate_reflection(today, prev, cum)
    md = render_md(today, n_failed, prev, prev_date, cum, reflection)
    html = render_html(today, n_failed, prev, prev_date, cum, reflection)

    flag = "🟢" if today["roi"] >= 100 else "🔴"
    pending_tag = f" ⏳残{today['n_pending']}" if today["n_pending"] else ""
    failed_tag = f" ⚠️失敗{n_failed}" if n_failed else ""
    subject = (f"[autorace] 🌙 夜次 {today_str} {flag} "
               f"暫定ROI={today['roi']:.0f}% "
               f"({today['n_hit']}/{today['n_settled']}) "
               f"¥{today['profit']:+,}{pending_tag}{failed_tag}")

    if args.dry_run:
        print("--- DRY RUN ---")
        print(subject)
        print()
        print(md)
        (DATA / "_night_preview.html").write_text(html, encoding="utf-8")
        print("\n(html preview: data/_night_preview.html)")
        return 0

    # 永続化: フルレポート MD (boat 流儀) + 反省の正本 (契約 §3)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORT_DIR / f"night_{today_str}.md"
    md_path.write_text(md, encoding="utf-8")
    logging.info("saved MD: %s", md_path)
    persist_reflection(today_str, reflection, today)

    if args.no_email:
        return 0
    try:
        from gmail_notify import send_email
        send_email(subject=subject, body=md, html=html)  # [mail] sent マーカー
        logging.info("night mail 送信完了: %s", subject)
    except Exception as e:
        logging.error("night mail 送信失敗: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
