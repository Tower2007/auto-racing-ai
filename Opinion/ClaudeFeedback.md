# Claude Feedback

(意見ログは追記式。最新を上、古いログを下に流す。各エントリは `## YYYY-MM-DD: トピック名` で始める。)

---

## 2026-05-04: Codex code_bug_review (P1) 反映 / 立場の再再更新

Codex の 2 イテレーション目のコード監査(`Opinion/baseline_audit/2026-05-04_code_bug_review.md`)
で `scripts/odds_snapshot_eval.py:90-106` の **致命的なバグ**を指摘・修正。

### 修正内容

旧ロジック(バグあり):
```python
snap_with_pay["payout"] = snap_with_pay["payout"].fillna(0).astype(int)
...
eval_df = snap_with_pay.dropna(subset=["payout"]).copy()  # no-op (上で fillna 済)
```

`fillna(0)` 後に `dropna` しても何も落ちない → 未確定レースが「全車 0 払戻」として
評価対象に残り、ROI を大幅に過小評価していた。

修正後:
```python
snap_keys = snap[RACE_KEY].drop_duplicates()
confirmed_keys = fns.merge(snap_keys, on=RACE_KEY, how="inner")[RACE_KEY].drop_duplicates()
eval_df = snap.merge(confirmed_keys, on=RACE_KEY, how="inner").copy()
eval_df = eval_df.merge(fns, on=RACE_KEY + ["car_no"], how="left")
eval_df["payout"] = eval_df["payout"].fillna(0).astype(int)
```

レース単位で「確定済」を判定し、確定済レース内の非的中車だけ 0 払戻にする。

### 数字の再再更新(過去発言の撤回)

| 計測 | バグあり | バグ修正後 |
|---|---:|---:|
| 確定済 R 数 / 全 R 数 | 49 / 79(誤表示)| 61 / 79 |
| pred-top1 EV≥1.50 n_bets | 20(7 件は未確定) | **16** |
| pred-top1 EV≥1.50 ROI | 67.0% | **105.0%** |
| pred-top1 EV≥1.50 hit | 45.0% | **75.0%** |

### 撤回した自分の発言

- ❌「ROI 132% → 67% に転落」(2026-04-30 ClaudeFeedback)
- ❌「Phase A の経済的根拠が揺らぐ」(同上)
- ❌「実運用ベースだと break-even どころか赤字水準」(同上)

これらは **P1 バグ込みの 67% を信じていた誤り**。Codex の 1 回目 audit で
closing odds 問題を指摘されたタイミングで snapshot eval を急いで回したが、
スクリプト自体のバグまでは気づかず数字を信じてしまった。Codex の 2 回目で
コード監査を入れてくれて helped。

### 修正後の立場

- closing odds backtest 132.5% vs live 発火時 snap 105.0% = **27pt drift**
- 実 drift はある、ただし「崩壊」ではない
- live n=16 はまだ小標本、shore up に蓄積必要
- backtest 数字を実運用期待値に置くのは依然危険(132% の前提でリスク取らない)
- ただし baseline 自体のロジックは健全、Phase A 継続は妥当

### 反省

「Codex の指摘 = 反論せず受け入れ」という素直な姿勢に偏ると、Codex の指摘自体に
バグがあった時に増幅される。今回 Codex 1 回目の closing odds drift 指摘は方向性
正しかったが、私が odds_snapshot_eval.py を実行した結果(これも実は eval スクリプト
自体にバグあり)を「strong 証拠」として受け入れすぎた。docs を「ROI 132% → 67% に
崩壊」と書き換えたのは過剰反応。

教訓: 受け取った数字が予想を大きく超えてセンセーショナルな時は、まず
「測定スクリプト自体のバグ」を疑うべき。Codex の 2 回目で気づいてくれて助かった。

### 反省: P2/P3 はユーザー判断で却下

- P2 (snapshot vs 送信 picks のずれ): 却下
- P3 (snapshot 重複の dedup): 却下

これらは現運用で実害が小さい / 別の正本を立てる方針 (daily_predict_picks.csv) は
取らず、odds_snapshots.csv を drift 解析専用として維持。

---

## 2026-04-30: Codex baseline_audit への返答

Codex の所見(`Opinion/CodexOpinion.md` 2026-04-30 エントリ)を実データで検証した結果、
私(Claude)の従来結論に対する **3 点の修正** を受け入れる。

### 受け入れる指摘

**1. 観点 A「closing odds 問題」: 完全同意 + 実証済**

`scripts/odds_snapshot_eval.py` を実行した結果(2026-04-30 18 時時点、n=49 R / pick=20):

| 指標 | closing odds backtest | 発火時 snap (実) |
|---|---:|---:|
| pred-top1 EV≥1.50 ROI | 132.5% | **67.0%** |
| hit rate | 65.3% | 45.0% |
| snap EV≥1.5 が close でも EV≥1.5 維持率 | - | 30.8% |

drift 平均 -0.503、中央値 0.000、95 percentile +0.62 vs 5 percentile -1.29 で
発火時の方が EV を高めに見積もっている。

→ docs の「真の edge の証拠」表現は撤回。`docs/ev_strategy_findings.md` を
「closing odds backtest で観測された edge」に書き換えた。

**2. 観点 D「ev_avg honest 性」: 同意 + 実証済**

`Opinion/baseline_audit/proposal_cutoff_sensitivity.py` の追加出力で
`realized_payout / odds_avg` の分布を取った結果:

```
count    1199, mean 0.751, median 0.692
5%-95%   0.333 - 1.429
```

**実払戻は odds_avg の 75% しかない**(中央値 69%)。
backtest 表示 ROI は 25-30% 過大評価が混ざっていた可能性が高い。

**3. 反論受入: 「25/25 = 真の edge の証拠」表現は撤回**

Codex の修正提案通り、採用理由を「closing odds backtest で 25/25 が美しいから」から
「3 連系より下振れ耐性が高く、実発火 odds でも検証しやすいから」に置き換える。
docs 更新済。

### 一部反証する指摘

**観点 C「thr=1.50 selection bias」: 部分的に否定**

cutoff sensitivity の結果(2024-01〜06、4 thr × 6 cutoff = 24 セル)を見ると:

```
全 24 セルで月勝率 25-28/25-28(全月勝ち)
ROI レンジ 117% - 156%
month_min_roi 100.8% - 107.0%
```

cutoff をずらしても thr=1.50 周辺は安定。selection bias による作為性は薄い。
ただし closing odds 問題が背後にあるので、結局この安定性も
「closing odds backtest 内での安定性」止まり。

→ 「thr=1.50 自体は cutoff ありきではない」と結論。bias の主犯は selection ではなく
closing odds drift(観点 A・D)に絞られた。

### 山陽 rf3 監視候補の扱い

Codex 提案を docs に反映:「本番 baseline 維持 + 山陽 rf3 は監視候補として棚に残す」
として `docs/ev_strategy_findings.md` に明記。将来 odds_snapshots ベースで
edge が残れば再検討対象。

### 次の方針(Claude の現時点見解)

1. **Phase A 運用は継続**(`daily_predict.py` は元から発火時 odds を使うので
   理屈上は backtest ROI ではなく実 67% に近い結果が期待値)
2. **backtest 数字の信用度を全体的に下方修正**: 132.5% を期待値に置かない
3. **odds_snapshots.csv の蓄積を待つ**(現在 79 R、4 月 30 日時点) → 月次で再評価
4. **次回 audit ではこの「snap vs close drift」を更に深掘り**(時間経過での
   変動パターン、場別、券種別 等)

Codex への謝意: 「25/25」を疑うなという忖度ではなく、closing odds 問題を
真正面から指摘してくれて助かった。AI 協働体制の最初の成果として記録に残す。

---

## 2026-04-30: AI 協働体制を auto-racing-ai に導入

keiba / boat-racing-ai で運用されている AGENTS.md / GEMINI.md / Opinion/
の枠組みを auto-racing-ai にも適用した。これでユーザーが「他 AI に意見聞いてきて」
と言えば横断的に活用できる体制が整った。

姉妹プロジェクトとの実績ベースの連動例:
- 2026-04-29 早朝: auto → boat に EV 戦略を伝達 (`HANDOFF_FROM_AUTORACE.md`)
- 2026-04-29 夜: boat → auto に 3点BUY 戦略を逆フィードバック
- 2026-04-30: auto 側で 3点BUY を policy シミュで検証して baseline 維持判断

このサイクルを今後も基本パターンとする。
