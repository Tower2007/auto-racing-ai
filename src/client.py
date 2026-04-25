"""autorace.jp JSON API クライアント

CSRF トークン自動取得 + Session 管理 + リトライ/指数バックオフ。
全 POST エンドポイントは Laravel CSRF 認証が必要。
"""

import os
import re
import time
import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://autorace.jp"

# 稼働中の場コード (船橋=1 は 2016年閉場)
VENUE_CODES: dict[int, str] = {
    2: "kawaguchi",
    3: "isesaki",
    4: "hamamatsu",
    5: "iizuka",
    6: "sanyou",
}


class AutoraceAPIError(Exception):
    """API リクエスト失敗"""

    def __init__(self, endpoint: str, status_code: int, detail: str = ""):
        self.endpoint = endpoint
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{endpoint} returned {status_code}: {detail}")


class AutoraceClient:
    """autorace.jp JSON API ラッパー"""

    def __init__(self) -> None:
        self._delay = float(os.getenv("AUTORACE_REQUEST_DELAY_SEC", "0.5"))
        self._ua = os.getenv(
            "AUTORACE_USER_AGENT",
            "Mozilla/5.0 (auto-racing-ai research; "
            "+https://github.com/Tower2007/auto-racing-ai)",
        )
        self._session = self._build_session()
        self._csrf_token: str | None = None

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        # urllib3 レベルのリトライ (接続エラー・タイムアウト用)
        retries = Retry(
            total=5,
            backoff_factor=1.0,  # 1, 2, 4, 8, 16 sec
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retries)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({"User-Agent": self._ua})
        return s

    # ─── CSRF ───────────────────────────────────────────

    def _ensure_csrf(self) -> str:
        """CSRF トークンを取得 (未取得 or 期限切れ時)。"""
        if self._csrf_token:
            return self._csrf_token

        url = f"{BASE_URL}/race_info/Live/kawaguchi"
        logger.info("CSRF token fetch: GET %s", url)
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()

        m = re.search(r'csrf-token"\s+content="([^"]+)"', resp.text)
        if not m:
            raise AutoraceAPIError(url, resp.status_code, "csrf-token meta not found")

        self._csrf_token = m.group(1)
        # Session Cookie (XSRF-TOKEN) は requests.Session が自動保存
        self._session.headers.update(
            {
                "X-CSRF-TOKEN": self._csrf_token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            }
        )
        logger.info("CSRF token acquired (len=%d)", len(self._csrf_token))
        return self._csrf_token

    def reset_csrf(self) -> None:
        """CSRF トークンを強制リセット (419 エラー時に呼ぶ)。"""
        self._csrf_token = None

    # ─── 内部 HTTP ──────────────────────────────────────

    def _get(self, path: str, **kwargs: Any) -> Any:
        url = f"{BASE_URL}{path}"
        logger.debug("GET %s", url)
        resp = self._session.get(url, timeout=30, **kwargs)
        resp.raise_for_status()
        time.sleep(self._delay)
        return resp.json()

    def _post(self, path: str, data: dict[str, Any]) -> Any:
        """POST リクエスト。CSRF 自動取得 + 419 リトライ。"""
        self._ensure_csrf()
        url = f"{BASE_URL}{path}"
        logger.debug("POST %s data=%s", url, data)

        for attempt in range(3):
            resp = self._session.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            if resp.status_code == 419:
                # CSRF expired — 再取得して再試行
                logger.warning("419 CSRF expired, refreshing (attempt %d)", attempt + 1)
                self.reset_csrf()
                self._ensure_csrf()
                continue
            resp.raise_for_status()
            time.sleep(self._delay)
            return resp.json()

        raise AutoraceAPIError(path, 419, "CSRF refresh exhausted after 3 attempts")

    # ─── 公開 API ──────────────────────────────────────

    def get_today_hold(self) -> Any:
        """当日開催情報 (GET、認証不要)。"""
        return self._get("/race_info/XML/Hold/Today")

    def get_recent_hold(self, place_code: int) -> Any:
        """直近開催情報。"""
        return self._post("/race_info/XML/Hold/Recent", {"placeCode": place_code})

    def get_players(self, place_code: int, race_date: str) -> Any:
        """開催の出場選手リスト。race_date: 'YYYY-MM-DD'"""
        return self._post(
            "/race_info/Player",
            {"placeCode": place_code, "raceDate": race_date},
        )

    def get_program(self, place_code: int, race_date: str, race_no: int) -> Any:
        """出走表 (1レース分)。"""
        return self._post(
            "/race_info/Program",
            {"placeCode": place_code, "raceDate": race_date, "raceNo": race_no},
        )

    def get_odds(self, place_code: int, race_date: str, race_no: int) -> Any:
        """オッズ + AI予想。"""
        return self._post(
            "/race_info/Odds",
            {"placeCode": place_code, "raceDate": race_date, "raceNo": race_no},
        )

    def get_race_result(self, place_code: int, race_date: str, race_no: int) -> Any:
        """レース結果 + 周回ランク変動。"""
        return self._post(
            "/race_info/RaceResult",
            {"placeCode": place_code, "raceDate": race_date, "raceNo": race_no},
        )

    def get_race_refund(self, place_code: int, race_date: str) -> Any:
        """払戻金 (1日分まとめて、raceNo 不要)。"""
        return self._post(
            "/race_info/RaceRefund",
            {"placeCode": place_code, "raceDate": race_date},
        )
