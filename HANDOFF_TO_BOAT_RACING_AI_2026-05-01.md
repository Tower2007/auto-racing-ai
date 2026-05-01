# 依頼書: boat-racing-ai への autorace 知見移植検証

**発信元**: auto-racing-ai プロジェクト (auto_racing_ai/)
**宛先**: boat-racing-ai プロジェクト
**日付**: 2026-05-01
**目的**: autorace で得た edge 検証手法・運用知見を競艇に適用し、収益スケール可能性を判定

---

## 0. なぜこの依頼か

auto-racing-ai は 25 ヶ月 walk-forward で **真の edge (z-score +24.3σ)** を確認したが、
**autorace の 複勝プールが極小 (median ¥6,000)** のため、¥300/件 以上のベットで
ROI が崩壊し、年 +¥21k 程度が収益上限となる構造的限界に到達した。

競艇は autorace の **約 1,500 倍のプール規模** (中規模 R で ¥1,000 万)。同じ edge が
あれば年 +¥1M 以上にスケールする。これを検証してほしい。

---

## 1. autorace で確立した検証手法 (転用してほしい)

### 1-1. 真の edge かどうかの厳密検証 (`scripts/edge_validation.py`)

「結果論ではないか」を否定するため、以下 3 段論法を使った。boat でも同じ手法で
検証可能:

| 手法 | autorace 結果 |
|---|---|
| **(a) ランダム picks bootstrap (100 シード × 1R1車)** | mean ROI 82.8% ± 2.0% (= 控除率 17.2%) |
| **(b) 反予想 (pred 最下位車) 対照群** | ROI 57.8% ← モデルの予測力を独立確認 |
| **(c) サブサンプル安定性 (25mo を 5 等分)** | 5 期間とも ROI 125-140% |
| 本命戦略 | ROI 132.5%, **z-score +24.3σ** |

→ **本命 ROI が「ランダム distribution の何σ」と「サブサンプル全期間で安定か」**
の 2 軸で判定。boat でも必須。

### 1-2. 自分のベットインパクト計算 (`scripts/realistic_roi.py`)

eval ROI は「ベットがプールに影響しない」仮定。実 ROI は:

```
new_payout_per_¥100 = (R × T + b × (1-r) / 3) / (T + b/100)
  R: 元 payout per ¥100
  T: 当該車 votes (= refund_votes)
  b: ベット額 (yen)
  r: 控除率
  /3: 複勝の 3 winning cars 等分配仮定
```

これで eval 132% が実 115% (¥100 ベット) ~ 64% (¥1000 ベット) に修正される。
boat の場合 (1R 6艇、複勝 2艇 etc) で同じ計算を実装してほしい。

### 1-3. 信号 persistence 解析 (`scripts/odds_snapshot_eval.py`)

発火時オッズ (例 -5min) と確定後オッズの差分を測定。autorace では「人気車に late
money が集中して odds drift down」を観測。boat でも同様の現象あるはず。

データ収集: `data/odds_snapshots.csv` に発火時 snap を append → 後日確定 odds と join。

---

## 2. autorace 戦略の最終形 (転用検討)

### 2-1. 中間モデル (オッズあり、試走なし)

訓練: 6 CSV (entries, stats, results, laps, payouts, odds_summary) → 55 特徴量
モデル: LightGBM, target=top3 (複勝的中)
校正: isotonic regression (走行データ直前カットオフで分割)

特徴量: pre-race info のみ (オッズ・選手過去成績・コース条件)
**結果情報リーク無し** (rank系 4 個も全て pre-race info)

### 2-2. EV 戦略

- pred-top1 (校正済確率 1 位車) の中で `EV = pred_calib × (place_min + place_max) / 2 ≥ 1.50`
- 25 ヶ月で +¥59,690 (理論値、¥100 ベット)
- 月勝率 25/25, 最大連敗 7, 最大DD ¥1,280

### 2-3. 検討して採用しなかった案 (boat でも警戒)

| 案 | 不採用理由 |
|---|---|
| max-EV (top1 縛り無し) | 1.9% の big-pay 依存 = 宝くじ型、最大連敗 34 |
| 3点 BUY (複勝+3連単+3連複) | top 3 ヶ月の上振れに 84-93% 依存、本質 edge 無し |
| EV-top3 box (3連複) | 控除率 30% × 命中率 4.3% = 大赤字 (ROI 62%) |
| 複勝=pred + 3連=max-EV ハイブリッド | 上記の合算でも -¥373k (-25mo) |

教訓: **3連系は控除率が高すぎて単一車 edge では到達不能**。複勝のみが現実的。

---

## 3. 運用システムの構成 (boat 側で参考に)

### 3-1. データパイプライン
- `daily_ingest.py` (06:30): 昨日結果 + 出走表ingest
- `weekly_status.py` (月曜 07:30): データ収集ステータス + 累積成績メール

### 3-2. 動的発火スケジューラ
- `dynamic_scheduler.py` (毎朝 07:00): Program/Print から各 R 発走時刻取得
- `AutoraceDyn_{venue}_R{n}` (各 R 発走 -5 分): `daily_predict.py --venues X --races N`

### 3-3. メール通知
- 各 R 発走 -5min で daily_predict 起動
- top1 EV >= thr なら通知
- top1 EV NaN: 60s × 2 retry (人気車の fns 未公開対応)
- top1 EV ≥ thr-0.30: 60s × 2 retry (drift up 救済) ← **NEW**
- メール末尾に累積成績 (戦績・ROI) 表示

### 3-4. Streamlit エンタメアプリ
- `app/streamlit_app.py`: ライブ予想表示
- 30 秒毎 auto-rerun + 各 R 発走 -5min で全件 refresh
- BUY 推奨は -5min 以降のみ点灯 (drift bias 考慮)
- 投票ボタンで autorace.jp 公式投票へ誘導

---

## 4. boat-racing-ai に依頼したい具体的問い

### 4-1. 必須 (採算性判断のため)

1. **既存の boat 戦略の edge 検証**
   - `scripts/edge_validation.py` (autorace) と同等のスクリプトを実装し、
     pred-top1 + EV>=thr のランダム対照・反予想・5 期間サブサンプルを出す
   - z-score が ≥3σ あれば真 edge、それ未満なら戦略再検討

2. **複勝プール規模の実測**
   - autorace の median ¥6,000 と比較
   - boat の median, mean, 99%tile を場別 (24 場) × R 番号で集計
   - 想定: ¥100 万-3,000 万 (autorace の数百倍)

3. **realistic ROI by bet 額** (¥100, ¥500, ¥1k, ¥5k, ¥1万, ¥3万 各バケット)
   - 公式: `new_payout_per_¥100 = (R*T + b*(1-r)/2) / (T + b/100)`
     ※ 競艇複勝は 2 艇 (top-2) なので /3 → /2 に注意
   - 月利益 / 年利益のスケール曲線を出す
   - autorace は ¥300 で頭打ち、boat は ¥1万-¥3万 まで利益拡大できると予想

### 4-2. 任意 (転用余地検討)

4. **odds drift の場別・節別差**
   - 競艇は SG (G1) と一般戦で pool 規模が大きく違う
   - drift パターンも違う可能性 → snapshot 蓄積で測定

5. **賢い refresh / リトライ機構**
   - autorace の dynamic_scheduler + LEAD_MIN=5 + retry の構成は転用可能
   - 競艇の場合 1 日 144 R もあるので、全レース監視は API 負荷大
   - 場/節を絞ってからの動的発火を推奨

6. **boat 既存実装の現状と最終戦略**
   - CLAUDE.md に「boat-racing-ai の教訓: walk-forward 検証必須、集計 ROI に騙されない」
     とあるので、過去に何らかの結論が出ている可能性
   - 現状の戦略・ROI・運用状況を共有してほしい

---

## 5. 期待される結論パターン

| ケース | 結論 |
|---|---|
| boat に z≥3σ 真 edge 確認、pool 大 | ⭐ **autorace 戦略を boat に移植 + ベット ¥1万級** で年 +¥1M 規模可能 |
| boat に edge 確認、pool 小 (autorace 並) | autorace と同程度、別領域へ |
| boat に edge 無し、しかし他券種 (二連単等) で edge 探索余地 | 探索継続 |
| boat にも edge 無し | 公営競技は諦め、別ドメインへ |

---

## 6. 引き渡し方

### 6-1. autorace 側 (本プロジェクト) で参照可能なファイル
```
scripts/edge_validation.py       # 真 edge 検証 (z-score, 対照群, サブサンプル)
scripts/realistic_roi.py         # 自分インパクト込み実 ROI 計算
scripts/odds_snapshot_eval.py    # 発火時 snap vs 確定 odds の persistence 解析
scripts/ev_strategy_compare.py   # max-EV 検証 (宝くじ型 trap 検出)
scripts/ev_hybrid_compare.py     # 複勝+3連 ハイブリッド検証 (boat も控除率高で同じ結論予想)
scripts/build_votes_lookup.py    # 場×R 別推奨ベット額算出
daily_predict.py                 # 推奨提示型運用 (メール送信、累積成績フッタ)
dynamic_scheduler.py             # 動的発火スケジューラ
app/streamlit_app.py             # エンタメ版ライブ予想
docs/ev_strategy_findings.md     # 戦略仕様書
HANDOFF_TO_BOAT_RACING_AI_2026-05-01.md  # 本資料
```

### 6-2. 確認順
1. このファイル (HANDOFF) を読む
2. `scripts/edge_validation.py` の構造を boat 用に移植
3. realistic_roi 計算で boat の bet 額別 ROI を出す
4. 上記 §4 の 1-3 (必須項目) の結果を boat 側 HANDOFF に記載

---

## 7. autorace 側の現状 (参考情報)

- 運用開始: 2026-04-29
- 戦略: pred-top1 + EV>=1.50, ¥100/件 (推奨額機能で場×R 別調整可)
- LEAD_MIN: 発走 5 分前発火, 60s × 2 retry
- 累積成績: メール末尾 + weekly_status で可視化
- データ: 2021-04-26 〜 現在、5 場 (川口/伊勢崎/浜松/飯塚/山陽)
- 期待値: 年 +¥21k (¥100 ベット), 上限 +¥24k (¥300 ベット)

技術スタック: Python 3.13, LightGBM, pandas, sklearn, Streamlit, Gmail SMTP

---

## 8. 連絡

質問・補足必要な場合は、auto-racing-ai 側の以下のメモを参照:
- `CLAUDE.md`: プロジェクト全体ガイド
- `docs/ev_strategy_findings.md`: 戦略決定の経緯
- memory ディレクトリ (Claude Code 記憶): 過去決定の判断根拠

以上、よろしくお願いします。
