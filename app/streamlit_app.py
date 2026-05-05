"""オートレース予想 エンタメ版 (Streamlit ローカル版)

使い方:
  streamlit run app/streamlit_app.py

仕様:
- 日付 + 場を選ぶと、その日の全レース×5券種の買い目を表示
- 2 モード自動切替:
  * リプレイモード(過去日 OOF データあり): 中間モデル OOF 予測 + 実績結果
  * ライブ予想モード(今日以降 / OOF 範囲外): autorace.jp から program/odds を取得し
    本番モデル(production_model.lgb)で予測。結果は表示しない(まだ走ってない)
- 5 券種: 単勝/複勝/ワイド/三連複/三連単 (二車連・二車単 除外)
"""

from __future__ import annotations

import datetime as dt
import json
import pickle
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))  # daily_predict / src を import 可能に
RACE_KEY = ["race_date", "place_code", "race_no"]

# JST 固定 (Streamlit Cloud は UTC なので明示変換が必須)
JST = dt.timezone(dt.timedelta(hours=9))


def jst_now() -> dt.datetime:
    """現在時刻 JST (tz-naive)"""
    return dt.datetime.now(JST).replace(tzinfo=None)


def jst_today() -> dt.date:
    """本日 JST"""
    return jst_now().date()

# デプロイモード判定: 以下いずれかで cloud モードに切替
#   1. 環境変数 DEPLOY_MODE=cloud
#   2. Streamlit Cloud は /mount/src 配下で実行されるので path で自動判定
# デフォルト (local) は PC ローカル運用向けフル UI。
import os as _os
DEPLOY_MODE = _os.environ.get("DEPLOY_MODE", "").lower()
if not DEPLOY_MODE:
    # Streamlit Cloud auto-detect (path は /mount/src/<repo>/...)
    DEPLOY_MODE = "cloud" if str(ROOT).startswith("/mount/src") else "local"
IS_CLOUD = DEPLOY_MODE == "cloud"

# 推奨ベット額ルックアップ (daily_predict.recommended_bet_yen と同等、独立実装で
# Streamlit reload 時の import 衝突を回避)
EXPECTED_VOTES_CSV = ROOT / "data" / "expected_votes.csv"
_VOTES_LOOKUP_CACHE: dict | None = None


def recommended_bet_yen(place_code: int, race_no: int) -> int:
    """場×R 別 推奨ベット額。CSV 無ければ ¥100 fallback。"""
    global _VOTES_LOOKUP_CACHE
    if _VOTES_LOOKUP_CACHE is None:
        try:
            import pandas as _pd
            df = _pd.read_csv(EXPECTED_VOTES_CSV)
            _VOTES_LOOKUP_CACHE = {
                (int(r["place_code"]), int(r["race_no"])): int(r["rec_yen_10pct"])
                for _, r in df.iterrows()
            }
        except Exception:
            _VOTES_LOOKUP_CACHE = {}
    return max(100, _VOTES_LOOKUP_CACHE.get((place_code, race_no), 100))

BET = 100
CALIB_CUTOFF = "2024-04"

VENUE_NAMES = {2: "kawaguchi", 3: "isesaki", 4: "hamamatsu", 5: "iizuka", 6: "sanyou"}
VENUE_JP = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
NAME_TO_PC = {v: k for k, v in VENUE_NAMES.items()}

BET_LABELS = {
    "tns": "単勝", "fns": "複勝", "wid": "ワイド",
    "rfw": "二車連", "rtw": "二車単",
    "rf3": "三連複", "rt3": "三連単",
}
BET_ORDER = ["tns", "fns", "wid", "rfw", "rtw", "rf3", "rt3"]


# ── ロジック層 ──

@st.cache_data(show_spinner="データ読み込み中…")
def load_data():
    preds = pd.read_parquet(DATA / "walkforward_predictions_morning_top3.parquet")
    preds["race_date"] = pd.to_datetime(preds["race_date"])
    odds = pd.read_csv(DATA / "odds_summary.csv", low_memory=False)
    odds["race_date"] = pd.to_datetime(odds["race_date"])
    pay = pd.read_csv(DATA / "payouts.csv", low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])

    calib = preds[preds["test_month"] < CALIB_CUTOFF]
    if calib.empty:
        # データ retention で CALIB_CUTOFF 以前が消えた場合のフォールバック:
        # production_calib.pkl (週次再学習で生成) を使う
        with open(DATA / "production_calib.pkl", "rb") as f:
            iso = pickle.load(f)
    else:
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(calib["pred"].values, calib["target_top3"].values)
    preds["pred_calib"] = iso.transform(preds["pred"].values)
    return preds, odds, pay


@st.cache_resource(show_spinner=False)
def load_production_artifacts():
    """ライブ予想用: 本番モデル + isotonic 校正器 + meta を読む。"""
    import lightgbm as lgb
    model = lgb.Booster(model_file=str(DATA / "production_model.lgb"))
    with open(DATA / "production_calib.pkl", "rb") as f:
        iso = pickle.load(f)
    with open(DATA / "production_meta.json", "r", encoding="utf-8") as f:
        meta = json.load(f)
    return model, iso, meta


@st.cache_resource(show_spinner=False)
def get_autorace_client():
    from src.client import AutoraceClient
    return AutoraceClient()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_today_open_venues() -> list[tuple[int, bool]]:
    """Hold/Today API で本日開催 + 中止でない場の (place_code, is_finished) リストを返す。

    is_finished: finalRefundFlg=1 (全 R 払戻確定 = 本日終了)
    終了場も結果振り返り目的で残す (selectbox では「(終了)」suffix で識別)。
    """
    from daily_predict import fetch_today_schedule
    client = get_autorace_client()
    schedule = fetch_today_schedule(client)
    try:
        raw = client.get_today_hold().get("body", {}).get("today", [])
        final_refund = {
            int(h.get("placeCode")): str(h.get("finalRefundFlg")) == "1"
            for h in raw if h.get("placeCode") is not None
        }
    except Exception:
        final_refund = {}
    out = []
    for pc, info in schedule.items():
        if str(info.get("cancelFlg")) == "1":
            continue
        out.append((pc, final_refund.get(pc, False)))
    # 進行中 → 終了の順、各群内は pc 昇順
    return sorted(out, key=lambda x: (x[1], x[0]))


@st.cache_data(ttl=300, show_spinner=False)
def fetch_race_start_times(date_str: str, pc: int) -> dict[int, str]:
    """R 毎の発走予定時刻 'HH:MM' を返す。Program/Print ページから取得。"""
    try:
        client = get_autorace_client()
        venue_key = VENUE_NAMES[pc]
        times = client.get_program_print_times(venue_key, date_str)
        if times:
            return {int(k): v for k, v in times.items()}
    except Exception:
        pass
    return {}


PRE_RACE_PRECISION_MIN = 60  # 発走 N 分以内のオッズを「直前(本予想)」、それ以遠を「前売」


def race_status(start_time_str: str | None, now: dt.datetime,
                has_odds: bool, has_result: bool) -> tuple[str, str]:
    """発走時刻 + 現在時刻 + odds/result 状況から (バッジ, 詳細) を返す。
    オッズあり時、発走 60 分以内 = 🎯本予想 / それ以遠 = 🔮前売予想 で区別。"""
    if not start_time_str:
        return ("❓ 不明", "発走時刻未取得")
    try:
        hh, mm = map(int, start_time_str.split(":"))
        # ミッドナイト "24:04" / "25:30" 表記を翌日扱いに正規化
        day_offset = 0
        if hh >= 24:
            hh -= 24
            day_offset = 1
        start_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if day_offset:
            start_dt += dt.timedelta(days=day_offset)
    except Exception:
        return ("❓ 不明", f"時刻形式不正: {start_time_str}")
    is_past = start_dt < now
    if is_past:
        return ("✅ 確定", "結果反映済") if has_result else ("⏳ 結果待ち", "発走済・結果未取得")
    if not has_odds:
        return ("📝 暫定予想", "オッズ未公開")
    minutes_to_start = (start_dt - now).total_seconds() / 60
    if minutes_to_start <= PRE_RACE_PRECISION_MIN:
        return ("🎯 本予想", f"直前オッズ・発走まで {int(minutes_to_start)} 分")
    return ("🔮 前売予想", f"前売オッズ・発走まで {int(minutes_to_start)} 分")


@st.cache_data(ttl=300, show_spinner=False)
def fetch_live_day(date_str: str, pc: int) -> dict:
    """1 場 12 R 分の状態+予測+結果を取得。
    戻り値: {race_no: {has_odds, has_result, source, top_cars, df, refund_info}}
    source: 'middle_model' or 'ai_official' or 'none'
    """
    from daily_predict import build_features_for_race
    client = get_autorace_client()
    model, iso, meta = load_production_artifacts()
    out = {}

    # 結果は 1 度だけ取得 (1 場分まとめて返ってくる)
    try:
        refund_resp = client.get_race_refund(pc, date_str)
        refund_body = refund_resp.get("body", []) if refund_resp else []
    except Exception:
        refund_body = []
    refund_by_race = {}
    if isinstance(refund_body, list):
        for race in refund_body:
            try:
                rn = int(race.get("raceNo", 0))
                if race.get("refundInfo"):
                    refund_by_race[rn] = race
            except Exception:
                pass

    for r in range(1, 13):
        info = {
            "has_odds": False,
            "has_result": r in refund_by_race,
            "source": "none", "top_cars": [], "df": None,
            "refund_info": refund_by_race.get(r),
            # 全券種オッズ (display 用)
            "odds_lists": {},  # {bet_type: raw API odds dict}
        }
        try:
            prog = client.get_program(pc, date_str, r)
            if prog.get("result") != "Success":
                out[r] = info; continue
            prog_body = prog.get("body", {})
            if not isinstance(prog_body, dict) or not prog_body.get("playerList"):
                out[r] = info; continue
        except Exception:
            out[r] = info; continue

        # オッズ取得 (単勝・複勝とも 4 車以上有効ならば中間モデル可能)
        try:
            odds_resp = client.get_odds(pc, date_str, r)
            odds_body = odds_resp.get("body", {})
            # 全券種オッズを保存 (display 用、bet_type ごとに raw dict)
            if isinstance(odds_body, dict):
                for bt_key, list_key in [
                    ("tns", "tnsOddsList"), ("fns", "fnsOddsList"),
                    ("wid", "widOddsList"), ("rfw", "rfwOddsList"),
                    ("rtw", "rtwOddsList"),
                    ("rf3", "rf3OddsList"), ("rt3", "rt3OddsList"),
                ]:
                    v = odds_body.get(list_key)
                    if isinstance(v, dict) and v:
                        info["odds_lists"][bt_key] = v
            tns = odds_body.get("tnsOddsList") if isinstance(odds_body, dict) else None
            n_valid_tns = 0
            if isinstance(tns, dict):
                for v in tns.values():
                    try:
                        if float(v) > 0:
                            n_valid_tns += 1
                    except Exception:
                        pass
            # 複勝の min が有効な車数も別途カウント (EV 計算に必須)
            fns = odds_body.get("fnsOddsList") if isinstance(odds_body, dict) else None
            n_valid_fns = 0
            if isinstance(fns, dict):
                for entry in fns.values():
                    if isinstance(entry, dict):
                        try:
                            m = float(entry.get("min", 0))
                            if m > 0:
                                n_valid_fns += 1
                        except Exception:
                            pass
            # 単勝・複勝とも 4 車以上有効ならば中間モデルで本予想
            has_odds = (n_valid_tns >= 4) and (n_valid_fns >= 4)
        except Exception:
            odds_body, has_odds = {}, False
        info["has_odds"] = has_odds

        # 予想生成
        if has_odds:
            try:
                feat = build_features_for_race(pc, date_str, r, prog_body, odds_body)
                if not feat.empty:
                    feat = feat[feat["is_absent"] == 0].copy()
                    if not feat.empty:
                        from daily_predict import align_features
                        X = align_features(feat, meta)
                        feat["pred"] = model.predict(X)
                        feat["pred_calib"] = iso.transform(feat["pred"].values)
                        feat["ev_avg_calib"] = (
                            feat["pred_calib"]
                            * (feat["place_odds_min"] + feat["place_odds_max"])
                            / 2
                        )
                        feat["pred_rank"] = feat["pred"].rank(method="min", ascending=False)
                        feat = feat.sort_values("pred_calib", ascending=False)
                        info["df"] = feat
                        info["top_cars"] = [int(c) for c in feat["car_no"].head(3).tolist()]
                        info["source"] = "middle_model"
            except Exception as e:
                st.warning(f"R{r} 中間モデル予測失敗: {e}")

        if not info["top_cars"]:
            # AI 予想を fallback (sunnyExpectCode 昇順 = 1 が本命)
            ai_ranked = sorted(
                [
                    (int(p["carNo"]), p.get("sunnyExpectCode"))
                    for p in prog_body.get("playerList", [])
                    if p.get("sunnyExpectCode") not in (None, "", 0)
                ],
                key=lambda x: int(x[1]) if str(x[1]).isdigit() else 99,
            )
            if ai_ranked:
                info["top_cars"] = [c for c, _ in ai_ranked[:3]]
                info["source"] = "ai_official"

        out[r] = info
    return out


def make_picks(top_cars: list[int]) -> dict[str, list[int]]:
    t1 = top_cars[0]
    t2 = top_cars[1] if len(top_cars) >= 2 else None
    t3 = top_cars[2] if len(top_cars) >= 3 else None
    return {
        "tns": [t1],
        "fns": [t1],
        "wid": [t1, t2] if t2 else [],
        "rfw": [t1, t2] if t2 else [],  # 二車連 (順序なし)
        "rtw": [t1, t2] if t2 else [],  # 二車単 (順序あり)
        "rf3": [t1, t2, t3] if (t2 and t3) else [],
        "rt3": [t1, t2, t3] if (t2 and t3) else [],
    }


def fmt_combo(bt: str, cars: list[int]) -> str:
    if bt in ("tns", "fns"):
        return str(cars[0])
    # 順序ありは半角ダッシュ + > で表現 (mobile 列幅節約のため → を使わない)
    if bt in ("rt3", "rtw"):
        return ">".join(str(c) for c in cars)
    return "-".join(str(c) for c in sorted(cars))


def lookup_odds(bt: str, cars: list[int], odds_lists: dict) -> str:
    """券種別オッズを raw API dict から lookup → 表示用 string"""
    if not cars or not odds_lists:
        return "未公開"
    od = odds_lists.get(bt)
    if not isinstance(od, dict) or not od:
        return "未公開"
    try:
        if bt == "tns":
            v = od.get(str(cars[0]))
            return f"{float(v):.1f}" if v and float(v) > 0 else "未公開"
        if bt == "fns":
            entry = od.get(str(cars[0]), {})
            mn, mx = float(entry.get("min", 0)), float(entry.get("max", 0))
            return f"{mn:.1f}-{mx:.1f}" if mn > 0 else "未公開"
        if bt == "wid":
            # widOddsList[c1][c2] = {min, max} (c1 < c2)
            s = sorted(cars)
            entry = od.get(str(s[0]), {}).get(str(s[1]), {})
            mn, mx = float(entry.get("min", 0)), float(entry.get("max", 0))
            return f"{mn:.1f}-{mx:.1f}" if mn > 0 else "未公開"
        if bt == "rfw":
            # rfwOddsList[c1][c2] = odds (二車連、c1 < c2)
            s = sorted(cars)
            v = od.get(str(s[0]), {}).get(str(s[1]))
            return f"{float(v):.1f}" if v and float(v) > 0 else "未公開"
        if bt == "rtw":
            # rtwOddsList[c1][c2] = odds (二車単、順序あり)
            v = od.get(str(cars[0]), {}).get(str(cars[1]))
            return f"{float(v):.1f}" if v and float(v) > 0 else "未公開"
        if bt == "rf3":
            # rf3OddsList[c1][c2] = odds (三連複、c1 < c2 < c3、最後のキーは省略可能)
            s = sorted(cars)
            entry = od.get(str(s[0]), {}).get(str(s[1]))
            if isinstance(entry, dict):
                v = entry.get(str(s[2]))
                return f"{float(v):.1f}" if v and float(v) > 0 else "未公開"
            return f"{float(entry):.1f}" if entry and float(entry) > 0 else "未公開"
        if bt == "rt3":
            # rt3OddsList[c1][c2][c3] = odds (三連単、順序あり)
            v = od.get(str(cars[0]), {}).get(str(cars[1]), {}).get(str(cars[2]))
            return f"{float(v):.1f}" if v and float(v) > 0 else "未公開"
    except (ValueError, TypeError, AttributeError):
        pass
    return "未公開"


def check_hit(bt: str, picked: list[int], pay_rows: pd.DataFrame) -> tuple[bool, float]:
    if not picked or pay_rows.empty:
        return False, 0.0
    if bt in ("tns", "fns"):
        match = pay_rows[pay_rows["car_no_1"] == picked[0]]
    elif bt == "wid":
        s = sorted(picked)
        match = pay_rows[
            ((pay_rows["car_no_1"] == s[0]) & (pay_rows["car_no_2"] == s[1])) |
            ((pay_rows["car_no_1"] == s[1]) & (pay_rows["car_no_2"] == s[0]))
        ]
    elif bt == "rf3":
        s = sorted(picked)
        match = pay_rows[
            (pay_rows["car_no_1"] == s[0]) &
            (pay_rows["car_no_2"] == s[1]) &
            (pay_rows["car_no_3"] == s[2])
        ]
    elif bt == "rt3":
        match = pay_rows[
            (pay_rows["car_no_1"] == picked[0]) &
            (pay_rows["car_no_2"] == picked[1]) &
            (pay_rows["car_no_3"] == picked[2])
        ]
    else:
        return False, 0.0
    if match.empty:
        return False, 0.0
    return True, float(match["refund"].sum())


def fmt_yen(v: float) -> str:
    if pd.isna(v):
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(int(v)):,}"


# ── UI 層 ──

st.set_page_config(
    page_title="オートレース予想 エンタメ版",
    page_icon="🏁",
    # cloud (iPhone) は centered で読みやすく、local (PC) は wide で情報量重視
    layout="centered" if IS_CLOUD else "wide",
    initial_sidebar_state="expanded",  # iPhone でも最初からサイドバー開く
)
# iPhone Safari で適切な viewport meta + ホーム画面追加 OK
if IS_CLOUD:
    st.markdown("""
    <style>
    /* モバイル: フォント大きめ、表は横スクロール、ボタン押しやすく */
    body { font-size: 16px; }
    .stButton button, .stLinkButton a { padding: 12px 18px !important; font-size: 15px !important; }
    [data-testid="stMetricValue"] { font-size: 22px !important; }
    /* リプレイモード関連の widget が誤って残ってもサイドバー狭く */
    section[data-testid="stSidebar"] { min-width: 240px; max-width: 280px; }
    </style>
    """, unsafe_allow_html=True)

# カスタム CSS (エンタメ感アップ)
st.markdown("""
<style>
.race-card-win {
    background: linear-gradient(135deg, rgba(46,204,113,0.15), rgba(46,204,113,0.03));
    border-left: 4px solid #2ecc71;
    padding: 8px 12px;
    border-radius: 4px;
    margin: 6px 0;
}
.race-card-loss {
    background: linear-gradient(135deg, rgba(231,76,60,0.10), rgba(231,76,60,0.02));
    border-left: 4px solid #e74c3c;
    padding: 8px 12px;
    border-radius: 4px;
    margin: 6px 0;
}
.race-card-next {
    background: linear-gradient(135deg, rgba(241,196,15,0.20), rgba(241,196,15,0.05));
    border-left: 4px solid #f1c40f;
    padding: 8px 12px;
    border-radius: 4px;
    margin: 6px 0;
    box-shadow: 0 0 12px rgba(241,196,15,0.3);
}
.race-card-future {
    background: rgba(150,150,150,0.05);
    border-left: 4px solid #888;
    padding: 8px 12px;
    border-radius: 4px;
    margin: 6px 0;
}
.race-card-pending {
    background: linear-gradient(135deg, rgba(52,152,219,0.10), rgba(52,152,219,0.02));
    border-left: 4px solid #3498db;
    padding: 8px 12px;
    border-radius: 4px;
    margin: 6px 0;
}
.big-win {
    color: #f39c12;
    font-weight: bold;
    text-shadow: 0 0 8px rgba(243,156,18,0.5);
}
/* === 💎 購入推奨 派手バナー === */
@keyframes rec-pulse {
    0%, 100% {
        box-shadow: 0 0 10px rgba(255, 215, 0, 0.7),
                    0 0 20px rgba(255, 100, 100, 0.5),
                    inset 0 0 12px rgba(255,255,255,0.3);
        transform: scale(1);
    }
    50% {
        box-shadow: 0 0 24px rgba(255, 215, 0, 1),
                    0 0 48px rgba(255, 100, 100, 0.8),
                    inset 0 0 20px rgba(255,255,255,0.5);
        transform: scale(1.02);
    }
}
@keyframes rec-rainbow {
    0%   { background-position: 0% 50%; }
    100% { background-position: 200% 50%; }
}
@keyframes rec-shake {
    0%, 100% { transform: rotate(0deg); }
    25% { transform: rotate(-3deg); }
    75% { transform: rotate(3deg); }
}
@keyframes diamond-spin {
    0% { transform: scale(1) rotate(0deg); }
    50% { transform: scale(1.4) rotate(180deg); }
    100% { transform: scale(1) rotate(360deg); }
}
.recommend-banner {
    display: block;
    padding: 10px 18px;
    margin: 10px 0 -4px 0;
    border-radius: 8px;
    font-size: 17px;
    font-weight: 900;
    color: white;
    text-align: center;
    letter-spacing: 1px;
    text-shadow: 0 2px 4px rgba(0,0,0,0.5),
                 0 0 8px rgba(255,255,255,0.4);
    background: linear-gradient(
        90deg,
        #ff006e 0%, #ff8c00 16%, #ffd700 33%,
        #00d4aa 50%, #00b4ff 66%, #8b5cf6 83%, #ff006e 100%);
    background-size: 300% auto;
    animation:
        rec-pulse 1.0s ease-in-out infinite,
        rec-rainbow 4s linear infinite;
    border: 2px solid rgba(255, 255, 255, 0.6);
}
.recommend-banner .gem {
    display: inline-block;
    animation: diamond-spin 1.5s ease-in-out infinite;
    font-size: 22px;
    margin: 0 6px;
}
.recommend-banner .urgent {
    display: inline-block;
    animation: rec-shake 0.4s ease-in-out infinite;
    color: #fff200;
    text-shadow: 0 0 8px rgba(255,242,0,0.8);
}
/* 推奨ティッカー (画面上の点滅バー) */
@keyframes ticker-blink {
    0%, 50% { opacity: 1; }
    51%, 100% { opacity: 0.65; }
}
.rec-ticker {
    background: linear-gradient(90deg, #c0392b, #e67e22, #c0392b);
    background-size: 200% 100%;
    animation: rec-rainbow 3s linear infinite, ticker-blink 0.8s ease-in-out infinite;
    color: white;
    padding: 8px 14px;
    border-radius: 6px;
    font-weight: bold;
    text-align: center;
    margin: 8px 0;
    box-shadow: 0 0 16px rgba(231,76,60,0.6);
}
.hero-title {
    font-size: 28px;
    font-weight: bold;
    text-align: center;
    background: linear-gradient(90deg, #ff6b6b, #f59e0b, #ffd93d);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 8px;
}
/* === エキサイトバイク風 走行アニメ === */
.bike-track {
    position: relative;
    height: 64px;
    background:
        linear-gradient(180deg,
            #87CEEB 0%, #87CEEB 38%,           /* 空 */
            #5C8A3A 38%, #5C8A3A 50%,          /* 草 */
            #C9A878 50%, #B8956A 100%);        /* オレンジ土の路面 */
    border-radius: 6px;
    overflow: hidden;
    margin: 8px 0 16px 0;
    box-shadow: inset 0 -3px 0 rgba(0,0,0,0.3),
                inset 0 2px 4px rgba(255,255,255,0.2);
}
/* 路面の白い破線 (走行レーン) */
.bike-track::before {
    content: "";
    position: absolute;
    bottom: 14px;
    left: 0; right: 0;
    height: 2px;
    background: repeating-linear-gradient(
        90deg,
        rgba(255,255,255,0.7) 0px,
        rgba(255,255,255,0.7) 16px,
        transparent 16px, transparent 32px
    );
    animation: lane-scroll 0.6s linear infinite;
}
@keyframes lane-scroll {
    0% { background-position-x: 0; }
    100% { background-position-x: -32px; }
}
/* 雲 */
.bike-track::after {
    content: "☁️ ☁️ ☁️ ☁️";
    position: absolute;
    top: 2px;
    white-space: nowrap;
    font-size: 12px;
    opacity: 0.7;
    animation: cloud-drift 30s linear infinite;
}
@keyframes cloud-drift {
    0% { left: -20%; }
    100% { left: 110%; }
}
.bike-lane {
    position: absolute;
    animation: bike-ride linear infinite;
    white-space: nowrap;
    filter: drop-shadow(2px 2px 0 rgba(0,0,0,0.3));
}
.bike-lane svg { display: block; }
/* 各バイクの個性 (速度・位置・遅延) — 8 車実装 */
.lane-1 { bottom: 14px; animation-duration: 5.2s; animation-delay: 0s;    }
.lane-2 { bottom: 19px; animation-duration: 5.8s; animation-delay: -0.7s; }
.lane-3 { bottom: 11px; animation-duration: 4.4s; animation-delay: -1.4s; }
.lane-4 { bottom: 23px; animation-duration: 6.0s; animation-delay: -2.1s; }
.lane-5 { bottom: 16px; animation-duration: 5.0s; animation-delay: -2.8s; }
.lane-6 { bottom: 20px; animation-duration: 5.5s; animation-delay: -3.5s; }
.lane-7 { bottom: 13px; animation-duration: 4.7s; animation-delay: -4.2s; }
.lane-8 { bottom: 24px; animation-duration: 6.3s; animation-delay: -4.9s; }
@keyframes bike-ride {
    0%   { left: -50px; }
    100% { left: 100%; }
}
/* バイクが走るときの上下バウンド */
.bike-bounce {
    display: inline-block;
    animation: bounce 0.18s ease-in-out infinite alternate;
}
@keyframes bounce {
    from { transform: translateY(0); }
    to   { transform: translateY(-2px); }
}
/* 砂埃エフェクト */
.dust {
    position: absolute;
    bottom: 4px;
    font-size: 14px;
    opacity: 0.5;
    animation: dust-puff linear infinite;
}
.dust-1 { animation-duration: 5s; animation-delay: -0.3s; }
.dust-2 { animation-duration: 6.5s; animation-delay: -1.8s; }
.dust-3 { animation-duration: 4.2s; animation-delay: -3.3s; }
@keyframes dust-puff {
    0% { left: -30px; opacity: 0; }
    20% { opacity: 0.5; }
    100% { left: 100%; opacity: 0; }
}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="hero-title">🏁 オートレース 予想 エンタメ版 🏁</div>', unsafe_allow_html=True)

# エキサイトバイク風 アニメーション バナー (オートレース車両 SVG, 公式車番色 1-8)
# 公式枠色: 1=白 2=黒 3=赤 4=青 5=黄 6=緑 7=橙 8=桃
_CAR_COLORS = {
    1: ("#ffffff", "#000000"),  # (helmet_bg, text_color)
    2: ("#222222", "#ffffff"),
    3: ("#e74c3c", "#ffffff"),
    4: ("#3498db", "#ffffff"),
    5: ("#f1c40f", "#000000"),
    6: ("#27ae60", "#ffffff"),
    7: ("#e67e22", "#ffffff"),
    8: ("#ec7ab5", "#ffffff"),
}


def _autorace_bike_svg(num: int) -> str:
    """オートレース風 (低姿勢・前傾ライダー) のバイク SVG。ヘルメット色 = 車番色。"""
    helm_bg, txt = _CAR_COLORS[num]
    body = helm_bg if num != 1 else "#dddddd"  # 1号車の白いボディは見にくいので薄灰に
    return (
        f'<svg width="44" height="22" viewBox="0 0 44 22" xmlns="http://www.w3.org/2000/svg">'
        # 後輪 (大きめ・スポーク感)
        f'<circle cx="9" cy="17" r="4.5" fill="#1a1a1a"/>'
        f'<circle cx="9" cy="17" r="1.5" fill="#999"/>'
        # 前輪 (やや小さめ)
        f'<circle cx="35" cy="17" r="4.2" fill="#1a1a1a"/>'
        f'<circle cx="35" cy="17" r="1.4" fill="#999"/>'
        # 低い水平フレーム + エンジン部の塊
        f'<rect x="11" y="13" width="20" height="3" fill="#3a3a3a" rx="1"/>'
        f'<rect x="14" y="10" width="14" height="4" fill="{body}" stroke="#000" stroke-width="0.4" rx="1"/>'
        # ゼッケン番号 (車体側面)
        f'<text x="21" y="13.5" font-size="5" font-weight="bold" fill="{txt}" '
        f'text-anchor="middle" font-family="Arial,sans-serif">{num}</text>'
        # ハンドル / フロントフォーク (短く前傾)
        f'<line x1="29" y1="11" x2="34" y2="8" stroke="#555" stroke-width="1.8" stroke-linecap="round"/>'
        # ライダー (前傾・ハンドルに伏せた形)
        f'<path d="M 18 10 Q 23 5 30 8" stroke="{body}" stroke-width="3.5" fill="none" '
        f'stroke-linecap="round"/>'
        # ヘルメット (車番色)
        f'<circle cx="32" cy="6" r="2.7" fill="{helm_bg}" stroke="#000" stroke-width="0.6"/>'
        # マフラー (後輪後ろから少し出す)
        f'<rect x="2" y="15.5" width="6" height="1.5" fill="#888" rx="0.5"/>'
        f'</svg>'
    )


_bikes_html = "".join(
    f'<div class="bike-lane lane-{n}">{_autorace_bike_svg(n)}</div>'
    for n in range(1, 9)
)
_dusts_html = (
    '<span class="dust dust-1">💨</span>'
    '<span class="dust dust-2">💨</span>'
    '<span class="dust dust-3">💨</span>'
)
st.markdown(f'<div class="bike-track">{_dusts_html}{_bikes_html}</div>', unsafe_allow_html=True)

st.caption("中間モデル(直前) + 公式 AI 予想(前売) を時間で自動切替。5〜7 券種を提示します。")

# cloud モードはリプレイ機能を使わないので大型 CSV/parquet を読まない
# (data/walkforward_predictions_morning_top3.parquet, odds_summary.csv 等は git 管理外)
if IS_CLOUD:
    import pandas as _pd
    preds = _pd.DataFrame({"race_date": []})
    odds = _pd.DataFrame()
    pay = _pd.DataFrame()
    min_date = max_date = jst_today()
else:
    preds, odds, pay = load_data()
    min_date = preds["race_date"].min().date()
    max_date = preds["race_date"].max().date()

# サイドバー
with st.sidebar:
    st.header("設定")

    if IS_CLOUD:
        # 公開版: ライブ予想のみ (リプレイは個人ログ依存で重いので非表示)
        is_live_mode = True
        st.caption("📡 ライブ予想モード (公開版)")
    else:
        mode = st.radio(
            "モード",
            options=["📡 ライブ予想 (今日)", "📼 リプレイ (過去日)"],
            index=0,
            horizontal=False,
            help="ライブ=本番モデル+autorace.jp から今日のデータ取得 / リプレイ=OOF 予測+実績結果",
        )
        is_live_mode = mode.startswith("📡")

    if is_live_mode:
        # 今日の開催場だけ抽出 (進行中 + 終了の両方含む、終了は「(終了)」suffix で識別)
        with st.spinner("今日の開催場を確認中…"):
            open_pcs_with_state = fetch_today_open_venues()  # [(pc, is_finished), ...]
        if not open_pcs_with_state:
            st.error("⚠️ 今日は 5 場とも開催なしです。リプレイモードを使ってください。")
            st.stop()
        # selectbox label: 進行中はそのまま、終了は「(終了)」付き
        venue_options = [
            f"{VENUE_JP[pc]}{' (終了)' if is_fin else ''}"
            for pc, is_fin in open_pcs_with_state
        ]
        venue_label = st.selectbox("場を選ぶ (今日の開催場)", options=venue_options, index=0)
        # 選択された label から pc を逆引き
        selected_idx = venue_options.index(venue_label)
        pc = open_pcs_with_state[selected_idx][0]
        venue = VENUE_NAMES[pc]
        target_date = jst_today()
        n_active = sum(1 for _, fin in open_pcs_with_state if not fin)
        n_finished = sum(1 for _, fin in open_pcs_with_state if fin)
        cap = f"📅 {target_date} の開催場: 進行中 {n_active} / 終了 {n_finished}"
        st.caption(cap)
    else:
        venue_label = st.selectbox(
            "場を選ぶ",
            options=[VENUE_JP[pc] for pc in [3, 6, 5, 4, 2]],  # 伊勢崎 山陽 飯塚 浜松 川口 順
            index=0,
        )
        pc = NAME_TO_PC[
            next(name for name, jp in zip(VENUE_NAMES.values(), VENUE_JP.values()) if jp == venue_label)
        ]
        venue = VENUE_NAMES[pc]
    if not is_live_mode:
        # 該当場で開催実績ある日付に絞る (開催日のみ階層選択)
        venue_dates = preds[preds["place_code"] == pc]["race_date"].dt.date.drop_duplicates().sort_values()
        venue_dates_list = venue_dates.tolist()

        # 年→月→日 の階層
        years_avail = sorted({d.year for d in venue_dates_list}, reverse=True)
        sel_year = st.selectbox("年", years_avail, index=0)
        months_avail = sorted({d.month for d in venue_dates_list if d.year == sel_year}, reverse=True)
        sel_month = st.selectbox(
            "月",
            months_avail,
            index=0,
            format_func=lambda m: f"{m} 月 ({sum(1 for d in venue_dates_list if d.year==sel_year and d.month==m)} 日開催)",
        )
        days_avail = sorted([d for d in venue_dates_list if d.year == sel_year and d.month == sel_month])
        target_date = st.selectbox(
            "開催日",
            days_avail,
            index=len(days_avail) - 1,  # 月内最終日をデフォルト
            format_func=lambda d: f"{d.month}/{d.day} ({['月','火','水','木','金','土','日'][d.weekday()]})",
        )
        st.caption(f"{venue_label} の開催日数 (全期間): {len(venue_dates_list)} 日")
    # ライブ予想モードでは target_date は本日固定 (上で設定済)、追加 UI なし
    if is_live_mode:
        st.caption("⚠️ 各レースで API 2 回 × 12R = 約 12 秒かかります")

    selected_labels = st.multiselect(
        "購入する券種",
        options=[BET_LABELS[bt] for bt in BET_ORDER],
        default=[BET_LABELS[bt] for bt in BET_ORDER],
        help="チェックを外すとその券種は購入しない (1 日分一括)",
    )
    selected_bets = [bt for bt in BET_ORDER if BET_LABELS[bt] in selected_labels]
    if not selected_bets:
        st.warning("少なくとも 1 つは選んでください")
        st.stop()

    bet_amount = st.number_input(
        "1 券種あたり金額 (¥)", min_value=100, max_value=10000, value=100, step=100
    )
    recommend_thr = st.number_input(
        "💎 購入推奨 EV 閾値",
        min_value=1.0, max_value=3.0, value=1.50, step=0.05,
        help="top1 の ev_avg_calib がこの値以上で「💎 推奨」を表示。本番運用は 1.50。",
    )
    # 結果表示 checkbox はリプレイ専用 (ライブでは未走 R が結果なし、
    # 確定済 R は別ロジックで表示するため checkbox を出しても意味無し)
    if IS_CLOUD or is_live_mode:
        show_results = True
    else:
        show_results = st.checkbox("結果も表示する(リプレイなので答え合わせ)", value=True)

# メインエリア
target_ts = pd.Timestamp(target_date)

if is_live_mode:
    # API 再取得イベント: R 毎に発走 (-3min, 0min, +6min) の 3 タイミング。
    # 0min = レース開始時、+6min = 結果反映待ち余裕込み。
    # 起動時・場所変更時・更新ボタンは別経路で fetch される (cache miss / 手動 clear)。
    REFETCH_EVENTS_MIN = [-3, 0, 6]  # 発走時刻からのオフセット (分)
    HOT_WINDOW_MIN = 5               # この範囲に最近イベントがあれば「ホット」帯

    # 更新ボタン
    col_btn, col_now = st.columns([1, 4])
    with col_btn:
        if st.button("🔄 更新", help="autorace.jp から最新の状態を強制再取得"):
            st.cache_data.clear()
            st.session_state.pop(f"refreshed_set_{target_date}_{venue}", None)
            st.rerun()

    with st.spinner(f"autorace.jp から {target_date} {venue_label} のデータを取得中…"):
        race_start_times = fetch_race_start_times(str(target_date), pc)
        live_data = fetch_live_day(str(target_date), pc)

    # ── 各 R の発走時刻 → 絶対 datetime に解決 ──
    now_dt = jst_now()
    today_d = jst_today()
    race_start_dts: dict[int, dt.datetime] = {}
    for r_no, time_str in (race_start_times or {}).items():
        if not time_str:
            continue
        try:
            hh, mm = map(int, str(time_str).split(":"))
            # ミッドナイト "24:30" / "25:00" 表記正規化
            day_offset = 0
            if hh >= 24:
                hh -= 24
                day_offset = 1
            rs = dt.datetime.combine(
                today_d + dt.timedelta(days=day_offset), dt.time(hh, mm)
            )
        except (ValueError, AttributeError):
            continue
        # 深夜跨ぎ補正
        if rs < now_dt - dt.timedelta(hours=12):
            rs += dt.timedelta(days=1)
        race_start_dts[r_no] = rs

    # ── スマート refresh: 各 R で (-3, +1, +5) min イベントを 1 度ずつ発火 ──
    # 初回ロード時に過去イベントを大量に発火させて無限 fetch ループしないよう、
    # session_state 未初期化時は「現時点までの過去イベントを全て発火済」扱いにする
    # (今 fetch したデータが最新の状態を反映しているため再 fetch は不要)。
    refreshed_key = f"refreshed_set_{target_date}_{venue}"
    if refreshed_key not in st.session_state:
        initial: set = set()
        for r_no, race_start in race_start_dts.items():
            for offset in REFETCH_EVENTS_MIN:
                ev_at = race_start + dt.timedelta(minutes=offset)
                if now_dt >= ev_at:
                    initial.add((r_no, offset))
        st.session_state[refreshed_key] = initial
    refreshed_set: set = st.session_state[refreshed_key]

    for r_no, race_start in race_start_dts.items():
        for offset in REFETCH_EVENTS_MIN:
            ev_key = (r_no, offset)
            if ev_key in refreshed_set:
                continue
            ev_at = race_start + dt.timedelta(minutes=offset)
            if now_dt >= ev_at:
                refreshed_set.add(ev_key)
                st.cache_data.clear()
                label = f"-{-offset}min" if offset < 0 else f"+{offset}min"
                st.toast(f"🔄 R{r_no} 発走 {label}: 最新データに更新中…", icon="⚡")
                st.rerun()

    # ── アダプティブ rerun 間隔: ホット帯 30s / 通常 120s / 終了後 300s ──
    # 「ホット帯」= 直近の未発火イベントまで HOT_WINDOW_MIN 分以内 or
    # 直前のイベントから HOT_WINDOW_MIN 分以内
    next_event_dt: dt.datetime | None = None
    last_event_dt: dt.datetime | None = None
    for r_no, race_start in race_start_dts.items():
        for offset in REFETCH_EVENTS_MIN:
            ev_at = race_start + dt.timedelta(minutes=offset)
            if ev_at >= now_dt:
                if next_event_dt is None or ev_at < next_event_dt:
                    next_event_dt = ev_at
            else:
                if last_event_dt is None or ev_at > last_event_dt:
                    last_event_dt = ev_at

    if next_event_dt is None:
        # 全 R 終了 (これ以上のイベントなし)
        rerun_interval_ms = 300_000
        rerun_caption = "全レース終了 ／ 5 分毎に画面更新"
    else:
        sec_to_next = (next_event_dt - now_dt).total_seconds()
        sec_since_last = (now_dt - last_event_dt).total_seconds() if last_event_dt else 1e9
        is_hot = sec_to_next <= HOT_WINDOW_MIN * 60 or sec_since_last <= HOT_WINDOW_MIN * 60
        if is_hot:
            rerun_interval_ms = 60_000
            rerun_caption = "ホット帯: 60 秒毎に画面更新"
        else:
            rerun_interval_ms = 120_000
            rerun_caption = "2 分毎に画面更新"

    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=rerun_interval_ms, key=f"live_refresh_{venue}")

    now = jst_now()
    with col_now:
        st.caption(
            f"⏰ {now.strftime('%H:%M')} ／ {rerun_caption}、"
            f"各 R 発走 -3 / +1 / +5 min で API 再取得"
        )

    valid_races = [r for r, info in live_data.items() if info["top_cars"]]
    if not valid_races:
        st.error(f"{venue_label} {target_date}: program/AI 予想とも取得不可。開催なし or API エラー。")
        st.stop()

    races = sorted(live_data.keys())
    st.markdown(f"### 📡 ライブ予想  📅 {target_date} {venue_label} ({len(races)} レース)")

    # ヒーローカードのスペースを先に確保 (集計後に埋める)
    hero_placeholder = st.empty()
    st.markdown("---")

    # 次のレース番号を先に計算 (expander ハイライト用)
    next_race_no = None
    next_race_min = None
    next_race_time = None
    for r in races:
        info = live_data[r]
        if info["has_result"] or not info["top_cars"]:
            continue
        st_str = race_start_times.get(r)
        if not st_str:
            continue
        try:
            hh, mm = map(int, st_str.split(":"))
            day_offset = 0
            if hh >= 24:
                hh -= 24
                day_offset = 1
            start_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if day_offset:
                start_dt += dt.timedelta(days=day_offset)
            if start_dt > now:
                mins_to = int((start_dt - now).total_seconds() / 60)
                if next_race_min is None or mins_to < next_race_min:
                    next_race_min = mins_to
                    next_race_no = r
                    next_race_time = st_str
        except Exception:
            pass

    # 各レースを 4 状態で表示
    settled_cost, settled_refund = 0, 0  # 確定済みレースのみ集計
    pending_cost = 0  # 未走の予定投資 (参考用)
    biggest_win_amount = 0
    biggest_win_race = None
    biggest_payout = 0  # 単発で一番デカい払戻 (1 ベット分)
    biggest_payout_race = None
    biggest_payout_bt = None
    total_hits = 0
    total_picks_settled = 0
    n_recommended_settled = 0   # 推奨判定された確定済みレース数
    n_recommended_pending = 0   # 推奨判定された未走レース数
    recommended_settled_races: list[int] = []  # R 番号の昇順
    recommended_pending_races: list[int] = []
    for r in races:
        info = live_data[r]
        if not info["top_cars"]:
            continue
        start_time_str = race_start_times.get(r)
        badge, detail = race_status(start_time_str, now, info["has_odds"], info["has_result"])
        top_cars = info["top_cars"]
        picks = make_picks(top_cars)

        # ヘッダ表示
        time_label = f" 発走 {start_time_str}" if start_time_str else ""
        source_label = "中間モデル" if info["source"] == "middle_model" else "公式 AI 予想"

        # top1 の EV (中間モデル時のみ計算可)
        top1_ev = None
        if info["df"] is not None and "ev_avg_calib" in info["df"].columns:
            top1_row = info["df"].iloc[0]  # df は pred_calib 降順でソート済
            try:
                v = float(top1_row["ev_avg_calib"])
                if not pd.isna(v):
                    top1_ev = v
            except Exception:
                top1_ev = None

        # 発走 -5min 〜 発走時刻の間だけ推奨を出す
        # (それより前は drift bias で当てにならない、過去レースは推奨しても意味なし)
        within_5min_window = False
        if start_time_str:
            try:
                hh, mm = map(int, str(start_time_str).split(":"))
                day_offset = 0
                if hh >= 24:
                    hh -= 24
                    day_offset = 1
                race_start_dt = dt.datetime.combine(
                    jst_today() + dt.timedelta(days=day_offset), dt.time(hh, mm)
                )
                if race_start_dt < now - dt.timedelta(hours=12):
                    race_start_dt += dt.timedelta(days=1)
                # 発走 -5min から発走時刻まで (発走後はもう推奨対象外)
                within_5min_window = (
                    race_start_dt - dt.timedelta(minutes=5) <= now < race_start_dt
                )
            except (ValueError, AttributeError):
                pass

        # 推奨判定 (NaN は推奨しない、確定済 R も対象外)
        ev_above_thr = (top1_ev is not None) and (top1_ev >= recommend_thr)
        is_recommended = ev_above_thr and within_5min_window and not info["has_result"]
        if is_recommended:
            if info["has_result"]:
                n_recommended_settled += 1
                recommended_settled_races.append(r)
            else:
                n_recommended_pending += 1
                recommended_pending_races.append(r)

        ev_label = ""
        if top1_ev is not None:
            if is_recommended and not info["has_result"]:
                ev_mark = " 💎 推奨"  # 未走 + -5min 以降のみ推奨
            elif is_recommended and info["has_result"]:
                ev_mark = " 💎"       # 終了済は装飾のみ
            elif ev_above_thr and not within_5min_window and not info["has_result"]:
                ev_mark = " ⏳"       # -5min 待機中 (閾値超えだが時刻まだ早い)
            else:
                ev_mark = ""
            ev_label = f" EV {top1_ev:.2f}{ev_mark}"

        header_extra = f" | 予想 top1: {top_cars[0]}号{ev_label} ({source_label})"

        # 結果反映用の payout 情報を refund_info から構築
        race_refund = 0
        race_cost = 0
        result_rows = []

        # オッズ map (本予想時のみ)
        if info["df"] is not None:
            df = info["df"]
            win_odds_map = dict(zip(df["car_no"].astype(int), df.get("win_odds", pd.Series(dtype=float))))
            place_min_map = dict(zip(df["car_no"].astype(int), df.get("place_odds_min", pd.Series(dtype=float))))
            place_max_map = dict(zip(df["car_no"].astype(int), df.get("place_odds_max", pd.Series(dtype=float))))
        else:
            win_odds_map = place_min_map = place_max_map = {}

        # refundInfo を bet_type ごとに整理
        refund_by_bet = {}
        if info["refund_info"]:
            ri = info["refund_info"].get("refundInfo", {})
            if isinstance(ri, dict):
                refund_by_bet = ri

        # 実際の着順 (rt3 から取得。API 構造は
        # {'typeCode':..., 'list': [{'1thCarNo':1,'2thCarNo':4,'3thCarNo':7,'refund':930,...}]})
        actual_top3 = None
        if info["has_result"] and "rt3" in refund_by_bet:
            rt3_data = refund_by_bet["rt3"]
            rt3_list = rt3_data.get("list", []) if isinstance(rt3_data, dict) else []
            if rt3_list:
                entry = rt3_list[0]
                try:
                    actual_top3 = [int(entry.get(k)) for k in ("1thCarNo", "2thCarNo", "3thCarNo")
                                    if entry.get(k) not in (None, "")]
                    if len(actual_top3) < 3:
                        actual_top3 = None
                except Exception:
                    actual_top3 = None

        for bt in selected_bets:
            picked = picks.get(bt, [])
            if not picked:
                continue
            combo = fmt_combo(bt, picked)
            # 全券種オッズを raw API 経由で lookup (単勝・複勝以外も対応)
            od_s = lookup_odds(bt, picked, info.get("odds_lists", {}))
            # 結果判定 (API 構造の違い:
            #   tns/fns: {carNo, refund, ...}
            #   wid/rfw/rtw: {1thCarNo, 2thCarNo, refund, ...}
            #   rf3/rt3: {1thCarNo, 2thCarNo, 3thCarNo, refund, ...})
            hit, refund = False, 0.0
            if info["has_result"] and bt in refund_by_bet:
                bt_data = refund_by_bet[bt]
                entries = bt_data.get("list", []) if isinstance(bt_data, dict) else []
                for entry in entries:
                    cars_in_entry = []
                    if bt in ("tns", "fns"):
                        v = entry.get("carNo")
                        if v not in (None, "", 0):
                            try:
                                cars_in_entry.append(int(v))
                            except Exception:
                                pass
                    else:
                        for k in ("1thCarNo", "2thCarNo", "3thCarNo"):
                            v = entry.get(k)
                            if v not in (None, "", 0):
                                try:
                                    cars_in_entry.append(int(v))
                                except Exception:
                                    pass
                    # マッチング
                    if bt in ("tns", "fns"):
                        match = bool(cars_in_entry and cars_in_entry[0] == picked[0])
                    elif bt == "wid":
                        match = (sorted(cars_in_entry) == sorted(picked))
                    elif bt == "rfw":
                        match = (sorted(cars_in_entry) == sorted(picked))
                    elif bt == "rtw":
                        match = (cars_in_entry == picked)
                    elif bt == "rf3":
                        match = (sorted(cars_in_entry) == sorted(picked))
                    elif bt == "rt3":
                        match = (cars_in_entry == picked)
                    else:
                        match = False
                    if match:
                        hit = True
                        try:
                            refund += float(entry.get("refund", 0)) * (bet_amount / BET)
                        except Exception:
                            pass

            race_cost += bet_amount
            race_refund += refund
            # 単発払戻最大の更新 (🏆 ハイライト用)
            if hit and refund > biggest_payout:
                biggest_payout = refund
                biggest_payout_race = r
                biggest_payout_bt = BET_LABELS[bt]
            if info["has_result"]:
                total_picks_settled += 1
                if hit:
                    total_hits += 1
            # 結果セル: 的中なら ○、外れなら 実際の正解を併記
            if not info["has_result"]:
                result_cell = "—"
            elif hit:
                # 大配当(¥1000以上)は 🏆 でハイライト
                result_cell = "🏆 ○" if refund >= 1000 else "○"
            else:
                # 外れ: 実結果を bet_type に応じて表示
                # mobile では狭い列で truncate されるので「→」「実着順=」等は省略
                if actual_top3 and bt == "tns":
                    result_cell = f"✗ 1着 {actual_top3[0]}"
                elif actual_top3 and bt == "fns":
                    result_cell = f"✗ 3着 {','.join(str(c) for c in actual_top3)}"
                elif actual_top3 and bt == "wid":
                    result_cell = f"✗ 3着 {','.join(str(c) for c in actual_top3)}"
                elif actual_top3 and bt == "rfw":
                    result_cell = f"✗ 1-2 {'-'.join(str(c) for c in sorted(actual_top3[:2]))}"
                elif actual_top3 and bt == "rtw":
                    result_cell = f"✗ 1-2 {'-'.join(str(c) for c in actual_top3[:2])}"
                elif actual_top3 and bt == "rf3":
                    result_cell = f"✗ 3着 {'-'.join(str(c) for c in sorted(actual_top3))}"
                elif actual_top3 and bt == "rt3":
                    result_cell = f"✗ 着順 {'-'.join(str(c) for c in actual_top3)}"
                else:
                    result_cell = "✗"
            result_rows.append({
                "券種": BET_LABELS[bt],
                "買い目": combo,
                "オッズ": od_s,
                "結果": result_cell,
                "払戻": fmt_yen(refund) if info["has_result"] else "—",
            })

        if info["has_result"]:
            settled_cost += race_cost
            settled_refund += race_refund
            win_amount = race_refund - race_cost
            if win_amount > biggest_win_amount:
                biggest_win_amount = win_amount
                biggest_win_race = r
        else:
            pending_cost += race_cost

        # 折りたたみヘッダ
        race_summary_label = ""
        if info["has_result"]:
            actual_str = ""
            if actual_top3:
                actual_str = f" | 実: {actual_top3[0]}→{actual_top3[1]}→{actual_top3[2]}"
            race_summary_label = actual_str + " | 当日収支: " + (
                f"🟢 {fmt_yen(race_refund - race_cost)}"
                if race_refund > race_cost
                else f"🔴 {fmt_yen(race_refund - race_cost)}"
            )

        # 次のレースは 🔥 prefix + 自動展開
        is_next = (r == next_race_no)
        next_prefix = "🔥 NEXT  " if is_next else ""

        # 💎 購入推奨バナー (expander の前)
        if is_recommended and not info["has_result"]:
            urgent_part = (
                f'<span class="urgent">⚡ MAMONAKU !!</span>' if (is_next and next_race_min is not None and next_race_min <= 10)
                else ""
            )
            rec_yen = recommended_bet_yen(pc, r)
            st.markdown(
                f'<div class="recommend-banner">'
                f'<span class="gem">💎</span> BUY RECOMMENDED <span class="gem">💎</span>'
                f'&nbsp;&nbsp;R{r} {start_time_str or ""}&nbsp;&nbsp;'
                f'予想 {top_cars[0]} 号  EV <b>{top1_ev:.2f}</b>'
                f'&nbsp;&nbsp;<span style="color:#fff; background:#1565c0; padding:2px 8px; border-radius:4px; font-size:0.85em;">推奨額 ¥{rec_yen}</span>'
                f'&nbsp;&nbsp;{urgent_part}'
                f'</div>',
                unsafe_allow_html=True,
            )
            # オッズ確認 + 投票の 2 ボタン
            bcol1, bcol2 = st.columns(2)
            with bcol1:
                st.link_button(
                    f"📊 {venue} R{r} オッズを見る",
                    f"https://autorace.jp/race_info/Odds/{venue}/{target_date}/{r}",
                    use_container_width=True,
                )
            with bcol2:
                st.link_button(
                    f"🎯 投票へ (autorace.jp 公式)",
                    "https://vote.autorace.jp/",
                    use_container_width=True,
                )

        with st.expander(
            f"{next_prefix}R{r}{time_label}  {badge}  ({detail}){header_extra}{race_summary_label}",
            expanded=(is_next or is_recommended or badge.startswith("⏳")),
        ):
            c1, c2 = st.columns([1, 2])
            with c1:
                st.markdown(f"**予想 top 3** ({source_label})")
                if info["df"] is not None:
                    cols_show = ["car_no", "pred_calib"]
                    if "ev_avg_calib" in info["df"].columns:
                        cols_show.append("ev_avg_calib")
                    top3 = info["df"].head(3)[cols_show].copy()
                    top3["car_no"] = top3["car_no"].astype(int)
                    top3["pred_calib"] = top3["pred_calib"].round(3)
                    rename_map = {"car_no": "車", "pred_calib": "pred"}
                    if "ev_avg_calib" in top3.columns:
                        top3["ev_avg_calib"] = top3["ev_avg_calib"].round(2)
                        rename_map["ev_avg_calib"] = "EV"
                    top3 = top3.rename(columns=rename_map)
                    st.dataframe(top3, hide_index=True, use_container_width=True)
                else:
                    st.dataframe(
                        pd.DataFrame({"車": top_cars, "順位": ["◎ 本命", "○ 対抗", "▲ 単穴"][:len(top_cars)]}),
                        hide_index=True, use_container_width=True,
                    )
            with c2:
                n_bets = len(selected_bets)
                st.markdown(f"**買い目** ({fmt_yen(bet_amount)} × {n_bets} = {fmt_yen(bet_amount*n_bets)})")
                st.dataframe(pd.DataFrame(result_rows), hide_index=True, use_container_width=True)

    # ── ヒーローカードを最上段に充填 ──
    n_settled = sum(1 for info in live_data.values() if info["has_result"])
    profit = settled_refund - settled_cost
    roi = settled_refund / settled_cost if settled_cost > 0 else 0

    with hero_placeholder.container():
        st.markdown("#### 🏁 本日の戦況")
        # 進捗バー
        if races:
            st.progress(
                n_settled / len(races),
                text=f"進捗: {n_settled} / {len(races)} レース確定 ／ 投票残 {len(races) - n_settled} R",
            )
        # 推奨ティッカー (未走推奨があれば点滅バー)
        if n_recommended_pending > 0:
            st.markdown(
                f'<div class="rec-ticker">'
                f'💎 BUY 推奨 {n_recommended_pending} レース あり！'
                f' (EV ≥ {recommend_thr:.2f})  '
                f'下のレースカードを確認してください'
                f'</div>',
                unsafe_allow_html=True,
            )
            # 一度だけ風船演出 (session_state で過剰発火を抑制)
            sig = f"{target_date}_{venue}_{n_recommended_pending}"
            if st.session_state.get("balloon_sig") != sig:
                st.balloons()
                st.session_state["balloon_sig"] = sig

        cols = st.columns(5)
        with cols[0]:
            if n_settled > 0:
                profit_emoji = "🟢" if profit > 0 else ("🔴" if profit < 0 else "⚪")
                st.metric(
                    f"💰 当日収支 {profit_emoji}",
                    fmt_yen(profit),
                    delta=f"ROI {roi*100:.1f}%" if settled_cost else None,
                )
                st.caption(
                    f"投資 {fmt_yen(settled_cost)} → 払戻 {fmt_yen(settled_refund)}"
                )
            else:
                st.metric("💰 当日収支", "—", help="まだ確定レースなし")
        with cols[1]:
            if total_picks_settled > 0:
                hit_rate = total_hits / total_picks_settled * 100
                st.metric("🎯 的中数", f"{total_hits} / {total_picks_settled}",
                          delta=f"{hit_rate:.0f}%")
            else:
                st.metric("🎯 的中数", "—")
        with cols[2]:
            if biggest_payout > 0:
                st.metric(
                    "🏆 最大払戻",
                    fmt_yen(biggest_payout),
                    delta=f"R{biggest_payout_race} {biggest_payout_bt}",
                    delta_color="off",
                )
            else:
                st.metric("🏆 最大払戻", "—")
        with cols[3]:
            if next_race_no:
                badge_txt = "🔥 まもなく!" if next_race_min <= 10 else (
                    "⏳ 直前予想中" if next_race_min <= 60 else "🔮 前売段階"
                )
                st.metric(
                    f"⏰ 次のレース",
                    f"R{next_race_no}  {next_race_time}",
                    delta=f"{next_race_min} 分後  {badge_txt}",
                    delta_color="off",
                )
            else:
                st.metric("⏰ 次のレース", "🏁 全終了")
        with cols[4]:
            label = "💎 BUY 推奨"
            if n_recommended_pending > 0:
                # 未走で推奨されてるレースがある = actionable
                pending_rs = ", ".join(f"R{r}" for r in sorted(recommended_pending_races))
                value_txt = (
                    f"{pending_rs}"
                    if n_recommended_pending == 1
                    else f"{n_recommended_pending} R ({pending_rs})"
                )
                st.metric(
                    label,
                    value_txt,
                    delta=f"今すぐ投票検討",
                    delta_color="off",
                )
            else:
                # 未走推奨なし
                st.metric(label, "—", help=f"EV ≥ {recommend_thr:.2f} の未走レース")
            # 終了済の EV 高水準は補助情報として caption
            if n_recommended_settled > 0:
                settled_rs = ", ".join(f"R{r}" for r in sorted(recommended_settled_races))
                st.caption(
                    f"📊 振り返り: 終了 R で EV≥{recommend_thr:.2f} だった R は {settled_rs}"
                    f"({n_recommended_settled}件)"
                )

        if pending_cost > 0 and n_settled > 0:
            st.caption(f"💸 未走レース予定投資: {fmt_yen(pending_cost)}（確定すると上の収支に加算）")

    st.markdown("---")
    st.caption(
        "🎮 エンタメ用途 ／ 本予想=中間モデル(直前オッズ) ／ 暫定予想=公式 AI ／ "
        "前売予想=前売オッズ参考。投票は現地・公式投票サイトから手動で。"
    )
    st.stop()  # ライブモードはここで終了 (リプレイ用の表示はスキップ)
else:
    day_preds = preds[(preds["race_date"] == target_ts) & (preds["place_code"] == pc)]
    day_odds = odds[(odds["race_date"] == target_ts) & (odds["place_code"] == pc)]
    day_pay = pay[(pay["race_date"] == target_ts) & (pay["place_code"] == pc)]

if day_preds.empty:
    st.error(f"{venue_label} は {target_date} に開催なし。サイドバーで別の日を選んでください。")
    st.stop()

# リプレイモードでは day_preds (OOF) に odds が無いので merge する
if not is_live_mode:
    day_preds = day_preds.merge(
        day_odds[["race_no", "car_no", "win_odds", "place_odds_min", "place_odds_max"]],
        on=["race_no", "car_no"], how="left",
    )

races = sorted(day_preds["race_no"].unique())
mode_badge = "📡 ライブ予想" if is_live_mode else "📼 リプレイ"
st.markdown(f"### {mode_badge}  📅 {target_date} {venue_label} ({len(races)} レース)")

grand_cost = 0
grand_refund = 0
race_summaries = []

for r in races:
    race_preds = day_preds[day_preds["race_no"] == r].sort_values("pred_calib", ascending=False)
    top_cars = [int(c) for c in race_preds["car_no"].tolist()[:3]]
    picks = make_picks(top_cars)
    # オッズ map (リプレイ/ライブ共通: day_preds に既に odds 列がある前提)
    win_odds_map = dict(zip(race_preds["car_no"].astype(int), race_preds.get("win_odds", pd.Series(dtype=float))))
    place_min_map = dict(zip(race_preds["car_no"].astype(int), race_preds.get("place_odds_min", pd.Series(dtype=float))))
    place_max_map = dict(zip(race_preds["car_no"].astype(int), race_preds.get("place_odds_max", pd.Series(dtype=float))))
    race_pay = day_pay[day_pay["race_no"] == r] if not day_pay.empty else pd.DataFrame()

    race_cost = 0
    race_refund = 0
    rows = []
    for bt in selected_bets:
        picked = picks.get(bt, [])
        if not picked:
            continue
        combo = fmt_combo(bt, picked)
        if bt == "tns":
            od = win_odds_map.get(picked[0])
            od_s = f"{od:.1f}" if pd.notna(od) else "—"
        elif bt == "fns":
            pmn = place_min_map.get(picked[0])
            pmx = place_max_map.get(picked[0])
            od_s = f"{pmn:.1f}-{pmx:.1f}" if pd.notna(pmn) else "—"
        else:
            od_s = "—"
        bt_pay = race_pay[race_pay["bet_type"] == bt]
        hit, refund = check_hit(bt, picked, bt_pay)
        race_cost += bet_amount
        race_refund += refund * (bet_amount / BET)  # 100円単位の payout を bet_amount に換算
        rows.append({
            "券種": BET_LABELS[bt],
            "買い目": combo,
            "オッズ": od_s,
            "結果": "○" if hit else "✗" if show_results else "—",
            "払戻": fmt_yen(refund * (bet_amount / BET)) if show_results else "—",
        })
    grand_cost += race_cost
    grand_refund += race_refund

    race_profit = race_refund - race_cost
    summary_emoji = "🟢" if race_profit > 0 else ("🔴" if race_profit < 0 else "⚪")
    race_summaries.append({
        "race": r, "profit": race_profit, "cost": race_cost, "refund": race_refund,
    })

    with st.expander(
        f"R{r}  予測 top1: {top_cars[0]}号  "
        + (f"|  当日収支: {summary_emoji} {fmt_yen(race_profit)}" if show_results else ""),
        expanded=(r == 1),
    ):
        # 上位 3 車
        c1, c2 = st.columns([1, 2])
        with c1:
            st.markdown("**予測 top 3**")
            top3_df = race_preds.head(3)[["car_no", "pred_calib"]].copy()
            top3_df["car_no"] = top3_df["car_no"].astype(int)
            top3_df["pred_calib"] = top3_df["pred_calib"].round(3)
            top3_df.columns = ["車", "pred"]
            st.dataframe(top3_df, hide_index=True, use_container_width=True)
        with c2:
            n_bets = len(selected_bets)
            st.markdown(f"**買い目** ({fmt_yen(bet_amount)} × {n_bets} = {fmt_yen(bet_amount*n_bets)})")
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

# 1日サマリ
st.markdown("---")
if show_results:
    profit = grand_refund - grand_cost
    roi = grand_refund / grand_cost if grand_cost else 0
    cols = st.columns(4)
    cols[0].metric("投資合計", fmt_yen(grand_cost))
    cols[1].metric("払戻合計", fmt_yen(grand_refund))
    cols[2].metric("収支", fmt_yen(profit), delta=f"{(roi-1)*100:+.1f}%")
    cols[3].metric("ROI", f"{roi*100:.1f}%")

    # レース別収支グラフ
    st.markdown("### レース別 当日収支")
    chart_df = pd.DataFrame(race_summaries)
    chart_df["cum_profit"] = chart_df["profit"].cumsum()
    chart_df["race_label"] = chart_df["race"].apply(lambda x: f"R{x}")
    st.bar_chart(chart_df.set_index("race_label")["profit"])
    st.line_chart(chart_df.set_index("race_label")["cum_profit"])
else:
    n_bets = len(selected_bets)
    st.info(f"投資総額: {fmt_yen(grand_cost)} ({len(races)} R × {n_bets} 券種 × {fmt_yen(bet_amount)})")

st.markdown("---")
st.caption(
    "⚠️ エンタメ用途。控除率 25% のため長期 ROI は 80% 前後 (=月10%程度の損失)。"
    "ガチ勝負は本番運用 (top1 + EV>=1.50 + 複勝) を使うこと。"
)
