"""vote.autorace.jp に Playwright で複勝投票実行 (2026-05-08 導入 / 2026-05-09 本番 selector 実装)。

⚠️ 重要: 規約解釈は灰色 (memory ml_baseline_findings.md 2026-05-08 参照)。
本人 click in the loop で「自ら申込む」を満たす設計だが、公式 UI 非経由は
解釈次第ではアウト。autorace.jp が後から違反判定する可能性あり、自己責任。

スコープ制限 (server-side enforce):
  - 券種は 複勝(fns) / 三連単(rt3) / 三連複(rf3) のみ
    (rt3/rf3 は浜松・山陽の EV>=1.80 まとめ買い専用、2026-05-30 追加)
  - 1 券種 ≤ ¥1,000、1 R 合計 ≤ ¥1,500 (誤発火による損失上限)
  - 1 token 1 回限り (buy_token.py 側で重複防止)

まとめ買い (2026-05-30):
  --bets-json '[{"type":"fns","cars":[6],"amount":300},
                {"type":"rt3","cars":[6,5,4],"amount":100},
                {"type":"rf3","cars":[4,5,6],"amount":100}]'
  各券種を順に投票シートへ追加 → 1 回で確認・投票。
  三連単=1着/2着/3着列、三連複=BOX列に 3 車。

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
MAX_AMOUNT_YEN = 1000   # 1 券種あたりの最大投資額
MAX_TOTAL_YEN = 1500    # 1 R の全券種合計上限 (複勝¥1000 + rt3¥100 + rf3¥100 余裕)

# 券種メタ (2026-05-30 三連系まとめ買い対応、inspect_purchase_page 調査で確定)
#   tab_label: #select-bettype 内の券種ラベル (全角)
#   n_cars   : 必要な車番数
#   columns  : iv__table 内で click する td:nth-child 番号のリスト
#              三連単 = 1着(1)/2着(2)/3着(3) を着順で、三連複 = BOX(4) に 3 車、
#              複勝 = 1着(1) に 1 車
#   deme_sep : GraphQL packDeme の区切り (検証用)。fns="6" rt3="6-5-4" rf3="4=5=6"
#   gql_type : GraphQL betType enum (検証用)
BET_META = {
    "fns": {"tab_label": "複勝",   "n_cars": 1, "columns": [1],
            "deme_sep": "",  "gql_type": "FUKUSHOU"},
    "rt3": {"tab_label": "３連単", "n_cars": 3, "columns": [1, 2, 3],
            "deme_sep": "-", "gql_type": "SANRENTAN"},
    "rf3": {"tab_label": "３連複", "n_cars": 3, "columns": [4, 4, 4],
            "deme_sep": "=", "gql_type": "SANRENFUKU"},
}
# #select-bettype の全券種ラベル (active 正規化で「対象以外を OFF」にするため)
ALL_BET_TAB_LABELS = ["３連単", "３連複", "２連単", "２連複", "ワイド", "単勝", "複勝"]


def _deme_str(bet_type: str, cars: list) -> str:
    """券種別の packDeme 表記。fns='6' / rt3='6-5-4' / rf3='4=5=6'(昇順)。"""
    sep = BET_META[bet_type]["deme_sep"]
    if bet_type == "rf3":
        cars = sorted(int(c) for c in cars)  # 三連複は順不同 → 昇順正規化
    return sep.join(str(int(c)) for c in cars)


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


def _run_auto_login() -> str:
    """auto_login_autorace.py を subprocess で実行し、結果を返す。

    セッション切れ時の自動復旧用 (2026-05-26 導入)。
    execute_buy() 内から asyncio.to_thread() 経由で呼ばれる。
    """
    import subprocess as _sp
    result = _sp.run(
        [sys.executable, str(ROOT / "scripts" / "auto_login_autorace.py")],
        cwd=str(ROOT),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"auto_login exit={result.returncode}: "
            f"{result.stderr[-300:]}"
        )
    return result.stdout[-200:].strip()


async def _launch_context(pw):
    """Playwright の persistent context を起動して (context, page) を返す。"""
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
    return context, page


# ─── 三連系まとめ買い用 helper (2026-05-30) ──────────────────────

async def _bettype_active_map(page) -> dict:
    """#select-bettype 内の各券種ラベルが active(赤) か判定。

    active タブは背景が赤系 (r>150, g<110, b<110)。computed style で判定。
    戻り値: {"複勝": True, "３連単": False, ...}
    """
    return await page.evaluate(
        """() => {
            const res = {};
            const root = document.querySelector('#select-bettype');
            if (!root) return res;
            for (const lab of root.querySelectorAll('label')) {
                const t = lab.innerText.trim();
                if (!t || t.length > 4) continue;
                const bg = getComputedStyle(lab).backgroundColor || '';
                const m = bg.match(/(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)/);
                let active = false;
                if (m) {
                    const r = +m[1], g = +m[2], b = +m[3];
                    active = (r > 150 && g < 110 && b < 110);
                }
                res[t] = active;
            }
            return res;
        }"""
    )


async def _normalize_bettype(page, target_label: str) -> bool:
    """#select-bettype を「target のみ active」に正規化。

    複数同時 active 設計のため、対象以外の active を OFF にし、対象を ON にする。
    最大 3 回試行。成功で True。
    """
    for _ in range(3):
        amap = await _bettype_active_map(page)
        # 対象以外の active を OFF
        for lab, active in list(amap.items()):
            if lab in ALL_BET_TAB_LABELS and active and lab != target_label:
                try:
                    await page.locator(
                        f'#select-bettype label:has-text("{lab}")'
                    ).first.click(timeout=4000)
                    await asyncio.sleep(0.7)
                except Exception:
                    pass
        # 対象を ON
        amap = await _bettype_active_map(page)
        if not amap.get(target_label, False):
            try:
                await page.locator(
                    f'#select-bettype label:has-text("{target_label}")'
                ).first.click(timeout=4000)
                await asyncio.sleep(0.7)
            except Exception:
                pass
        # 検証: target のみ active か
        amap = await _bettype_active_map(page)
        active_now = [l for l, a in amap.items()
                      if a and l in ALL_BET_TAB_LABELS]
        if active_now == [target_label]:
            return True
    return False


async def _select_cars(page, bet_type: str, cars: list) -> None:
    """iv__table (1着/2着/3着/BOX) で券種別に車番チェックボックスを click。

    fns: 1着列に 1 車。rt3: 1着=cars[0]/2着=cars[1]/3着=cars[2]。
    rf3: BOX列(td4) に 3 車。
    """
    columns = BET_META[bet_type]["columns"]
    for car, col in zip(cars, columns):
        sel = (
            f'table:has(th:has-text("BOX")) tbody '
            f'tr:nth-child({int(car)}) td:nth-child({col}) label'
        )
        loc = page.locator(sel).first
        if await loc.count() == 0:
            raise RuntimeError(
                f"車番 {car} col{col} の checkbox が見つからない ({bet_type})"
            )
        await loc.click(timeout=5000)
        await asyncio.sleep(0.6)


async def _set_amount_units(page, amount: int) -> bool:
    """口数入力 (可視 text input) を amount//100 に set。"""
    unit_value = str(amount // 100)
    for sel in ['input[type="text"]:visible', 'input[type="number"]:visible']:
        try:
            inp = page.locator(sel).first
            if await inp.count() > 0:
                await inp.fill(unit_value, timeout=3000)
                await asyncio.sleep(0.4)
                return True
        except Exception:
            continue
    return False


async def _add_to_sheet(page) -> None:
    """「投票シートに追加」 click (disabled なら error)。"""
    btn = page.locator('button:has-text("投票シートに追加")').first
    if await btn.count() == 0:
        raise RuntimeError("「投票シートに追加」 button が見つからない")
    if await btn.is_disabled():
        raise RuntimeError(
            "「投票シートに追加」が disabled (車番未選択 / 締切 / 不正)"
        )
    await btn.click(timeout=5000)
    await asyncio.sleep(2)


async def execute_buy(
    race_date: str,
    place_code: int,
    race_no: int,
    car_no: int | None = None,
    amount: int | None = None,
    dry_run: bool = True,
    bets: list | None = None,
) -> dict:
    """Playwright で投票実行。

    Args:
        bets: [{"type":"fns"|"rt3"|"rf3", "cars":[..], "amount":int}, ...]
              None なら car_no/amount から単一複勝 bet を構築 (後方互換)。

    Returns:
        dict with keys: success (bool), dry_run (bool), message (str), url (str)
    """
    # bets 構築 (後方互換: 単一複勝)
    if bets is None:
        if car_no is None or amount is None:
            raise ValueError("bets も car_no/amount も無い")
        bets = [{"type": "fns", "cars": [int(car_no)], "amount": int(amount)}]

    # 値域・合計チェック
    total = 0
    for b in bets:
        if b["type"] not in BET_META:
            raise ValueError(f"未知の券種: {b['type']}")
        if int(b["amount"]) > MAX_AMOUNT_YEN:
            raise ValueError(
                f"{b['type']} amount {b['amount']} > {MAX_AMOUNT_YEN} (safety)"
            )
        if len(b["cars"]) != BET_META[b["type"]]["n_cars"]:
            raise ValueError(
                f"{b['type']} cars 数不正: {b['cars']}"
            )
        total += int(b["amount"])
    if total > MAX_TOTAL_YEN:
        raise ValueError(f"合計 {total} > {MAX_TOTAL_YEN} (safety limit)")
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
        context, page = await _launch_context(pw)

        try:
            # === Step 1: login 状態確認 (保護ページでリダイレクト検査) ===
            # 実際の投票ページに navigate して /login リダイレクトを検査
            # → セッション有効性を確実に判定 (2026-05-26 導入)
            check_url = (
                f"https://vote.autorace.jp/vote"
                f"?vel_code={vel_code}&race_num={race_no}"
            )
            await page.goto(check_url,
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            if "/login" in page.url:
                # セッション切れ → context を閉じてから auto_login 実行
                # (同じ profile dir を使うため、ロック競合を回避)
                print(
                    f"[execute_purchase] session 切れ検出 ({page.url}), "
                    f"context 閉じて auto_login で再ログイン試行",
                    file=sys.stderr,
                )
                try:
                    await context.close()
                except Exception:
                    pass

                try:
                    login_result = await asyncio.to_thread(
                        _run_auto_login
                    )
                    print(
                        f"[execute_purchase] auto_login 結果: {login_result}",
                        file=sys.stderr,
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"auto_login 自動再ログイン失敗: {e}。"
                        "手動で scripts/auto_login_autorace.py を実行してください。"
                    )

                # 再ログイン後、context を再起動して投票ページに再アクセス
                context, page = await _launch_context(pw)
                await page.goto(check_url,
                                wait_until="domcontentloaded",
                                timeout=30000)
                await asyncio.sleep(2)
                if "/login" in page.url:
                    raise RuntimeError(
                        "auto_login 実行後もセッション切れ。"
                        "手動で scripts/auto_login_autorace.py を確認してください。"
                    )
            print(f"[execute_purchase] logged-in 確認 (投票ページ直接検査)",
                  file=sys.stderr)

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

            # === Step 3-6: 各券種を順にシートへ追加 ===
            # 券種ごとに: 券種正規化(target のみ active) → 車番 click →
            #             口数 set → 投票シートに追加
            for bi, b in enumerate(bets):
                bt = b["type"]
                cars = [int(c) for c in b["cars"]]
                amt = int(b["amount"])
                tab = BET_META[bt]["tab_label"]
                print(
                    f"[execute_purchase] step 3-6[{bi}]: {tab} "
                    f"cars={cars} ¥{amt}",
                    file=sys.stderr,
                )
                # 3: 券種を target のみ active に正規化
                if not await _normalize_bettype(page, tab):
                    raise RuntimeError(
                        f"券種正規化失敗 ({tab} のみ active にできない)"
                    )
                # 4: 車番 click (列は券種別: fns=1着 / rt3=1着2着3着 / rf3=BOX)
                try:
                    await _select_cars(page, bt, cars)
                except Exception as e:
                    raise RuntimeError(f"{tab} 車番選択失敗: {e}")
                # 5: 口数 set (amount//100)
                if not await _set_amount_units(page, amt):
                    print(
                        f"[execute_purchase] 警告: {tab} 金額 input 見つからず "
                        f"default で進む",
                        file=sys.stderr,
                    )
                # 6: 投票シートに追加
                try:
                    await _add_to_sheet(page)
                    print(f"[execute_purchase]   → {tab} シート追加 OK",
                          file=sys.stderr)
                except Exception as e:
                    raise RuntimeError(f"{tab} 投票シートに追加 失敗: {e}")

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

            # P1 hardening: 構造的検証 (券種 / 場 / R / 件数 / 合計金額)
            import re as _re
            venue_jp = VENUE_JP_MAP.get(place_code, "?")
            n_bets = len(bets)
            total_yen = sum(int(b["amount"]) for b in bets)

            # 確認画面の date 照合は外す (Codex 4 次 review、主防御は
            # TTL 30 分 + yesterday 5 時まで + 場/R/券種/件数/金額)。

            checks = [
                (
                    rf"{_re.escape(venue_jp)}\s*{race_no}\s*R",
                    f"場/R 不一致 (期待: {venue_jp} {race_no}R)",
                ),
                (
                    rf"投票数\s*\n?\s*{n_bets}\s*組",
                    f"投票数が {n_bets}組 でない",
                ),
                (
                    rf"合計購入額\s*\n?\s*{total_yen}\s*円",
                    f"合計購入額が {total_yen}円 でない",
                ),
            ]
            # 各券種の券種ラベルが確認画面に出ているか
            _CONFIRM_LABEL = {"fns": "複勝", "rt3": "３連単", "rf3": "３連複"}
            for b in bets:
                lbl = _CONFIRM_LABEL[b["type"]]
                checks.append((
                    _re.escape(lbl),
                    f"券種 {lbl} が確認画面に無い",
                ))
                # 出目の各車番が確認画面テキストに含まれるか (緩め: 桁の存在のみ)
                for c in b["cars"]:
                    checks.append((
                        rf"\b{int(c)}\b",
                        f"{lbl} の車番 {int(c)} 表示確認失敗",
                    ))

            failures: list[str] = []
            for pattern, errmsg in checks:
                if not _re.search(pattern, page_text):
                    failures.append(f"  - {errmsg} (pattern: {pattern})")

            if failures:
                raise RuntimeError(
                    "確認画面検証失敗:\n"
                    + "\n".join(failures)
                    + f"\n  body text 抜粋:\n{page_text[:600]!r}"
                )

            bets_desc = " / ".join(
                f"{_CONFIRM_LABEL[b['type']]} {_deme_str(b['type'], b['cars'])}"
                f" ¥{b['amount']}" for b in bets
            )
            print(
                f"[execute_purchase] 確認画面 OK: {race_date} / "
                f"{venue_jp} {race_no}R / {n_bets}組 / 合計¥{total_yen} / "
                f"{bets_desc} を全て確認",
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
                        f"dry-run OK: 確認画面到達、{n_bets}組 合計¥{total_yen} "
                        f"({bets_desc}) 表示済"
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

                # exact match を探す (全 bet について):
                #   - createdAt が click_started_at - 30秒 (grace) 以後
                #   - packs に betType=gql_type かつ packDeme=期待出目
                #     かつ voteAmount=amount のものがある
                # 全 bet が match して初めて history_ok=True。
                #
                # Codex 5 次 review (P2): grace 30秒で PC 時計↔サーバ時計のズレ
                # / 秒丸め / 注文生成時刻ズレによる取り逃しを防ぐ。
                CLICK_GRACE = _dt.timedelta(seconds=30)
                lower_bound = click_started_at - CLICK_GRACE

                # 期待 bet の (gql_type, deme, amount) 集合
                expected = []
                for b in bets:
                    expected.append({
                        "gql_type": BET_META[b["type"]]["gql_type"],
                        "deme": _deme_str(b["type"], b["cars"]),
                        "amount": int(b["amount"]),
                        "type": b["type"],
                        "matched": False,
                    })

                matches = []
                for o in orders:
                    created_str = o.get("createdAt", "")
                    try:
                        s = created_str.replace("Z", "+00:00")
                        created = _dt.datetime.fromisoformat(s)
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=_jst)
                    except Exception:
                        continue
                    if created < lower_bound:
                        continue
                    for p in o.get("packs", []):
                        bet_type = str(p.get("betType", "")).strip()
                        deme = str(p.get("packDeme", "")).strip()
                        vote_amt = int(p.get("voteAmount", 0))
                        for exp in expected:
                            if exp["matched"]:
                                continue
                            if (bet_type == exp["gql_type"]
                                    and deme == exp["deme"]
                                    and vote_amt == exp["amount"]):
                                exp["matched"] = True
                                matches.append({
                                    "order_id": o.get("id"),
                                    "created_at": created.isoformat(),
                                    "betType": bet_type,
                                    "packDeme": deme,
                                    "voteAmount": vote_amt,
                                })
                                break

                n_matched = sum(1 for e in expected if e["matched"])
                if n_matched == len(expected):
                    history_ok = True
                    history_match_info = (
                        f"{n_matched}/{len(expected)} bet exact match: "
                        f"{matches}"
                    )
                    print(
                        f"[execute_purchase] ✅ GraphQL exact match (全 bet): "
                        f"{history_match_info}",
                        file=sys.stderr,
                    )
                else:
                    unmatched = [
                        f"{e['gql_type']}/{e['deme']}/¥{e['amount']}"
                        for e in expected if not e["matched"]
                    ]
                    print(
                        f"[execute_purchase] ⚠️ GraphQL exact match 不足 "
                        f"({n_matched}/{len(expected)}、未一致: {unmatched}、"
                        f"orders={len(orders)} 件)",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(
                    f"[execute_purchase] GraphQL exact match 失敗(継続): {e}",
                    file=sys.stderr,
                )

            # 成功根拠の総合判定 (Codex 4 次 review):
            # (a) failure_hits: 即 fail (既に上で raise されている)
            # (b) history_ok: GraphQL exact match (全 bet 成立確定)
            # (c) success keyword: fallback (受付番号付き完了画面)
            # (d) URL 遷移: ログ用、成功根拠にしない
            # 複数 bet 時は history_ok (全 bet 一致) を必須にする
            # (success keyword だけでは「全 bet 通った」保証にならないため)。
            multi_bet = len(bets) > 1
            if multi_bet:
                if not history_ok:
                    raise RuntimeError(
                        f"まとめ買い投票の全 bet 成立確認が取れない "
                        f"(GraphQL exact match 不足)。"
                        f"\n  URL: {final_url}"
                        f"\n  body text 抜粋: {completion_text[:400]!r}"
                    )
            elif not history_ok and not success_hits:
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
                    f"投票完了: {bets_desc} "
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
    p.add_argument("--car", type=int, default=None, help="car number (単一複勝時)")
    p.add_argument("--amount", type=int, default=None,
                   help="bet amount in yen (単一複勝時)")
    p.add_argument("--bets-json", type=str, default=None,
                   help='まとめ買い: \'[{"type":"fns","cars":[6],"amount":300},'
                        '{"type":"rt3","cars":[6,5,4],"amount":100},'
                        '{"type":"rf3","cars":[4,5,6],"amount":100}]\'')
    p.add_argument("--dry-run", action="store_true",
                   help="navigate のみ、実投票はしない (default: 実投票)")
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # === bets 構築 (--bets-json 優先、無ければ --car/--amount から単一複勝) ===
    bets = None
    if args.bets_json:
        try:
            bets = json.loads(args.bets_json)
        except Exception as e:
            print(f"[execute_purchase] ❌ --bets-json parse 失敗: {e}",
                  file=sys.stderr)
            sys.exit(2)
        if not isinstance(bets, list) or not bets:
            print("[execute_purchase] ❌ --bets-json は非空 list 必須",
                  file=sys.stderr)
            sys.exit(2)
    else:
        if args.car is None or args.amount is None:
            print("[execute_purchase] ❌ --bets-json も --car/--amount も無い",
                  file=sys.stderr)
            sys.exit(2)
        bets = [{"type": "fns", "cars": [args.car], "amount": args.amount}]

    # === 値域・上限チェック (全 bet 共通) ===
    total = 0
    for b in bets:
        bt = b.get("type")
        if bt not in BET_META:
            print(f"[execute_purchase] ❌ 未知の券種: {bt}", file=sys.stderr)
            sys.exit(2)
        cars = b.get("cars", [])
        if len(cars) != BET_META[bt]["n_cars"]:
            print(f"[execute_purchase] ❌ {bt} cars 数不正: {cars}",
                  file=sys.stderr)
            sys.exit(2)
        for c in cars:
            if not (1 <= int(c) <= 8):
                print(f"[execute_purchase] ❌ {bt} 車番不正: {c}",
                      file=sys.stderr)
                sys.exit(2)
        if bt in ("rt3", "rf3") and len(set(int(c) for c in cars)) != 3:
            print(f"[execute_purchase] ❌ {bt} 車番重複: {cars}",
                  file=sys.stderr)
            sys.exit(2)
        amt = int(b.get("amount", 0))
        if amt > MAX_AMOUNT_YEN or amt < 100 or amt % 100 != 0:
            print(f"[execute_purchase] ❌ {bt} amount 不正 "
                  f"(100-{MAX_AMOUNT_YEN}, 100円単位): {amt}", file=sys.stderr)
            sys.exit(2)
        total += amt
    if total > MAX_TOTAL_YEN:
        print(f"[execute_purchase] ❌ 合計 {total} > {MAX_TOTAL_YEN} (safety)",
              file=sys.stderr)
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

    # place_code / race_no の値域
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

    # 注意: 本番モード (`--dry-run` なし) は実投票を発生させる。
    # 三連系まとめ買い対応 (2026-05-30)。dry-run で確認画面検証後に本番。

    try:
        result = asyncio.run(execute_buy(
            args.race_date, args.place, args.race,
            dry_run=args.dry_run, bets=bets,
        ))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)},
                         ensure_ascii=False))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
