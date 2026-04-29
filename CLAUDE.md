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

| タスク | 時刻 | 内容 |
|---|---|---|
| `AutoraceDailyIngest` | 毎日 06:30 | データ収集 (catchup 2 日) |
| `AutoraceMorningPredict` | 毎日 08:00 | `--venues 2 3 4 5 6 --time-slot morning`(liveStart < 13:00 の場のみ) |
| `AutoraceNoonPredict` | 毎日 13:00 | `--venues 2 3 4 5 6 --time-slot noon`(13:00-17:00) |
| `AutoraceEveningPredict` | 毎日 17:00 | `--venues 2 3 4 5 6 --time-slot evening`(>= 17:00 の場) |
| `AutoraceWeeklyRetrain` | 毎日曜 03:00 | 本番モデル再学習 |
| `AutoraceWeeklyStatus` | 毎月曜 07:30 | 週次ステータス報告 |

戦略仕様: `docs/ev_strategy_findings.md` 参照(thr=1.50、中間モデル、複勝 top-1)。
全 5 場(川口/伊勢崎/浜松/飯塚/山陽)を全 slot に登録、`--time-slot` で Hold/Today の
`liveStartTime` を見て朝/昼/夕タスクに動的振り分け。場ごとに開催形態(通常/ナイター/
ミッドナイト)が変わっても `liveStartTime` で自動追従するため取り逃がしなし。
該当場なし時は空打ちメールを抑止。賭け運用は手動投票(自動投票は ToS グレーで非実施)。

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
