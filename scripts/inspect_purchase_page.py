"""vote.autorace.jp の購入画面構造を調査する一時スクリプト。

execute_purchase.py の本番 form 投票ロジックを実装する前に、実際の購入画面の
input/select/button 構造を取り出して dump する。inspect_login_form.py の
購入画面版。

使い方:
  # 山陽 R5 の購入画面を調査 (place_code=6, race=5)
  python scripts/inspect_purchase_page.py 6 5

  # 車番も指定 (試行的に複勝 4 号を選択しようとする)
  python scripts/inspect_purchase_page.py 6 5 4

  # 日付指定
  python scripts/inspect_purchase_page.py 6 5 4 --date 2026-05-08

事前準備:
  - accounts.json に user_id / password / buy_secret_key 設定済
  - python -m playwright install chromium 済
  - vote.autorace.jp が現在開催中の R を引数指定すること (= 終了済 R には navigate しない)

出力:
  - data/purchase_form_dump.html  (HTML 全体)
  - data/purchase_form_dump.txt   (input/select/button/form 構造を整形)
  - 標準出力に summary
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "profiles" / "autorace"
DUMP_HTML = ROOT / "data" / "purchase_form_dump.html"
DUMP_TXT = ROOT / "data" / "purchase_form_dump.txt"

VEL_CODE_MAP = {2: "002", 3: "003", 4: "004", 5: "005", 6: "006"}
VENUE_JP_MAP = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}

# 試行する URL pattern (vote.autorace.jp 構造調査済 2026-05-09)
URL_PATTERNS = [
    # 実購入画面 (確認済): /vote?vel_code=NNN&race_num=N
    "https://vote.autorace.jp/vote?vel_code={vel_code}&race_num={race}",
    # フォールバック: 旧 race info ページ (購入は不可だが構造調査用)
    "https://vote.autorace.jp/race/{vel_code}/{date}/R{race}",
    "https://vote.autorace.jp/vote?velCode={vel_code}&date={date}&raceNum={race}",
]


async def inspect(place_code: int, race_no: int, car_no: int | None,
                  date: str) -> None:
    from playwright.async_api import async_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    DUMP_HTML.parent.mkdir(parents=True, exist_ok=True)

    vel_code = VEL_CODE_MAP[place_code]
    venue_jp = VENUE_JP_MAP[place_code]

    lines: list[str] = []

    def out(msg: str) -> None:
        print(msg)
        lines.append(msg)

    out(f"=== inspect_purchase_page ===")
    out(f"target: {venue_jp} (place_code={place_code}, vel_code={vel_code}) "
        f"R{race_no} 車{car_no or '?'} on {date}")
    out("")

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
            # === Step 1: ホーム到達 (ログイン判定はバグやすいので URL pattern 側で確認) ===
            await page.goto("https://vote.autorace.jp/", timeout=30000)
            await asyncio.sleep(2)
            current = page.url
            out(f"[1] HOME URL: {current}")
            # 補助情報: ログアウトボタンが見えていれば logged-in
            try:
                logout_btn = page.locator(
                    'a:has-text("ログアウト"), button:has-text("ログアウト")'
                ).first
                logged_in = (
                    await logout_btn.count() > 0
                    and await logout_btn.is_visible()
                )
                out(f"[1] ログアウトボタン検出 = {logged_in} "
                    f"({'logged-in と推定' if logged_in else 'unknown'})")
            except Exception:
                out("[1] ログイン状態判定スキップ")

            # === Step 2: URL pattern 試行 ===
            successful_url = None
            login_redirect_count = 0
            for pattern in URL_PATTERNS:
                url = pattern.format(
                    vel_code=vel_code, date=date, race=race_no,
                )
                out(f"\n[2] 試行: {url}")
                try:
                    response = await page.goto(url, timeout=15000,
                                               wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                    final_url = page.url
                    status = response.status if response else None
                    out(f"    → {final_url} (status={status})")
                    if status and status < 400 and "vote.autorace.jp" in final_url:
                        if "/login" in final_url:
                            out(f"    → ログイン画面に redirect されました (skip)")
                            login_redirect_count += 1
                            continue
                        successful_url = final_url
                        break
                except Exception as e:
                    out(f"    → 失敗: {e}")
                    continue

            if not successful_url:
                if login_redirect_count > 0:
                    out("\n[!] 全 URL pattern がログイン画面に redirect されました。")
                    out("    → scripts/auto_login_autorace.py を実行してから再試行してください。")
                else:
                    out("\n[!] 全 URL pattern が失敗。手動で vote.autorace.jp を開いて、")
                    out("    実際の購入画面 URL を URL_PATTERNS リストに追加してください。")
                # 最後にホームに戻して手動探索ヒント出す
                await page.goto("https://vote.autorace.jp/", timeout=15000)
                await asyncio.sleep(3)
                html = await page.content()
                DUMP_HTML.write_text(html, encoding="utf-8")
                out(f"\n[6] HTML 保存: {DUMP_HTML} (ホーム画面)")
                DUMP_TXT.write_text("\n".join(lines), encoding="utf-8")
                return

            out(f"\n[3] 購入画面到達: {successful_url}")
            await asyncio.sleep(3)

            # === Step 4: form / input / select / button を全 dump ===
            out("\n[4] form 要素:")
            forms = await page.locator("form").all()
            out(f"    form 数: {len(forms)}")
            for i, f in enumerate(forms):
                try:
                    info = await f.evaluate(
                        "(el) => ({action: el.action, method: el.method, "
                        "id: el.id, name: el.name, "
                        "fields: Array.from(el.elements).slice(0,20).map(e => "
                        "({tag: e.tagName, type: e.type, name: e.name, id: e.id}))})"
                    )
                    out(f"    [form {i}] action={info['action']} method={info['method']}")
                    for fld in info.get("fields", []):
                        out(f"        {fld}")
                except Exception as e:
                    out(f"    [form {i}] error: {e}")

            out("\n[5] input 要素 (画面全体):")
            inputs = await page.locator("input").all()
            out(f"    input 数: {len(inputs)}")
            for i, inp in enumerate(inputs[:50]):  # 最大 50 個
                try:
                    info = await inp.evaluate(
                        "(el) => ({name: el.name, id: el.id, type: el.type, "
                        "value: el.value, placeholder: el.placeholder, "
                        "label: el.getAttribute('aria-label'), "
                        "visible: el.offsetParent !== null})"
                    )
                    out(f"    [{i}] {info}")
                except Exception as e:
                    out(f"    [{i}] error: {e}")

            out("\n[6] select 要素:")
            selects = await page.locator("select").all()
            out(f"    select 数: {len(selects)}")
            for i, sel in enumerate(selects):
                try:
                    info = await sel.evaluate(
                        "(el) => ({name: el.name, id: el.id, "
                        "options: Array.from(el.options).slice(0,20).map(o => "
                        "({value: o.value, text: o.text}))})"
                    )
                    out(f"    [{i}] name={info['name']} id={info['id']}")
                    for opt in info.get("options", []):
                        out(f"        {opt}")
                except Exception as e:
                    out(f"    [{i}] error: {e}")

            out("\n[7] button 要素:")
            buttons = await page.locator("button").all()
            out(f"    button 数: {len(buttons)}")
            for i, btn in enumerate(buttons[:30]):
                try:
                    info = await btn.evaluate(
                        "(el) => ({type: el.type, id: el.id, "
                        "text: el.innerText.slice(0,30), "
                        "visible: el.offsetParent !== null})"
                    )
                    out(f"    [{i}] {info}")
                except Exception as e:
                    out(f"    [{i}] error: {e}")

            # === Step 8: HTML 保存 ===
            html = await page.content()
            DUMP_HTML.write_text(html, encoding="utf-8")
            out(f"\n[8] HTML 保存: {DUMP_HTML}")
            out(f"    最終 URL: {page.url}")

            # === Step 9: 試行 - 複勝タブ / 車番 click 等の selector hint ===
            out("\n[9] 想定 selector の存在チェック (hint):")
            for sel_desc, sel_query in [
                ("複勝 タブ", 'button:has-text("複勝"), a:has-text("複勝")'),
                ("車番 ボタン", 'button[data-car], button[data-num]'),
                ("金額入力", 'input[name*="amount"], input[name*="price"], input[name*="kingaku"]'),
                ("確認ボタン", 'button:has-text("確認"), button:has-text("投票")'),
            ]:
                try:
                    cnt = await page.locator(sel_query).count()
                    out(f"    {sel_desc}: '{sel_query}' → {cnt} 個ヒット")
                except Exception as e:
                    out(f"    {sel_desc}: error {e}")

            # === Step 10: 複勝タブ → 車番 click → 金額入力 までの探索 ===
            out("\n[10] 複勝タブ click を試行 (購入フォーム探索):")
            clicked_fukushou = False
            try:
                # 複勝タブの候補
                for sel in [
                    'button:has-text("複勝")',
                    'a:has-text("複勝")',
                    'li:has-text("複勝")',
                    '[role="tab"]:has-text("複勝")',
                    '[data-bet-type="fukushou"]',
                ]:
                    cnt = await page.locator(sel).count()
                    if cnt > 0:
                        out(f"    複勝候補発見: {sel} ({cnt} 個)")
                        try:
                            await page.locator(sel).first.click(timeout=5000)
                            clicked_fukushou = True
                            out(f"    → click OK")
                            break
                        except Exception as e:
                            out(f"    → click 失敗: {e}")
                if not clicked_fukushou:
                    out(f"    複勝タブ見つからず")
                else:
                    await asyncio.sleep(3)
                    after_url = page.url
                    out(f"    複勝 click 後 URL: {after_url}")

                    # フォーム再 dump
                    out("\n[11] (複勝 click 後) form 要素:")
                    forms2 = await page.locator("form").all()
                    out(f"    form 数: {len(forms2)}")
                    for i, f in enumerate(forms2):
                        try:
                            info = await f.evaluate(
                                "(el) => ({action: el.action, method: el.method, "
                                "id: el.id, name: el.name})"
                            )
                            out(f"    [form {i}] {info}")
                        except Exception as e:
                            out(f"    [form {i}] error: {e}")

                    out("\n[12] (複勝 click 後) input 要素:")
                    inputs2 = await page.locator("input").all()
                    out(f"    input 数: {len(inputs2)}")
                    for i, inp in enumerate(inputs2[:80]):
                        try:
                            info = await inp.evaluate(
                                "(el) => ({name: el.name, id: el.id, type: el.type, "
                                "value: el.value, placeholder: el.placeholder, "
                                "visible: el.offsetParent !== null})"
                            )
                            out(f"    [{i}] {info}")
                        except Exception as e:
                            out(f"    [{i}] error: {e}")

                    out("\n[13] (複勝 click 後) select 要素:")
                    selects2 = await page.locator("select").all()
                    out(f"    select 数: {len(selects2)}")
                    for i, sel in enumerate(selects2):
                        try:
                            info = await sel.evaluate(
                                "(el) => ({name: el.name, id: el.id, "
                                "options: Array.from(el.options).slice(0,15).map(o => "
                                "({value: o.value, text: o.text}))})"
                            )
                            out(f"    [{i}] name={info['name']} id={info['id']}")
                            for opt in info.get("options", []):
                                out(f"        {opt}")
                        except Exception as e:
                            out(f"    [{i}] error: {e}")

                    out("\n[14] (複勝 click 後) button 要素:")
                    buttons2 = await page.locator("button").all()
                    out(f"    button 数: {len(buttons2)}")
                    for i, btn in enumerate(buttons2[:40]):
                        try:
                            info = await btn.evaluate(
                                "(el) => ({type: el.type, id: el.id, "
                                "text: el.innerText.slice(0,40), "
                                "visible: el.offsetParent !== null})"
                            )
                            out(f"    [{i}] {info}")
                        except Exception as e:
                            out(f"    [{i}] error: {e}")

                    # 通常投票後 HTML も保存
                    html2 = await page.content()
                    DUMP_HTML2 = ROOT / "data" / "purchase_form_dump_after.html"
                    DUMP_HTML2.write_text(html2, encoding="utf-8")
                    out(f"\n[15] (複勝 click 後) HTML 保存: {DUMP_HTML2}")

                    # 複勝 / 車番 等の selector ヒット数チェック (再)
                    out("\n[16] (複勝 click 後) selector ヒント:")
                    for sel_desc, sel_query in [
                        ("複勝 (button)", 'button:has-text("複勝")'),
                        ("複勝 (a/li)", 'a:has-text("複勝"), li:has-text("複勝")'),
                        ("単勝", 'button:has-text("単勝"), a:has-text("単勝")'),
                        ("車番セルクス", 'button[data-car], td[data-car], div[data-car]'),
                        ("number input", 'input[type="number"]'),
                        ("text input", 'input[type="text"]'),
                        ("td 全般", 'td'),
                        ("確定/投票/購入", 'button:has-text("確定"), button:has-text("購入"), button:has-text("投票")'),
                    ]:
                        try:
                            cnt = await page.locator(sel_query).count()
                            out(f"    {sel_desc}: '{sel_query}' → {cnt} 個")
                        except Exception as e:
                            out(f"    {sel_desc}: error {e}")
            except Exception as e:
                out(f"    [10] 例外: {e}")

            # 5 秒待って閉じる (目視確認用)
            out("\n[done] 5 秒後にウインドウを閉じます ...")
            await asyncio.sleep(5)

        finally:
            try:
                await context.close()
            except Exception:
                pass

    DUMP_TXT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n=== summary ===")
    print(f"HTML: {DUMP_HTML}")
    print(f"TXT : {DUMP_TXT}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("place_code", type=int, choices=[2, 3, 4, 5, 6],
                   help="2=川口 3=伊勢崎 4=浜松 5=飯塚 6=山陽")
    p.add_argument("race_no", type=int, help="レース番号 (1-12)")
    p.add_argument("car_no", type=int, nargs="?", default=None,
                   help="車番 (省略可、navigate 確認のみなら不要)")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: 今日)")
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    date = args.date or dt.date.today().isoformat()
    asyncio.run(inspect(args.place_code, args.race_no, args.car_no, date))


if __name__ == "__main__":
    main()
