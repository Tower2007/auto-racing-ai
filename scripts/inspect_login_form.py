"""vote.autorace.jp のログインフォーム構造を調査する一時スクリプト。

auto_login_autorace.py の selector candidate が合わなかったので、実際の
HTML 構造を取り出して input/button の name/id/type を全部 dump する。
結果を見て auto_login_autorace.py の selector list を修正する。

使い方:
  python scripts/inspect_login_form.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "profiles" / "autorace"


async def inspect():
    from playwright.async_api import async_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
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

        try:
            # まず home ページに遷移、ログインボタンを目視で探す
            await page.goto("https://vote.autorace.jp/", wait_until="domcontentloaded",
                            timeout=30000)
            await asyncio.sleep(3)
            print(f"\n[1] HOME URL: {page.url}")

            # ログインリンクを探す
            login_links = await page.locator(
                'a:has-text("ログイン"), button:has-text("ログイン")'
            ).all()
            print(f"[1] ログインリンク数: {len(login_links)}")
            for i, link in enumerate(login_links):
                try:
                    href = await link.get_attribute("href")
                    text = await link.inner_text()
                    print(f"    [{i}] href={href!r}  text={text.strip()!r}")
                except Exception as e:
                    print(f"    [{i}] error: {e}")

            # 最初のログインリンクをクリック
            if login_links:
                print("\n[2] ログインリンクをクリック ...")
                try:
                    await login_links[0].click(timeout=5000)
                    await asyncio.sleep(3)
                    print(f"    遷移後 URL: {page.url}")
                except Exception as e:
                    print(f"    click 失敗: {e}")

            # ログインページの HTML を dump
            print("\n[3] 現在のページの全 input 要素:")
            inputs = await page.locator("input").all()
            print(f"    input 数: {len(inputs)}")
            for i, inp in enumerate(inputs):
                try:
                    info = await inp.evaluate(
                        "(el) => ({name: el.name, id: el.id, type: el.type, "
                        "placeholder: el.placeholder, label: el.getAttribute('aria-label'), "
                        "visible: el.offsetParent !== null})"
                    )
                    print(f"    [{i}] {info}")
                except Exception as e:
                    print(f"    [{i}] error: {e}")

            print("\n[4] 現在のページの button 要素:")
            buttons = await page.locator("button").all()
            print(f"    button 数: {len(buttons)}")
            for i, btn in enumerate(buttons):
                try:
                    info = await btn.evaluate(
                        "(el) => ({type: el.type, id: el.id, "
                        "text: el.innerText, visible: el.offsetParent !== null})"
                    )
                    print(f"    [{i}] {info}")
                except Exception as e:
                    print(f"    [{i}] error: {e}")

            print("\n[5] 現在のページの form 要素:")
            forms = await page.locator("form").all()
            print(f"    form 数: {len(forms)}")
            for i, form in enumerate(forms):
                try:
                    info = await form.evaluate(
                        "(el) => ({action: el.action, method: el.method, id: el.id, name: el.name})"
                    )
                    print(f"    [{i}] {info}")
                except Exception as e:
                    print(f"    [{i}] error: {e}")

            # ページ全体の HTML を保存
            html_dump = ROOT / "data" / "login_form_dump.html"
            html_dump.parent.mkdir(parents=True, exist_ok=True)
            html = await page.content()
            html_dump.write_text(html, encoding="utf-8")
            print(f"\n[6] HTML 保存先: {html_dump}")
            print(f"    最終 URL: {page.url}")

            # 5 秒待って window を閉じる
            print("\n[done] 5 秒後にウインドウを閉じます ...")
            await asyncio.sleep(5)

        finally:
            try:
                await context.close()
            except Exception:
                pass


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    asyncio.run(inspect())


if __name__ == "__main__":
    main()
