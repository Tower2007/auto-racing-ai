"""夜次メール + 反省ログ (2026-08-07 導入、夜次メール契約 v1 対応)。

契約: Public-Race-Management-System/docs/nightly_mail_reflection_spec.md
ハンドオフ: 同 docs/patches/night_mail_auto_banei_handoff.md (Auto 節)
モデル: boat-racing-ai の night_report.py (集計→振り返り→メール→MD保存)

2 段構え (boat と同様):
  - 当日分は auto_buy_state.json (当日の実発注) + RaceRefund API で **暫定** 集計。
    未確定レース (ミッドナイト等) は「結果待ち」表示。
  - 確定値は翌 02:30 の AutoraceFetchOrderHistory が bet_history.csv に書くため、
    前日分の **確定** セクションを翌晩のメールに載せて補正する。

契約対応:
  §1 開催日の夜に結果サマリをメール (発注ゼロの日は送らない)
  §2 振り返り 1 節 (テンプレ生成: 的中率/収支/期待との乖離を機械的に 1-3 行)
  §3 反省を logs/reflections/YYYY-MM.md に日付見出しで永続化 (メール本文だけ禁止)
  §4 [mail] sent マーカー (gmail_notify が出力、data/night_report.log に残る)

使い方:
  python night_report.py             # 集計 + 反省MD追記 + メール送信
  python night_report.py --dry-run   # 送信せず本文を表示 (反省MDにも書かない)
タスク: AutoraceNightReport 毎日 22:00 (scripts/run_night_report_hidden.vbs 経由)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
STATE_FILE = DATA / "auto_buy_state.json"
BET_HISTORY = DATA / "bet_history.csv"
BET_DETAIL = DATA / "bet_history_detail.csv"
REFLECTION_DIR = ROOT / "logs" / "reflections"
LOG_FILE = DATA / "night_report.log"

VENUE_PC = {"kawaguchi": 2, "isesaki": 3, "hamamatsu": 4, "iizuka": 5,
            "sanyou": 6}
VENUE_JP = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}

# 的中率の期待レンジ (乖離コメント用、実績ベースの目安)
EXPECT_HIT = {"fns": (0.55, 0.80), "sanren": (0.05, 0.35)}


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


# ─── 当日 (暫定): auto_buy_state + RaceRefund API ────────────────────────

def parse_bets_desc(desc: str) -> list[dict]:
    """auto_buy の日本語 bets 表記を構造化する。

    format_bets_jp の逆変換 (表記は auto_buy が機械生成するため決定的):
      '複勝 8号 ¥100' / '三連単 5→3→6 ¥100' / '三連複 3=5=6 ¥100'
    """
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
    """当日の実発注 (verdict=executed) と失敗数を返す。"""
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


def fetch_refunds(pcs: set[int], today: str) -> dict[tuple[int, int], dict]:
    """{(pc, race_no): refundInfo} を返す。API 失敗場は欠損 (結果待ち扱い)。"""
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
    """1 bet の払戻額 (¥)。外れ 0。"""
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


def settle_today(execs: list[dict], refunds: dict) -> dict:
    """当日実発注の暫定精算。レース単位の的中/払戻/結果待ちを付与して集計。"""
    total_bet = total_refund = 0
    n_pending = 0
    stats = {"fns": [0, 0], "sanren": [0, 0]}  # [hit, n]
    for ex in execs:
        ri = refunds.get((ex["pc"], ex["race_no"]))
        ex["pending"] = ri is None
        ex["bet_yen"] = sum(b["amount"] for b in ex["bets"])
        ex["refund_yen"] = 0
        total_bet += ex["bet_yen"]
        if ri is None:
            n_pending += 1
            continue
        for b in ex["bets"]:
            r = settle_bet(b, ri)
            b["refund"] = r
            ex["refund_yen"] += r
            key = "fns" if b["type"] == "fns" else "sanren"
            stats[key][1] += 1
            if r > 0:
                stats[key][0] += 1
        total_refund += ex["refund_yen"]
    settled = [e for e in execs if not e["pending"]]
    return {
        "execs": execs, "n": len(execs), "n_settled": len(settled),
        "n_pending": n_pending,
        "n_hit": sum(1 for e in settled if e["refund_yen"] > 0),
        "bet": total_bet,
        "refund": total_refund,
        "profit": total_refund - sum(e["bet_yen"] for e in settled),
        "stats": stats,
    }


# ─── 前日 (確定) + 累計: bet_history.csv ─────────────────────────────────

def confirmed_summary(target: str | None = None) -> dict | None:
    """bet_history.csv から target 日 (None なら全期間) の確定集計。"""
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
        return {"n": len(df), "bet": bet, "refund": refund,
                "profit": refund - bet,
                "roi": refund / bet * 100 if bet else 0.0}
    except Exception as e:
        logging.warning("bet_history 集計失敗: %s", e)
        return None


# ─── 振り返り (テンプレ、機械的 1-3 行) ──────────────────────────────────

def build_reflection(today: str, prov: dict, prev: dict | None) -> list[str]:
    lines = []
    if prov["n"] > 0:
        lines.append(
            f"発注 {prov['n']}R / 暫定 的中 {prov['n_hit']}/{prov['n_settled']}R"
            + (f" (結果待ち {prov['n_pending']}R)" if prov["n_pending"] else "")
            + f" / 暫定収支 ¥{prov['profit']:+,}"
        )
        # 的中率の乖離コメント (n が小さすぎる時は判定しない)
        for key, label in (("fns", "複勝"), ("sanren", "三連系")):
            hit, n = prov["stats"][key]
            if n >= 3:
                rate = hit / n
                lo, hi = EXPECT_HIT[key]
                if rate < lo:
                    lines.append(f"{label} {hit}/{n} ({rate*100:.0f}%) は期待レンジ"
                                 f" {lo*100:.0f}-{hi*100:.0f}% を下振れ。"
                                 f"数日続くなら EV 閾値と校正の点検を")
                elif rate > hi:
                    lines.append(f"{label} {hit}/{n} ({rate*100:.0f}%) は期待レンジ"
                                 f"上振れ。オッズ取りこぼしがないか翌朝確定値で確認")
    if prev is not None:
        lines.append(
            f"前日確定: {prev['n']}R 収支 ¥{prev['profit']:+,} "
            f"(ROI {prev['roi']:.0f}%)。暫定→確定の乖離が大きければ"
            f" RaceRefund 精算ロジックを疑う"
        )
    if not lines:
        lines.append("発注なし (候補が EV 閾値に届かず)。非開催 or 選別が正常に絞った日")
    return lines[:3]


def persist_reflection(today: str, lines: list[str], prov: dict) -> Path:
    """logs/reflections/YYYY-MM.md に日付見出しで追記 (契約 §3)。冪等。"""
    REFLECTION_DIR.mkdir(parents=True, exist_ok=True)
    path = REFLECTION_DIR / f"{today[:7]}.md"
    heading = f"## {today}"
    if path.exists() and heading in path.read_text(encoding="utf-8"):
        logging.info("reflection 既存 (%s) — 追記スキップ (冪等)", heading)
        return path
    if not path.exists():
        path.write_text(
            f"# 夜次 反省ログ {today[:7]} (auto-racing-ai)\n\n"
            "夜次メール契約 v1 §3。正本はこのファイル "
            "(メール本文だけに存在する反省は禁止)。\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n{heading}\n")
        # lines[0] が既に集計行 (発注/的中/収支) を含むため、ここでは重複させない
        for ln in lines:
            f.write(f"- {ln}\n")
    logging.info("reflection 追記: %s", path)
    return path


# ─── 本文 ────────────────────────────────────────────────────────────────

def render_mail(today: str, prov: dict, n_failed: int,
                prev: dict | None, prev_date: str,
                alltime: dict | None, reflection: list[str]) -> tuple[str, str]:
    lines = [f"🌙 auto-racing-ai 夜次レポート ({today})", "=" * 56, ""]

    lines.append(f"【当日 (暫定)】 発注 {prov['n']}R"
                 + (f" / 発注失敗 {n_failed}" if n_failed else ""))
    for ex in prov["execs"]:
        if ex["pending"]:
            res = "⏳ 結果待ち"
        elif ex["refund_yen"] > 0:
            res = f"🎯 的中 ¥{ex['refund_yen']:,}"
        else:
            res = "✗"
        lines.append(f"  {ex['venue_jp']} R{ex['race_no']:<2d} {ex['desc']}  → {res}")
    if prov["n"]:
        lines.append(f"  暫定計: 投資 ¥{prov['bet']:,} / 払戻 ¥{prov['refund']:,}"
                     f" / 確定分収支 ¥{prov['profit']:+,}"
                     + (f" / 結果待ち {prov['n_pending']}R" if prov["n_pending"] else ""))
        lines.append("  ※ 確定値は翌 02:30 取得。乖離があれば明晩のメールで補正")
    lines.append("")

    if prev is not None:
        lines.append(f"【前日確定 ({prev_date})】 {prev['n']}R / "
                     f"投資 ¥{prev['bet']:,} / 払戻 ¥{prev['refund']:,} / "
                     f"収支 ¥{prev['profit']:+,} / ROI {prev['roi']:.1f}%")
        lines.append("")

    if alltime is not None:
        lines.append(f"【累計 (確定分)】 {alltime['n']}R / "
                     f"収支 ¥{alltime['profit']:+,} / ROI {alltime['roi']:.1f}%")
        lines.append("")

    lines.append("【🪞 振り返り】")
    for ln in reflection:
        lines.append(f"  - {ln}")
    lines.append("")
    lines.append(f"(反省の正本: logs/reflections/{today[:7]}.md)")

    mark = "🎯" if prov["n_hit"] > 0 else ("⏳" if prov["n_pending"] else "✗")
    subject = (f"[autorace] 🌙 夜次 {today} {mark} 発注{prov['n']}R "
               f"的中{prov['n_hit']} 暫定収支 ¥{prov['profit']:+,}")
    return subject, "\n".join(lines)


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="送信せず本文表示 (反省MDにも書かない)")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (既定: 今日)")
    args = ap.parse_args()

    today = args.date or dt.date.today().isoformat()
    prev_date = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
    logging.info("=== night_report start: %s ===", today)

    execs, n_failed = load_today_executions(today)
    refunds = fetch_refunds({e["pc"] for e in execs}, today) if execs else {}
    prov = settle_today(execs, refunds)
    prev = confirmed_summary(prev_date)
    alltime = confirmed_summary(None)

    if prov["n"] == 0 and prev is None:
        logging.info("当日発注なし・前日確定なし — メールなし (契約 §1 非開催)")
        return 0

    reflection = build_reflection(today, prov, prev)
    subject, body = render_mail(today, prov, n_failed, prev, prev_date,
                                alltime, reflection)

    if args.dry_run:
        print("--- DRY RUN ---")
        print(subject)
        print()
        print(body)
        return 0

    persist_reflection(today, reflection, prov)
    try:
        from gmail_notify import send_email
        send_email(subject=subject, body=body)   # [mail] sent マーカーを出力
        logging.info("night mail 送信完了: %s", subject)
    except Exception as e:
        logging.error("night mail 送信失敗: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
