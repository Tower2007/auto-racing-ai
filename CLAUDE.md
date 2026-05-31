# auto-racing-ai 運用ガイド

## AI 協働体制 (2026-04-30 導入)

複数 AI で意見を出し合って改善していく方針。詳細は各ガイド:
- `AGENTS.md` — Codex 用(分析・代替案、`Opinion/` のみ編集可、常任)
- `GEMINI.md` — Gemini 用(大局観・第三者レビュー、`Opinion/` のみ編集可、スポット)
- `Opinion/README.md` — 意見ファイル運用ルール
- 意見ログ: `Opinion/{ClaudeFeedback,CodexOpinion,GeminiOpinion}.md`

Claude(自分)は実装本体・コード編集・git 操作を担当。他 AI の意見はユーザーが
明示的に「○○の意見も読んで」と指示した時だけ参照する。

姉妹プロジェクト keiba / boat-racing-ai と同じ枠組み。boat とは EV 戦略の往復実績あり
(`Boat_racing_AI/HANDOFF_FROM_AUTORACE.md` 経由)。

### 数値判断のチェックポイント(2026-05-04 Gemini oversight より)

**期待値から大きく外れるセンセーショナルな数値が出た時は、まず測定スクリプトの
バグを疑う**。R1-R3 のサイクル(`Opinion/CodexOpinion.md` 2026-04-30 / 05-04)で、
「ROI 132% → 67% に転落」と思った数字が `odds_snapshot_eval.py` の P1 バグ
(未確定レースを 0 払戻として混入)による過小評価で、実際は ROI 105.0% だった
事例あり。**docs / 運用判断に直結する eval スクリプトは、結果を信用する前に
ロジックを再読する**。

重要意思決定の根拠となるコード(scripts/ev_*.py のうち docs に数字を載せる
スクリプト、本番モデル選定に使う比較スクリプト等)には、簡易的な単体テストや
sanity check(Mock データでの ROI 検算等)を **推奨**(義務化はしない)。

---

## プロジェクト概要

オートレース（autorace.jp）のデータ蓄積・可視化・ML検証アプリ。
娯楽/研究用途（賭け運用は非推奨、控除率30%）。

## 技術スタック

- Python 3.13
- DB: ローカル CSV（data/ 配下）
- データ取得: autorace.jp JSON API（HTML スクレイピング不要）
- ML: LightGBM（将来）

## ディレクトリ構成

```
src/
  client.py            # autorace.jp API クライアント
  parser.py            # JSON → CSV 用フラット dict 変換
  storage.py           # CSV 読み書き (data/ 配下)
ml/
  features.py          # 6 CSV → ml_features.parquet
  train.py             # holdout 評価
  walkforward.py       # 月次 walk-forward
  walkforward_morning.py  # 中間モデル(試走なし・オッズあり)
  walkforward_preday.py   # 前日モデル(両方なし)
  train_production.py  # 本番モデル + isotonic 校正(週次再学習)
smoke_test.py          # 1日分スモークテスト (JSON 保存)
ingest_day.py          # 1日分データ取得 → CSV 保存
backfill.py            # 過去データ一括取得
daily_ingest.py        # 日次データ収集オーケストレータ(catchup 2)
daily_predict.py       # 当日対象場の EV ベース買い候補メール送信
                       # (--races / --suppress-noresult-email 対応で 1R 単位呼出可)
dynamic_scheduler.py   # 各レース発走 LEAD_MIN (現在 5) 分前に daily_predict を
                       # 1R 単位で起動する schtasks one-shot を毎朝生成
weekly_status.py       # 週次ステータスメール
gmail_notify.py        # Gmail SMTP 送信
scripts/
  ev_*.py              # EV 戦略 5 段階検証
  daily_pnl_*.py       # 場・期間別 P&L
  fix_*.py / dq_*.py   # データ品質チェック・修正
  auto_login_autorace.py  # vote.autorace.jp 自動ログイン (Playwright)
  buy_token.py         # click-to-buy HMAC token sign/verify
  execute_purchase.py  # Playwright で投票実行 (--dry-run default)
  inspect_login_form.py / inspect_purchase_page.py  # 構造調査用 helper
app/
  streamlit_app.py     # 予想表示 (port 8501)
  buy_app.py           # click-to-buy 確認 UI (port 8502)
data/                  # CSV + production_*.lgb/.pkl/.json (.gitignore)
docs/                  # 調査結果・戦略まとめ
reports/               # 各種分析レポート(commit 対象)
```

## 自動運用タスク(Phase A: 推奨提示型)

### per-race 動的発火方式(2026-04-30〜)

| タスク | 時刻 | 内容 |
|---|---|---|
| `AutoraceDailyIngest` | 毎日 06:30 | データ収集 (catchup 2 日) |
| `AutoraceDynamicScheduler` | 毎日 07:00 | `python dynamic_scheduler.py`: Program/Print ページから各場 R 毎の発走時刻を取得し、各レース発走 `LEAD_MIN` 分前 (現在 4 分前) の `AutoraceDyn_{venue}_R{n}` one-shot を 12 R × 場数ぶん登録(冪等、毎日再生成) |
| `AutoraceDyn_{venue}_R{n}` | 各レース発走 LEAD_MIN 分前(現在 4 分前、動的) | `python daily_predict.py --venues {pc} --races {n} --suppress-noresult-email`: 1 R 単位で予測、候補ありのみメール送信。near-miss retry 廃止、処理 ~10 秒で締切 ~2 分前に到着 |
| `AutoraceWeeklyRetrain` | 毎日曜 03:00 | 本番モデル再学習 |
| `AutoraceWeeklyStatus` | 毎月曜 07:20 | 週次ステータス報告 |
| `AutoraceFetchOrderHistory` | 毎日 02:30 | `python scripts/daily_fetch_order_history.py`: vote.autorace.jp の購入履歴を `--since 2d --detail --cookie-source playwright` で取得し `data/bet_history.csv` / `bet_history_detail.csv` にマージ。失敗時のみ Gmail 通知。**2026-05-08 から Playwright auto-login** に切替(SBI IPO project と同じパターン)。資格情報は `accounts.json`(.gitignore)。実装: `scripts/auto_login_autorace.py`。旧 Firefox cookie 方式は `--cookie-source firefox` で fallback 可。経緯: memory `ml_baseline_findings.md` 2026-05-08 |

#### 設計
- 発走時刻取得: `/race_info/Program/Print/{venueKey}/{YYYY-MM-DD}` から R 毎の発走予定時刻を HTML スクレイプ。12 R 全て掲載されるので推定ではなく実時刻ベースで登録。
  - 取得失敗時のみ fallback: Hold/Today の `(nowRaceNo, raceStartTime)` を anchor、`liveEndTime − 5 min` を R12 とした線形補間。`liveStartTime`(放送開始、R1 より約 30 分早い)は更なる fallback。
  - 深夜跨ぎ(R 番号順に時刻が前 R より早くなる)は +1 日として処理。
- 各レース発走 `LEAD_MIN` 分前 (現在 4 分前、`dynamic_scheduler.py:LEAD_MIN`) で one-shot 発火 → そのレースの 1 R 分だけ predict
  - LEAD_MIN は 30→15→10→5→2→4 と変遷。2 分前 (2026-05-14〜05-17) では処理+送信で締切ギリギリに到着する問題が発生。4 分前に戻し near-miss retry を廃止することで通知が締切 ~2 分前に安定到着。drift は 5 分前時 (-20%) より軽微と判断。
- `--suppress-noresult-email`: 候補なしの R はメールスキップ(候補ありの R のみ通知)
- 当日中止・全 fallback 失敗の場は登録スキップ
- 冪等: 既存 `AutoraceDyn_*` を全削除してから再登録、同日中の手動再走 OK

#### 旧 fixed-slot 方式(参考、2026-04-30 まで)
朝 10:00 / 昼 13:00 / 夕 17:00 の 3 固定 task で `--time-slot` フィルタ。
- 問題: 09:00 はオッズ未公開 → 10:00 に変更 → それでも morning slot 後半 R(11:00–13:00 開始)で odds 薄く NaN → 取りこぼし発生
- 動的方式に置換。`AutoraceMorningPredict` / `NoonPredict` / `EveningPredict` は 動的稼働確認後に disable / 削除予定

戦略仕様: `docs/ev_strategy_findings.md` 参照(thr=1.50、中間モデル、複勝 top-1)。
三連系 (rt3 + rf3) 推奨: 浜松(4) + 山陽(6) 限定、ev_avg_calib >= 1.80 で pred top-3 の
三連単 1 点 + 三連複 1 点を追加推奨 (2026-05-29 導入、paper 記録 `data/rt3_paper.csv`)。
過去検証 `scripts/ev_3point_by_place.py` thr=1.80:
rt3 浜松 ROI 530% / 山陽 ROI 141%、rf3 浜松 ROI 330% / 山陽 ROI 185%。

三連系まとめ買い click-to-buy (2026-05-30 本番稼働): 浜松・山陽 EV>=1.80 のメールに
「💰 3点購入」ボタンを出し、1 click で 複勝(推奨額)+三連単(¥100)+三連複(¥100) を
まとめて投票。`daily_predict.py:RT3_BUY_ENABLED` で on/off。実装は
`execute_purchase.py --bets-json`(複数券種をシート追加→1回投票、三連複=BOX列)、
`buy_token.py` の bets payload、`buy_app.py` の 3 券種表示。投票前にカート全削除
(モーダル OK)→確認画面で N組/合計額/各出目を構造検証→投票後 GraphQL で全 bet 照合。
浜松 R7 で本番テスト成功 (¥500 投票受付完了) 済。

自動投票 Phase 1 (2026-05-31 導入、`auto_buy.py`): 機会損失対策の自動発注。
`AUTO_BUY_ENABLED` (デフォルト OFF) が True の時のみ、daily_predict のメール送信
直前に厳格ガード (1日上限¥2000 / 当日損失-¥2000停止 / EV異常>10除外 / 連続失敗3回停止)
を全通過した候補を `execute_purchase.py` で自動投票。state は `data/auto_buy_state.json`
(atomic write・日次 reset)、**毎回 Gmail 即時通知 (券種・出目・金額を日本語で明記)**。
2026-05-31 ユーザー要望で **`AUTO_BUY_ANYTIME=True` (デフォルト) = 時間帯制限なし常時発注**。
False にすると夜間限定 (`AUTO_BUY_HOUR_START`/`END`、22-6時) に戻る。段階導入:
Week1 `AUTO_BUY_DRY_RUN=True` (判定のみ) → Week2 複勝のみ live →
Week3 `AUTO_BUY_INCLUDE_RT3=True` で三連系含む。設定は全て .env で上書き可。
ガード単体テスト `tests/test_auto_buy_guards.py` (10/10)。
ToS グレー (約定書13条「自ら申込む」) をユーザー承知の上で進行。
依頼書: `Opinion/codex_briefs/2026-05-31_auto_buy_phase1.md`。
場ごとに開催形態(通常/ナイター/ミッドナイト)が変わっても `liveStartTime` / `liveEndTime`
で自動追従するため取り逃がしなし。賭け運用は手動投票(自動投票は ToS グレーで非実施)。

## CSV ファイル構成 (data/)

| ファイル | 内容 | キー |
|---------|------|------|
| race_entries.csv | 出走表 | race_date + place_code + race_no + car_no |
| race_stats.csv | 選手集計成績 (90d/180d/通算) | 同上 |
| race_results.csv | レース結果 | 同上 |
| race_laps.csv | 周回ランク変動 | race_date + place_code + race_no + lap_no + car_no |
| payouts.csv | 払戻金 (7券種) | race_date + place_code + race_no + bet_type |
| odds_summary.csv | 単勝/複勝オッズ + 平均値 | race_date + place_code + race_no + car_no |
| bet_history.csv | 購入履歴 R 単位サマリ (vote.autorace.jp) | date + place_code + race_no |
| bet_history_detail.csv | 購入履歴 券種別 pack 詳細 | date + place_code + race_no + order_id + bet_type_code + pack_deme |

## autorace.jp API メモ

- 全 POST は CSRF トークン必須（`client.py` が自動取得）
- 場コード: 2=川口, 3=伊勢崎, 4=浜松, 5=飯塚, 6=山陽
- 過去データ: 2006-10-15 以降
- リクエスト間隔: 0.5秒（`.env` の AUTORACE_REQUEST_DELAY_SEC）

## コーディング規約

- 出力言語: 日本語
- docstring: 日本語
- 変数名: snake_case (英語)
- 進捗ログ: ASCII のみ（Windows cp932 対策）
- finish_position=0 → NULL として保存
- 全角数字 → 半角に正規化

## 既知の注意点

- WinError 10035: リトライ/指数バックオフで対応
- CSRF 419: トークン再取得で自動リカバリ
- boat-racing-ai の教訓: walk-forward 検証必須、集計ROI に騙されない
