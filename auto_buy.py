"""夜間限定 自動投票 Phase 1 (2026-05-31 導入)。

寝てる時間帯 (山陽ミッドナイト + 川口ナイター後半) の機会損失対策として、
厳格ガード付きで daily_predict の買い候補を自動投票する。

設計方針 (Opinion/codex_briefs/2026-05-31_auto_buy_phase1.md):
  - マスタースイッチ AUTO_BUY_ENABLED (デフォルト OFF)
  - AUTO_BUY_DRY_RUN=True の間は実投票せず log + state 更新のみ
  - 夜間 (AUTO_BUY_HOURS) のみ、1 日上限 / 当日損失停止 / EV 異常 /
    連続失敗 の 5 ガードを全て満たした R のみ自動投票
  - 既存 execute_purchase.py を subprocess で再利用 (--bets-json)
  - 毎回 Gmail 即時通知 (ユーザーの唯一の monitoring 手段)

ToS: 約定書 第13条「自ら申込む」のグレー解釈をユーザー承知の上で進行
     (本人 PC・本人 cookie・本人スクリプト)。凍結リスクはゼロでない。

AUTO_BUY_ENABLED=False の時は本モジュールは何もしない (従来 click-to-buy のまま)。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("auto_buy")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = DATA / "auto_buy_state.json"
EXECUTE_SCRIPT = ROOT / "scripts" / "execute_purchase.py"
BET_HISTORY_CSV = DATA / "bet_history.csv"
JST = dt.timezone(dt.timedelta(hours=9))


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    try:
        return float(v) if v is not None else default
    except ValueError:
        return default


def _load_env() -> None:
    """.env を読み込む (dotenv があれば)。"""
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=ROOT / ".env", override=False)
    except Exception:
        pass


_load_env()

# ─── 設定 (env で上書き可、デフォルトはブリーフ準拠) ───────────────
AUTO_BUY_ENABLED = _env_bool("AUTO_BUY_ENABLED", False)
AUTO_BUY_DRY_RUN = _env_bool("AUTO_BUY_DRY_RUN", True)
# 三連系 (rt3+rf3) も自動対象にするか。Week2 は複勝のみ (False)、Week3 で True。
AUTO_BUY_INCLUDE_RT3 = _env_bool("AUTO_BUY_INCLUDE_RT3", False)
# 複勝 (fns) を自動投票に含めるか。2026-06-26: 複勝は実弾 ROI 93.6% で控除率の壁を
# 越えず累計を削る主因と判明したため **デフォルト OFF (三連系一本)**。
# 復活させる場合は env AUTO_BUY_INCLUDE_FNS=1。経緯: memory project_decisions.md 2026-06-26。
AUTO_BUY_INCLUDE_FNS = _env_bool("AUTO_BUY_INCLUDE_FNS", False)
# 時間帯ガードを無視して常時自動発注するか (2026-05-31 ユーザー要望でデフォ True)。
# False にすると下の AUTO_BUY_HOUR_START/END の夜間限定に戻る。
AUTO_BUY_ANYTIME = _env_bool("AUTO_BUY_ANYTIME", True)
# 自動投票を許可する時間帯 (JST hour)。(22, 6) = 22:00〜翌06:00。
# AUTO_BUY_ANYTIME=False の時のみ有効。
AUTO_BUY_HOUR_START = _env_int("AUTO_BUY_HOUR_START", 22)
AUTO_BUY_HOUR_END = _env_int("AUTO_BUY_HOUR_END", 6)
MAX_DAILY_AUTO_YEN = _env_int("MAX_DAILY_AUTO_YEN", 3000)
DAILY_LOSS_STOP_YEN = _env_int("DAILY_LOSS_STOP_YEN", -3000)
EV_ANOMALY_CAP = _env_float("EV_ANOMALY_CAP", 10.0)
CONSECUTIVE_FAILURES_STOP = _env_int("CONSECUTIVE_FAILURES_STOP", 3)
# 残高 (ポイント+払戻金 合計) がこれ以下になったら警告メール (1 日 1 回)
AUTO_BUY_LOW_BALANCE_YEN = _env_int("AUTO_BUY_LOW_BALANCE_YEN", 3000)


# ─── 時刻 / state ─────────────────────────────────────────────

def now_jst() -> dt.datetime:
    return dt.datetime.now(JST)


def in_buy_hours(now: dt.datetime,
                 start: int = AUTO_BUY_HOUR_START,
                 end: int = AUTO_BUY_HOUR_END) -> bool:
    """now (JST) が自動投票許可時間帯か。start>end は日跨ぎ (22..6)。"""
    h = now.hour
    if start == end:
        return True
    if start < end:
        return start <= h < end
    # 日跨ぎ: h>=start or h<end
    return h >= start or h < end


def _empty_state(date_str: str) -> dict:
    return {
        "date": date_str,
        "spent_yen": 0,
        "profit_yen": 0,
        "consecutive_failures": 0,
        "executions": [],
    }


def load_state(now: dt.datetime | None = None) -> dict:
    """auto_buy_state.json を読み込む。日付が変わっていれば reset。"""
    now = now or now_jst()
    today = now.date().isoformat()
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if st.get("date") == today:
                return st
        except Exception as e:
            logger.warning("auto_buy_state 読み込み失敗、reset: %s", e)
    return _empty_state(today)


def save_state(state: dict) -> None:
    """atomic write (tempfile + os.replace)。"""
    DATA.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(DATA), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def today_profit_from_history(today: str) -> int:
    """bet_history.csv から当日の累積損益を best-effort 集計。

    レース結果取得は翌 02:30 のため intraday は 0 / 部分的になりうる
    (安全ベルト用途、主ガードは spent 上限)。
    """
    if not BET_HISTORY_CSV.exists():
        return 0
    try:
        import csv
        total = 0
        with BET_HISTORY_CSV.open(encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if not row or row[0] == "date":
                    continue
                if row[0] == today:
                    try:
                        total += int(float(row[-1]))  # profit (最終列)
                    except (ValueError, IndexError):
                        continue
        return total
    except Exception as e:
        logger.warning("today_profit 集計失敗: %s", e)
        return 0


# ─── ガード (純粋関数、tests から直接呼ぶ) ────────────────────────

def check_guards(
    state: dict,
    now: dt.datetime,
    race_amount: int,
    ev: float,
    *,
    anytime: bool = AUTO_BUY_ANYTIME,
    hour_start: int = AUTO_BUY_HOUR_START,
    hour_end: int = AUTO_BUY_HOUR_END,
    max_daily_yen: int = MAX_DAILY_AUTO_YEN,
    loss_stop_yen: int = DAILY_LOSS_STOP_YEN,
    ev_cap: float = EV_ANOMALY_CAP,
    consecutive_stop: int = CONSECUTIVE_FAILURES_STOP,
) -> tuple[bool, str]:
    """ガードを順に評価。全通過で (True, "ok")、不可なら (False, 理由)。

    anytime=True (デフォルト) の時は時間帯ガードを無視 (常時発注)。
    """
    if not anytime and not in_buy_hours(now, hour_start, hour_end):
        return False, f"skip_hours (JST {now.hour:02d}時、許可 {hour_start}-{hour_end})"
    if ev > ev_cap:
        return False, f"skip_ev_anomaly (EV {ev:.2f} > {ev_cap})"
    if state.get("spent_yen", 0) + race_amount > max_daily_yen:
        return False, (f"skip_daily_cap (既出 {state.get('spent_yen',0)} + "
                       f"{race_amount} > {max_daily_yen})")
    if state.get("profit_yen", 0) <= loss_stop_yen:
        return False, (f"skip_loss_stop (当日損益 {state.get('profit_yen',0)} "
                       f"<= {loss_stop_yen})")
    if state.get("consecutive_failures", 0) >= consecutive_stop:
        return False, (f"skip_failures (連続失敗 "
                       f"{state.get('consecutive_failures',0)} >= {consecutive_stop})")
    return True, "ok"


# ─── bets 構築 ────────────────────────────────────────────────

_BET_JP = {"fns": "複勝", "rt3": "三連単", "rf3": "三連複"}
_BET_JP_SEP = {"fns": "", "rt3": "→", "rf3": "="}


def format_bets_jp(bets: list[dict]) -> str:
    """通知用の読みやすい買い目表記。

    例: '複勝 5号 ¥300 / 三連単 5→6→7 ¥100 / 三連複 5=6=7 ¥100'
    """
    parts = []
    for b in bets:
        bt = b["type"]
        cars = [int(c) for c in b["cars"]]
        if bt == "fns":
            deme = f"{cars[0]}号"
        else:
            deme = _BET_JP_SEP[bt].join(str(c) for c in cars)
        parts.append(f"{_BET_JP.get(bt, bt)} {deme} ¥{int(b['amount'])}")
    return " / ".join(parts)


def build_bets(car_no: int, rec_yen: int,
               rt3_ref: dict | None,
               include_rt3: bool = AUTO_BUY_INCLUDE_RT3,
               include_fns: bool = AUTO_BUY_INCLUDE_FNS) -> list[dict]:
    """自動投票用の bets list を構築。

    include_fns(デフォルト OFF, 2026-06-26〜): 複勝 (推奨額) を含める。
    include_rt3 かつ rt3_ref があれば三連系を追加。
    rt3_ref["has_rt3"]=True(浜松・山陽): 三連単+三連複 を追加。
    rt3_ref["has_rt3"]=False(伊勢崎・飯塚): 三連複のみ追加。

    複勝 OFF かつ三連系対象外のレースでは **空 list** を返す
    (呼び出し側は空なら投票スキップ)。
    """
    bets: list[dict] = []
    if include_fns:
        bets.append({"type": "fns", "cars": [int(car_no)], "amount": int(rec_yen)})
    if include_rt3 and rt3_ref:
        cars_ord = [int(c) for c in rt3_ref["cars_ordered"]]
        cars_srt = [int(c) for c in rt3_ref["cars_sorted"]]
        if rt3_ref.get("has_rt3", True):  # 浜松・山陽: 三連単も追加
            bets.append({"type": "rt3", "cars": cars_ord, "amount": 100})
        bets.append({"type": "rf3", "cars": cars_srt, "amount": 100})
    return bets


# ─── 実行 ─────────────────────────────────────────────────────

def _notify(subject: str, body: str) -> None:
    """Gmail 即時通知。AUTO_BUY_NOTIFY_TO 優先、無ければ MAIL_TO。

    件名は他の通知と揃えて [autorace] を前置する。
    """
    try:
        from gmail_notify import send_email
        to = os.environ.get("AUTO_BUY_NOTIFY_TO", "").strip()
        recipients = [a.strip() for a in to.split(",") if a.strip()] or None
        if not subject.startswith("[autorace]"):
            subject = f"[autorace] {subject}"
        send_email(subject=subject, body=body, recipients=recipients)
    except Exception as e:
        logger.error("auto_buy Gmail 通知失敗: %s", e)


def _run_execute_purchase(race_date: str, place_code: int, race_no: int,
                          bets: list[dict]) -> tuple[bool, str, dict | None]:
    """execute_purchase.py を subprocess 実行 (実投票)。

    返り値: (success, detail, balance)
      balance = {"points","cash","total"} or None (確認画面から抽出)
    """
    cmd = [
        sys.executable, str(EXECUTE_SCRIPT),
        "--race-date", race_date,
        "--place", str(place_code),
        "--race", str(race_no),
        "--bets-json", json.dumps(bets, ensure_ascii=False),
    ]
    try:
        r = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout >180s"

    # execute_purchase は stdout に結果 JSON を indent=2 で複数行出力する。
    # → 最終行 ('}') だけでなく stdout 全体 / 最後の {...} ブロックを解釈する。
    out = (r.stdout or "").strip()
    parsed = None
    if out:
        try:
            parsed = json.loads(out)
        except Exception:
            i, j = out.find("{"), out.rfind("}")
            if i != -1 and j > i:
                try:
                    parsed = json.loads(out[i:j + 1])
                except Exception:
                    parsed = None
    # detail: 成否メッセージ (parse 出来れば message/error、無ければ末尾)
    if parsed is not None:
        detail = str(parsed.get("message") or parsed.get("error") or parsed)[:300]
    else:
        detail = (out or (r.stderr or ""))[-300:]

    # 残高 (execute_purchase が確認画面から抽出して返す)
    balance = parsed.get("balance") if isinstance(parsed, dict) else None

    # returncode を主判定にする (0=happy path 完走=success:true、
    # 1=例外/検証失敗、2=引数不正)。parsed.success は sanity check。
    if r.returncode == 0:
        if parsed is not None and parsed.get("success") is False:
            return False, detail, balance
        return True, detail, balance
    return False, detail, balance


def _maybe_low_balance_alert(state: dict, balance: dict | None) -> None:
    """残高 (ポイント+払戻金 合計) が AUTO_BUY_LOW_BALANCE_YEN 以下なら
    警告メール。1 日 1 回まで (state["low_balance_alerted"] で重複抑止、
    state は日次 reset)。"""
    if not balance:
        return
    total = balance.get("total")
    if total is None:
        return
    # 最新残高を state に記録 (週次等で参照可)
    state["last_balance"] = balance
    if total > AUTO_BUY_LOW_BALANCE_YEN:
        return
    if state.get("low_balance_alerted"):
        return
    state["low_balance_alerted"] = True
    pts = balance.get("points")
    cash = balance.get("cash")
    _notify(
        f"[AUTO-BUY] ⚠️ 残高警告 合計¥{total:,} (≤¥{AUTO_BUY_LOW_BALANCE_YEN:,})",
        f"【残高低下警告】\n\n"
        f"投票残高 (ポイント+払戻金) が ¥{total:,} まで低下しました。\n"
        f"  ポイント残高: {pts if pts is not None else '?'} pt\n"
        f"  払戻金残高:   ¥{cash if cash is not None else '?'}\n\n"
        f"しきい値: ¥{AUTO_BUY_LOW_BALANCE_YEN:,}\n"
        f"自動投票を続けるにはチャージを検討してください。\n"
        f"(この警告は本日分は1回のみ。明日また低ければ再通知)")


LOCK_FILE = DATA / "auto_buy.lock"
LOCK_STALE_SEC = 600          # 10 分超の残置ロックはクラッシュ残骸とみなし破棄
LOCK_WAIT_SEC = 90            # 取得待ちの上限 (先行プロセスの発注は最長 ~60s)
LOCK_POLL_SEC = 2.0


def _acquire_lock() -> bool:
    """プロセス間ロックを取得 (O_CREAT|O_EXCL の原子作成)。

    2026-07-11 監査 P1-2: state にロックがなく、複数の AutoraceDyn_* が同時発火
    すると日次 cap (spent_yen) を二重に通過し得る。全履歴で同時発火は 0 回だが、
    安価に最悪ケースを塞ぐ。LOCK_WAIT_SEC 待っても取れなければ False
    (呼び出し側は発注せずスキップ = cap 破りより機会損失を選ぶ)。
    """
    deadline = time.monotonic() + LOCK_WAIT_SEC
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {dt.datetime.now().isoformat()}")
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
                if age > LOCK_STALE_SEC:
                    logger.warning("[auto_buy] stale lock (%ds) を破棄", int(age))
                    LOCK_FILE.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL_SEC)
        except OSError as e:
            logger.warning("[auto_buy] lock 作成失敗 (継続不能): %s", e)
            return False


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def run_auto_buy(candidates: list[dict],
                 now: dt.datetime | None = None,
                 dry_run: bool | None = None) -> list[dict]:
    """候補リストを順にガード判定 → 自動投票 (または dry-run log)。

    candidates: [{race_date, place_code, venue, venue_jp, race_no,
                  car_no, ev, bets, amount}] (amount=bets合計)
    返り値: 各候補の verdict dict list。
    """
    if not AUTO_BUY_ENABLED:
        return []
    if not any(c.get("bets") for c in candidates):
        return []
    if not _acquire_lock():
        logger.warning("[auto_buy] lock 取得不能 (先行プロセス実行中?) — "
                       "cap 二重通過防止のため今回の発注をスキップ")
        return [{"race": f"{c.get('venue','?')}_R{c.get('race_no')}",
                 "verdict": "skip_lock", "amount": int(c.get("amount", 0)),
                 "timestamp": (now or now_jst()).isoformat()}
                for c in candidates if c.get("bets")]
    try:
        return _run_auto_buy_locked(candidates, now, dry_run)
    finally:
        _release_lock()


def _run_auto_buy_locked(candidates: list[dict],
                         now: dt.datetime | None = None,
                         dry_run: bool | None = None) -> list[dict]:
    now = now or now_jst()
    dry = AUTO_BUY_DRY_RUN if dry_run is None else dry_run
    state = load_state(now)
    state["profit_yen"] = today_profit_from_history(state["date"])

    results = []
    for c in candidates:
        if not c.get("bets"):  # 複勝 OFF かつ三連系対象外 → 投票なし (防御的スキップ)
            continue
        amount = int(c["amount"])
        ev = float(c.get("ev", 0.0))
        race_label = f"{c.get('venue','?')}_R{c['race_no']}"
        ok, reason = check_guards(state, now, amount, ev)
        if not ok:
            logger.info("[auto_buy] %s skip: %s", race_label, reason)
            rec = {"race": race_label, "amount": amount,
                   "verdict": reason, "timestamp": now.isoformat()}
            state["executions"].append(rec)
            results.append(rec)
            _notify(f"[AUTO-BUY] {race_label} skip",
                    f"{race_label}\n判定: {reason}\n金額: ¥{amount}\n"
                    f"当日 spent ¥{state['spent_yen']} / "
                    f"profit ¥{state['profit_yen']}")
            continue

        bets_desc = format_bets_jp(c["bets"])  # 読みやすい日本語表記
        venue_jp = c.get("venue_jp", "?")
        race_jp = f"{venue_jp} R{c['race_no']} ({c['race_date']})"
        if dry:
            logger.info("[auto_buy][DRY-RUN] %s 投票相当: %s (¥%d)",
                        race_label, bets_desc, amount)
            state["spent_yen"] += amount  # cap ロジック検証のため simulate
            rec = {"race": race_label, "amount": amount,
                   "verdict": "dry_run", "bets": bets_desc,
                   "timestamp": now.isoformat()}
            state["executions"].append(rec)
            results.append(rec)
            _notify(
                f"[AUTO-BUY][DRY-RUN] {venue_jp} R{c['race_no']} 計¥{amount}",
                f"【自動発注 DRY-RUN(実投票なし)】\n"
                f"{race_jp}\n\n"
                f"買い目:\n  {bets_desc}\n\n"
                f"合計: ¥{amount}\n"
                f"当日 自動発注額(模擬): ¥{state['spent_yen']}")
            save_state(state)
            continue

        # 実投票
        logger.info("[auto_buy][LIVE] %s 投票: %s (¥%d)",
                    race_label, bets_desc, amount)
        success, detail, balance = _run_execute_purchase(
            c["race_date"], int(c["place_code"]), int(c["race_no"]), c["bets"])
        if success:
            state["spent_yen"] += amount
            state["consecutive_failures"] = 0
            verdict = "executed"
        else:
            state["consecutive_failures"] += 1
            verdict = "failed"
        # 残高低下警告 (ポイント+払戻金 合計 ≤ しきい値、1 日 1 回)
        _maybe_low_balance_alert(state, balance)
        rec = {"race": race_label, "amount": amount, "verdict": verdict,
               "bets": bets_desc, "detail": detail[-200:],
               "timestamp": now.isoformat()}
        state["executions"].append(rec)
        results.append(rec)
        _notify(
            f"[AUTO-BUY] {venue_jp} R{c['race_no']} 計¥{amount} "
            f"{'投票完了' if success else '失敗'}",
            f"【自動発注 {'投票完了' if success else '失敗'}】\n"
            f"{race_jp}\n\n"
            f"買い目:\n  {bets_desc}\n\n"
            f"合計: ¥{amount}\n"
            f"結果: {verdict}\n"
            f"当日 自動発注額: ¥{state['spent_yen']} / "
            f"連続失敗 {state['consecutive_failures']}\n\n"
            f"詳細: {detail[-300:]}")
        save_state(state)

    save_state(state)
    return results
