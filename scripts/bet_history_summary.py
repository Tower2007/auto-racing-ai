"""
実購入履歴 (bet_history) 損益サマリ
-----------------------------------------------------------------------
data/bet_history.csv (R 単位) と data/bet_history_detail.csv (券種別 pack)
を読んで weekly_status.py / 単独 CLI 用に text / html を生成する。

ブロック:
  ① 直近 N 日 全体サマリ (R 数 / 投資 / 払戻 / 損益 / ROI)
  ② 直近 N 日 場別
  ③ 直近 N 日 券種別 (detail から)
  ④ 全期間累計 (R 数 / 投資 / 払戻 / 損益 / ROI)

使い方:
  python scripts/bet_history_summary.py            # 直近 7 日 + 全期間
  python scripts/bet_history_summary.py --days 30  # 直近 30 日
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SUMMARY_CSV = ROOT / "data" / "bet_history.csv"
DETAIL_CSV = ROOT / "data" / "bet_history_detail.csv"

VENUE_NAMES = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}

# ─── 三連系 (3連単 rt3 + 3連複 rf3) 実弾 分離集計 ─────────────────────────
# 2026-07-26 の Go/No-Go 判定は「三連系実弾が 100R / ROI>120% を維持するか」。
# bet_history.csv には券種列が無い (複勝混在) ため、券種付きの detail から
# 三連系のみを抽出して常設集計する。閾値は事前固定 (勝手に変えない)。
SANREN_BET_TYPES = ("rt3", "rf3")   # 3連単・3連複
SANREN_DECISION_MIN_N = 100         # 判定に必要な最小 R 数 (事前固定)
SANREN_DECISION_ROI = 120.0         # 維持すべき ROI 下限 % (事前固定)


def load_summary() -> pd.DataFrame | None:
    """bet_history.csv (R 単位) を読み、無ければ None。"""
    if not SUMMARY_CSV.exists():
        return None
    df = pd.read_csv(SUMMARY_CSV)
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def load_detail() -> pd.DataFrame | None:
    """bet_history_detail.csv (券種別 pack) を読み、無ければ None。"""
    if not DETAIL_CSV.exists():
        return None
    df = pd.read_csv(DETAIL_CSV)
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def _agg(df: pd.DataFrame) -> dict:
    """ベース集計: n, bet, refund, profit, roi%"""
    bet = int(df["bet_amount"].sum()) if "bet_amount" in df else int(df["vote_amount"].sum())
    if "refund_amount" in df:
        refund = int(df["refund_amount"].sum())
    else:
        refund = int(
            df["hit_amount"].sum()
            + df["henkan_amount"].sum()
            + df["tokubarai_amount"].sum()
        )
    n = len(df)
    profit = refund - bet
    roi = (refund / bet * 100) if bet else 0.0
    return {"n": n, "bet": bet, "refund": refund, "profit": profit, "roi": roi}


def filter_recent_n_days(df: pd.DataFrame, days: int) -> pd.DataFrame:
    """`今日 - days 〜 今日 - 1` の範囲。weekly_status の get_recent_days_status と整合。"""
    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    end = today - dt.timedelta(days=1)
    return df[(df["date"] >= start) & (df["date"] <= end)].copy()


def by_venue(df_period: pd.DataFrame) -> list[dict]:
    """場別集計 (R 数の降順は不要、VENUE_NAMES の順)。"""
    out = []
    for pc, name in VENUE_NAMES.items():
        sub = df_period[df_period["place_code"] == pc]
        if sub.empty:
            continue
        a = _agg(sub)
        a["place_code"] = pc
        a["place_name"] = name
        out.append(a)
    return out


def by_bet_type(detail_period: pd.DataFrame) -> list[dict]:
    """券種別 (pack 単位)。投資額大きい順。"""
    if detail_period is None or detail_period.empty:
        return []
    rows = []
    for (code, label), sub in detail_period.groupby(["bet_type_code", "bet_type_label"]):
        a = _agg(sub)
        a["bet_type_code"] = code
        a["bet_type_label"] = label
        rows.append(a)
    rows.sort(key=lambda r: -r["bet"])
    return rows


def _sanren_agg(detail: pd.DataFrame) -> dict:
    """三連系 (rt3+rf3) を R 単位に畳んで n / 的中 / 投資 / 払戻 / ROI を返す。

    pack (券種) 単位の detail を、同一 R (date, place_code, race_no) 内の
    三連系 pack をまとめて 1 R とカウントする。的中は R 単位で払戻>0 の R 数。
    払戻 = hit_amount + henkan_amount(返還) + tokubarai_amount(特払)。
    """
    empty = {"n": 0, "hits": 0, "hit_rate": 0.0, "bet": 0, "refund": 0,
             "profit": 0, "roi": 0.0}
    if detail is None or detail.empty:
        return empty
    sub = detail[detail["bet_type_code"].isin(SANREN_BET_TYPES)].copy()
    if sub.empty:
        return empty
    sub["_refund"] = (
        sub["hit_amount"].fillna(0)
        + sub["henkan_amount"].fillna(0)
        + sub["tokubarai_amount"].fillna(0)
    )
    grp = sub.groupby(["date", "place_code", "race_no"]).agg(
        bet=("vote_amount", "sum"), refund=("_refund", "sum")
    )
    n = int(len(grp))
    hits = int((grp["refund"] > 0).sum())
    bet = int(grp["bet"].sum())
    refund = int(grp["refund"].sum())
    profit = refund - bet
    roi = (refund / bet * 100) if bet else 0.0
    hit_rate = (hits / n * 100) if n else 0.0
    return {"n": n, "hits": hits, "hit_rate": hit_rate, "bet": bet,
            "refund": refund, "profit": profit, "roi": roi}


def _sanren_cumulative(detail: pd.DataFrame, tail: int = 8) -> list[dict]:
    """三連系 R を日付順に累積し、日別の累積推移 (末尾 tail 日) を返す。"""
    if detail is None or detail.empty:
        return []
    sub = detail[detail["bet_type_code"].isin(SANREN_BET_TYPES)].copy()
    if sub.empty:
        return []
    sub["_refund"] = (
        sub["hit_amount"].fillna(0)
        + sub["henkan_amount"].fillna(0)
        + sub["tokubarai_amount"].fillna(0)
    )
    perR = sub.groupby(["date", "place_code", "race_no"]).agg(
        bet=("vote_amount", "sum"), refund=("_refund", "sum")
    ).reset_index()
    perR["hit"] = (perR["refund"] > 0).astype(int)
    daily = perR.groupby("date").agg(
        n=("hit", "size"), hits=("hit", "sum"),
        bet=("bet", "sum"), refund=("refund", "sum")
    ).sort_index()
    daily["cum_n"] = daily["n"].cumsum()
    daily["cum_bet"] = daily["bet"].cumsum()
    daily["cum_refund"] = daily["refund"].cumsum()
    rows = []
    for d, r in daily.iterrows():
        cum_roi = (r["cum_refund"] / r["cum_bet"] * 100) if r["cum_bet"] else 0.0
        rows.append({
            "date": d.isoformat() if hasattr(d, "isoformat") else str(d),
            "n": int(r["n"]), "hits": int(r["hits"]),
            "cum_n": int(r["cum_n"]),
            "cum_roi": cum_roi,
            "cum_profit": int(r["cum_refund"] - r["cum_bet"]),
        })
    return rows[-tail:]


def _sanren_verdict(agg: dict) -> tuple[str, str]:
    """判定基準 (100R / ROI>120%) に対する現状を (mark, text) で返す。閾値は固定。"""
    n, roi = agg["n"], agg["roi"]
    if n < SANREN_DECISION_MIN_N:
        return ("ℹ️", f"n={n} < {SANREN_DECISION_MIN_N}R → 判定サンプル未達 "
                       f"(あと {SANREN_DECISION_MIN_N - n}R)")
    if roi >= SANREN_DECISION_ROI:
        return ("🟢", f"n={n} 到達 / ROI {roi:.1f}% ≥ {SANREN_DECISION_ROI:.0f}% "
                       f"→ 基準クリア")
    return ("🔴", f"n={n} 到達 / ROI {roi:.1f}% < {SANREN_DECISION_ROI:.0f}% "
                   f"→ 基準未達")


def render_sanren_text(alltime: dict, recent: dict, days: int,
                       cum: list[dict]) -> str:
    """三連系実弾の判定用セクション (text)。全期間を主指標、直近を補助表示。"""
    mark, verdict = _sanren_verdict(alltime)
    lines = ["", "【🎯 三連系実弾 判定指標 (3連単+3連複 / 全場)】",
             "-" * 60,
             f"  判定基準 (2026-07-26): 100R / ROI>120% を維持できるか"]
    lines.append(
        f"  全期間: {alltime['n']:>3d} R / 的中 {alltime['hits']:>3d} "
        f"({alltime['hit_rate']:>4.1f}%) / 投資 ¥{alltime['bet']:>7,} / "
        f"払戻 ¥{alltime['refund']:>7,} / 損益 {alltime['profit']:>+8,} 円 / "
        f"ROI {alltime['roi']:>5.1f}%"
    )
    lines.append(
        f"  直近{days}日: {recent['n']:>3d} R / 的中 {recent['hits']:>3d} "
        f"({recent['hit_rate']:>4.1f}%) / 投資 ¥{recent['bet']:>7,} / "
        f"払戻 ¥{recent['refund']:>7,} / 損益 {recent['profit']:>+8,} 円 / "
        f"ROI {recent['roi']:>5.1f}%"
    )
    lines.append(f"  判定: {mark} {verdict}")
    if cum:
        lines.append("  -- 累積推移 (直近日) --")
        lines.append(f"    {'date':12s} {'日R':>3s} {'累計R':>5s} {'累計ROI':>7s} {'累計損益':>9s}")
        for r in cum:
            lines.append(
                f"    {r['date']:12s} {r['n']:>3d} {r['cum_n']:>5d} "
                f"{r['cum_roi']:>6.1f}% {r['cum_profit']:>+8,}円"
            )
    return "\n".join(lines)


def render_sanren_html(alltime: dict, recent: dict, days: int,
                       cum: list[dict]) -> str:
    """三連系実弾の判定用セクション (html, inline style)。"""
    TBL = ('border="1" cellpadding="6" cellspacing="0" '
           'style="border-collapse:collapse; border-color:#bbb; '
           'font-family:Arial,sans-serif; font-size:13px;"')
    TH = ('style="background:#e8e8e8; text-align:left; padding:6px 10px; '
          'font-weight:bold; border:1px solid #bbb;"')
    TH_R = ('style="background:#e8e8e8; text-align:right; padding:6px 10px; '
            'font-weight:bold; border:1px solid #bbb;"')
    TD_L = 'style="text-align:left; padding:6px 10px; border:1px solid #ddd;"'
    TD_R = 'style="text-align:right; padding:6px 10px; border:1px solid #ddd;"'
    ROW_ALT = 'style="background:#fafafa;"'

    def _profit(p: int) -> str:
        c = "#2e7d32" if p > 0 else ("#c62828" if p < 0 else "#444")
        return (f'<td style="text-align:right; padding:6px 10px; '
                f'border:1px solid #ddd; color:{c}; font-weight:bold;">{p:+,} 円</td>')

    def _roi(roi: float) -> str:
        c = "#2e7d32" if roi >= SANREN_DECISION_ROI else "#c62828"
        return (f'<td style="text-align:right; padding:6px 10px; '
                f'border:1px solid #ddd; color:{c}; font-weight:bold;">{roi:.1f}%</td>')

    mark, verdict = _sanren_verdict(alltime)
    v_color = {"🟢": "#2e7d32", "🔴": "#c62828", "ℹ️": "#e65100"}.get(mark, "#444")
    parts = [
        '<h3 style="color:#444; margin:18px 0 8px 0;">'
        '🎯 三連系実弾 判定指標 '
        '<span style="font-weight:normal; color:#666; font-size:13px;">'
        '(3連単+3連複 / 全場)</span></h3>',
        '<p style="margin:0 0 6px 0; color:#555; font-size:12px;">'
        f'判定基準 (2026-07-26): <b>100R / ROI&gt;120%</b> を維持できるか</p>',
        f'<table {TBL}>',
        f'<tr><th {TH}>区分</th><th {TH_R}>R 数</th><th {TH_R}>的中</th>'
        f'<th {TH_R}>投資</th><th {TH_R}>払戻</th><th {TH_R}>損益</th>'
        f'<th {TH_R}>ROI</th></tr>',
        f'<tr><td {TD_L}><b>全期間</b></td>'
        f'<td {TD_R}>{alltime["n"]:,}</td>'
        f'<td {TD_R}>{alltime["hits"]:,} ({alltime["hit_rate"]:.1f}%)</td>'
        f'<td {TD_R}>¥{alltime["bet"]:,}</td>'
        f'<td {TD_R}>¥{alltime["refund"]:,}</td>'
        f'{_profit(alltime["profit"])}{_roi(alltime["roi"])}</tr>',
        f'<tr {ROW_ALT}><td {TD_L}><b>直近{days}日</b></td>'
        f'<td {TD_R}>{recent["n"]:,}</td>'
        f'<td {TD_R}>{recent["hits"]:,} ({recent["hit_rate"]:.1f}%)</td>'
        f'<td {TD_R}>¥{recent["bet"]:,}</td>'
        f'<td {TD_R}>¥{recent["refund"]:,}</td>'
        f'{_profit(recent["profit"])}{_roi(recent["roi"])}</tr>',
        '</table>',
        f'<p style="margin:6px 0; font-weight:bold; color:{v_color};">'
        f'判定: {mark} {verdict}</p>',
    ]
    if cum:
        parts.append(
            '<h4 style="color:#555; margin:12px 0 6px 0;">累積推移 (直近日)</h4>'
        )
        parts.append(f'<table {TBL}>')
        parts.append(
            f'<tr><th {TH}>date</th><th {TH_R}>日R</th><th {TH_R}>累計R</th>'
            f'<th {TH_R}>累計ROI</th><th {TH_R}>累計損益</th></tr>'
        )
        for i, r in enumerate(cum):
            alt = ROW_ALT if i % 2 == 1 else ""
            parts.append(
                f'<tr {alt}><td {TD_L}>{r["date"]}</td>'
                f'<td {TD_R}>{r["n"]}</td><td {TD_R}>{r["cum_n"]}</td>'
                f'{_roi(r["cum_roi"])}{_profit(r["cum_profit"])}</tr>'
            )
        parts.append('</table>')
    return "\n".join(parts)


def build_sanren_section(days: int) -> tuple[str, str] | None:
    """weekly_status から呼ぶ用。三連系実弾の判定セクション (text, html)。

    detail が無い / 三連系実績ゼロなら None (セクション非表示)。
    """
    detail = load_detail()
    if detail is None:
        return None
    alltime = _sanren_agg(detail)
    if alltime["n"] == 0:
        return None
    recent = _sanren_agg(filter_recent_n_days(detail, days))
    cum = _sanren_cumulative(detail)
    text = render_sanren_text(alltime, recent, days, cum)
    html = render_sanren_html(alltime, recent, days, cum)
    return text, html


def render_text(days: int, recent: dict, venues: list[dict],
                bet_types: list[dict], alltime: dict,
                period_label: str, alltime_label: str) -> str:
    lines: list[str] = []
    lines.append(f"【💰 実購入損益】 {period_label}")
    lines.append("-" * 60)

    # ① 直近 N 日 全体
    lines.append(
        f"  直近{days}日: {recent['n']:>3d} R / "
        f"投資 ¥{recent['bet']:>7,} / 払戻 ¥{recent['refund']:>7,} / "
        f"損益 {recent['profit']:>+8,} 円 / ROI {recent['roi']:>5.1f}%"
    )
    lines.append("")

    # ② 場別
    if venues:
        lines.append(f"  -- 場別 (直近{days}日) --")
        lines.append(f"  {'場':6s} {'R':>3s} {'投資':>9s} {'払戻':>9s} {'損益':>10s} {'ROI':>6s}")
        for v in venues:
            lines.append(
                f"  {v['place_name']:6s} {v['n']:>3d} ¥{v['bet']:>7,} ¥{v['refund']:>7,} "
                f"{v['profit']:>+8,}円 {v['roi']:>5.1f}%"
            )
        lines.append("")

    # ③ 券種別
    if bet_types:
        lines.append(f"  -- 券種別 (直近{days}日, pack 単位) --")
        lines.append(f"  {'券種':10s} {'件':>3s} {'投資':>9s} {'払戻':>9s} {'損益':>10s} {'ROI':>6s}")
        for b in bet_types:
            lines.append(
                f"  {b['bet_type_label']:10s} {b['n']:>3d} ¥{b['bet']:>7,} ¥{b['refund']:>7,} "
                f"{b['profit']:>+8,}円 {b['roi']:>5.1f}%"
            )
        lines.append("")

    # ④ 全期間
    lines.append(
        f"  全期間 ({alltime_label}): {alltime['n']:>3d} R / "
        f"投資 ¥{alltime['bet']:>7,} / 払戻 ¥{alltime['refund']:>7,} / "
        f"損益 {alltime['profit']:>+8,} 円 / ROI {alltime['roi']:>5.1f}%"
    )
    return "\n".join(lines)


def render_html(days: int, recent: dict, venues: list[dict],
                bet_types: list[dict], alltime: dict,
                period_label: str, alltime_label: str) -> str:
    TBL = ('border="1" cellpadding="6" cellspacing="0" '
           'style="border-collapse:collapse; border-color:#bbb; '
           'font-family:Arial,sans-serif; font-size:13px;"')
    TH = ('style="background:#e8e8e8; text-align:left; padding:6px 10px; '
          'font-weight:bold; border:1px solid #bbb;"')
    TH_R = ('style="background:#e8e8e8; text-align:right; padding:6px 10px; '
            'font-weight:bold; border:1px solid #bbb;"')
    TD_L = 'style="text-align:left; padding:6px 10px; border:1px solid #ddd;"'
    TD_R = 'style="text-align:right; padding:6px 10px; border:1px solid #ddd;"'
    ROW_ALT = 'style="background:#fafafa;"'

    def _profit_cell(p: int) -> str:
        color = "#2e7d32" if p > 0 else ("#c62828" if p < 0 else "#444")
        return (f'<td style="text-align:right; padding:6px 10px; '
                f'border:1px solid #ddd; color:{color}; font-weight:bold;">'
                f'{p:+,} 円</td>')

    def _roi_cell(roi: float) -> str:
        color = "#2e7d32" if roi >= 100 else "#c62828"
        return (f'<td style="text-align:right; padding:6px 10px; '
                f'border:1px solid #ddd; color:{color}; font-weight:bold;">'
                f'{roi:.1f}%</td>')

    parts: list[str] = []
    parts.append(
        '<h3 style="color:#444; margin:18px 0 8px 0;">'
        f'💰 実購入損益 <span style="font-weight:normal; color:#666; font-size:13px;">'
        f'({period_label})</span></h3>'
    )

    # ① 直近 N 日 + ④ 全期間: 1 つのまとめ表
    parts.append(f'<table {TBL}>')
    parts.append(
        f'<tr><th {TH}>区分</th>'
        f'<th {TH_R}>R 数</th><th {TH_R}>投資</th>'
        f'<th {TH_R}>払戻</th><th {TH_R}>損益</th><th {TH_R}>ROI</th></tr>'
    )
    parts.append(
        f'<tr><td {TD_L}><b>直近{days}日</b></td>'
        f'<td {TD_R}>{recent["n"]:,}</td>'
        f'<td {TD_R}>¥{recent["bet"]:,}</td>'
        f'<td {TD_R}>¥{recent["refund"]:,}</td>'
        f'{_profit_cell(recent["profit"])}'
        f'{_roi_cell(recent["roi"])}'
        f'</tr>'
    )
    parts.append(
        f'<tr {ROW_ALT}><td {TD_L}><b>全期間</b><br>'
        f'<span style="font-weight:normal; color:#888; font-size:11px;">{alltime_label}</span></td>'
        f'<td {TD_R}>{alltime["n"]:,}</td>'
        f'<td {TD_R}>¥{alltime["bet"]:,}</td>'
        f'<td {TD_R}>¥{alltime["refund"]:,}</td>'
        f'{_profit_cell(alltime["profit"])}'
        f'{_roi_cell(alltime["roi"])}'
        f'</tr>'
    )
    parts.append('</table>')

    # ② 場別
    if venues:
        parts.append(
            f'<h4 style="color:#555; margin:14px 0 6px 0;">場別 (直近{days}日)</h4>'
        )
        parts.append(f'<table {TBL}>')
        parts.append(
            f'<tr><th {TH}>場</th>'
            f'<th {TH_R}>R 数</th><th {TH_R}>投資</th>'
            f'<th {TH_R}>払戻</th><th {TH_R}>損益</th><th {TH_R}>ROI</th></tr>'
        )
        for i, v in enumerate(venues):
            alt = ROW_ALT if i % 2 == 1 else ""
            parts.append(
                f'<tr {alt}><td {TD_L}>{v["place_name"]}</td>'
                f'<td {TD_R}>{v["n"]:,}</td>'
                f'<td {TD_R}>¥{v["bet"]:,}</td>'
                f'<td {TD_R}>¥{v["refund"]:,}</td>'
                f'{_profit_cell(v["profit"])}'
                f'{_roi_cell(v["roi"])}'
                f'</tr>'
            )
        parts.append('</table>')

    # ③ 券種別
    if bet_types:
        parts.append(
            f'<h4 style="color:#555; margin:14px 0 6px 0;">'
            f'券種別 (直近{days}日, pack 単位)</h4>'
        )
        parts.append(f'<table {TBL}>')
        parts.append(
            f'<tr><th {TH}>券種</th>'
            f'<th {TH_R}>件</th><th {TH_R}>投資</th>'
            f'<th {TH_R}>払戻</th><th {TH_R}>損益</th><th {TH_R}>ROI</th></tr>'
        )
        for i, b in enumerate(bet_types):
            alt = ROW_ALT if i % 2 == 1 else ""
            parts.append(
                f'<tr {alt}><td {TD_L}>{b["bet_type_label"]}</td>'
                f'<td {TD_R}>{b["n"]:,}</td>'
                f'<td {TD_R}>¥{b["bet"]:,}</td>'
                f'<td {TD_R}>¥{b["refund"]:,}</td>'
                f'{_profit_cell(b["profit"])}'
                f'{_roi_cell(b["roi"])}'
                f'</tr>'
            )
        parts.append('</table>')

    return "\n".join(parts)


def build_summary(days: int) -> tuple[str, str] | None:
    """weekly_status から呼ぶ用。データ無ければ None。"""
    summary = load_summary()
    if summary is None:
        return None
    detail = load_detail()

    recent = filter_recent_n_days(summary, days)
    if recent.empty:
        # 直近 days 日にデータが無い場合も全期間は出したい
        recent_agg = {"n": 0, "bet": 0, "refund": 0, "profit": 0, "roi": 0.0}
        venues_agg: list[dict] = []
        bet_types_agg: list[dict] = []
    else:
        recent_agg = _agg(recent)
        venues_agg = by_venue(recent)
        if detail is not None:
            detail_recent = filter_recent_n_days(detail, days)
            bet_types_agg = by_bet_type(detail_recent)
        else:
            bet_types_agg = []

    alltime_agg = _agg(summary)
    alltime_label = (
        f"{summary['date'].min().isoformat()} 〜 {summary['date'].max().isoformat()}"
    )
    today = dt.date.today()
    period_label = (
        f"直近{days}日: "
        f"{(today - dt.timedelta(days=days)).isoformat()} 〜 "
        f"{(today - dt.timedelta(days=1)).isoformat()}"
    )

    text = render_text(days, recent_agg, venues_agg, bet_types_agg, alltime_agg,
                       period_label, alltime_label)
    html = render_html(days, recent_agg, venues_agg, bet_types_agg, alltime_agg,
                       period_label, alltime_label)
    return text, html


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args()

    result = build_summary(args.days)
    if result is None:
        print(f"(データ無し: {SUMMARY_CSV})")
        return
    text, _ = result
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(text)


if __name__ == "__main__":
    main()
