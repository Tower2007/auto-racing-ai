"""通知済み買い候補の hit / ROI を実績データで照合する週次監査

`data/daily_predict_picks.csv` に蓄積された候補を、
`data/payouts.csv`(fns / 複勝)で実払戻と突き合わせて hit/ROI を集計する。

Standalone 実行:
  python scripts/picks_audit.py             # 直近 7 日
  python scripts/picks_audit.py --days 30   # 直近 30 日
  python scripts/picks_audit.py --all       # 全期間

weekly_status.py から import して使う関数も提供。
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
BET = 100


def load_picks() -> pd.DataFrame:
    p = DATA / "daily_predict_picks.csv"
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    df = pd.read_csv(p)
    if df.empty:
        return df
    df["race_date"] = pd.to_datetime(df["race_date"])
    df["sent_at"] = pd.to_datetime(df["sent_at"])
    return df


def attach_results(picks: pd.DataFrame) -> pd.DataFrame:
    """実払戻と紐付け、has_result(結果データあり?) と hit/payout を付与。"""
    if picks.empty:
        return picks

    res_p = DATA / "race_results.csv"
    pay_p = DATA / "payouts.csv"
    if not res_p.exists() or not pay_p.exists():
        picks = picks.copy()
        picks["has_result"] = False
        picks["payout"] = 0.0
        picks["hit"] = 0
        return picks

    # 結果が ingest 済みの (date, place) セット
    res_dates = pd.read_csv(res_p, usecols=["race_date", "place_code"]).drop_duplicates()
    res_dates["race_date"] = pd.to_datetime(res_dates["race_date"])
    res_set = set(zip(res_dates["race_date"].dt.date, res_dates["place_code"]))

    # 複勝払戻
    pay = pd.read_csv(pay_p, low_memory=False)
    pay["race_date"] = pd.to_datetime(pay["race_date"])
    fns = pay[pay["bet_type"] == "fns"][RACE_KEY + ["car_no_1", "refund"]]
    fns = fns.groupby(RACE_KEY + ["car_no_1"], as_index=False)["refund"].sum()

    out = picks.merge(
        fns.rename(columns={"car_no_1": "car_no", "refund": "payout"}),
        on=RACE_KEY + ["car_no"], how="left",
    )
    out["payout"] = out["payout"].fillna(0)
    out["has_result"] = [
        (d.date(), pc) in res_set for d, pc in zip(out["race_date"], out["place_code"])
    ]
    out["hit"] = ((out["payout"] > 0) & out["has_result"]).astype(int)
    return out


def filter_period(audit: pd.DataFrame, days: int | None) -> pd.DataFrame:
    if days is None or audit.empty:
        return audit
    cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days)
    return audit[audit["sent_at"] >= cutoff]


def summarize(audit: pd.DataFrame) -> dict:
    if audit.empty:
        return {
            "n_total": 0, "n_settled": 0, "n_pending": 0, "n_hits": 0,
            "hit_rate": 0, "cost": 0, "payout": 0, "profit": 0, "roi": 0,
        }
    settled = audit[audit["has_result"]]
    pending = audit[~audit["has_result"]]
    cost = len(settled) * BET
    payout = float(settled["payout"].sum())
    return {
        "n_total": int(len(audit)),
        "n_settled": int(len(settled)),
        "n_pending": int(len(pending)),
        "n_hits": int(settled["hit"].sum()),
        "hit_rate": float(settled["hit"].mean()) if len(settled) else 0,
        "cost": int(cost),
        "payout": payout,
        "profit": float(payout - cost),
        "roi": float(payout / cost) if cost else 0,
    }


def by_batch(audit: pd.DataFrame) -> pd.DataFrame:
    if audit.empty:
        return pd.DataFrame()
    settled = audit[audit["has_result"]].copy()
    if settled.empty:
        return pd.DataFrame()
    g = settled.groupby("batch").agg(
        n=("hit", "size"), n_hits=("hit", "sum"), payout=("payout", "sum"),
    ).reset_index()
    g["cost"] = g["n"] * BET
    g["profit"] = g["payout"] - g["cost"]
    g["roi"] = g["payout"] / g["cost"]
    g["hit_rate"] = g["n_hits"] / g["n"]
    return g


def by_venue(audit: pd.DataFrame) -> pd.DataFrame:
    if audit.empty:
        return pd.DataFrame()
    settled = audit[audit["has_result"]].copy()
    if settled.empty:
        return pd.DataFrame()
    g = settled.groupby("venue").agg(
        n=("hit", "size"), n_hits=("hit", "sum"), payout=("payout", "sum"),
    ).reset_index()
    g["cost"] = g["n"] * BET
    g["profit"] = g["payout"] - g["cost"]
    g["roi"] = g["payout"] / g["cost"]
    g["hit_rate"] = g["n_hits"] / g["n"]
    return g


def render_text(audit: pd.DataFrame, days: int | None) -> str:
    s = summarize(audit)
    period = f"直近 {days} 日" if days else "全期間"
    lines = [
        f"📋 通知候補 監査({period})",
        "=" * 50,
    ]
    if s["n_total"] == 0:
        lines.append("通知された候補はありません。")
        lines.append("(Phase A 運用開始直後は通常です)")
        return "\n".join(lines)

    lines += [
        f"通知候補 計: {s['n_total']} 件",
        f"  決着済: {s['n_settled']} / 結果未取得: {s['n_pending']}",
        "",
        f"【決着分の P&L】",
        f"  投資: ¥{s['cost']:,}",
        f"  回収: ¥{int(s['payout']):,}",
        f"  損益: {'+' if s['profit']>=0 else ''}¥{int(s['profit']):,}",
        f"  ROI: {s['roi']*100:.2f}%",
        f"  命中: {s['n_hits']}/{s['n_settled']} ({s['hit_rate']*100:.1f}%)",
    ]

    bv = by_venue(audit)
    if not bv.empty:
        lines += ["", "【場別】"]
        for _, r in bv.iterrows():
            lines.append(
                f"  {r['venue']:9s} n={int(r['n']):3d} hit={int(r['n_hits']):3d} "
                f"({r['hit_rate']*100:5.1f}%) ROI={r['roi']*100:6.2f}% "
                f"損益={'+' if r['profit']>=0 else ''}¥{int(r['profit']):,}"
            )
    bb = by_batch(audit)
    if not bb.empty:
        lines += ["", "【バッチ別】"]
        for _, r in bb.iterrows():
            lines.append(
                f"  {r['batch']:14s} n={int(r['n']):3d} hit={int(r['n_hits']):3d} "
                f"ROI={r['roi']*100:6.2f}% 損益={'+' if r['profit']>=0 else ''}¥{int(r['profit']):,}"
            )
    return "\n".join(lines)


def render_html(audit: pd.DataFrame, days: int | None) -> str:
    s = summarize(audit)
    period = f"直近 {days} 日" if days else "全期間"
    BORDER = '"border-collapse:collapse; border-color:#bbb; font-family:Arial,sans-serif; font-size:13px;"'
    TH = '"background:#e8e8e8; padding:6px 10px; border:1px solid #bbb; text-align:center;"'
    TD = '"padding:6px 10px; border:1px solid #ddd; text-align:right;"'
    TD_L = '"padding:6px 10px; border:1px solid #ddd; text-align:left;"'

    parts = [
        f'<h3 style="color:#444; margin:18px 0 8px 0;">📋 通知候補 監査({period})</h3>',
    ]
    if s["n_total"] == 0:
        parts.append(
            '<p style="color:#666;">通知された候補はありません(Phase A 運用開始直後は通常)。</p>'
        )
        return "\n".join(parts)

    profit_color = "#2e7d32" if s["profit"] >= 0 else "#c62828"
    parts.append(f'<table border="1" cellpadding="6" cellspacing="0" style={BORDER}>')
    parts.append(
        f'<tr><th style={TH}>項目</th><th style={TH}>値</th></tr>'
        f'<tr><td style={TD_L}>通知候補</td><td style={TD}>{s["n_total"]} 件</td></tr>'
        f'<tr><td style={TD_L}>決着済 / 未取得</td>'
        f'<td style={TD}>{s["n_settled"]} / {s["n_pending"]}</td></tr>'
        f'<tr style="background:#fafafa;"><td style={TD_L}><b>投資</b></td>'
        f'<td style={TD}>¥{s["cost"]:,}</td></tr>'
        f'<tr><td style={TD_L}><b>回収</b></td>'
        f'<td style={TD}>¥{int(s["payout"]):,}</td></tr>'
        f'<tr style="background:#fafafa;"><td style={TD_L}><b>損益</b></td>'
        f'<td style={TD}><b style="color:{profit_color}">'
        f'{"+" if s["profit"]>=0 else ""}¥{int(s["profit"]):,}</b></td></tr>'
        f'<tr><td style={TD_L}><b>ROI</b></td>'
        f'<td style={TD}><b>{s["roi"]*100:.2f}%</b></td></tr>'
        f'<tr style="background:#fafafa;"><td style={TD_L}>命中</td>'
        f'<td style={TD}>{s["n_hits"]}/{s["n_settled"]} ({s["hit_rate"]*100:.1f}%)</td></tr>'
    )
    parts.append("</table>")

    bv = by_venue(audit)
    if not bv.empty:
        parts.append('<h4 style="margin:14px 0 6px 0;">場別</h4>')
        parts.append(f'<table border="1" cellpadding="6" cellspacing="0" style={BORDER}>')
        parts.append(
            f'<tr><th style={TH}>場</th><th style={TH}>n</th>'
            f'<th style={TH}>命中率</th><th style={TH}>ROI</th><th style={TH}>損益</th></tr>'
        )
        for i, (_, r) in enumerate(bv.iterrows()):
            alt = ' style="background:#fafafa;"' if i % 2 == 1 else ""
            pc_clr = "#2e7d32" if r["profit"] >= 0 else "#c62828"
            parts.append(
                f'<tr{alt}>'
                f'<td style={TD_L}>{r["venue"]}</td>'
                f'<td style={TD}>{int(r["n"])}</td>'
                f'<td style={TD}>{r["hit_rate"]*100:.1f}%</td>'
                f'<td style={TD}>{r["roi"]*100:.2f}%</td>'
                f'<td style={TD}><span style="color:{pc_clr}">'
                f'{"+" if r["profit"]>=0 else ""}¥{int(r["profit"]):,}</span></td>'
                f'</tr>'
            )
        parts.append("</table>")
    return "\n".join(parts)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--all", action="store_true", help="全期間")
    args = p.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    days = None if args.all else args.days
    picks = load_picks()
    audit = attach_results(picks)
    audit = filter_period(audit, days)
    print(render_text(audit, days))


if __name__ == "__main__":
    main()
