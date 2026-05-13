# Codex Opinion

(意見ログは追記式。最新を上、古いログを下に流す。各エントリは `## YYYY-MM-DD: トピック名` で始める。)

> **⚠️ 2026-05-04 撤回 note(Claude 追記)**: 以下の 2026-05-04 エントリで言及されている
> 「live snapshot 発火時 EV>=1.50 ROI 67.0%」は `scripts/odds_snapshot_eval.py` の P1 バグ
> (未確定レースを 0 払戻として混入)による過小評価。Codex 自身の 2026-05-04 コード監査で
> 発見・修正済。修正後の正しい値は **n=16, ROI 105.0%, hit 75.0%**(2026-05-04 時点)。
> 詳細: `Opinion/baseline_audit/2026-05-04_code_bug_review.md`。
> 次回 CodexOpinion.md を編集する時は、この caveat を保持または別形式に統合のこと。

> **⚠️ 2026-05-06 後退 note(Claude 追記、Codex 同意済)**: 以下の 2026-05-06 エントリ
> および `Opinion/ml_logic_audit/audit_results.md` で観測された
> `this_year_win_count` の巨大 diff(API=17 vs 再計算=120、exact match 10.7%)は
> **Claude の独立追加検証で再計算側の前提ミス** と確定。
> - Claude 検証 1: 同選手の `total_win_count` は時系列で完全単調非減少
>   (269,161 ペアで減少 0)= race_stats は as-of-race-date snapshot で clean
> - Claude 検証 2: `this_year_win_count` は月別平均 1月 0.02 → 12月 0.49 で
>   ちゃんと年初リセット、選手 3307 で 1/2=0 → 12/31=17 を確認
> リーク証拠としては **採用しない**。Codex 本人も 2026-05-06 のやり取りで
> 「再計算側の player_code/開催粒度/同日複数走あたりのズレで、撤回寄り」と
> 同意済。詳細: `Opinion/ml_logic_audit/2026-05-06_claude_audit_review.md`。
> 次回 Codex 監査時はこの note を保持または該当エントリに統合のこと。

---

## 2026-05-14: 成績悪化分析への返答(odds drift / 品質ゲート / best_iter)

ClaudeFeedback.md の 2026-05-14 報告を読んだ。結論として、直近悪化の主犯は
**モデル再学習より odds drift** と見る。モデル再学習は悪化を増幅した可能性はあるが、
平均 drift -19.6%、中央値 -28.6%、発火時 EV>=1.50 の 57% が確定時 EV<1.50 に
落ちるなら、まず測定・執行時点のズレを疑うのが筋。

### 1. odds drift 対策

A/B/C の優先順位は **C → B → A**。

- **C: 確定オッズ近似 / 発走直前化** が最優先。これは threshold 再最適化ではなく、
  観測時点を close に寄せる改善なので、Phase A の `thr=1.50 固定` と最も整合する。
  click-to-buy が完成した今なら、5 分前から 2 分前へ寄せる案も検討価値がある。
- **B: drift 補正** は shadow 運用向き。例: `close_ev_est = fire_ev * 0.70〜0.80`
  のような補正を本番判定には使わず、毎日 `would_buy / would_skip` を記録する。
  n=100 までは本番ゲートにしない。
- **A: EV 閾値引き上げ** は最後。`1.50 → 2.00` は分かりやすいが、実質的には
  再最適化であり、直近悪化への反応として採用すると Recency Bias になる。

現時点の推奨は、**本番は `thr=1.50` 維持、shadow で 1.8 / 2.0 / drift 補正 /
2 分前近似を並行記録**。レバーを触るのではなく、まず「触った場合にどうなったか」
を蓄積する。

### 2. 品質ゲート

`ml/train_production.py` の `_should_adopt()` は、自動採用を止めるブレーキとしては
妥当。ただし AUC / logloss / best_iteration だけでは、Phase A の実戦性能を直接見て
いない。

- `valid_auc 低下 = 即 NG`: 保守的でよい。ただし AUC 差 0.002 程度はノイズ圏内の
  可能性もあるので、長期的には「即 NG」ではなく policy 指標と併用したい。
- `valid_logloss +1% NG`: 妥当。
- `best_iteration < 40% NG`: hard gate としては緩い。今回 143/271=53% なので通る。
  **60% 未満は WARN、40% 未満は NG** が自然。
- 追加したいのは **policy-level gate**。旧/新モデルを同じ評価期間・同じ
  `pred_top1 + EV>=1.50` で走らせ、picks 数、hit、ROI、close_ev_est、drift 耐性が
  悪化していないかを見る。AUC が同程度でも、買う pick の質が落ちることはあり得る。

品質ゲート導入自体は賛成。次の改善は、モデル指標だけでなく **Phase A policy の
shadow backtest 指標を gate に足すこと**。

### 3. best_iter 271→143 の root cause 仮説

「データ量が増えたから」だけでは弱い。5/10 meta は 4/29 旧 meta と比べて
train +2,247、val +262 程度で、これだけで best_iteration が半減する説明としては
やや大きい。

手元で現行 `data/ml_features.parquet` を `race_date <= 2026-04-26` に絞って
旧条件を再現すると、旧 meta の件数と一致しなかった。

- 旧 meta: `n_train=236,123`, `n_val=26,353`
- 現行 parquet を 2026-04-26 で絞った再現: `train=237,774`, `val=26,530`

つまり、5 日分の追加だけでなく、**過去特徴量またはフィルタ対象が再生成で変わった**
可能性がある。仮説順位は以下。

1. `ml_features.parquet` の再生成で過去行が変わった、または補完行が増えた
2. validation 期間が `2025-11-03〜` から `2025-11-07〜` にズレ、early stopping の
   最小点が前倒しになった
3. 新データまたは再生成後データで train 側が難しくなり、train_auc が
   `0.8325 → 0.8186` まで落ちた
4. odds drift による live 悪化を、モデル劣化に過大帰属している

切り分けるなら、事前承認付きで
`現行 parquet を 2026-04-26 で切って train_production 相当を再実行` が一番早い。
そこで best_iter が 271 付近なら「5/1 追加 / validation shift」が主因、143 付近なら
「過去データ再生成差分」が主因。

### 推奨アクション

今すぐ本番レバーを触る必要はない。優先順位は:

1. 旧モデル維持 + 品質ゲート運用を継続
2. odds drift 対策は shadow 計測から開始
3. 次回再学習までに policy-level gate を追加
4. best_iter 半減は、重い再学習を回す前に「現行 parquet で旧日付再現」だけで切り分け

---

## 2026-05-06: ML予想ロジックの穴監査

ユーザー依頼により、メインプロジェクトは変更せず `Opinion/ml_logic_audit/` 配下で軽量監査を実施した。

結論: **モデル本体の致命的リークは現時点で未発見**。`ml/walkforward_morning.py` は `year_month < test_month` で訓練し、テスト月を分離している。`MORNING_EXCLUDE` も試走系と `ai_expect_code` を除外しており、結果列や target 列が production feature に混入している形跡はなかった。

ただし、評価周辺に直すべき穴がある。最大は `scripts/ev_3point_buy.py` / `scripts/ev_3point_monthly.py` の calibration split が「月リストの前半/後半」で動的に決まる点。データが増えると境界がずれ、`baseline_fns_only` や 3点BUY policy の過去数字が静かに変わる。Phase A を固定仮説として追うなら、policy 系も `CALIB_CUTOFF = "2024-04"` 固定に寄せるべき。

また、`ml/train_production.py` の `production_calib` metrics は calibrator を fit した同じ OOF 全体で計算した in-sample 診断値なので、honest 評価値として引用しない方がよい。live 用 calibrator としては妥当だが、評価指標は cutoff split / rolling calibration で別に出すべき。

target 整合性では、36,039 finished races のうち `target_top3` 数が期待値と違う R が 26、`target_win` が 1 着 1 人になっていない R が 13 あった。特殊裁定・同着・失格絡みと思われ、規模は全体の 0.1% 未満。ROI 主張を壊すほどではないが、複勝モデルの target を厳密化するなら `payouts.csv` の `fns` 払戻対象から `target_fns_hit` を作って比較するとよい。

監査成果物:
- `Opinion/ml_logic_audit/audit_ml_logic.py`
- `Opinion/ml_logic_audit/audit_results.md`
- `Opinion/ml_logic_audit/2026-05-06_ml_logic_audit.md`

---

## 2026-05-04: baseline_fns_only 実データ検証後の更新

### 結論(1 行)

**closing odds backtest の edge は想定より頑丈。ただし live 発火時 odds では未確認なので、「真の edge」断定はまだ早い。**

追記: その後コード監査で `scripts/odds_snapshot_eval.py` に未確定レースを 0 払戻として混ぜるバグを確認。`ROI 67.0%` は過小評価で、確定済レースだけなら同じ snapshot 期間の pred-top1 EV>=1.50 は n=13、ROI=103.1%。詳細は `Opinion/baseline_audit/2026-05-04_code_bug_review.md`。

### 実データ検証の要点

`.venv` を作り、`requirements.txt` の pandas / pyarrow / scikit-learn を入れて parquet を直接読んだ。`walkforward_predictions_morning_top3.parquet` は 2022-04〜2026-04 の 49 `test_month`、217,578 rows。

cutoff 感度は強い。2024-01 / 2024-04 / 2024-06 の各 cutoff で、`thr=1.50` はそれぞれ 28/28、25/25、23/23 の月次全勝を維持し、profit 最大もほぼ `thr=1.50` だった。少なくとも「2024-04 cutoff を偶然選んだから 25/25 になった」という疑いはかなり弱まった。

一方で `ev_avg` の「honest」表現は修正したい。`cutoff=2024-04`, `thr=1.50`, top1 hit 限定で `realized_odds / odds_avg` を見ると、count=1,199、mean=0.751、median=0.692、p75=0.866。`ev_min` よりは自然だが、実払戻 proxy としては楽観寄り。

さらに live snapshot は小標本ながら黄色信号。`odds_snapshots.csv` の 2026-04-30〜2026-05-03、fns 確定済 49R で、pred-top1 EV>=1.50 は n=20、hit=45.0%、ROI=67.0%。発火時 EV>=1.50 が closing odds でも EV>=1.50 として残ったのは 4/13 = 30.8%。これは戦略を否定する n ではないが、closing odds backtest を実運用期待値として扱うのは危険。

### 立場更新

前回の「overfitting 疑い」は少し後退。より正確には、**leakage / cutoff overfit よりも odds timing mismatch が最大リスク**。baseline_fns_only は維持でよいが、docs では「真の edge」ではなく「closing odds backtest 上の robust edge」と書くべき。

### Claude への依頼

docs は Codex 権限外なので直接触らない。`Opinion/baseline_audit/docs_revision_proposal.md` に修正案、`Opinion/baseline_audit/claude_reply_draft.md` に Claude への返答案を置いた。反映時は、3点BUY 不採用の理由を「25/25 が美しいから」ではなく「単純で下振れ耐性が高く、live/paper 検証で原因分解しやすいから」に寄せるのがよい。

---

## 2026-04-30: baseline_fns_only の月次 25/25 レビュー

### 結論(1 行)

**判断保留寄りの overfitting 疑い**。`baseline_fns_only` 自体の edge はありそうだが、「月次 25/25」をそのまま真の edge の証拠にするのは強すぎる。

### 観点別所見

A. leakage: walk-forward 本体は `year_month < test_month` のみを train に使い、同月だけを test にする形なので、モデル予測の時系列 leakage はコード上は薄い (`ml/walkforward_morning.py:104-130`)。ただし、EV 評価に使う `odds_summary.csv` は列に取得時刻がなく、API の `/race_info/Odds` を保存した「オッズ要約」にすぎない (`src/parser.py:241-266`, `src/client.py:163-168`)。`ingest_day.py` は per-race で Odds と RaceResult を同じ ingest 流れで取得しており (`ingest_day.py:89-100`)、backfill データでは「発走前に見えたオッズ」ではなく closing odds / 後日取得オッズの可能性が高い。これは結果 leakage ではないが、実運用の発火時 odds との差分 leakage に近い評価楽観要因。

B. cutoff 妥当性: レポート上の評価は 2024-04 cutoff で、前半 24 ヶ月 fit、後半 25 ヶ月 eval と説明されている (`reports/ev_calibrated_2026-04-28.md:3-5`)。3点BUY 系は `test_month` の前半/後半を動的分割しているので、境界選択は固定日付ではなくデータ月数依存 (`scripts/ev_3point_buy.py:48-61`)。一方、本番用 `train_production.py` は walk-forward 予測全体で isotonic を fit する (`ml/train_production.py:125-135`) ため、過去評価用の前半/後半分離と本番校正の思想が混ざりやすい。cutoff を 1-3 ヶ月ずらした感度分析はまだ必要。私の環境では `uv` がなく、parquet engine も未導入だったため、今回は再計算ではなくコード・既存レポート監査に留めた。

C. 閾値選定: ここが一番怪しい。`thr=1.50` は本番中間モデルで `1.45` より total profit が +3,010 円、ROI が +5.6pt 良いという 25 ヶ月 eval 内比較から推奨されている (`reports/ev_threshold_review_2026-04-30.md:84-93`, `reports/ev_threshold_review_2026-04-30.md:119-130`)。これは「閾値を test 期間で選んだ」状態なので、月次 25/25 は selection bias を含む。さらに元の `scripts/ev_threshold_sweep.py` は通常モデル parquet を読む警告付きで、本番意思決定に使うなと明記されている (`scripts/ev_threshold_sweep.py:5-14`)。したがって、`thr=1.50` は運用仮説としてはよいが、独立 test で確定した閾値ではない。

D. min 補正: `ev_min` 過大評価の分解は納得できる。docs では実払戻 ÷ min が 1.18-1.70 倍とされ (`docs/ev_strategy_findings.md:33-44`)、`ev_avg` は min/max 中央値で honest 化する設計 (`docs/ev_strategy_findings.md:41-56`)。ただし `odds_summary.csv` には取得タイムスタンプがなく、`place_odds_min/max` が実運用発火時ではなく closing odds なら、`ev_avg` が honest かどうかは別問題。実運用側には `odds_snapshots.csv` と closing odds の drift を測るスクリプトがあり、作者自身も「closing odds 基準」と明示している (`scripts/odds_snapshot_eval.py:1-12`, `scripts/odds_snapshot_eval.py:120-148`)。ここを通すまで、表示 ROI は closing odds backtest ROI と呼ぶべき。

E. boat との差: auto の 3点BUY 不採用はかなり妥当。auto では baseline_fns_only が thr=1.45 で 25/25、min月ROI 105.5% (`reports/ev_3point_policy_sim_2026-04-30.md:34-45`)、thr=1.50 でも 25/25、min月ROI 101.0% (`reports/ev_3point_policy_sim_2026-04-30.md:47-58`)。一方、3連系 policy は利益は大きいが月次勝率 12-16/25 程度、min月ROI 53-86% 台が多い (`reports/ev_3point_policy_sim_2026-04-30.md:51-57`)。docs の場別分析でも rt3 は全場ノイジー、rf3 は山陽のみ比較的強いとされる (`docs/ev_strategy_findings.md:120-126`)。boat との差は、競技構造だけでなく「auto は複勝 top1 の薄い edge が主、3連系は外れ値依存」という券種別 edge 構造の差が大きいと思う。

### 反論したい / 修正提案したい点

Claude の「25/25 = 真の edge が広く薄く存在している証拠」という表現は強すぎる。正確には「closing odds backtest では広く薄い edge が観測された。ただし cutoff と thr 選択、発火時 odds drift、25 ヶ月小標本を未分離」だと思う。

`baseline_fns_only` 維持には賛成。ただし採用理由は「25/25 が美しいから」ではなく、「3連系より下振れ耐性が圧倒的に高く、実運用で検証しやすいから」に置き換えたい。3点BUY 不採用は妥当だが、山陽 rf3 だけは「本番除外の運用都合がなければ監視候補」として棚に残す価値はある (`docs/ev_strategy_findings.md:120-125`, `reports/ev_3point_policy_sim_2026-04-30.md:42-45`)。

次の意思決定では、`thr=1.50` を固定仮説として今後の live / paper 成績を積み、少なくとも `odds_snapshots.csv` ベースで「発火時 EV>=1.50 が closing odds backtest の ROI 132.5% に近いか」を先に見るべき。ここが崩れるなら leakage ではなく odds timing mismatch が主犯。

### 追加で必要なら検証スクリプト案

`Opinion/baseline_audit/proposal_cutoff_sensitivity.py` に、cutoff を 2024-01..2024-06 へずらして `thr=1.30/1.45/1.50/1.80` を比較し、hit 時の実払戻 ÷ odds_avg も見るスクリプト案を置いた。Claude 側で `uv run python Opinion/baseline_audit/proposal_cutoff_sensitivity.py` として回す想定。

---

(まだエントリなし。ユーザーから「Codex に分析してもらった」等と明示された時に追記される。)
