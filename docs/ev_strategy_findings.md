# EV-based 戦略 検証結果まとめ

最終更新: 2026-04-28

walk-forward LightGBM 予測を使って「期待値ベースで複勝を選別すれば ROI > 100% にできるか」を 5 段階で honest に検証した記録。**結論: 真のエッジ約 +5%(thr=1.00 ベース)、調整次第で +30% 程度まで引き出せる。ただし利益総額は小さく、月次分散も大きい。**

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
- ev_avg(min/max 中央値)で payout 推定を honest 化
- → **真の ROI 104.7%**(thr=1.00、top-1 限定、25ヶ月 7,546 ベット)
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

## 関連ファイル

- 予測: `data/walkforward_predictions_top3.parquet`
- 校正カーブ: なし(scripts/ev_calibrated.py で都度生成)
- スクリプト:
  - `scripts/ev_selection.py` — 初期 EV 選別(過大評価)
  - `scripts/ev_verify.py` — キャリブレーション + 安定性検証
  - `scripts/ev_audit.py` — leakage / random baseline / 直近検証
  - `scripts/ev_calibrated.py` — isotonic 校正 + 真 ROI
  - `scripts/ev_threshold_sweep.py` — thr スイープ + 利益最大点探索
- レポート(reports/):
  - ev_selection_2026-04-28.md
  - ev_verify_2026-04-28.md
  - ev_audit_2026-04-28.md
  - ev_calibrated_2026-04-28.md
  - ev_threshold_sweep_2026-04-28.md

## 結論判断

**実運用は推奨しない**(年 ¥34K のために手間と精神コスト)が、**「市場越え」は技術的に達成済**として記録に残す。
今後の方針:
- データ蓄積は継続(daily_ingest 自動化済)
- ML パイプラインは維持(walk-forward は再現可能)
- 賭け運用に踏み出す前に必ずこの doc を再読
