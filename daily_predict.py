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
)

DATA = ROOT / "data"
LOG_FILE = DATA / "daily_predict.log"
PRODUCTION_LOG = DATA / "daily_predict_picks.csv"
ODDS_SNAPSHOT_LOG = DATA / "odds_snapshots.csv"  # 発火時オッズスナップ (信号 persistence 解析用)
EXPECTED_VOTES_CSV = DATA / "expected_votes.csv"  # 場×R別 typical 票数 → 推奨ベット額算出


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
        picks = load_picks()
        if picks.empty:
            return None
        audit = attach_results(picks)
        summary = summarize(audit)
        summary["start_date"] = audit["race_date"].min().date()
        summary["end_date"] = audit["race_date"].max().date()
        return summary
    except Exception as e:
        logging.warning("cumulative_performance() 失敗: %s", e)
        return None


def render_cumulative_text(perf: dict) -> str:
    """累積成績の text 版 (メール末尾用)。"""
    if perf is None or perf["n_total"] == 0:
        return "📊 本番運用累積成績: データなし"
    lines = [
        "================================================",
        f"📊 本番運用累積成績 (運用開始 {perf['start_date']}〜{perf['end_date']})",
        f"   戦績: {perf['n_hits']} 勝 / {perf['n_settled']} 確定 = {perf['hit_rate']*100:.1f}%"
        f" / 累積¥{perf['profit']:+,.0f} / ROI {perf['roi']*100:.1f}%",
        f"   未確定: {perf['n_pending']} 件 (本日分など、結果待ち)",
        "================================================",
    ]
    return "\n".join(lines)


def render_cumulative_html(perf: dict) -> str:
    """累積成績の HTML 版 (メール末尾用)。"""
    if perf is None or perf["n_total"] == 0:
        return ""
    profit_color = "#2e7d32" if perf["profit"] >= 0 else "#c62828"
    return (
        '<div style="margin-top:20px; padding:12px 16px; '
        'background:#f0f7ff; border-left:4px solid #1565c0; '
        'border-radius:4px; font-family:Arial,sans-serif; font-size:13px;">'
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


PREDICT_RETRY_MAX = 2          # top1 EV NaN 時の odds 再取得試行回数 (1min × 2 = 最大 2 分待機)
PREDICT_RETRY_SLEEP_SEC = 60   # 再取得までの待機秒


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
            # 異常 odds を NaN 化 (3 パターン)。
            # 異常時は NaN にすることで near-miss retry に再評価させる。
            #   1. max > 50: min=1.0/max=183 のセンチネル
            #      (複勝 odds は実質 30 倍程度が上限)
            #   2. max/min > 20: 過渡期 snapshot (通常は 5x 以内)
            #   3. min < 1.1 or max < 1.1: 1.0/1.0 等のセンチネル
            #      (複勝の理論最小オッズは 1.1 = 控除率 10%、1.0 はあり得ない)
            ODDS_MAX_CAP = 50.0
            ODDS_RATIO_CAP = 20.0
            ODDS_MIN_FLOOR = 1.1
            odds_min = feat["place_odds_min"]
            odds_max = feat["place_odds_max"]
            anomalous = (
                (odds_max > ODDS_MAX_CAP)
                | ((odds_min > 0) & (odds_max / odds_min > ODDS_RATIO_CAP))
                | (odds_min < ODDS_MIN_FLOOR)
                | (odds_max < ODDS_MIN_FLOOR)
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

def render_text(picks: pd.DataFrame, today: str, time_label: str, thr: float) -> str:
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
    # 累積成績フッタ
    perf = cumulative_performance()
    if perf and perf["n_total"] > 0:
        lines.append("")
        lines.append(render_cumulative_text(perf))
    return "\n".join(lines)


def render_html(picks: pd.DataFrame, today: str, time_label: str, thr: float) -> str:
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
        parts.append(f'<table border="1" cellpadding="6" cellspacing="0" style={BORDER}>')
        parts.append(
            f'<tr><th style={TH}>場</th><th style={TH}>R</th><th style={TH}>車</th>'
            f'<th style={TH}>pred</th><th style={TH}>EV</th>'
            f'<th style={TH}>fns_min</th><th style={TH}>fns_max</th><th style={TH}>tns</th>'
            f'<th style={TH}>推奨¥</th><th style={TH}>オッズ</th><th style={TH}>投票</th></tr>'
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
                f'</tr>'
            )
        parts.append('</table>')
        parts.append(
            f'<p style="margin:12px 0;">計 <b>{len(picks)}</b> 候補'
            f' / 推奨額合計 <b>¥{total_rec:,}</b>'
            f' &nbsp;<span style="color:#888; font-size:11px;">'
            f'(オッズ低下≦10% 目安、過去180日中央値票数ベース)</span></p>'
        )
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
            NEAR_MISS_BAND = 0.30  # EV ≥ thr-0.30 なら drift up を期待して retry
            NEAR_MISS_RETRIES = 2  # 60s × 2 で最大 -3min まで監視
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

        if all_picks:
            picks = pd.concat(all_picks, ignore_index=True)
            picks = picks.sort_values(["place_code", "race_no"]).reset_index(drop=True)
        else:
            picks = pd.DataFrame()

        text = render_text(picks, target_date, time_label, args.thr)
        html = render_html(picks, target_date, time_label, args.thr)
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
