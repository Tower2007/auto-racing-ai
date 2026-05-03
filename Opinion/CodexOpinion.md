# Codex Opinion

(意見ログは追記式。最新を上、古いログを下に流す。各エントリは `## YYYY-MM-DD: トピック名` で始める。)

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
