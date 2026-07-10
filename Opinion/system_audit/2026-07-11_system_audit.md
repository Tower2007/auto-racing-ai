# 2026-07-11 システム全体監査

## 結論

現時点の基幹データは健全で、モデルの feature 列と production meta も一致している。
一方で、**実運用の安全性・測定の正本・方針文書**には修正すべき不整合がある。
最優先は戦略の再最適化ではなく、発注・ingest・損益測定を壊れにくくすること。

本監査は読み取り専用で実施した。重い walk-forward、再学習、バックフィル、実投票は行っていない。

## 実施した確認

- 単体スモーク: `test_auto_buy_guards.py` 10/10、`test_train_gate.py` 9/9、
  `test_odds_prerace_daemon.py` 6/6 が成功。
- 基幹 CSV: entries / stats / results / odds は各 280,157 行で、車番キーの null・重複・
  相互 join 欠損は 0 件。
- 日付範囲: 2021-04-26〜2026-07-10。production model は 2026-07-05 作成、
  学習データ上限は 2026-07-03 で整合。
- model meta と LightGBM feature は、ともに 55 列で順序まで完全一致。
- 監査補助: `audit_data_integrity.py` は data を変更しない読み取り専用スクリプト。

## 重大度順の指摘

### P1: 日次 ingest は途中失敗を「完了」と誤認し、再実行不能になり得る

`ingest_day.py` は `race_entries.csv` にその日の行が一つでもあれば day 全体を skip する。
一方、取得・追記は payout、各 R の program/odds/result/laps を逐次実行し、個別失敗は
ログだけで継続する。このため途中で API 失敗・プロセス停止が起きると、部分 day が永続化され、
次回は entries の存在だけを根拠に復旧不能となる。

**提案**: 日別 manifest（expected races / 各テーブル行数 / complete）を正本にする。全取得を
検証してから day 単位で merge/upsert するか、少なくとも incomplete day は強制再取得可能にする。
取得失敗は `daily_ingest` の WARN/NG に必ず返す。

### P1: 自動発注の上限は並行プロセスと購入結果不明に対して原子的ではない

`auto_buy.run_auto_buy()` は state を一度読み、発注成功後にだけ `spent_yen` を増やす。
同時刻の複数 dynamic task が同じ state を読めば、いずれも daily cap を通過して発注でき、
最後の writer が state を上書きする。また timeout 等で「実際には成立したが確認できない」場合に
spent が増えず、次の発注を許す。

**提案**: ファイルロック下で `spent + reserved` を検査し、発注前に reservation を永続化する。
結果は `executed / failed / unknown` で精算し、unknown は order history で照合されるまで
cap を消費したままにする。上限は「楽観的な成功判定」ではなく「保守的な資金予約」で守る。

### P1: 三連系の購入対象と停止基準の対象が異なる

購入側は RF3 を伊勢崎・浜松・飯塚・山陽で作るが、`weekly_status` の三連系 health は浜松・山陽
しか集計しない。現データでも飯塚 RF3 は 17 レース、投資 ¥1,700、払戻 ¥0 である一方、
停止判定には入らない。反対に共通の stop flag は全場の三連系を止める。

**提案**: policy registry を正本にして、購入可否・監視・停止フラグを同じ
`venue × bet_type` 単位から生成する。少なくとも現行の飯塚・伊勢崎 RF3 を監視に含めるまで、
自動購入は保守的に止める価値がある。

### P2: 日次 pick / snapshot の重複定義が評価スクリプト間で一貫しない

`daily_predict_picks.csv` には同 race/car の別 batch 行があり、picks audit は batch を含む key でしか
除外しない。snapshot evaluator は同 race/car の重複を除外しない。現時点の strict dedup との差は
virtual fns ROI 102.4%→102.2%、snapshot ROI 101.3%→100.9% と小さいが、再実行時には増幅し得る。

**提案**: 「実際に送った一意の推奨」「同一 R の最新 snapshot」「実購入」を別データセットとして
明文化し、各 evaluator が同じ selection rule（first/last/closest-to-send）を共有する。raw log は
消さず、評価用 view だけを固定する。

### P2: EHI は実運用戦略の health を表していない

EHI は closing 単勝オッズの 1番人気だけから算出する市場構造指標であり、発火時 odds、pred top-1、
EV 閾値、RF3/RT3、実購入額とは直接対応しない。fav tie の race は複数行が平均に入る点も、
race 数と重みが一致しない。直近では EHI が HEALTHY でも実購入ポートフォリオは ROI 91.2% だった。

**提案**: EHI は `Market Favorite Bias Monitor` に改称して参考指標へ下げる。運用 health は
`推薦 → 発注 → 注文履歴 → 払戻` の strategy-specific ledger で、固定した期間・券種・場別に
表示する。停止判断に使うのは後者だけにする。

### P2: 運用ドキュメントが実態と衝突している

`docs/project_overview.md` は手動投票・賭け自動化なし・山陽除外を正本のように説明するが、
現在は auto-buy、dynamic schedule、三連系、山陽を含む code が存在する。
`ev_strategy_findings.md` も fns ¥100 固定・n=100 まで券種固定と書く一方、三連系の本番稼働を
別節に持ち、現行の purchase universe との対応が曖昧である。

**提案**: `docs/current_operating_policy.md` を単一正本にする。そこに environment-independent な
購入可能券種/場、既定 dry-run、cap、kill switch、停止基準、measurement source、変更手続きを記載し、
overview はそこへのリンクに留める。

### P2: policy 選択は同一 OOF で多数比較した後の採用で、最終 holdout がない

三連系の venue / bet type / threshold を同じ post-half OOF で多数比較して選んでいる。
探索としては有用だが、採用 ROI は選択バイアスを含む。現行の live 実績もまだ小標本で、全実購入
190 R は ROI 91.2%（95% bootstrap: 69.5–115.7%）、strict dedup virtual fns 312 R は 102.2%
（91.7–113.6%）で、いずれも優位性を確定できない。

**提案**: 既存方針を policy version として凍結し、選定に未使用の時系列 holdout を一度だけ使う。
以後は live の receipt-based ROI を primary とし、threshold/venue/券種の探索は shadow に隔離する。

## 追加の改善提案

1. `Order ledger`: pick_id を発行し、推薦、送信時 odds、予約、実注文 ID、履歴、払戻を一行で結ぶ。
2. `write lock`: CSV append と prediction log にプロセス間ロックを導入する。動的スケジューラ下の
   header race / 行重複を防ぐ。
3. `policy contract test`: 購入対象 venue/bet type と weekly health の対象集合が一致すること、
   停止 flag が想定範囲だけ止めることをテストする。
4. `reconciliation gate`: 毎朝、reserved/unknown order と履歴の未照合が残れば新規自動購入を停止する。
5. `model gate`: AUC だけでなく、固定の holdout における calibration/logloss と policy-level EV/ROI
   を診断値として保存する。ただし live n が十分になるまで自動採用条件は動かさない。

## 方針への所感

「モデルを頻繁にいじらず、実測パイプラインを先に固める」という方向性は正しい。ただし現在は
**手動検証 Phase A の記述**と**自動・三連系を含む実資金運用**が混在している。今の最大のリスクは
予測精度そのものより、何を買い、何が成立し、どの数字で継続・停止を判断するかが一意でないこと。
まず P1 の資金・データ耐障害性を直し、次に measurement 正本と docs を一本化する順を推奨する。
