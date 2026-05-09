# execute_purchase 本番安全性強化(Codex 引き継ぎ)

作成: 2026-05-09(Claude)
状態: **Codex 修正待ち**(本番モードは暫定無効化中)

## 経緯

- 2026-05-09: Claude が click-to-buy 機構を実装、本番モードで 1 回テスト投票成立
- 直後 Codex review で P1-P3 の本番安全性問題が指摘された
- ユーザー判断「本番購入は止めたい」を即実現するため、Claude が `execute_purchase.py:main()` の冒頭で `--dry-run` 強制チェックを入れて本番モード暫定無効化
- 本格修正は Codex に依頼

## P1-P3 指摘内容(Codex 2026-05-09 review より)

### P1-1: 確認画面で券種・車番・場/R を構造的に検証していない
**現状**: `execute_purchase.py:344` 付近で `body.innerText` に "1組" と "{amount}円" が含まれるかしか check していない。

**問題**: selector が別車番や別券種を選んでも 1組100円なら通過する。`tr:nth-child({car_no})` が画面構造変更や欠車表示で別行を指しても検出不可。

**修正方針**:
- 確認画面 DOM から構造的に bet type / car / amount を読む
- 「複勝」テキスト、対象車番(`複勝 1組 / N` の N が car_no と一致)、場名(浜松/川口等)、`{race_no}R` を全て確認
- いずれかが不一致なら abort

### P1-2: token の race_date が実投票 URL に検証されていない
**現状**: payload の race_date は token に含まれているが、実投票 URL は `/vote?vel_code=...&race_num=...` のみで日付なし。TTL 24h。

**問題**: 古いメール / 日付をまたいだ token から、同じ場/R の **別開催** を購入するリスク。

**修正方針**:
- `payload["race_date"]` が今日 (JST today) と一致することを起動時に検証
- もしくは投票画面 / 確認画面の日付表示を読んで payload と比較
- token TTL を 10〜30 分程度に短縮

### P2-1: 投票完了を確認せず success を返している
**現状**: `execute_purchase.py:370-397` 付近、「投票する」 click 後 5 秒待って URL を出すだけで `success=True`。

**問題**: 締切、残高不足、追加確認、サイト側エラー、通信失敗が画面に出ても購入完了扱い。

**修正方針**:
- 「投票する」 click 後、完了メッセージ / 受付番号 / 完了画面の URL を確認
- もしくは fetch_order_history で該当 R の購入確認が取れた場合だけ success
- 失敗パターンの error message も網羅

### P2-2: token 消費が check-and-set になっていない
**現状**: `app/buy_app.py:91-172` で `is_consumed(sig)` 確認後に別関数で `log_token(..., consumed)`。CSV 追記にロックなし。

**問題**: 同じ token を複数タブ/同時押しで両方が未消費判定を通過する race condition。

**修正方針**:
- file lock 付きの atomic consume/reserve 関数にまとめる
- `fcntl.flock` (Linux) / `msvcrt.locking` (Windows) / `portalocker` 等で排他制御

### P3: 金額 validation が上限だけ
**現状**: `execute_purchase.py:66-69` で `amount > 1000` のみ弾いている。

**問題**: 0円、負数、100 円単位でない金額が CLI や将来の token 生成ミスから渡る。

**修正方針**:
- `100 <= amount <= 1000 and amount % 100 == 0` を強制
- buy_token.py 側でも同じ validation を入れる(二重防御)

## 暫定対応(Claude 2026-05-09 実装)

`scripts/execute_purchase.py:main()` の冒頭で:
```python
if not args.dry_run:
    print("[execute_purchase] ⚠️ 本番モードは現在 暫定無効化")
    sys.exit(2)
```

これで本番モードで起動されても abort、--dry-run のみ動作可能。
本番運用は Codex 修正完了まで停止。

## Codex への依頼

優先度順に上記 P1-P3 を修正してください。完了したら:
1. `scripts/execute_purchase.py:main()` 冒頭の暫定無効化ブロックを削除
2. 修正の test を 1 回 dry-run + 1 回本番(¥100、慎重に)で実施
3. memory `ml_baseline_findings.md` に修正完了記録を追加
4. 本 brief を Opinion/codex_briefs/ から完了印付きで残す or 別 archive へ移す

## 5 次 review 後の P3 (¥100 テスト後 follow-up)

Codex 5 次 review (2026-05-09) で「P1 残無し、P2 grace を直せば本番解除に近い」
判定。P2 は反映済 (CLICK_GRACE 30 秒)。残る P3 は ¥100 テスト後の follow-up:

### P3-A: success keyword fallback を受付番号構造まで寄せる
現状: `SUCCESS_KEYWORDS = ("投票が完了しました", "投票完了", "受付番号",
"投票を受け付けました")` の単純文字列 match。
推奨: 受付番号の実体パターン (例: `受付番号\s*[:：]?\s*\w+`) や強い完了文言に
限定して、断片マッチでの誤成功を更に潰す。

### P3-B: fetch_order_history の library 化
現状: `fetch_orders()` の下流 `post_graphql()` が認証失敗時に
`sys.exit(1)` する設計。library として呼ぶ buy_app / execute_purchase 側で
`except Exception` が捕捉できず、終了が荒くなる。
推奨: `post_graphql` を sys.exit ではなく専用例外 (例: `AuthError`,
`GraphQLError`) を raise する形に refactor、cli main 側だけ exit に変換する。

これらは初回 ¥100 テストの後で OK。

## 関連 commit / file

- `scripts/execute_purchase.py` (main 関数)
- `scripts/buy_token.py` (sign / verify / log_token / is_consumed)
- `app/buy_app.py` (確認 UI、subprocess.run で execute_purchase 呼出し)
- `daily_predict.py` (推奨メールに button 埋込み、payload 生成)

直近 commit:
- `74b37d3` fix(execute_purchase): 5 つの安全装置追加(本 brief 受領前の Claude 実装)
- `b7826d1` feat(execute_purchase): 本番投票ロジック実装
- (今回 commit): fail-safe で本番暫定無効化
