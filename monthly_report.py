"""月次収支レポート生成スクリプト (auto-racing-ai).

週次の死活監視 (`weekly_status.py`) とは別に、1 か月単位で
推奨(仮想)収支・実購入収支・通算収支をまとめて CSV / Gmail HTML レポートにする。

【設計方針】
- 計算ロジックは既存の `scripts/picks_audit.py` (推奨=複勝 top-1 の仮想収支) と
  `scripts/bet_history_summary.py` (実購入) を import して共有する
  (weekly_status と同じ評価ルールを担保)。
- 単価: 推奨(仮想)は ¥100/本 (picks_audit.BET)、実購入は投票履歴の実額。
- グラフは画像を使わず HTML/CSS 横棒バーで描画 (Gmail 含む全クライアントで表示)。

【レポート構成】
  A. 当月サマリ        … 推奨(仮想 複勝top1) と 実購入 の 投資/回収/収支/ROI
  B. 月次 ROI 推移      … 推奨(仮想) と 実購入 の月別 ROI バー
  C. 実購入 券種別収支  … 当月の 複勝/三連単/三連複 別 投資・回収・収支・ROI
  D. 場別収支 (当月)    … 推奨(仮想) と 実購入 を場別に
  E. レース別 推奨と結果 … 当月の推奨ピック (場/R/車/EV/的中/仮想損益)
  F. 通算収支          … 推奨(仮想) + 実購入 の全期間

【実行例】
  python monthly_report.py                      # 前月分 (自動)
  python monthly_report.py --month 2026-05      # 月指定
  python monthly_report.py --month 2026-05 --send-email
  python monthly_report.py --dry-run            # 送信せず本文表示

【自動実行】
  Windows タスク `AutoraceMonthlyReport` (毎月 1 日 08:00) で前月分を送信:
    python monthly_report.py --send-email
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from collections import OrderedDict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import picks_audit as pa  # noqa: E402  推奨(仮想 複勝top1)
import bet_history_summary as bh  # noqa: E402  実購入

REPORT_DIR = ROOT / "reports" / "monthly"
VENUE_NAMES = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
BET = pa.BET  # ¥100


# ─────────────────────────────────────────────────────────────
# 期間決定
# ─────────────────────────────────────────────────────────────

def default_month() -> tuple[int, int]:
    """前月 (year, month)。"""
    first = dt.date.today().replace(day=1)
    last_prev = first - dt.timedelta(days=1)
    return last_prev.year, last_prev.month


def month_range(year: int, month: int) -> tuple[dt.date, dt.date]:
    start = dt.date(year, month, 1)
    if month == 12:
        end = dt.date(year, 12, 31)
    else:
        end = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return start, end


def _iter_months(start_ym: str, end_ym: str):
    y, m = map(int, start_ym.split("-"))
    ey, em = map(int, end_ym.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ─────────────────────────────────────────────────────────────
# 推奨(仮想 複勝top1) — picks_audit を共有
# ─────────────────────────────────────────────────────────────

def build_virtual_audit() -> pd.DataFrame:
    """全期間の推奨ピックに実払戻を紐付けた audit DataFrame。"""
    return pa.attach_results(pa.load_picks())


def _virtual_agg(sub: pd.DataFrame) -> dict:
    """推奨ピック群 (¥100/本) の集計。ROI は %。"""
    if sub is None or sub.empty:
        return {"n_total": 0, "n_settled": 0, "n_pending": 0, "n_hits": 0,
                "hit_rate": None, "bet": 0, "return": 0, "profit": 0, "roi": None}
    settled = sub[sub["has_result"]]
    n_settled = int(len(settled))
    cost = n_settled * BET
    payout = float(settled["payout"].sum())
    hits = int(settled["hit"].sum())
    return {
        "n_total": int(len(sub)), "n_settled": n_settled,
        "n_pending": int(len(sub) - n_settled), "n_hits": hits,
        "hit_rate": (hits / n_settled * 100 if n_settled else None),
        "bet": int(cost), "return": int(round(payout)),
        "profit": int(round(payout - cost)),
        "roi": (payout / cost * 100 if cost else None),
    }


def _filter_month(audit: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    if audit is None or audit.empty:
        return audit
    d = audit["race_date"].dt.date
    return audit[(d >= start) & (d <= end)].copy()


def virtual_by_venue(sub: pd.DataFrame) -> list[dict]:
    """当月推奨の場別 (ROI %)。"""
    out = []
    if sub is None or sub.empty:
        return out
    settled = sub[sub["has_result"]]
    for pc, name in VENUE_NAMES.items():
        v = settled[settled["place_code"] == pc]
        if v.empty:
            continue
        cost = len(v) * BET
        payout = float(v["payout"].sum())
        out.append({
            "place_code": pc, "place_name": name, "n": int(len(v)),
            "n_hits": int(v["hit"].sum()),
            "bet": int(cost), "return": int(round(payout)),
            "profit": int(round(payout - cost)),
            "roi": (payout / cost * 100 if cost else None),
        })
    return out


def virtual_monthly_trend(audit: pd.DataFrame) -> list[dict]:
    """決着済み推奨を月別 (race_date[:7]) に集約した ROI 推移 (%)。"""
    if audit is None or audit.empty:
        return []
    settled = audit[audit["has_result"]].copy()
    if settled.empty:
        return []
    settled["ym"] = settled["race_date"].dt.strftime("%Y-%m")
    out = []
    for ym, g in settled.groupby("ym"):
        cost = len(g) * BET
        payout = float(g["payout"].sum())
        out.append({"month": ym, "bet": int(cost), "return": int(round(payout)),
                    "profit": int(round(payout - cost)),
                    "roi": (payout / cost * 100 if cost else None)})
    out.sort(key=lambda r: r["month"])
    return out


# ─────────────────────────────────────────────────────────────
# 実購入 — bet_history_summary を共有
# ─────────────────────────────────────────────────────────────

def _filter_month_actual(df: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    return df[(df["date"] >= start) & (df["date"] <= end)].copy()


def actual_monthly_trend(summary: pd.DataFrame) -> list[dict]:
    """実購入 (bet_history.csv) を月別に集約した ROI 推移 (%)。"""
    if summary is None or summary.empty:
        return []
    df = summary.copy()
    df["ym"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m")
    out = []
    for ym, g in df.groupby("ym"):
        a = bh._agg(g)
        out.append({"month": ym, "bet": a["bet"], "return": a["refund"],
                    "profit": a["profit"],
                    "roi": (a["roi"] if a["bet"] else None)})
    out.sort(key=lambda r: r["month"])
    return out


# ─────────────────────────────────────────────────────────────
# 当月データ構築
# ─────────────────────────────────────────────────────────────

def build_month_data(start: dt.date, end: dt.date,
                     audit: pd.DataFrame,
                     summary: pd.DataFrame | None,
                     detail: pd.DataFrame | None) -> dict:
    # 推奨(仮想)
    v_sub = _filter_month(audit, start, end)
    v_summary = _virtual_agg(v_sub)
    v_venues = virtual_by_venue(v_sub)

    # 実購入
    a_sub = None
    if summary is not None:
        a_sub = _filter_month_actual(summary, start, end)
        if a_sub is not None and not a_sub.empty:
            a_summary = bh._agg(a_sub)
            a_venues = bh.by_venue(a_sub)
        else:
            a_summary = {"n": 0, "bet": 0, "refund": 0, "profit": 0, "roi": 0.0}
            a_venues = []
    else:
        a_summary = {"n": 0, "bet": 0, "refund": 0, "profit": 0, "roi": 0.0}
        a_venues = []
    if detail is not None:
        d_sub = _filter_month_actual(detail, start, end)
        bet_types = bh.by_bet_type(d_sub) if (d_sub is not None and not d_sub.empty) else []
    else:
        bet_types = []

    # 実購入を (date, place_code, race_no) でレース単位に集約 (複数券種は合算)
    actual_by_race: dict[tuple, dict] = {}
    if a_sub is not None and not a_sub.empty:
        for _, r in a_sub.iterrows():
            key = (r["date"].isoformat() if hasattr(r["date"], "isoformat")
                   else str(r["date"]), int(r["place_code"]), int(r["race_no"]))
            acc = actual_by_race.setdefault(key, {"bet": 0, "return": 0, "profit": 0})
            acc["bet"] += int(r.get("bet_amount", 0) or 0)
            acc["return"] += int(r.get("refund_amount", 0) or 0)
            acc["profit"] += int(r.get("profit", 0) or 0)

    # レース別: 推奨ピックと実購入をマージ (どちらか一方しか無い R も含める)
    rows_by_key: dict[tuple, dict] = {}
    if v_sub is not None and not v_sub.empty:
        for _, r in v_sub.iterrows():
            key = (r["race_date"].date().isoformat(), int(r["place_code"]),
                   int(r["race_no"]))
            if r["has_result"]:
                result = "的中" if r["hit"] else "外"
            else:
                result = "結果待ち"
            rows_by_key[key] = {
                "race_date": key[0],
                "venue": r.get("venue", VENUE_NAMES.get(int(r["place_code"]), "?")),
                "race_no": key[2],
                "car_no": int(r["car_no"]),
                "ev": float(r.get("ev_avg_calib", 0) or 0),
                "result": result,
                "v_profit": (int(round(float(r["payout"]) - BET))
                             if r["has_result"] else None),
                "a_bet": None, "a_return": None, "a_profit": None,
            }
    # 実購入をマージ (推奨が無い R は推奨側を空欄で行追加)
    for key, acc in actual_by_race.items():
        row = rows_by_key.get(key)
        if row is None:
            row = {
                "race_date": key[0],
                "venue": VENUE_NAMES.get(key[1], str(key[1])),
                "race_no": key[2], "car_no": None, "ev": None,
                "result": "(推奨外)", "v_profit": None,
                "a_bet": None, "a_return": None, "a_profit": None,
            }
            rows_by_key[key] = row
        row["a_bet"] = acc["bet"]
        row["a_return"] = acc["return"]
        row["a_profit"] = acc["profit"]
    race_rows = [rows_by_key[k] for k in sorted(rows_by_key)]

    return {"virtual": v_summary, "v_venues": v_venues,
            "actual": a_summary, "a_venues": a_venues,
            "bet_types": bet_types, "race_rows": race_rows}


# ─────────────────────────────────────────────────────────────
# 表示ヘルパー
# ─────────────────────────────────────────────────────────────

def _roi(v) -> str:
    return f"{v:.1f}%" if v is not None else "—"


def _color_profit_html(v: int) -> str:
    if v > 0:
        return f'<span style="color:#c00;font-weight:bold;">+{v:,}</span>'
    if v < 0:
        return f'<span style="color:#06c;">{v:,}</span>'
    return f"<span>{v:,}</span>"


def _roi_bar_html(label: str, roi, *, width_max: float = 200.0, sub: str = "") -> str:
    if roi is None:
        pct, color, roi_txt = 0.0, "#bbb", "—"
    else:
        pct = max(0.0, min(roi, width_max)) / width_max * 100
        color = "#c00" if roi >= 100 else ("#e8a308" if roi >= 50 else "#06c")
        roi_txt = f"{roi:.1f}%"
    return (
        '<div style="display:flex;align-items:center;margin:3px 0;font-size:0.9em;">'
        f'<div style="width:120px;flex:none;color:#444;">{label}</div>'
        '<div style="flex:1;background:#eee;border-radius:3px;position:relative;height:18px;">'
        f'<div style="position:absolute;left:{100/width_max*100:.1f}%;top:0;bottom:0;'
        'width:1px;background:#999;"></div>'
        f'<div style="width:{pct:.1f}%;background:{color};height:100%;border-radius:3px;"></div>'
        '</div>'
        f'<div style="width:140px;flex:none;text-align:right;color:#333;">'
        f'{roi_txt}<small style="color:#888;"> {sub}</small></div>'
        '</div>'
    )


# ─────────────────────────────────────────────────────────────
# テキスト出力
# ─────────────────────────────────────────────────────────────

def render_text(month_label: str, data: dict, v_trend: list, a_trend: list,
                cum_virtual: dict, cum_actual: dict) -> str:
    v, a = data["virtual"], data["actual"]
    L = ["=" * 60, f"月次収支レポート ({month_label})", "=" * 60, ""]
    L.append("【当月サマリ】")
    L.append(f"  推奨(仮想 複勝top1): 投資 ¥{v['bet']:,} / 回収 ¥{v['return']:,} "
             f"/ 収支 {v['profit']:+,}円 / ROI {_roi(v['roi'])} "
             f"(命中 {v['n_hits']}/{v['n_settled']}, 結果待ち {v['n_pending']})")
    L.append(f"  実購入             : 投資 ¥{a['bet']:,} / 回収 ¥{a['refund']:,} "
             f"/ 収支 {a['profit']:+,}円 / ROI {_roi(a['roi'] if a['bet'] else None)} "
             f"({a['n']}件)")
    L.append("")

    L.append("【月次 ROI 推移】")
    L.append("  月       推奨(仮想)            実購入")
    vmap = {m["month"]: m for m in v_trend}
    amap = {m["month"]: m for m in a_trend}
    for ym in sorted(set(list(vmap) + list(amap))):
        vv, aa = vmap.get(ym), amap.get(ym)
        v_txt = f"{_roi(vv['roi'])} ({vv['profit']:+,}円)" if vv else "—"
        a_txt = f"{_roi(aa['roi'])} ({aa['profit']:+,}円)" if aa else "—"
        L.append(f"  {ym}  {v_txt:<22} {a_txt}")
    L.append("")

    if data["bet_types"]:
        L.append("【実購入 券種別収支 (当月)】")
        for b in data["bet_types"]:
            L.append(f"  {b['bet_type_label']:<6} 投資 ¥{b['bet']:,} / 回収 ¥{b['refund']:,} "
                     f"/ 収支 {b['profit']:+,}円 / ROI {_roi(b['roi'] if b['bet'] else None)}")
        L.append("")

    if data["a_venues"] or data["v_venues"]:
        L.append("【場別収支 (当月)】")
        L.append(f"  {'場':6s} {'推奨ROI':>9s} {'推奨損益':>10s} | {'実購入ROI':>9s} {'実購入損益':>10s}")
        vv = {x["place_code"]: x for x in data["v_venues"]}
        av = {x["place_code"]: x for x in data["a_venues"]}
        for pc, name in VENUE_NAMES.items():
            if pc not in vv and pc not in av:
                continue
            x = vv.get(pc)
            y = av.get(pc)
            xr = _roi(x["roi"]) if x else "—"
            xp = f"{x['profit']:+,}" if x else "—"
            yr = _roi(y["roi"]) if y else "—"
            yp = f"{y['profit']:+,}" if y else "—"
            L.append(f"  {name:6s} {xr:>9s} {xp:>10s} | {yr:>9s} {yp:>10s}")
        L.append("")

    L.append("【レース別 推奨と結果 (当月)】")
    if not data["race_rows"]:
        L.append("  推奨/購入なし")
    for r in data["race_rows"]:
        car = f"車{r['car_no']}" if r["car_no"] is not None else "  - "
        ev = f"EV{r['ev']:.2f}" if r["ev"] is not None else "EV  - "
        vp = f"仮想{r['v_profit']:+,}円" if r["v_profit"] is not None else "仮想    - "
        if r["a_bet"] is not None:
            ap = f"実購入{r['a_profit']:+,}円(投{r['a_bet']:,}/戻{r['a_return']:,})"
        else:
            ap = "実購入 -"
        L.append(f"  {r['race_date']} {r['venue']:8s} R{r['race_no']:<2d} "
                 f"{car} {ev} {r['result']:7s} {vp:<12s} {ap}")
    if data["race_rows"]:
        tv = sum(r["v_profit"] or 0 for r in data["race_rows"])
        ta = sum(r["a_profit"] or 0 for r in data["race_rows"])
        L.append(f"  合計: 仮想{tv:+,}円 / 実購入{ta:+,}円")
    L.append("")

    L.append("【通算収支 (全期間)】")
    if cum_virtual and cum_virtual["n_settled"] > 0:
        L.append(f"  推奨(仮想): 投資 ¥{cum_virtual['bet']:,} / 回収 ¥{cum_virtual['return']:,} "
                 f"/ 収支 {cum_virtual['profit']:+,}円 / ROI {_roi(cum_virtual['roi'])} "
                 f"(命中 {cum_virtual['n_hits']}/{cum_virtual['n_settled']})")
    if cum_actual and cum_actual.get("n", 0) > 0:
        L.append(f"  実購入   : {cum_actual['date_min']}〜{cum_actual['date_max']} / "
                 f"投資 ¥{cum_actual['bet']:,} / 回収 ¥{cum_actual['refund']:,} "
                 f"/ 収支 {cum_actual['profit']:+,}円 / ROI {_roi(cum_actual['roi'] if cum_actual['bet'] else None)}")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────
# HTML 出力
# ─────────────────────────────────────────────────────────────

def render_html(month_label: str, data: dict, v_trend: list, a_trend: list,
                cum_virtual: dict, cum_actual: dict) -> str:
    v, a = data["virtual"], data["actual"]
    css = '''<style>
    body{font-family:'Yu Gothic UI','Meiryo',sans-serif;max-width:820px;
         margin:0 auto;padding:16px;color:#222;}
    h2{color:#c62828;border-bottom:2px solid #c62828;padding-bottom:4px;}
    h3{color:#444;margin-top:26px;}
    table{border-collapse:collapse;width:100%;margin:8px 0;font-size:0.9em;}
    th{background:#333;color:#fff;padding:6px 8px;text-align:left;white-space:nowrap;}
    td{padding:5px 8px;border-bottom:1px solid #eee;}
    .box{background:#fff5f5;border-left:4px solid #c62828;padding:12px 16px;
         border-radius:4px;margin:12px 0;}
    .note{color:#666;font-size:0.85em;margin-top:16px;border-top:1px solid #ccc;
          padding-top:8px;}
    </style>'''
    out = ['<!DOCTYPE html><html><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           css, '</head><body>']
    out.append(f'<h2>🗓 月次収支レポート ({month_label})</h2>')

    _append_summary_html(out, v, a)
    _append_trend_html(out, v_trend, a_trend)
    _append_bet_types_html(out, data)
    _append_venues_html(out, data)
    _append_race_rows_html(out, data, v, a)
    _append_cumulative_html(out, cum_virtual, cum_actual)

    out.append('<div class="note">')
    out.append(f'※ 推奨(仮想) は複勝 top-1 推奨を <b>1本 {BET}円</b> で買った仮想収支 '
               '(picks_audit と共通、結果取込済みのみ集計)。<br>')
    out.append('※ 実購入は vote.autorace.jp 投票履歴ベースの実額 (複勝/三連単/三連複 含む)。<br>')
    out.append('※ 控除率 25-30% のため長期 ROI は理論上 70-75%。エンタメ用途。')
    out.append('</div></body></html>')
    return "\n".join(out)


def _append_summary_html(out: list[str], v: dict, a: dict) -> None:
    """A. 当月サマリ (render_html の下請け)。"""
    # A. 当月サマリ
    out.append('<div class="box"><h3 style="margin:0 0 8px 0;">当月サマリ</h3>')
    out.append('<table><thead><tr><th>区分</th><th>投資</th><th>回収</th>'
               '<th>収支</th><th>ROI</th><th>備考</th></tr></thead><tbody>')
    out.append(f'<tr><td><b>推奨(仮想 複勝top1)</b></td><td>¥{v["bet"]:,}</td>'
               f'<td>¥{v["return"]:,}</td><td>{_color_profit_html(v["profit"])}円</td>'
               f'<td>{_roi(v["roi"])}</td>'
               f'<td>命中 {v["n_hits"]}/{v["n_settled"]} / 結果待ち {v["n_pending"]}</td></tr>')
    out.append(f'<tr><td><b>実購入</b></td><td>¥{a["bet"]:,}</td>'
               f'<td>¥{a["refund"]:,}</td><td>{_color_profit_html(a["profit"])}円</td>'
               f'<td>{_roi(a["roi"] if a["bet"] else None)}</td>'
               f'<td>{a["n"]}件</td></tr>')
    out.append('</tbody></table></div>')


def _append_trend_html(out: list[str], v_trend: list, a_trend: list) -> None:
    """B. 月次 ROI 推移 (render_html の下請け)。"""
    # B. 月次 ROI 推移
    out.append('<h3>月次 ROI 推移</h3>')
    out.append('<div style="font-size:0.85em;color:#666;margin-bottom:4px;">'
               '基準線 = ROI 100% (回収=投資)</div>')
    vmap = {m["month"]: m for m in v_trend}
    amap = {m["month"]: m for m in a_trend}
    for ym in sorted(set(list(vmap) + list(amap))):
        out.append(f'<div style="margin:8px 0 4px;font-weight:bold;color:#555;">{ym}</div>')
        vv, aa = vmap.get(ym), amap.get(ym)
        out.append(_roi_bar_html("推奨(仮想)", vv["roi"] if vv else None,
                                 sub=(f'{vv["profit"]:+,}円' if vv else "")))
        out.append(_roi_bar_html("実購入", aa["roi"] if aa else None,
                                 sub=(f'{aa["profit"]:+,}円' if aa else "")))


def _append_bet_types_html(out: list[str], data: dict) -> None:
    """C. 実購入 券種別収支 (render_html の下請け)。"""
    # C. 実購入 券種別
    if data["bet_types"]:
        out.append('<h3>実購入 券種別収支 (当月)</h3>')
        out.append('<table><thead><tr><th>券種</th><th>件</th><th>投資</th>'
                   '<th>回収</th><th>収支</th><th>ROI</th></tr></thead><tbody>')
        for b in data["bet_types"]:
            out.append(f'<tr><td><b>{b["bet_type_label"]}</b></td><td>{b["n"]}</td>'
                       f'<td>¥{b["bet"]:,}</td><td>¥{b["refund"]:,}</td>'
                       f'<td>{_color_profit_html(b["profit"])}</td>'
                       f'<td>{_roi(b["roi"] if b["bet"] else None)}</td></tr>')
        out.append('</tbody></table>')


def _append_venues_html(out: list[str], data: dict) -> None:
    """D. 場別収支 (render_html の下請け)。"""
    # D. 場別収支
    if data["a_venues"] or data["v_venues"]:
        out.append('<h3>場別収支 (当月)</h3>')
        out.append('<table><thead><tr><th>場</th><th>推奨ROI</th><th>推奨損益</th>'
                   '<th>実購入ROI</th><th>実購入損益</th></tr></thead><tbody>')
        vv = {x["place_code"]: x for x in data["v_venues"]}
        av = {x["place_code"]: x for x in data["a_venues"]}
        for pc, name in VENUE_NAMES.items():
            if pc not in vv and pc not in av:
                continue
            x, y = vv.get(pc), av.get(pc)
            out.append(
                f'<tr><td>{name}</td>'
                f'<td>{_roi(x["roi"]) if x else "—"}</td>'
                f'<td>{_color_profit_html(x["profit"]) if x else "—"}</td>'
                f'<td>{_roi(y["roi"]) if y else "—"}</td>'
                f'<td>{_color_profit_html(y["profit"]) if y else "—"}</td></tr>')
        out.append('</tbody></table>')


def _append_race_rows_html(out: list[str], data: dict, v: dict, a: dict) -> None:
    """E. レース別 推奨と結果 (render_html の下請け)。"""
    # E. レース別 推奨と結果 + 実購入
    out.append('<h3>レース別 推奨と結果 (当月)</h3>')
    if not data["race_rows"]:
        out.append('<p>推奨/購入なし</p>')
    else:
        out.append('<table><thead><tr><th>日</th><th>場</th><th>R</th><th>車</th>'
                   '<th>EV</th><th>結果</th><th>仮想損益</th>'
                   '<th>実購入投資</th><th>実購入損益</th></tr></thead><tbody>')
        for r in data["race_rows"]:
            rc = {"的中": "#c00", "外": "#06c", "結果待ち": "#888",
                  "(推奨外)": "#999"}.get(r["result"], "#444")
            car = r["car_no"] if r["car_no"] is not None else "—"
            ev = f'{r["ev"]:.2f}' if r["ev"] is not None else "—"
            vp = _color_profit_html(r["v_profit"]) if r["v_profit"] is not None else "—"
            abet = f'¥{r["a_bet"]:,}' if r["a_bet"] is not None else "—"
            ap = _color_profit_html(r["a_profit"]) if r["a_profit"] is not None else "—"
            out.append(f'<tr><td>{r["race_date"][5:]}</td><td>{r["venue"]}</td>'
                       f'<td>R{r["race_no"]}</td><td>{car}</td>'
                       f'<td>{ev}</td>'
                       f'<td style="color:{rc};">{r["result"]}</td>'
                       f'<td>{vp}</td><td>{abet}</td><td>{ap}</td></tr>')
        tr_v = sum(r["v_profit"] or 0 for r in data["race_rows"])
        tr_a = sum(r["a_profit"] or 0 for r in data["race_rows"])
        out.append('<tr style="background:#ffecec;font-weight:bold;">'
                   '<td colspan="6">合計</td>'
                   f'<td>{_color_profit_html(tr_v)}'
                   f'<br><small style="color:#555;">仮想ROI {_roi(v["roi"])}</small></td>'
                   '<td></td>'
                   f'<td>{_color_profit_html(tr_a)}'
                   f'<br><small style="color:#555;">実ROI {_roi(a["roi"] if a["bet"] else None)}</small></td></tr>')
        out.append('</tbody></table>')


def _append_cumulative_html(out: list[str], cum_virtual: dict, cum_actual: dict) -> None:
    """F. 通算収支 (render_html の下請け)。"""
    # F. 通算
    out.append('<h3>通算収支 (全期間)</h3>')
    out.append('<div class="box" style="background:#fffbeb;border-left-color:#a16207;">')
    if cum_virtual and cum_virtual["n_settled"] > 0:
        out.append(f'<div>推奨(仮想): 投資 <b>¥{cum_virtual["bet"]:,}</b> / '
                   f'回収 <b>¥{cum_virtual["return"]:,}</b> / '
                   f'収支 <b>{_color_profit_html(cum_virtual["profit"])}円</b> / '
                   f'ROI <b>{_roi(cum_virtual["roi"])}</b> '
                   f'(命中 {cum_virtual["n_hits"]}/{cum_virtual["n_settled"]})</div>')
    if cum_actual and cum_actual.get("n", 0) > 0:
        out.append(f'<div style="margin-top:4px;">実購入: '
                   f'<b>{cum_actual["date_min"]}〜{cum_actual["date_max"]}</b> / '
                   f'投資 <b>¥{cum_actual["bet"]:,}</b> / 回収 <b>¥{cum_actual["refund"]:,}</b> / '
                   f'収支 <b>{_color_profit_html(cum_actual["profit"])}円</b> / '
                   f'ROI <b>{_roi(cum_actual["roi"] if cum_actual["bet"] else None)}</b></div>')
    out.append('</div>')


# ─────────────────────────────────────────────────────────────
# CSV
# ─────────────────────────────────────────────────────────────

def write_csv(month_label: str, data: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"monthly_{month_label}.csv"
    fields = ["race_date", "venue", "race_no", "car_no", "ev", "result",
              "v_profit", "a_bet", "a_return", "a_profit"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in data["race_rows"]:
            w.writerow({k: r.get(k, "") for k in fields})
    return path


# ─────────────────────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--month", type=str, help="YYYY-MM (省略時: 前月)")
    ap.add_argument("--send-email", action="store_true")
    ap.add_argument("--to", type=str, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="--send-email 指定でも送信せず本文表示")
    args = ap.parse_args()

    if args.month:
        year, month = map(int, args.month.split("-"))
    else:
        year, month = default_month()
    start, end = month_range(year, month)
    month_label = f"{year:04d}-{month:02d}"
    print(f"集計対象月: {month_label} ({start} 〜 {end})", flush=True)

    # データ読込 (共有モジュール)
    audit = build_virtual_audit()
    summary = bh.load_summary()
    detail = bh.load_detail()

    data = build_month_data(start, end, audit, summary, detail)

    # 推移 (対象月以前・直近12か月)
    v_trend = [m for m in virtual_monthly_trend(audit) if m["month"] <= month_label][-12:]
    a_trend = ([m for m in actual_monthly_trend(summary) if m["month"] <= month_label][-12:]
               if summary is not None else [])

    # 通算
    cum_virtual = _virtual_agg(audit)
    cum_actual = None
    if summary is not None and not summary.empty:
        ca = bh._agg(summary)
        ca["date_min"] = pd.to_datetime(summary["date"]).min().date().isoformat()
        ca["date_max"] = pd.to_datetime(summary["date"]).max().date().isoformat()
        cum_actual = ca

    csv_path = write_csv(month_label, data)
    print(f"CSV保存: {csv_path}")

    text = render_text(month_label, data, v_trend, a_trend, cum_virtual, cum_actual)
    print("\n" + text, flush=True)

    if args.send_email and not args.dry_run:
        from gmail_notify import send_email
        subject = f"[autorace] 月次レポート {month_label}"
        body = text + f"\n\nCSV: {csv_path}\n"
        html = render_html(month_label, data, v_trend, a_trend, cum_virtual, cum_actual)
        recipients = ([a.strip() for a in args.to.split(",") if a.strip()]
                      if args.to else None)
        try:
            send_email(subject=subject, body=body, html=html, recipients=recipients)
            print("Gmail送信: OK")
        except Exception as ex:
            print(f"Gmail送信失敗: {ex}")


if __name__ == "__main__":
    main()
