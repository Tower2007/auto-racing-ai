# CHANGELOG

## 2026-07-12 (9) — 購入ゲート: acquire を timeout/broken に3値化 — 競合は待ち続け、破損のみ非write

(8) への Codex 第8R判定で残った 1 点。`acquire_purchase_gate()` は **30秒タイムアウト(競合)
でも None** を返し、生成側はそれを一律「mutex 破損」と解釈して非write・正常復帰していた。
この同値は不成立。反例: click 側が OS スケジューリング遅延でゲートを 30秒超保持 → 生成側が
timeout → 非write で run mutex 解放 → click 再開して送出 → sticky フラグ無しで後続購入も再開。

- **取得を 3 値化** (`auto_buy.py`): `_acquire_purchase_gate_ex()` が
  `GATE_OK`(handle) / `GATE_TIMEOUT`(競合=ゲートは在るが他者保持) / `GATE_BROKEN`
  (CreateMutexW 失敗・非Windows・異常 rc) を返す。timeout と broken を必ず区別。
- **click 側ラッパ** `acquire_purchase_gate()`: 従来どおり gate-or-None を返す
  (timeout も broken も None → click 側はどちらでも fail-closed abort が正しい)。互換維持。
- **生成側 (WAIT_ABANDONED 経路)** は `acquire_purchase_gate_blocking()` を使用:
  - `GATE_OK` → ゲート保持下で原子的に write + 解放。
  - `GATE_TIMEOUT`(競合) → **諦めず再試行し続ける**。click 側は必ず finally で
    解放する (最終検査 + click timeout 5秒 + 解放の最短区間) ため、総上限
    `PURCHASE_GATE_TOTAL_WAIT_SEC=300秒` (click 保持 5秒を大幅超過) 内に必ず取得できる。
  - `GATE_BROKEN`(mutex 生成不可) → **非write**。この時のみ「click 側も生成不可で
    fail-closed=購入不可だからフラグ非writeでも安全」の論法が成立 (broken 限定で正しい)。
  - 総上限超過 (通常起き得ない異常) のみ **最後の砦**: best-effort でフラグを書き
    後続購入を停止 (この click 単体の原子性は既に達成不能=click は自ゲート保持下で
    検査済みだが、sticky halt で後続を止めるのが安全上最重要)。
- **デッドロック/ハング**: ネスト順 run mutex → 購入ゲート は不変。生成側が総上限まで
  待つ間 run mutex を保持し続けるが、click 側は run mutex を取らないので循環しない。
- `tests/test_final_gate_recheck.py`: ⑧ 3 値化の回帰 — `_acquire_purchase_gate_ex` の
  timeout/broken 区別 (実 mutex 競合→GATE_TIMEOUT・解放後 GATE_OK) / broken→非write・
  GATE_OK→write / timeout は再試行し取得後 write (排他外 write なし) / 総上限超過→
  最後の砦 best-effort write。既存の実 mutex 待機テストも維持。
- 回帰テスト 66 本全緑 (実発注は禁止スタブ/fake、通知スタブ、フラグ・mutex 名は
  テスト用に一時分離)。`data/rt3_backstop_stop.flag` は不変 (SHA256 一致)。

## 2026-07-12 (8) — 購入ゲート: 生成側の排他外 write を廃止 (所有下のみ書込) — 最後の逃げ道閉塞

(7) への Codex 第7R判定で残った**生成側の逃げ道**を訂正。(7) では「ゲートが取れなくても
フラグは必ず書く」としていたが、これが穴になる: 生成側がゲート取得を 30 秒でタイムアウト
→ ゲート外で write → その隙に (ゲート保持中だった) click 側が送出、という順序が
「OS スケジューリング含む厳密性」で残っていた。

- **生成側 (`auto_buy.py` run_auto_buy の WAIT_ABANDONED 経路)**: 購入ゲートを
  **所有したときだけ** `_write_abandoned_flag()` を実行する。取得成功時のみ
  「保持下で write → finally で解放」。`acquire_purchase_gate()` が None
  (= 待機しても取れない = mutex サブシステム破損) のときは**排他外 write を行わない**
  (逃げ道の削除)。生成側の取得待ち PURCHASE_GATE_WAIT_SEC(30秒) は click 側の最大
  保持時間 (click timeout 5秒) を十分上回るため、通常/競合時は待てば必ずゲート下で書ける。
- **None 時に write しない安全性の根拠 (設計に明記)**: 同じ壊れたゲートを **click 側も
  取得できず fail-closed で発注中止する** (execute_purchase の購入ゲート取得が None →
  abort)。したがって mutex 破損中は購入が起き得ず、生成側がフラグを書けなくても安全。
  トレードオフとして破損中は sticky halt の永続フラグが残らないが、その間は購入自体が
  不可能なので実害はない。逃げ道の排他外 write は原子性を壊すため許容しない。
- **click 側 (`scripts/execute_purchase.py`)**: (6)/(7) のとおり購入ゲート取得後に
  「最終検査 → click」を行い、**必ず finally で解放**する (click timeout 5秒で必ず抜ける)。
  この回では変更なし (生成側の訂正のみ)。
- `tests/test_final_gate_recheck.py`: 生成側の回帰 2 本を追加/更新 —
  ゲート None → 排他外 write せずフラグ未生成 (release も呼ばない・通知本文はゲート
  取得不能を明示) / ゲート取得成功 → 保持下で write + 解放。加えて原子性:
  click 側 (別スレッド) がゲート保持中は生成側が**待機**しフラグ未生成、解放後に
  ゲート下で write (実 mutex・別スレッドでブロックを観測)。None 時に click 側が
  abort する側は既存 `test_click_aborts_when_purchase_gate_unavailable` が担保。
- 回帰テスト 63 本全緑 (実発注は禁止スタブ/fake、通知スタブ、フラグ・mutex 名は
  テスト用に一時分離)。`data/rt3_backstop_stop.flag` は不変 (SHA256 一致)。

## 2026-07-12 (7) — 共有購入ゲート mutex で「フラグ生成」と「検査→click」をプロセス間原子化

(6) への Codex 第6R判定で残った厳密なプロセス間非原子性 (検査→click の間に別プロセスが
フラグ生成する余地) を、専用の共有 named mutex で完全閉塞。

- **購入ゲート named mutex を新設** (`auto_buy.py`):
  `Global\AutoRacingAI_purchase_gate_<sha1(ROOT)[:8]>` (run mutex とは別名前空間)。
  `acquire_purchase_gate()` / `release_purchase_gate()` / `_purchase_gate_name()` /
  `PURCHASE_GATE_WAIT_SEC=30` を追加。実装は既存 named mutex パターン (WinDLL
  use_last_error、argtypes/restype 明示、WAIT_OBJECT_0/ABANDONED 判定、ReleaseMutex
  戻り値検査) を流用。非Windows は排他保証がないため fail-closed (None)。短命
  区間用なので abandoned は所有引き継ぎのみ (sticky 停止は run 側が担う)。
- **フラグ生成側** (`auto_buy.py` run_auto_buy の WAIT_ABANDONED 経路):
  `_write_abandoned_flag()` を購入ゲート保持下で実行 (取得→書込→解放)。ゲートが
  取れなくてもフラグ自体は必ず書く (停止が最優先・fail-closed)。
- **click 側** (`scripts/execute_purchase.py` Step 8): ブラウザ準備 (locator/count/
  is_disabled) をゲート外で済ませ、購入ゲート取得後は「最終 `_stop_flags_block` 検査
  → `vote_btn.click()`」だけを最短で実行し解放。ゲート取得不能は fail-closed で発注中止。
  これで、click 側がゲート保持中はフラグ生成側がブロックされ、検査〜click に別
  プロセスのフラグを割り込ませられない (逆順ならフラグは検査で必ず可視)。
- **デッドロック回避**: ネスト順は常に「run mutex → 購入ゲート」の一方向。フラグ
  生成側は run mutex 保持中にゲートを取り、click 側は run mutex を一切取得しない
  (ゲートのみ) ため循環待ちが生じない。テストで担保。
- `tests/test_final_gate_recheck.py`: ⑦ 購入ゲートの回帰 3 本追加 — ゲート取得不能
  →click 未発火で fail-closed abort / 保持中は別スレッドが取得不能・解放後に取得可
  (相互排他) / run mutex 保持中でもゲート取得がデッドロックしない。`_AutoBuySandbox`
  に購入ゲート名の分離を追加。
- 回帰テスト 61 本全緑 (実発注は禁止スタブ/fake、通知スタブ、フラグ・mutex 名は
  テスト用に一時分離)。`data/rt3_backstop_stop.flag` は不変 (SHA256 一致)。

## 2026-07-12 (6) — execute_purchase: click 直前の最終再検査で並行 TOCTOU を原子化

(5) への Codex 第5R判定で残った並行 TOCTOU (窓は数百行→数十msに激減したが未閉塞) を閉じる。

- **click 直前の最終再検査** (`scripts/execute_purchase.py` Step 8):
  Step 8 冒頭 (:769 相当) の `_stop_flags_block` 検査から実 `vote_btn.click()` までに
  時刻取得・locator 取得・`await vote_btn.count()`・`await vote_btn.is_disabled()` の
  2 つの await があり、その待ち中に別プロセスが `abandoned_lock_stop.flag` を生成すると
  再検査なしで click に進み得た。**全 await 完了後・`await vote_btn.click()` の直前行**に
  `_stop_flags_block(bets)` をもう一度挿入。停止/判定不能なら click せず RuntimeError で
  abort (fail-closed)。Step 8 冒頭の検査は残す (早期 abort でブラウザ操作を無駄にしない)
  ため、実質「await の前後で 2 回」になり原子的に近づく。
- **docstring 修正**: `_stop_flags_block` の docstring が三連系ゲートをまだ
  `backstop_active()` と記載していた (実コードは (5) で `backstop_blocks_purchase()` に
  修正済) のを実装に合わせて訂正。
- `tests/test_final_gate_recheck.py`: 既存「開始前フラグ作成」テストに加え、
  **「:769 検査通過後・await 中にフラグ生成 → click 未発火で abort」** を追加
  (`is_disabled` の await 副作用で一時 dir にフラグ生成しタイミングを再現、
  playwright 全 fake・ROOT 一時 dir)。全 fake 駆動を `_drive_execute_buy` に共通化。
- 回帰テスト 58 本全緑 (実発注は禁止スタブ/fake、通知スタブ、フラグは一時 dir のみ)。
  `data/rt3_backstop_stop.flag` は不変 (SHA256 一致)。

## 2026-07-12 (5) — execute_purchase: クリック直前再検査 + 三連系ゲートを backstop_blocks_purchase 化

(4) の入口ゲートに対する Codex 第4R判定で残った 2 点を修正。

- **① 実投票クリック直前の停止フラグ再検査** (`scripts/execute_purchase.py`):
  入口ゲート `_stop_flags_block` はブラウザ処理の数百行手前で 1 回見るだけだった。
  buy_app/直CLI 経路は auto_buy の named mutex を保持しないため、入口検査通過〜
  実クリックの間に別 run が abandoned フラグを作成し得る。実際に金銭が確定する
  「投票する」click の直前 (Step 8、`vote_btn.click` の最小スコープ直前) に
  `_stop_flags_block(bets)` を再挿入。停止中/判定不能 (fail-closed) なら
  クリックせず `RuntimeError` で abort (main が success:false + exit 1 に変換)。
  発注ロジック本体・カート構築には触れていない。
- **② 三連系入口ゲートを `backstop_blocks_purchase()` に** (`_stop_flags_block`):
  旧実装は `backstop_active()` (フラグのファイル存在のみ) を見ており、直CLI 経路で
  台帳異常・閾値超過を迂回できた (台帳ゲート=True なのに execute_purchase ゲート
  =False)。`backstop_blocks_purchase()` (フラグ存在 OR 台帳読取不能 OR 閾値超過で
  停止、いずれも fail-closed) に置換。rt3_backstop_stop.flag のファイル存在停止も
  当然包含。abandoned (全券種) は従来どおり `auto_buy.abandoned_stop_active()` 共有。
- `tests/test_final_gate_recheck.py`: ⑥ クリック直前再検査の回帰を追加 —
  ブラウザを全 fake (playwright/`_launch_context`/券種ヘルパをスタブ、ROOT を
  一時 dir に差替えて実 data/ を汚さない) にし、Step 8 直前で abandoned フラグを
  立てると「投票する」click 関数が一度も呼ばれず abort することをアサート。
  入口ゲートテストは backstop_blocks_purchase 化に合わせ、台帳異常 (フラグ無し) →
  三連系停止 (② の直CLI迂回穴) のケースを追加。
- 回帰テスト 57 本全緑 (実発注は禁止スタブ/fake、通知スタブ、フラグは一時 dir のみ)。
  `data/rt3_backstop_stop.flag` は不変 (SHA256 一致)。

## 2026-07-12 (4) — abandoned 停止の並行 run 競合窓を閉塞 + execute_purchase 入口ゲート

Codex 再検証で、WAIT_ABANDONED sticky 停止に**並行実行の競合窓**が残ると指摘:
入口の abandoned フラグ検査が mutex 取得**前**の 1 回だけのため、
(1) run B が入口でフラグ不存在を確認 → mutex 待ちに入り、(2) その間に run A が
WAIT_ABANDONED を受領して sticky フラグを作成・(3) mutex を正常解放すると、
(4) run B は WAIT_OBJECT_0 で正常取得し `lock[2]` 分岐に入らず、作成済みフラグを
再検査せず発注してしまう。

- `auto_buy.py`: `run_auto_buy` の mutex 取得後・`_run_auto_buy_locked` 呼出前
  (発注ループ直前) に `abandoned_stop_active()` を**再検査**。存在すれば全候補を
  `skip_abandoned_pending` で return (発注なし、日次1回の残存通知)。判定不能も
  fail-closed。ロックは finally で従来どおり正常解放する。
- `scripts/execute_purchase.py`: 発注の全経路 (auto_buy subprocess 経由・手動UI
  buy_app 経由) が最終的に集約される入口に fail-closed ゲート `_stop_flags_block`
  を追加。abandoned_lock_stop.flag は**全券種**、rt3_backstop_stop.flag は
  **三連系 (rt3/rf3) を含む bets のみ**参照し、停止フラグ ON / 判定不能なら
  `sys.exit(3)` で実発注 (`execute_buy`) に到達させない。発注ロジック本体・既存の
  停止経路 (daily_predict / buy_app 再検査) は不変 = 多重防御。
- `tests/test_final_gate_recheck.py`: 並行 run 競合窓の回帰 3 本追加
  — ④ mutex 待ち中のフラグ作成 → 取得後再検査で skip / 取得後フラグ無しは
  通常発注へ復帰 (誤停止しない回帰) / ⑤ execute_purchase 入口ゲート
  (全券種 abandoned・三連系のみ backstop・判定不能 fail-closed)。
  実 mutex 不要 (フラグ作成タイミングのみ制御) で全 OS 実行可。
- 回帰テスト 56 本全緑 (実発注は禁止スタブ=AssertionError、通知スタブ、
  フラグは一時 dir のみ)。`data/rt3_backstop_stop.flag` には触れていない。

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
