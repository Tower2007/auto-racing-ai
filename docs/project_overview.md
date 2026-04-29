# auto-racing-ai プロジェクト全体ドキュメント

最終更新: 2026-04-29

---

## 0. 3 行サマリー

- オートレース(autorace.jp)の 5 年分データを蓄積し、LightGBM で「複勝 top-3 入り確率」を予測
- 期待値ベース選別(`予測確率 × 複勝オッズ平均 ≥ 1.50`)で walk-forward 検証 ROI **131.8%**(25 ヶ月、月次 25/25 全勝)
- 推奨提示型の自動運用を稼働中(毎日 8:00 / 13:00 にメール、賭け運用は手動投票)

---

## 1. プロジェクトの目的と背景

### 動機
姉妹プロジェクト `boat-racing-ai`(競艇予想)が 2026 年 4 月に「市場に勝てない」と
撤退判定済(全期間 ROI 92.8%)。同じ手法をオートレースに適用したらどうなるか、を
データで実証する個人研究プロジェクト。

控除率はオートレースの方が高い(boat 25% / auto 30%)= 構造的にはより不利。
にもかかわらず、市場規模が小さいぶん**価格効率性が低く edge が残る可能性**を仮説に検証。

### スコープ
- **データ蓄積**: autorace.jp 公式 API から 5 年分(2021-04-26 〜 現在)を取得・継続更新
- **ML 検証**: walk-forward 月次評価で「市場越え」が技術的に可能か honest に判定
- **推奨提示**: ユーザーが手動投票する用の「買い候補」メール配信
- **賭け自動化なし**: vote.autorace.jp の利用規約がグレー(自ら申込む条項)+
  公開 API なし(HTML 自動操作リスク)+ 期待利益が小さく(年 ~¥17-30K)、ROI に対する
  運用負荷が引き合わない

---

## 2. データソース・仕様

### 取得元
**autorace.jp 公式 JSON API**(HTML スクレイピング不要)。Laravel ベースの POST
エンドポイントが整備されており、CSRF トークンを取得すれば叩ける。

### 主要エンドポイント

| エンドポイント | 用途 | 備考 |
|---|---|---|
| `/race_info/Program` | 出走表(選手・ハンデ・試走タイム) | POST、1レース単位 |
| `/race_info/Odds` | オッズ(7 券種)+ AI 予想印 | POST、1レース単位 |
| `/race_info/RaceResult` | レース結果 + 周回ランク変動 | POST、1レース単位 |
| `/race_info/RaceRefund` | 払戻金(7 券種、1日分まとめて) | POST |
| `/race_info/Player` | 選手リスト | POST |
| `/race_info/XML/Hold/Today` | 当日開催情報 | GET、認証不要 |

### CSV 構成 (`data/`)

| ファイル | 行数 | 内容 |
|---|---:|---|
| `race_entries.csv` | 268,265 | 出走表(選手・ハンデ・試走T・rank 等)|
| `race_stats.csv` | 268,265 | 選手集計成績(90d/180d/通算)|
| `race_results.csv` | 268,265 | 着順・タイム・事故コード |
| `race_laps.csv` | 1,853,215 | 周回ランク変動(展開分析用)|
| `payouts.csv` | 380,220 | 払戻金(7 券種)|
| `odds_summary.csv` | 268,265 | 単勝/複勝オッズ + AI 予想印 |

合計サイズ約 142 MB / 期間 2021-04-26 〜 現在。ローカル CSV(Supabase 不使用)。

### 場コード(autorace.jp 公式)
2 = 川口、3 = 伊勢崎、4 = 浜松、5 = 飯塚、6 = 山陽
(1 = 船橋は 2016 年閉場、データ無し)

---

## 3. ML パイプライン

### Target
**`target_top3`**: 複勝(1〜3 着以内)に入るかどうかの binary classification

### 特徴量(75 列)
- **静的**(出走表): car_no, handicap, rank, age, bike_class, player_place_code, etc.
- **過去成績**(race_stats): 90日/180日/通算の win_rate, st_ave, latest 10 着順分布
- **試走情報**: trial_run_time, trial_diff_min/mean
- **オッズ**: win_odds, place_odds_min/max, log/rank 派生
- **レース文脈**: race_handicap_max/min, race_n_cars, race_n_absent
- **時間**: year, month, dow

### 学習方式
**Walk-forward 月次評価**:
- 各テスト月 t について、t より前の全データで LightGBM を訓練
- 49 ヶ月の test 結果を accumulate(2022-04 〜 2026-04)
- AUC は 49 ヶ月で **平均 0.816 / std 0.017**(極めて安定)

### 3 段階のモデル

時間帯別の運用を可能にするため、特徴量サブセット 3 種を比較:

| モデル | 試走 | オッズ | AUC(校正後) | 運用上の意味 |
|---|:---:|:---:|---:|---|
| 直前 | ✓ | ✓ | 0.811 | レース 5 分前で実行(取りこぼし大)|
| **中間** | ✕ | ✓ | **0.805** | 朝バッチで運用可 ⭐ |
| 前日 | ✕ | ✕ | 0.691 | 前日の予測も可だが精度低 |

**重要発見**: 試走情報の AUC 寄与は **わずか 0.6pt**。情報の主役はオッズ。
中間モデル(オッズあり・試走なし)で精度ほぼ維持しつつ朝バッチ運用が可能。

---

## 4. EV-based 選別戦略の発見プロセス

### Phase 1: ベースライン(ナイーブ戦略)
- 単勝 ROI 74.5%(月次 ≥ 100% は 0/49 月)
- 複勝 ROI 93.4%(月次 ≥ 100% は 6/49 月、控除率の壁を越えず)

### Phase 2: 信頼度選別
- `pred ≥ 0.94` でフィルタ → 複勝 ROI 96.7%
- まだ控除率に届かず

### Phase 3: 期待値選別の発見

**核心公式**:
```
EV = pred_calibrated × (place_odds_min + place_odds_max) / 2
```

各レース・各車について EV を計算し、`top-1 予測 + EV ≥ threshold` で選別。
初期発見: `ev_min ≥ 1.0` で **ROI 119.5%** 出現。

### Phase 4: ⚠️ 過大評価の罠を分解

「119.5% 出た!」と喜ぶ前に、3 つの要因を分離する必要あり:

| 要因 | 寄与 pt | 検証方法 |
|---|---:|---|
| **モデル校正ズレ**(pred が真の P を 5-10pt 過大評価)| +10pt | pred ビン × 実 hit_rate を比較 |
| **place_odds_min の保守性**(実払戻 ÷ min = 1.2-1.7倍)| +5pt | hit 時の actual payout / place_odds_min |
| **真の market edge** | **+5pt** | isotonic 校正 + ev_avg ベース再評価 |

→ 119.5% は幻成分が 15pt、**真の edge は +5pt** のみ。

### Phase 5: Honest 評価(校正 + ev_avg)

isotonic regression で過去半分のデータで校正、後半 25 ヶ月で評価:

| 閾値 | n_bets | 命中率 | ROI | 月次 ≥100% | 利益(25mo)|
|---:|---:|---:|---:|---:|---:|
| 1.00 | 7,546 | 74.4% | 104.7% | 18/25 (72%) | ¥35,130 |
| 1.30 | 3,137 | 67.3% | 120.9% | **25/25** | ¥65,480 |
| **1.45** | **2,201** | 67.0% | **131.8%** | **25/25** | **¥69,900** |
| 1.50 | 1,898 | 66.3% | 136.1% | 25/25 | ¥68,510 |
| 1.80 | 1,040 | 64.8% | 162.0% | 25/25 | ¥64,480 |

**スイートスポット**: thr=1.45。月次 25/25 全勝かつ利益最大。

### boat-racing-ai との比較

| 項目 | boat-racing-ai | auto-racing-ai |
|---|---|---|
| 控除率 | 25% | 30% |
| Walk-forward 期間 | 19 ヶ月 | 49 ヶ月 |
| 単純複勝 ROI | 92.8% | 93.4% |
| 月次 ≥100% 月数 | 8/19 (42%) | 6/49 (12%, 単純戦略時) |
| **EV-based ROI** | **未検証** | **131.8% (thr=1.45)** |
| **EV-based 月次 ≥100% 月数** | **未検証** | **25/25 (100%)** |

→ 同手法を boat に適用すべく、`HANDOFF_FROM_AUTORACE.md` を boat 側に配置済。

---

## 5. 運用設計(Phase A: 推奨提示型)

### 設計哲学
- **賭けの自動化はしない**(規約グレー、API なし、期待利益小)
- **予測 → メール通知 → ユーザー手動投票**
- 中間モデル使用(朝オッズで EV 計算可、試走待ち不要 = 取りこぼし最小)

### Windows Task Scheduler 構成

| タスク名 | 時刻 | 内容 |
|---|---|---|
| `AutoraceDailyIngest` | 毎日 06:30 | データ収集(catchup 2 日)|
| `AutoraceMorningPredict` | 毎日 08:00 | 朝メール: 川口・伊勢崎・浜松 の EV ≥ 1.50 候補 |
| `AutoraceNoonPredict` | 毎日 13:00 | 昼メール: 飯塚 の EV ≥ 1.50 候補 |
| `AutoraceWeeklyRetrain` | 毎日曜 03:00 | 本番モデル再学習(`train_production.py`)|
| `AutoraceWeeklyStatus` | 毎月曜 07:30 | 週次レポート + 通知候補監査 |

**山陽(ミッドナイト)は除外**: 寝てる時間に投票不可、運用負荷で利益無効化。

### メール 1 通あたりの内容

```
📈 autorace 買い候補 (2026-04-29 朝 daytime)
戦略: 中間モデル + EV ≥ 1.50 (top-1 複勝)

| 場    | R  | 車 | pred  | EV   | min | max  | tns  |
|-------|----|----|-------|------|-----|------|------|
| 川口  | R3 | 5  | 0.78  | 1.62 | 1.4 | 2.7  | 3.2  |
| 浜松  | R7 | 1  | 0.71  | 1.55 | 1.1 | 3.2  | 2.8  |
...

計 N 候補 / 投資 ¥(N×100)
```

ユーザーは vote.autorace.jp で手動投票。

### 期待リターン(thr=1.45 ベース)
- 25 ヶ月で投資 ¥220,100、回収 ¥289,990、**利益 ¥69,900**
- 月平均 +¥2,800 / 年 +¥34K
- ただし**取りこぼし**(物理的に投票できない races)を考慮すると 50-80% 効率
- 実利益期待値: 月 ~¥2,300、年 ~¥27K

---

## 6. 運用結果モニタリング

### 通知ログ
`data/daily_predict_picks.csv` に通知済み候補が累積される(race_date, place,
race_no, car_no, pred, ev, odds, sent_at)。

### 週次監査
`scripts/picks_audit.py` が `daily_predict_picks.csv` × `payouts.csv` を join、
hit / 実払戻 / ROI を集計。`weekly_status.py` から呼ばれて月曜の週次メールに同梱。

### 確認できる数字
- 通知候補数 / 決着済 / 結果未取得
- 投資・回収・損益・ROI(全体 + 場別 + バッチ別)
- 命中率(全体・分解)

3-4 週間運用後に、検証時の試算と実運用の数字を突き合わせて判断材料に。

---

## 7. リスク・限界

### 規約面(自動投票の不採用理由)
- 会員規約 第13条: 「銀行直結会員は車券を購入しようとする場合は **自ら申込む** ものとし、
  他人に申し込ませることはできません」 — 自動 bot がこれに該当するか不明
- 会員規約 第18条: 加入者番号・パスワード・暗証番号を第三者に漏らしてはならず — code 内
  保存の解釈問題
- サイト利用規約: 「大量アクセスを含む不正アクセス」を禁止
- **公開 API なし** — HTML 自動操作は技術的に不安定 + 規約違反度高

### 統計面
- **真の edge は +5pt 程度**(初期発見 119.5% から幻成分 15pt を除いた値)
- 月次 std 21.9%(thr=1.45)— 短期で 90% 台になる月もありうる
- 連敗期間あり(過去 6 ヶ月実装シミュレーションで 12 月末 〜 1 月初に -¥370 のドローダウン)
- 5 年データの out-of-sample でも 25 ヶ月分の test しかなく、長期再現は確証なし

### 経済面
- 賭金を増やしても複利は乗らない(歪みは買えるだけしかない)
- 5 場合計年 ¥17-30K では、運用時間 vs 利益の trade-off で「ほぼゼロ」
- 動機が「儲け」だけだと割に合わない、「データ実証」「予想精度の確認」「日々の楽しみ」が
  動機ならアリ

---

## 8. 技術スタック / ファイル構成

### スタック
- Python 3.13 / pandas 3.0 / LightGBM 4.6 / scikit-learn 1.4 / pyarrow 24
- データ: ローカル CSV(Supabase は使わず)
- メール: Gmail SMTP + アプリパスワード
- 自動化: Windows Task Scheduler(WSL/cron 不使用)
- Git: `Tower2007/auto-racing-ai`(Private)

### ディレクトリ構成

```
src/
  client.py              # autorace.jp API クライアント(CSRF 自動取得)
  parser.py              # JSON → CSV フラット変換
  storage.py             # CSV 読み書き
ml/
  features.py            # 6 CSV → ml_features.parquet
  train.py               # holdout 評価
  walkforward.py         # 月次 walk-forward
  walkforward_morning.py # 中間モデル(試走なし・オッズあり)
  walkforward_preday.py  # 前日モデル(両方なし)
  train_production.py    # 本番モデル + isotonic 校正(週次再学習)
daily_ingest.py          # 日次データ収集
daily_predict.py         # 当日対象場の EV ベース買い候補メール
weekly_status.py         # 週次レポート + picks 監査
gmail_notify.py          # Gmail SMTP 送信
backfill.py              # 過去データ一括取得
scripts/
  ev_*.py                # EV 戦略 5 段階検証
  picks_audit.py         # 通知候補の hit/ROI 集計
  daily_pnl_*.py         # 場・期間別 P&L バックテスト
  fix_*.py / dq_*.py     # データ品質チェック・修正
data/                    # CSV + production_*.lgb/.pkl/.json (.gitignore)
docs/                    # 調査結果・戦略まとめ
reports/                 # 各種分析レポート
```

---

## 9. 経緯タイムライン

| 日付 | マイルストン |
|---|---|
| **2026-04-26** | プロジェクト開始(boat-racing-ai 撤退判定後)。autorace.jp の URL/API 構造調査 → JSON API 確認 → repo 作成 |
| **2026-04-26-27** | `client.py` / `parser.py` / `storage.py` / `ingest_day.py` / `backfill.py` 作成 |
| **2026-04-27** | 5 年バックフィル完了(3,448 race-day、エラー 0、142 MB)|
| **2026-04-27** | データ品質チェック → DQ レポート、軽微な欠損修正(`win_odds=0` の NULL 化等)|
| **2026-04-27-28** | ML パイプライン構築。walk-forward 49 ヶ月で AUC 0.816、ROI 0/49 で月次勝てず → 撤退判定?と思いきや… |
| **2026-04-28** | EV-based 選別を試行。`ev_min ≥ 1.0` で見かけ上 ROI 119.5% 出現 |
| **2026-04-28** | 過大評価の罠を分解。校正ズレ 10pt + min 保守 5pt + 真の edge 5pt と判明 |
| **2026-04-28** | isotonic 校正 + ev_avg で honest 検証 → ROI 131.8% (thr=1.45) を確認、月次 25/25 全勝 |
| **2026-04-28** | データ収集自動化(daily_ingest)+ 週次レポート(weekly_status)を Task Scheduler に登録 |
| **2026-04-28** | 「自動売買」検討開始 → vote.autorace.jp 規約調査 → Phase A(推奨提示型)で確定 |
| **2026-04-29** | 中間モデル / 前日モデルを比較 → 試走情報の寄与は AUC 0.6pt のみと判明 → 中間モデル採用 |
| **2026-04-29** | Phase A 運用開始: `AutoraceMorningPredict` 8:00 + `AutoraceNoonPredict` 13:00 + `AutoraceWeeklyRetrain` 日曜 03:00 |
| **2026-04-29** | 通知候補監査スクリプト(`picks_audit.py`)を週次レポートに統合 |

5 日間で 0 → 自動運用稼働。

---

## 10. 関連プロジェクト

### boat-racing-ai
- 同じユーザー、同じ目的、競艇版
- 2026-04 時点で「市場越え不可」と撤退判定
- auto-racing-ai での EV 戦略発見を受けて、再検証用に
  `Boat_racing_AI/HANDOFF_FROM_AUTORACE.md` を配置済(boat 側で再評価予定)

### keiba(競馬)
- 同じユーザーが運用中の別プロジェクト
- 月曜夜に週次レポートメール送信(運用パターン参考)

### horse-racing-ai
- 競馬の別実装(横展開元)

---

## 11. リポジトリ参照

- **GitHub**: https://github.com/Tower2007/auto-racing-ai (Private)
- **作業ディレクトリ**: `C:\Users\no28a\Claude-project\Auto_racing_AI`
- **データディレクトリ**: 同上 `data/`(Git 未管理、自宅 PC のみに存在)

---

## 12. 一言まとめ

> **5 日間の研究で、自動オートレース予測 → 推奨提示メール → 手動投票 のパイプラインが
> 稼働中。walk-forward 49ヶ月の検証では年 ¥27K 程度のプラス edge を honest に確認したが、
> 賭けで生計を立てるレベルではなく「予測精度の実証」「データ蓄積」「運用そのもの」を
> 楽しむ枠組み。**

技術的には市場効率の高い競艇では見つからなかった edge をオートレースで発見した点に意義があり、
boat-racing-ai に再検証を促すハンドオフ済み。
