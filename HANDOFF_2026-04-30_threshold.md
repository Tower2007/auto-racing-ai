# 引継ぎ資料 (2026-04-30 夕方版): EV 閾値の妥当性検討

別 PC で本タスクを進めるためのスナップショット。
朝版 `HANDOFF_2026-04-30.md` は当時の odds 未公開バグ回避が主題で別件(既に修正済)。

## 0. 一言で

本番運用 `thr=1.50` (top1, ev_avg_calib, 複勝) の **閾値が現実の運用に対して適切か別 PC で検証する**。

きっかけ: 2026-04-30 sanyou R1 (10:26)、R2 (10:51) ともに「候補なし」。
ユーザーが「メールこなかった」 → 1日の中で候補ゼロのケースが多すぎないか?
を疑った。`reports/ev_threshold_sweep_2026-04-28.md` 上の数字では月 76 ベット程度
(全R 1200 のうち約 6%、16R に 1R 候補)、つまり1場あたり0.5〜1本/日が想定。
今日 R1/R2 連続 0 件はサンプル内でも普通だが、**期待頻度に対する許容感** と
**閾値設計** を別 PC で再評価したい。

## 1. リポジトリ状態 (2026-04-30 12:10)

- ブランチ: `main`、`origin/main` と一致
- 直近コミット(本日分):
  ```
  9dc25d0 Add threshold-review handoff for cross-PC continuation
  0d072d4 Use Program/Print page to get exact per-race start times
  9056b35 Use raceStartTime/nowRaceNo as scheduler anchor
  390e720 Add dynamic per-race firing scheduler
  ```
- working tree: 本ファイル追加のみ(コミット予定)
- データ: `data/race_*.csv`, `payouts.csv`, `odds_summary.csv` は 5 年分 + 当日分
- モデル: `data/production_model.lgb`, `production_calib.pkl`, `production_meta.json`
  (週次再学習タスク `AutoraceWeeklyRetrain` が日曜 03:00 に上書き)

### 1-A. データの所在(2026-04-30 移行)

PC#1(本機)の `data/` は **directory junction** で Google Drive のミラーフォルダを指す:

```
C:\Users\no28a\Claude-project\Auto_racing_AI\data
  → G:\マイドライブ\auto-racing-ai-data\        (177MB, 24 ファイル)
```

- 書き込みは junction 透過 → Drive ミラーフォルダに反映 → 自動アップロード → 別 PC のミラー
  に降りてくる。junction 経由のスケジューラタスク発火(sanyou R3/R4 等)で書き込み実証済。
- **書き込みは原則 PC#1 のみ**:
  - `daily_ingest`(06:30)、`daily_predict`(R 毎の `AutoraceDyn_*`)、`dynamic_scheduler`(07:00)、
    `weekly_retrain`(日 03:00)はすべて PC#1 のスケジュールタスクで動く
  - 別 PC は **読み取り専用** で運用。`ml_features.parquet` の再生成も PC#1 のみで
    (両PC同時実行→ Drive で `-conflict` 付きファイルが生まれる)
- 差分転送ではなく **CSV は変更時に丸ごと再アップ**(Drive の仕様)。
  - 日次合計 ≒ 150MB↑ / PC#1、150MB↓ / PC#2、月 9GB 規模
  - 06:30 の ingest 完了 〜 1〜3 分で Drive 反映、別 PC 起動時にダウンロード

### 1-B. 別 PC 移行手順(初回のみ)

1. リポジトリ:
   ```
   git clone https://github.com/Tower2007/auto-racing-ai.git
   cd auto-racing-ai
   git pull origin main
   ```
2. Drive 同期完了を待つ(タスクトレイの Drive アイコンで「最新の状態」確認、家庭回線 5–15 分)
3. ローカル空 `data/` を削除し junction を貼る:
   ```cmd
   rmdir data
   mklink /J data "G:\マイドライブ\auto-racing-ai-data"
   ```
   別 PC の Drive ミラーパスが違う場合は target を実パスに合わせる。
4. `.env` を旧 PC からコピー(Gmail SMTP 認証等)
5. 動作確認:
   ```
   python -c "import pandas as pd; print(pd.read_csv('data/race_results.csv', nrows=3))"
   ```

## 2. 自動運用の現状(本日改修後)

| タスク | 時刻 | 動作 |
|---|---|---|
| `AutoraceDailyIngest` | 06:30 | データ収集 |
| `AutoraceDynamicScheduler` | 07:00 | `dynamic_scheduler.py`: Program/Print から R 毎の正確な発走時刻を取得し、各 R 発走 30 分前に `AutoraceDyn_{venue}_R{n}` を 12 R × 場数登録 |
| `AutoraceDyn_{venue}_R{n}` | 動的 | `daily_predict.py --venues {pc} --races {n} --suppress-noresult-email` を 1 R 単位で実行 |
| `AutoraceWeeklyRetrain` | 日 03:00 | 本番モデル再学習 |
| `AutoraceWeeklyStatus` | 月 07:30 | 週次ステータス |
| 旧 `AutoraceMorningPredict` 等 | — | Disabled(動的方式に統合済) |

本日の dynamic_scheduler 動作実績(2026-04-30, 5 場のうち sanyou と isesaki が開催):
- sanyou R1=10:56 (発火 10:26)、R12=16:35 (発火 16:05) — `exact` source 12 R
- isesaki R1=15:00 (発火 14:30)、R12=20:30 (発火 20:00) — `exact` source 12 R
- sanyou R1, R2 とも候補なし(EV>=1.5 通過なし)→ メール送信なし(仕様通り)

## 3. 閾値検討用に既知の数字(全部 `reports/ev_threshold_sweep_2026-04-28.md`)

評価期間: 2024-04 〜 2026-04 の **25 ヶ月、isotonic-calibrated `pred` で eval**。
top1 + `ev_avg_calib`(本番設定)。

| thr | n_bets | hit% | ROI | profit | month_min | month_ge_1/25 |
|----:|------:|:----|:---|:------|:--------|---:|
| 1.00 | 7,546 | 74.4% | 104.66% | ¥35,130 | 93.8% | 18 |
| 1.10 | 5,151 | 69.5% | 110.27% | ¥52,880 | 97.9% | 22 |
| 1.20 | 4,026 | 68.1% | 115.0%  | ¥60,390 | 97.1% | 24 |
| 1.30 | 3,137 | 67.3% | 120.87% | ¥65,480 | 102.3% | 25 |
| 1.40 | 2,485 | 66.6% | 127.07% | ¥67,260 | 107.7% | 25 |
| **1.45** | 2,201 | 67.0% | 131.76% | **¥69,900** | 107.8% | **25** |
| **1.50 (現本番)** | **1,898** | 66.3% | 136.1% | ¥68,510 | 110.4% | 25 |
| 1.60 | 1,554 | 65.3% | 143.53% | ¥67,640 | 109.8% | 25 |
| 1.80 | 1,040 | 64.8% | 162.0%  | ¥64,480 | 117.1% | 25 |
| 2.00 |   717 | 65.1% | 184.98% | ¥60,930 | 119.4% | 25 |
| 2.50 |   343 | 63.3% | 239.59% | ¥47,880 | 36.7% | 24 |
| 3.00 |   202 | 62.9% | 296.58% | ¥39,710 | 30.0% | 23 |

主要観察:
- **profit 最大は thr=1.45**(¥69,900、月平均 88 ベット、約 14R に 1R 候補)
- **thr=1.50 で月平均 76 ベット**(60.8R に 1R/場; 1場 12R で 1〜2 日に 1 候補)
- thr 上げると ROI 上がるが分散も急増。`month_min` が 1.50→1.80 で安定、2.00 超で
  単月 30〜36% という壊滅月が発生し始める(分散負け)
- `thr=1.45` を採用すると profit +¥1,400/25mo (+2%)、ベット数+15.9% で目立った
  リスク悪化なし。**現状最適は 1.45 寄り、1.50 は安全寄り**

候補無しが続く違和感への補足:
- 5 場 × 12R で月 1,200R(20 開催日仮定)。thr=1.50 で月 76 ベット → R 16 本に 1 本
- 1 場 (12R) あたり期待値 0.75 ベット/日 → **1場で 0 候補日が普通にある**
- 2 場開催で 0 候補は 1日のうち 25〜30% 程度発生する見込み(独立仮定)

## 4. 別 PC で具体的にやってほしいこと

### 4-1. **再現タスク**(まずこれ)
1. `git pull origin main` / `data/` 同期
2. `python -c "import pandas as pd; print(pd.read_csv('data/walkforward_top3.csv').tail())"`
   が走ること(ml_features.parquet 必要)
3. `reports/ev_threshold_sweep_2026-04-28.md` を読む

### 4-2. **検討の入口になる質問**
- (A) 候補数の実分布 ― 月平均だけでなく **「候補 0 候補な日」の割合・連続日数** を集計したい。
  → `scripts/ev_*.py` 系か新規スクリプトで `walkforward_top3` 由来の picks を日別 group。
- (B) thr=1.45 vs 1.50 の **walk-forward (out-of-fold) での月次対決** を再評価。
  既存 sweep は eval set 全体集約なので、月毎の優位性検証は別。
- (C) **thr=1.30** まで下げた場合の **手動投票負荷** との trade-off。
  thr=1.30 で月 125 ベット ≒ 4 ベット/日、思い切って張れる量か?
- (D) hit% は thr 上げても 65〜67% でほぼ flat。**hit% を上げる別軸の絞り込み**
  (場別、時間帯別、グレード別、雨/晴別)で thr=1.5 のままサブセットを足切りすると
  もっと profit 取れないか? → `reports/daily_pnl_*` や `reports/ev_3point_by_place_*` 参照。

### 4-3. **避けてほしいこと**
- production_model の上書き(週次再学習以外)。検証は eval split で。
- 自動投票化(ToS グレー)。検証結果は手動投票前提で。
- 3点BUY(rt3/rf3)系の再採用検討 — 2026-04-30 既に `decision_3point_buy_rejected.md`
  に却下記録あり。複勝 top-1 ベースでの閾値調整に集中。
- `data/` への書き込み(daily_ingest 起動、ml_features 再生成、結果 CSV 出力など)。
  PC#1 と同時に走ると Drive で `-conflict` ファイル発生 → 整合性破綻。**読み取りのみ**。

## 5. 関連ファイル

| 種別 | パス |
|---|---|
| 戦略仕様 | `docs/ev_strategy_findings.md` |
| 閾値スイープ実装 | `scripts/ev_threshold_sweep.py`(あれば) / 結果: `reports/ev_threshold_sweep_2026-04-28.md` |
| EV 計算実装 | `daily_predict.py:` の `decide_picks` 周り |
| picks audit | `picks_audit.py` / `data/daily_predict_picks.csv` |
| 場別月次 | `reports/daily_pnl_*` |
| 場別 EV | `reports/ev_3point_by_place_2026-04-29.md`(3点BUY 由来だが場差傾向の参考) |

## 6. 次セッションが最初にやること(別 PC で)

```bash
git pull origin main
cat HANDOFF_2026-04-30_threshold.md            # 本ファイル
cat reports/ev_threshold_sweep_2026-04-28.md   # 数字
ls reports/ | head                              # 最新の調査
git log --oneline -10                          # 直近コミット
```

その上で §4-2 (A)-(D) のどれを優先するか決定 → 新規 script で日別/場別/期間別の
再集計 → 結論を `reports/ev_threshold_review_YYYY-MM-DD.md` に出してコミット。

## 7. メモリで既知の重要事項

- DB は CSV、5 年バックフィル済(`memory/project_decisions.md`)
- git push は毎回明示許可(`memory/feedback_git_push_policy.md`)
- 3点BUY 不採用(`memory/decision_3point_buy_rejected.md`)
- ML phase1/phase2 経緯(`memory/ml_baseline_findings.md`)
