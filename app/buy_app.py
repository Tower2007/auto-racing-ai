"""click-to-buy 確認 UI (Streamlit、2026-05-08 導入)。

メールの「💰 購入する」ボタンから token 付き URL でアクセスされる。
1. token 検証 (HMAC + 期限 + 消費済チェック)
2. レース情報 + 推奨内容を表示
3. PIN 認証 (accounts.json に "pin" 設定時のみ、未設定なら skip)
4. 「✅ 購入する」ボタンで scripts/execute_purchase.py を起動
5. 結果表示

使い方:
  streamlit run app/buy_app.py --server.port 8502 --server.address 0.0.0.0

  → 同 LAN / Tailscale 内の端末から http://<HOST-IP>:8502/?p=<payload>&s=<sig>

設計メモ:
  - --server.address 0.0.0.0 でスマホからも LAN / Tailscale 内アクセス可
  - port 8502 (メイン streamlit_app.py は 8501)
  - dry-run mode は execute_purchase.py 側で制御
  - PIN は出先からの URL 流出時の最終防衛 (Tailscale 暗号化 + token + PIN の 3 層)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

VENUE_JP_MAP = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
ACCOUNTS_PATH = ROOT / "accounts.json"

st.set_page_config(page_title="autorace 購入確認", page_icon="💰", layout="centered")


def _load_pin() -> str:
    """accounts.json から PIN を読む (未設定なら空文字 = PIN 認証 skip)。"""
    if not ACCOUNTS_PATH.exists():
        return ""
    try:
        with ACCOUNTS_PATH.open(encoding="utf-8") as f:
            config = json.load(f)
        accounts = config.get("accounts", [])
        if accounts:
            return str(accounts[0].get("pin", "")).strip()
    except Exception:
        pass
    return ""


def _import_token_module():
    try:
        import buy_token  # noqa: F401
        return buy_token
    except ImportError as e:
        st.error(f"buy_token モジュール読込失敗: {e}")
        st.stop()


bt = _import_token_module()

try:
    from ehi_monitor import calculate_ehi
    ehi = calculate_ehi(7)
except Exception:
    ehi = None

params = st.query_params
b64 = params.get("p", "")
sig = params.get("s", "")

# ===== 入力チェック =====
if not b64 or not sig:
    st.title("🚫 無効なアクセス")
    st.write("メールから推奨候補のリンクを開いてください。")
    st.write("URL に `?p=...&s=...` パラメータが必要です。")
    st.stop()

# ===== 検証 =====
try:
    payload = bt.verify(b64, sig)
except ValueError as e:
    st.title("🚫 トークン検証失敗")
    st.error(f"{e}")
    st.write("以下のいずれかの可能性:")
    st.write("- 期限切れ (default 30 分)")
    st.write("- URL が改ざんされている")
    st.write("- accounts.json の buy_secret_key が変更された")
    bt.log_token(payload={}, sig=sig, status="failed",
                 note=f"verify: {e}")
    st.stop()

# ===== P3 hardening: payload validation (strict_amount=True / Phase A) =====
try:
    bt.validate_payload(payload, strict_amount=True)
except ValueError as e:
    st.title("🚫 payload validation 失敗")
    st.error(f"{e}")
    bt.log_token(payload, sig=sig, status="failed",
                 note=f"payload: {e}")
    st.stop()

# ===== P1 hardening: race_date が「現在開催中の日付」と一致確認 =====
# 日跨ぎ対応: today_jst または today-1 day を accept (深夜ミッドナイト用)
# 本当の一致は execute_purchase 側で確認画面の date と構造的に check する
race_date_payload = str(payload.get("race_date", ""))
if not bt.is_active_race_date(race_date_payload):
    st.title("🚫 古い race_date のトークン")
    st.error(
        f"payload race_date={race_date_payload} は今日 / 昨日のいずれでもない。"
    )
    st.write("古いメールリンクや 2 日以上前の token は使えません。")
    bt.log_token(payload, sig=sig, status="failed",
                 note=f"date out of active range: {race_date_payload}")
    st.stop()

# ===== 消費済みチェック =====
if bt.is_consumed(sig):
    st.title("⚠️ 既に消費済みのトークン")
    st.warning("このトークンは既に購入処理されています。")
    st.write("**重複購入を防ぐため、再度の購入はできません。**")
    st.write("購入履歴を確認: `data/buy_tokens.csv`")
    st.stop()

# ===== 購入確認画面 =====
place_code = int(payload.get("place_code", 0))
venue_jp = payload.get("venue_jp") or VENUE_JP_MAP.get(place_code, "?")
race_no = int(payload.get("race_no", 0))
car_no = int(payload.get("car_no", 0))
amount = int(payload.get("amount", 0))
ev = float(payload.get("ev", 0))
race_date = payload.get("race_date", "")

st.title("💰 購入確認")

if ehi and ehi.get("ehi") is not None:
    st.caption(
        f"🛡️ Edge Health Index (7d): **{ehi['ehi']}** {ehi['emoji']} {ehi['status']} "
        f"(n={ehi.get('n_races', 0)})"
    )

st.markdown(f"### {venue_jp} R{race_no}  /  {car_no}号  /  複勝")

col1, col2, col3 = st.columns(3)
col1.metric("金額", f"¥{amount:,}")
col2.metric("EV", f"{ev:.2f}")
col3.metric("レース日", race_date)

st.write("---")

# 累積成績(参考)
try:
    sys.path.insert(0, str(ROOT))
    from daily_predict import cumulative_performance
    perf = cumulative_performance()
    if perf and perf["n_total"] > 0:
        roi_pct = perf["roi"] * 100
        prof = perf["profit"]
        st.caption(
            f"📊 累積: {perf['n_total']} R / "
            f"{'+' if prof >= 0 else ''}¥{prof:,} / ROI {roi_pct:.1f}%"
        )
except Exception:
    pass

st.write("")

# dry-run チェックボックス (default: True で安全側)
dry_run = st.checkbox(
    "🛡️ dry-run (実際には購入しない、navigate + form fill のみ)",
    value=True,
    help=(
        "初期は dry-run で動作確認推奨。チェック外すと実際に投票実行。"
        "外す前に execute_purchase.py の selector が正しいか確認すること。"
    ),
)

# PIN 認証 (accounts.json に pin 設定時のみ表示)
expected_pin = _load_pin()
pin_input = ""
if expected_pin:
    st.write("")
    pin_input = st.text_input(
        "🔢 PIN (4 桁)",
        type="password",
        max_chars=8,
        help=(
            "accounts.json で設定した PIN を入力。"
            "URL 流出時の最終防衛(Tailscale 暗号化 + token + PIN の 3 層)。"
        ),
    )

st.write("")

if st.button("✅ 購入する", type="primary", use_container_width=True):
    # PIN 認証チェック (設定時のみ)
    if expected_pin and pin_input != expected_pin:
        st.error("🚫 PIN が一致しません")
        bt.log_token(payload, sig=sig, status="failed",
                     note="PIN mismatch")
        st.stop()

    # P2 hardening: atomic reserve (本番のみ)
    if not dry_run:
        if not bt.reserve_token(payload, sig=sig,
                                note="reserved via buy_app"):
            st.error(
                "🚫 このトークンは既に予約 / 消費済 (race condition / 二重 click)"
            )
            st.stop()
    else:
        # dry-run は consume せず、ログだけ残す
        bt.mark_dry_run(payload, sig=sig, note="dry-run via buy_app")

    with st.spinner("Playwright で投票実行中... (Chrome window が開きます)"):
        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "execute_purchase.py"),
            "--race-date", str(race_date),
            "--place", str(place_code),
            "--race", str(race_no),
            "--car", str(car_no),
            "--amount", str(amount),
        ]
        if dry_run:
            cmd.append("--dry-run")

        try:
            result = subprocess.run(
                cmd, cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
        except subprocess.TimeoutExpired:
            st.error("❌ タイムアウト (>180s)")
            if not dry_run:
                bt.mark_failed(payload, sig=sig, note="timeout >180s")
            st.stop()

    if result.returncode == 0:
        if dry_run:
            st.success("✅ dry-run 完了")
        else:
            st.success("✅ 購入完了")
            bt.mark_executed(payload, sig=sig,
                             note=result.stdout[-200:])
        with st.expander("実行ログ"):
            st.code(result.stdout)
    else:
        st.error(f"❌ 失敗 (exit={result.returncode})")
        if not dry_run:
            bt.mark_failed(payload, sig=sig, note=result.stderr[-200:])
        else:
            bt.log_token(payload, sig=sig, status="dry_run_failed",
                         note=result.stderr[-200:])
        with st.expander("エラー詳細"):
            st.code(result.stderr or result.stdout or "(no output)")

st.write("---")
_pin_label = "PIN 認証 ON" if expected_pin else "PIN 認証 OFF (accounts.json で 'pin' 設定で有効化)"
st.caption(
    "© autorace-ai click-to-buy &nbsp;|&nbsp; "
    f"🛡️ token 1 回限り / Phase A 推奨のみ / 金額 ¥100 固定 / {_pin_label}"
)
