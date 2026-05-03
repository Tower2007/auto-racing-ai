# Codex 依頼書: baseline_fns_only の「月次 25/25」を疑ってほしい

依頼日: 2026-04-30
依頼者: ユーザー → Codex(via Claude が起草)
出力先: `Opinion/CodexOpinion.md` に追記

---

## 背景

auto-racing-ai では 2026-04-28〜30 にかけて EV-based 戦略を 5 段階で検証し、
最終的に **baseline_fns_only**(複勝 top-1 + `ev_avg_calib >= 1.50`)を採用、
3点BUY(複勝+3連単+3連複 同時購入)は不採用と判断した。

経緯と数字の出処は `docs/ev_strategy_findings.md` に集約済み(必読)。

採用された baseline は walk-forward 25 ヶ月の eval set(2024-04 〜 2026-04)で
以下のように振る舞った:

| 指標 | 値 |
|---|---:|
| n_bets | 1,898 |
| ROI | **136.10%** |
| 月次 ≥100% 月数 | **25/25** |
| 累計利益 | +¥68,510(thr=1.50) |
| 月次 ROI std | 25.1% |

**月次 25/25 完璧** は異常に良い数字。Claude(私)はこれを「複勝 top-1 EV ≥ 1.50 の
真の edge が広く薄く存在している証拠」と解釈して採用した。

ただし「異常に良い数字」の素直な疑い方として、以下のいずれかの可能性は残る:

- (i) data leakage(訓練と評価の境界が漏れている)
- (ii) キャリブレーションの後付け最適化(閾値を data-driven で選んでいる)
- (iii) 評価期間のたまたま(25ヶ月では小サンプル)
- (iv) 集計バイアス(同着・欠車・パッフ補正のミス)

姉妹プロジェクト boat-racing-ai では類似手法で **3点BUY を採用** している
(v0.5.0、2026-04-29)。auto は「3点BUY 不採用」で結論が分かれている。
この差自体も興味深い。

---

## 依頼内容

**baseline_fns_only の 25/25 が本物の edge か、leakage か、overfitting か、評価したい。**

下記 5 観点を Codex の独立視点で検証してください。Claude(私)が出した結論を
すべて鵜呑みにせず、忖度なしで反論をください。

### 観点 A: data leakage の有無

- `walkforward_predictions_morning_top3.parquet` の test_month と
  各予測が使ったオッズ・出走表データが本当に時系列順に並んでいるか
- `ml/walkforward_morning.py` で訓練するデータが test_month 以前の月だけに
  限定されているか確認
- `odds_summary.csv` のオッズが「レース直前の確定オッズ」なのか
  「結果反映後のオッズ(=実払戻情報)」なのか — backfill 時のタイムスタンプ
  注釈が無いので疑う余地あり

### 観点 B: キャリブレーション cutoff の妥当性

- isotonic regression は前半 24mo (test_month < 2024-04) で fit、
  後半 25mo (>= 2024-04) で評価
- このカットオフ自体が「結果として 25/25 になる位置」を選んでいないか
- もし cutoff を 1-3 ヶ月ずらすと月勝率はどう変わるか
- カットオフ感度分析を Codex 側で簡単にやってほしい

### 観点 C: 閾値 1.50 の選定経緯

- thr=1.50 は scripts/ev_threshold_sweep.py の sweep で「月勝率 25/25 を維持しつつ
  利益最大に近い点」として選ばれている
- 25mo の data から最適閾値を選ぶ行為は、訓練/評価分離の精神に反していないか
- 実運用で thr=1.50 が外れ値で、本当の最適は thr=1.30 や 1.45 だった可能性は?
- thr 選定そのものを test 期間外でやるべきだったか

### 観点 D: min payout 保守性の補正

- `docs/ev_strategy_findings.md` で「ev_min は実払戻より 18-70% 低い」と分解されている
- ev_avg(min/max 中央値)を使えば honest と言ったが、本当に honest か?
- 実 payout vs ev_avg の比率はどうか(中央値 / 平均 / 最大値)
- もし ev_avg も systematic に低いなら、表示 ROI は更に過大評価の可能性

### 観点 E: boat-racing-ai との結論差

- boat: 3点BUY 採用(v0.5.0、ROI 149.7% で運用)
- auto: 3点BUY 不採用(月勝率 13/25、外れ値依存度極端)
- この差は構造的(競技性質の差)か、検証手法の差か、データ期間の差か
- boat の policy sim と auto の policy sim を直接比較したい
  (boat 側の `Boat_racing_AI/ml_combo_simulation.py` 等を読み込んで OK)

---

## 期待する成果物

`Opinion/CodexOpinion.md` に以下を追記:

```markdown
## 2026-04-30: baseline_fns_only の月次 25/25 レビュー

### 結論(1 行)
「真の edge」「leakage 疑い」「overfitting 疑い」「判断保留」のいずれか。

### 観点別所見
A. leakage: ...
B. cutoff 妥当性: ...
C. 閾値選定: ...
D. min 補正: ...
E. boat との差: ...

### 反論したい / 修正提案したい点

(Claude の結論で同意できない部分があれば率直に書いてほしい。
 例: 「3点BUY 不採用は早計、もう 1 段階の検証が必要」等)

### 追加で必要なら検証スクリプト案

(`Opinion/<topic>/proposal_*.py` 形式で擬似コード or 仕様提案。
 実装は Claude に依頼する想定)
```

**期限**: 急がない。Codex のセッション都合で OK。
**出力先制約**: `Opinion/` 配下のみ(編集権限ルール厳守)。

---

## 参照すべきファイル

優先順:

| 順位 | ファイル | 内容 |
|---|---|---|
| 1 | `docs/ev_strategy_findings.md` | 検証 5 段階の全記録、policy 比較表 |
| 2 | `reports/ev_3point_policy_sim_2026-04-30.md` | 4 戦略比較の結論 |
| 3 | `reports/ev_calibrated_2026-04-28.md` | 校正前後の ROI 比較 |
| 4 | `ml/walkforward_morning.py` | 中間モデル(本番採用)の walk-forward |
| 5 | `ml/train_production.py` | 本番モデル学習 + 校正 fit |
| 6 | `scripts/ev_3point_policy_sim.py` | 4 policy 比較スクリプト |
| 7 | `scripts/ev_threshold_sweep.py` | 閾値 sweep スクリプト |
| 8 | `data/walkforward_predictions_morning_top3.parquet` | 予測データ(直接 pandas で読込) |
| 9 | `data/payouts.csv`, `odds_summary.csv`, `race_results.csv` | 実データ |
| 10 | `Boat_racing_AI/ml_ev_strategy.py`, `ml_combo_simulation.py` | 姉妹プロジェクトの実装 |

---

## 制約と注意

- **大きな計算前は事前承認**: 例えば walk-forward を再生成する場合は
  ユーザーに伝えてから(`AGENTS.md` 既定ルール)
- **コード本体を編集しない**: Codex は `Opinion/` 配下のみ編集可
- **数値主張には出典必須**: ファイル名:行番号 / parquet 列名 / レポート §章番号
- **忖度しない**: Claude の結論で間違っていそうな箇所は率直に書く

---

## 依頼書の運用

この依頼書は Claude が起草、ユーザーが Codex に渡す。Codex は:

1. `AGENTS.md`(全体運用ルール)を読む
2. 本依頼書(`Opinion/codex_briefs/2026-04-30_baseline_audit.md`)を読む
3. 上記参照ファイルを必要に応じて読む
4. `Opinion/CodexOpinion.md` に意見を追記

順序通りに進めれば文脈ゼロでも作業可能。
