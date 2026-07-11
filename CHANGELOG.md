# CHANGELOG

## 2026-07-12 — Codex 再検証 (18f8601/32df58f 後の残存3件) 対応

三連系 LIVE 購入は `data/rt3_backstop_stop.flag` (sticky、人間削除でのみ解除) で
停止中。本対応はフラグに一切触れていない (無傷)。

### ① backstop の完全 fail-closed 化 (`src/backstop.py`)
- 旧: `vote_amount` / `hit_amount` の数値変換失敗を `pass` で握りつぶし、
  「CSV は開けるが金額セルが壊れている」場合に投資額 0 円として過小集計
  → 閾値未達と誤判定 → 購入許可し得た。
- 新: 必須ヘッダ (`bet_type_code` / `vote_amount` / `hit_amount`) の存在検証 +
  三連系 (rt3/rf3) 全行の金額を厳格検証 (`_strict_amount`: 空・非数値・負値・
  NaN は不正)。**1 セルでも不正なら profit=None のまま error を立て、部分集計を
  返さない** → `backstop_blocks_purchase()` が True (購入停止 + 警告ログ)。
- sticky フラグ書き出し・メール通知の発火条件は従来どおり「真の閾値割れのみ」
  (fail-closed とは区別)。

### ② 停止フラグの最終発注点での再検査 (`auto_buy.py` / `app/buy_app.py`)
- `auto_buy.rt3_final_gate_blocks(bets)` 新設: bets に三連系が含まれる場合、
  `daily_predict.rt3_buy_active()` を再評価。停止中 or 判定不能は True (fail-closed)。
- `_run_auto_buy_locked` のループ内 (mutex 取得後・`_run_execute_purchase` 前) で
  再検査し、ブロック時は `skip_rt3_stop_recheck` verdict で発注しない
  (候補判定 → mutex 待ち最大 90 秒の間にフラグが立った場合を拾う)。
- `app/buy_app.py`: 「✅ 購入する」ボタン押下直後 (トークン予約・消費前) に
  同じゲートで再検査。停止中は実行拒否 + 画面表示 + `buy_tokens` に failed 記録。

### ③ WAIT_ABANDONED 時の即続行をやめる (`auto_buy.py`)
- 旧: abandoned mutex の所有を引き継いでそのまま発注再開 → 先行プロセスが
  「投票クリック後、state/台帳保存前」に死んだ場合、当日 cap 過少計上・
  重複投票の不確実性が残った。
- 新: `_acquire_lock` が `(k32, handle, abandoned)` を返し、`run_auto_buy` は
  abandoned=True なら**発注を続行せず全候補 `skip_abandoned_lock` + 警告メール**
  (投票履歴 / auto_buy_state.json / bet_history の人手確認を依頼)。
  ロックは finally で正常解放するため以後の run はブロックされない
  (スキップは当該 run 限り。状態異常の確認・解消は人間の運用)。

### ④ 回帰テスト追加 (`tests/test_final_gate_recheck.py`, 8本)
- 「候補生成後にフラグ作成」→ 発注直前再検査で skip (kill-switch / backstop
  sticky / 判定不能 fail-closed の各系)
- 「壊れた台帳 (金額セル不正・ヘッダ欠落)」→ backstop fail-closed
  (部分集計なし、sticky フラグ・通知は発火しない)
- 「投票後クラッシュ (WAIT_ABANDONED 受領)」→ 全候補 skip + 警告通知 +
  ロック正常解放 (以後の run は通常動作)
- 既存 42 本と合わせ 50 本全緑。実発注関数は禁止スタブ (呼ばれたら AssertionError)、
  通知はスタブ、フラグ類は一時ディレクトリのみ使用。

### 残課題 (今回スコープ外)
- 週次再学習 (AutoraceWeeklyRetrain) の自動採用後、本番モデル成果物
  (production_model.lgb / production_calib.pkl / production_meta.json) の
  コミット漏れ防止機構 (retrain タスク末尾での git add/commit 自動化)。
  7/12 は手動コミット (825a57f) で回復済み。自動化は運用の複雑化
  (retrain 実行ユーザーの git 権限・コンフリクト時の挙動) と相談の上で別途。
