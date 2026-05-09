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
    st.write("- 期限切れ (default 24 時間)")
    st.write("- URL が改ざんされている")
    st.write("- accounts.json の buy_secret_key が変更された")
    bt.log_token(payload={}, sig=sig, status="failed",
                 note=f"verify: {e}")
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
    # 消費としてマーク (実行前に記録、再 click 防止)
    bt.log_token(payload, sig=sig,
                 status="consumed" if not dry_run else "dry_run",
                 note="confirmed via buy_app")

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
            bt.log_token(payload, sig=sig, status="failed",
                         note="timeout")
            st.stop()

    if result.returncode == 0:
        if dry_run:
            st.success("✅ dry-run 完了")
        else:
            st.success("✅ 購入完了")
            bt.log_token(payload, sig=sig, status="executed",
                         note=result.stdout[-200:])
        with st.expander("実行ログ"):
            st.code(result.stdout)
    else:
        st.error(f"❌ 失敗 (exit={result.returncode})")
        bt.log_token(payload, sig=sig, status="failed",
                     note=result.stderr[-200:])
        with st.expander("エラー詳細"):
            st.code(result.stderr or result.stdout or "(no output)")

st.write("---")
_pin_label = "PIN 認証 ON" if expected_pin else "PIN 認証 OFF (accounts.json で 'pin' 設定で有効化)"
st.caption(
    "© autorace-ai click-to-buy &nbsp;|&nbsp; "
    f"🛡️ token 1 回限り / Phase A 推奨のみ / 金額 ¥100 固定 / {_pin_label}"
)
