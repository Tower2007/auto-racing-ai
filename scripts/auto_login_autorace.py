"""vote.autorace.jp に Playwright で自動ログインし、cookie を取得する。

2026-05-08 導入: Firefox cookie 失効が daily 頻度に増えたため、IPO project
(C:/Users/no28a/Claude-project/sbi_ipo_auto) と同じ Playwright + 永続プロファイル
パターンで自動ログイン化。

使い方:
  - スタンドアロン: python scripts/auto_login_autorace.py
                   → cookie 文字列を stdout に出力 (デバッグ用)
  - import 経由: from auto_login_autorace import login_and_get_cookie
                cookie = login_and_get_cookie()
                # → fetch_order_history.py の cookie source として使用

事前準備:
  1. pip install playwright
  2. playwright install chromium  (初回のみ)
  3. accounts.json 作成 (accounts.json.template 参照)
  4. 初回実行は accounts.json の "headless": false で目視確認
     (フォーム selector が想定と合うか、CAPTCHA 等が出ないか)

設計メモ:
  - 永続プロファイル (profiles/autorace/) を使うことで、初回ログイン後の
    cookie / セッションが PC 内に保存される
  - 2 回目以降はプロファイル経由で「ログイン済」状態で起動 → 失効していれば
    自動的にフォーム入力 → cookie 更新
  - SBI と違い vote.autorace.jp は端末認証 (OTP) が無さそうなので、毎回 1 つの
    スクリプトで login → cookie 取得 まで完結する想定。
  - もし OTP / CAPTCHA が出たら本スクリプトは失敗、Firefox 手動運用に fallback。

ToS 観点 (memory ml_baseline_findings.md 2026-05-08):
  - 自動ログイン採用 (IPO project と同じスタンス: 本人 PC 上で本人運用)
  - 自動購入は引き続き不採用 (規約 第13条「自ら申込む」抵触)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_PATH = ROOT / "accounts.json"
PROFILE_DIR = ROOT / "profiles" / "autorace"

# vote.autorace.jp のログインフロー (2026-05-08 inspect で確認済)
# - 公式 URL: https://vote.autorace.jp/login
# - 入力フィールド: userNumber (半角数字), password (半角英数字)
# - 規約同意 checkbox 3 個 (全部 check してから submit)
# - submit ボタン: button[type="submit"] (text "ログインする")
LOGIN_URL = "https://vote.autorace.jp/login"
HOME_URL_PREFIX = "https://vote.autorace.jp"


async def _login_with_playwright(account: dict, headless: bool = False) -> str:
    """Playwright で login し cookie 文字列を返す。

    Returns:
        "name1=value1; name2=value2; ..." 形式の Cookie ヘッダ文字列
        (requests / GraphQL POST にそのまま使える形)

    Raises:
        RuntimeError: login 失敗 / cookie 取得失敗
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "playwright が未インストール。\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        # SBI と同じボット検知回避設定
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            channel="chrome",
            viewport={"width": 1280, "height": 900},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            ignore_default_args=["--enable-automation"],
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined})"
        )
        page = await context.new_page()
        cookie_str = ""

        try:
            # まず home ページに行ってログイン状態を確認
            await page.goto(HOME_URL_PREFIX + "/", wait_until="domcontentloaded",
                            timeout=30000)
            await asyncio.sleep(2)

            # ログイン状態判定: ログインフォーム / ログインボタンが見えるか
            need_login = False
            try:
                # ログインボタンが見えれば未ログイン
                login_link = page.locator(
                    'a:has-text("ログイン"), button:has-text("ログイン")'
                ).first
                if await login_link.count() > 0 and await login_link.is_visible():
                    need_login = True
            except Exception:
                need_login = True

            if need_login:
                print(f"[auto_login] ログインが必要 (current URL: {page.url})",
                      file=sys.stderr)
                # login URL に直接遷移
                await page.goto(LOGIN_URL, wait_until="domcontentloaded",
                                timeout=30000)
                await asyncio.sleep(2)

                # フォーム入力 (selector は 2026-05-08 inspect で確定)
                user_id_filled = False
                for sel in [
                    'input[name="userNumber"]',  # vote.autorace.jp の正式名
                    'input[name="userId"]',
                    'input[name="user_id"]',
                    'input[name="memberId"]',
                    'input[type="text"]:visible',
                ]:
                    try:
                        if await page.locator(sel).count() > 0:
                            await page.fill(sel, account["user_id"], timeout=3000)
                            user_id_filled = True
                            print(f"[auto_login] user_id input: {sel}",
                                  file=sys.stderr)
                            break
                    except Exception:
                        continue
                if not user_id_filled:
                    raise RuntimeError(
                        "ログインフォームの user_id 入力欄が見つかりません。\n"
                        "ヒント: scripts/inspect_login_form.py で構造を再確認"
                    )

                pw_filled = False
                for sel in [
                    'input[name="password"]',
                    'input[type="password"]:visible',
                ]:
                    try:
                        if await page.locator(sel).count() > 0:
                            await page.fill(sel, account["password"], timeout=3000)
                            pw_filled = True
                            print(f"[auto_login] password input: {sel}",
                                  file=sys.stderr)
                            break
                    except Exception:
                        continue
                if not pw_filled:
                    raise RuntimeError("password 入力欄が見つかりません。")

                # PIN がある場合 (vote.autorace.jp は通常 PIN なし)
                if "pin" in account:
                    for sel in [
                        'input[name="pin"]',
                        'input[name="pinCode"]',
                        'input[type="tel"]:visible',
                    ]:
                        try:
                            if await page.locator(sel).count() > 0:
                                await page.fill(sel, account["pin"], timeout=3000)
                                print(f"[auto_login] pin input: {sel}",
                                      file=sys.stderr)
                                break
                        except Exception:
                            continue

                # 規約同意 checkbox を全て check (vote.autorace.jp は 3 個)
                try:
                    checkboxes = await page.locator(
                        'input[type="checkbox"]:visible'
                    ).all()
                    if checkboxes:
                        for i, cb in enumerate(checkboxes):
                            try:
                                is_checked = await cb.is_checked()
                                if not is_checked:
                                    await cb.check(timeout=2000)
                                    print(f"[auto_login] checkbox[{i}] checked",
                                          file=sys.stderr)
                            except Exception as e:
                                print(f"[auto_login] checkbox[{i}] check 失敗: {e}",
                                      file=sys.stderr)
                except Exception as e:
                    print(f"[auto_login] checkbox 処理スキップ: {e}",
                          file=sys.stderr)

                # submit
                submitted = False
                for sel in [
                    'button[type="submit"]',
                    'button:has-text("ログインする")',
                    'button:has-text("ログイン")',
                    'input[type="submit"]',
                ]:
                    try:
                        await page.click(sel, timeout=3000)
                        submitted = True
                        print(f"[auto_login] submit: {sel}", file=sys.stderr)
                        break
                    except Exception:
                        continue
                if not submitted:
                    raise RuntimeError("ログイン submit ボタンが見つかりません。")

                # ログイン成功を URL 変化で判定 (login → home へのリダイレクト)
                try:
                    await page.wait_for_url(
                        lambda url: "/login" not in url,
                        timeout=15000,
                    )
                except Exception:
                    print(f"[auto_login] login URL 待機タイムアウト: {page.url}",
                          file=sys.stderr)

                await asyncio.sleep(2)

                if "/login" in page.url:
                    raise RuntimeError(
                        f"ログイン失敗 (login ページに留まっている): {page.url}\n"
                        "ID/PW 誤り or CAPTCHA / 2FA 発動の可能性。"
                    )
                print(f"[auto_login] login 完了: {page.url}", file=sys.stderr)
            else:
                print(f"[auto_login] 既にログイン済 (URL: {page.url})",
                      file=sys.stderr)

            # autorace.jp ドメインの cookie を全部抽出
            cookies = await context.cookies()
            cookie_pairs = [
                f"{c['name']}={c['value']}"
                for c in cookies
                if "autorace.jp" in c.get("domain", "")
            ]
            if not cookie_pairs:
                raise RuntimeError("autorace.jp の cookie が見つかりません。")
            cookie_str = "; ".join(cookie_pairs)
            print(f"[auto_login] cookie 取得 ({len(cookie_pairs)} 個)",
                  file=sys.stderr)

        finally:
            try:
                await context.close()
            except Exception:
                pass

    if not cookie_str:
        raise RuntimeError("auto_login: cookie 取得失敗")
    return cookie_str


def login_and_get_cookie() -> str:
    """同期 wrapper。fetch_order_history.py から import される想定。"""
    if not ACCOUNTS_PATH.exists():
        raise FileNotFoundError(
            f"{ACCOUNTS_PATH} が無い。\n"
            f"  cp {ACCOUNTS_PATH.with_suffix('.json.template')} {ACCOUNTS_PATH}\n"
            f"  → 実際の vote.autorace.jp 資格情報を記入"
        )
    with ACCOUNTS_PATH.open(encoding="utf-8") as f:
        config = json.load(f)
    accounts = config.get("accounts", [])
    if not accounts:
        raise RuntimeError("accounts.json に 'accounts' が無い")
    account = accounts[0]  # vote.autorace.jp は 1 ユーザー想定
    headless = config.get("headless", False)
    return asyncio.run(_login_with_playwright(account, headless=headless))


def main() -> None:
    """スタンドアロン実行。cookie 文字列を stdout に出す (デバッグ・確認用)。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cookie = login_and_get_cookie()
    # cookie 全体は秘匿性高いので長さだけ stdout、cookie 値は stderr
    print(f"OK: cookie 取得 ({len(cookie)} bytes)")
    print(f"[debug] cookie (head): {cookie[:80]}...", file=sys.stderr)


if __name__ == "__main__":
    main()
