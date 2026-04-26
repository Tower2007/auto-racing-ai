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
  client.py      # autorace.jp API クライアント
  parser.py      # JSON → CSV 用フラット dict 変換
  storage.py     # CSV 読み書き (data/ 配下)
smoke_test.py    # 1日分スモークテスト (JSON 保存)
ingest_day.py    # 1日分データ取得 → CSV 保存
backfill.py      # 過去データ一括取得（将来）
data/            # CSV + スモーク JSON (.gitignore)
docs/            # 調査結果
```

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
