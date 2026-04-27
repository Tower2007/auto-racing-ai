# ML holdout 評価レポート (2026-04-28)

- target: `target_top3`
- test 期間: 末尾 6 ヶ月 (cutoff = 2025-10-25)
- train 行数: 211,337 / val: 23,687 / test: 27,452
- best_iteration: 274

## Test metrics

| metric | value |
|---|---:|
| n | 27452 |
| positive_rate | 0.4070 |
| logloss | 0.4902 |
| auc | 0.8326 |
| brier | 0.1628 |
| ap | 0.7756 |
| topk_pick_acc | 0.6963 |
| win_n_bets | 3725 |
| win_hit_rate | 0.4779 |
| win_roi | 0.7166 |
| place_n_bets | 3724 |
| place_hit_rate | 0.7846 |
| place_roi | 0.9293 |

## Odds-only baseline

| metric | value |
|---|---:|
| logloss | 1.1730 |
| auc | 0.7716 |

## Top 25 features by gain

| feature                   |      gain |   split |
|:--------------------------|----------:|--------:|
| win_odds_rank             | 304606    |     247 |
| win_odds                  | 183512    |     730 |
| trial_diff_mean           |  48859    |    1341 |
| graduation_code           |  26768.5  |    1697 |
| place_odds_min_rank       |  16976.3  |     305 |
| race_dev_num              |  11738.8  |     715 |
| place_odds_min            |  11406.1  |     611 |
| car_no                    |   9896.53 |     491 |
| win_rate3                 |   8094.55 |     597 |
| win_implied_prob          |   7847.03 |     133 |
| trial_diff_min            |   6801.68 |     348 |
| month                     |   6704.55 |     597 |
| race_no                   |   6489.09 |     591 |
| race_n_cars               |   6478.97 |     272 |
| place_odds_max            |   5850.07 |     520 |
| race_trial_mean           |   5337.69 |     470 |
| win_rate1                 |   5204.24 |     445 |
| rank_num                  |   4827.66 |     461 |
| good_track_rate2_180d     |   4624.85 |     427 |
| win_rate2                 |   4420.4  |     401 |
| wet_track_rate2_180d      |   4420.06 |     395 |
| good_track_race_ave       |   3923.2  |     373 |
| handicap                  |   3584.38 |     243 |
| good_track_run_count_180d |   3453.82 |     333 |
| log_win_odds              |   3385.91 |      69 |