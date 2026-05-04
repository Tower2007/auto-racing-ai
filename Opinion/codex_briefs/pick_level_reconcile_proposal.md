# 将来 Codex brief: pick-level reconcile

作成: 2026-05-05(R9 振り返りで起票)
状態: **保留 / 未依頼**

## 背景

`scripts/reconcile_recommendations_vs_bets.py`(R6/R7 で導入)は
`race_date × place_code × race_no` の R 単位でしか照合していない。

Codex R9 指摘:
> bet_history.csv 側を race_date/place_code/race_no だけで集約しているため、
> 推奨があった R で別の車番や別券種を買っていても A=推奨かつ購入済みに
> 分類されます。「推奨内容 / 実購入内容の照合」と読むと過大に一致して
> 見える可能性があります。

現状は Phase A 監視(R 単位の取りこぼし検知)として割り切り、docstring と
weekly mail 表示に「R 単位 / 車番・券種は別」と明記して期待値ズレを防いだ。

## 厳密化の余地

`data/bet_history_detail.csv` には pack 単位(bet_type_code + pack_deme)で
購入内容が記録されている。これと `daily_predict_picks.csv`(車番・推奨 EV あり)
を突き合わせれば pick-level reconcile が可能。

## 提案する Codex への依頼内容(時期未定)

1. `scripts/reconcile_pick_level.py` を新規作成
2. キー: `race_date × place_code × race_no × car_no × bet_type`
3. `bet_type` は推奨側 = "fns"(複勝)固定なので、まずは fns のみ pick-level
   で照合 → 拡張しやすい設計に
4. 4 カテゴリ命名:
   - `pick_recommended_and_bought_exact`
   - `pick_recommended_other_bought`(同 R で別車・別券種購入 = 元 A の一部)
   - `pick_recommended_not_bought`
   - `pick_other_bought_no_recommend`(裁量)
5. weekly では件数のみ、明細は `--csv-out` で出力(R6 の R7 化と同じ方針)
6. CSV に `manual_reason` 列予約
7. 既存 R 単位 reconcile は残す(運用と pick-level の二段表示)

## 横展開の可能性

boat-racing-ai / keiba にも同じ R 単位 reconcile を移植すれば、3 プロジェクトで
同じ pick-level 拡張ニーズが発生する見込み。**共通化先行ポリシー** に従い、
mock test(R6 で保留)と同じタイミングで Codex に依頼する形が ROI 高い。

## 実装優先度

低い。Phase A は live n=16(2026-05-05 時点)の段階で、R 単位監視で十分。
n=50+ で Phase A 停止基準が機能し始め、かつ「推奨と違う買い方」の頻度が
上がってきたら厳密化を検討する。
