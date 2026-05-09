"""HMAC token sign / verify for click-to-buy 機構 (2026-05-08 導入)。

各 Phase A 推奨候補ごとに改ざん防止 token を発行し、購入確認 URL に埋める。
buy_app.py が token を verify して確認画面を表示、ユーザーが click で購入実行。

設計:
  - 各 token は payload (race info + amount + ev + exp) に HMAC-SHA256 を 16 文字
  - secret は accounts.json の "buy_secret_key" に保存
  - default TTL 24 時間
  - 1 token 1 回限り (data/buy_tokens.csv で管理)
  - 改ざん検出 → ValueError、期限切れ → ValueError、消費済み → is_consumed True

依存: accounts.json に buy_secret_key を設定。
  例: "buy_secret_key": "適当な long random string (32+ chars)"
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_PATH = ROOT / "accounts.json"
TOKEN_LOG = ROOT / "data" / "buy_tokens.csv"

DEFAULT_TTL_SEC = 24 * 3600  # 24 時間


def _load_secret() -> bytes:
    """accounts.json から buy_secret_key を読む。"""
    if not ACCOUNTS_PATH.exists():
        raise FileNotFoundError(
            f"{ACCOUNTS_PATH} が無い。template からコピーして buy_secret_key を設定。"
        )
    with ACCOUNTS_PATH.open(encoding="utf-8") as f:
        config = json.load(f)
    secret = config.get("buy_secret_key", "")
    if not secret:
        raise RuntimeError(
            "accounts.json に 'buy_secret_key' が無い。\n"
            "32 文字以上のランダム文字列を設定してください。"
        )
    return secret.encode()


def sign(payload: dict, ttl_sec: int = DEFAULT_TTL_SEC) -> tuple[str, str]:
    """payload に exp を付けて (b64_payload, signature) を返す。

    payload は race_date / place_code / race_no / car_no / amount / ev を含む dict。
    Returns:
        (b64_payload_url_safe, signature_hex_16char)
    """
    p = dict(payload)
    p.setdefault("exp", int(time.time()) + ttl_sec)
    raw = json.dumps(p, separators=(",", ":"), sort_keys=True).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    sig = hmac.new(_load_secret(), b64.encode("ascii"),
                   hashlib.sha256).hexdigest()[:16]
    return b64, sig


def verify(b64_payload: str, signature: str) -> dict:
    """signature 検証 + 期限チェック → payload dict。

    Raises:
        ValueError: 不正 signature / 期限切れ
    """
    expected = hmac.new(_load_secret(), b64_payload.encode("ascii"),
                        hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(expected, signature):
        raise ValueError("signature mismatch (token 改ざんの可能性)")
    pad = "=" * (-len(b64_payload) % 4)
    raw = base64.urlsafe_b64decode((b64_payload + pad).encode("ascii"))
    payload = json.loads(raw)
    exp = payload.get("exp", 0)
    if int(time.time()) > exp:
        raise ValueError(f"token expired (exp={exp})")
    return payload


def _ensure_log_header() -> None:
    if TOKEN_LOG.exists():
        return
    TOKEN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TOKEN_LOG.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([
            "created_at", "status",
            "race_date", "place_code", "race_no", "car_no",
            "amount", "ev", "sig", "note",
        ])


def log_token(payload: dict, sig: str, status: str, note: str = "") -> None:
    """tokens.csv に記録 (audit)。

    status: "issued" | "consumed" | "executed" | "failed" | "dry_run"
    """
    _ensure_log_header()
    with TOKEN_LOG.open("a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([
            time.strftime("%Y-%m-%dT%H:%M:%S"), status,
            payload.get("race_date", ""), payload.get("place_code", ""),
            payload.get("race_no", ""), payload.get("car_no", ""),
            payload.get("amount", ""), payload.get("ev", ""),
            sig, note,
        ])


def is_consumed(sig: str) -> bool:
    """token が既に consume / executed されたかチェック (重複購入防止)。"""
    if not TOKEN_LOG.exists():
        return False
    with TOKEN_LOG.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("sig") == sig and row.get("status") in (
                "consumed", "executed",
            ):
                return True
    return False


def _get_tailscale_ip() -> str | None:
    """Tailscale CLI で自分の Tailscale IPv4 を取得 (失敗時 None)。"""
    import subprocess
    candidates = [
        "tailscale",
        r"C:\Program Files\Tailscale\tailscale.exe",
        r"C:\Program Files (x86)\Tailscale\tailscale.exe",
    ]
    for cmd in candidates:
        try:
            r = subprocess.run(
                [cmd, "ip", "-4"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                ip = r.stdout.strip().splitlines()[0].strip()
                if ip.startswith("100."):  # Tailscale CGNAT 範囲
                    return ip
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        except Exception:
            continue
    return None


def get_lan_ip() -> str:
    """LAN IP を検出 (UDP socket trick で 8.8.8.8 への route から自分の IP を取得)。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def get_host_ip() -> str:
    """購入 URL に使うホスト IP を決定 (優先順位):

    1. accounts.json の "host_ip" が設定されていればそれ
       (Tailscale IP / 公開 IP / LAN IP を手動指定可)
    2. Tailscale CLI で自動検出 (100.x.y.z)
    3. LAN IP fallback (192.168.x.y 等)
    """
    # (1) accounts.json 手動指定
    if ACCOUNTS_PATH.exists():
        try:
            with ACCOUNTS_PATH.open(encoding="utf-8") as f:
                config = json.load(f)
            host_ip = config.get("host_ip", "").strip()
            if host_ip and not host_ip.startswith("REPLACE"):
                return host_ip
        except Exception:
            pass

    # (2) Tailscale 自動検出
    ts_ip = _get_tailscale_ip()
    if ts_ip:
        return ts_ip

    # (3) LAN IP fallback
    return get_lan_ip()


def build_buy_url(payload: dict, host: str | None = None,
                   port: int = 8502) -> str:
    """購入確認画面 (buy_app.py) への URL を返す。

    Args:
        payload: race_date / place_code / race_no / car_no / amount / ev / venue_jp
        host: None なら get_host_ip() で自動決定 (accounts.json > Tailscale > LAN)
        port: buy_app の port (default 8502)
    """
    b64, sig = sign(payload)
    if host is None:
        host = get_host_ip()
    return f"http://{host}:{port}/?p={b64}&s={sig}"


if __name__ == "__main__":
    # 自己テスト
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    test_payload = {
        "race_date": "2026-05-08",
        "place_code": 6, "venue": "sanyou", "venue_jp": "山陽",
        "race_no": 5, "car_no": 4,
        "amount": 100, "ev": 1.62,
    }
    b64, sig = sign(test_payload)
    print(f"b64: {b64}")
    print(f"sig: {sig}")
    verified = verify(b64, sig)
    print(f"verified: {verified}")
    url = build_buy_url(test_payload)
    print(f"url: {url}")
    print(f"host_ip 決定経路:")
    ts = _get_tailscale_ip()
    print(f"  tailscale ip -4 → {ts!r}")
    print(f"  LAN IP fallback → {get_lan_ip()!r}")
