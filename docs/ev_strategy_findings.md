# EV-based 戦略 検証結果まとめ

最終更新: 2026-04-30(Codex audit 反映)

walk-forward LightGBM 予測を使って「期待値ベースで複勝を選別すれば ROI > 100% にできるか」を 5 段階で検証した記録。
**結論: closing odds backtest では複勝 top-1 に robust な edge が観測された(cutoff 2024-01〜06 を全て試しても thr=1.50 近傍の月次全勝が崩れない)。ただし実発火 odds で測ると ROI 132% → 67% に転落するため、「真の edge」と断定できる段階ではない。live / paper 検証待ち(2026-04-30 Codex audit + odds_snapshot_eval で確認)。**

## ⚠️ Closing odds 問題(2026-04-30 追加・最重要)

本 doc 全体の数字は **closing odds (= 後日 API で取得した最終オッズ) backtest** に基づく。
実発火 5 分前のスナップショット(`data/odds_snapshots.csv`)で測定すると以下のように乖離:

| 指標 | closing backtest | 発火時 snap (n=20) |
|---|---:|---:|
| pred-top1 EV≥1.50 ROI | **132.5%** | **67.0%** |
| pred-top1 EV≥1.50 hit | 65.3% | 45.0% |
| snap EV≥1.5 が close でも EV≥1.5 維持率 | - | 30.8% |
| snap EV → close EV drift 平均 | - | -0.503 |

つまり「発火時に EV≥1.50 で買おうとした 13 件のうち、close 時点で EV≥1.50 を保つのは 4 件のみ」「backtest の ROI 132.5% は実発火基準では 67% に転落」。

加えて `realized_payout / odds_avg` は中央値 0.69(平均 0.75)で、**backtest は odds_avg で 25-30% 過大評価**していた可能性が高い。

**示唆**:
- 本 doc の以下の数字はすべて「closing odds backtest ROI」と読み替えるべき
- 実運用では **odds_snapshots.csv ベース**の発火時 EV を信頼する(daily_predict.py は既にそうなっている)
- backtest ROI 130% を実利益の前提に置くのは危険。snap データの蓄積を待つ
- 関連: `scripts/odds_snapshot_eval.py` / Codex audit (`Opinion/CodexOpinion.md` 2026-04-30 エントリ)

cutoff 感度分析(2024-01〜06、4 thr スイープ)では月勝率は cutoff invariant で安定。
selection bias は二次的、主犯は closing odds drift(Codex 観点 A・D)。
詳細: `Opinion/baseline_audit/proposal_cutoff_sensitivity.py`

---

## TL;DR

| 戦略 | ROI | 25ヶ月 利益 | 月次 ≥100% | 性格 |
|---|---:|---:|---:|---|
| top-1 + ev_avg_calib ≥ 1.00 | 104.7% | ¥35,130 | 18/25 | 機会多い・低分散 |
| top-1 + ev_avg_calib ≥ **1.30** | 120.9% | ¥65,480 | **25/25** | 月次完璧・推奨 |
| top-1 + ev_avg_calib ≥ **1.45** | 131.8% | **¥69,900** | **25/25** | 利益最大点 ⭐ |
| top-1 + ev_avg_calib ≥ 1.80 | 162.0% | ¥64,480 | 25/25 | 高分散 |
| top-1 + ev_avg_calib ≥ 3.00 | 296.6% | ¥39,710 | 23/25 | カジノ的 |

**実用イメージ(thr=1.45)**: 月 88 ベット = 約 ¥8,800 投資 / ¥2,800 月利益(+31.8%)。年換算 ¥34K 利益。

## 5 段階の検証プロセス

### Step 1: 単純な複勝 top-1
- 全期間 ROI **93.4%**(boat-racing-ai と同水準)
- 単体では控除率 30% を埋められない

### Step 2: 信頼度閾値選別(pred ≥ 0.94)
- ROI **96.74%**(+3.34pt)
- まだ控除率に届かず

### Step 3: EV ベース選別(初期発見、ev_min ≥ 1.0 + top-1)
- ROI **119.5%**(超過大)
- 初見で「市場越え」と判断したが、後の検証で過大評価と判明

### Step 4: 過大評価の分解
EV の 119.5% は 3 要因の積み重ね:
| 要因 | 寄与 pt |
|---|---:|
| モデルが pred を過大評価(中レンジで +0.10pt 過大)| 約 +10pt |
| place_odds_min が保守的(実払戻 ÷ min = 1.18 〜 1.70 倍)| 約 +5pt |
| **真の market edge** | **+5pt** |

### Step 5: 校正 + ev_avg ベースで再評価
- isotonic regression で pred を校正(前半 24ヶ月 fit、後半 25ヶ月評価)
- ev_avg(min/max 中央値)で **ev_min より過度な保守バイアスを緩和**
  - ただし hit 時の `realized_odds / odds_avg` は中央値 0.692 / 平均 0.751 で、
    ev_avg を実払戻の honest proxy と断定するのは強い(2026-04-30 Codex audit 確認)
- → **closing odds backtest ROI 104.7%**(thr=1.00、top-1 限定、25ヶ月 7,546 ベット)
- 校正自体の効果は限定的(time-shift が原因と推定)

## 真の戦略仕様

```python
# 最小限の擬似コード
def select_bets(predictions, odds, threshold=1.30):
    """各レースで EV ≥ threshold の予測 top-1 車を 100 円複勝で買う"""
    df = predictions.merge(odds, on=['race_date','place_code','race_no','car_no'])
    df['ev_avg'] = df['pred_calibrated'] * (df['place_odds_min'] + df['place_odds_max']) / 2
    df['rank'] = df.groupby(['race_date','place_code','race_no'])['pred'].rank(method='min', ascending=False)
    return df[(df['rank'] == 1) & (df['ev_avg'] >= threshold)]
```

依存:
- LightGBM 予測(walk-forward、target=top3、月次再訓練)
- isotonic regression 校正(過去予測 + 実 hit 結果でカーブ fit)
- 確定オッズ(レース直前の place_odds_min/max)

## なぜ boat-racing-ai では見つからなかったか

- boat: 控除率 25% / 全期間 ROI 92.8% / 月次 ≥100% は 8/19 (42%)
- auto: 控除率 30% / EV 戦略 ROI 120-136% / 月次 ≥100% は 25/25 (100%) at thr=1.30+

**仮説**:
1. **市場効率性の差**: boat は競技人口・売上が大きく市場が研究されすぎている。auto は相対的に小規模で歪みが残る
2. **データの richness**: 試走タイム・偏差・周回ランク変動など auto 固有の特徴量
3. **券種の構造**: 8 車レース + ハンデ戦は 6 車 boat より組合せ多く、本命固めの圧力が緩い

## 警告 / 注意点

### モデル校正は完全ではない
- 校正前後で diff が大して縮まらず(例: pred 0.5 で +0.10 過大のまま)
- 時期によって過信パターンが変動 = isotonic だけでは追従できない
- 結果として「実は +10pt は校正ズレ」が安定して残っている

### 25 ヶ月という小サンプル
- thr が高い領域(2.0+)では n_bets が小さく、月次振れ幅が大
- 5-10 年の eval では数字がブレる可能性

### 実運用の隠れコスト
- **時間**: 毎日 3 ベットの選別と発注、結果確認
- **精神**: 連敗月の存在(高 thr では特に)
- **時給換算**: 年 ¥34K 利益 - 時間コスト = ほぼゼロ or マイナス

### 賭け額を増やしても複利は乗らない
- 年 +31.8% は「投入金額に対する」リターン
- ベット額を 10 倍にしても利益も 10 倍だが、ROI は変わらない(歪みは買えるだけしかない)
- 大金を投じてもプロにはなれない

## 3点BUY 戦略の場別探索 (2026-04-29)

boat-racing-ai 移植の 3点BUY (複勝+3連単+3連複) を auto に適用、場別に分解して検証。
ロジックは boat の night_report 内 EV合致レース 3点BUY と等価(検算済)。

### 場別 thr=1.45 の合算 ROI

| 場 | races | 複勝 | 3連単 | 3連複 | 合算 | 利益 |
|---|---:|---:|---:|---:|---:|---:|
| 川口 | 357 | 122% | 168% | 110% | 133% | +¥35,710 |
| 伊勢崎 | 363 | 125% | 67% | 231% | 141% | +¥44,350 |
| 浜松 | 278 | 153% | 267% | 196% | 205% | +¥87,770 |
| **飯塚** | 565 | 118% | **61%** | 94% | **91%** | **-¥15,130** |
| 山陽 | 541 | 126% | 99% | 123% | 116% | +¥26,540 |

### 月次安定性(thr=1.45 月次 ≥100% 達成率)

| 場 | rt3 | rf3 | rt3 median | rf3 median |
|---|---:|---:|---:|---:|
| 川口 | 25% | 21% | 36% | 68% |
| 伊勢崎 | 16% | 32% | 36% | 65% |
| 浜松 | 20% | 30% | 30% | 57% |
| 飯塚 | 14% | 19% | 43% | 69% |
| **山陽** | **32%** | **40%** | **47%** | **84%** |

### 結論

1. **3連単(rt3)は全場で月次ノイジー** — median 30〜47%、外れ値 1〜2 レースで mean が押し上がっているだけ。期待値ベースでは黒字だが運用安定性なし。
2. **3連複(rf3)は山陽のみ構造的に強い** — median 84%、≥100% 月 40%、min 22%(他場は全て 0%)。他場は外れ値依存。
3. **飯塚は 3連系 edge 薄い** — rt3 の max がたった 238%(他場 397〜2795%)、上振れすら出ない。市場効率化(売上規模大)と整合。
4. **山陽が rt3/rf3 両方で月次安定性最高** — Phase A 除外(ミッドナイト運用都合)はロジック面では損失。通知タイミング設計の見直し余地。
5. **複勝(fns)は依然全場で edge あり** — Phase A 主軸(複勝 top-1, thr=1.45)維持でよい。

### 実用提案(検証直後の素朴な解釈)

- 3連系を Phase A に乗せるなら **山陽のみ**(rt3/rf3 両方で edge)
- 飯塚は 3連系から除外推奨(複勝のみ継続 — 複勝 ROI 117.9% は健在)
- rt3 単体は全場で「お遊び枠」

### 山陽 rf3 監視候補(2026-04-30 Codex audit 反映)

3点BUY 全体としては不採用だが、**山陽 rf3 のみ別格**(月勝率 40%、median 84%、min 22%
= 他場 0% 比較で構造的)。本番除外していても監視候補として棚に残す価値あり。
Codex 提案: 「本番 baseline 維持 + 山陽 rf3 を将来再検討の対象として保持」。
将来の検証で発火時オッズベースでも edge が残れば、その時点で Phase A に追加検討。

## 2026-04-30 最終判断: 3点BUY 不採用、baseline_fns_only 維持

上記の場別 ROI と「実用提案」を踏まえ、policy シミュレータで複数案を比較した
(`scripts/ev_3point_policy_sim.py` + `scripts/ev_3point_outlier_analysis.py`)。
結論: **複勝 top-1 のみの現行 baseline を維持**、3点BUY は採用しない。

### policy 比較(thr=1.45、25mo 後半)

| policy | 利益 | 月勝率 | min月 | 連敗 | 最大DD | top3除外利益 |
|---|---:|---:|---:|---:|---:|---:|
| **baseline_fns_only**(維持) | ¥+55,870 | **25/25** | **¥+430** | **0月** | **¥0** | ¥+41,280 (-26%) |
| sanyo_rf3 | ¥+68,550 | 23/25 | ¥-810 | 1月 | ¥-810 | ¥+32,220 (-53%) |
| ex_iizuka_full | ¥+204,470 | 13/25 | ¥-7,600 | 3月 | ¥-18,720 | ¥+32,780 (**-84%**) |
| all_3types | ¥+179,240 | 13/25 | ¥-11,650 | 4月 | ¥-25,080 | ¥+13,040 (**-93%**) |

### 不採用の根拠

1. **baseline は closing odds backtest で異常な安定性**: 25 ヶ月全月プラス、最悪月 +¥430、累計 DD ¥0
   (複勝 top-1 EV ≥ 1.45 の closing odds backtest で広く薄い edge が観測される。
   閾値は eval 期間の sweep で選ばれているため「真の edge」と断定せず live / paper
   検証待ち — ⚠️ Closing odds 問題セクション参照)
2. **採用理由の主語**: 「baseline の 25/25 が真の edge の証拠だから」ではなく、
   **「baseline は stakes が単純で下振れ耐性が高く、live / paper 検証で崩れた時に
   原因分解しやすいから」**。3連系は過去 backtest の利益額こそ大きいが、外れ値依存・
   月次赤字・券種別 drift の検証負荷が大きく、closing odds 問題が解決するまでは
   切り分けが難しい(2026-04-30 Codex audit 反映)
3. **3連系は上振れ依存度が極端**: ex_iizuka_full は top 3 ヶ月 (2025-05/-11/-12) で
   ¥+171,690 を稼ぎ、残り 22 ヶ月の利益はわずか ¥+32,780。月平均 +¥1,490
3. **過去再現性が低い**: 549.4% / 394.9% / 291.2% という単月 ROI は外れ値であり、
   次の 25 ヶ月で同等の大当たりが来る保証はない。3連系のサンプル数が薄いため
4. **最大ドローダウンと連敗が運用心理に重い**: ex_iizuka_full は運用途中で
   累計利益が ¥-18,720 まで沈む期間があり、3 ヶ月連続赤字も発生
5. **sanyo_rf3 の積み上げは限定的**: baseline+¥12,680 / 25mo = 月+¥507。
   月勝率を 100% → 92% に落とす対価としては薄い

レポート詳細: `reports/ev_3point_policy_sim_2026-04-30.md` /
`reports/ev_3point_outlier_analysis_2026-04-30.md`

## 関連ファイル

- 予測: `data/walkforward_predictions_top3.parquet`
- 校正カーブ: なし(scripts/ev_calibrated.py で都度生成)
- スクリプト:
  - `scripts/ev_selection.py` — 初期 EV 選別(過大評価)
  - `scripts/ev_verify.py` — キャリブレーション + 安定性検証
  - `scripts/ev_audit.py` — leakage / random baseline / 直近検証
  - `scripts/ev_calibrated.py` — isotonic 校正 + 真 ROI
  - `scripts/ev_threshold_sweep.py` — thr スイープ + 利益最大点探索
  - `scripts/ev_3point_buy.py` — 3点BUY 戦略 thr スイープ
  - `scripts/ev_3point_monthly.py` — 3点BUY 月次安定性
  - `scripts/ev_3point_by_place.py` — 3点BUY 場別 × thr マトリクス
  - `scripts/ev_3point_iizuka_diag.py` — 場別 月次 rt3/rf3 統計
- レポート(reports/):
  - ev_selection_2026-04-28.md
  - ev_verify_2026-04-28.md
  - ev_audit_2026-04-28.md
  - ev_calibrated_2026-04-28.md
  - ev_threshold_sweep_2026-04-28.md
  - ev_3point_by_place_2026-04-29.md

## 結論判断

**実運用は推奨しない**(年 ¥34K のために手間と精神コスト)が、**「市場越え」は技術的に達成済**として記録に残す。
今後の方針:
- データ蓄積は継続(daily_ingest 自動化済)
- ML パイプラインは維持(walk-forward は再現可能)
- 賭け運用に踏み出す前に必ずこの doc を再読
