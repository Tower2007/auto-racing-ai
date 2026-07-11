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
  - daily_predict.rt3_buy_active() が backstop_blocks_purchase() を参照 (購入ゲート、
    履歴読取失敗時は fail-closed = 三連系購入スキップ。2026-07-12 Codex艦隊監査 P2-2)
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
# 集計に必須のヘッダ (欠落 = 台帳フォーマット異常 → fail-closed)
REQUIRED_DETAIL_COLUMNS = ("bet_type_code", "vote_amount", "hit_amount")


def _strict_amount(v) -> float | None:
    """金額セルの厳格パース。数値化できない/負値なら None (不正)。

    2026-07-12 Codex再検証 ①: 旧実装は変換失敗を pass で握りつぶし
    「CSVは開けるが金額セルが壊れている」場合に投資額0円として過小集計
    → 購入を許可し得た。空セル・欠損 (短い行の None) も不正として扱う。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        x = float(s)
    except (TypeError, ValueError):
        return None
    if x < 0 or x != x:  # 負値・NaN は台帳として不正
        return None
    return x


def backstop_active() -> bool:
    """バックストップ停止フラグが立っているか (sticky: 存在 = 停止)。"""
    return BACKSTOP_FLAG.exists()


def backstop_blocks_purchase() -> bool:
    """三連系購入ゲート: 購入を止めるべきなら True (2026-07-12 Codex艦隊監査 P2-2)。

    - sticky フラグ存在                      → True (発動済み)
    - bet_history_detail が読めない/集計不能 → True (fail-closed) + 警告。
      履歴を確認できない状態では「バックストップ発動済みかどうか」を判定できない
      ため、旧設計 (評価失敗では止めない) を改め購入をスキップする。
      sticky フラグの書き出し・メール通知はしない (真の発動と区別する)。
    - 集計できて閾値割れ                     → True (enforce 前でも購入は止める)
    - 集計できて閾値内                       → False
    """
    if BACKSTOP_FLAG.exists():
        return True
    try:
        ev = evaluate_backstop()
    except Exception as e:  # noqa: BLE001 (evaluate 内で握るが二重防御)
        logger.warning("[backstop] 評価失敗 (%s) — fail-closed で三連系購入をスキップ", e)
        return True
    if ev.get("profit") is None:
        logger.warning("[backstop] bet_history_detail 読取不能 (%s) — "
                       "fail-closed で三連系購入をスキップ", ev.get("error"))
        return True
    return bool(ev.get("breached"))


def evaluate_backstop() -> dict:
    """全場・全期間の三連系累積損益を集計 (純関数、副作用なし)。

    戻り値:
      n_rows / invest / payout / profit(int|None) / breached(bool) /
      threshold / flag_exists / error(str|None)
    detail が読めない場合は profit=None, breached=False (「発動」とは扱わず
    sticky フラグも書かない)。ただし購入ゲート backstop_blocks_purchase() は
    profit=None を fail-closed (三連系購入スキップ) として扱う (P2-2)。

    2026-07-12 Codex再検証 ①: 必須ヘッダ欠落、または三連系行の金額セルが
    1つでも厳格パース不能 (空・非数値・負値) なら profit=None のまま error を
    立てる = 完全 fail-closed。部分集計で「読めたセルだけの損益」を返すことは
    しない (過小集計 → 閾値未達と誤判定 → 購入許可、を根絶)。
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
        bad_cells: list[str] = []
        with open(DETAIL_CSV, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            missing = [c for c in REQUIRED_DETAIL_COLUMNS if c not in fields]
            if missing:
                out["error"] = (f"必須ヘッダ欠落 {missing} — "
                                f"fail-closed (台帳フォーマット異常)")
                logger.warning("[backstop] %s", out["error"])
                return out
            for lineno, row in enumerate(reader, start=2):
                if row.get("bet_type_code") not in SANREN_BET_TYPES:
                    continue
                n += 1
                v = _strict_amount(row.get("vote_amount"))
                h = _strict_amount(row.get("hit_amount"))
                if v is None or h is None:
                    bad_cells.append(
                        f"L{lineno}(vote={row.get('vote_amount')!r},"
                        f"hit={row.get('hit_amount')!r})")
                    continue
                invest += v
                payout += h
        out["n_rows"] = n
        if bad_cells:
            # 1セルでも不正なら profit=None のまま (部分集計を返さない)
            out["error"] = (f"三連系行の金額セル不正 {len(bad_cells)} 件 "
                            f"(例: {', '.join(bad_cells[:3])}) — fail-closed")
            logger.warning("[backstop] %s", out["error"])
            return out
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
