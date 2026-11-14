# CHANGELOG

## [Unreleased] - 発火のリードを全場 4 分前に (2026-11-14)

### Changed
- **`dynamic_scheduler.py`**: `LEAD_MIN_DEFAULT` を 3 → 4。飯塚・川口も発走 4 分前に発火する (利用者判断)。
  9/1 以降の実発注 215 件 (投票完了 211・失敗 4) を集計すると、3 分前の 2 場 (85 件) は発注完了が発走の
  125〜147 秒前で、コメントの「締切は発走 -2:30」より後に完了して全件成功していた (実際の締切の位置は不明)。
  投票操作は中央 32〜35 秒・最大 44 秒で、サイトが重い日の遅れを吸収する余裕が無かった。4 分前の 3 場 (126 件) は
  発走の 180〜200 秒前に完了している。引き換えは判断に使うオッズが 1 分古くなること。
  反映は次回の朝 07:00 の登録から (当日分は登録済みのため変わらない)。
  日跨ぎの R (発走 00:04) は 00:00 発火になり、夜次レポート (00:00) と同時刻に動く

## 2026-11-13 — 全期間 (直近まで) で学習したシャドーを並走 (本番の学習方法・予測値・発注は不変)

- **事実**: 本番 (`ml/train_production.py`) は日付の 90% 分位より前だけで学習し、末尾 10% は早期打ち切り
  (と v3 の比較窓) にしか使っていない。11/08 版のデータでは 学習 2021-04-26〜2026-05-04 (263,735 行) /
  末尾 2026-05-05〜11-06 (29,454 行。v3 では早期打ち切り 05-05〜08-04 14,692 行 + 比較窓 08-05〜11-06 14,762 行)。
  初版 (2026-04-29, 17dc238) の docstring は「学習データ: ml_features.parquet 全体」「末尾 10% を validation にして
  early stopping」で、意図的な取り置きの記述は無く、推論用の全データ再学習の工程が入っていないだけとみられる。
  hokkaido は best_iteration 本数で全データを学び直した推論用フルモデルを別に持つ。
- **擬似実験** (学習終端をずらし、本番方式 vs 全期間再学習を直後 3 か月の同じ窓で比較、3 着内 logloss):
  終端 2026-05-01 → 評価 05-01〜07-31 (14,807 行): 本番方式 0.5142 / 全期間 0.5122 (−0.0020)、AUC 0.8151 → 0.8167。
  終端 2026-08-01 → 評価 08-01〜11-06 (15,408 行): 0.5097 / 0.5080 (−0.0017)、AUC 0.8178 → 0.8191。
  試走あり版 (同じ 08-01 終端): 0.5009 / 0.5008 (−0.0001)。本数を行数比で増やした版 (×1.11) との差は ±0.0001。
- **`ml/shadow_full.py` 新設**: 全期間版 (本番と同じ特徴量) と 試走あり×全期間版 を、本番と同じ手順 (train_full_v3 の
  早期打ち切り) で決まった best_iteration 本数で全行学習し、`data/shadow_full_model.lgb` /
  `data/shadow_full_trial_model.lgb` (+ meta) に保存 (本番・試走シャドーとは別ファイル)。初回: 185 本 / 246 本、293,189 行。
  週次再学習の任意ステップに追加 (失敗しても本番に影響なし)。
- **発火時の記録** (`daily_predict.py` +8 行、試走シャドーの直後・購入処理の後、try/except): 別ファイル
  `data/shadow_full_preds.csv` に pred_prod / pred_full / pred_full_trial / trial_status を記録 (既存の
  `shadow_trial_preds.csv` の列は変えない)。所要: 試走シャドーと合わせて 1 発火あたり 169〜228 ms (初回ロード込み、4 回実測)。
- **週次メール** (🎯 予測精度の計器): 前向きに記録した同じ R (試走あり R) で 本番 / 試走シャドー / 全期間 / 試走×全期間 の
  logloss を 1 行。4 週分で「判断材料が揃った」。過去の窓は全期間版が学習済みなので比較に使わない。
- tests: `tests/test_shadow_full.py` (9 件)

## 2026-11-12 (4) — 精度検証 Auto C/E: 試走ありシャドー・採用ゲート v3・walk-forward 楽観の注記 (本番の予測値・発注は不変)

- **試走ありの直前モデルをシャドーで併走** (`ml/shadow_trial.py` 新設): 本番と同じデータ・分割・パラメータで、
  試走タイム関連 6 列だけを特徴量に戻したモデルを `data/shadow_trial_model.lgb` / `shadow_trial_meta.json` に保存
  (本番 `data/production_*` は触らない)。初回学習: 比較窓 (2026-08-05〜, 14,762 行) の logloss シャドー 0.5007 /
  本番 0.5090、AUC 0.8239 / 0.8182 (両モデルとも学習終端 5/4 で窓は学習外)。
  - 発火経路 (`daily_predict.py`、+12 行): 購入処理 (auto_buy) の**後**に、本番の予測と並べて
    `data/shadow_trial_preds.csv` に記録 (pred_prod / pred_calib_prod / pred_shadow / 試走の取得状況 trial_status =
    ok / partial / none)。try/except で囲み、本番の pred / pred_calib / EV / 発注には一切使わない。
    実測: モデル読み込み込み 58 ms (初回) / 44 ms (2 回目) / 1 R。
  - 週次再学習 (`scripts/weekly_retrain.py`): 本番の 3 ステップの後に任意ステップとして学習。失敗はログのみで
    終了コード・通知・本番の採用に影響しない。
  - 週次メール (`weekly_status.py` 🧠 の下に「🎯 予測精度の計器」): 試走の取得率と、同じ R での本番 vs シャドーの
    logloss を 1 行ずつ。4 週分たまったら「本番化の判断材料が揃った」(本番化は利用者の判断)。
  - 参考: odds_ts (7/5〜11/12, 2,779 R) で T-4 / T-3 時点に全車の試走タイムが揃っていた R は 99.5%。
- **採用ゲート v3** (`ml/train_production.py`): (a) ROI 拒否権を運用どおり**複勝 (fns)** の top1 で計算
  (旧: 単勝 tns)。(b) 末尾 10% を早期終了用 (q0.90〜0.95) と**比較用の窓** (q0.95〜) に分け、精度比較は比較用の窓で行う
  (学習に使う行は従来と同じ)。(c) 現役の学習終端が比較窓に食い込む場合は v3 比較をしない (v2 へフォールバック。
  旧 meta は同じ規則で学習終端を再現、新 meta は train_date_max を記録)。(d) **logloss を主指標** (候補が現役 +0.0015 以内で OK、
  現役 35 日超なら +0.005 以内で WARN 採用、それ以外 NG)。AUC は併記。roi_gate.py の式は不変。
  retrain_history に gate_mode / cmp_* / roi_bet_type 列。
  再採点: git に残る 6/28〜11/08 の本番モデル 17 版の連続 16 ペアを v3 で採点し直し、**16/16 が OK (判定は変わらない)**。
  複勝 ROI 拒否権も 16/16 OK (dROI −1.05〜+1.41pt、CI はすべて 0 をまたぐ)。
- **walk-forward の楽観の注記**: 学習の odds 特徴は確定オッズ、本番は発火時オッズのため、walk-forward / 学習時の
  検証値は前向きより logloss で 0.013〜0.02 楽観的 (2026-05〜10 の月別比較)。週次メールの計器欄と CLAUDE.md に注記。
  発火時オッズ版のシャドーは見送り: 発火時点のオッズは odds_snapshots (発火した R のみ 4/30〜, 約 4,200 R) と
  odds_ts (全 R, 7/5〜, 約 2,800 R、月 約 650 R) しかなく、本番の学習 (約 26 万行 = 約 3.5 万 R) に遠い。
  odds_ts が 1 年分 (約 7,800 R) たまる 2027-07 頃に再評価。
- 発見: 本番モデルは日付の 90% 分位より前だけで学習しており、直近 約 6 か月 (例 11/08 版は 5/4 以降) を学習に使っていない
  (末尾 10% は早期終了と比較のみ)。変更はしていない (要検討)。
- tests: `tests/test_shadow_trial_gate_v3.py` (16 件。シャドー無し・破損・特徴量欠け・予測例外・書込不可で本番値が不変)

## 2026-11-12 (3) — 自動発注の堅牢化: 連続失敗停止の持ち越し・発注台帳・冪等キー・二重発火・結果不明の照合 (総合検証 指摘9〜13 と残る弱点)

- **指摘9 連続失敗の停止が 0:00 で自動解除**: 失敗・結果不明が 3 回続くと
  `data/consecutive_failure_stop.flag` (対象 R の JSON) を書き、自動発注を全券種停止 (sticky)。
  連続失敗のカウント・対象 R・発動済みの印は日付をまたいで持ち越す。解除は (a) 人がフラグを削除
  (次の run が検知してカウントを 0 に戻す) か (b) 翌 02:30 の注文履歴取得の後、対象 R すべての成立有無が
  bet_history で確定した時 (`release_failure_stop_if_confirmed`、`daily_fetch_order_history.py` から呼ぶ。
  run mutex を取ってから state を更新、解除メールに成立/不成立の一覧)。フラグを書けなかった場合は
  持ち越したカウントで `skip_failures` が続く (fail-closed)。停止中の通知は 1 日 1 回。
  失敗・結果不明・停止のメール送信に失敗したら `data/auto_buy_pending_mail.jsonl` に積み、
  次回起動時 (daily_predict の起動時 / run_auto_buy) に再送。`_notify` は成否を返すようにした。
- **指摘10 冪等キー**: 場_R番号 + 開催日。開催日の無い旧記録はラベルだけで一致 (安全側)。発注記録に
  race_date / place_code / race_no を持たせた。
- **指摘11 夜次が単日上書きの state に依存**: 発注記録を追記型台帳 `data/auto_buy_ledger.jsonl` にも残し
  (save_state の中で未記帳分を追記)、日付が変わる時に前日の state を `data/auto_buy_state_archive/<date>.json`
  に退避。夜次は state・退避・台帳を重複除去して読む (`night_report.load_exec_records`)。state の形式は互換。
- **二重発火の弱点**: 後続 run が `skip_lock` の時、先行 run が同じ R を処理中 (state の `inflight`、10 分で失効)
  なら `skip_lock_inflight` (手動購入ボタン付き候補メールを出さない)。click 前の失敗 (`failed`) の再試行は
  同一 R・1 日 1 回まで (`skip_retry_limit`)。`daily_predict_picks.csv` は同じ R・車を重複記録しない。
- **結果不明の自動照合**: 夜次で前日の `uncertain` R を bet_history と照合し「成立 / 不成立 / 未確認」を 1 行出す
  (`src/order_ledger.py`。翌日 02:00 以降に成功した注文履歴取得が無ければ「未確認」)。
- **buy_app**: 手動購入で exit 4 (結果不明) の時「結果不明 (投票履歴を確認)」と表示 (表示のみ)。
- **dynamic_scheduler**: AutoraceClient 初期化失敗・Hold/Today 取得失敗で要確認メール + exit 1 (マーカーなし)。
  取得成功で開催 0 件ならマーカーを書く。`daily_predict.fetch_today_schedule_ex` (失敗理由を返す) を追加
  (既存の `fetch_today_schedule` の戻り値は不変)。
- **指摘13 直前オッズデーモン**: Program/Print が取れた場は実レース数で打ち切り、存在しない R を推定補間しない
  (稼働中のデーモンは次回起動から反映)。
- **指摘12 ドキュメント**: CLAUDE.md の三連系対象場 (正本 `THREE_POINT_POLICY`)、タスク表 (OddsTsCollectorOnLogon・BuyApp)、
  `auto_buy.py` の docstring (ANYTIME 既定) を実装に合わせた。
- **pytest**: `pytest.ini` に `testpaths = tests` (素の `pytest` が Opinion/tmp_pip の PermissionError で失敗していた)。
  `tests/conftest.py` でテスト中の実メール送信を全面禁止 (この作業中、差し替え漏れのテストから
  要確認メールが 7 通誤送信されたため)。
- テスト追加: `test_auto_buy_hardening.py` (19)、`test_dynamic_scheduler_rescue.py` に 3 件。

## 2026-11-12 (2) — ログオン時の救済登録・未発火検知・週次 schtasks 判定・損失停止の明記 (総合検証 指摘4・5・7・8)

- **指摘4 ログオン時の再登録が全件 Access denied**: `AutoraceDynSchedulerOnLogon` (Interactive) は
  本体 `AutoraceDynamicScheduler` (Password 型) が作った `AutoraceDyn_*` を削除も上書きもできず、
  7/19 以降すべて空振り (rc=0・無通知) だった。`dynamic_scheduler.py` に登録成功マーカー
  `data/dynamic_scheduler_state.json` と起動時判定 (`startup_gate`) を追加:
  当日マーカーあり → 何もしない / 07:00 前 → 本体に任せる / 07:00 以降でマーカーなし → 登録 (救済)。
  両タスクは同一コマンドで区別できないため時刻とマーカーで振る舞いを決める。
  通常名が Access denied (英/日の文言) のときは、同名タスクの StartBoundary が同じ発火時刻なら既存を使い、
  そうでなければ別名 `<通常名>_alt` を XML で登録 (EndBoundary 発火+2h・DeleteExpiredTaskAfter PT1H)。
  判別できない失敗・別名でも失敗した R が 1 件でもあれば exit 2 + 要確認メール (マーカーは書かない)。
  ログ行は通常名で `registered AutoraceDyn_<場>_R<n> @ HH:MM (..., alt=...)` と書く (夜次が読む形式を維持)。
- **指摘5 未ログオン時の未発火を検知**: `night_report.load_unfired_races` が dynamic_scheduler.log の登録 R と
  daily_predict.log / dynamic_run.log の起動 R を突合し、未発火があれば件名 `⚠️未発火N` と本文注記
  (PC 未ログオンの可能性) を出す。発火予定 + 3 分が未到来の R (0 時以降のミッドナイト等) は数えない。
  実ログで 10/30 浜松 R1〜R9 の 9 件を検出することを確認。発火の仕組み (Interactive 登録) は変えていない。
- **指摘7 週次の常時 WARN**: `weekly_status.check_schtasks_health` を `evaluate_schtasks_rows` に分離。
  a6d15ad (自タスクの実行中のみ正常・6 時間超は固着) を土台に統合 (マージ時、下記)。
  監視対象を現行の定期タスク (完全一致) に更新 (削除済み Morning/Noon/
  EveningPredict を外し NightReport・OddsPrerace・OddsTsCollector・MonthlyReport・DynSchedulerOnLogon を追加)、
  常駐タスク (`RESIDENT_TASKS` = AutoraceOddsPrerace) の実行中と、OddsPrerace の 0x800710E0
  (稼働中への多重起動拒否) を正常扱い、NumberOfMissedRuns > 0 を
  「取りこぼし n 回」として NG に数える (PowerShell の Get-ScheduledTaskInfo を読み取りで実行)。
- **指摘8 当日損失停止**: 当日中は発動しないことを CLAUDE.md とコードのコメントに明記。判定は不変。
  当日上限が損失停止の閾値 (絶対値) を超える設定なら `run_auto_buy` 起動時に警告ログ
  (`auto_buy.config_warnings`、メールなし)。既定 (¥3,000 / −¥3,000) では出ない。
- テスト追加: `test_dynamic_scheduler_rescue.py` (10)、`test_night_unfired.py` (5)、
  `test_weekly_schtasks_health.py` (11、a6d15ad 統合後の仕様)、`test_auto_buy_config_warnings.py` (3)。

## 2026-11-12 — 自動発注に「結果不明 (uncertain)」判定を追加 (統合監視 総合検証 指摘1・2)

- **指摘1 タイムアウトで例外落ち**: `auto_buy._run_execute_purchase` がタイムアウト時だけ
  2 値を返し、呼び出し側の 3 値 unpack が ValueError で落ちていた (2026-07-26 川口 R9 で実発生。
  state 未記録・上限未計上・失敗メールなし・手動購入ボタン付き候補メール送信)。
  常に 3 値 `(status, detail, balance)` を返し、タイムアウトは結果不明とする。呼び出し側は
  `_normalize_exec_result` で値の個数に依存せず解釈し、発注呼び出しの例外も外へ漏らさない。
- **指摘2 click 後の失敗が failed 扱い**: `execute_purchase.py` は「投票する」click
  (注文が成立しうる最初の操作) の直前で `clicked=True` を立て、それ以降の例外
  (click 自体の失敗・失敗キーワード検出・まとめ買いの GraphQL 全一致不足・完了確認不能・想定外)
  を `PurchaseUncertain` に分類して **exit 4 (`EXIT_UNCERTAIN`)** で終える。click 前の失敗は従来どおり
  exit 1 (2 = 引数不正 / 3 = 停止フラグ)。2026-05-31 浜松 R5 は failed 判定なのに注文成立していた。
- **結果不明の扱い** (`auto_buy`, verdict `uncertain`): タイムアウト / exit 4 / 想定外の終了コード
  (強制終了など) / 発注呼び出しの例外。予定額を当日上限 `spent_yen` に計上 (買えた前提)、
  冪等ガード対象 (同じ R を当日再発注しない)、`consecutive_failures` +1 (既存の 3 回停止に乗せる)、
  警告メール「[AUTO-BUY] ⚠️ 場 R 計¥ 結果不明 (投票履歴を確認)」を 1 通。新しい sticky 停止は足していない。
- **候補メール**: `daily_predict.candidate_mail_decision` は結果不明を含む run では
  「⚠️自動発注できず (手動購入可)」の 1-click 購入ボタン付き候補メールを送らない
  (`AUTO_BUY_SEND_CANDIDATE_MAIL=1` でも送らない。二重購入防止)。click 前の `failed` は従来どおり送る。
- **夜次**: `night_report.load_uncertain_executions` で結果不明 R を件名 (`❓結果不明N`) と本文注記に出す。
  暫定精算・的中・投資には含めない (成立分は bet_history = 翌晩の前日確定欄が正)。
- **テスト**: `tests/test_auto_buy_uncertain.py` (22 件) を追加。`test_check_3point_health_strict_pair_scope`
  は再開基準日 (2026-11-03) 導入後に固定データ (7/1) が全除外されて落ちていたため、テスト側で
  since を切り、境界テスト `test_check_3point_health_killswitch_since_boundary` を追加 (判定ロジックは不変)。
- 未対応 (別途): buy_app の手動実行は exit 4 も従来どおり「投票失敗」と表示する。

## 2026-11-12 — 週次メールの自己 WARN を解消 (毎週月曜の誤 🟡)

週次レポートを生成している最中は `AutoraceWeeklyStatus` 自身が必ず rc=267009 (実行中) に
見える。これを schtasks の NG に数えていたため、全項目が正常でも 2026-08-10〜11-09 の
毎週月曜に件名が `🟡WARN (… health: schtasks NG 1 件)` になっていた (統合監視側の指摘)。
本物の警告と区別がつかず、🟡 の信頼度を下げていた。

- `classify_task_result` を新設し、**自タスクの実行中だけ**を正常扱いに
  (表示は `⏳ … (実行中 = 本レポート自身)`)。他タスクの実行中は「朝バッチが終わっていない」
  兆候なので従来どおり異常に数える。自タスクでも 6 時間を超えて実行中のままなら
  前回起動の固着として異常に残す。
- 判定基準ブロックの文言を実装に合わせた: schtasks の異常終了は 🟡WARN の条件
  (従来は 🔴NG の列挙に入っていたが、実装は当初から WARN)。自タスク除外の規則も明記。
- テスト: `tests/test_weekly_status_self_task.py` (10 件)。

## 2026-11-11 — メール ROI 整合監査の是正 (仮想/実弾の表記・夜次の返還/日跨ぎ・取込窓)

統合監視側の監査 (「通知ごとに ROI が違う」) で、計算自体は一致していたが
同じ語で別物を指していた箇所と実際の誤り 8 件 (C-1〜C-8) を是正。

- **C-1 「本番運用累積成績」は仮想値だった**: 候補メール・週次の見出しを
  「推奨(仮想) 複勝top1 ¥100均一 累積」に改め、同じ枠に「実購入 通算 (実弾・全券種・実円)」
  (bet_history 全期間) を並べた (`daily_predict.render_cumulative_*`、月次の 2 本立て表記に揃えた)。
  例: 旧「📊 本番運用累積成績 … ROI 101.2%」→ 新「📊 推奨(仮想) … ROI 100.9%」+「💰 実購入 通算 … ROI 92.0%」。
- **C-5 週次の期間・母集団の明記**: 「通知候補 監査 [仮想・複勝top1 ¥100均一・送信時刻基準]
  (直近 7 日: 送信 YYYY-MM-DD 〜 現在)」「実購入損益 [実弾・全券種・レース日基準] 直近7日: A 〜 B」。
  実購入の全期間は「〜YYYY-MM-DD 取込分」と締め日を付けた。
- **C-6 仮想の定義を統合監視と一致**: `picks_audit` の重複除去キーを (race, car) に
  (5/28 山陽 R4/R5 がバッチ名違いで二重計上されていた)、払戻行が空の不成立レース
  (5/21 山陽 R8・6/27 伊勢崎 R4) は外れ扱いせず除外。通算 710 件 / ROI 100.76% →
  706 件 / 100.95% で統合監視 (706 / 払戻 71,270) と一致。
- **C-3 夜次の暫定精算が返還を数えていなかった**: `settle_bet_detail` を追加し、
  欠車 (`refundInfo.absent`)・不成立 (typeCode 3)・特払を払戻に計上 (返還は的中に数えない)。
  11/06 は暫定 ¥-900 → ¥-600 (確定と一致) を再現。
- **C-4 日跨ぎ**: 夜次の本日集計を race_date 基準に。auto_buy_state は購入時刻の日付で
  記録されるため、発売 0:00〜4:59 の発注は前日開催のミッドナイトとして本日から除外し
  前日確定欄に任せる (11/10 夜次で 11/09 開催の飯塚 R8 が ⏳残1 になっていた)。
  00:00 以降発走の当日ミッドナイト R は dynamic_scheduler.log から拾って本文に明示。
  auto_buy_state の記録日付・当日上限・タスク発火時刻は変更していない。
- **C-8 夜次の件名・文言**: 発注ゼロは「⚪ 購入なし」(旧「🔴 暫定ROI=0% (0/0)」)、
  ±0 は「⚪ … ¥±0」「収支トントン」(旧「🟢 … ¥+0」「でプラス」)、全件結果待ちは「⏳ 結果待ち nR」。
  累積は「〜YYYY-MM-DD 取込分」。件名の外形 `[autorace] 🌙 夜次 <日付> …` は維持。
- **C-7 三連系の全期間累積 2 通り**: 表示ラベルだけ区別 (判定指標「全期間 (返還込み)」、
  停止基準 ⑤「全場・全期間累積 (返還除く・停止判定用)」)。`src/backstop.py` の判定は不変。
- **C-2 10/14 の欠落と再発防止**: 10/15〜16 に実行が無く、固定 2 日窓 (`--catchup 2` /
  `--since 2d`) から 10/14 が漏れて、実弾 4R (¥400) と結果・払戻が未取込だった。
  `daily_ingest` は manifest 上の前回成功日の翌日まで、`daily_fetch_order_history` は
  ログ上の前回成功日まで窓を自動で広げる (最低 2 日・上限 31 日)。10/14 分は別コミットで補填。
- テスト: `tests/test_mail_roi_consistency.py` (返還計上・11/06 再現・日跨ぎ・件名・重複除去・取込窓)。

## 2026-08-05 — 週次メール書式契約 v1 対応 + 直前コミット 7fddd50 の記録訂正

統合監視 7PJ 突合の指摘 P1/P3 に対応 (実体は `7fddd50` に含まれる)。
横断仕様は `Public-Race-ManagementｰSystem/docs/weekly_mail_spec.md` (書式契約 v1)。

- **P1 総合状態の一元化**: 従来は「件名」「HTML 本文 h2」「死活監視セクション」が
  それぞれ別ロジックで状態を出しており、死活監視 WARN の週に
  **件名 🟡WARN / 本文 🟢正常** が実際に起こり得た。
  `compute_overall_status()` を新設し 3 箇所が同一 dict の `badge` を共有。
  第4状態 `?` を廃止し 🟢OK/🟡WARN/🔴NG の 3 段階に統一 (材料不足は WARN に倒す)。
- **P3 style 二重指定**: `render_html` の日別マトリクス 5 箇所で
  `TD_R`/`TD_L` 定数が既に `style="..."` を含むのに再度 `style="..."` を付けており、
  HTML の先勝ちルールで色・太字が無効化されていた。`td()` ヘルパーに集約し、
  未使用になった定数を削除して再発を構造的に防止。
- 節番号 (P2) はこの PJ には元から無く、共有部品が日次メールにも同じ見出しを出すため
  週次都合の採番は見送り。代わりに HTML/plaintext のセクション順一致を機械チェック化。

### 7fddd50 のコミットメッセージに関する訂正

`7fddd50` は並行セッションが作業ツリーをスナップショットして main へ直接 commit + push
したもので、記述に 2 点の不正確さがある。**コード自体は検証済みの内容とバイト単位で一致**
しているため、公開済み履歴は書き換えずここに訂正を残す。

1. 「**既知の未達: 検証 7 (セクション順不一致)**」「並び順の統一は次ラウンドで対応」→ **誤り**。
   スナップショット時点の途中状態を記述したもので、その後セクション順は修正され
   検証 7 は PASS 済み (11 セクションが HTML/plaintext で完全一致)。
2. `data/bet_history.csv` / `data/bet_history_detail.csv` (各 +6 行) が同梱されている。
   これは 8/2〜8/3 の**実弾ベット履歴という正当な実データ**で、通常は `chore(data)` として
   単独コミットしている分。revert すると実記録を失うため**そのまま残す**。

## 2026-07-12 (10) — 購入ゲート: GATE_BROKEN も best-effort write に — 全ケースで halt を必ず永続化

(9) への Codex 第9R判定で残った最後の 1 点。`GATE_BROKEN` の「非write でも安全」は**時間方向に
成立しない**。broken には非Windows だけでなく「一時的な CreateMutexW 失敗・異常 rc・例外」も
含まれ、「全プロセスで恒久的に購入不能」を保証しない。反例: WAIT_ABANDONED 検知 → 生成
プロセスだけ一時的に broken → 非write で run mutex 解放 → 障害回復後や別 click プロセスでは
正常 → 後続 click がゲート取得・検査通過し sticky halt 無しで購入再開。

- **`auto_buy.py` の縮退経路を統一**: `GATE_BROKEN` を「非write」から、総上限超過時と
  **同じ best-effort write** に変更。broken でも `_write_abandoned_flag()` を実行して
  sticky halt を永続化する (実行中 click との完全原子性は broken/総上限では保証できないが、
  後続購入停止=halt 永続化を優先する縮退方針)。log / flag_note に 'best-effort' を明示。
- これで **全ケースで halt が必ず残る**:
  - `GATE_OK` → ゲート保持下で原子的 write。
  - `GATE_TIMEOUT`(競合) → 再試行して取得後にゲート下で原子的 write。
  - `GATE_BROKEN` → best-effort write (縮退)。
  - 総上限超過 → best-effort write (縮退・最後の砦)。
- (9) までの「broken は非write が安全」という論法 (コメント/docstring/CHANGELOG) は撤回し、
  「broken/総上限は best-effort で必ず halt を永続化する」に書き換えた。
- `tests/test_final_gate_recheck.py`: `test_generator_broken_best_effort_writes_and_ok_writes`
  に更新 — broken → フラグ生成される・通知に 'best-effort' 明示・ゲート未保持なので
  release は呼ばない / GATE_OK は従来どおりゲート下 write。総上限超過テストは維持。
- 回帰テスト 66 本全緑 (実発注は禁止スタブ/fake、通知スタブ、フラグ・mutex 名は
  テスト用に一時分離)。`data/rt3_backstop_stop.flag` は不変 (SHA256 一致)。

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
