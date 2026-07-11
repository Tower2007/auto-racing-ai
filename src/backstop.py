"""三連系 全場・全期間 絶対損失バックストップ (2026-07-11 監査 P2 + Codex 承認)。

背景 (ラチェット構造の穴):
  kill-switch (weekly_status.check_3point_health, 選択肢 B) は「現役ポリシー」
  のみを監視するため、負けた場を廃止するたびに現役損失の基盤がリセットされる。
  最悪、場を順に廃止しながら合計で際限なく損失を積める構造だった。

対策 (常設の安全弁):
  ポリシー (場の廃止/追加) に関係なく、**全場・全期間** の三連系 (rt3+rf3)
  累積損益を bet_history_detail.csv から集計し、
  THREE_POINT_BACKSTOP_LOSS_YEN (-¥10,000) 以下になったら三連系購入を停止する。

sticky 仕様 (Codex 条件):
  一度発動したら data/rt3_backstop_stop.flag に恒久フラグを書き、
  **人間が明示的にファイルを削除するまで** 三連系購入を止め続ける。
  ポリシー変更・場の廃止/追加・detail の再取得ではリセットされない
  (フラグ存在 = 停止、が唯一の真実)。

損益計算式は kill-switch ② と同じ hit_amount - vote_amount
(返還 henkan は払戻に含めない = 保守側に倒す)。

参照側:
  - daily_predict.rt3_buy_active() が backstop_active() を参照 (購入ゲート)
  - daily_predict.main() 冒頭で enforce_backstop() を評価 (発動 + メール通知)
  - weekly_status.check_3point_health() が条件⑤として表示 + 発動
"""

from __future__ import annotations

import csv
import datetime as _dt
import logging
from pathlib import Path

from src.strategy_config import THREE_POINT_BACKSTOP_LOSS_YEN

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DETAIL_CSV = DATA / "bet_history_detail.csv"
# rt3_stop.flag (kill-switch, 人間削除で再開) とは独立した sticky フラグ。
BACKSTOP_FLAG = DATA / "rt3_backstop_stop.flag"

SANREN_BET_TYPES = ("rt3", "rf3")


def backstop_active() -> bool:
    """バックストップ停止フラグが立っているか (sticky: 存在 = 停止)。"""
    return BACKSTOP_FLAG.exists()


def evaluate_backstop() -> dict:
    """全場・全期間の三連系累積損益を集計 (純関数、副作用なし)。

    戻り値:
      n_rows / invest / payout / profit(int|None) / breached(bool) /
      threshold / flag_exists / error(str|None)
    detail が読めない場合は profit=None, breached=False (評価失敗で購入は
    止めない — 現役スコープの kill-switch ②(-5,000) が別途効いている)。
    """
    out: dict = {
        "n_rows": 0, "invest": 0, "payout": 0, "profit": None,
        "breached": False, "threshold": THREE_POINT_BACKSTOP_LOSS_YEN,
        "flag_exists": BACKSTOP_FLAG.exists(), "error": None,
    }
    if not (DETAIL_CSV.exists() and DETAIL_CSV.stat().st_size > 0):
        out["error"] = "bet_history_detail.csv なし"
        return out
    try:
        invest = 0.0
        payout = 0.0
        n = 0
        with open(DETAIL_CSV, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("bet_type_code") not in SANREN_BET_TYPES:
                    continue
                n += 1
                try:
                    invest += float(row.get("vote_amount") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    payout += float(row.get("hit_amount") or 0)
                except (TypeError, ValueError):
                    pass
        out["n_rows"] = n
        out["invest"] = int(invest)
        out["payout"] = int(payout)
        out["profit"] = int(payout - invest)
        out["breached"] = out["profit"] <= THREE_POINT_BACKSTOP_LOSS_YEN
    except Exception as e:  # noqa: BLE001
        out["error"] = f"detail 集計失敗: {e}"
    return out


def _write_flag(profit: int) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    BACKSTOP_FLAG.write_text(
        f"stopped_at={_dt.datetime.now().isoformat(timespec='seconds')}\n"
        f"profit_all_venues_yen={profit}\n"
        f"threshold_yen={THREE_POINT_BACKSTOP_LOSS_YEN}\n"
        f"# 三連系 全場・全期間 絶対損失バックストップ (sticky)。\n"
        f"# このファイルがある間、三連系まとめ買い (rt3+rf3) を停止します。\n"
        f"# ポリシー変更 (場の廃止/追加) では解除されません。\n"
        f"# 再開するには、原因を検討した上で人間がこのファイルを削除してください。\n",
        encoding="utf-8")


def _send_notify(subject: str, body: str) -> None:
    """既存の通知経路 (gmail_notify) で通知。テストで monkeypatch する分離点。"""
    from gmail_notify import send_email
    send_email(subject=subject, body=body)


def enforce_backstop(notify: bool = True) -> dict:
    """バックストップを評価し、閾値割れなら sticky フラグ書き出し + メール通知。

    戻り値は evaluate_backstop() の dict に
      active (bool) / newly_triggered (bool) を追加したもの。
    sticky: フラグが既にあれば損益に関係なく active=True (再送・再書き込みなし)。
    評価やフラグ書き込みの失敗は例外を外に出さない (daily_predict を止めない)。
    """
    try:
        out = evaluate_backstop()
    except Exception as e:  # noqa: BLE001 (evaluate 内で握るが二重防御)
        out = {"profit": None, "breached": False, "error": str(e),
               "threshold": THREE_POINT_BACKSTOP_LOSS_YEN,
               "flag_exists": backstop_active()}
    out["newly_triggered"] = False

    if out.get("flag_exists"):
        out["active"] = True
        return out

    if not out.get("breached"):
        out["active"] = False
        return out

    # 新規発動
    out["active"] = True
    out["newly_triggered"] = True
    profit = int(out.get("profit") or 0)
    try:
        _write_flag(profit)
        out["flag_exists"] = True
    except Exception as e:  # noqa: BLE001
        logger.error("[backstop] フラグ書き込み失敗: %s", e)
        out["error"] = f"flag write failed: {e}"
    if notify:
        try:
            _send_notify(
                subject=(f"[autorace] 三連系バックストップ発動 "
                         f"(全場累積 {profit:+,}円 <= "
                         f"{THREE_POINT_BACKSTOP_LOSS_YEN:,}円)"),
                body=(
                    "三連系 全場・全期間 絶対損失バックストップが発動しました。\n\n"
                    f"  全場・全期間 三連系累積損益: {profit:+,} 円\n"
                    f"  閾値: {THREE_POINT_BACKSTOP_LOSS_YEN:,} 円\n"
                    f"  投資 {out.get('invest', 0):,} 円 / 払戻 {out.get('payout', 0):,} 円 "
                    f"/ 対象 {out.get('n_rows', 0)} 行\n\n"
                    "data/rt3_backstop_stop.flag を書き出しました。\n"
                    "このフラグがある間、三連系まとめ買い (rt3+rf3) は停止します\n"
                    "(複勝・参考メール表示は継続)。\n\n"
                    "sticky 仕様: 場の廃止/追加などポリシー変更では解除されません。\n"
                    "再開するには原因を検討した上で、人間がフラグファイルを削除してください。\n"
                ))
        except Exception as e:  # noqa: BLE001
            logger.error("[backstop] 発動メール送信失敗: %s", e)
    return out
