# セッション引継ぎ（ノートPC → 自宅PC）

更新: 2026-04-26 / 元セッション: Claude Code on ノートPC

## 自宅 PC で最初にやること

```bash
cd <auto-racing-ai repo path>
git pull origin main
pip install -r requirements.txt
```

## 完了したこと

1. ✓ autorace.jp URL/API 構造調査 → JSON API 直接取得
2. ✓ `src/client.py` — API クライアント（CSRF自動取得、419リカバリ、リトライ/指数バックオフ）
3. ✓ `smoke_test.py` — 飯塚 2026-04-24 全12R 取得成功（39ファイル、2MB）
4. ✓ `src/parser.py` — JSON → CSV フラット変換（全角→半角、NULL正規化含む）
5. ✓ `src/storage.py` — CSV 読み書き（重複検知付き）
6. ✓ `ingest_day.py` — 1日分の全データ取得→CSV保存（開催なし日は1コールで即スキップ）
7. ✓ `backfill.py` — 5年分バックフィル（中断再開対応、進捗追跡）
8. ✓ 飯塚 2日分（4/24, 4/25）で動作確認済み

## 確定方針

| 項目 | 決定 |
|---|---|
| DB | **ローカル CSV**（data/ 配下。Supabase は Free 枠の都合で見送り） |
| バックフィル範囲 | **直近 5 年**（2021-04-26 〜 現在） |
| データ保管PC | **自宅PC**（CSV は git に含まれない。分析も自宅PCで行う） |

## 次にやること: バックフィル実行

```bash
python backfill.py
```

これだけで 2021-04-26 〜 今日 × 5場の全データ取得が始まる。

- 所要時間: 約33時間（0.5秒間隔）
- Ctrl+C で中断 → 再度 `python backfill.py` で続きから再開
- 進捗: `data/backfill_done.txt` に記録、`data/backfill_stats.json` に統計
- 既投入分（飯塚 4/24, 4/25）は自動スキップ

## CSV ファイル構成（data/）

| ファイル | 内容 | 1日あたり行数 |
|---------|------|-------------|
| race_entries.csv | 出走表（選手・ハンデ・試走タイム等） | 96 (12R×8車) |
| race_stats.csv | 選手集計成績（90d/180d/通算） | 96 |
| race_results.csv | 結果（着順・タイム・事故コード） | 96 |
| odds_summary.csv | 単勝/複勝オッズ + 平均値 | 96 |
| payouts.csv | 払戻金（7券種） | ~132 |
| race_laps.csv | 周回ランク変動（ML特徴量用） | ~672 |

5年分完了時の推定サイズ: 200-300MB

## バックフィル後の次ステップ

1. データ品質チェック（欠損率・エラー率の確認）
2. pandas で基本統計・可視化
3. ML 特徴量設計 → LightGBM（boat-racing-ai から流用）
4. walk-forward 検証

## 重要メモ

- 場コード: 2=川口, 3=伊勢崎, 4=浜松, 5=飯塚, 6=山陽
- 開催なしの日は Program R1 で1コール判定、即スキップ
- 失格系（order≧9）は着順 NULL に変換済み
- API の typo `ohter`（other）はそのまま保持（parser で対応済み）

## git 状態

- ブランチ: `main`
- 最新コミット: `50c2713`
- リモート: https://github.com/Tower2007/auto-racing-ai.git
