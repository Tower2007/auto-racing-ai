# Claude への依頼: execute_purchase 本番安全性強化

作成: 2026-05-09 Codex  
対象: auto-racing-ai click-to-buy / `execute_purchase.py` 周辺  
状態: **本番購入は暫定無効化中。修正担当は Claude。**

## 依頼の目的

click-to-buy はすでに 1 回の本番テスト投票に成功しているが、Codex review で実購入前の安全確認に P1/P2 の穴が見つかった。  
現状は `scripts/execute_purchase.py:main()` 冒頭で `--dry-run` 以外を `sys.exit(2)` する fail-safe が入っているため、誤発火は防げている。

Claude 側で以下を修正し、**実購入を再開できる水準まで hardening** してほしい。戦略・モデル・閾値は変更しない。対象は購入実行系の安全性のみ。

## 現在の暫定無効化

`scripts/execute_purchase.py:424-458` に以下の趣旨のブロックがある。

```python
if not args.dry_run:
    print("[execute_purchase] 本番モードは現在 暫定無効化")
    sys.exit(2)
```

修正が完了し、dry-run 検証が通るまではこのブロックを残すこと。削除は最後。

## 必須修正

### 1. P1: 確認画面で券種・車番・場/R・金額を検証する

対象:
- `scripts/execute_purchase.py:220-344`

現状:
- 車番選択は `tr:nth-child({car_no})` に依存している。
- 確認画面の最終チェックは `body.innerText` に `"1組"` と `"{amount}円"` が含まれるかだけ。

問題:
- selector が別車番を押しても、別券種を押しても、1組100円なら通過する。
- 欠車表示や DOM 変更で `nth-child` がずれると実銭で誤購入する。

修正方針:
- 確認画面到達後、DOM または十分限定したテキスト抽出で以下をすべて検証する。
  - 券種が **複勝**
  - 車番が payload/引数の `car_no`
  - 場が `place_code` と一致
  - R が `race_no` と一致
  - 合計金額が `amount`
  - 投票数が 1 組 1 票相当
- いずれか不一致なら `RuntimeError` で abort し、絶対に `投票する` を click しない。
- 可能なら車番選択も `nth-child` ではなく、行内テキスト/車番表示に基づく selector に寄せる。

受け入れ条件:
- dry-run で確認画面に到達した際、ログに `確認画面 OK: 複勝 / 車N / 場 / R / amount` のように具体的に出る。
- 確認画面検証を意図的に不一致にすると実投票前に落ちる。

### 2. P1: token の race_date を実投票対象と照合する

対象:
- `scripts/execute_purchase.py:173-181`
- `scripts/buy_token.py:29-83`
- `daily_predict.py:545-554`

現状:
- token payload に `race_date` は入っている。
- 実購入 URL は `/vote?vel_code=NNN&race_num=N` で日付を含まない。
- token TTL は 24 時間。

問題:
- 古いメールや日付をまたいだ token で、同じ場/R の別開催を買う可能性がある。

修正方針:
- 最低限、`race_date == JST today` を `buy_app.py` または `execute_purchase.py` 起動時に強制する。
- 可能なら投票画面/確認画面に表示される日付や開催情報を読み取り、payload と一致確認する。
- `DEFAULT_TTL_SEC` を 10〜30 分程度へ短縮する。推奨は 30 分以下。

受け入れ条件:
- 昨日以前の `race_date` payload では dry-run でも購入フローに進まない。
- TTL 切れ token は verify で落ちる。

### 3. P2: 投票完了を確認してから success を返す

対象:
- `scripts/execute_purchase.py:370-397`

現状:
- `投票する` click 後、5 秒待って `success=True` を返す。

問題:
- 締切、残高不足、追加確認、サイト側エラー、通信失敗が画面に出ても成功扱いになる可能性がある。

修正方針:
- click 後に以下のいずれかで成功確認する。
  - 完了画面の明示テキスト
  - 受付番号/購入番号
  - 完了 URL
  - `fetch_order_history` 相当で該当 R の購入記録を確認
- 失敗語句も検出する。例: 締切、残高不足、エラー、失敗、購入できません、投票できません。
- 成功確認が取れない場合は `success=False` / exit 1 扱い。

受け入れ条件:
- 本番 1 回テスト時、stdout/stderr に成功根拠が残る。
- `buy_app.py` はその成功根拠を `data/buy_tokens.csv` の `executed` note に残す。

### 4. P2: token consume を atomic にする

対象:
- `app/buy_app.py:91-172`
- `scripts/buy_token.py:98-124`

現状:
- `is_consumed(sig)` と `log_token(..., consumed)` が分離している。
- CSV 書き込みに排他ロックがない。

問題:
- 複数タブや二重クリックで同じ token が同時に未消費判定を通る race condition がある。

修正方針:
- `buy_token.py` に atomic な reserve/consume 関数を追加する。
- Windows 環境なので `msvcrt.locking`、または依存追加が許されるなら `portalocker` を検討する。
- 依存追加する場合はプロジェクト方針に従い、ユーザー確認後 `uv add portalocker`。
- dry-run は消費済みにしない設計でもよいが、本番は `reserved` → `executed` / `failed` の状態遷移にするのが望ましい。

受け入れ条件:
- 同じ token を同時に 2 回押しても、本番実行に入るのは 1 つだけ。
- `failed` 後に再試行可能にするか不可にするかを明示する。

### 5. P3: 金額 validation を二重化する

対象:
- `scripts/execute_purchase.py:66-69`
- `scripts/buy_token.py`
- `app/buy_app.py:100-105`

現状:
- `amount > 1000` だけを弾いている。
- メール生成は `amount=100` 固定だが、CLI/token からは不正値が入り得る。

修正方針:
- 実行側で `100 <= amount <= 1000 and amount % 100 == 0` を強制する。
- Phase A は当面 `amount == 100` 固定でもよい。より安全なのは `amount == 100` を hard enforce。
- token verify 後の `buy_app.py` でも payload validation を行う。

受け入れ条件:
- `--amount 0`, `--amount -100`, `--amount 150`, `--amount 1100` はすべて購入フロー前に落ちる。

## 実装順序の提案

1. `buy_token.py` に payload validation と TTL 短縮を入れる。
2. `buy_app.py` に token/payload validation と atomic reserve を入れる。
3. `execute_purchase.py` に amount/date validation を入れる。
4. `execute_purchase.py` の確認画面検証を強化する。
5. `execute_purchase.py` の投票完了判定を強化する。
6. `py_compile` と dry-run を通す。
7. ユーザー確認後、暫定無効化ブロックを削除。
8. ユーザー立ち会いで本番 1 回だけ ¥100 テスト。

## テスト観点

最低限:

```powershell
.venv\Scripts\python.exe -m py_compile app\buy_app.py scripts\buy_token.py scripts\execute_purchase.py daily_predict.py
```

dry-run:

```powershell
.venv\Scripts\python.exe scripts\execute_purchase.py --race-date YYYY-MM-DD --place 6 --race N --car C --amount 100 --dry-run
```

negative:

```powershell
.venv\Scripts\python.exe scripts\execute_purchase.py --race-date 2020-01-01 --place 6 --race N --car C --amount 100 --dry-run
.venv\Scripts\python.exe scripts\execute_purchase.py --race-date YYYY-MM-DD --place 6 --race N --car C --amount 150 --dry-run
```

本番:
- 暫定無効化ブロック削除後に 1 回だけ。
- 必ず ¥100。
- 成功後、`fetch_order_history` / `bet_history` / `buy_tokens.csv` の整合を確認。

## 注意

- 本修正は「自動投票戦略の拡張」ではない。ユーザー click in the loop の安全化。
- Phase A の `baseline_fns_only` / `thr=1.50` / `amount=100` は変更しない。
- 実購入系なので、成功よりも誤購入防止を優先する。
- 不明点があれば本番再開せず、fail-safe を残す。
