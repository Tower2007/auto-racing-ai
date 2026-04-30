# auto-racing-ai 運用ガイド

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
dynamic_scheduler.py   # 各レース発走 30 分前に daily_predict を 1R 単位で
                       # 起動する schtasks one-shot を毎朝生成
weekly_status.py       # 週次ステータスメール
gmail_notify.py        # Gmail SMTP 送信
scripts/
  ev_*.py              # EV 戦略 5 段階検証
  daily_pnl_*.py       # 場・期間別 P&L
  fix_*.py / dq_*.py   # データ品質チェック・修正
data/                  # CSV + production_*.lgb/.pkl/.json (.gitignore)
docs/                  # 調査結果・戦略まとめ
reports/               # 各種分析レポート(commit 対象)
```

## 自動運用タスク(Phase A: 推奨提示型)

### per-race 動的発火方式(2026-04-30〜)

| タスク | 時刻 | 内容 |
|---|---|---|
| `AutoraceDailyIngest` | 毎日 06:30 | データ収集 (catchup 2 日) |
| `AutoraceDynamicScheduler` | 毎日 07:00 | `python dynamic_scheduler.py`: Hold/Today から各場の anchor(nowRaceNo の raceStartTime)と liveEndTime を取得し、各レース発走 30 分前の `AutoraceDyn_{venue}_R{n}` one-shot を 12 R × 場数ぶん登録(冪等、毎日再生成) |
| `AutoraceDyn_{venue}_R{n}` | 各レース発走 30 分前(動的) | `python daily_predict.py --venues {pc} --races {n} --suppress-noresult-email`: 1 R 単位で予測、候補ありのみメール送信 |
| `AutoraceWeeklyRetrain` | 毎日曜 03:00 | 本番モデル再学習 |
| `AutoraceWeeklyStatus` | 毎月曜 07:30 | 週次ステータス報告 |

#### 設計
- 発走時刻推定: anchor = `(nowRaceNo, raceStartTime)`(その場の現時点 R の実発走時刻)。`R12 ≒ liveEndTime − 5 min`(最終R終了→発走時刻補正)、間隔 = (R12 − anchor) / (12 − anchor_r)。通常 30-40 分。
  - `liveStartTime` は放送開始(R1 より約 30 分早い)で誤差源のため fallback のみ。
  - `raceStartTime` は schedule 進行とともに更新されるので、朝 07:00 起動時は R1 を、再走時は当時点の進行 R を anchor に取る。
- 各レース発走 30 分前で one-shot 発火 → そのレースの 1 R 分だけ predict
- `--suppress-noresult-email`: 候補なしの R はメールスキップ(候補ありの R のみ通知)
- 当日中止・anchor 取得失敗(raceStartTime/liveStartTime 共に欠落)の場は登録スキップ
- 冪等: 既存 `AutoraceDyn_*` を全削除してから再登録、同日中の手動再走 OK

#### 旧 fixed-slot 方式(参考、2026-04-30 まで)
朝 10:00 / 昼 13:00 / 夕 17:00 の 3 固定 task で `--time-slot` フィルタ。
- 問題: 09:00 はオッズ未公開 → 10:00 に変更 → それでも morning slot 後半 R(11:00–13:00 開始)で odds 薄く NaN → 取りこぼし発生
- 動的方式に置換。`AutoraceMorningPredict` / `NoonPredict` / `EveningPredict` は 動的稼働確認後に disable / 削除予定

戦略仕様: `docs/ev_strategy_findings.md` 参照(thr=1.50、中間モデル、複勝 top-1)。
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
