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

# ===== カスタム CSS (演出強化 2026-06-11) =====
st.markdown("""
<style>
/* ヒーローカード: 流れるグラデ背景 */
.buy-hero {
    background: linear-gradient(135deg, #1a2a6c, #b21f1f, #fdbb2d);
    background-size: 300% 300%;
    animation: hero-bg 6s ease infinite;
    border-radius: 14px;
    padding: 22px 18px;
    color: white;
    text-align: center;
    box-shadow: 0 6px 24px rgba(0,0,0,.35);
    margin-bottom: 14px;
}
@keyframes hero-bg {
    0%   { background-position: 0% 50%; }
    50%  { background-position: 100% 50%; }
    100% { background-position: 0% 50%; }
}
.buy-hero .sub {
    opacity: .85;
    font-size: 13px;
    letter-spacing: 3px;
    font-weight: 700;
}
.buy-hero .venue {
    font-size: 34px;
    font-weight: 900;
    letter-spacing: 2px;
    text-shadow: 0 2px 6px rgba(0,0,0,.5);
    margin: 4px 0;
}
.buy-hero .meta { opacity: .92; font-size: 15px; }
.buy-hero .ev-chip {
    display: inline-block;
    background: rgba(255,215,0,.25);
    border: 1px solid rgba(255,215,0,.7);
    border-radius: 20px;
    padding: 2px 14px;
    margin-top: 6px;
    font-weight: 800;
    font-size: 16px;
    text-shadow: 0 0 8px rgba(255,215,0,.6);
}
/* 買い目行 */
.bet-row {
    display: flex; justify-content: space-between; align-items: center;
    background: rgba(255,255,255,.06);
    border-left: 4px solid #ffd700;
    padding: 10px 16px; margin: 6px 0;
    border-radius: 8px;
    font-size: 17px; font-weight: 700;
    animation: row-in .4s ease both;
}
.bet-row:nth-child(2) { animation-delay: .08s; }
.bet-row:nth-child(3) { animation-delay: .16s; }
.bet-row.total {
    border-left-color: #ff512f;
    background: rgba(255,81,47,.10);
    font-size: 19px;
}
@keyframes row-in {
    from { opacity: 0; transform: translateX(-12px); }
    to   { opacity: 1; transform: translateX(0); }
}
/* 購入ボタン: 脈動グラデ */
.stButton button {
    background: linear-gradient(90deg, #ff512f, #dd2476) !important;
    border: none !important;
    color: white !important;
    font-size: 20px !important;
    font-weight: 900 !important;
    padding: 14px !important;
    border-radius: 12px !important;
    letter-spacing: 2px;
    animation: buy-pulse 1.4s ease-in-out infinite;
}
@keyframes buy-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(221,36,118,.55); }
    50%      { box-shadow: 0 0 0 12px rgba(221,36,118,0); }
}
/* 投票完了バナー: チェッカーフラッグ + ポップイン */
.done-banner {
    position: relative;
    border-radius: 12px;
    overflow: hidden;
    margin: 12px 0;
    animation: done-pop .5s cubic-bezier(.2,1.6,.4,1) both;
}
.done-banner .checker {
    position: absolute; inset: 0;
    background: repeating-conic-gradient(#000 0% 25%, #fff 0% 50%);
    background-size: 28px 28px;
    opacity: .15;
    animation: checker-scroll 1.2s linear infinite;
}
@keyframes checker-scroll {
    0%   { background-position: 0 0; }
    100% { background-position: 28px 0; }
}
.done-banner .inner {
    position: relative;
    text-align: center;
    padding: 20px;
    background: linear-gradient(135deg, rgba(46,204,113,.92), rgba(39,174,96,.92));
    color: white;
    font-size: 24px;
    font-weight: 900;
    letter-spacing: 2px;
    text-shadow: 0 2px 6px rgba(0,0,0,.4);
}
@keyframes done-pop {
    0%   { transform: scale(.6); opacity: 0; }
    100% { transform: scale(1);  opacity: 1; }
}
/* 失敗バナー: シェイク */
.fail-banner {
    background: linear-gradient(135deg, #c0392b, #e74c3c);
    color: white; text-align: center;
    padding: 18px; border-radius: 12px;
    font-size: 20px; font-weight: 900;
    margin: 12px 0;
    box-shadow: 0 4px 18px rgba(231,76,60,.5);
    animation: fail-shake .5s ease-in-out 2;
}
@keyframes fail-shake {
    0%, 100% { transform: translateX(0); }
    20%, 60% { transform: translateX(-8px); }
    40%, 80% { transform: translateX(8px); }
}
</style>
""", unsafe_allow_html=True)


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
    bt.validate_payload(payload, strict_amount=False)
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
bets = payload.get("bets") or []  # 三連系まとめ買いモード (浜松・山陽 EV>=1.80)

# 券種コード → 日本語ラベル + 出目フォーマット
_BET_LABEL = {"fns": "複勝", "rt3": "三連単", "rf3": "三連複"}
_BET_SEP = {"fns": "", "rt3": "→", "rf3": "-"}


def _deme_str(bet_type: str, cars: list) -> str:
    """券種別の出目表記。fns='6' / rt3='6→5→4' / rf3='4-5-6'。"""
    sep = _BET_SEP.get(bet_type, "-")
    return sep.join(str(int(c)) for c in cars)


# ===== ヒーローカード =====
st.markdown(
    f'<div class="buy-hero">'
    f'<div class="sub">💰 PURCHASE CONFIRMATION</div>'
    f'<div class="venue">🏁 {venue_jp} R{race_no}</div>'
    f'<div class="meta">{race_date}</div>'
    f'<div class="ev-chip">EV {ev:.2f}</div>'
    f'</div>',
    unsafe_allow_html=True,
)

if ehi and ehi.get("ehi") is not None:
    st.caption(
        f"🛡️ Edge Health Index (7d): **{ehi['ehi']}** {ehi['emoji']} {ehi['status']} "
        f"(n={ehi.get('n_races', 0)})"
    )

if bets:
    # === 三連系まとめ買いモード ===
    total_amount = sum(int(b.get("amount", 0)) for b in bets)
    rows_html = ""
    for b in bets:
        bt_code = str(b.get("type", ""))
        label = _BET_LABEL.get(bt_code, bt_code)
        deme = _deme_str(bt_code, b.get("cars", []))
        amt = int(b.get("amount", 0))
        rows_html += (
            f'<div class="bet-row"><span>🎫 {label}　<b>{deme}</b></span>'
            f'<span>¥{amt:,}</span></div>'
        )
    rows_html += (
        f'<div class="bet-row total"><span>💰 合計 ({len(bets)} 券種)</span>'
        f'<span>¥{total_amount:,}</span></div>'
    )
    st.markdown(rows_html, unsafe_allow_html=True)
    amount = total_amount  # 以降の表示・互換用
else:
    # === 単一複勝モード (従来) ===
    st.markdown(
        f'<div class="bet-row"><span>🎫 複勝　<b>{car_no}号</b></span>'
        f'<span>¥{amount:,}</span></div>',
        unsafe_allow_html=True,
    )

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

# dry-run チェックボックス (default: OFF — 購入リンク経由の実運用を優先)
dry_run = st.checkbox(
    "🛡️ dry-run (実際には購入しない、navigate + form fill のみ)",
    value=False,
    help=(
        "ON にすると投票実行せず画面遷移のみ。"
        "通常は OFF で実投票。PIN 認証が最終防衛。"
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
        ]
        if bets:
            # 三連系まとめ買い: bets を JSON で渡す
            cmd += ["--bets-json", json.dumps(bets, ensure_ascii=False)]
        else:
            # 単一複勝 (従来)
            cmd += ["--car", str(car_no), "--amount", str(amount)]
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
            # 🏁 チェッカーフラッグ演出 + バルーン
            st.markdown(
                '<div class="done-banner">'
                '<div class="checker"></div>'
                '<div class="inner">🏁 投票完了！ GOOD LUCK！ 🏁</div>'
                '</div>',
                unsafe_allow_html=True,
            )
            st.balloons()
            bt.mark_executed(payload, sig=sig,
                             note=result.stdout[-200:])
        with st.expander("実行ログ"):
            st.code(result.stdout)
    else:
        st.markdown(
            f'<div class="fail-banner">❌ 投票失敗 (exit={result.returncode})</div>',
            unsafe_allow_html=True,
        )
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
    f"🛡️ token 1 回限り / Phase A 推奨のみ / 複勝=推奨額・三連=各¥100 / {_pin_label}"
)
