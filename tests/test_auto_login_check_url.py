"""自動再ログインの判定 (2026-11-21)。

「auto_login 実行後もセッション切れ」(2026-08-19 / 11-17) の対策。auto_login がログイン要否を
トップページのボタン有無で決めていたため、画面の描画前に「ログイン済み」と誤り、何も入力せず
正常終了していた。発注側と同じ「保護ページを開いてログイン画面へ飛ばされるか」に揃えた。

実サイトには出ない。Playwright は偽物に差し替える。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import auto_login_autorace as al  # noqa: E402

CHECK_URL = "https://vote.autorace.jp/vote?vel_code=5&race_num=11"
HOME_URL = "https://vote.autorace.jp/"
ACCOUNT = {"user_id": "12345678", "password": "dummy-password"}


class _Locator:
    def __init__(self, page, selector):
        self._page, self._selector = page, selector

    @property
    def first(self):
        return self

    async def count(self):
        if "ログイン" in self._selector:          # トップページのログインボタン
            return 1 if self._page.home_shows_login_button else 0
        if "checkbox" in self._selector:
            return 0
        return 1 if "/login" in self._page.url else 0   # 入力欄はログイン画面にだけある

    async def is_visible(self):
        return True

    async def all(self):
        return []


class _Page:
    """セッションの有無で遷移先が変わる、投票サイトの最小限の模型。"""

    def __init__(self, *, session_valid, login_works, session_sticks=True,
                 home_shows_login_button=False):
        self.session_valid = session_valid
        self.login_works = login_works            # ログイン操作でログイン画面を抜けるか
        self.session_sticks = session_sticks      # 抜けた後、保護ページが開けるか
        self.home_shows_login_button = home_shows_login_button
        self.url = "about:blank"
        self.visited: list[str] = []
        self.filled: list[str] = []
        self.submitted = False

    async def goto(self, url, **_):
        self.visited.append(url)
        protected = "/vote?" in url
        if url.endswith("/login") or (protected and not self.session_valid):
            self.url = al.LOGIN_URL
        else:
            self.url = url

    def locator(self, selector):
        return _Locator(self, selector)

    async def fill(self, selector, value, **_):
        self.filled.append(selector)

    async def click(self, selector, **_):
        self.submitted = True
        if self.login_works:
            self.url = HOME_URL
            self.session_valid = self.session_sticks

    async def wait_for_url(self, predicate, **_):
        if not predicate(self.url):
            raise TimeoutError("still on login")


class _Context:
    def __init__(self, page):
        self._page = page

    async def add_init_script(self, *_):
        pass

    async def new_page(self):
        return self._page

    async def cookies(self):
        return [{"name": "sid", "value": "x", "domain": "vote.autorace.jp"}]

    async def close(self):
        pass


def _install_fake_playwright(monkeypatch, page):
    class _PW:
        def __init__(self):
            self.chromium = self

        async def launch_persistent_context(self, **_):
            return _Context(page)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    mod = types.ModuleType("playwright.async_api")
    mod.async_playwright = lambda: _PW()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", mod)

    async def _no_sleep(*_):
        return None

    monkeypatch.setattr(al.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(al, "PROFILE_DIR", ROOT / "tests" / "_no_such_profile_dir")
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: None)


def _run(page, monkeypatch, check_url=CHECK_URL):
    _install_fake_playwright(monkeypatch, page)
    return asyncio.run(al._login_with_playwright(ACCOUNT, headless=True, check_url=check_url))


# ─── 判定の部品 ────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://vote.autorace.jp/login", True),
    ("https://vote.autorace.jp/login?redirect=%2Fvote", True),
    (CHECK_URL, False),
    (HOME_URL, False),
    ("", False),
    (None, False),
])
def test_is_login_redirect(url, expected):
    assert al.is_login_redirect(url) is expected


def test_check_url_must_be_on_the_vote_site():
    assert al.validate_check_url(None) is None
    assert al.validate_check_url(CHECK_URL) == CHECK_URL
    for bad in ("https://example.com/vote", "http://vote.autorace.jp/vote",
                "https://vote.autorace.jp.evil.example/vote", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            al.validate_check_url(bad)


# ─── ログインの流れ ────────────────────────────────────────────

def test_expired_session_logs_in_even_when_home_shows_no_button(monkeypatch):
    """不具合の場面: トップページにボタンがまだ出ていなくても、保護ページの判定でログインする。"""
    page = _Page(session_valid=False, login_works=True, home_shows_login_button=False)
    cookie = _run(page, monkeypatch)
    assert page.submitted, "ログイン操作が行われていない (= 旧来の誤判定)"
    assert page.filled, "ID / パスワードが入力されていない"
    assert cookie == "sid=x"
    assert page.visited.count(CHECK_URL) == 2     # 事前の判定 + ログイン後の確認


def test_valid_session_does_not_touch_the_login_form(monkeypatch):
    page = _Page(session_valid=True, login_works=True)
    cookie = _run(page, monkeypatch)
    assert not page.submitted and not page.filled
    assert cookie == "sid=x"
    assert al.LOGIN_URL not in page.visited


def test_login_that_does_not_stick_is_a_failure(monkeypatch):
    """ログイン画面は抜けたのに保護ページが開けない → 正常終了させない (呼び出し側を誤らせない)。"""
    page = _Page(session_valid=False, login_works=True, session_sticks=False)
    with pytest.raises(RuntimeError, match="ログイン画面へ"):
        _run(page, monkeypatch)


def test_login_rejected_is_a_failure(monkeypatch):
    page = _Page(session_valid=False, login_works=False)
    with pytest.raises(RuntimeError, match="ログイン失敗"):
        _run(page, monkeypatch)


def test_without_check_url_keeps_the_old_home_page_judgement(monkeypatch):
    """check_url なし (購入履歴の取得) は従来どおり。ボタンが見えればログインする。"""
    page = _Page(session_valid=False, login_works=True, home_shows_login_button=True)
    _run(page, monkeypatch, check_url=None)
    assert page.submitted
    assert CHECK_URL not in page.visited


# ─── 発注側からの受け渡し ──────────────────────────────────────

def _load_execute_purchase():
    import importlib
    return importlib.import_module("execute_purchase")


def test_execute_purchase_passes_the_check_url(monkeypatch):
    ep = _load_execute_purchase()
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="OK: cookie 取得 (10 bytes)", stderr="")

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    out = ep._run_auto_login(CHECK_URL)
    assert out.startswith("OK")
    assert seen["cmd"][-2:] == ["--check-url", CHECK_URL]
    assert seen["cmd"][1].endswith("auto_login_autorace.py")


def test_execute_purchase_raises_when_auto_login_fails(monkeypatch):
    ep = _load_execute_purchase()
    import subprocess
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="ログイン画面へ戻される"))
    with pytest.raises(RuntimeError, match="auto_login exit=1"):
        ep._run_auto_login(CHECK_URL)
