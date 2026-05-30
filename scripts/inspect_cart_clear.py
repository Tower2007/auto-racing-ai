"""カート「全削除」後に出る確認ポップアップの構造を調査する一時スクリプト。

execute_purchase の Step 1.5 (カートクリア) が全削除を押してもカートが消えない
問題の調査用。全削除クリック後に出るモーダル / dialog のボタン文言を dump する。

使い方:
  python scripts/inspect_cart_clear.py            # 浜松(4) R7 の confirm 画面
  python scripts/inspect_cart_clear.py 4 7        # place_code, race_no 指定

⚠️ このスクリプトは「全削除」を押すが、確認ポップアップでは何も押さない
   (観察のみ)。実際の削除はしない想定だが、もし確認なしで即削除される UI なら
   カートは空になる (それはそれで OK)。

出力:
  - data/cart_clear_dump.txt   (全削除 前後の visible button / dialog 文言)
  - data/cart_clear_before.png / cart_clear_after.png
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "profiles" / "autorace"
DUMP_TXT = ROOT / "data" / "cart_clear_dump.txt"

VEL_CODE_MAP = {2: "002", 3: "003", 4: "004", 5: "005", 6: "006"}


async def _visible_buttons(page) -> list:
    """画面上の visible な button / a / [role=button] のテキスト一覧。"""
    return await page.evaluate(
        """() => {
            const out = [];
            const sels = 'button, a, [role=button], input[type=submit], input[type=button]';
            for (const el of document.querySelectorAll(sels)) {
                if (el.offsetParent === null) continue;
                const t = (el.innerText || el.value || '').trim();
                if (t) out.push({tag: el.tagName, text: t.slice(0, 30),
                                 cls: (el.className || '').toString().slice(0, 50)});
            }
            return out;
        }"""
    )


async def _dialog_like(page) -> list:
    """role=dialog / class に modal|dialog|popup を含む要素のテキスト。"""
    return await page.evaluate(
        """() => {
            const out = [];
            const els = document.querySelectorAll(
                '[role=dialog], [class*=modal], [class*=Modal], '
                + '[class*=dialog], [class*=Dialog], [class*=popup], [class*=Popup]'
            );
            for (const el of els) {
                if (el.offsetParent === null) continue;
                out.push({cls: (el.className||'').toString().slice(0,60),
                          text: (el.innerText||'').trim().slice(0,200)});
            }
            return out;
        }"""
    )


async def _cart_n(page) -> str:
    try:
        txt = await page.evaluate("() => document.body.innerText")
    except Exception:
        return "?"
    import re
    m = re.search(r"投票数\s*\n?\s*(\d+)\s*組", txt)
    return m.group(1) + "組" if m else "(組数不明)"


async def inspect(place_code: int, race_no: int) -> None:
    from playwright.async_api import async_playwright

    vel_code = VEL_CODE_MAP[place_code]
    lines: list[str] = []

    def out(msg: str) -> None:
        print(msg)
        lines.append(msg)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    DUMP_TXT.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            channel="chrome",
            viewport={"width": 1280, "height": 900},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            ignore_default_args=["--enable-automation"],
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        # native dialog が出たら内容を記録 (accept はしない = 観察のみ)
        dialog_seen = []

        def _on_dialog(d):
            dialog_seen.append({"type": d.type, "message": d.message})
            # 観察のみ: dismiss して画面を保つ
            asyncio.create_task(d.dismiss())

        page.on("dialog", _on_dialog)

        try:
            confirm_url = (
                f"https://vote.autorace.jp/vote/confirm"
                f"?vel_code={vel_code}&race_num={race_no}"
            )
            out(f"[1] goto: {confirm_url}")
            await page.goto(confirm_url, wait_until="domcontentloaded",
                            timeout=30000)
            await asyncio.sleep(2)
            out(f"    URL: {page.url}")
            if "/login" in page.url:
                out("    [!] /login にリダイレクト — セッション切れ。"
                    "先に auto_login_autorace.py を実行してください。")
                return

            out(f"[2] 全削除クリック前: カート {await _cart_n(page)}")
            await page.screenshot(
                path=str(ROOT / "data" / "cart_clear_before.png"),
                full_page=True)

            del_btn = page.locator('button:has-text("全削除")').first
            if await del_btn.count() == 0:
                out("    [!] 全削除ボタンが無い (カート空?)。終了")
                return

            out("[3] 全削除をクリック → 直後の状態を観察")
            await del_btn.click(timeout=5000)
            await asyncio.sleep(1.5)

            out(f"\n[4] native dialog: {dialog_seen if dialog_seen else 'なし'}")

            out("\n[5] click 直後の visible buttons:")
            for b in await _visible_buttons(page):
                out(f"    {b}")

            out("\n[6] modal / dialog like 要素:")
            dl = await _dialog_like(page)
            if dl:
                for d in dl:
                    out(f"    cls={d['cls']!r}")
                    out(f"      text={d['text']!r}")
            else:
                out("    (modal/dialog class の要素なし)")

            out(f"\n[7] 現在のカート: {await _cart_n(page)}")
            await page.screenshot(
                path=str(ROOT / "data" / "cart_clear_after.png"),
                full_page=True)
            out("    screenshot: data/cart_clear_after.png")

            out("\n[done] 8 秒後に閉じます (画面を目視確認してください)")
            await asyncio.sleep(8)
        finally:
            try:
                await context.close()
            except Exception:
                pass

    DUMP_TXT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n=== dump: {DUMP_TXT} ===")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("place_code", type=int, nargs="?", default=4,
                   choices=[2, 3, 4, 5, 6])
    p.add_argument("race_no", type=int, nargs="?", default=7)
    args = p.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    asyncio.run(inspect(args.place_code, args.race_no))


if __name__ == "__main__":
    main()
