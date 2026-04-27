# データ品質レポート (2026-04-27)

対象: `data/*.csv`(backfill 2021-04-26 〜 2026-04-26、完了 2026-04-27)

## 1. 行数とサイズ

| CSV | 行数 | サイズ (MB) |
|---|---:|---:|
| race_entries | 268,109 | 26.0 |
| race_stats | 268,109 | 26.7 |
| race_results | 268,109 | 19.4 |
| odds_summary | 268,109 | 14.9 |
| payouts | 380,001 | 15.7 |
| race_laps | 1,852,134 | 39.2 |

## 2. 日付カバレッジ

- 期間: 2021-04-26 〜 2026-04-26 (1827 日)

### 場別 race-day 数(出走表ベース)

| 場 | race-day 数 | 最古 | 最新 |
|---|---:|---|---|
| 川口 (pc=2) | 718 | 2021-04-26 | 2026-04-23 |
| 伊勢崎 (pc=3) | 646 | 2021-04-29 | 2026-04-15 |
| 浜松 (pc=4) | 533 | 2021-05-03 | 2026-04-26 |
| 飯塚 (pc=5) | 815 | 2021-04-30 | 2026-04-26 |
| 山陽 (pc=6) | 736 | 2021-04-26 | 2026-04-26 |

## 3. ファイル間整合性 (race_id レベル)

race_id = (race_date, place_code, race_no)

| 集合 | race_id 数 |
|---|---:|
| race_entries | 36,366 |
| race_results | 36,366 |
| odds_summary | 36,366 |

### 差分

- results にあって entries に無い race_id: **0**
- odds にあって entries に無い race_id: **0**
- entries にあって results に無い race_id: **0**
- results ∩ odds の race_id: **36,366**

## 4. 重複検知

| CSV | キー | 重複行数 |
|---|---|---:|
| race_entries | race_date,place_code,race_no,car_no | 0 |
| race_stats | race_date,place_code,race_no,car_no | 0 |
| race_results | race_date,place_code,race_no,car_no | 0 |
| odds_summary | race_date,place_code,race_no,car_no | 0 |
| race_laps | race_date,place_code,race_no,lap_no,car_no | 0 |

※ payouts: (race_date,place_code,race_no,bet_type,car_no_1,car_no_2,car_no_3) 重複 = 0 行

## 5. 主要列の NULL 率

### race_entries (n=268,109)

| 列 | NULL 数 | NULL 率 |
|---|---:|---:|
| car_no | 0 | 0.00% |
| player_code | 0 | 0.00% |
| handicap | 0 | 0.00% |
| trial_run_time | 2,956 | 1.10% |
| rank | 0 | 0.00% |
| rate2 | 0 | 0.00% |
| rate3 | 0 | 0.00% |

### race_results (n=268,109)

| 列 | NULL 数 | NULL 率 |
|---|---:|---:|
| order | 2,740 | 1.02% |
| race_time | 5,633 | 2.10% |
| st | 3,637 | 1.36% |
| trial_time | 2,956 | 1.10% |
| accident_code | 265,360 | 98.97% |
| foul_code | 266,300 | 99.33% |

### odds_summary (n=268,109)

| 列 | NULL 数 | NULL 率 |
|---|---:|---:|
| win_odds | 3,840 | 1.43% |
| place_odds_min | 48,852 | 18.22% |
| place_odds_max | 48,852 | 18.22% |

### payouts (n=380,001)

| 列 | NULL 数 | NULL 率 |
|---|---:|---:|
| refund | 2,942 | 0.77% |
| pop | 0 | 0.00% |
| refund_votes | 0 | 0.00% |
| car_no_1 | 3,383 | 0.89% |
| car_no_2 | 126,101 | 33.18% |
| car_no_3 | 307,188 | 80.84% |

### race_laps (n=1,852,134)

| 列 | NULL 数 | NULL 率 |
|---|---:|---:|
| lap_no | 0 | 0.00% |
| rank | 0 | 0.00% |

## 6. 着順 / 失格 分布

総結果行: 268,109

### 着順分布

| order | 件数 | 比率 |
|---:|---:|---:|
| 0 | 2,893 | 1.08% |
| 1 | 35,959 | 13.41% |
| 2 | 35,951 | 13.41% |
| 3 | 35,952 | 13.41% |
| 4 | 35,926 | 13.40% |
| 5 | 35,892 | 13.39% |
| 6 | 35,531 | 13.25% |
| 7 | 30,571 | 11.40% |
| 8 | 16,694 | 6.23% |
| NULL (失格/欠車) | 2,740 | 1.02% |

### accident_code 分布(NULL 以外)

| code | name | 件数 |
|---|---|---:|
| 530.0 | 反妨 | 616 |
| 100.0 | 欠車 | 521 |
| 800.0 | 他落 | 444 |
| 300.0 | 自落 | 222 |
| 120.0 | 欠責 | 191 |
| 311.0 | 落因 | 175 |
| 310.0 | 落妨 | 144 |
| 500.0 | 内突 | 113 |
| 531.0 | 反因 | 86 |
| 200.0 | 停止 | 57 |
| 510.0 | 外突 | 44 |
| 540.0 | 周誤 | 35 |
| 400.0 | 故障 | 29 |
| 810.0 | 未完 | 21 |
| 420.0 | 故妨 | 10 |
| 441.0 | 故因 | 10 |
| 600.0 | 故完 | 9 |
| 220.0 | 停責 | 8 |
| 440.0 | 妨故 | 6 |
| 210.0 | 停妨 | 3 |
| 532.0 | 危険 | 2 |
| 110.0 | 欠妨 | 1 |
| 230.0 | 再停 | 1 |
| 410.0 | 故落 | 1 |

### foul_code 分布(NULL 以外)

| foul_code | 件数 |
|---|---:|
| F | 1,389 |
| W | 373 |
| A | 39 |
| L | 5 |
| B | 3 |

## 7. オッズ異常値

odds_summary 行数: 268,109

### win_odds 統計

- 非 NULL 件数: 264,269
- NULL: 3,840
- 0.0 (要注意): **0**
- 中央値: 9.90
- 平均: 19.53
- 最大: 1343.00
- ≥ 100: 5,289
- ≥ 500: 35
- ≥ 1000: 1

### place_odds 範囲チェック

- place_odds_min > place_odds_max(逆転): 0
- place_odds_min が NULL: 48,852
- place_odds_max が NULL: 48,852

## 8. 周回データ整合性

- race_results に存在する race_id: 36,366
- race_laps に存在する race_id: 36,013
- laps が無い race_id: **353**
- laps はあるが results に無い race_id: 0

### レースあたりの lap 数分布

| lap 数 | 件数 |
|---:|---:|
| 7 | 35,747 |
| 9 | 241 |
| 11 | 25 |

## 9. 払戻データ整合性

### 券種別行数

| bet_type | bet_name | 行数 |
|---|---|---:|
| wid | ワイド | 108,306 |
| fns | 複勝 | 89,722 |
| rt3 | 3連単 | 36,422 |
| rtw | 2連単 | 36,397 |
| rf3 | 3連複 | 36,391 |
| rfw | 2連複 | 36,384 |
| tns | 単勝 | 36,379 |

### refund 異常値

- refund NULL: 2,942
- refund = 0: 7,764
- refund ≥ 100,000 (10万円): 810
- refund 中央値: 330
- refund 最大値: 4751760
