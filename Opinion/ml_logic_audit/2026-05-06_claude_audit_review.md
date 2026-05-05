# 2026-05-06: ML 予想ロジック独立監査(Claude 視点)

Codex の監査(`Opinion/ml_logic_audit/2026-05-06_ml_logic_audit.md`)を読んだ上で、
Claude が独立に追加角度の検査を回した結果。**メインプロジェクトは未編集**、
Opinion/ 配下のみ編集。

監査スクリプト: `Opinion/ml_logic_audit/claude_audit_extra.py`
監査結果生データ: `Opinion/ml_logic_audit/claude_audit_results.md`

## 1 行結論

**Codex の核心結論「モデル本体に致命的リークはない」を独立検証で補強できた。**
特に Codex が「強い疑い」と濁した `this_year_win_count` 10.7% exact match は、
**再計算側のバグの可能性が高い**(本物の API 値はリセット挙動が正しい)。

ただし Codex 4 findings には全部同意 + 1 件追加観点。

## Codex finding ごとの再評価

| Codex finding | Claude 評価 | 補強検証 |
|---|---|---|
| 1. policy/3点BUY の calib split が動的 | ✅ 同意 / **必須修正** | コード再読で確認、CALIB_CUTOFF="2024-04" に統一すべき |
| 2. production_calib metrics は in-sample | ✅ 同意 / **推奨** | 検証 8 で月別 pred 分布が安定 → calibrator 自体は OK、metrics 表示だけ honest 化 |
| 3. target_top3 の特殊レース 26+13 | ✅ 同意 / **優先低** | 0.1% 未満、ROI 132.9% を 132.5% に動かす程度 |
| 4. production model が refit していない | ✅ 同意 / **優先低** | underfit 方向、緊急度低 |

## Claude が追加で検証した 8 項目

### ✅ 強い positive 発見(リーク否定)

#### A. race_stats は「そのレース時点」の snapshot で間違いない
- 同選手の `total_win_count` を時系列で並べたとき、**減少ペア = 0 / 269,161 ペア**
- もし後日 snapshot なら、過去レコードの total_win_count が「現在値」で書かれて
  時系列順に並べると減少することがあるはず → **完全に単調非減少**
- **temporal leakage の最重要 sanity が clean** と確定。Codex が「強い疑いではない」
  と濁したところを「**確定 clean**」と言える

#### B. this_year_win_count もちゃんと年初リセットされる
- 月別平均: 1月 = **0.02**、12月 = **0.49**(リセット効いている)
- 12月 max = 17、過去の Codex 検証で出てきた「同年 120 勝」は再計算側のバグ濃厚
- 選手 3307 の 2025 年で確認: 1/2 で this_year=0 → 12/31 で this_year=17(完璧)
- → **Codex finding 「exact match 10.7%」は再計算式の問題で、API 値は正しい**

#### C. walk-forward の test_month と race_date が完全整合
- `race_date.month != test_month` の不一致 = **0 件**
- CAR_KEY 重複 = 0
- → walk-forward の月別分割は完全に正しい

#### D. categorical encoding が月別で安定
- 49 ヶ月の pred 分布: mean 0.40 ± 0.01 / std 0.24 ± 0.02 / min, max もスムーズ
- ジャンプなし → 月毎再訓練でも categorical の符号化は実質一致
- LightGBM 'auto' categorical_feature が安全に効いている

### ⚠️ 注意点(リークではないが運用観点)

#### E. win_odds 1番人気の overround 0.76 = 顕著な過剰評価
| win_rank | n | win_hit | implied_win_prob | overround_factor |
|---:|---:|---:|---:|---:|
| 1 | 36,903 | 0.464 | 0.610 | **0.760** |
| 2 | 36,074 | 0.219 | 0.230 | 0.951 |
| 3 | 36,051 | 0.127 | 0.135 | 0.939 |
| 6 | 36,271 | 0.030 | 0.033 | 0.911 |

- 中位人気は overround 0.93〜0.98(= 控除率 30% から逆算した期待値とほぼ一致)
- **1 番人気だけ 0.76 と顕著に低い** = closing odds 上で 1 番人気は実力以上に売れている
- これは **既知の「人気サイドに売れすぎる」現象**(ファン投票)で、Phase A の
  `pred_top1 + EV>=1.50` が edge を取れる構造的根拠の 1 つ
- **戦略は 1 番人気 fade で edge → これが closing odds backtest 132.5% を支えている**

#### F. NaN 数と hit rate の二極構造
| nan_count | n | hit_rate |
|---:|---:|---:|
| 5(主要) | 157,163 | 0.378 |
| 10 | 43,413 | 0.274 |
| 4 | 59,490 | 0.583 |
| 2 | 1,594 | 0.667 |

- nan=10 の 43k 行は欠損多 → 新人 / 復帰直後で hit rate 低い
- LightGBM は NaN を natively 扱えるので drop はしていない、**バイアスではない**
- ただし pred 値を確率として直接読むときは「nan が多い行は構造的に低 pred」になる
- 校正には影響しない(isotonic は予測値全体で fit)

#### G. categorical coverage の月変化
- place_code: 開催場の入れ替わりで月により 2〜4 場
- graduation_code: 新しい卒業期(35-38)が test に出るが train にない → LightGBM では
  unknown category として処理、影響軽微
- rank_class 'S' は最古月にあって最新月で消失 → これは S 級が引退で他クラスになった
  選手の連続性。学習時に S→A の選手は両方カバーされる

## Codex に + 1 件追加で提案したい点

### Claude 追加finding 5: odds_summary の closing-style 性質を docs に明示

検証 6 の overround パターン(中位人気 0.93-0.98 / 1番人気 0.76)は **closing odds
を非常に強く示唆**。

- これは既に Codex / Gemini 監査で「情報の鮮度料」フレームに統合済(2026-05-04)
- ただし `docs/ev_strategy_findings.md` の現行記述は曖昧で、**closing odds backtest
  での edge** = 「1 番人気の人気売れすぎ」を fade する戦略 という核心構造が
  明記されていない
- **将来 TODO**: docs に「**戦略の edge 源泉 = 1 番人気の overround 0.76 を
  fade する**」と明示。これにより:
  - Phase A の改善案が「1 番人気以外の選別」に向いて筋違いになるのを防ぐ
  - 「どこで edge が消えるか」(= 1 番人気の overround が 0.85+ に上がる時)が
    停止基準の追加候補になる

## 監査優先順位(Codex + Claude 統合)

| # | 項目 | 出所 | 緊急度 |
|---|---|---|---|
| 1 | policy split を `CALIB_CUTOFF="2024-04"` に統一 | Codex | **必須** |
| 2 | production_calib metrics を honest 別測(rolling / split) | Codex | 推奨 |
| 3 | docs に「edge 源泉 = 1 番人気 fade」を明記 | Claude | 推奨 |
| 4 | target_fns_hit を作って 26+13 R を再確認 | Codex | 低 |
| 5 | early stopping 後の refit | Codex | 低 |

1〜3 は monitoring 系の監査ループ(R6〜R9)が安定したら次の Codex / Claude 共同
タスクとして起票するのが良さそう。**ただし Recency Bias 警告(live n=100 まで
モデル変更禁止)があるので、コード修正自体は live n=50 まで保留** が原則。
docs / metrics 表示の honest 化だけは即着手 OK。

## 反論 / 反証

なし。Codex 4 findings はすべて妥当で、私の検証はそれを補強・確定する方向。
Codex の `this_year_win_count` 検証(再計算側のバグ疑い)を私が単純なリセット
挙動チェックで救えたのが、AI 協働体制 (Claude + Codex) の良い例。

## 自分が一番納得した点

検証 6 の **「1 番人気 overround 0.76」**。これまで「auto は薄い edge がある」
と漠然と言ってきたが、**何の歪みを利用しているのか** が初めて数値で見えた。
ファン投票で 1 番人気に売れすぎる(競艇でも観測される現象)を fade している
のが Phase A の本質。これは:
- 構造的(オッズ生成の人気バイアス)= 持続性高い
- 一方で、AI 予想が市場参加者になれば 1 番人気の overround は薄まり edge 消失
  (Gemini R5 が指摘した「市場効率化のジレンマ」と整合)

Phase A 停止基準の `live_EV < close_EV 逆転` は、まさにこの overround が
薄まったときに発火する設計になっていて、辻褄が合っている。

## 成果物

- `Opinion/ml_logic_audit/claude_audit_extra.py` — 8 項目の独立検証スクリプト
- `Opinion/ml_logic_audit/claude_audit_results.md` — 検証生データ
- 本ファイル — 評価メモ
