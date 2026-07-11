"""日次予想スクリプト: 中間モデルで当日対象場の買い候補を計算 + メール送信

使い方:
  python daily_predict.py --venues 2 3 4              # 朝(daytime 3場)
  python daily_predict.py --venues 5                  # 昼(飯塚)
  python daily_predict.py --venues 2 3 4 6 --time-slot morning   # 山陽を含めて朝 slot
  python daily_predict.py --venues 6   --time-slot evening       # 夕(山陽ミッドナイト)
  python daily_predict.py --venues 2 3 4 --no-email   # dry-run
  python daily_predict.py --venues 5 --thr 1.30       # 閾値変更
  python daily_predict.py --venues 5 --date 2026-04-29  # 日付指定

設計:
- 中間モデル(オッズあり・試走なし、AUC 0.80)
- 校正: isotonic regression(walk-forward 予測で fit 済み)
- 選別: 各レースで予測 top-1 車 × ev_avg_calib >= thr(default 1.50)
  ※ 2026-05-31 ev_threshold_sweep で 2.00 (ROI効率185%) も検討したが、総利益と
    賭け機会(本数)を優先して 1.50 (ROI136%・総利益¥68,510・〜2.5本/日) を維持。
- 出力: ログ + メール(候補ありの場合のみ)
- --time-slot: Hold/Today の liveStartTime に応じて --venues を動的フィルタ
  morning=<13:00, noon=13:00-17:00, evening=>=17:00。山陽の開催形態
  (通常/ナイター/ミッドナイト)に追従。履歴日(--date 過去)では無効。

依存ファイル:
- data/production_model.lgb
- data/production_calib.pkl
- data/production_meta.json

予測時の特徴量エンジニアリングは ml/features.py の build() に揃える(中間モデル用)。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pickle
import subprocess
import sys
import time
import traceback
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.client import AutoraceClient, VENUE_CODES
from src.parser import (
    parse_program_entries, parse_program_stats, parse_odds_summary,
    parse_odds_combo,
)
from src.storage import append_rows

DATA = ROOT / "data"
LOG_FILE = DATA / "daily_predict.log"


def _snapshot_combo_odds(place_code: int, race_date: str, race_no: int,
                         odds_body: dict) -> None:
    """発火時の連勝式オッズ板を odds_combo_snapshots.csv に記録。

    closing オッズ (odds_combo.csv) との drift 検証用。
    記録失敗しても予測フローは止めない。
    """
    try:
        rows = parse_odds_combo(place_code, race_date, race_no, odds_body)
        ts = dt.datetime.now().isoformat(timespec="seconds")
        for r in rows:
            r["captured_at"] = ts
        append_rows("odds_combo_snapshots.csv", rows)
    except Exception as e:  # noqa: BLE001 — 観測系の失敗は本体に影響させない
        logging.warning("combo odds snapshot failed (%s %s R%d): %s",
                        race_date, place_code, race_no, e)


def _snapshot_weather(client: AutoraceClient, place_code: int,
                      race_date: str, race_no: int) -> None:
    """発火時点の気象 (温度/湿度/天候) を Hold/Today から記録。

    Hold/Today はリアルタイム値のみで過去日は取れないため、発火時に取るのが唯一の手段。
    記録失敗しても予測フローは止めない。
    """
    try:
        hold = client.get_today_hold()
        for v in (hold.get("body") or {}).get("today", []):
            if int(v.get("placeCode", -1)) == int(place_code):
                append_rows("weather_snapshots.csv", [{
                    "race_date": race_date,
                    "place_code": place_code,
                    "race_no": race_no,
                    "temp": v.get("temp"),
                    "humid": v.get("humid"),
                    "roadtemp": v.get("roadtemp"),  # 走路温度
                    "weather": v.get("weather"),
                    "weather_code": v.get("weatherCode"),
                    "situation_code": v.get("situationCode"),
                    "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
                }])
                return
    except Exception as e:  # noqa: BLE001
        logging.warning("weather snapshot failed (%s %s R%d): %s",
                        race_date, place_code, race_no, e)
PRODUCTION_LOG = DATA / "daily_predict_picks.csv"
ODDS_SNAPSHOT_LOG = DATA / "odds_snapshots.csv"  # 発火時オッズスナップ (信号 persistence 解析用)
SHADOW_LOG = DATA / "shadow_picks.csv"  # shadow 判定ログ (drift 補正 / 閾値変更の仮想評価)
SANYO_RF3_PAPER_LOG = DATA / "sanyo_rf3_paper.csv"  # 山陽 三連複 paper trading (観測のみ、投票せず)
SANYO_PLACE_CODE = 6  # 山陽 (過去検証で rf3 のみ構造的 edge: 月勝率40% median84%)
EXPECTED_VOTES_CSV = DATA / "expected_votes.csv"  # 場×R別 typical 票数 → 推奨ベット額算出

# 三連系推奨: ev_avg_calib >= 1.80 の場合に発動
# 過去検証 ev_3point_by_place / ev_3point_policy_sim (2026-06-09 rt2_rf4 policy):
#   RT3(三連単): 浜松 ROI 530% / 山陽 ROI 141%
#   RF3(三連複): 伊勢崎 ROI 377% / 浜松 ROI 330% / 飯塚 ROI 114% / 山陽 ROI 185%
#   ※ 上記 sim ROI は odds_summary 過大オッズ由来の楽観バイアスあり (2026-06-28 確認)
# 購入対象は src/strategy_config.py の THREE_POINT_POLICY が正本 (監査 P1-3 対策)。
# 2026-07-11: 飯塚(5) を RF3 から除外 (実弾 0/17)。設定変更は strategy_config で。
from src.strategy_config import (  # noqa: E402
    RT3_ELIGIBLE_PLACES, RF3_ELIGIBLE_PLACES,
)
# 全場・全期間 絶対損失バックストップ (-¥10,000, sticky)。2026-07-11 監査 P2。
from src.backstop import backstop_active, enforce_backstop  # noqa: E402
RT3_THR = 1.80
RT3_PAPER_LOG = DATA / "rt3_paper.csv"
# weekly_status の停止基準が発動すると書かれる kill-switch。
# 存在する間は三連系まとめ買い(購入)を停止 (複勝・参考メール表示は継続)。
RT3_STOP_FLAG = DATA / "rt3_stop.flag"


def rt3_buy_active() -> bool:
    """三連系まとめ買い(購入)が現在有効か。いずれかのフラグ ON なら停止。

    - data/rt3_stop.flag         : kill-switch (現役ポリシー健全性、選択肢 B)
    - data/rt3_backstop_stop.flag: 全場・全期間 絶対損失バックストップ (sticky)
    """
    return (RT3_BUY_ENABLED
            and not RT3_STOP_FLAG.exists()
            and not backstop_active())
# 三連系まとめ買い (複勝+三連単+三連複 を 1 ボタンで購入) の click-to-buy 有効化フラグ。
# 2026-05-30: 浜松 R7 で本番テスト成功 (複勝5¥300 + 三連単5-6-7¥100 +
# 三連複5=6=7¥100 = ¥500 投票受付完了) を確認し True に。
# False に戻すと浜松・山陽でも従来通り「複勝のみ」の購入ボタンになる。
RT3_BUY_ENABLED = True

# Shadow 判定用パラメータ (本番には影響しない、記録のみ)
SHADOW_DRIFT_FACTORS = [0.70, 0.80]  # close_ev_est = fire_ev * factor
SHADOW_THRESHOLDS = [1.80, 2.00]     # 代替閾値


def _load_votes_lookup() -> dict[tuple[int, int], int]:
    """expected_votes.csv → {(place_code, race_no): rec_yen_10pct} dict"""
    if not EXPECTED_VOTES_CSV.exists():
        return {}
    try:
        df = pd.read_csv(EXPECTED_VOTES_CSV)
        return {
            (int(r["place_code"]), int(r["race_no"])): int(r["rec_yen_10pct"])
            for _, r in df.iterrows()
        }
    except Exception:
        return {}


_VOTES_LOOKUP = None


def recommended_bet_yen(place_code: int, race_no: int) -> int:
    """場×R 別の推奨ベット額 (オッズ低下 ≦ 10%、100円単位)。
    expected_votes.csv が無い or 行が無ければ ¥100 を返す。
    """
    global _VOTES_LOOKUP
    if _VOTES_LOOKUP is None:
        _VOTES_LOOKUP = _load_votes_lookup()
    return max(100, _VOTES_LOOKUP.get((place_code, race_no), 100))


def cumulative_performance() -> dict | None:
    """data/daily_predict_picks.csv + payouts.csv から累積運用成績を集計。

    戻り値 dict:
      start_date, end_date, n_total, n_settled, n_pending,
      n_hits, hit_rate, cost, payout, profit, roi
    データ無ければ None。
    """
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from picks_audit import load_picks, attach_results, summarize  # noqa: WPS433
        from ehi_monitor import calculate_ehi  # noqa: WPS433
        picks = load_picks()
        if picks.empty:
            ehi = calculate_ehi(7)
            return {"ehi": ehi}
        audit = attach_results(picks)
        summary = summarize(audit)
        summary["start_date"] = audit["race_date"].min().date()
        summary["end_date"] = audit["race_date"].max().date()
        summary["ehi"] = calculate_ehi(7)
        return summary
    except Exception as e:
        logging.warning("cumulative_performance() 失敗: %s", e)
        return None


def render_cumulative_text(perf: dict) -> str:
    """累積成績の text 版 (メール末尾用)。"""
    if perf is None:
        return ""
    
    lines = ["================================================"]
    
    ehi = perf.get("ehi")
    if ehi and ehi.get("ehi") is not None:
        lines.append(f"🛡️ Edge Health Index (7d): {ehi['ehi']} {ehi['emoji']} {ehi['status']}")
    
    if perf.get("n_total", 0) > 0:
        lines.append(f"📊 本番運用累積成績 (運用開始 {perf['start_date']}〜{perf['end_date']})")
        lines.append(
            f"   戦績: {perf['n_hits']} 勝 / {perf['n_settled']} 確定 = {perf['hit_rate']*100:.1f}%"
            f" / 累積¥{perf['profit']:+,.0f} / ROI {perf['roi']*100:.1f}%"
        )
        lines.append(f"   未確定: {perf['n_pending']} 件 (本日分など、結果待ち)")
    
    lines.append("================================================")
    return "\n".join(lines)


def render_cumulative_html(perf: dict) -> str:
    """累積成績の HTML 版 (メール末尾用)。"""
    if perf is None:
        return ""
    
    ehi = perf.get("ehi")
    ehi_html = ""
    if ehi and ehi.get("ehi") is not None:
        ehi_color = ehi.get("color", "#666")
        ehi_html = (
            f'<div style="margin-bottom:8px; font-weight:bold;">'
            f'🛡️ Edge Health Index (7d): <span style="color:{ehi_color};">'
            f'{ehi["ehi"]} {ehi["emoji"]} {ehi["status"]}</span></div>'
        )

    perf_html = ""
    if perf.get("n_total", 0) > 0:
        profit_color = "#2e7d32" if perf["profit"] >= 0 else "#c62828"
        perf_html = (
            f'<div style="font-weight:bold; color:#1565c0; margin-bottom:6px;">'
            f'📊 本番運用累積成績 '
            f'<span style="color:#666; font-weight:normal; font-size:11px;">'
            f'({perf["start_date"]}〜{perf["end_date"]})</span></div>'
            f'<div>戦績: <b>{perf["n_hits"]}</b> 勝 / <b>{perf["n_settled"]}</b> 確定 = '
            f'<b>{perf["hit_rate"]*100:.1f}%</b>'
            f' &nbsp;|&nbsp; 累積収支: <b style="color:{profit_color};">'
            f'¥{perf["profit"]:+,.0f}</b>'
            f' &nbsp;|&nbsp; ROI <b>{perf["roi"]*100:.1f}%</b></div>'
            f'<div style="color:#888; font-size:11px; margin-top:4px;">'
            f'未確定: {perf["n_pending"]} 件 (本日分など結果待ち)</div>'
        )

    if not ehi_html and not perf_html:
        return ""

    return (
        '<div style="margin-top:20px; padding:12px 16px; '
        'background:#f0f7ff; border-left:4px solid #1565c0; '
        'border-radius:4px; font-family:Arial,sans-serif; font-size:13px;">'
        f'{ehi_html}{perf_html}'
        '</div>'
    )


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ─── 特徴量エンジニアリング(ml/features.py と同じロジックを per-race で) ──

RACE_KEY = ["race_date", "place_code", "race_no"]
CAR_KEY = RACE_KEY + ["car_no"]


def _engineer_entries(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    rank_split = df["rank"].fillna("X-0").astype(str).str.split("-", n=1, expand=True)
    df["rank_class"] = rank_split[0]
    df["rank_num"] = pd.to_numeric(rank_split[1], errors="coerce")
    df["race_dev_num"] = pd.to_numeric(df["race_dev"], errors="coerce")
    df["is_absent"] = df["absent"].notna().astype(int)
    df["has_trial"] = df["trial_run_time"].notna().astype(int)
    return df


def _engineer_race_context(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    grp = df.groupby(RACE_KEY)
    df["race_handicap_max"] = grp["handicap"].transform("max")
    df["race_handicap_min"] = grp["handicap"].transform("min")
    df["race_trial_min"] = grp["trial_run_time"].transform("min")
    df["race_trial_mean"] = grp["trial_run_time"].transform("mean")
    df["race_n_cars"] = grp["car_no"].transform("count")
    df["race_n_absent"] = grp["is_absent"].transform("sum")
    df["handicap_diff_min"] = df["handicap"] - df["race_handicap_min"]
    df["trial_diff_min"] = df["trial_run_time"] - df["race_trial_min"]
    df["trial_diff_mean"] = df["trial_run_time"] - df["race_trial_mean"]
    return df


def _engineer_odds(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # parser が None を返す列があるので numeric 化して NaN にしてから log1p。
    for col in ("win_odds", "place_odds_min", "place_odds_max"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["log_win_odds"] = np.log1p(df["win_odds"])
    df["log_place_odds_min"] = np.log1p(df["place_odds_min"])
    df["log_place_odds_max"] = np.log1p(df["place_odds_max"])
    grp = df.groupby(RACE_KEY)
    df["win_odds_rank"] = grp["win_odds"].rank(method="min")
    df["place_odds_min_rank"] = grp["place_odds_min"].rank(method="min")
    df["win_implied_prob"] = 1.0 / df["win_odds"]
    return df


def build_features_for_race(
    place_code: int, race_date: str, race_no: int,
    program_body: dict, odds_body: dict,
) -> pd.DataFrame:
    """1 レース分の生 JSON body から特徴量 DataFrame を構築。"""
    entries_rows = parse_program_entries(place_code, race_date, race_no, program_body)
    stats_rows = parse_program_stats(place_code, race_date, race_no, program_body)
    odds_rows = parse_odds_summary(place_code, race_date, race_no, odds_body)

    if not entries_rows:
        return pd.DataFrame()

    entries = pd.DataFrame(entries_rows)
    entries["race_date"] = pd.to_datetime(entries["race_date"])
    entries = _engineer_entries(entries)
    entries = _engineer_race_context(entries)

    stats = pd.DataFrame(stats_rows)
    stats["race_date"] = pd.to_datetime(stats["race_date"])

    odds = pd.DataFrame(odds_rows) if odds_rows else pd.DataFrame()
    if not odds.empty:
        odds["race_date"] = pd.to_datetime(odds["race_date"])
        odds = _engineer_odds(odds)

    feat = entries.merge(
        stats.drop(columns=["player_code"], errors="ignore"),
        on=CAR_KEY, how="left",
    )
    if not odds.empty:
        odds_drop = ["player_code", "st_ave", "good_track_trial_ave",
                     "good_track_race_ave", "good_track_race_best"]
        feat = feat.merge(
            odds.drop(columns=[c for c in odds_drop if c in odds.columns]),
            on=CAR_KEY, how="left",
        )
    feat["year"] = feat["race_date"].dt.year
    feat["month"] = feat["race_date"].dt.month
    feat["dow"] = feat["race_date"].dt.dayofweek
    return feat


# ─── 推論 ─────────────────────────────────────────────────────

def load_production():
    model = lgb.Booster(model_file=str(DATA / "production_model.lgb"))
    with open(DATA / "production_calib.pkl", "rb") as f:
        iso = pickle.load(f)
    with open(DATA / "production_meta.json", "r", encoding="utf-8") as f:
        meta = json.load(f)
    return model, iso, meta


SLOT_BOUNDS: dict[str, tuple[str, str]] = {
    "morning": ("00:00", "13:00"),
    "noon":    ("13:00", "17:00"),
    "evening": ("17:00", "29:59"),  # 翌日早朝までを 17:00 起点で吸収
}


def _in_slot(start_time: str | None, slot: str) -> bool:
    """liveStartTime ('HH:MM') が指定 slot の範囲内か。
    None / 不正値は True(=フィルタ素通し)。"""
    if not start_time or not slot:
        return True
    lo, hi = SLOT_BOUNDS[slot]
    return lo <= start_time < hi


def fetch_today_schedule(client: AutoraceClient) -> dict[int, dict]:
    """Hold/Today から place_code 別の開催情報を返す。

    キー: liveStartTime / liveEndTime / nighterCode / nighterName /
          lastNightProgramFlg / finalRaceNo / nowRaceNo / oddsRaceNo /
          raceStartTime / gradeName / title / cancelFlg
    body.today[] のみ対象(明日以降は body.next[] にいる)。

    raceStartTime は nowRaceNo が指すレースの実発走時刻('HH:MM')。
    liveStartTime(放送開始)とは別物で約30分遅い。動的発火時刻計算に必須。
    """
    try:
        resp = client.get_today_hold()
    except Exception as e:
        logging.warning("Hold/Today 取得失敗: %s", e)
        return {}
    body = resp.get("body", {}) or {}
    today_list = body.get("today", []) if isinstance(body, dict) else []
    out: dict[int, dict] = {}
    for h in today_list:
        pc = h.get("placeCode")
        if pc is None:
            continue
        out[int(pc)] = {
            "liveStartTime": h.get("liveStartTime"),
            "liveEndTime": h.get("liveEndTime"),
            "nighterCode": h.get("nighterCode"),
            "nighterName": h.get("nighterName"),
            "lastNightProgramFlg": h.get("lastNightProgramFlg"),
            "finalRaceNo": h.get("finalRaceNo"),
            "nowRaceNo": h.get("nowRaceNo"),
            "oddsRaceNo": h.get("oddsRaceNo"),
            "raceStartTime": h.get("raceStartTime"),
            "gradeName": h.get("gradeName"),
            "title": h.get("title"),
            "cancelFlg": h.get("cancelFlg"),
        }
    return out


def align_features(df: pd.DataFrame, meta: dict) -> pd.DataFrame:
    cols = meta["feature_columns"]
    cat = set(meta["categorical"])
    out = pd.DataFrame(index=df.index)
    for c in cols:
        if c in df.columns:
            out[c] = df[c]
        else:
            out[c] = np.nan
    for c in cat:
        if c in out.columns:
            out[c] = out[c].astype("category")
    for c in out.select_dtypes(include=["object", "string"]).columns:
        out[c] = out[c].astype("category")
    return out


PREDICT_RETRY_MAX = 0          # near-miss retry 廃止 (LEAD_MIN=4 で締切 2 分前到着を優先)
PREDICT_RETRY_SLEEP_SEC = 30   # 未使用 (PREDICT_RETRY_MAX=0)


def predict_race(
    client: AutoraceClient, model: lgb.Booster, iso, meta,
    place_code: int, race_date: str, race_no: int,
) -> pd.DataFrame:
    """1 レース分の予測 + EV 計算結果を返す(空 DataFrame なら対象外)。

    top-1 (pred 1位) の ev_avg_calib が NaN (= 人気車の複勝オッズが
    まだ未確定で API センチネル 0.0/0.0) の場合、最大 PREDICT_RETRY_MAX 回
    PREDICT_RETRY_SLEEP_SEC 秒待って odds を再取得する。1 回でも有効な
    数値が取れれば以降の集計に使う。
    """
    try:
        prog = client.get_program(place_code, race_date, race_no)
        if prog.get("result") != "Success":
            return pd.DataFrame()
        body = prog.get("body", {})
        if isinstance(body, list) or not body.get("playerList"):
            return pd.DataFrame()

        feat = pd.DataFrame()
        for attempt in range(PREDICT_RETRY_MAX + 1):
            odds_resp = client.get_odds(place_code, race_date, race_no)
            odds_body = odds_resp.get("body", {})
            if isinstance(odds_body, list):
                return pd.DataFrame()
            # オッズ未公開(早朝)は tnsOddsList が list/空 dict で返る。
            # EV ベース戦略はオッズなしでは計算不能なので、ここで早期 return。
            tns = odds_body.get("tnsOddsList") if isinstance(odds_body, dict) else None
            if not isinstance(tns, dict) or not tns:
                logging.info("predict_race(%d, %s, %d): odds 未公開 — skip",
                             place_code, race_date, race_no)
                return pd.DataFrame()

            feat = build_features_for_race(place_code, race_date, race_no, body, odds_body)
            if feat.empty:
                return pd.DataFrame()
            feat = feat[feat["is_absent"] == 0].copy()
            if feat.empty:
                return pd.DataFrame()

            X = align_features(feat, meta)
            feat["pred"] = model.predict(X)
            feat["pred_calib"] = iso.transform(feat["pred"].values)
            # 異常 odds を NaN 化 (2 パターン)。
            # 異常時は NaN にすることで near-miss retry に再評価させる。
            #   1. max > 50: min=1.0/max=183 のセンチネル
            #      (複勝 odds は実質 30 倍程度が上限)
            #   2. max/min > 20: 過渡期 snapshot (通常は 5x 以内)
            # 旧条件 3 (min<1.1 AND max<1.1) は 2026-05-06 R7 の live データで誤検出
            # と判明したため削除:
            #   - R7 三連複 4-5-6 が odds 1.0 で ¥100 払戻されており、1.0-1.0 は
            #     正規の元返し圏(本命に集中投票時の minimum 保証)
            #   - 複勝のみ条件 3 で NaN 化されると、的中時 payout 計算が抜ける
            #   - R7 6号 (3着) の複勝 ¥100 が機会損失として実観測された
            ODDS_MAX_CAP = 50.0
            ODDS_RATIO_CAP = 20.0
            odds_min = feat["place_odds_min"]
            odds_max = feat["place_odds_max"]
            anomalous = (
                (odds_max > ODDS_MAX_CAP)
                | ((odds_min > 0) & (odds_max / odds_min > ODDS_RATIO_CAP))
            )
            ev_raw = feat["pred_calib"] * (odds_min + odds_max) / 2
            feat["ev_avg_calib"] = ev_raw.where(~anomalous, np.nan)
            feat["pred_rank"] = feat["pred"].rank(method="min", ascending=False)

            top1 = feat[feat["pred_rank"] == 1]
            top1_ev = float(top1["ev_avg_calib"].iloc[0]) if not top1.empty else float("nan")
            if not pd.isna(top1_ev):
                if attempt > 0:
                    logging.info("predict_race(%d, %s, %d): リトライ %d 回目で top1 EV=%.2f を取得",
                                 place_code, race_date, race_no, attempt, top1_ev)
                # 発火時の連勝式オッズ板を記録 (live はこの瞬間しか取れない。
                # closing は odds_combo.csv に ingest が保存 → drift 検証用)
                _snapshot_combo_odds(place_code, race_date, race_no, odds_body)
                _snapshot_weather(client, place_code, race_date, race_no)
                return feat
            if attempt < PREDICT_RETRY_MAX:
                logging.info("predict_race(%d, %s, %d): top1 EV=NaN, %d 秒後にリトライ (試行 %d/%d)",
                             place_code, race_date, race_no, PREDICT_RETRY_SLEEP_SEC,
                             attempt + 1, PREDICT_RETRY_MAX)
                time.sleep(PREDICT_RETRY_SLEEP_SEC)
        return feat
    except Exception as e:
        logging.error("predict_race(%d, %s, %d) failed: %s", place_code, race_date, race_no, e)
        logging.error(traceback.format_exc())
        return pd.DataFrame()


# ─── 通知 ─────────────────────────────────────────────────────

def _render_rt3_text(refs: list[dict]) -> list[str]:
    """三連系 (rt3+rf3 or rf3 only) 推奨セクション (text)。
    has_rt3=True: 浜松・山陽 → 三連単+三連複
    has_rt3=False: 伊勢崎・飯塚 → 三連複のみ
    """
    if not refs:
        return []
    lines = [
        "",
        "=" * 40,
        "🎯 三連系 推奨 発注 (EV>=1.80)",
        "  rt3: 浜松・山陽 / rf3: 伊勢崎・浜松・山陽 (飯塚は 0/17 で 2026-07-11 除外)",
    ]
    for ref in refs:
        has_rt3 = ref.get("has_rt3", True)
        rf3_odds = ref.get("rf3_odds")
        rf3_str = f"{rf3_odds:.1f}倍" if rf3_odds else "(?)"
        if has_rt3:
            rt3_odds = ref.get("rt3_odds")
            rt3_str = f"{rt3_odds:.1f}倍" if rt3_odds else "(?)"
            lines.append(
                f"  {ref.get('venue_jp', '?')} R{ref['race_no']}: "
                f"三連単 {ref.get('deme_rt3', '?')} {rt3_str} / "
                f"三連複 {ref.get('deme_rf3', '?')} {rf3_str}"
            )
        else:
            lines.append(
                f"  {ref.get('venue_jp', '?')} R{ref['race_no']}: "
                f"三連複 {ref.get('deme_rf3', '?')} {rf3_str} (RF3のみ)"
            )
    lines.append("=" * 40)
    return lines


def _render_sanyo_rf3_text(refs: list[dict]) -> list[str]:
    """山陽 三連複 参考セクション (text)。投票非推奨・観測用。"""
    if not refs:
        return []
    lines = [
        "",
        "─" * 40,
        "📊 山陽 三連複 参考 (投票非推奨・観測用)",
        "  山陽は過去 rf3 に構造的 edge (月勝率40% / median84%)。",
        "  下記はモデル pred top-3 を三連複 1 点で買った場合の参考。",
        "  ※ 実際の投票は上記の複勝のみ推奨。",
    ]
    for ref in refs:
        odds = ref.get("rf3_odds")
        odds_str = f"{odds:.1f}倍" if odds else "(オッズ取得不可)"
        lines.append(f"  山陽 R{ref['race_no']}: 三連複 {ref['deme']}  {odds_str}")
    lines.append("─" * 40)
    return lines


def render_text(picks: pd.DataFrame, today: str, time_label: str, thr: float,
                sanyo_rf3_refs: list[dict] | None = None,
                rt3_refs: list[dict] | None = None) -> str:
    if picks.empty:
        return f"[autorace] {today} {time_label} 候補なし(EV>={thr})"
    lines = [
        f"📈 autorace EV-based 買い候補 ({today} {time_label})",
        f"閾値: ev_avg_calib >= {thr}, top-1 のみ",
        "",
        f"{'場':6s}{'R':>3s}{'車':>3s}{'pred':>7s}{'EV':>6s}{'min':>6s}{'max':>7s}",
    ]
    total_rec = 0
    for _, r in picks.iterrows():
        rec = recommended_bet_yen(int(r["place_code"]), int(r["race_no"]))
        total_rec += rec
        lines.append(
            f"{r['venue']:6s}{int(r['race_no']):3d}{int(r['car_no']):3d}"
            f"{r['pred_calib']:7.3f}{r['ev_avg_calib']:6.2f}"
            f"{r['place_odds_min']:6.1f}{r['place_odds_max']:7.1f}"
            f"  推奨¥{rec}"
        )
    lines.append("")
    lines.append(f"計 {len(picks)} 候補 / 投資 ¥{len(picks)*100:,} (=¥100 均一)")
    lines.append(f"        推奨額合計 ¥{total_rec:,} (オッズ低下≦10% 目安、過去180日中央値ベース)")
    lines.append("")
    lines.append("【オッズ確認 / 投票】")
    for _, r in picks.iterrows():
        lines.append(
            f"  {r['venue']} R{int(r['race_no'])}: "
            f"https://autorace.jp/race_info/Odds/{r['venue']}/{today}/{int(r['race_no'])}"
        )
    lines.append(f"  🎯 投票 (公式): https://vote.autorace.jp/")
    # 三連単 推奨 (浜松 + 山陽, EV>=1.80)
    lines.extend(_render_rt3_text(rt3_refs or []))
    # 山陽 三連複 参考 (投票非推奨・観測用)
    lines.extend(_render_sanyo_rf3_text(sanyo_rf3_refs or []))
    # 累積成績フッタ
    perf = cumulative_performance()
    if perf and perf["n_total"] > 0:
        lines.append("")
        lines.append(render_cumulative_text(perf))
    return "\n".join(lines)


def _render_rt3_html(refs: list[dict], today: str) -> str:
    """三連系 推奨セクション (HTML)。
    has_rt3=True(浜松・山陽): 三連単+三連複 / has_rt3=False(伊勢崎・飯塚): 三連複のみ
    """
    if not refs:
        return ""
    rows_html = []
    for ref in refs:
        has_rt3 = ref.get("has_rt3", True)
        rf3_odds = ref.get("rf3_odds")
        rf3_str = f"{rf3_odds:.1f}倍" if rf3_odds else "(?)"
        venue = ref.get("venue", "")
        race_no = ref["race_no"]
        odds_url = (
            f"https://autorace.jp/race_info/Odds/{venue}/{today}/{race_no}"
        )
        if has_rt3:
            rt3_odds = ref.get("rt3_odds")
            rt3_str = f"{rt3_odds:.1f}倍" if rt3_odds else "(?)"
            rows_html.append(
                f'<tr>'
                f'<td style="padding:4px 8px;">{ref.get("venue_jp", "?")}</td>'
                f'<td style="padding:4px 8px;">R{race_no}</td>'
                f'<td style="padding:4px 8px; font-weight:bold;">'
                f'{ref.get("deme_rt3", "?")}</td>'
                f'<td style="padding:4px 8px; color:#c62828;">{rt3_str}</td>'
                f'<td style="padding:4px 8px; font-weight:bold;">'
                f'{ref.get("deme_rf3", "?")}</td>'
                f'<td style="padding:4px 8px; color:#1565c0;">{rf3_str}</td>'
                f'<td style="padding:4px 8px;">'
                f'<a href="{odds_url}" style="color:#1565c0;">オッズ</a></td>'
                f'</tr>'
            )
        else:
            rows_html.append(
                f'<tr>'
                f'<td style="padding:4px 8px;">{ref.get("venue_jp", "?")}</td>'
                f'<td style="padding:4px 8px;">R{race_no}</td>'
                f'<td style="padding:4px 8px; color:#999;" colspan="2">—</td>'
                f'<td style="padding:4px 8px; font-weight:bold;">'
                f'{ref.get("deme_rf3", "?")}</td>'
                f'<td style="padding:4px 8px; color:#1565c0;">{rf3_str}</td>'
                f'<td style="padding:4px 8px;">'
                f'<a href="{odds_url}" style="color:#1565c0;">オッズ</a></td>'
                f'</tr>'
            )
    return (
        '<div style="margin-top:16px; padding:10px 14px; '
        'background:#e8f5e9; border:1px solid #66bb6a; border-radius:6px; '
        'font-size:13px; color:#222;">'
        '<b style="color:#2e7d32;">🎯 三連系 推奨 発注 (EV≥1.80)</b>'
        '<p style="margin:4px 0; font-size:12px; color:#555;">'
        'rt3(三連単): 浜松・山陽 &nbsp;|&nbsp; '
        'rf3(三連複): 伊勢崎・浜松・山陽 (飯塚は 0/17 で 2026-07-11 除外)</p>'
        '<table style="margin:4px 0; border-collapse:collapse;">'
        '<tr style="background:#c8e6c9; font-size:12px;">'
        '<th style="padding:4px 8px;">場</th>'
        '<th style="padding:4px 8px;">R</th>'
        '<th style="padding:4px 8px;">三連単</th>'
        '<th style="padding:4px 8px;">rt3</th>'
        '<th style="padding:4px 8px;">三連複</th>'
        '<th style="padding:4px 8px;">rf3</th>'
        '<th style="padding:4px 8px;">確認</th></tr>'
        + "".join(rows_html)
        + '</table></div>'
    )


def _render_sanyo_rf3_html(refs: list[dict]) -> str:
    """山陽 三連複 参考セクション (HTML)。投票非推奨・観測用。"""
    if not refs:
        return ""
    rows = []
    for ref in refs:
        odds = ref.get("rf3_odds")
        odds_str = f"{odds:.1f}倍" if odds else "(取得不可)"
        rows.append(
            f'<li>山陽 R{ref["race_no"]}: <b>三連複 {ref["deme"]}</b> '
            f'<span style="color:#1565c0;">{odds_str}</span></li>'
        )
    return (
        '<div style="margin-top:16px; padding:10px 14px; '
        'background:#fff8e1; border:1px solid #ffd54f; border-radius:6px; '
        'font-size:13px; color:#444;">'
        '<b>📊 山陽 三連複 参考 (投票非推奨・観測用)</b>'
        '<p style="margin:4px 0; font-size:12px; color:#666;">'
        '山陽は過去 rf3 に構造的 edge (月勝率40% / median84%)。'
        'モデル pred top-3 を三連複 1 点で買った場合の参考です。'
        '<b>実際の投票は上記の複勝のみ推奨。</b></p>'
        '<ul style="margin:4px 0 0 18px;">' + "".join(rows) + '</ul>'
        '</div>'
    )


def render_html(picks: pd.DataFrame, today: str, time_label: str, thr: float,
                ngrok_url: str | None = None,
                sanyo_rf3_refs: list[dict] | None = None,
                rt3_refs: list[dict] | None = None) -> str:
    BORDER = '"border-collapse:collapse; border-color:#bbb; font-family:Arial,sans-serif; font-size:13px;"'
    TH = '"background:#e8e8e8; padding:6px 10px; border:1px solid #bbb; text-align:center;"'
    TD = '"padding:6px 10px; border:1px solid #ddd; text-align:right;"'
    TD_L = '"padding:6px 10px; border:1px solid #ddd; text-align:left;"'

    parts = [
        f'<div style="font-family:Arial,sans-serif; font-size:14px; color:#222; max-width:720px;">',
        f'<h2 style="color:#c62828; margin:0 0 12px 0;">'
        f'📈 autorace 買い候補 <span style="color:#222; font-weight:normal;">{today} {time_label}</span></h2>',
        f'<p style="color:#666; margin:0 0 12px 0;">'
        f'戦略: 中間モデル + EV ≥ {thr} (top-1 複勝)</p>',
    ]
    if picks.empty:
        parts.append('<p style="color:#999;">本日この時間帯の候補はありません。</p>')
    else:
        # click-to-buy URL を生成 (2026-05-08 導入、buy_token + buy_app.py 連携)
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
            from buy_token import build_buy_url as _build_buy_url
            _buy_enabled = True
        except Exception:
            _buy_enabled = False  # buy_secret_key 未設定 等で安全に skip

        # 三連系まとめ買い: (place_code, race_no) → rt3_ref の lookup
        # 浜松・山陽 EV>=1.80 のレースは複勝 + 三連単 + 三連複を 1 ボタンで購入
        rt3_lookup = {
            (int(ref["place_code"]), int(ref["race_no"])): ref
            for ref in (rt3_refs or [])
        }

        parts.append(f'<table border="1" cellpadding="6" cellspacing="0" style={BORDER}>')
        parts.append(
            f'<tr><th style={TH}>場</th><th style={TH}>R</th><th style={TH}>車</th>'
            f'<th style={TH}>pred</th><th style={TH}>EV</th>'
            f'<th style={TH}>fns_min</th><th style={TH}>fns_max</th><th style={TH}>tns</th>'
            f'<th style={TH}>推奨¥</th><th style={TH}>オッズ</th><th style={TH}>投票</th>'
            + (f'<th style={TH}>1-click</th>' if _buy_enabled else '')
            + '</tr>'
        )
        BTN_ODDS = (
            '"display:inline-block; padding:5px 10px; background:#1565c0; '
            'color:#ffffff; text-decoration:none; border-radius:4px; '
            'font-weight:bold; font-size:12px;"'
        )
        BTN_VOTE = (
            '"display:inline-block; padding:5px 10px; background:#c62828; '
            'color:#ffffff; text-decoration:none; border-radius:4px; '
            'font-weight:bold; font-size:12px;"'
        )
        BTN_BUY = (
            '"display:inline-block; padding:5px 10px; background:#2e7d32; '
            'color:#ffffff; text-decoration:none; border-radius:4px; '
            'font-weight:bold; font-size:12px;"'
        )
        VENUE_JP_MAP_ = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
        total_rec = 0
        for i, (_, r) in enumerate(picks.iterrows()):
            alt = ' style="background:#fafafa;"' if i % 2 == 1 else ""
            odds_url = (
                f'https://autorace.jp/race_info/Odds/{r["venue"]}/'
                f'{today}/{int(r["race_no"])}'
            )
            vote_url = "https://vote.autorace.jp/"
            rec_yen = recommended_bet_yen(int(r["place_code"]), int(r["race_no"]))
            total_rec += rec_yen
            parts.append(
                f'<tr{alt}>'
                f'<td style={TD_L}>{r["venue"]}</td>'
                f'<td style={TD}>R{int(r["race_no"])}</td>'
                f'<td style={TD}><b>{int(r["car_no"])}</b></td>'
                f'<td style={TD}>{r["pred_calib"]:.3f}</td>'
                f'<td style={TD}><b style="color:#c62828;">{r["ev_avg_calib"]:.2f}</b></td>'
                f'<td style={TD}>{r["place_odds_min"]:.1f}</td>'
                f'<td style={TD}>{r["place_odds_max"]:.1f}</td>'
                f'<td style={TD}>{r["win_odds"]:.1f}</td>'
                f'<td style={TD}><b style="color:#1565c0;">¥{rec_yen}</b></td>'
                f'<td style={TD}><a href="{odds_url}" style={BTN_ODDS}>📊 オッズ</a></td>'
                f'<td style={TD}><a href="{vote_url}" style={BTN_VOTE}>🎯 投票</a></td>'
            )
            if _buy_enabled:
                try:
                    pc = int(r["place_code"])
                    rno = int(r["race_no"])
                    rt3_ref = rt3_lookup.get((pc, rno)) if rt3_buy_active() else None
                    base_payload = {
                        "race_date": today,
                        "place_code": pc,
                        "venue": r["venue"],
                        "venue_jp": VENUE_JP_MAP_.get(pc, "?"),
                        "race_no": rno,
                        "car_no": int(r["car_no"]),
                        "ev": float(r["ev_avg_calib"]),
                    }
                    if rt3_ref:
                        # 浜松・山陽 EV>=1.80: 3 券種まとめ買い
                        # 複勝=推奨額、三連単=¥100、三連複=¥100
                        cars_ord = [int(c) for c in rt3_ref["cars_ordered"]]
                        cars_srt = [int(c) for c in rt3_ref["cars_sorted"]]
                        base_payload["amount"] = rec_yen  # buy_app 表示・互換用
                        base_payload["bets"] = [
                            {"type": "fns", "cars": [int(r["car_no"])],
                             "amount": rec_yen},
                            {"type": "rt3", "cars": cars_ord, "amount": 100},
                            {"type": "rf3", "cars": cars_srt, "amount": 100},
                        ]
                        btn_label = "💰 3点購入"
                    else:
                        # 従来: 複勝 top1 のみ
                        base_payload["amount"] = rec_yen
                        btn_label = "💰 購入"
                    buy_url = _build_buy_url(base_payload, host=ngrok_url)
                    parts.append(
                        f'<td style={TD}>'
                        f'<a href="{buy_url}" style={BTN_BUY}>{btn_label}</a>'
                        f'</td>'
                    )
                except Exception:
                    parts.append(f'<td style={TD}>—</td>')
            parts.append('</tr>')
        parts.append('</table>')
        parts.append(
            f'<p style="margin:12px 0;">計 <b>{len(picks)}</b> 候補'
            f' / 推奨額合計 <b>¥{total_rec:,}</b>'
            f' &nbsp;<span style="color:#888; font-size:11px;">'
            f'(オッズ低下≦10% 目安、過去180日中央値票数ベース)</span></p>'
        )
    # 三連単 推奨 (浜松 + 山陽, EV>=1.80)
    rt3_html = _render_rt3_html(rt3_refs or [], today)
    if rt3_html:
        parts.append(rt3_html)
    # 山陽 三連複 参考 (投票非推奨・観測用)
    rf3_html = _render_sanyo_rf3_html(sanyo_rf3_refs or [])
    if rf3_html:
        parts.append(rf3_html)
    # 累積成績フッタ (青ボックス)
    perf = cumulative_performance()
    if perf and perf["n_total"] > 0:
        parts.append(render_cumulative_html(perf))
    parts.append(
        '<hr style="border:none; border-top:1px solid #ddd; margin:18px 0 8px 0;">'
        '<p style="color:#999; font-size:11px; margin:0;">'
        'auto-racing-ai daily prediction (Phase A: 推奨提示型, 投票はユーザー手動)</p>'
    )
    parts.append('</div>')
    return "\n".join(parts)


def append_picks_log(picks: pd.DataFrame, time_label: str):
    if picks.empty:
        return
    PRODUCTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "race_date", "place_code", "venue", "race_no", "car_no",
        "pred", "pred_calib", "ev_avg_calib",
        "place_odds_min", "place_odds_max", "win_odds",
    ]
    new = picks[cols].copy()
    new["batch"] = time_label
    new["sent_at"] = dt.datetime.now().isoformat(timespec="seconds")
    write_header = not PRODUCTION_LOG.exists() or PRODUCTION_LOG.stat().st_size == 0
    new.to_csv(PRODUCTION_LOG, mode="a", header=write_header, index=False)


def append_shadow_log(top1: pd.DataFrame, time_label: str, thr: float):
    """shadow 判定を記録。本番には影響しない。

    各 top-1 候補に対して、複数の仮想条件での buy/skip を記録:
      - drift 補正: close_ev_est = fire_ev * 0.70 / 0.80
      - 代替閾値: thr=1.80 / 2.00
    後日の結果照合で、どの条件が最適かを評価する。
    """
    if top1 is None or top1.empty:
        return
    try:
        SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = top1.iloc[0]
        fire_ev = float(row.get("ev_avg_calib", float("nan")))
        if pd.isna(fire_ev):
            return
        rec = {
            "race_date": row.get("race_date", ""),
            "place_code": int(row.get("place_code", 0)),
            "race_no": int(row.get("race_no", 0)),
            "car_no": int(row.get("car_no", 0)),
            "pred_calib": float(row.get("pred_calib", 0)),
            "fire_ev": fire_ev,
            "place_odds_min": float(row.get("place_odds_min", 0)),
            "place_odds_max": float(row.get("place_odds_max", 0)),
            "batch": time_label,
            "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
            # 本番判定
            "live_buy": 1 if fire_ev >= thr else 0,
            "live_thr": thr,
        }
        # drift 補正 shadow
        for factor in SHADOW_DRIFT_FACTORS:
            est = fire_ev * factor
            rec[f"drift{int(factor*100)}_ev"] = round(est, 3)
            rec[f"drift{int(factor*100)}_buy"] = 1 if est >= thr else 0
        # 代替閾値 shadow
        for alt_thr in SHADOW_THRESHOLDS:
            rec[f"thr{int(alt_thr*100)}_buy"] = 1 if fire_ev >= alt_thr else 0
            # drift + 代替閾値の組み合わせ
            for factor in SHADOW_DRIFT_FACTORS:
                est = fire_ev * factor
                rec[f"drift{int(factor*100)}_thr{int(alt_thr*100)}_buy"] = 1 if est >= alt_thr else 0

        new = pd.DataFrame([rec])
        write_header = not SHADOW_LOG.exists() or SHADOW_LOG.stat().st_size == 0
        new.to_csv(SHADOW_LOG, mode="a", header=write_header, index=False)
    except Exception as e:
        logging.warning("shadow_log 記録失敗: %s", e)


def append_odds_snapshot(feat: pd.DataFrame, time_label: str):
    """発火時の全車オッズ + EV を odds_snapshots.csv に追記。

    後日 odds_summary.csv (= 確定後オッズ) と join して、信号 persistence
    (発火時 EV>=thr が確定時にも残るか) を測定するため。
    """
    if feat is None or feat.empty:
        return
    ODDS_SNAPSHOT_LOG.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "race_date", "place_code", "race_no", "car_no",
        "pred", "pred_calib", "ev_avg_calib",
        "pred_rank", "place_odds_min", "place_odds_max", "win_odds",
    ]
    keep = [c for c in cols if c in feat.columns]
    snap = feat[keep].copy()
    snap["batch"] = time_label
    snap["captured_at"] = dt.datetime.now().isoformat(timespec="seconds")
    write_header = not ODDS_SNAPSHOT_LOG.exists() or ODDS_SNAPSHOT_LOG.stat().st_size == 0
    snap.to_csv(ODDS_SNAPSHOT_LOG, mode="a", header=write_header, index=False)


def _rf3_odds_lookup(rf3_list: dict, cars: list[int]) -> float | None:
    """rf3OddsList (ネスト dict rf3[i][j][k], i<j<k) から三連複オッズを引く。"""
    if not isinstance(rf3_list, dict) or len(cars) != 3:
        return None
    i, j, k = sorted(int(c) for c in cars)
    try:
        return float(rf3_list[str(i)][str(j)][str(k)])
    except (KeyError, TypeError, ValueError):
        return None


def _rt3_odds_lookup(rt3_list: dict, cars_ordered: list[int]) -> float | None:
    """rt3OddsList (ネスト dict rt3[1st][2nd][3rd]) から三連単オッズを引く。"""
    if not isinstance(rt3_list, dict) or len(cars_ordered) != 3:
        return None
    a, b, c = [int(x) for x in cars_ordered]
    try:
        return float(rt3_list[str(a)][str(b)][str(c)])
    except (KeyError, TypeError, ValueError):
        return None


def build_sanyo_rf3_reference(
    client: AutoraceClient, feat: pd.DataFrame,
    race_date: str, race_no: int,
) -> dict | None:
    """山陽の三連複 (rf3) 参考情報を生成 (投票せず観測のみ)。

    過去検証 (docs/ev_strategy_findings.md) で山陽 rf3 のみ構造的 edge
    (月勝率 40% / median 84% / min 22%、他場は min 0%) が観測された。
    Phase A 規律 (複勝 top-1 のみ投票) は崩さず、山陽で複勝推奨が出た R に
    限り「モデル pred top-3 を三連複 1 点で買ったら」の参考情報を案内する。

    返り値 dict (メール表示 + paper CSV 記録用)、取得失敗時は None。
    """
    if feat is None or feat.empty:
        return None
    # モデル pred 上位 3 車 (三連複は順不同なので昇順ソート)
    top3 = feat.sort_values("pred_calib", ascending=False).head(3)
    if len(top3) < 3:
        return None
    cars = sorted(int(c) for c in top3["car_no"].tolist())
    # rf3 オッズ取得 (発火時点の最新)
    rf3_odds = None
    try:
        odds_resp = client.get_odds(SANYO_PLACE_CODE, race_date, race_no)
        odds_body = odds_resp.get("body", {})
        if isinstance(odds_body, dict):
            rf3_odds = _rf3_odds_lookup(odds_body.get("rf3OddsList", {}), cars)
    except Exception as e:
        logging.warning("山陽 rf3 オッズ取得失敗 R%d: %s", race_no, e)

    ref = {
        "race_date": race_date,
        "place_code": SANYO_PLACE_CODE,
        "race_no": race_no,
        "cars": cars,                       # 例: [3, 5, 7]
        "deme": "-".join(str(c) for c in cars),
        "rf3_odds": rf3_odds,
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    return ref


def append_sanyo_rf3_paper(ref: dict) -> None:
    """山陽 rf3 参考を paper trading CSV に記録 (後で payouts と join し検証)。"""
    if not ref:
        return
    import csv
    SANYO_RF3_PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
    header = ["race_date", "place_code", "race_no", "deme",
              "rf3_odds", "captured_at"]
    row = [ref["race_date"], ref["place_code"], ref["race_no"],
           ref["deme"], ref.get("rf3_odds", ""), ref["captured_at"]]
    try:
        new_file = (not SANYO_RF3_PAPER_LOG.exists()
                    or SANYO_RF3_PAPER_LOG.stat().st_size == 0)
        with open(SANYO_RF3_PAPER_LOG, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(header)
            w.writerow(row)
    except Exception as e:
        logging.warning("sanyo_rf3_paper.csv 追記失敗: %s", e)


def build_rt3_reference(
    client: AutoraceClient, feat: pd.DataFrame,
    race_date: str, race_no: int, place_code: int,
) -> dict | None:
    """浜松/山陽の三連系 (rt3 + rf3) 推奨情報を生成。

    過去検証 ev_3point_by_place (2026-05-29) で thr>=1.80:
    - 浜松: rt3 ROI 530% hit 8.0% / rf3 ROI 330% hit 25.6%
    - 山陽: rt3 ROI 141% hit 9.1% / rf3 ROI 185% hit 30.7%
    モデル pred top-3 の着順予測で三連単 1 点 + 三連複 1 点を推奨。
    """
    if feat is None or feat.empty:
        return None
    top3 = feat.sort_values("pred_calib", ascending=False).head(3)
    if len(top3) < 3:
        return None
    cars_ordered = [int(c) for c in top3["car_no"].tolist()]
    cars_sorted = sorted(cars_ordered)

    rt3_odds = None
    rf3_odds = None
    try:
        odds_resp = client.get_odds(place_code, race_date, race_no)
        odds_body = odds_resp.get("body", {})
        if isinstance(odds_body, dict):
            rt3_odds = _rt3_odds_lookup(
                odds_body.get("rt3OddsList", {}), cars_ordered)
            rf3_odds = _rf3_odds_lookup(
                odds_body.get("rf3OddsList", {}), cars_sorted)
    except Exception as e:
        logging.warning("3point odds lookup failed R%d: %s", race_no, e)

    venue = VENUE_CODES.get(place_code, str(place_code))
    venue_jp = {2: "川口", 3: "伊勢崎", 4: "浜松",
                5: "飯塚", 6: "山陽"}.get(place_code, "?")

    return {
        "race_date": race_date,
        "place_code": place_code,
        "venue": venue,
        "venue_jp": venue_jp,
        "race_no": race_no,
        "cars_ordered": cars_ordered,
        "cars_sorted": cars_sorted,
        "deme_rt3": "→".join(str(c) for c in cars_ordered),
        "deme_rf3": "-".join(str(c) for c in cars_sorted),
        "rt3_odds": rt3_odds,
        "rf3_odds": rf3_odds,
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def append_rt3_paper(ref: dict) -> None:
    """三連系 (rt3 + rf3) 推奨を paper trading CSV に記録。"""
    if not ref:
        return
    import csv
    RT3_PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
    header = ["race_date", "place_code", "venue", "race_no",
              "deme_rt3", "deme_rf3",
              "car_1st", "car_2nd", "car_3rd",
              "rt3_odds", "rf3_odds", "captured_at"]
    cars = ref.get("cars_ordered", [0, 0, 0])
    row = [ref["race_date"], ref["place_code"], ref.get("venue", ""),
           ref["race_no"], ref.get("deme_rt3", ""), ref.get("deme_rf3", ""),
           cars[0] if len(cars) > 0 else "",
           cars[1] if len(cars) > 1 else "",
           cars[2] if len(cars) > 2 else "",
           ref.get("rt3_odds", ""), ref.get("rf3_odds", ""),
           ref["captured_at"]]
    try:
        new_file = (not RT3_PAPER_LOG.exists()
                    or RT3_PAPER_LOG.stat().st_size == 0)
        with open(RT3_PAPER_LOG, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(header)
            w.writerow(row)
    except Exception as e:
        logging.warning("rt3_paper.csv append failed: %s", e)


# ─── メイン ───────────────────────────────────────────────────

def _notify_fatal(target_date: str, time_label: str, err: Exception) -> None:
    """fatal error を即時メール通知。送信失敗してもログだけ出して諦める。"""
    try:
        from gmail_notify import send_email
        subject = f"[autorace] 🚨 daily_predict fatal error {target_date} {time_label}"
        body = (
            f"daily_predict が異常終了しました。\n\n"
            f"対象日: {target_date}\n"
            f"バッチ: {time_label}\n\n"
            f"【エラー】\n{err!r}\n\n"
            f"【スタックトレース】\n{traceback.format_exc()}\n\n"
            f"対処: data/daily_predict.log を確認。"
        )
        send_email(subject=subject, body=body)
    except Exception as e:
        logging.error("fatal 通知メール送信も失敗: %s", e)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--venues", type=int, nargs="+", required=True)
    p.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: 今日)")
    p.add_argument("--thr", type=float, default=1.50)
    p.add_argument("--no-email", action="store_true")
    p.add_argument("--time-label", type=str, default=None,
                   help="メール件名・ログ用ラベル(default: venues / slot から自動)")
    p.add_argument("--time-slot", type=str, default=None,
                   choices=["morning", "noon", "evening"],
                   help="Hold/Today の liveStartTime で venues を動的フィルタ")
    p.add_argument("--races", type=int, nargs="+", default=None,
                   help="対象 race_no を限定(default: 1..12 全部)")
    p.add_argument("--suppress-noresult-email", action="store_true",
                   help="候補なしの場合メール送信スキップ(動的発火用、空打ち抑止)")
    args = p.parse_args()

    setup_logging()
    logger = logging.getLogger("daily_predict")

    target_date = args.date or dt.date.today().isoformat()
    if args.time_label:
        time_label = args.time_label
    elif args.time_slot:
        time_label = {"morning": "朝", "noon": "昼", "evening": "夕"}[args.time_slot]
    elif set(args.venues) <= {2, 3, 4}:
        time_label = "朝(daytime)"
    elif set(args.venues) == {5}:
        time_label = "昼(飯塚)"
    else:
        time_label = ",".join(str(v) for v in args.venues)

    logger.info("=== daily_predict start: date=%s venues=%s time=%s thr=%.2f ===",
                target_date, args.venues, time_label, args.thr)

    # 三連系 全場・全期間 絶対損失バックストップ (2026-07-11 監査 P2、sticky)。
    # 新規発動時はフラグ書き出し + メール通知。以後 rt3_buy_active() が False になり
    # 三連系 (メールの 3 点購入ボタン / auto_buy) が止まる。評価失敗では止めない
    # (現役スコープの kill-switch ②(-5,000) が別途効いている)。
    try:
        _bs = enforce_backstop()
        if _bs.get("newly_triggered"):
            logger.warning("[backstop] 発動: 全場三連系累積 %s 円 <= %s 円 — "
                           "rt3_backstop_stop.flag 書き出し (sticky)",
                           _bs.get("profit"), _bs.get("threshold"))
        elif _bs.get("active"):
            logger.warning("[backstop] 有効中 (sticky) — 三連系購入は停止 "
                           "(解除は data/rt3_backstop_stop.flag を人間が削除)")
    except Exception as e:  # noqa: BLE001
        logger.warning("[backstop] 評価失敗 (継続): %s", e)

    try:
        model, iso, meta = load_production()
        client = AutoraceClient()

        # 当日スケジュール取得(Hold/Today)— 場ごとの開催状況・発走時間帯を確認
        # 当日 args.date が today と異なる場合は履歴日扱いなので skip
        schedule: dict[int, dict] = {}
        is_today = args.date is None or args.date == dt.date.today().isoformat()
        if is_today:
            schedule = fetch_today_schedule(client)
            logger.info("Hold/Today 開催数: %d 場", len(schedule))
            for pc in args.venues:
                s = schedule.get(pc)
                if s is None:
                    logger.info("  pc=%d (%s): 当日開催なし", pc, VENUE_CODES.get(pc, "?"))
                else:
                    logger.info(
                        "  pc=%d (%s): %s 〜 %s, %s, finalR=%s, lastNightProg=%s, cancel=%s",
                        pc, VENUE_CODES.get(pc, "?"),
                        s.get("liveStartTime"), s.get("liveEndTime"),
                        s.get("nighterName") or "通常",
                        s.get("finalRaceNo"), s.get("lastNightProgramFlg"),
                        s.get("cancelFlg"),
                    )

        # --time-slot 指定時、開催あり & 該当 slot & 中止でない場のみに絞る
        # 履歴日(schedule 空)は filter なし — そのまま全 venues 予測(再現性)
        if args.time_slot and is_today:
            kept = []
            for pc in args.venues:
                s = schedule.get(pc)
                if s is None:
                    logger.info("  [slot=%s] pc=%d スキップ(当日開催なし)",
                                args.time_slot, pc)
                    continue
                if str(s.get("cancelFlg")) == "1":
                    logger.info("  [slot=%s] pc=%d スキップ(中止)", args.time_slot, pc)
                    continue
                st = s.get("liveStartTime")
                if not _in_slot(st, args.time_slot):
                    logger.info("  [slot=%s] pc=%d スキップ(liveStart=%s 範囲外)",
                                args.time_slot, pc, st)
                    continue
                kept.append(pc)
            if kept != args.venues:
                logger.info("  → 対象 venues: %s → %s", args.venues, kept)
            args.venues = kept

            if not args.venues:
                # 空打ちメール抑止: slot に該当する場が無ければログだけで終了
                logger.info("該当 venue なし — メール送信スキップ")
                return

        all_picks = []
        sanyo_rf3_refs = []  # 山陽 三連複 参考 (投票せず観測のみ)
        rt3_refs = []  # 三連単 推奨 (浜松 + 山陽, EV>=1.80)
        race_nos = args.races if args.races else list(range(1, 13))
        # サマリ用カウンタ: 発走 30→15 分前へ短縮しても複勝オッズが薄ければ
        # NaN で silent skip する。可視化のため eval/NaN/below/hit を集計。
        n_eval = 0
        n_nan = 0
        n_below_thr = 0
        nan_races = []  # (venue, race_no) の list
        for pc in args.venues:
            venue = VENUE_CODES.get(pc, str(pc))
            logger.info("--- %s (pc=%d) races=%s ---", venue, pc, race_nos)
            NEAR_MISS_BAND = 0.30  # (参考値、retry 廃止により未使用)
            NEAR_MISS_RETRIES = 0  # near-miss retry 廃止 (締切 2 分前到着を優先)
            for race_no in race_nos:
                df = predict_race(client, model, iso, meta, pc, target_date, race_no)
                if df.empty:
                    continue
                # 初回 snapshot のみ保存 (retry 分は別行になり persistence 解析を歪めるため)
                try:
                    append_odds_snapshot(df, time_label)
                except Exception as e:
                    logger.warning("odds_snapshot 追記失敗: %s", e)
                n_eval += 1
                top1 = df[df["pred_rank"] == 1].copy()
                top1_ev = float(top1["ev_avg_calib"].iloc[0]) if not top1.empty else float("nan")
                # near-miss retry: 閾値未達だが近接 → odds drift up で thr 跨ぎ可能性
                for nm_attempt in range(NEAR_MISS_RETRIES):
                    if pd.isna(top1_ev):
                        break  # NaN は predict_race 内部で既に retry 済
                    if top1_ev >= args.thr:
                        break  # 閾値到達、retry 不要
                    if top1_ev < args.thr - NEAR_MISS_BAND:
                        break  # 大きく未達、drift up で届く可能性低い
                    car = int(top1["car_no"].iloc[0]) if not top1.empty else 0
                    logger.info("  R%d 近接未達 (車%d EV=%.2f < %.2f, band %.2f), %d 秒後リトライ (%d/%d)",
                                race_no, car, top1_ev, args.thr, NEAR_MISS_BAND,
                                PREDICT_RETRY_SLEEP_SEC, nm_attempt + 1, NEAR_MISS_RETRIES)
                    time.sleep(PREDICT_RETRY_SLEEP_SEC)
                    df = predict_race(client, model, iso, meta, pc, target_date, race_no)
                    if df.empty:
                        break
                    top1 = df[df["pred_rank"] == 1].copy()
                    top1_ev = float(top1["ev_avg_calib"].iloc[0]) if not top1.empty else float("nan")
                if pd.isna(top1_ev):
                    n_nan += 1
                    nan_races.append(f"{venue}_R{race_no}")
                    pmin = top1["place_odds_min"].iloc[0] if not top1.empty else None
                    pmax = top1["place_odds_max"].iloc[0] if not top1.empty else None
                    logger.warning("  R%d top1 EV=NaN (place_odds_min=%s, max=%s) — fns 未公開で silent skip",
                                   race_no, pmin, pmax)
                    continue
                # shadow 記録: 閾値判定の前に全 top1 を記録 (drift 補正 / 代替閾値の仮想評価用)
                append_shadow_log(top1, time_label, args.thr)
                cands = top1[top1["ev_avg_calib"] >= args.thr].copy()
                if cands.empty:
                    n_below_thr += 1
                    car = int(top1["car_no"].iloc[0])
                    logger.info("  R%d top1 車%d EV=%.2f < %.2f", race_no, car, top1_ev, args.thr)
                    continue
                cands["venue"] = venue
                all_picks.append(cands)
                for _, r in cands.iterrows():
                    logger.info("  R%d 車%d pred=%.3f EV=%.2f min=%.1f max=%.1f",
                                int(r["race_no"]), int(r["car_no"]),
                                r["pred_calib"], r["ev_avg_calib"],
                                r["place_odds_min"], r["place_odds_max"])

                # 山陽のみ: 三連複 参考情報を生成 (投票せず観測のみ、paper 記録)
                # ただし EV>=1.80 の R は三連系推奨に含まれるため重複スキップ
                if pc == SANYO_PLACE_CODE and top1_ev < RT3_THR:
                    try:
                        ref = build_sanyo_rf3_reference(client, df, target_date, race_no)
                        if ref:
                            sanyo_rf3_refs.append(ref)
                            append_sanyo_rf3_paper(ref)
                            logger.info("  [山陽 rf3 参考] 三連複 %s オッズ=%s (観測のみ、投票せず)",
                                        ref["deme"], ref.get("rf3_odds"))
                    except Exception as e:
                        logger.warning("  山陽 rf3 参考生成失敗 R%d: %s", race_no, e)

                # RF3 対象場(伊勢崎/浜松/飯塚/山陽): 三連系推奨 (EV>=1.80, paper 記録)
                # RT3(三連単)は浜松・山陽のみ、RF3(三連複)は4場全て
                if pc in RF3_ELIGIBLE_PLACES and top1_ev >= RT3_THR:
                    try:
                        rt3_ref = build_rt3_reference(
                            client, df, target_date, race_no, pc)
                        if rt3_ref:
                            # has_rt3: 三連単を購入するか(浜松・山陽のみ True)
                            rt3_ref["has_rt3"] = pc in RT3_ELIGIBLE_PLACES
                            rt3_refs.append(rt3_ref)
                            append_rt3_paper(rt3_ref)
                            if rt3_ref["has_rt3"]:
                                logger.info(
                                    "  [3point] rt3 %s (%s) / rf3 %s (%s)",
                                    rt3_ref.get("deme_rt3"),
                                    rt3_ref.get("rt3_odds"),
                                    rt3_ref.get("deme_rf3"),
                                    rt3_ref.get("rf3_odds"))
                            else:
                                logger.info(
                                    "  [rf3only] rf3 %s (%s)",
                                    rt3_ref.get("deme_rf3"),
                                    rt3_ref.get("rf3_odds"))
                    except Exception as e:
                        logger.warning("  3point 推奨生成失敗 R%d: %s", race_no, e)

        if all_picks:
            picks = pd.concat(all_picks, ignore_index=True)
            picks = picks.sort_values(["place_code", "race_no"]).reset_index(drop=True)
        else:
            picks = pd.DataFrame()

        # ─── 夜間限定 自動投票 (Phase 1、AUTO_BUY_ENABLED=False の間は no-op) ───
        # click-to-buy を壊さず、寝てる時間帯のみガード付きで自動投票。
        try:
            import auto_buy
            if auto_buy.AUTO_BUY_ENABLED and not picks.empty:
                rt3_map = {
                    (int(r["place_code"]), int(r["race_no"])): r
                    for r in rt3_refs
                }
                # 停止フラグ ON 時は三連系を自動投票に含めない (複勝のみ)
                _ab_include_rt3 = (auto_buy.AUTO_BUY_INCLUDE_RT3
                                   and rt3_buy_active())
                candidates = []
                for _, r in picks.iterrows():
                    pc = int(r["place_code"])
                    rno = int(r["race_no"])
                    rec_yen = recommended_bet_yen(pc, rno)
                    bets = auto_buy.build_bets(
                        int(r["car_no"]), rec_yen, rt3_map.get((pc, rno)),
                        include_rt3=_ab_include_rt3)
                    if not bets:
                        # 複勝 OFF かつ三連系対象外 → 自動投票なし (予測メールは別途送る)
                        continue
                    candidates.append({
                        "race_date": target_date,
                        "place_code": pc,
                        "venue": r["venue"],
                        "venue_jp": {2: "川口", 3: "伊勢崎", 4: "浜松",
                                     5: "飯塚", 6: "山陽"}.get(pc, "?"),
                        "race_no": rno,
                        "car_no": int(r["car_no"]),
                        "ev": float(r["ev_avg_calib"]),
                        "bets": bets,
                        "amount": sum(int(b["amount"]) for b in bets),
                    })
                verdicts = auto_buy.run_auto_buy(candidates)
                logger.info("[auto_buy] %d 候補処理 (dry_run=%s): %s",
                            len(candidates), auto_buy.AUTO_BUY_DRY_RUN,
                            [v.get("verdict") for v in verdicts])
        except Exception as e:
            logger.error("[auto_buy] 自動投票処理エラー(継続): %s", e)
            logger.error(traceback.format_exc())

        # 候補ありの場合 buy_app + ngrok トンネルを起動 (スマホからの 1-click 購入用)
        # 4 分後に両方自動停止する cleanup プロセスも spawn
        BUY_TTL_SEC = 240  # 4 分
        ngrok_url = None
        buy_app_pid = None
        if not picks.empty:
            # buy_app が未起動ならバックグラウンドで起動
            try:
                import urllib.request as _ur
                _ur.urlopen("http://127.0.0.1:8502/_stcore/health", timeout=2)
                logger.info("buy_app already running on :8502")
            except Exception:
                try:
                    buy_app_path = str(Path(__file__).resolve().parent / "app" / "buy_app.py")
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "streamlit", "run", buy_app_path,
                         "--server.port", "8502", "--server.headless", "true"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                    )
                    buy_app_pid = proc.pid
                    logger.info("buy_app started on :8502 (pid=%d)", buy_app_pid)
                    time.sleep(3)
                except Exception as e:
                    logger.warning("buy_app start failed: %s", e)
            # ngrok トンネル起動
            try:
                import sys as _sys
                _scripts = str(Path(__file__).resolve().parent / "scripts")
                if _scripts not in _sys.path:
                    _sys.path.insert(0, _scripts)
                from ngrok_tunnel import start_tunnel
                ngrok_url = start_tunnel(port=8502, ttl_sec=BUY_TTL_SEC)
                if ngrok_url:
                    logger.info("ngrok tunnel: %s (%ds TTL)", ngrok_url, BUY_TTL_SEC)
                else:
                    logger.warning("ngrok tunnel start failed, falling back to LAN URL")
            except Exception as e:
                logger.warning("ngrok unavailable: %s", e)
            # cleanup: BUY_TTL_SEC 後に ngrok + buy_app を停止する独立プロセス
            cleanup_script = (
                f"import time, subprocess, os, signal; "
                f"time.sleep({BUY_TTL_SEC}); "
                f"subprocess.run(['taskkill','/F','/IM','ngrok.exe'],"
                f"capture_output=True,timeout=5); "
                + (f"os.kill({buy_app_pid}, signal.SIGTERM); " if buy_app_pid else "")
            )
            try:
                subprocess.Popen(
                    [sys.executable, "-c", cleanup_script],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                )
                logger.info("cleanup scheduled in %ds (ngrok%s)",
                            BUY_TTL_SEC, f" + buy_app pid={buy_app_pid}" if buy_app_pid else "")
            except Exception as e:
                logger.warning("cleanup scheduler failed: %s", e)

        text = render_text(picks, target_date, time_label, args.thr,
                           sanyo_rf3_refs=sanyo_rf3_refs,
                           rt3_refs=rt3_refs)
        html = render_html(picks, target_date, time_label, args.thr,
                           ngrok_url=ngrok_url, sanyo_rf3_refs=sanyo_rf3_refs,
                           rt3_refs=rt3_refs)
        print()
        print(text)

        n_hit = len(picks)
        logger.info("サマリ: eval=%d / hit=%d / below_thr=%d / NaN-skip=%d%s",
                    n_eval, n_hit, n_below_thr, n_nan,
                    f" [{', '.join(nan_races)}]" if nan_races else "")
        if not picks.empty:
            append_picks_log(picks, time_label)
            logger.info("候補数: %d / 投資 ¥%d", len(picks), len(picks) * 100)
        else:
            logger.info("候補なし")

        if args.no_email:
            logger.info("--no-email: 送信スキップ")
            return

        if args.suppress_noresult_email and picks.empty:
            logger.info("--suppress-noresult-email: 候補なしのため送信スキップ")
            return

        try:
            from gmail_notify import send_email
        except Exception as e:
            logger.error("gmail_notify インポート失敗: %s", e)
            return
    except Exception as fatal:
        logger.error("daily_predict fatal: %s", fatal)
        logger.error(traceback.format_exc())
        _notify_fatal(target_date, time_label, fatal)
        sys.exit(1)
        return

    n = len(picks)
    subject = f"[autorace] {target_date} {time_label} {n}候補"
    if n == 0:
        subject = f"[autorace] {target_date} {time_label} 候補なし"
    try:
        send_email(subject=subject, body=text, html=html)
        logger.info("メール送信完了: %s", subject)
    except Exception as e:
        logger.error("メール送信失敗: %s", e)


if __name__ == "__main__":
    main()
