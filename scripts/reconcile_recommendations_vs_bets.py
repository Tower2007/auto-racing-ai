"""推奨候補 vs 実購入の照合(Codex R6 提案・優先 2 / R7 で独立スクリプト化)

⚠️ R 単位の照合(車番・券種は照合しない)
  本スクリプトは race_date × place_code × race_no の 3 キーでのみ突き合わせる。
  推奨が 6 号車 複勝でも、ユーザーが同 R で 3 号車 三連単を買えば「A=推奨かつ購入済」
  に分類される。Phase A の運用監視(レース単位の取りこぼし検知)としては許容だが、
  「推奨内容と購入内容の整合」を厳密に問うなら bet_history_detail.csv の
  car_no + bet_type まで含めた pick-level reconcile が別途必要。
  → 将来 TODO: Opinion/codex_briefs/pick_level_reconcile_proposal.md

Phase A の運用整合性を 4 カテゴリで分解する(R7 命名に統一):

  recommended_and_bought         (A): 推奨 ✓ / 購入 ✓ — 通常運用
  recommended_not_bought         (B): 推奨 ✓ / 購入 ✗ — 取りこぼし
  bought_not_recommended         (C): 推奨 ✗ / 購入 ✓ — 裁量 / 誤購入 / 別戦略
  snapshot_signal_not_recommended(D): snap EV ≥ thr / 推奨無し — 通知漏れ / 閾値跨ぎ

ソース:
  - data/daily_predict_picks.csv  (推奨候補・通知ログ)
  - data/bet_history.csv          (実購入記録、フォーマット集約: date×place×R)
  - data/odds_snapshots.csv       (発火時 odds スナップショット)

Standalone 実行:
  python scripts/reconcile_recommendations_vs_bets.py                 # 直近 7 日
  python scripts/reconcile_recommendations_vs_bets.py --days 30
  python scripts/reconcile_recommendations_vs_bets.py --all
  python scripts/reconcile_recommendations_vs_bets.py --csv-out data/reconcile.csv

CSV 出力には manual_reason 列を予約(後から手で埋める運用、初期値は空文字)。

weekly_status.py からは build_summary() + render_compact_text/html() を import:
weekly では件数のみ表示し、詳細は本スクリプトを単独実行することで確認する。
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


CATEGORIES = {
    "A": "recommended_and_bought",
    "B": "recommended_not_bought",
    "C": "bought_not_recommended",
    "D": "snapshot_signal_not_recommended",
}


def build_rows(days: int | None = 7, threshold: float = EV_THRESHOLD) -> pd.DataFrame:
    """4 カテゴリを 1 つの DataFrame に正規化(CSV 出力用)。

    列: race_date, place_code, race_no, category, manual_reason
    manual_reason は空文字で予約(後から手で埋める運用)。
    """
    s = build_summary(days, threshold, _internal_keys=True)
    frames = []
    for cat, keys_df in [
        ("A", s["_a_keys"]),
        ("B", s["_b_keys"]),
        ("C", s["_c_keys"]),
        ("D", s["_d_keys"]),
    ]:
        if keys_df is None or keys_df.empty:
            continue
        f = keys_df.copy()
        f["category"] = CATEGORIES[cat]
        f["category_short"] = cat
        frames.append(f)
    if not frames:
        return pd.DataFrame(columns=RACE_KEY + ["category", "category_short", "manual_reason"])
    out = pd.concat(frames, ignore_index=True)
    out["manual_reason"] = ""
    out = out[["race_date", "place_code", "race_no",
               "category_short", "category", "manual_reason"]]
    return out.sort_values(["race_date", "place_code", "race_no", "category_short"]).reset_index(drop=True)


def build_summary(days: int | None = 7, threshold: float = EV_THRESHOLD,
                   _internal_keys: bool = False) -> dict:
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

    out = {
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
    if _internal_keys:
        out["_a_keys"] = a
        out["_b_keys"] = b
        out["_c_keys"] = c
        out["_d_keys"] = d
    return out


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


def render_compact_text(s: dict) -> str:
    """weekly mail に載せる 1〜2 行の要約版(R7: 詳細は外す)。"""
    period = f"直近 {s['days']} 日" if s["days"] else "全期間"
    sign = "+" if s["c_pnl_total"] >= 0 else ""
    line1 = (
        f"📐 推奨 vs 購入 [R 単位] ({period}, thr={s['threshold']}): "
        f"A={s['a_ok']} / B={s['b_missed']} / C={s['c_discretion']} / D={s['d_unsent_above_thr']}"
    )
    line2 = (
        f"  (A=通常 / B=取りこぼし / C=裁量 {sign}¥{s['c_pnl_total']:,} / D=通知漏れ"
        f" — 詳細は scripts/reconcile_recommendations_vs_bets.py)"
    )
    return line1 + "\n" + line2


def render_compact_html(s: dict) -> str:
    """weekly mail HTML 用の 1 行サマリ + 凡例。"""
    period = f"直近 {s['days']} 日" if s["days"] else "全期間"
    sign = "+" if s["c_pnl_total"] >= 0 else ""
    pnl_color = "#2e7d32" if s["c_pnl_total"] >= 0 else "#c62828"

    def cell(n: int, label: str, color_if_nonzero: str = "#888") -> str:
        c = color_if_nonzero if n > 0 else "#888"
        return f'<b style="color:{c}">{label}={n}</b>'

    return (
        f'<h3 style="color:#444; margin:18px 0 6px 0;">📐 推奨 vs 購入 '
        f'<span style="font-weight:normal; color:#888; font-size:12px;">'
        f'[R 単位 / 車番・券種は別] ({period}, thr={s["threshold"]})</span></h3>'
        f'<p style="margin:4px 0; font-size:13px;">'
        f'{cell(s["a_ok"], "A 通常", "#2e7d32")} &nbsp;/&nbsp; '
        f'{cell(s["b_missed"], "B 取りこぼし", "#c62828")} &nbsp;/&nbsp; '
        f'{cell(s["c_discretion"], "C 裁量", "#e65100")} &nbsp;/&nbsp; '
        f'{cell(s["d_unsent_above_thr"], "D 通知漏れ", "#e65100")}'
        f'</p>'
        f'<p style="margin:2px 0; font-size:12px; color:#666;">'
        f'C 損益: <span style="color:{pnl_color}">{sign}¥{s["c_pnl_total"]:,}</span>'
        f' &nbsp;|&nbsp; 詳細: <code>scripts/reconcile_recommendations_vs_bets.py</code></p>'
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--all", action="store_true")
    p.add_argument("--threshold", type=float, default=EV_THRESHOLD)
    p.add_argument("--csv-out", type=str, default=None,
                   help="4 カテゴリの明細を CSV に出力(manual_reason 列を予約)")
    args = p.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    days = None if args.all else args.days
    s = build_summary(days, args.threshold)
    print(render_text(s))

    if args.csv_out:
        rows = build_rows(days, args.threshold)
        out_path = Path(args.csv_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"\n[csv-out] {len(rows)} rows → {out_path}")


if __name__ == "__main__":
    main()
