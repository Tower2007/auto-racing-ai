"""vote.autorace.jp に Playwright で複勝投票実行 (2026-05-08 導入)。

⚠️ 重要: 規約解釈は灰色 (memory ml_baseline_findings.md 2026-05-08 参照)。
本人 click in the loop で「自ら申込む」を満たす設計だが、公式 UI 非経由は
解釈次第ではアウト。autorace.jp が後から違反判定する可能性あり、自己責任。

スコープ制限 (server-side enforce):
  - 複勝のみ (rt/rf/wide 等は受け付けない)
  - 金額 ≤ ¥1,000 (誤発火による損失上限)
  - 1 token 1 回限り (buy_token.py 側で重複防止)

⚠️ 初期 release (2026-05-08): dry-run mode のみ動作確認済。
   実購入の selector / form 構造は **未調査**。dry-run で navigate のみ確認後、
   inspect_purchase_page.py で確認 → execute_purchase.py の TODO 部分を埋める
   → 慎重に手動テストしてから本番 enable。

使い方:
  python scripts/execute_purchase.py --race-date 2026-05-08 --place 6 \\
    --race 5 --car 4 --amount 100 --dry-run

  --dry-run なし = 実購入 (危険、初期は使わない)
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
            # === Step 1: ログイン状態確認 (auto_login_autorace と同じ機構) ===
            await page.goto("https://vote.autorace.jp/", timeout=30000)
            await asyncio.sleep(2)
            login_link = page.locator(
                'a:has-text("ログイン"), button:has-text("ログイン")'
            ).first
            need_login = (
                await login_link.count() > 0 and await login_link.is_visible()
            )
            if need_login:
                # 永続プロファイルが切れている場合、auto_login_autorace を呼んで
                # cookie を更新する手順が必要。ここは初期版では未実装。
                raise RuntimeError(
                    "ログイン状態が切れています。先に "
                    "scripts/auto_login_autorace.py を実行してから再試行してください。"
                )

            # === Step 2: 投票ページに遷移 ===
            # ⚠️ TODO: 実際の投票画面 URL pattern は要調査
            # 候補 1: /race/{velCode}/{YYYY-MM-DD}/R{n}/vote
            # 候補 2: /vote?velCode=...&date=...&race=...
            # → 初回は inspect_purchase_page.py を作って実際の URL を確認
            target_url = (
                f"https://vote.autorace.jp/race/{vel_code}/{race_date}/R{race_no}"
            )
            print(f"[execute_purchase] navigating to: {target_url}",
                  file=sys.stderr)
            await page.goto(target_url, timeout=30000)
            await asyncio.sleep(3)

            current_url = page.url
            print(f"[execute_purchase] current URL: {current_url}",
                  file=sys.stderr)

            # === Step 3: 複勝の入力 (要調査) ===
            # ⚠️ TODO: 以下は仮の selector。実際は要調査:
            #   - 券種選択タブ ("複勝")
            #   - 車番選択 (car_no)
            #   - 金額入力 (¥100)
            #   - 投票ボタン

            if dry_run:
                # dry-run mode: 実際の投票はせず、画面到達のみ確認
                print(
                    f"[execute_purchase] dry-run: would buy "
                    f"複勝 {car_no}号 ¥{amount} at {current_url}",
                    file=sys.stderr,
                )
                # 5 秒待ってから window 閉じる (目視確認用)
                await asyncio.sleep(5)
                return {
                    "success": True,
                    "dry_run": True,
                    "url": current_url,
                    "message": (
                        f"dry-run OK: navigated to {current_url}, "
                        f"would buy 複勝 {car_no}号 ¥{amount}"
                    ),
                }

            # === Step 4: 実購入 (未実装) ===
            raise NotImplementedError(
                "実購入ロジック (form 入力・確認モーダル・投票実行) は未実装。\n"
                "scripts/inspect_purchase_page.py で vote.autorace.jp の購入画面構造を\n"
                "調査してから execute_purchase.py の TODO 部分を実装してください。"
            )

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
