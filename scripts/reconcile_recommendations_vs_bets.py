"""推奨候補 vs 実購入の照合(Codex R6 提案・優先 2)

Phase A の運用整合性を 4 カテゴリで分解する:

  A. 推奨 ✓ / 購入 ✓: 通常運用
  B. 推奨 ✓ / 購入 ✗: 取りこぼし(ユーザー不在 / 締切超過 / オッズ急落 / 手動スキップ)
  C. 推奨 ✗ / 購入 ✓: 裁量購入 / 誤購入 / 別戦略
  D. snapshot EV ≥ thr / 推奨なし: retry 失敗 / dedup / 閾値跨ぎ

ソース:
  - data/daily_predict_picks.csv  (推奨候補・通知ログ)
  - data/bet_history.csv          (実購入記録、フォーマット集約: date×place×R)
  - data/odds_snapshots.csv       (発火時 odds スナップショット)

Standalone 実行:
  python scripts/reconcile_recommendations_vs_bets.py            # 直近 7 日
  python scripts/reconcile_recommendations_vs_bets.py --days 30
  python scripts/reconcile_recommendations_vs_bets.py --all

weekly_status.py から build_summary() / render_text() / render_html() を import 可能。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RACE_KEY = ["race_date", "place_code", "race_no"]

# Phase A の閾値(docs/ev_strategy_findings.md と同期)
EV_THRESHOLD = 1.50


def _load_picks(days: int | None) -> pd.DataFrame:
    p = DATA / "daily_predict_picks.csv"
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame(columns=RACE_KEY + ["car_no", "ev_avg_calib"])
    df = pd.read_csv(p)
    if df.empty:
        return df
    df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
    df = df.dropna(subset=["race_date"])
    if days is not None:
        cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days)
        df = df[df["race_date"] >= cutoff]
    df = df.sort_values("sent_at" if "sent_at" in df.columns else "race_date")
    df = df.drop_duplicates(subset=RACE_KEY + ["car_no"], keep="first").reset_index(drop=True)
    return df


def _load_bets(days: int | None) -> pd.DataFrame:
    p = DATA / "bet_history.csv"
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame(columns=["race_date", "place_code", "race_no",
                                     "bet_amount", "refund_amount", "profit"])
    df = pd.read_csv(p)
    if df.empty:
        return df
    df["race_date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["race_date"])
    if days is not None:
        cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days)
        df = df[df["race_date"] >= cutoff]
    df["place_code"] = df["place_code"].astype(int)
    df["race_no"] = df["race_no"].astype(int)
    return df[["race_date", "place_code", "race_no",
               "bet_amount", "refund_amount", "profit"]]


def _load_snapshots(days: int | None, threshold: float) -> pd.DataFrame:
    """snapshot 上で EV ≥ threshold の top-1 候補のみ返す。"""
    p = DATA / "odds_snapshots.csv"
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame(columns=RACE_KEY + ["car_no", "ev_avg_calib"])
    df = pd.read_csv(p)
    if df.empty:
        return df
    df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
    df = df.dropna(subset=["race_date"])
    if days is not None:
        cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days)
        df = df[df["race_date"] >= cutoff]
    if "pred_rank" in df.columns:
        df = df[df["pred_rank"] == 1]
    df = df[df["ev_avg_calib"] >= threshold]
    # 同 R 複数 snapshot は最新を採用
    sort_col = "captured_at" if "captured_at" in df.columns else "race_date"
    df = df.sort_values(sort_col).drop_duplicates(
        subset=RACE_KEY + ["car_no"], keep="last"
    ).reset_index(drop=True)
    return df


def build_summary(days: int | None = 7, threshold: float = EV_THRESHOLD) -> dict:
    """4 カテゴリの集計を返す。"""
    picks = _load_picks(days)
    bets = _load_bets(days)
    snaps = _load_snapshots(days, threshold)

    picks_keys = picks[RACE_KEY].drop_duplicates() if not picks.empty else \
        pd.DataFrame(columns=RACE_KEY)
    bets_keys = bets[RACE_KEY].drop_duplicates() if not bets.empty else \
        pd.DataFrame(columns=RACE_KEY)
    snap_keys = snaps[RACE_KEY].drop_duplicates() if not snaps.empty else \
        pd.DataFrame(columns=RACE_KEY)

    # race_results.csv で「結果確定済」の (date, place) を絞り込む(まだ走ってない R を除外)
    res_p = DATA / "race_results.csv"
    if res_p.exists():
        res = pd.read_csv(res_p, usecols=["race_date", "place_code"]).drop_duplicates()
        res["race_date"] = pd.to_datetime(res["race_date"])
        res["place_code"] = res["place_code"].astype(int)
        res_set = set(zip(res["race_date"], res["place_code"]))
    else:
        res_set = None  # 全件確定扱い

    def _is_settled(row) -> bool:
        if res_set is None:
            return True
        return (row["race_date"], int(row["place_code"])) in res_set

    # A. 推奨 ✓ / 購入 ✓
    a = picks_keys.merge(bets_keys, on=RACE_KEY, how="inner")
    # B. 推奨 ✓ / 購入 ✗(ただし結果確定済の R に限る、未走 R は除く)
    b_all = picks_keys.merge(bets_keys, on=RACE_KEY, how="left", indicator=True)
    b = b_all[b_all["_merge"] == "left_only"].drop(columns=["_merge"])
    if not b.empty:
        b = b[b.apply(_is_settled, axis=1)].reset_index(drop=True)
    # C. 推奨 ✗ / 購入 ✓
    c_all = bets_keys.merge(picks_keys, on=RACE_KEY, how="left", indicator=True)
    c = c_all[c_all["_merge"] == "left_only"].drop(columns=["_merge"])
    # D. snapshot EV ≥ thr 但し picks に通知履歴が無い
    if not snap_keys.empty:
        d_all = snap_keys.merge(picks_keys, on=RACE_KEY, how="left", indicator=True)
        d = d_all[d_all["_merge"] == "left_only"].drop(columns=["_merge"])
    else:
        d = pd.DataFrame(columns=RACE_KEY)

    # C カテゴリの収益(裁量購入の損益も気になる)
    c_pnl = bets.merge(c, on=RACE_KEY, how="inner") if not c.empty and not bets.empty \
        else pd.DataFrame()

    return {
        "days": days,
        "threshold": threshold,
        "n_picks": int(len(picks_keys)),
        "n_bets": int(len(bets_keys)),
        "n_snap_above_thr": int(len(snap_keys)),
        "a_ok": int(len(a)),
        "b_missed": int(len(b)),
        "c_discretion": int(len(c)),
        "d_unsent_above_thr": int(len(d)),
        "b_details": b.head(10).to_dict("records") if not b.empty else [],
        "c_details": c.head(10).to_dict("records") if not c.empty else [],
        "d_details": d.head(10).to_dict("records") if not d.empty else [],
        "c_pnl_total": int(c_pnl["profit"].sum()) if not c_pnl.empty else 0,
    }


def render_text(s: dict) -> str:
    period = f"直近 {s['days']} 日" if s["days"] else "全期間"
    lines = [
        f"📐 推奨 vs 購入 整合監査({period}, thr={s['threshold']})",
        "=" * 56,
        f"  推奨 R 数:        {s['n_picks']:>4d}",
        f"  購入 R 数:        {s['n_bets']:>4d}",
        f"  snapshot 上 EV>=thr R: {s['n_snap_above_thr']:>4d}",
        "",
        f"  A. 推奨✓ / 購入✓:           {s['a_ok']:>4d}  (通常運用)",
        f"  B. 推奨✓ / 購入✗ (確定済): {s['b_missed']:>4d}  (取りこぼし)",
        f"  C. 推奨✗ / 購入✓:           {s['c_discretion']:>4d}  (裁量 / 誤購入)",
        f"  D. snap EV>=thr / 推奨無し: {s['d_unsent_above_thr']:>4d}  (通知失敗 / 閾値跨ぎ)",
    ]
    if s["c_discretion"] > 0:
        sign = "+" if s["c_pnl_total"] >= 0 else ""
        lines.append(f"     C 損益: {sign}¥{s['c_pnl_total']:,}")
    if s["b_details"]:
        lines.append("")
        lines.append("  [B 詳細(先頭 10 件)]")
        for r in s["b_details"]:
            lines.append(f"    - {r['race_date']:%Y-%m-%d} 場{int(r['place_code'])} R{int(r['race_no'])}")
    if s["d_details"]:
        lines.append("")
        lines.append("  [D 詳細(先頭 10 件)]")
        for r in s["d_details"]:
            lines.append(f"    - {r['race_date']:%Y-%m-%d} 場{int(r['place_code'])} R{int(r['race_no'])}")
    return "\n".join(lines)


def render_html(s: dict) -> str:
    period = f"直近 {s['days']} 日" if s["days"] else "全期間"
    BORDER = '"border-collapse:collapse; border-color:#bbb; font-family:Arial,sans-serif; font-size:13px;"'
    TH = '"background:#e8e8e8; padding:6px 10px; border:1px solid #bbb; text-align:center;"'
    TD = '"padding:6px 10px; border:1px solid #ddd; text-align:left;"'
    TD_R = '"padding:6px 10px; border:1px solid #ddd; text-align:right;"'

    parts = [
        f'<h3 style="color:#444; margin:18px 0 8px 0;">📐 推奨 vs 購入 整合監査'
        f' <span style="color:#888; font-weight:normal; font-size:12px;">'
        f'({period}, thr={s["threshold"]})</span></h3>',
        f'<table border="1" cellpadding="6" cellspacing="0" style={BORDER}>',
        f'<tr><th style={TH}>カテゴリ</th><th style={TH}>件数</th><th style={TH}>意味</th></tr>',
    ]
    rows = [
        ("A. 推奨✓ / 購入✓", s["a_ok"], "通常運用", "#2e7d32"),
        ("B. 推奨✓ / 購入✗", s["b_missed"], "取りこぼし(不在・締切・手動 skip)",
         "#c62828" if s["b_missed"] > 0 else "#888"),
        ("C. 推奨✗ / 購入✓", s["c_discretion"], "裁量 / 誤購入 / 別戦略",
         "#e65100" if s["c_discretion"] > 0 else "#888"),
        ("D. snap EV≥thr / 推奨無し", s["d_unsent_above_thr"],
         "retry 失敗 / dedup / 閾値跨ぎ",
         "#e65100" if s["d_unsent_above_thr"] > 0 else "#888"),
    ]
    for i, (label, n, note, color) in enumerate(rows):
        alt = ' style="background:#fafafa;"' if i % 2 == 1 else ""
        parts.append(
            f"<tr{alt}>"
            f'<td style={TD}>{label}</td>'
            f'<td style={TD_R}><b style="color:{color}">{n}</b></td>'
            f'<td style={TD}>{note}</td>'
            f"</tr>"
        )
    parts.append("</table>")

    if s["c_discretion"] > 0:
        sign = "+" if s["c_pnl_total"] >= 0 else ""
        color = "#2e7d32" if s["c_pnl_total"] >= 0 else "#c62828"
        parts.append(
            f'<p style="margin:4px 0; font-size:12px;">'
            f'C 損益: <b style="color:{color}">{sign}¥{s["c_pnl_total"]:,}</b></p>'
        )

    if s["b_details"] or s["d_details"]:
        parts.append('<details style="margin-top:6px;"><summary style="cursor:pointer; color:#666; font-size:12px;">B/D 詳細(先頭 10 件)</summary>')
        if s["b_details"]:
            parts.append('<p style="margin:4px 0 2px 0; font-size:12px; color:#c62828;"><b>B (取りこぼし)</b></p>')
            parts.append('<ul style="margin:0 0 6px 18px; font-size:12px;">')
            for r in s["b_details"]:
                parts.append(f"<li>{r['race_date']:%Y-%m-%d} 場{int(r['place_code'])} R{int(r['race_no'])}</li>")
            parts.append("</ul>")
        if s["d_details"]:
            parts.append('<p style="margin:4px 0 2px 0; font-size:12px; color:#e65100;"><b>D (通知漏れ?)</b></p>')
            parts.append('<ul style="margin:0 0 6px 18px; font-size:12px;">')
            for r in s["d_details"]:
                parts.append(f"<li>{r['race_date']:%Y-%m-%d} 場{int(r['place_code'])} R{int(r['race_no'])}</li>")
            parts.append("</ul>")
        parts.append("</details>")
    return "\n".join(parts)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--all", action="store_true")
    p.add_argument("--threshold", type=float, default=EV_THRESHOLD)
    args = p.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    days = None if args.all else args.days
    s = build_summary(days, args.threshold)
    print(render_text(s))


if __name__ == "__main__":
    main()
