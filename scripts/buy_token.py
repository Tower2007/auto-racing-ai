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

DEFAULT_TTL_SEC = 30 * 60  # 30 分 (2026-05-09 Codex P1 hardening: TTL 短縮)

# Phase A 制約 (server-side enforce、payload validation で hard-code)
ALLOWED_AMOUNT = 100  # ¥100 固定 (Phase A 推奨と同期)
ALLOWED_AMOUNT_MIN = 100
ALLOWED_AMOUNT_MAX = 1000
ALLOWED_AMOUNT_UNIT = 100  # 100 円単位

# 三連系まとめ買い (2026-05-30 導入): 複勝(最大¥1000) + rt3¥100 + rf3¥100 = ¥1200
# 余裕を見て合計上限 ¥1500。execute_purchase 側 MAX_TOTAL_YEN と整合させること。
MAX_TOTAL_YEN = 1500
ALLOWED_BET_TYPES = {"fns", "rt3", "rf3"}
# 券種別の cars 長さ (fns=複勝 1 車、rt3=三連単 3 車、rf3=三連複 3 車)
BET_CARS_LEN = {"fns": 1, "rt3": 3, "rf3": 3}


def _validate_bets(bets: list) -> None:
    """bets list (3 券種まとめ買い) を validate。

    各 bet: {"type": "fns"|"rt3"|"rf3", "cars": [..], "amount": int}
    Raises:
        ValueError: 不正値
    """
    if not isinstance(bets, list) or not bets:
        raise ValueError("bets は非空 list である必要があります")
    total = 0
    for i, b in enumerate(bets):
        if not isinstance(b, dict):
            raise ValueError(f"bets[{i}] が dict でない: {b!r}")
        bt = str(b.get("type", ""))
        if bt not in ALLOWED_BET_TYPES:
            raise ValueError(f"bets[{i}].type 不正 ({ALLOWED_BET_TYPES} 期待): {bt!r}")
        cars = b.get("cars", [])
        if not isinstance(cars, list) or len(cars) != BET_CARS_LEN[bt]:
            raise ValueError(
                f"bets[{i}].cars は長さ {BET_CARS_LEN[bt]} の list 必須 ({bt}): {cars!r}"
            )
        for c in cars:
            ci = int(c)
            if not (1 <= ci <= 8):
                raise ValueError(f"bets[{i}].cars に不正な車番 (1-8 期待): {ci}")
        # 三連系は重複車不可
        if bt in ("rt3", "rf3") and len(set(int(c) for c in cars)) != 3:
            raise ValueError(f"bets[{i}].cars に重複車番 ({bt}): {cars!r}")
        amount = int(b.get("amount", 0))
        if amount < ALLOWED_AMOUNT_MIN or amount > ALLOWED_AMOUNT_MAX:
            raise ValueError(
                f"bets[{i}].amount 範囲外 ({ALLOWED_AMOUNT_MIN}-{ALLOWED_AMOUNT_MAX}): {amount}"
            )
        if amount % ALLOWED_AMOUNT_UNIT != 0:
            raise ValueError(f"bets[{i}].amount は {ALLOWED_AMOUNT_UNIT}円単位: {amount}")
        total += amount
    if total > MAX_TOTAL_YEN:
        raise ValueError(f"bets 合計 {total} > 上限 {MAX_TOTAL_YEN}")


def validate_payload(payload: dict, *, strict_amount: bool = True) -> None:
    """payload の必須項目と値域を validate (P3 hardening)。

    Args:
        payload: race_date / place_code / race_no を含む。
                 単一複勝モード: car_no / amount を持つ。
                 まとめ買いモード: bets (list) を持つ (car_no/amount は不要)。
        strict_amount: **default True** (Codex 3 次 review):
                       Phase A の単一複勝本番入口は amount == 100 固定。
                       金額拡張・まとめ買いは明示的に False。

    Raises:
        ValueError: 不正値
    """
    # 共通フィールド (race_date / place_code / race_no)
    for k in ("race_date", "place_code", "race_no"):
        if k not in payload:
            raise ValueError(f"payload に '{k}' が無い")

    # race_date: YYYY-MM-DD format
    rd = str(payload["race_date"])
    try:
        import datetime as _dt
        _dt.date.fromisoformat(rd)
    except Exception:
        raise ValueError(f"race_date 不正 (YYYY-MM-DD 期待): {rd!r}")

    place = int(payload["place_code"])
    if place not in (2, 3, 4, 5, 6):
        raise ValueError(f"place_code 不正 (2-6 期待): {place}")

    race = int(payload["race_no"])
    if not (1 <= race <= 12):
        raise ValueError(f"race_no 不正 (1-12 期待): {race}")

    # まとめ買いモード: bets があればそれを検証して return
    if "bets" in payload and payload["bets"]:
        _validate_bets(payload["bets"])
        return

    # 単一複勝モード (従来)
    for k in ("car_no", "amount"):
        if k not in payload:
            raise ValueError(f"payload に '{k}' が無い")

    car = int(payload["car_no"])
    if not (1 <= car <= 8):
        raise ValueError(f"car_no 不正 (1-8 期待): {car}")

    amount = int(payload["amount"])
    if strict_amount:
        if amount != ALLOWED_AMOUNT:
            raise ValueError(
                f"amount 不正 (Phase A は {ALLOWED_AMOUNT}円 固定): {amount}"
            )
    else:
        if not (ALLOWED_AMOUNT_MIN <= amount <= ALLOWED_AMOUNT_MAX):
            raise ValueError(
                f"amount 範囲外 ({ALLOWED_AMOUNT_MIN}-{ALLOWED_AMOUNT_MAX} 期待): {amount}"
            )
        if amount % ALLOWED_AMOUNT_UNIT != 0:
            raise ValueError(
                f"amount は {ALLOWED_AMOUNT_UNIT}円単位: {amount}"
            )


def is_today_jst(race_date: str) -> bool:
    """race_date が JST today と一致するか (厳密版、deprecated 寄り)。"""
    import datetime as _dt
    jst = _dt.timezone(_dt.timedelta(hours=9))
    today_jst = _dt.datetime.now(jst).date().isoformat()
    return str(race_date) == today_jst


YESTERDAY_ACCEPT_HOUR = 5  # JST 0:00-05:00 までは yesterday も accept


def is_active_race_date(race_date: str) -> bool:
    """race_date が「現在開催中の日付」として妥当か。

    Codex 4 次 review (2026-05-09) を反映:
      - JST today は常に accept
      - JST yesterday は 0:00-05:00 にのみ accept
        (ミッドナイト R の締切後余裕を見て 3 時 → 5 時に拡大)
    """
    import datetime as _dt
    jst = _dt.timezone(_dt.timedelta(hours=9))
    now_jst = _dt.datetime.now(jst)
    today = now_jst.date().isoformat()
    if str(race_date) == today:
        return True
    yesterday = (now_jst.date() - _dt.timedelta(days=1)).isoformat()
    if str(race_date) == yesterday and now_jst.hour < YESTERDAY_ACCEPT_HOUR:
        return True
    return False


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
    """token が既に consume / executed / reserved されたかチェック。

    P2 hardening: reserved 状態 (実行中の他リクエスト) も「消費済」扱いに含める。
    """
    if not TOKEN_LOG.exists():
        return False
    with TOKEN_LOG.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("sig") == sig and row.get("status") in (
                "reserved", "consumed", "executed",
            ):
                return True
    return False


# === P2 hardening: atomic reserve/consume (file lock + check-and-set) ===

LOCK_FILE = TOKEN_LOG.with_suffix(".lock")


class _FileLock:
    """Windows (msvcrt.locking) / Posix (fcntl.flock) 両対応の file lock。

    msvcrt は実装が non-blocking で OSError(`errno=13`) になるので、
    短い retry loop を入れる。
    """

    def __init__(self, path: Path, timeout_sec: float = 10.0):
        self.path = path
        self.timeout = timeout_sec
        self._fp = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(self.path, "a+b")
        # Codex 2 次 review: lock 前にも seek(0) で「先頭 1 byte をロック」を明示
        try:
            self._fp.seek(0)
        except Exception:
            pass
        import time as _t
        deadline = _t.monotonic() + self.timeout
        try:
            import msvcrt  # type: ignore
            while True:
                try:
                    msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if _t.monotonic() > deadline:
                        raise RuntimeError(
                            f"file lock timeout ({self.timeout}s) on {self.path}"
                        )
                    _t.sleep(0.05)
        except ImportError:
            # Posix fallback
            import fcntl  # type: ignore
            fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX)
        return self._fp

    def __exit__(self, *exc):
        if self._fp is None:
            return
        try:
            import msvcrt  # type: ignore
            try:
                # Lock 解放のため seek to start (LK_UNLCK は同 byte)
                self._fp.seek(0)
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        except ImportError:
            try:
                import fcntl  # type: ignore
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            self._fp.close()
        except Exception:
            pass


def _read_status(sig: str) -> str | None:
    """sig に対応する最新 status を返す。複数行あれば最後を採用。"""
    if not TOKEN_LOG.exists():
        return None
    last_status = None
    with TOKEN_LOG.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("sig") == sig:
                last_status = row.get("status", "")
    return last_status


def reserve_token(payload: dict, sig: str, note: str = "") -> bool:
    """token を atomic に reserve (check-and-set)。

    P2 hardening: 同 token を複数タブ / 二重 click しても、reserve に成功するのは
    1 つだけ。reserve 後は execute_purchase が走り、終わり次第
    mark_executed / mark_failed のいずれかで状態遷移する。

    Returns:
        True: 初めて reserve した (購入処理に進んでよい)
        False: 既に reserved/executed/consumed (重複、購入処理してはいけない)
    """
    with _FileLock(LOCK_FILE):
        status = _read_status(sig)
        if status in ("reserved", "consumed", "executed"):
            return False
        log_token(payload, sig=sig, status="reserved", note=note or "atomic reserve")
        return True


def mark_executed(payload: dict, sig: str, note: str = "") -> None:
    """reserve 後の token を executed として記録。"""
    with _FileLock(LOCK_FILE):
        log_token(payload, sig=sig, status="executed", note=note)


def mark_failed(payload: dict, sig: str, note: str = "") -> None:
    """reserve 後の token を failed として記録。再試行可能か方針による。"""
    with _FileLock(LOCK_FILE):
        log_token(payload, sig=sig, status="failed", note=note)


def mark_dry_run(payload: dict, sig: str, note: str = "") -> None:
    """dry-run の記録 (consume 扱いしない、過去ログとして残すだけ)。"""
    log_token(payload, sig=sig, status="dry_run", note=note)


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
    if host.startswith("http://") or host.startswith("https://"):
        base = host.rstrip("/")
    else:
        base = f"http://{host}:{port}"
    return f"{base}/?p={b64}&s={sig}"


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
