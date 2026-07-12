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
from pathlib import Path

logger = logging.getLogger("auto_buy")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = DATA / "auto_buy_state.json"
EXECUTE_SCRIPT = ROOT / "scripts" / "execute_purchase.py"
BET_HISTORY_CSV = DATA / "bet_history.csv"
# 「発注結果不明」sticky 停止フラグ (2026-07-12 Codex再々検証 ③)。
# WAIT_ABANDONED (先行プロセスが ReleaseMutex せず異常終了) を検知した時に
# 書き出し、人間が投票履歴/state/台帳を照合して明示削除するまで
# **全券種** の発注 (auto_buy + buy_app トークン実行) を停止する。
# 運用パターンは data/rt3_backstop_stop.flag と同じ (存在=停止、人間のみ削除)。
# 機械照合による自動解除は実装しない (確実性優先)。
ABANDONED_STOP_FLAG = DATA / "abandoned_lock_stop.flag"
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


# ─── 三連系 停止フラグの最終発注点 再検査 (2026-07-12 Codex再検証 ②) ──────

SANREN_BET_TYPES = ("rt3", "rf3")


def bets_include_sanren(bets: list[dict] | None) -> bool:
    """bets に三連系 (rt3/rf3) が含まれるか。"""
    return any(str(b.get("type")) in SANREN_BET_TYPES for b in (bets or []))


def rt3_final_gate_blocks(bets: list[dict] | None) -> bool:
    """発注直前 (mutex 取得後) の三連系停止フラグ再検査。True = 発注しない。

    daily_predict 側の候補判定 (rt3_buy_active) から mutex 待ち最大
    LOCK_WAIT_SEC (90秒) を経て実発注に至るため、その間に停止フラグ
    (rt3_stop.flag / rt3_backstop_stop.flag) が立った場合を最終発注点で拾う。

    - 三連系を含まない bets → False (再検査不要、複勝は対象外)
    - rt3_buy_active() が False → True (停止中)
    - 判定自体が失敗 → True (fail-closed)
    """
    if not bets_include_sanren(bets):
        return False
    try:
        import daily_predict  # 遅延 import (呼び出し元が daily_predict なら実質無償)
        return not daily_predict.rt3_buy_active()
    except Exception as e:  # noqa: BLE001
        logger.warning("[auto_buy] 三連系停止フラグ再検査に失敗 (%s) — "
                       "fail-closed で当該候補をスキップ", e)
        return True


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


# ── プロセス間排他 (Windows named mutex) ──────────────────────────────
# 2026-07-11 監査 P1-2 → 2026-07-12 Codex艦隊監査 P1-1 で置換:
# 旧実装 (O_CREAT|O_EXCL ロックファイル + 600s stale 破棄) は「生存中プロセスの
# ロックでも 10 分経過で他者が unlink できる」削除経路が残り、重複投票・
# 日次 cap 二重通過が起こり得た。stale 判定→unlink は原子的にできず TOCTOU が
# 原理的に残るため、ファイル削除ロジックは全廃し、統合マネジメントシステム
# (Public-Race-ManagementｰSystem/monitor/snapshot.py, Codexレビュー5往復通過) の
# named mutex パターンを移植:
#   - 排他は OS が管理 (削除・横取りという操作自体が存在しない)
#   - 保持者がクラッシュしても abandoned mutex として次の待機者へ所有が移る
#   - WinDLL(use_last_error) + argtypes/restype 明示 (64bit HANDLE 切り詰め防止)
LOCK_WAIT_SEC = 90            # 取得待ちの上限 (先行プロセスの発注は最長 ~60s)

WAIT_OBJECT_0, WAIT_ABANDONED, WAIT_TIMEOUT = 0x0, 0x80, 0x102


def abandoned_stop_active() -> bool:
    """「発注結果不明」sticky 停止フラグが立っているか (存在=停止、全券種)。

    buy_app のトークン実行直前ゲートからも参照される (三連系に限らず
    全券種をブロックする点が rt3 系フラグと異なる)。解除は人間が
    投票履歴/auto_buy_state.json/bet_history を照合した上での明示削除のみ。
    """
    return ABANDONED_STOP_FLAG.exists()


def _write_abandoned_flag(mutex_name: str) -> None:
    """WAIT_ABANDONED 検知時の sticky フラグ書き出し (検知時刻・mutex名・手順)。"""
    DATA.mkdir(parents=True, exist_ok=True)
    ABANDONED_STOP_FLAG.write_text(
        f"detected_at={now_jst().isoformat(timespec='seconds')}\n"
        f"mutex={mutex_name}\n"
        f"# 先行の自動発注プロセスが ReleaseMutex せずに異常終了しました\n"
        f"# (WAIT_ABANDONED)。「投票クリック後、state/台帳保存前」に死んでいた\n"
        f"# 場合、当日 cap の過少計上や重複投票の可能性が残ります。\n"
        f"# このファイルがある間、自動発注 (auto_buy) と buy_app のトークン実行は\n"
        f"# 全券種停止します (sticky)。\n"
        f"# 人手照合の手順:\n"
        f"#   1. autorace.jp の投票履歴に意図しない/二重の投票がないか\n"
        f"#   2. data/auto_buy_state.json の spent_yen が実投票と一致するか\n"
        f"#   3. data/bet_history.csv / logs/ の直近エントリ\n"
        f"# 問題がない (または台帳を修正した) ことを確認した上で、\n"
        f"# 人間がこのファイルを削除して発注を再開してください (自動解除なし)。\n",
        encoding="utf-8")


def _mutex_name() -> str:
    """プロジェクトパスのハッシュ入り mutex 名
    (固定名だと別チェックアウト・別ユーザー・テストと干渉するため名前空間を分離)。"""
    import hashlib
    h = hashlib.sha1(str(ROOT).encode("utf-8")).hexdigest()[:8]
    return f"Global\\AutoRacingAI_auto_buy_{h}"


def _kernel32():
    """kernel32 を HANDLE/DWORD/BOOL/LPCWSTR の argtypes/restype 明示で返す
    (restype 未指定は c_int 扱いで、64bit Windows ではポインタサイズの HANDLE が
    切り詰められ WaitForSingleObject が失敗し得る)。"""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    k32.ReleaseMutex.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _acquire_lock(wait_sec: float | None = None, name: str | None = None):
    """named mutex を取得。成功なら (k32, handle, abandoned)、取得不能なら None。

    abandoned (bool): WAIT_ABANDONED で所有を引き継いだ場合 True。
    先行プロセスが「投票クリック後、state/台帳保存前」に死んだ可能性があり、
    当日 cap の過少計上・重複投票の不確実性が残る。呼び出し側 (run_auto_buy)
    は abandoned=True では発注を続行せず、全候補 skip + sticky な
    「発注結果不明」フラグ (ABANDONED_STOP_FLAG) 書き出し + 警告通知に倒す
    (2026-07-12 Codex再々検証 ③)。以後の全 run はフラグを人間が照合・削除
    するまで発注を停止する。ロック自体は取得済みなので正常解放する
    (mutex が発注可否を持つのではなく、フラグが持つ)。

    発注ゲートなので例外は外に出さない: None → 呼び出し側は skip_lock verdict
    (発注せずスキップ = cap 破りより機会損失を選ぶ)。
    name はテスト専用の注入口 (既定はプロジェクトパスハッシュ入り)。"""
    if wait_sec is None:
        wait_sec = LOCK_WAIT_SEC
    if os.name != "nt":
        # 非Windows に named mutex の排他保証はない —
        # 黙って無ロックで発注するより fail-closed
        logger.warning("[auto_buy] named mutex は Windows 専用 — "
                       "排他保証がないため発注をスキップ (fail-closed)")
        return None
    import ctypes
    try:
        k32 = _kernel32()
        handle = k32.CreateMutexW(None, False, name or _mutex_name())
        if not handle:
            logger.warning("[auto_buy] CreateMutexW 失敗 (WinError=%d) — skip",
                           ctypes.get_last_error())
            return None
    except Exception as e:
        logger.warning("[auto_buy] mutex 初期化失敗 (%s) — skip", e)
        return None
    try:
        rc = k32.WaitForSingleObject(handle, 0)
        if rc == WAIT_TIMEOUT:
            logger.info("[auto_buy] 先行プロセスが発注中 — 最長 %ds 待機",
                        int(wait_sec))
            rc = k32.WaitForSingleObject(handle, int(wait_sec * 1000))
        if rc == WAIT_TIMEOUT:
            k32.CloseHandle(handle)
            return None
        if rc not in (WAIT_OBJECT_0, WAIT_ABANDONED):
            logger.warning("[auto_buy] WaitForSingleObject 失敗 "
                           "(rc=0x%X, WinError=%d) — skip",
                           rc, ctypes.get_last_error())
            k32.CloseHandle(handle)
            return None
        abandoned = (rc == WAIT_ABANDONED)
        if abandoned:
            # 先行プロセスが ReleaseMutex せず死んだ。所有は OS が本プロセスへ
            # 移譲済みだが、先行が「投票クリック後、state/台帳保存前」に死んだ
            # 場合は当日 cap 過少計上・重複投票の不確実性が残る。
            # → 発注は続行しない (run_auto_buy が全候補 skip + 警告通知)。
            logger.warning("[auto_buy] 先行プロセスの異常終了を検知 "
                           "(abandoned mutex) — 結果不明のため本 run の発注は"
                           "全候補スキップし sticky フラグを書き出す "
                           "(ロックは正常解放、発注停止はフラグが担う)")
        return (k32, handle, abandoned)
    except Exception as e:
        logger.warning("[auto_buy] mutex 取得失敗 (%s) — skip", e)
        try:
            k32.CloseHandle(handle)
        except Exception:
            pass
        return None


def _release_lock(lock) -> None:
    """ReleaseMutex の戻り値を必ず検査する (失敗の握りつぶし禁止)。
    ただし例外にはしない: 発注結果 (verdict list) を失うわけにいかず、本プロセスは
    per-race one-shot で間もなく終了 → 所有スレッド終了時に abandoned 化して
    待機者が自動回復するため、ここは大声のログで足りる。"""
    import ctypes
    k32, handle = lock[0], lock[1]
    try:
        if not k32.ReleaseMutex(handle):
            logger.error("[auto_buy] ReleaseMutex 失敗 (WinError=%d) — "
                         "所有スレッド終了時に abandoned 化し待機者は回復します",
                         ctypes.get_last_error())
    finally:
        k32.CloseHandle(handle)


# ── 購入ゲート (短命クリティカルセクション用 named mutex) ─────────────────
# 2026-07-12 Codex第6R: run mutex (_mutex_name / _acquire_lock) とは別物。
# run 全体ではなく「abandoned フラグ生成 (書込)」と「execute_purchase の
# 最終検査 → click 送出」を**同一排他区間**に入れ、プロセス間で原子化する
# 短命ロック。フラグ生成側がゲート保持中は click 側の検査〜click が待たされ、
# 逆も同様なので、検査と click の間に別プロセスがフラグを割り込ませられない。
#
# デッドロック回避 (必須): ネスト順は常に「run mutex → 購入ゲート」の一方向。
#   - フラグ生成側 (run_auto_buy の WAIT_ABANDONED 経路) は run mutex を保持した
#     まま購入ゲートを取得する (run mutex → gate)。
#   - click 側 (execute_purchase) は run mutex を一切取得せず購入ゲートのみ取得する。
# click 側が run mutex を待つ経路が存在しないため循環待ちが生じない。
# 購入ゲートは最短区間だけ保持する (ブラウザ準備の await はゲート外)。
PURCHASE_GATE_WAIT_SEC = 30          # 単発取得の待ち上限 (click 側/1トライ)
# 生成側 (WAIT_ABANDONED) の総リトライ上限。click 側のゲート保持は
# 「最終検査 + click(timeout 5秒) + 解放」の最短区間で、必ず finally 解放される
# ため、通常運用ではこの上限に達する前に必ず取得できる (5秒 << 300秒)。
PURCHASE_GATE_TOTAL_WAIT_SEC = 300

# acquire の 3 値化 (2026-07-12 Codex第8R): 取得失敗を区別する。
#   GATE_OK      … 取得成功 (gate=(k32,handle))
#   GATE_TIMEOUT … 競合 (ゲートは存在するが他者が保持中で待ち上限内に取れず)
#   GATE_BROKEN  … mutex サブシステム自体が使えない (CreateMutexW 失敗 / 非Windows /
#                  WaitForSingleObject が異常 rc)。破損は一時的/局所的でありうる (全
#                  プロセスで恒久的に購入不能とは限らない) ため、halt を落とさぬよう
#                  broken でも best-effort でフラグを書く (第10R Codex 指摘で修正)
GATE_OK, GATE_TIMEOUT, GATE_BROKEN = "ok", "timeout", "broken"


def _purchase_gate_name() -> str:
    """購入ゲート named mutex 名 (run mutex とは別名前空間、パスハッシュで分離)。"""
    import hashlib
    h = hashlib.sha1(str(ROOT).encode("utf-8")).hexdigest()[:8]
    return f"Global\\AutoRacingAI_purchase_gate_{h}"


def _acquire_purchase_gate_ex(wait_sec: float, name: str | None = None):
    """購入ゲート取得の内部実装。返り値 (status, gate)。

    status は GATE_OK / GATE_TIMEOUT / GATE_BROKEN。
    - GATE_OK: gate=(k32, handle) を所有 (WAIT_OBJECT_0 / WAIT_ABANDONED)。
    - GATE_TIMEOUT: 競合 (他者が保持) で wait_sec 内に取得できず。gate=None。
    - GATE_BROKEN: mutex 生成不可・非Windows・異常 rc。gate=None。
    「タイムアウト(競合)」と「破損」を必ず区別する (第8R): 呼び出し側の
    「取得まで待ち続ける (timeout) / best-effort write (broken) 」判断がこの区別に
    依存する。broken でも halt は best-effort で永続化する (第10R)。"""
    if os.name != "nt":
        # 非Windows: named mutex 自体が使えない = broken 扱い (fail-closed)。
        logger.warning("[auto_buy] 購入ゲートは Windows 専用 — broken 扱い")
        return GATE_BROKEN, None
    import ctypes
    try:
        k32 = _kernel32()
        handle = k32.CreateMutexW(None, False, name or _purchase_gate_name())
        if not handle:
            logger.warning("[auto_buy] 購入ゲート CreateMutexW 失敗 "
                           "(WinError=%d) — broken", ctypes.get_last_error())
            return GATE_BROKEN, None
    except Exception as e:  # noqa: BLE001
        logger.warning("[auto_buy] 購入ゲート初期化失敗 (%s) — broken", e)
        return GATE_BROKEN, None
    try:
        rc = k32.WaitForSingleObject(handle, 0)
        if rc == WAIT_TIMEOUT:
            rc = k32.WaitForSingleObject(handle, int(wait_sec * 1000))
        if rc == WAIT_TIMEOUT:
            k32.CloseHandle(handle)
            # 競合: ゲートは存在するが他者が保持中。呼び出し側で再試行し得る。
            return GATE_TIMEOUT, None
        if rc not in (WAIT_OBJECT_0, WAIT_ABANDONED):
            logger.warning("[auto_buy] 購入ゲート WaitForSingleObject 異常 "
                           "(rc=0x%X, WinError=%d) — broken",
                           rc, ctypes.get_last_error())
            k32.CloseHandle(handle)
            return GATE_BROKEN, None
        if rc == WAIT_ABANDONED:
            # 先行がゲート保持中に異常終了。所有は OS が本プロセスに移譲済み。
            # 短区間ゲートなので所有を引き継いでそのまま続行する。
            logger.warning("[auto_buy] 購入ゲート abandoned (先行の異常終了) — "
                           "所有を引き継いで続行")
        return GATE_OK, (k32, handle)
    except Exception as e:  # noqa: BLE001
        # 取得中の予期せぬ例外は broken 扱い (fail-closed 側)。
        logger.warning("[auto_buy] 購入ゲート取得失敗 (%s) — broken", e)
        try:
            k32.CloseHandle(handle)
        except Exception:
            pass
        return GATE_BROKEN, None


def acquire_purchase_gate(wait_sec: float | None = None,
                          name: str | None = None):
    """購入ゲート named mutex を単発取得。成功なら (k32, handle)、取得不能なら None。

    click 側 (execute_purchase) 用の薄いラッパ: timeout(競合) も broken も None を
    返す (click 側はどちらでも「原子性を保証できない」ので fail-closed abort が正しい)。
    短命クリティカルセクション用。name はテスト専用の注入口。"""
    status, gate = _acquire_purchase_gate_ex(
        PURCHASE_GATE_WAIT_SEC if wait_sec is None else wait_sec, name)
    return gate if status == GATE_OK else None


def acquire_purchase_gate_blocking(name: str | None = None,
                                   per_try_sec: float | None = None,
                                   total_sec: float | None = None):
    """生成側 (WAIT_ABANDONED 経路) 用の取得。返り値 (status, gate)。

    第8R: timeout(競合) と broken(mutex 破損) を区別して扱う。
    - GATE_TIMEOUT(競合) は**諦めずに再試行し続ける**。click 側は必ず finally で
      ゲートを解放する (最終検査 + click timeout 5秒 + 解放の最短区間) ため、
      現実には total_sec 内に必ず取得できる。取得後 (GATE_OK, gate) を返す。
    - GATE_BROKEN(CreateMutexW 失敗・非Windows・異常 rc 等) は即 (GATE_BROKEN, None)。
      呼び出し側 (run_auto_buy) は broken を「非write」にせず best-effort で
      フラグを書いて halt を永続化する (第9R: broken は一時障害も含み「恒久的に
      購入不能」を保証しないため、非write では後続 click が halt 無しで購入再開し得る)。
    - total_sec を超えても競合が解消しない異常時のみ (GATE_TIMEOUT, None) を返す
      (最後の砦: 呼び出し側が同様に best-effort でフラグを書き sticky halt を残す)。
    """
    per_try = PURCHASE_GATE_WAIT_SEC if per_try_sec is None else per_try_sec
    total = PURCHASE_GATE_TOTAL_WAIT_SEC if total_sec is None else total_sec
    import time as _t
    deadline = _t.monotonic() + total
    attempts = 0
    while True:
        status, gate = _acquire_purchase_gate_ex(per_try, name)
        if status == GATE_OK:
            return GATE_OK, gate
        if status == GATE_BROKEN:
            return GATE_BROKEN, None
        # GATE_TIMEOUT: 競合。click 側の解放を待って再試行する。
        attempts += 1
        if _t.monotonic() >= deadline:
            logger.error("[auto_buy] 購入ゲート競合が総上限 %ds を超えても解消せず "
                         "(%d 回試行) — 最後の砦へ", int(total), attempts)
            return GATE_TIMEOUT, None
        logger.warning("[auto_buy] 購入ゲート競合で取得できず — 再試行 "
                       "(%d 回目、総上限 %ds まで)", attempts, int(total))


def release_purchase_gate(gate) -> None:
    """購入ゲートを解放 (ReleaseMutex 戻り値検査)。gate=None なら no-op。"""
    if gate is None:
        return
    import ctypes
    k32, handle = gate[0], gate[1]
    try:
        if not k32.ReleaseMutex(handle):
            logger.error("[auto_buy] 購入ゲート ReleaseMutex 失敗 (WinError=%d)",
                         ctypes.get_last_error())
    finally:
        k32.CloseHandle(handle)


def _mark_abandoned_alerted(now: dt.datetime | None) -> None:
    """当日の abandoned 関連通知済みマークを state に記録 (日次 reset に乗る)。
    検知時のメールと残存フラグの日次リマインドの重複送信を防ぐ。"""
    try:
        st = load_state(now)
        st["abandoned_alerted"] = True
        save_state(st)
    except Exception as e:  # noqa: BLE001
        logger.warning("[auto_buy] abandoned 通知済みマーク保存失敗: %s", e)


def _alert_abandoned_pending_once(now: dt.datetime | None,
                                  n_skipped: int) -> None:
    """発注結果不明フラグ残存の警告通知 (日次1回。毎 run はうるさいため)。"""
    try:
        st = load_state(now)
        if st.get("abandoned_alerted"):
            return
        st["abandoned_alerted"] = True
        save_state(st)
    except Exception as e:  # noqa: BLE001
        # state で重複判定できない場合は通知を優先 (多重送信 > 無通知)
        logger.warning("[auto_buy] abandoned 通知の重複判定失敗 (%s) — 送信する", e)
    _notify(
        "[AUTO-BUY] 🛑 発注結果不明フラグ残存 — 全券種の発注を停止中 (要人手照合)",
        "【自動発注 停止中: abandoned_lock_stop.flag が残っています】\n\n"
        "過去の run が先行プロセスの異常終了 (WAIT_ABANDONED) を検知して以降、\n"
        "人手照合が完了していないため、自動発注 (auto_buy) と buy_app の\n"
        "トークン実行は全券種停止しています。\n"
        f"本 run も {n_skipped} 候補を発注せずスキップしました "
        "(skip_abandoned_pending)。\n\n"
        "再開手順 (人手照合 → 問題なければフラグ削除):\n"
        "  1. autorace.jp の投票履歴に意図しない/二重の投票がないか\n"
        "  2. data/auto_buy_state.json の spent_yen が実投票と一致するか\n"
        "  3. data/bet_history.csv / logs/ の直近エントリ\n"
        f"  4. 問題なければ data/{ABANDONED_STOP_FLAG.name} を削除\n\n"
        "(この通知は 1 日 1 回です。フラグの詳細はフラグファイル内に記載)")


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
    # ── 「発注結果不明」sticky フラグ検査 (2026-07-12 Codex再々検証 ③) ──
    # 過去の run が WAIT_ABANDONED を検知して書いたフラグが残っている間は、
    # 人間が照合・削除するまで以後の全 run の発注を停止する
    # (警告メールを人間が確認する前に次のスケジュール実行が始まっても
    #  結果不明のまま購入が再開しない)。通知は日次1回に抑制。
    if abandoned_stop_active():
        verdicts = [
            {"race": f"{c.get('venue','?')}_R{c.get('race_no')}",
             "verdict": "skip_abandoned_pending",
             "amount": int(c.get("amount", 0)),
             "timestamp": (now or now_jst()).isoformat()}
            for c in candidates if c.get("bets")]
        logger.warning("[auto_buy] 発注結果不明フラグ (%s) が残存 — "
                       "%d 候補を skip_abandoned_pending で処理 (発注なし)。"
                       "人手照合の上フラグを削除するまで全券種停止",
                       ABANDONED_STOP_FLAG, len(verdicts))
        _alert_abandoned_pending_once(now, len(verdicts))
        return verdicts
    lock = _acquire_lock()
    if lock is None:
        logger.warning("[auto_buy] lock 取得不能 (先行プロセス実行中?) — "
                       "cap 二重通過防止のため今回の発注をスキップ")
        return [{"race": f"{c.get('venue','?')}_R{c.get('race_no')}",
                 "verdict": "skip_lock", "amount": int(c.get("amount", 0)),
                 "timestamp": (now or now_jst()).isoformat()}
                for c in candidates if c.get("bets")]
    try:
        if lock[2]:
            # WAIT_ABANDONED: 先行プロセス異常終了・結果不明 (2026-07-12 Codex③
            # 再々検証で sticky 化)。先行が「投票クリック後、state/台帳保存前」に
            # 死んだ場合、当日 cap の過少計上・重複投票の不確実性が残るため、
            # 本 run は発注せず全候補 skip + sticky フラグ書き出し + 警告通知。
            # フラグは人間が照合・削除するまで以後の全 run (全券種) を止める。
            # ロック自体は finally で正常解放する (mutex は再取得可能な状態に
            # 戻すが、発注はフラグが止める)。
            verdicts = [
                {"race": f"{c.get('venue','?')}_R{c.get('race_no')}",
                 "verdict": "skip_abandoned_lock",
                 "amount": int(c.get("amount", 0)),
                 "timestamp": (now or now_jst()).isoformat()}
                for c in candidates if c.get("bets")]
            flag_note = f"data/{ABANDONED_STOP_FLAG.name} を書き出しました。"
            # 通常経路では購入ゲート**所有下で**フラグを書く (2026-07-12 Codex第6R→
            # 第7R→第8R→第9R)。縮退経路 (broken / 総上限超過) は下記のとおり
            # best-effort で必ず書く (halt 永続化を最優先)。
            # click 側 (execute_purchase) は同じ購入ゲートを保持して「最終検査 →
            # click」を行い、必ず finally で解放する (click timeout 5秒で必ず抜ける)。
            # よって生成側はゲートを待てば必ず取得でき、その保持下で書けば、検査〜
            # click の間にフラグ書込が割り込むことも、書込〜可視化の間に click が
            # 抜けることもない (プロセス間で原子的)。
            #
            # ★第8R訂正: acquire の失敗を「timeout(競合)」と「broken(mutex破損)」で
            #  区別する。旧実装は 30秒タイムアウトでも None を返し、それを一律
            #  「破損」と誤解釈して非write・正常復帰していた。反例: click 側が OS
            #  スケジューリング遅延でゲートを 30秒超保持 → 生成側 timeout → 非write
            #  → click 送出 → sticky フラグ無しで後続購入も再開、が残っていた。
            #  対策: timeout(競合) は諦めず**再試行し続ける** (acquire_purchase_gate_
            #  blocking)。click は必ず解放するので現実には必ず取得できる。
            _gstatus, _gate = acquire_purchase_gate_blocking()
            if _gstatus == GATE_OK:
                # 通常経路: ゲート保持下で原子的に write。
                try:
                    _write_abandoned_flag(_mutex_name())
                except Exception as e:  # noqa: BLE001
                    logger.error("[auto_buy] 発注結果不明フラグ書き込み失敗: %s", e)
                    flag_note = (f"⚠️ フラグ書き出しに失敗しました ({e})。"
                                 "手動での状況確認を最優先してください。")
                finally:
                    release_purchase_gate(_gate)
            else:
                # GATE_BROKEN または GATE_TIMEOUT(総上限超過)。いずれも縮退経路で、
                # ゲート保持下の原子的 write は達成できない。だが**必ずフラグを書く**
                # (best-effort) — sticky halt (後続購入の停止) の永続化を最優先する。
                #
                # ★第9R訂正: 旧実装は GATE_BROKEN を「非write でも安全」としていたが、
                #  それは時間方向に成立しない。broken には非Windows だけでなく
                #  「一時的な CreateMutexW 失敗 / 異常 rc / 例外」も含まれ、
                #  「全プロセスで恒久的に購入不能」を保証しない。反例: 生成プロセス
                #  だけ一時的に broken → 非write → 障害回復後や別 click プロセスでは
                #  正常 → 後続 click がゲート取得・検査通過し sticky halt 無しで購入
                #  再開。よって broken でも best-effort で write して halt を残す
                #  (実行中 click との完全原子性は broken/総上限では保証できないが、
                #   後続購入停止=halt 永続化を優先。総上限超過と同じ縮退方針)。
                #  これで全ケースで halt が必ず残る:
                #    GATE_OK       → ゲート下で原子的 write
                #    GATE_TIMEOUT  → 再試行して原子的 write (この else には来ない)
                #    GATE_BROKEN   → best-effort write (下記)
                #    総上限超過     → best-effort write (下記)
                _reason = ("mutex 生成不可 (broken)" if _gstatus == GATE_BROKEN
                           else "購入ゲート競合が総上限を超え解消せず")
                logger.error(
                    "[auto_buy] %s — 縮退経路: best-effort でフラグを書き後続購入を"
                    "停止する (実行中 click との完全原子性は保証できないが halt の"
                    "永続化を最優先。要人手確認)", _reason)
                try:
                    _write_abandoned_flag(_mutex_name())
                    flag_note = (
                        f"⚠️ {_reason} のため、best-effort でフラグを書き出しました "
                        "(後続購入の停止=halt 永続化を優先)。mutex 異常や実行中 click "
                        "による二重投票がないか最優先で確認してください。")
                except Exception as e:  # noqa: BLE001
                    logger.error("[auto_buy] 縮退経路のフラグ書込も失敗: %s", e)
                    flag_note = (f"⚠️ フラグ書き出しに失敗しました ({e})。"
                                 "手動での状況確認を最優先してください。")
            _mark_abandoned_alerted(now)
            _notify(
                "[AUTO-BUY] 🛑 先行プロセス異常終了検知 — 全券種の発注を停止 (要人手照合)",
                "【自動発注 停止: abandoned mutex 検知 (sticky)】\n\n"
                "発注ロックの先行保持プロセスが ReleaseMutex せずに異常終了して"
                "いました (WAIT_ABANDONED)。\n"
                "先行プロセスが「投票クリック後、state/台帳保存前」に死んでいた"
                "場合、当日 cap の過少計上や重複投票の可能性が残るため、\n"
                f"本 run の {len(verdicts)} 候補は発注せずスキップし、"
                f"{flag_note}\n"
                "このフラグがある間、自動発注 (auto_buy) と buy_app のトークン"
                "実行は**全券種**停止します。\n\n"
                "再開手順 (人手照合 → 問題なければフラグ削除):\n"
                "  1. autorace.jp の投票履歴に意図しない/二重の投票がないか\n"
                "  2. data/auto_buy_state.json の spent_yen が実投票と一致するか\n"
                "  3. data/bet_history.csv / logs/ の直近エントリ\n"
                f"  4. 問題なければ data/{ABANDONED_STOP_FLAG.name} を削除\n\n"
                "機械照合による自動解除はありません (確実性優先、人手解除のみ)。")
            logger.warning("[auto_buy] abandoned mutex — %d 候補を "
                           "skip_abandoned_lock で処理し sticky フラグを書き出し "
                           "(発注なし、以後の run も人手解除まで停止)",
                           len(verdicts))
            return verdicts
        # ── mutex 取得後の abandoned フラグ再検査 (2026-07-12 Codex再々々検証) ──
        # 入口 (行 623) の検査は mutex 取得**前**の1回のみ。並行 run に競合窓が残る:
        #   1. run B が入口でフラグ不存在を確認 → mutex 待ちに入る
        #   2. run A が WAIT_ABANDONED を受領し sticky フラグを書き出す
        #   3. run A が mutex を正常解放
        #   4. run B が WAIT_OBJECT_0 で正常取得 → lock[2] 分岐に入らず、
        #      作成済みフラグを見ずに発注してしまう
        # これを塞ぐため、mutex 保持中 (発注ループ直前) にフラグを再検査する。
        # 判定不能も fail-closed (abandoned_stop_active は exists() のみで
        # 例外は事実上出ないが、防御的に True 扱いで全候補 skip)。
        try:
            _abandoned_now = abandoned_stop_active()
        except Exception as e:  # noqa: BLE001
            logger.error("[auto_buy] mutex 取得後の abandoned フラグ再検査に失敗 "
                         "(%s) — fail-closed で全候補スキップ", e)
            _abandoned_now = True
        if _abandoned_now:
            verdicts = [
                {"race": f"{c.get('venue','?')}_R{c.get('race_no')}",
                 "verdict": "skip_abandoned_pending",
                 "amount": int(c.get("amount", 0)),
                 "timestamp": (now or now_jst()).isoformat()}
                for c in candidates if c.get("bets")]
            logger.warning("[auto_buy] mutex 取得後の再検査で発注結果不明フラグ "
                           "(%s) を検知 — %d 候補を skip_abandoned_pending で処理 "
                           "(発注なし)。競合窓 (run B が mutex 待ち中に run A が"
                           "フラグ作成) を捕捉", ABANDONED_STOP_FLAG, len(verdicts))
            _alert_abandoned_pending_once(now, len(verdicts))
            return verdicts
        return _run_auto_buy_locked(candidates, now, dry_run)
    finally:
        _release_lock(lock)


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
        # 2026-07-12 Codex再検証 ②: 候補生成 → mutex 待ち (最大90秒) の間に
        # 三連系停止フラグが立った/バックストップが読めなくなった場合を
        # 発注直前 (ロック取得後) に再検査して拾う。fail-closed。
        if ok and rt3_final_gate_blocks(c.get("bets")):
            ok = False
            reason = ("skip_rt3_stop_recheck (発注直前再検査: "
                      "三連系停止フラグ ON または判定不能)")
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
