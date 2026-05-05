# 2026-05-06: ML予想ロジック監査メモ

## 結論

現時点で、予想モデル本体に「結果列を特徴量へ混ぜた」「walk-forward の訓練月にテスト月を混ぜた」
という類の致命的リークは見つからなかった。

ただし、評価・運用周辺に以下の穴がある。特に 1 は、固定仮説として今後も追跡するなら修正優先度が高い。

## Findings

### 1. 3点BUY / policy 系評価の calibration split がデータ追加で動く

対象:
- `scripts/ev_3point_buy.py:48-59`
- `scripts/ev_3point_monthly.py:41-52`
- `scripts/ev_3point_policy_sim.py:30` 経由

`walkforward_predictions_morning_top3.parquet` の月リストを半分に割って
`calib_months = months[:half]`, `eval_months = months[half:]` としている。
このため、データが 1 ヶ月増えるだけで calibration/eval の境界が動き、過去に出した
`baseline_fns_only` や policy 比較の数字が静かに変わる。

2026-05-06 時点では偶然 `2024-04` 境界に近いが、今後 2026-05 が加わると境界がずれる可能性がある。
「Phase A 固定仮説」「再最適化しない」と相性が悪い。

提案:
- policy 系も `CALIB_CUTOFF = "2024-04"` に固定する。
- レポートには必ず `calib=<2024-04 / eval>=2024-04` を明記する。
- 動的 half split は探索用に残すなら関数名・レポート名に `exploratory` を付ける。

### 2. production_calib の metrics は in-sample calibration 指標

対象:
- `ml/train_production.py:125-145`

本番用 calibrator は OOF 予測全体で isotonic を fit し、その同じデータで
`calib_logloss` / `calib_auc` を計算している。live 用 calibrator としては問題ないが、
この metrics は honest な評価指標ではない。

提案:
- `production_meta.json` の `calib_metrics` は「fit data 上の診断値」として扱う。
- 校正性能の評価は `CALIB_CUTOFF` split または rolling/prequential calibration で別途出す。

### 3. target_top3 は着順由来で、実際の複勝払戻 target と完全一致しないレースが少数ある

監査結果:
- finished/non-absent races: 36,039
- `top3` 数が期待値と違う R: 26
- `target_win` が 1 着 1 人になっていない R: 13

対象:
- `ml/features.py:_build_target`
- `src/parser.py:146-164`

失格・同着・判定系の特殊ケースと思われる。件数は全体の 0.1% 未満で、現行 ROI の主張を壊す規模ではない。
ただし、複勝モデルの target を本当に「fns が払戻対象だったか」に合わせるなら、
`payouts.csv` の `bet_type == "fns"` を正本にした target も作って比較した方がきれい。

提案:
- すぐ止める必要はない。
- 次の再学習・モデル監査時に `target_fns_hit` を作り、現 target との差分行だけ確認する。

### 4. production model は early stopping 後に全データ refit していない

対象:
- `ml/train_production.py:85-123`

末尾 10% を validation に切り、残り 90% だけで学習した Booster をそのまま保存している。
これは過大評価方向のバグではなく、むしろ最新データを model fit に使えていないという underfit/staleness 側の問題。

提案:
- best_iteration を決めた後、同じ best_iteration で全 kept data を使って final model を refit する案を検討。
- ただし live n がまだ少ないので、緊急度は 1 より低い。

## Negative Findings

以下は確認した範囲では問題なし。

- `race_entries.csv` / `race_stats.csv` / `odds_summary.csv` / `race_results.csv` / `ml_features.parquet` の `race_date, place_code, race_no, car_no` 重複は 0。
- `walkforward_predictions_morning_top3.parquet` の key 重複は 0。
- `ml/walkforward_morning.py` は `df_kept["year_month"] < tm` で訓練し、テスト月 `== tm` を分離している。
- 中間モデルは `trial_run_time`, `trial_diff_*`, `race_trial_*`, `ai_expect_code` を除外している。
- closing odds 使用は既知の問題で、コード上の新規バグではなく「評価値は closing odds backtest」と表現すべき問題。

## 監査成果物

- `Opinion/ml_logic_audit/audit_ml_logic.py`
- `Opinion/ml_logic_audit/audit_results.md`
