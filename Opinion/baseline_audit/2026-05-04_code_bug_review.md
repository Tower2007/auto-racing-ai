# odds snapshot / live ROI 周辺コード監査 (2026-05-04)

## 結論

コードバグの可能性は実際にあった。特に `scripts/odds_snapshot_eval.py` の未確定レース処理は live ROI を大きく過小評価していた。

## P1: 未確定レースを 0 払戻として評価に混ぜている

対象: `scripts/odds_snapshot_eval.py:90-106`

現状:

```python
snap_with_pay = snap.merge(fns, on=RACE_KEY + ["car_no"], how="left")
has_pay = snap_with_pay.dropna(subset=["payout"])
n_pay_races = has_pay.groupby(RACE_KEY).ngroups
snap_with_pay["payout"] = snap_with_pay["payout"].fillna(0).astype(int)
snap_with_pay["hit"] = (snap_with_pay["payout"] > 0).astype(int)
...
eval_df = snap_with_pay.dropna(subset=["payout"]).copy()
```

`fillna(0)` 後に `dropna(subset=["payout"])` しているため、未走 / 払戻未取り込みレースもすべて 0 払戻として `eval_df` に残る。

実データ影響:

- `odds_snapshots.csv`: 79R / 554 rows
- fns 確定済: 49R
- 現行ロジック: 79R すべて評価対象
- 現行表示: pred-top1 EV>=1.50 は n=20, ROI=67.0%
- 確定済 49R のみに絞ると: n=13, ROI=103.1%, hit=69.2%

つまり、`ROI 67%` は未確定 7 picks を外れ扱いした過小評価。

修正案:

```python
snap_with_pay = snap.merge(fns, on=RACE_KEY + ["car_no"], how="left")
confirmed_races = snap_with_pay.dropna(subset=["payout"])[RACE_KEY].drop_duplicates()

eval_df = snap.merge(confirmed_races, on=RACE_KEY, how="inner")
eval_df = eval_df.merge(fns, on=RACE_KEY + ["car_no"], how="left")
eval_df["payout"] = eval_df["payout"].fillna(0).astype(int)
eval_df["hit"] = (eval_df["payout"] > 0).astype(int)
```

ポイント:

- レース単位で「確定済」を判定する。
- 確定済レース内の非的中車だけ 0 払戻にする。
- 未確定レースは評価対象から外す。

## P2: snapshot 評価が実際の送信 picks と完全一致しない

対象: `daily_predict.py:691-714`

現状は初回 `predict_race` の直後に `append_odds_snapshot(df, time_label)` し、その後 near-miss retry で閾値を跨いだ場合、実際の送信 pick は retry 後の odds だが、snapshot は初回 odds のまま。

実データ例:

```text
2026-05-03 川口 R5 車7
snapshot ev_avg_calib = 1.490278
daily_predict_picks ev_avg_calib = 1.549819
```

この pick は実際には送信されているが、`odds_snapshot_eval.py` の `snap EV>=1.50` 条件では拾われない。

修正案:

- 送信対象を確定した後の `df` も snapshot として保存する。
- もしくは live ROI は `data/daily_predict_picks.csv` を正本にし、`odds_snapshots.csv` は drift 分析専用にする。

推奨:

`daily_predict_picks.csv` を「実際にユーザーへ提示した picks」の正本にする。snapshot は全車 odds drift 解析用。

## P3: odds_snapshots.csv に同一 race/car の重複があり、集計前に扱いを決めるべき

対象: `daily_predict.py:544-563`, `scripts/odds_snapshot_eval.py`

実データ:

- `odds_snapshots.csv` 554 rows
- `(race_date, place_code, race_no, car_no)` 重複 rows: 14
- 重複 group: 7
- 例: `2026-05-01 山陽 R1` が `10:41:14` と `10:46:52` の 2 snapshot を持つ

現状の `odds_snapshot_eval.py` は重複をそのまま評価するため、同じ race/car が複数回数えられる可能性がある。現データでは top1 重複は 1 race のみで影響は小さいが、今後の再実行や retry で増える。

修正案:

- drift 分析: first / last / sent_at closest のどれを見るか明示する。
- live ROI: `daily_predict_picks.csv` を使う。
- snapshot persistence: race/car ごとに `captured_at` の最終行または送信時刻に最も近い行へ dedup する。

## P4: daily_predict_picks.csv と odds_snapshots.csv の対象期間が違う

`daily_predict_picks.csv` は 2026-04-29 から 30 picks あるが、`odds_snapshots.csv` は 2026-04-30 からで、初期 8 picks は snapshot がない。

実際の送信 picks ベースで確定済だけ集計すると:

- 全 picks: confirmed 21 picks, ROI 118.1%, hit 76.2%
- 2026-05-01 以降: confirmed 13 picks, ROI 103.1%, hit 69.2%

よって、現時点では `ROI 67%` より `確定済送信 picks ROI 103.1%` の方が実態に近い。ただし n=13 なので結論は保留。

## 優先修正

1. `scripts/odds_snapshot_eval.py` の未確定レース除外バグを修正。
2. live ROI の正本を `daily_predict_picks.csv` に変更。
3. `odds_snapshots.csv` は drift / persistence 専用にし、dedup 方針を明示。
4. docs / Opinion の `ROI 67%` 表現は「現行スクリプトのバグ込み」と注記または撤回。

