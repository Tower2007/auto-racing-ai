# オートレース予想プロジェクト 引継ぎ資料

このドキュメントは、姉妹プロジェクト `boat-racing-ai`(競艇予想アプリ)の開発で得た
知見・教訓を、新規プロジェクト **`auto-racing-ai`**(オートレース予想アプリ)に
引き継ぐためのものです。新 Claude セッションが最初にこれを読めば、過去の試行錯誤を
繰り返さずに済みます。

**作成日**: 2026-04-26
**作成元**: `C:\Users\no28a\Claude-project\Boat_racing_AI\`(boat-racing-ai v0.2.0時点)
**移植先**: `C:\Users\no28a\Claude-project\Auto_racing_AI\`(新規予定)

---

## 1. ユーザー情報(共通)

- **GitHub**: `Tower2007`(認証は `gh` CLI 経由)
- **Email**: `no28akira2007@gmail.com`
- **OS**: Windows 11(自宅PC + ノートPC の2台運用)
- **Python**: 3.13
- **Shell**: PowerShell 7+ / bash
- **作業スタイル**: Claude がコード書き、ユーザーがレビュー & git 操作
- **git 操作スキル**: status/diff/add/commit/log/restore/tag/push/pull は習得済、
  branch/merge/rebase は未学習
- **出力言語**: 日本語

## 2. boat-racing-ai の最終結論(2026-04-26)

**戦略撤退判定**:
- ML(LightGBM)+ オッズ込み で walk-forward 検証(2024-10〜2026-04, 19ヶ月)
- 全期間 ROI = **92.8%**(805 レース、-11,590 円)
- 月次安定性 = 8/19 月で ROI≧100%(42.1%)
- 公開情報(選手・モーター・展示・気象・オッズ)は全てオッズに織り込まれ済み
- 控除率 25% の壁を越える優位性は見つけられず

**重要な学び**:
- 「特定期間で 100% 超え」は walk-forward では幻だった
- ML feature importance 上位にオッズが入っても ROI は改善せず
- 訓練期間の長短で Test1/Test2 の成績が逆転 → 過学習&不安定
- 「予想精度」と「市場を打ち負かす」は別物

オートレースは **控除率 30%** とさらに厳しい(予想で勝つのは構造的に困難)。
**賭け運用は非推奨**。データ蓄積・予想表示・娯楽用としてのみ意味がある前提。

## 3. オートレース ドメイン基本仕様

### 場
- **5 場のみ**: 川口(01) / 伊勢崎(02) / 浜松(03) / 飯塚(04) / 山陽(05)
- (場コードは autorace.jp の URL パラメータで確認すること)

### レース
- 1日 12 レース(基本)
- 1レース **8車**(欠車で 6-8 になることあり)
- 距離: 場により異なる(500m / 600m 等)、ナイター場あり

### 選手
- 男女混合
- ランク: S級 / A級 / B級
- 試走タイム(直前情報、競艇の展示タイムに相当)

### 車・エンジン
- ハンデ戦あり(良走者ほど後ろからスタート、距離差 10m〜)
- エンジン特性データあり

### 券種(競艇と同じ7種類)
- 単勝 / 複勝 / 2連単 / 2連複 / ワイド / 3連単 / 3連複
- 競艇の「拡連複」≒ ワイド

### 控除率
- **約 30%**(競艇 25% より厳しい)

## 4. データソース

### autorace.jp(公式サイト)
- レース検索: `https://autorace.jp/race_info/SearchRace`
- **過去データは 2006年10月以降が公開**(これは大きい、20年近い蓄積可能)
- LZH 一括 DL は無し → **HTML スクレイピング前提**(競艇より工数大)
- 出走表・結果・払戻・オッズ それぞれ別ページのはず → URL 構造調査が最初の仕事

### 別ソース候補(調査推奨)
- `db.netkeiba.com` 系のオートレース DB?
- 民間予想サイト(参考程度)

## 5. 技術スタック推奨

### データ保存先: **SQLite ローカル**(boat-racing-ai と方針変更)

**理由**:
- Supabase Free tier は 2 active project まで(現在 keiba-db + Boat-db で枠埋まり)
- 賭けないなら Supabase の利点(共有・SQL アクセス)はオーバースペック
- ローカル SQLite なら容量無制限、両PCは OneDrive 同期で対応可能
- pandas で `pd.read_sql(sql, sqlite_conn)` でほぼ同じインターフェース

**実装**:
- ファイル: `C:\Users\no28a\Claude-project\Auto_racing_AI\autorace.db`
- OneDrive 配下なら自動 sync(or git で db ファイルは ignore、CSV エクスポートで sync)
- daily_ingest 等で `import sqlite3` を使う

**Supabase 使いたい場合**:
- INACTIVE の `Tower2007's Project` を削除して枠を空ける
- または Pro 課金($25/月)で 8 GB & 4 active

### 言語・ライブラリ
- Python 3.13
- pandas, numpy, lightgbm
- requests または urllib(boat-racing-ai は urllib 使用、`requests` の方が retry/session 管理楽)
- BeautifulSoup4 または lxml(HTML パース、競艇は LZH テキストだったので未使用)
- python-dotenv(.env 管理)

### Git / GitHub
- 新 repo: `Tower2007/auto-racing-ai`(Private)
- main ブランチ追跡

## 6. 推奨アーキテクチャ(boat-racing-ai 流用)

```
Auto_racing_AI/
├── CLAUDE.md                # 本プロジェクト用の運用ガイド(新規作成)
├── README.md
├── CHANGELOG.md
├── .gitignore
├── .env.example
├── schema.sql              # SQLite スキーマ
├── ingest.py               # DB I/O モジュール
├── parser.py               # HTML パーサ
├── scraper.py              # autorace.jp 取得
├── backfill.py             # 過去データ一括取得
├── daily_ingest.py         # 日次バッチ
├── weekly_status.py        # 週次レポート
├── gmail_notify.py         # Gmail SMTP 送信
├── ml_features.py          # ML 特徴量
├── ml_train.py             # LightGBM 訓練
├── ml_validate.py          # 期間別検証
├── ml_walkforward.py       # 月次 walk-forward
└── data/                   # autorace.db (sqlite) + ログ
```

ほぼ同じ構成。`scraper.py` は HTML スクレイピング用に新規実装が必要。

## 7. DB スキーマ案(SQLite)

boat-racing-ai のスキーマがほぼ流用可能。差分:

```sql
-- 競艇との主な違い
-- 1. lane_number → 1〜8 (8車)
-- 2. course → starting handicap (m)
-- 3. exhibition_time → trial_time (試走タイム)
-- 4. winning_technique は競艇とは違う種類
-- 5. UUID 主キーは sqlite では TEXT として保存

CREATE TABLE venues (
  venue_id INTEGER PRIMARY KEY,
  venue_code TEXT NOT NULL UNIQUE,
  venue_name TEXT NOT NULL,
  is_night INTEGER  -- ナイター場フラグ
);

CREATE TABLE racers (
  racer_id INTEGER PRIMARY KEY,
  racer_name TEXT NOT NULL,
  rank TEXT,             -- S/A/B
  branch TEXT,
  -- ...
);

CREATE TABLE races (
  race_id TEXT PRIMARY KEY,           -- 'YYYYMMDD-jcd-rno' 合成
  race_date TEXT NOT NULL,
  venue_id INTEGER NOT NULL,
  race_number INTEGER NOT NULL,
  grade TEXT,                          -- 'SG'/'GⅠ'/'GⅡ'/'一般'
  distance INTEGER,                    -- 500/600/...
  weather TEXT,
  track_condition TEXT,
  -- ...
);

CREATE TABLE race_entries (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  race_id TEXT NOT NULL,
  racer_id INTEGER NOT NULL,
  car_number INTEGER NOT NULL,         -- 1〜8 (枠番)
  handicap_meters INTEGER,             -- 0/10/20m
  trial_time REAL,                     -- 試走タイム
  finish_position INTEGER,
  -- オッズ系 (boat-racing-ai と同じ)
  odds_win REAL,
  odds_place_low REAL,
  odds_place_high REAL,
  popularity INTEGER,
  -- ...
  UNIQUE(race_id, car_number),
  FOREIGN KEY(race_id) REFERENCES races(race_id) ON DELETE CASCADE
);

CREATE TABLE payouts (
  payout_id INTEGER PRIMARY KEY AUTOINCREMENT,
  race_id TEXT NOT NULL,
  bet_type TEXT,
  combination TEXT,
  payout_amount INTEGER,
  popularity INTEGER,
  FOREIGN KEY(race_id) REFERENCES races(race_id) ON DELETE CASCADE
);
```

## 8. 過去の落とし穴(必ず避けよ)

### A. パーサ系
- **Shift-JIS vs UTF-8**: 公式サイトは UTF-8 が多いが、過去データに Shift-JIS 混在あり
- **全角数字 → 半角数字**: 必ず正規化(boat-racing-ai の `zen2han()` 流用可)
- **`finish_position = 0` 罠**: 失格・転倒等は NULL にすべき(boat-racing-ai では int(0) が混入した)

### B. オッズスクレイピング
- **過去オッズが取れる範囲**: ページ仕様により「開催の○ヶ月前まで」等の制限ある可能性
  → 早めに調査して限界を把握
- **HTML 表示上の "0.0"**: 100倍超の超大穴を 0.0 と表示することがある
  → NULL として保存し、複勝オッズで補完

### C. インフラ系
- **Windows console + emoji**: cp932 で UnicodeEncodeError → 進捗ログは ASCII で
- **DATABASE_URL の `@` 文字**: パスワードに `@` 含むと URL 解釈エラー
  → `%40` にエンコード、または個別フィールドで psycopg2 に渡す
- **socket WinError 10035**: スクレイピング中によく出る
  → 5回リトライ、指数バックオフ(boat-racing-ai の `_fetch()` 参考)

### D. ML 検証
- **集計 ROI に騙されない**: 必ず **walk-forward**(月次再訓練)で評価
- **訓練期間で結果逆転**: 6ヶ月 vs 18ヶ月で Test1/Test2 が逆転した実例あり
- **オッズ特徴量を入れても ROI 上がらない可能性大**(競艇では実証済)
- **検証目的を明確に**: 「予想精度向上」と「賭けで勝つ」は別物

### E. データ容量
- **最初から 1年 retention 設計**:
  - 後から削除すると VACUUM FULL で苦労する(boat-racing-ai で経験済)
  - daily_ingest に `prune_old_data()` を最初から組み込む
  - SQLite なら `VACUUM` でファイル縮小、autocommit 不要

## 9. 初日のチェックリスト

新セッションで auto-racing-ai を開始する場合の最初のステップ:

```
1. このファイル(HANDOFF_TO_AUTORACE.md)を全部読む
2. ユーザーと方針確認:
   - SQLite ローカル or Supabase?(推奨: SQLite)
   - 過去データ取得範囲?(2006年〜? 直近1年〜?)
   - GitHub repo 作成タイミング?
3. autorace.jp の構造調査(2-3 時間):
   - 出走表ページの URL 構造
   - 結果ページ
   - 払戻ページ
   - オッズページ(過去保持範囲)
   - HTML 構造のサンプル取得
4. CLAUDE.md を新規作成(boat-racing-ai のものを基に調整)
5. schema.sql 設計
6. parser.py / scraper.py の雛形
7. 1場1日分でスクレイピング → DB 投入の動作確認
8. backfill 計画(規模・所要時間)
9. ML パイプライン(boat-racing-ai のコードほぼ流用)
10. walk-forward で正直な評価
```

## 10. 流用したいコード(boat-racing-ai より)

新プロジェクトでファイルごとコピーして調整推奨:

| ファイル | 流用度 | 調整箇所 |
|---|---|---|
| `gmail_notify.py` | **そのまま使える** | なし |
| `weekly_status.py` | ほぼそのまま | DB 接続を SQLite に |
| `ml_features.py` | 高 | 競艇特有の列(展示等)→ オートレース特有(試走、ハンデ等)に置換 |
| `ml_train.py` | **そのまま** | なし |
| `ml_backtest.py` | 高 | 券種ロジックは同じ |
| `ml_validate.py` | **そのまま** | なし |
| `ml_walkforward.py` | **そのまま** | なし |
| `simulator.py` | 中 | LANE_SCORE → CAR_SCORE(8車対応)、ハンデ補正追加 |
| `daily_ingest.py` | 中 | LZH ではなく HTML スクレイピング、prune_old_data はそのまま |
| `parser.py` | 低 | LZH テキストパーサ → HTML パーサに完全書換 |
| `scraper.py`(新規) | - | autorace.jp 用に新規作成 |
| `backfill.py` | 中 | 構造は同じ、fetch_day を HTML スクレイピング化 |

## 11. boat-racing-ai 側のリポジトリ参照先

実コードの参照元:
- ローカル: `C:\Users\no28a\Claude-project\Boat_racing_AI\`
- GitHub: `Tower2007/boat-racing-ai`(Private、`gh repo view Tower2007/boat-racing-ai`)
- 最新タグ: `v0.2.0`(2026-04-26)
- CHANGELOG.md にアーキテクチャ進化の履歴あり

## 12. 期待値の心構え

- **「予想精度を上げて市場に勝つ」のは諦める**(boat-racing-ai で立証済)
- **ML はあくまで「市場の歪みを見つけるツール」として使う**
- **データ蓄積と可視化を主目的に、副産物として ML 検証**
- **賭けに直結するコード(自動購入・推奨メール等)は提案前にユーザー確認必須**
- **Walk-forward で正直に検証 → 結果が悪くても受け入れる**

---

このドキュメントを読んだ新セッションは、最初に
「**HANDOFF_TO_AUTORACE.md を読みました。○○の方針で進めて良いですか?**」
とユーザーに確認してから具体作業に入ること。
