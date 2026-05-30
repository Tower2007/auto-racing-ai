# 自動投票 Phase 1 実装依頼 (夜間限定 + 厳格ガード)

作成: 2026-05-31 (出先 PC の Claude session)
状態: **実装待ち** (家 PC の Claude / Codex / Antigravity が実装)

## 経緯

- 2026-05-30 浜松 R6/R7/R9 で三連系まとめ買い本番稼働 (¥1,100 → -¥900)
- ユーザー意向: 寝てる時間帯 (山陽ミッドナイト + 川口ナイター後半) の機会損失を取り戻すため自動投票化したい
- 既存の click-to-buy (人間 click) は数秒で投票完了 → 起きてる時間は十分機能
- → **寝てる時間帯だけ自動化** するハイブリッド案を採用

## 規約調査結果 (出先 PC 側で実施)

- **autorace.jp/policy**: 自動投票・bot の明示禁止 **なし**
  - 残るリスク: 「当協会が不適切と判断する行為」(主観条項)
- **netvote_guide/agreement.php (約定書) 第13条**:
  > 「銀行直結会員は車券を購入しようとする場合は自ら申込むものとし、他人に申し込ませることはできません」
  - ユーザー解釈: 自分の PC で自分の cookie で自分のスクリプトが動く → 「他人」ではない (literal reading)
  - この解釈は autorace.jp が明示的に禁止していない以上 defensible
- **結論**: 明確な「黒」ではないが「白」でもないグレー領域。Phase 1 の限定スコープなら現実的にリスク最小化される

## 実装仕様

### 機能フラグ (デフォルト OFF)

`daily_predict.py` (または `.env` / 別ファイル) に:

```python
AUTO_BUY_ENABLED = False      # マスタースイッチ (デフォルト OFF)
AUTO_BUY_DRY_RUN = True       # 実投票せず log のみ (Week 1 はこれ)
AUTO_BUY_HOURS = (22, 6)      # 自動投票許可時間帯 (22:00 〜 翌 06:00)
MAX_DAILY_AUTO_YEN = 2000     # 1 日合計上限
DAILY_LOSS_STOP_YEN = -1500   # 当日累積これを下回ったら以降 skip (上限 ¥2000 の 75%)
EV_ANOMALY_CAP = 10.0         # EV がこれを超える R は skip (バグ防御)
CONSECUTIVE_FAILURES_STOP = 3 # 連続 Playwright エラー回数で停止
```

### 動作フロー

`daily_predict.py` 内、メール送信直前 (`send_email` 呼び出し直前) に以下を挟む:

```python
if AUTO_BUY_ENABLED and not picks.empty:
    auto_buy_eligible_picks = _filter_auto_buy_picks(picks, sanyo_rf3_refs, rt3_refs)
    if auto_buy_eligible_picks:
        _execute_auto_buy(auto_buy_eligible_picks, dry_run=AUTO_BUY_DRY_RUN)
```

`_filter_auto_buy_picks` の判定 (全て満たした R のみ自動投票対象):

1. **時間帯**: 各 R の発走時刻が `AUTO_BUY_HOURS` 範囲内 (22:00-06:00)
2. **EV 上限**: `ev_avg_calib <= EV_ANOMALY_CAP` (異常値除外)
3. **1日上限**: 既に当日自動投票合計 + 今回投資額 <= `MAX_DAILY_AUTO_YEN`
4. **累積損失**: 当日累積損益 (data/bet_history.csv) > `DAILY_LOSS_STOP_YEN`
5. **連続失敗**: 直近 `CONSECUTIVE_FAILURES_STOP` 回 連続失敗してない

これらは `data/auto_buy_state.json` (1 日ごとに reset) で管理:

```json
{
  "date": "2026-06-01",
  "spent_yen": 600,
  "profit_yen": -300,
  "consecutive_failures": 0,
  "executions": [
    {"race": "sanyou_R11", "amount": 300, "verdict": "executed", "timestamp": "..."},
    {"race": "sanyou_R12", "amount": 0, "verdict": "skip_hours", "timestamp": "..."}
  ]
}
```

### 実行: `_execute_auto_buy`

各 R について:
1. `auto_buy_state.json` を read/lock
2. ガード判定 (上記 5 つ)
3. dry_run=True なら `[DRY-RUN]` prefix で log + state 更新のみ (実投票なし)
4. dry_run=False なら `scripts/execute_purchase.py --bets-json ...` を subprocess.run で実行
5. 成功/失敗を state に追記
6. **Gmail 即時通知** (件名: `[AUTO-BUY] sanyou R11 ¥300 受付完了` 等)

### Gmail 即時通知の重要性

毎回投票後 30 秒以内に必ず Gmail 通知。これがユーザーの唯一の monitoring 手段になるため:
- 受付完了 → 結果通知
- ガードで skip → skip 理由通知
- Playwright エラー → エラー詳細 + state dump 通知

### 段階導入プラン

| Week | 設定 | 目的 |
|---|---|---|
| Week 1 | `AUTO_BUY_ENABLED=True, DRY_RUN=True` | 判定ロジック検証 (実投票なし) |
| Week 2 | `DRY_RUN=False`, 複勝のみ自動 (rt3+rf3 は引き続き手動) | 複勝の自動化検証 |
| Week 3 | 三連系含む完全自動 (時間帯内のみ) | フル機能 |

各週で **5 件以上の R で検証完了** + Gmail 通知 5 件以上 OK 確認後に次週移行。

### 注意点 (家 PC Claude/Codex への申し送り)

1. **既存の click-to-buy ロジックを壊さない**: AUTO_BUY_ENABLED=False の時は完全に従来通り動くこと
2. **execute_purchase.py は再利用**: 新規ロジック書き直しではなく、引数を整えて既存スクリプトを叩く
3. **dry-run の log 出力**: 実投票後の Playwright 確認画面 (N 組 / 合計額 / 各出目) と同じフォーマットで log
4. **時刻判定は JST**: `dt.datetime.now(JST)` 経由、UTC で誤動作しないこと
5. **state ファイルの atomic write**: 並行実行で破損しないよう tempfile + os.replace
6. **既存の sanyo_rf3 paper / rt3_paper.csv 記録は維持**: 自動投票後も従来通り CSV append

### テスト要件

- `tests/test_auto_buy_guards.py` を新規追加。以下を mock データで検証:
  - 時間帯外 → skip
  - 1日上限超過 → skip
  - 累積損失停止 → skip
  - EV 異常値 → skip
  - 連続失敗停止 → skip
  - 全条件満たす → execute (dry_run/live 両方)

### Gmail 設定変更

通常の `MAIL_TO` とは別に `AUTO_BUY_NOTIFY_TO` を分離可能に (.env)。
両方未設定なら通常 `MAIL_TO` にフォールバック。

## ユーザー判断の記録 (重要)

ユーザーは以下を理解した上で進行に GO を出した:

1. 約定書 第13条「他人」解釈は autorace.jp 側の主張次第で凍結リスクあり
2. 「不適切と判断する行為」主観条項により事後制限の可能性
3. 凍結時の資金没収リスクはゼロではない
4. → これらを承知の上、**Phase 1 (夜間限定 + 厳格ガード) で進める**

## 完了基準

- [ ] 上記仕様で実装 + tests/test_auto_buy_guards.py 緑
- [ ] Week 1 dry-run で 5 件以上の判定確認 (実投票なし)
- [ ] Gmail 通知が 5 件以上 30 秒以内に到着
- [ ] Week 2 移行判断はユーザー手動 (Opinion/ClaudeFeedback.md に記録)
