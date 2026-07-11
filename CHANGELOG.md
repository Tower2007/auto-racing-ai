# CHANGELOG

## 2026-07-12 (3) — 直前オッズデーモンの単一インスタンスガードを named mutex 化

固定 TCP ポート 58620 bind 方式のガードが Windows の動的除外ポート帯
(WinNAT/Hyper-V の excludedportrange。本日時点で 58529-58628 を包含) に入り、
誰も LISTEN していないのに bind が WinError 10013 で拒否される状態を確認。
旧 `acquire_singleton` は OSError を一括捕捉して None (=「別インスタンス稼働中」)
を返すため、10013 (除外帯) と 10048 (真の使用中) を区別できず、
**開催日の直前オッズ収集が「二重起動回避」として黙って止まる**。

- `odds_prerace_daemon.py`: ポート bind を廃止し、`auto_buy._acquire_lock` で
  実証済みの named mutex パターンに置換 (`Global\AutoRacingAI_odds_prerace_{パスhash}`)。
  - 他プロセスが mutex 所有中 (WAIT_TIMEOUT) の時**だけ** None = 二重起動回避。
  - ガード自体の故障 (CreateMutexW 失敗等) は **fail-open**: 警告ログの上で
    no-op lock を返し収集続行 (発注系 auto_buy の fail-closed とは逆。最悪は
    CSV 重複 append であり、開催日データ欠測より二重稼働のリスクを取る)。
  - WAIT_ABANDONED (先行の異常終了) は警告ログの上で所有を引き継いで続行
    (収集専用で台帳不整合の懸念がないため sticky 停止は不要)。
  - プロセス異常終了時はハンドルを OS が閉じ mutex は自動解放
    (旧方式の「プロセス終了で OS がポート解放」と等価の回復性)。
- `tests/test_odds_prerace_daemon.py`: ポート探索 (`_find_bindable_port`、
  それ自体も除外帯で flake) を廃し、mutex 名 / スレッド排他・解放後再取得 /
  ガード故障時 fail-open (10013 誤検知の回帰ガード) の 3 本に更新。
  加えて本番名でのプロセス間排他 (保持中 None → 解放後取得可) を実機確認。
- `scripts/register_odds_prerace_task.ps1` / `CLAUDE.md`: 58620 記述を更新
  (タスク定義自体は変更不要 — IgnoreNew との 2 段防御は従来どおり)。
- 回帰テスト 53 本全緑。`data/rt3_backstop_stop.flag` には触れていない。

## 2026-07-12 (2) — Codex 再々検証対応: WAIT_ABANDONED を sticky 停止に変更

再々検証で ①backstop厳格化・②最終ゲート再検査 は承認、③ が唯一の×判定:
「skip は当該 run 限りで停止状態を永続化していない — 警告メールを人間が
確認する前に次のスケジュール実行が始まれば、結果不明のまま購入が再開する」。
下記のとおり sticky 化した (本エントリが下記 (1) の ③ の記述を上書きする)。

- `auto_buy.py`: WAIT_ABANDONED 検知時に **全券種を止める sticky な
  「発注結果不明」フラグ `data/abandoned_lock_stop.flag`** を書き出す
  (検知時刻・mutex名・人手照合手順を記載)。運用は `rt3_backstop_stop.flag`
  と同じ「存在=停止、人間のみ削除」。機械照合による自動解除はなし (確実性優先)。
- `run_auto_buy()` 入口 (ロック取得前) でフラグ存在を検査し、存在すれば
  全候補 `skip_abandoned_pending` verdict で発注しない。残存リマインド通知は
  auto_buy_state.json の日次 reset に乗せて **1 日 1 回** に抑制
  (検知時のメールとも重複しない)。
- `app/buy_app.py`: トークン実行直前ゲートに `abandoned_stop_active()` を追加。
  こちらは**三連系に限らず全券種**ブロック (判定不能も fail-closed)。
- 検知メールは「停止 (sticky)」の文面に変更し、再開手順
  (投票履歴 / auto_buy_state.json / bet_history 照合 → 問題なければフラグ削除)
  を明記。mutex 自体は正常解放する (発注可否は mutex ではなくフラグが持つ)。
- テスト更新: abandoned テストを「次回 run **も** 停止 / フラグ明示削除後だけ
  通常復帰 / フラグ内容・通知文面」を要求する仕様に書き換え + フラグ既存時の
  入口停止・日次1回リマインド・複勝のみ候補も停止するテストを追加。計 51 本全緑。

## 2026-07-12 (1) — Codex 再検証 (18f8601/32df58f 後の残存3件) 対応

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
