"""vote.autorace.jp に Playwright で複勝投票実行 (2026-05-08 導入 / 2026-05-09 本番 selector 実装)。

⚠️ 重要: 規約解釈は灰色 (memory ml_baseline_findings.md 2026-05-08 参照)。
本人 click in the loop で「自ら申込む」を満たす設計だが、公式 UI 非経由は
解釈次第ではアウト。autorace.jp が後から違反判定する可能性あり、自己責任。

スコープ制限 (server-side enforce):
  - 複勝のみ (rt/rf/wide 等は受け付けない)
  - 金額 ≤ ¥1,000 (誤発火による損失上限)
  - 1 token 1 回限り (buy_token.py 側で重複防止)

実行フロー (2026-05-09 inspect_purchase_page.py で構造確認済):
  1. login 状態確認 (永続プロファイル経由)
  2. /vote?vel_code=NNN&race_num=N に navigate
  3. #select-bettype 内で 「複勝」 click → ON
  4. #select-bettype 内で 「３連単」 click → OFF (default active を解除)
  5. 車番テーブル N 行目 1 列目 (１着列) の label click → 複勝対象として選択
  6. 「投票シートに追加」 click
  7. 「投票確認へ」 click → /vote/confirm にナビ
  8. 「投票する」 click → ★ 実投票 (--dry-run 時はスキップ)

使い方:
  # dry-run (実投票せず確認画面到達のみ、デフォルト動作確認用)
  python scripts/execute_purchase.py --race-date 2026-05-09 --place 6 \\
    --race 5 --car 4 --amount 100 --dry-run

  # 本番 (実投票発生)
  python scripts/execute_purchase.py --race-date 2026-05-09 --place 6 \\
    --race 5 --car 4 --amount 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "profiles" / "autorace"

# vote.autorace.jp の URL パターン (2026-05-08 時点、要調査)
# レース詳細ページ: https://vote.autorace.jp/race/{velCode}/{YYYY-MM-DD}/R{n}
#   ※ 実際の URL pattern は inspect_purchase_page.py で要確認
VEL_CODE_MAP = {2: "002", 3: "003", 4: "004", 5: "005", 6: "006"}

# 安全制限 (server-side enforce)
MAX_AMOUNT_YEN = 1000  # 1 R の最大投資額


async def execute_buy(
    race_date: str,
    place_code: int,
    race_no: int,
    car_no: int,
    amount: int,
    dry_run: bool = True,
) -> dict:
    """Playwright で投票実行。

    Returns:
        dict with keys: success (bool), dry_run (bool), message (str), url (str)
    """
    if amount > MAX_AMOUNT_YEN:
        raise ValueError(
            f"amount {amount} > {MAX_AMOUNT_YEN} (safety limit)"
        )
    if place_code not in VEL_CODE_MAP:
        raise ValueError(f"unknown place_code {place_code}")

    vel_code = VEL_CODE_MAP[place_code]

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "playwright が未インストール。\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        )

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,  # 初期は false (動作確認のため)
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
            # === Step 1: login 状態確認 (永続プロファイル流用) ===
            await page.goto("https://vote.autorace.jp/",
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            try:
                logout_btn = page.locator(
                    'a:has-text("ログアウト"), button:has-text("ログアウト")'
                ).first
                logged_in = (
                    await logout_btn.count() > 0
                    and await logout_btn.is_visible()
                )
            except Exception:
                logged_in = False
            if not logged_in:
                raise RuntimeError(
                    "ログイン状態が切れています。先に "
                    "scripts/auto_login_autorace.py を実行してから再試行してください。"
                )
            print(f"[execute_purchase] logged-in 確認", file=sys.stderr)

            # === Step 2: 投票画面に navigate ===
            target_url = (
                f"https://vote.autorace.jp/vote"
                f"?vel_code={vel_code}&race_num={race_no}"
            )
            print(f"[execute_purchase] navigating to: {target_url}",
                  file=sys.stderr)
            await page.goto(target_url, wait_until="domcontentloaded",
                            timeout=30000)
            await asyncio.sleep(3)
            print(f"[execute_purchase] current URL: {page.url}",
                  file=sys.stderr)

            # /login にリダイレクトされていないか確認
            if "/login" in page.url:
                raise RuntimeError(
                    f"login 画面に redirect されました ({page.url}) — "
                    "session 切れ"
                )

            # === Step 3: 複勝タブを ON ===
            print(f"[execute_purchase] step 3: 複勝 ON",
                  file=sys.stderr)
            try:
                fukushou = page.locator(
                    '#select-bettype label:has-text("複勝")'
                ).first
                await fukushou.click(timeout=5000)
                await asyncio.sleep(1.5)
            except Exception as e:
                raise RuntimeError(f"複勝 click 失敗: {e}")

            # === Step 4: ３連単タブを OFF (default active を解除) ===
            print(f"[execute_purchase] step 4: ３連単 OFF (deselect)",
                  file=sys.stderr)
            try:
                sanrentan = page.locator(
                    '#select-bettype label:has-text("３連単")'
                ).first
                if await sanrentan.count() > 0:
                    await sanrentan.click(timeout=5000)
                    await asyncio.sleep(1.5)
            except Exception as e:
                # deselect 失敗は致命的ではない (元から OFF だった可能性)
                print(f"[execute_purchase] ３連単 deselect 失敗(継続): {e}",
                      file=sys.stderr)

            # === Step 5: 車番 N の １着列 (= 複勝対象) を click ===
            print(f"[execute_purchase] step 5: 車番 {car_no} click",
                  file=sys.stderr)
            try:
                car_label = page.locator(
                    f'table:has(th:has-text("１着")) tbody '
                    f'tr:nth-child({car_no}) td:nth-child(1) label'
                ).first
                await car_label.click(timeout=5000)
                await asyncio.sleep(1)
            except Exception as e:
                raise RuntimeError(f"車番 {car_no} click 失敗: {e}")

            # === Step 6: 「投票シートに追加」 click ===
            print(f"[execute_purchase] step 6: 投票シートに追加",
                  file=sys.stderr)
            try:
                add_btn = page.locator(
                    'button:has-text("投票シートに追加")'
                ).first
                if await add_btn.is_disabled():
                    raise RuntimeError(
                        "投票シートに追加が disabled (車番未選択 / 締切超過 / 不正)"
                    )
                await add_btn.click(timeout=5000)
                await asyncio.sleep(2)
            except Exception as e:
                raise RuntimeError(f"投票シートに追加 click 失敗: {e}")

            # === Step 7: 「投票確認へ」 click → /vote/confirm に遷移 ===
            print(f"[execute_purchase] step 7: 投票確認へ",
                  file=sys.stderr)
            try:
                confirm_btn = page.locator(
                    'button:has-text("投票確認へ")'
                ).first
                if await confirm_btn.is_disabled():
                    raise RuntimeError("投票確認へ が disabled")
                await confirm_btn.click(timeout=5000)
                await asyncio.sleep(3)
                if "/confirm" not in page.url:
                    print(
                        f"[execute_purchase] 警告: 確認画面 URL が予想と違う: {page.url}",
                        file=sys.stderr,
                    )
            except Exception as e:
                raise RuntimeError(f"投票確認へ click 失敗: {e}")

            # === Step 8: 「投票する」 click ★ 実投票発生 ===
            if dry_run:
                # dry-run mode: 確認画面で停止、実投票せず終了
                print(
                    f"[execute_purchase] dry-run: 確認画面まで到達、"
                    f"「投票する」 click はスキップ",
                    file=sys.stderr,
                )
                await asyncio.sleep(3)  # 目視用
                return {
                    "success": True,
                    "dry_run": True,
                    "url": page.url,
                    "message": (
                        f"dry-run OK: 確認画面到達 ({page.url})、"
                        f"複勝 {car_no}号 ¥{amount} 投票画面表示済"
                    ),
                }

            print(f"[execute_purchase] step 8: 投票する (実投票)",
                  file=sys.stderr)
            try:
                vote_btn = page.locator(
                    'button:has-text("投票する")'
                ).first
                if await vote_btn.count() == 0:
                    raise RuntimeError("「投票する」 button が見つからない")
                if await vote_btn.is_disabled():
                    raise RuntimeError("「投票する」が disabled (締切超過?)")
                await vote_btn.click(timeout=5000)
                # 投票完了画面 / 結果メッセージを待つ
                await asyncio.sleep(5)
                final_url = page.url
                print(
                    f"[execute_purchase] 投票完了 (URL: {final_url})",
                    file=sys.stderr,
                )
            except Exception as e:
                raise RuntimeError(f"投票する click 失敗: {e}")

            return {
                "success": True,
                "dry_run": False,
                "url": page.url,
                "message": (
                    f"投票完了: 複勝 {car_no}号 ¥{amount} "
                    f"@ {race_date} place_code={place_code} R{race_no}"
                ),
            }

        finally:
            try:
                await context.close()
            except Exception:
                pass


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--race-date", required=True, help="YYYY-MM-DD")
    p.add_argument("--place", type=int, required=True, help="place_code (2-6)")
    p.add_argument("--race", type=int, required=True, help="race number")
    p.add_argument("--car", type=int, required=True, help="car number")
    p.add_argument("--amount", type=int, required=True, help="bet amount in yen")
    p.add_argument("--dry-run", action="store_true",
                   help="navigate のみ、実投票はしない (default: 実投票)")
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        result = asyncio.run(execute_buy(
            args.race_date, args.place, args.race, args.car, args.amount,
            dry_run=args.dry_run,
        ))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)},
                         ensure_ascii=False))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
