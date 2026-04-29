"""日次予想スクリプト: 中間モデルで当日対象場の買い候補を計算 + メール送信

使い方:
  python daily_predict.py --venues 2 3 4              # 朝(daytime 3場)
  python daily_predict.py --venues 5                  # 昼(飯塚)
  python daily_predict.py --venues 2 3 4 --no-email   # dry-run
  python daily_predict.py --venues 5 --thr 1.30       # 閾値変更
  python daily_predict.py --venues 5 --date 2026-04-29  # 日付指定

設計:
- 中間モデル(オッズあり・試走なし、AUC 0.80)
- 校正: isotonic regression(walk-forward 予測で fit 済み)
- 選別: 各レースで予測 top-1 車 × ev_avg_calib >= thr(default 1.50)
- 出力: ログ + メール(候補ありの場合のみ)

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


def predict_race(
    client: AutoraceClient, model: lgb.Booster, iso, meta,
    place_code: int, race_date: str, race_no: int,
) -> pd.DataFrame:
    """1 レース分の予測 + EV 計算結果を返す(空 DataFrame なら対象外)。"""
    try:
        prog = client.get_program(place_code, race_date, race_no)
        if prog.get("result") != "Success":
            return pd.DataFrame()
        body = prog.get("body", {})
        if isinstance(body, list) or not body.get("playerList"):
            return pd.DataFrame()

        odds_resp = client.get_odds(place_code, race_date, race_no)
        odds_body = odds_resp.get("body", {})
        if isinstance(odds_body, list):
            return pd.DataFrame()

        feat = build_features_for_race(place_code, race_date, race_no, body, odds_body)
        if feat.empty:
            return pd.DataFrame()

        # 欠車除外
        feat = feat[feat["is_absent"] == 0].copy()
        if feat.empty:
            return pd.DataFrame()

        X = align_features(feat, meta)
        feat["pred"] = model.predict(X)
        feat["pred_calib"] = iso.transform(feat["pred"].values)
        feat["ev_avg_calib"] = feat["pred_calib"] * (
            feat["place_odds_min"] + feat["place_odds_max"]
        ) / 2
        feat["pred_rank"] = feat["pred"].rank(method="min", ascending=False)
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
    for _, r in picks.iterrows():
        lines.append(
            f"{r['venue']:6s}{int(r['race_no']):3d}{int(r['car_no']):3d}"
            f"{r['pred_calib']:7.3f}{r['ev_avg_calib']:6.2f}"
            f"{r['place_odds_min']:6.1f}{r['place_odds_max']:7.1f}"
        )
    lines.append("")
    lines.append(f"計 {len(picks)} 候補 / 投資 ¥{len(picks)*100:,}")
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
            f'<th style={TH}>fns_min</th><th style={TH}>fns_max</th><th style={TH}>tns</th></tr>'
        )
        for i, (_, r) in enumerate(picks.iterrows()):
            alt = ' style="background:#fafafa;"' if i % 2 == 1 else ""
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
                f'</tr>'
            )
        parts.append('</table>')
        parts.append(
            f'<p style="margin:12px 0;">計 <b>{len(picks)}</b> 候補 / 投資 <b>¥{len(picks)*100:,}</b></p>'
        )
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


# ─── メイン ───────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--venues", type=int, nargs="+", required=True)
    p.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: 今日)")
    p.add_argument("--thr", type=float, default=1.50)
    p.add_argument("--no-email", action="store_true")
    p.add_argument("--time-label", type=str, default=None,
                   help="メール件名・ログ用ラベル(default: venues から自動)")
    args = p.parse_args()

    setup_logging()
    logger = logging.getLogger("daily_predict")

    target_date = args.date or dt.date.today().isoformat()
    if args.time_label:
        time_label = args.time_label
    elif set(args.venues) <= {2, 3, 4}:
        time_label = "朝(daytime)"
    elif set(args.venues) == {5}:
        time_label = "昼(飯塚)"
    else:
        time_label = ",".join(str(v) for v in args.venues)

    logger.info("=== daily_predict start: date=%s venues=%s time=%s thr=%.2f ===",
                target_date, args.venues, time_label, args.thr)

    model, iso, meta = load_production()
    client = AutoraceClient()

    all_picks = []
    for pc in args.venues:
        venue = VENUE_CODES.get(pc, str(pc))
        logger.info("--- %s (pc=%d) ---", venue, pc)
        for race_no in range(1, 13):
            df = predict_race(client, model, iso, meta, pc, target_date, race_no)
            if df.empty:
                continue
            top1 = df[df["pred_rank"] == 1].copy()
            cands = top1[top1["ev_avg_calib"] >= args.thr].copy()
            if cands.empty:
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

    if not picks.empty:
        append_picks_log(picks, time_label)
        logger.info("候補数: %d / 投資 ¥%d", len(picks), len(picks) * 100)
    else:
        logger.info("候補なし")

    if args.no_email:
        logger.info("--no-email: 送信スキップ")
        return

    try:
        from gmail_notify import send_email
    except Exception as e:
        logger.error("gmail_notify インポート失敗: %s", e)
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
