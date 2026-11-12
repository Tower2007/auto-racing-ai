"""自動発注の追記型台帳と、注文履歴 (bet_history) との照合 (2026-11-12 監査 指摘11・結果不明の照合)。

auto_buy_state.json は暦日 1 日分を上書きで持つため、日付が変わった後の最初の発注
(00:01 のミッドナイト R など) で前日の記録が消える。夜次レポートや連続失敗停止の解除判定が
前日分を確実に読めるよう、発注記録を 1 行 1 JSON の台帳 (auto_buy_ledger.jsonl) にも残す。

ここは純粋な読み書きと照合だけを持つ (発注判定には使わない)。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path

LEDGER_NAME = "auto_buy_ledger.jsonl"
ARCHIVE_DIRNAME = "auto_buy_state_archive"

CONFIRMED_BOUGHT = "成立"
CONFIRMED_NOT_BOUGHT = "不成立"
UNCONFIRMED = "未確認"


def append_records(path: Path, records: list[dict]) -> None:
    """台帳に追記 (1 行 1 JSON)。"""
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_records(path: Path) -> list[dict]:
    """台帳を読む。壊れた行は読み飛ばす。無ければ空。"""
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except FileNotFoundError:
        pass
    return out


def record_key(r: dict) -> tuple:
    """重複除去のキー (同じ記録が state / 退避 / 台帳に重なって現れるため)。"""
    return (str(r.get("race", "")), str(r.get("timestamp", "")), str(r.get("verdict", "")))


# 開催日 D の R (ミッドナイトは翌 01:00 頃まで) の成立有無は、D+1 のこの時刻以降に
# 成功した注文履歴取得 (定時 02:30) で確定とみなす。
CONFIRM_AFTER = dt.time(2, 0)


def last_fetch_success(log_path: Path) -> dt.datetime | None:
    """fetch_order_history.log の最後の `=== <ts> END exit=0 ===` の時刻。"""
    pat = re.compile(r"^=== (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S* END exit=0 ===")
    last = None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = pat.match(line)
                if m:
                    last = m.group(1)
    except FileNotFoundError:
        return None
    try:
        return dt.datetime.fromisoformat(last) if last else None
    except ValueError:
        return None


def bought_keys(bet_history_csv: Path) -> set[tuple[str, int, int]]:
    """bet_history.csv の (date, place_code, race_no) 集合 (= 成立した注文のある R)。"""
    out: set[tuple[str, int, int]] = set()
    try:
        with open(bet_history_csv, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    out.add((row["date"], int(row["place_code"]), int(row["race_no"])))
                except (KeyError, ValueError, TypeError):
                    continue
    except FileNotFoundError:
        pass
    return out


def confirm_race(race_date: str, place_code: int, race_no: int,
                 bought: set[tuple[str, int, int]],
                 fetch_ok_at: dt.datetime | None) -> str:
    """1 R の成立有無。注文履歴の取得が開催日の翌日 02:00 以降に成功していれば確定とみなす。

    bet_history に行があれば「成立」。無く、かつ取得がその後に成功していれば
    「不成立」。取得がまだなら「未確認」(判断しない = 安全側)。
    """
    if (race_date, int(place_code), int(race_no)) in bought:
        return CONFIRMED_BOUGHT
    try:
        rd = dt.date.fromisoformat(str(race_date))
    except ValueError:
        return UNCONFIRMED
    cutoff = dt.datetime.combine(rd + dt.timedelta(days=1), CONFIRM_AFTER)
    if fetch_ok_at is not None and fetch_ok_at >= cutoff:
        return CONFIRMED_NOT_BOUGHT
    return UNCONFIRMED
