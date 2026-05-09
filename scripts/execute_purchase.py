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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "profiles" / "autorace"

# vote.autorace.jp の URL パターン (2026-05-08 時点、要調査)
# レース詳細ページ: https://vote.autorace.jp/race/{velCode}/{YYYY-MM-DD}/R{n}
#   ※ 実際の URL pattern は inspect_purchase_page.py で要確認
VEL_CODE_MAP = {2: "002", 3: "003", 4: "004", 5: "005", 6: "006"}
VENUE_JP_MAP = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}

# 安全制限 (server-side enforce)
MAX_AMOUNT_YEN = 1000  # 1 R の最大投資額

# 投票完了 / 失敗判定キーワード (Step 8.5 で使用)
# 2026-05-09 本番テストで「締切13:02」の「締切」が failure 誤検知された。
# vote.autorace.jp はレース情報として「締切」「残高」を通常表示するため、
# **完了画面でしか出ない強いフレーズ** に限定する。
SUCCESS_KEYWORDS = (
    "投票受付完了",                        # 実画面で確認済 (2026-05-09)
    "ご投票ありがとうございました",        # 実画面で確認済
    "投票を受け付けました",
    "投票が完了しました",
    "投票完了",
    "受付番号",
)
FAILURE_KEYWORDS = (
    # 強い失敗フレーズのみ (単独単語の "締切"/"エラー" は除外)
    "投票できません",
    "購入できません",
    "投票を受け付けることができません",
    "投票を受け付けできません",
    "ポイントが不足",
    "残高が不足しています",
    "認証が必要",
    "ログインが必要",
    "Traceback",
    "エラーが発生しました",
)


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

            # === Step 1.5: カートクリア (前回 dry-run 残骸対策) ===
            print(f"[execute_purchase] step 1.5: 既存カートクリア",
                  file=sys.stderr)
            try:
                # confirm 画面の dialog (確認 prompt 等) は自動 accept
                page.on(
                    'dialog',
                    lambda d: asyncio.create_task(d.accept()),
                )
                confirm_url = (
                    f"https://vote.autorace.jp/vote/confirm"
                    f"?vel_code={vel_code}&race_num={race_no}"
                )
                await page.goto(confirm_url, wait_until="domcontentloaded",
                                timeout=30000)
                await asyncio.sleep(2)
                # /login にリダイレクトされてないか確認
                if "/login" in page.url:
                    raise RuntimeError(
                        f"login 画面に redirect されました ({page.url}) — "
                        "session 切れ"
                    )
                # 「全削除」 button が visible なら click(カート空でない)
                delete_btn = page.locator(
                    'button:has-text("全削除")'
                ).first
                if await delete_btn.count() > 0:
                    try:
                        await delete_btn.click(timeout=5000)
                        await asyncio.sleep(2)
                        print(f"[execute_purchase] カートクリア OK",
                              file=sys.stderr)
                    except Exception as e:
                        print(
                            f"[execute_purchase] 全削除 click 失敗(継続): {e}",
                            file=sys.stderr,
                        )
                else:
                    print(f"[execute_purchase] カートは元々空", file=sys.stderr)
            except RuntimeError:
                raise
            except Exception as e:
                print(f"[execute_purchase] カートクリアスキップ: {e}",
                      file=sys.stderr)

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

            # === Step 5.5: 金額入力を amount/100 に明示 set ===
            # 「各 [N] 00円」の N を amount//100 に。
            # default は 1 (=¥100) だが前回テストで 2 等になってる可能性
            unit_value = str(amount // 100)
            print(
                f"[execute_purchase] step 5.5: 金額 input を '{unit_value}' "
                f"(={amount}円) に reset",
                file=sys.stderr,
            )
            amount_set_ok = False
            for sel in [
                'input[type="text"]:visible',
                'input[type="number"]:visible',
            ]:
                try:
                    amount_input = page.locator(sel).first
                    if await amount_input.count() > 0:
                        await amount_input.fill(unit_value, timeout=3000)
                        await asyncio.sleep(0.5)
                        amount_set_ok = True
                        break
                except Exception as e:
                    print(
                        f"[execute_purchase] 金額 set ({sel}) 失敗: {e}",
                        file=sys.stderr,
                    )
            if not amount_set_ok:
                print(
                    f"[execute_purchase] 警告: 金額 input が見つからず "
                    f"default 値で進む",
                    file=sys.stderr,
                )

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

            # === Step 7.5: 確認画面の件数 + 金額チェック ===
            # 期待: 投票数 1組 1票 / 合計購入額 = amount 円
            print(
                f"[execute_purchase] step 7.5: 確認画面の件数 / 金額 check",
                file=sys.stderr,
            )
            # screenshot 保存 (debug 用)
            try:
                shot_path = (
                    ROOT / "data"
                    / f"execute_step_7_confirm_{int(time.time())}.png"
                )
                await page.screenshot(path=str(shot_path), full_page=True)
                print(f"[execute_purchase] screenshot: {shot_path}",
                      file=sys.stderr)
            except Exception as e:
                print(f"[execute_purchase] screenshot 失敗(継続): {e}",
                      file=sys.stderr)

            try:
                page_text = await page.evaluate(
                    "() => document.body.innerText"
                )
            except Exception as e:
                page_text = ""
                print(f"[execute_purchase] body.innerText 取得失敗: {e}",
                      file=sys.stderr)

            # Codex 2 次 review: dry-run 用に body.innerText を保存
            # (regex の妥当性検証や画面表記の変化追跡に使う)
            try:
                txt_path = (
                    ROOT / "data"
                    / f"execute_step_7_confirm_{int(time.time())}.txt"
                )
                txt_path.write_text(page_text, encoding="utf-8")
                print(
                    f"[execute_purchase] body.innerText: {txt_path}",
                    file=sys.stderr,
                )
            except Exception as e:
                print(f"[execute_purchase] body.innerText 保存失敗: {e}",
                      file=sys.stderr)

            # P1 hardening: 構造的検証 (券種 / 場 / R / 車番 / 件数 / 金額 / 日付)
            import re as _re
            venue_jp = VENUE_JP_MAP.get(place_code, "?")

            # 確認画面の date 照合は外す (Codex 4 次 review):
            # 既存 PNG では確認画面に日付が可視テキストに出ていない可能性大。
            # body.innerText に hidden 的に入っているかは次回 dry-run の
            # data/execute_step_7_confirm_<ts>.txt で確認するまで保留。
            # 主防御は: TTL 30 分 + yesterday 5 時まで + 場/R/券種/車番/金額。

            checks = [
                (
                    r"複勝",
                    "券種が複勝でない (確認画面に '複勝' が無い)",
                ),
                (
                    rf"{_re.escape(venue_jp)}\s*{race_no}\s*R",
                    f"場/R 不一致 (期待: {venue_jp} {race_no}R)",
                ),
                # 「複勝 1組」直後に車番が表示される
                (
                    rf"複勝[\s\S]{{0,30}}\b{car_no}\b",
                    f"車番 {car_no} の表示確認失敗",
                ),
                (
                    r"投票数\s*\n?\s*1組",
                    "投票数が 1組 でない",
                ),
                (
                    r"1票",
                    "1票 表記なし (件数異常)",
                ),
                (
                    rf"合計購入額\s*\n?\s*{amount}\s*円",
                    f"合計購入額が {amount}円 でない",
                ),
            ]

            failures: list[str] = []
            for pattern, errmsg in checks:
                if not _re.search(pattern, page_text):
                    failures.append(f"  - {errmsg} (pattern: {pattern})")

            if failures:
                raise RuntimeError(
                    "確認画面検証失敗:\n"
                    + "\n".join(failures)
                    + f"\n  body text 抜粋:\n{page_text[:400]!r}"
                )

            print(
                f"[execute_purchase] 確認画面 OK: 複勝 / {race_date} / "
                f"{venue_jp} {race_no}R / {car_no}号 / 1組1票 / {amount}円 "
                f"を全て確認",
                file=sys.stderr,
            )

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

            # Codex 4 次 review: click 直前の時刻を JST aware で記録、
            # 履歴 exact match の filter 基準にする
            import datetime as _dt
            _jst = _dt.timezone(_dt.timedelta(hours=9))
            click_started_at = _dt.datetime.now(_jst)
            print(
                f"[execute_purchase] click_started_at: {click_started_at.isoformat()}",
                file=sys.stderr,
            )

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
            except Exception as e:
                raise RuntimeError(f"投票する click 失敗: {e}")

            # === Step 8.5: 投票完了 / 失敗の判定 (P2 hardening) ===
            print(
                f"[execute_purchase] step 8.5: 投票完了 / 失敗の判定",
                file=sys.stderr,
            )
            final_url = page.url
            try:
                completion_text = await page.evaluate(
                    "() => document.body.innerText"
                )
            except Exception:
                completion_text = ""

            # 完了 screenshot 保存
            try:
                done_shot = (
                    ROOT / "data"
                    / f"execute_step_8_done_{int(time.time())}.png"
                )
                await page.screenshot(path=str(done_shot), full_page=True)
                print(
                    f"[execute_purchase] done screenshot: {done_shot}",
                    file=sys.stderr,
                )
            except Exception:
                pass

            # 失敗キーワード検出 (優先)
            failure_hits = [
                kw for kw in FAILURE_KEYWORDS if kw in completion_text
            ]
            success_hits = [
                kw for kw in SUCCESS_KEYWORDS if kw in completion_text
            ]
            url_changed_to_done = (
                "/done" in final_url or "/complete" in final_url
                or "result" in final_url
            )

            # 失敗キーワードがあれば即 fail
            if failure_hits:
                raise RuntimeError(
                    f"投票失敗検出: {failure_hits} (URL: {final_url})\n"
                    f"  body text 抜粋: {completion_text[:400]!r}"
                )

            # === Step 8.6: GraphQL API exact match (Codex 4 次 review) ===
            # 旧: /mypage/order を navigate して body text regex 検索
            #     → 古い bet / 散在マッチで false-positive リスク
            # 新: 既存の fetch_order_history.py の GraphQL API を流用、
            #     click_started_at 以後の bet を fetch → 完全一致を確認
            history_ok = False
            history_match_info = ""
            try:
                # Playwright session の cookie を抽出 (autorace.jp domain)
                cookies = await context.cookies()
                cookie_pairs = [
                    f"{c['name']}={c['value']}"
                    for c in cookies
                    if "autorace.jp" in c.get("domain", "")
                ]
                cookie_str = "; ".join(cookie_pairs)

                # fetch_order_history の関数を import
                sys.path.insert(0, str(ROOT / "scripts"))
                from fetch_order_history import fetch_orders  # noqa

                # vel_code は 3 桁 string ("002" 等)
                vel_str = f"{place_code:03d}"

                print(
                    f"[execute_purchase] step 8.6: GraphQL fetch_orders "
                    f"vel={vel_str} race={race_no} day={race_date}",
                    file=sys.stderr,
                )
                orders = await asyncio.to_thread(
                    fetch_orders, cookie_str, race_date, vel_str, race_no
                )
                print(
                    f"[execute_purchase] orders 取得: {len(orders)} 件",
                    file=sys.stderr,
                )

                # exact match を探す:
                #   - createdAt が click_started_at - 30秒 (grace) 以後
                #   - packs に betType=FUKUSHOU かつ packDeme=str(car_no)
                #     かつ voteAmount=amount のものがある
                #
                # Codex 5 次 review (P2): grace 30秒を入れることで、PC 時計
                # ↔ サーバ時計のズレ / 秒丸め / 注文生成時刻が click 直前扱い
                # で取り逃すケースを防ぐ。false-negative (実購入後に失敗扱い)
                # は運用上紛らわしいため明示的に許容。
                CLICK_GRACE = _dt.timedelta(seconds=30)
                lower_bound = click_started_at - CLICK_GRACE
                matches = []
                for o in orders:
                    created_str = o.get("createdAt", "")
                    try:
                        # ISO8601 想定: "2026-05-09T11:39:41+09:00" or
                        # "...Z" or naive
                        s = created_str.replace("Z", "+00:00")
                        created = _dt.datetime.fromisoformat(s)
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=_jst)
                    except Exception:
                        # createdAt parse 失敗は skip (false-positive 防止)
                        continue
                    if created < lower_bound:
                        continue
                    for p in o.get("packs", []):
                        bet_type = str(p.get("betType", "")).strip()
                        deme = str(p.get("packDeme", "")).strip()
                        vote_amt = int(p.get("voteAmount", 0))
                        if (
                            bet_type == "FUKUSHOU"
                            and deme == str(car_no)
                            and vote_amt == amount
                        ):
                            matches.append({
                                "order_id": o.get("id"),
                                "created_at": created.isoformat(),
                                "betType": bet_type,
                                "packDeme": deme,
                                "voteAmount": vote_amt,
                            })
                            break

                if matches:
                    history_ok = True
                    history_match_info = (
                        f"{len(matches)} 件 exact match: "
                        f"{matches[0]}"
                    )
                    print(
                        f"[execute_purchase] ✅ GraphQL exact match: "
                        f"{history_match_info}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[execute_purchase] ⚠️ GraphQL exact match なし "
                        f"(orders={len(orders)} 件、click 以降 / 複勝 "
                        f"{car_no}号 / ¥{amount} に該当せず)",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(
                    f"[execute_purchase] GraphQL exact match 失敗(継続): {e}",
                    file=sys.stderr,
                )

            # 成功根拠の総合判定 (Codex 4 次 review):
            # (a) failure_hits: 即 fail (既に上で raise されている)
            # (b) history_ok: GraphQL exact match (server-side 成立確定)
            # (c) success keyword: fallback (受付番号付き完了画面)
            # (d) URL 遷移: ログ用、成功根拠にしない
            if not history_ok and not success_hits:
                raise RuntimeError(
                    f"投票完了の確認が取れない "
                    f"(GraphQL exact match NG / success keyword なし)。"
                    f"\n  URL: {final_url} (URL は補強情報のみ、単独成立不可)"
                    f"\n  body text 抜粋: {completion_text[:400]!r}"
                )

            print(
                f"[execute_purchase] ✅ 投票完了確認: "
                f"history_ok={history_ok} keywords={success_hits} "
                f"url_evidence={url_changed_to_done} url={final_url}",
                file=sys.stderr,
            )

            return {
                "success": True,
                "dry_run": False,
                "url": final_url,
                "success_evidence": {
                    "history_ok": history_ok,
                    "history_match_info": history_match_info,
                    "keywords": success_hits,
                    "url_changed": url_changed_to_done,
                },
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

    # === P3 hardening: amount strict == 100 (Codex 3 次 review) ===
    # Phase A の本番入口は amount == 100 固定。
    # 将来の金額拡張は別フェーズで MAX_AMOUNT_YEN を明示的に変更してから解除。
    if args.amount != 100:
        print(
            f"[execute_purchase] ❌ amount {args.amount} 不可 (Phase A は 100 固定)",
            file=sys.stderr,
        )
        sys.exit(2)

    # === P1 hardening: race_date が「現在開催中の日付」 ===
    # Codex 4 次 review: yesterday 窓を 0:00-05:00 に拡大
    import datetime as _dt
    _jst = _dt.timezone(_dt.timedelta(hours=9))
    _now = _dt.datetime.now(_jst)
    _today = _now.date().isoformat()
    _yesterday = (_now.date() - _dt.timedelta(days=1)).isoformat()
    _allowed = {_today}
    if _now.hour < 5:  # 0-04:59 JST
        _allowed.add(_yesterday)
    if args.race_date not in _allowed:
        print(
            f"[execute_purchase] ❌ race_date={args.race_date} 不可 "
            f"(allowed: {sorted(_allowed)} JST {_now.strftime('%H:%M')})。abort",
            file=sys.stderr,
        )
        sys.exit(2)

    # place_code / race_no / car_no の値域もここで弾く
    if args.place not in (2, 3, 4, 5, 6):
        print(
            f"[execute_purchase] ❌ place_code {args.place} 不正 (2-6 期待)",
            file=sys.stderr,
        )
        sys.exit(2)
    if not (1 <= args.race <= 12):
        print(
            f"[execute_purchase] ❌ race_no {args.race} 不正 (1-12 期待)",
            file=sys.stderr,
        )
        sys.exit(2)
    if not (1 <= args.car <= 8):
        print(
            f"[execute_purchase] ❌ car_no {args.car} 不正 (1-8 期待)",
            file=sys.stderr,
        )
        sys.exit(2)

    # 注意: 本番モード (`--dry-run` なし) は実投票を発生させる。
    # P1-P3 hardening 完了 (Codex 1-5 次 review、commit e06a200..c100df1)。
    # 詳細: Opinion/codex_briefs/execute_purchase_hardening.md
    # 1 回限りの本番テストはユーザー立ち会いで `--amount 100` のみ。

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
