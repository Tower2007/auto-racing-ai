"""
日次 投票履歴取得 (schtasks 用ラッパー)
-----------------------------------------------------------------------
fetch_order_history.py を `--since 2d --detail --cookie-source chrome` で呼び、
失敗時のみ Gmail 通知を送る。schtasks `AutoraceFetchOrderHistory` が毎日 02:30
に起動する想定。

ログ: data/fetch_order_history.log (追記)

使い方:
  python scripts/daily_fetch_order_history.py
  python scripts/daily_fetch_order_history.py --since 7d  # キャッチアップ
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "data" / "fetch_order_history.log"
FETCH_SCRIPT = ROOT / "scripts" / "fetch_order_history.py"


def append_log(text: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


def notify_failure(subject: str, body: str) -> None:
    """Gmail で失敗通知 (失敗しても黙って諦める)。"""
    try:
        sys.path.insert(0, str(ROOT))
        from gmail_notify import send_email
        send_email(subject=subject, body=body)
    except Exception as e:
        append_log(f"[warn] gmail_notify 失敗: {e}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", default="2d",
                   help="直近 N 日 (default: 2d、前日+前々日を冪等再取得)")
    p.add_argument("--cookie-source", default="firefox",
                   choices=["chrome", "firefox", "edge", "env"])
    p.add_argument("--no-detail", action="store_true",
                   help="券種別 pack 詳細を取らない (default: 取る)")
    args = p.parse_args()

    cmd = [
        sys.executable, str(FETCH_SCRIPT),
        "--since", args.since,
        "--cookie-source", args.cookie_source,
    ]
    if not args.no_detail:
        cmd.append("--detail")

    started = dt.datetime.now().isoformat(timespec="seconds")
    append_log(f"\n=== {started} START {' '.join(cmd)} ===")

    # 子プロセスに UTF-8 出力を強制 (Windows cp932 デフォルト回避)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        result = subprocess.run(
            cmd, cwd=str(ROOT), env=env,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=600,  # 10 min
        )
    except subprocess.TimeoutExpired:
        msg = f"timeout (>600s): {' '.join(cmd)}"
        append_log(f"[error] {msg}")
        notify_failure(
            "[autorace] 投票履歴取得 タイムアウト",
            f"{msg}\n\n10分以内に終わらなかった。手動で確認してください。",
        )
        return 1
    except Exception as e:
        msg = f"subprocess 起動失敗: {e}"
        append_log(f"[error] {msg}")
        notify_failure("[autorace] 投票履歴取得 起動失敗", msg)
        return 2

    # stdout/stderr を log に丸ごと追記
    if result.stdout:
        append_log("[stdout]\n" + result.stdout)
    if result.stderr:
        append_log("[stderr]\n" + result.stderr)
    finished = dt.datetime.now().isoformat(timespec="seconds")
    append_log(f"=== {finished} END exit={result.returncode} ===")

    if result.returncode != 0:
        body = (
            f"投票履歴の自動取得が失敗しました (exit={result.returncode})。\n\n"
            f"command: {' '.join(cmd)}\n"
            f"started: {started}\n"
            f"finished: {finished}\n\n"
            f"--- stderr ---\n{result.stderr or '(empty)'}\n\n"
            f"--- stdout ---\n{result.stdout or '(empty)'}\n\n"
            f"よくある原因:\n"
            f"  - Firefox に vote.autorace.jp のログインが切れている\n"
            f"     → ブラウザで再ログインすれば次回 02:30 から復活\n"
            f"  - GraphQL スキーマ変更 / ネットワーク障害\n\n"
            f"ログ: {LOG_FILE}\n"
        )
        notify_failure("[autorace] 投票履歴取得 失敗", body)
        return result.returncode

    return 0


if __name__ == "__main__":
    sys.exit(main())
