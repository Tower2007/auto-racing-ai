"""ngrok トンネル管理 — 購入推奨メール送信時に一時起動。

daily_predict.py から呼ばれ、buy_app.py (port 8502) への
ngrok トンネルを起動して公開 URL を返す。
指定秒後に自動停止する別スレッドを仕込む。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_ngrok_process: subprocess.Popen | None = None
_lock = threading.Lock()

NGROK_CMD = r"C:\Users\no28a\AppData\Local\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe"
DEFAULT_PORT = 8502
DEFAULT_TTL_SEC = 300  # 5 分
_NGROK_LOG = str(Path(__file__).resolve().parent.parent / "data" / "ngrok_process.log")

# authtoken: .env → 環境変数 の順で解決。config ファイルに依存しない
# (schtasks 環境では %LOCALAPPDATA%\ngrok\ngrok.yml が見えないため)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOTENV = _PROJECT_ROOT / ".env"


def _load_authtoken() -> str | None:
    """NGROK_AUTHTOKEN を .env → 環境変数の順で取得。"""
    # .env から直接読む (dotenv ライブラリ不要)
    if _DOTENV.exists():
        try:
            for line in _DOTENV.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("NGROK_AUTHTOKEN="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return val
        except Exception:
            pass
    return os.environ.get("NGROK_AUTHTOKEN")


MAX_RETRIES = 2  # CRL タイムアウト等の一時障害対策 (2026-05-27 導入)
POLL_TIMEOUT_SEC = 30  # 各試行での URL 待ち秒数


def start_tunnel(port: int = DEFAULT_PORT,
                 ttl_sec: int = DEFAULT_TTL_SEC) -> str | None:
    """ngrok トンネルを起動し、公開 URL を返す。

    ttl_sec 秒後に自動停止するタイマーを仕込む。
    既に起動中（手動含む）なら既存の URL を返す。
    一時的なネットワーク障害 (CRL timeout 等) に備え最大 MAX_RETRIES 回リトライ。
    失敗時は None。
    """
    global _ngrok_process

    # まず既存の ngrok（手動起動含む）の URL を試す — 殺さずに再利用
    url = _get_public_url()
    if url:
        logger.info("reusing existing ngrok tunnel: %s", url)
        _schedule_stop(ttl_sec)
        return url

    authtoken = _load_authtoken()
    if not authtoken:
        logger.error("NGROK_AUTHTOKEN not found in .env or env vars")
        return None

    for attempt in range(MAX_RETRIES):
        if attempt > 0:
            logger.info("ngrok retry %d/%d", attempt + 1, MAX_RETRIES)

        with _lock:
            _kill_existing()
            time.sleep(3)  # ポート解放待ち (4040 bind 競合回避)

            try:
                cmd = [NGROK_CMD, "http", str(port),
                       "--authtoken", authtoken,
                       "--log", _NGROK_LOG, "--log-format", "json"]
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                )
                _ngrok_process = proc
                logger.info("ngrok started (pid=%d, port=%d, ttl=%ds, authtoken=***)",
                            proc.pid, port, ttl_sec)
            except FileNotFoundError:
                logger.error("ngrok command not found: %s", NGROK_CMD)
                return None
            except Exception as e:
                logger.error("ngrok start failed: %s", e)
                return None

        for i in range(POLL_TIMEOUT_SEC):
            time.sleep(1)
            # プロセスが死んでいたら即打ち切り
            if _ngrok_process and _ngrok_process.poll() is not None:
                logger.error("ngrok process exited with code %d (see %s)",
                             _ngrok_process.returncode, _NGROK_LOG)
                break
            url = _get_public_url()
            if url:
                logger.info("ngrok tunnel URL: %s (%ds)", url, i + 1)
                _schedule_stop(ttl_sec)
                return url

        logger.warning("ngrok tunnel URL not available after %ds (attempt %d/%d)",
                       POLL_TIMEOUT_SEC, attempt + 1, MAX_RETRIES)
        stop_tunnel()

    logger.error("ngrok tunnel failed after %d attempts", MAX_RETRIES)
    return None


def stop_tunnel() -> None:
    """ngrok プロセスを停止。"""
    global _ngrok_process
    with _lock:
        _kill_existing()
        _ngrok_process = None


def _kill_existing() -> None:
    """既存の ngrok プロセスを終了。"""
    global _ngrok_process
    if _ngrok_process and _ngrok_process.poll() is None:
        try:
            _ngrok_process.terminate()
            _ngrok_process.wait(timeout=5)
        except Exception:
            try:
                _ngrok_process.kill()
            except Exception:
                pass
        logger.info("ngrok stopped (pid=%d)", _ngrok_process.pid)
    # orphan ngrok も止める
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "ngrok.exe"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _get_public_url() -> str | None:
    """ngrok API から公開 URL を取得。"""
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:4040/api/tunnels")
        with urllib.request.urlopen(req, timeout=1) as resp:
            data = json.loads(resp.read())
        for t in data.get("tunnels", []):
            url = t.get("public_url", "")
            if url.startswith("https://"):
                return url
        for t in data.get("tunnels", []):
            url = t.get("public_url", "")
            if url:
                return url
    except Exception:
        pass
    return None


def _schedule_stop(ttl_sec: int) -> None:
    """ttl_sec 秒後に ngrok を停止するタイマーを起動。"""
    def _delayed_stop():
        time.sleep(ttl_sec)
        logger.info("ngrok TTL expired (%ds), stopping tunnel", ttl_sec)
        stop_tunnel()

    t = threading.Thread(target=_delayed_stop, daemon=True)
    t.start()
