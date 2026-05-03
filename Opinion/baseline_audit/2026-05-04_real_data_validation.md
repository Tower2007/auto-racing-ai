# baseline_fns_only 実データ検証メモ (2026-05-04)

## 要約

Codex audit 後に `.venv` を作成し、`requirements.txt` の分析依存を導入して実データを読んだ。結論は少し更新する。

- closing odds backtest 上の `baseline_fns_only` は、cutoff を 2024-01〜2024-06 にずらしても月次全勝が崩れず、edge は当初疑ったより頑丈。
- ただし、`ev_avg` は hit 時の実払戻に対して楽観寄りの可能性がある。`realized payout / odds_avg` の中央値は 0.692、平均 0.751。
- 直近 live snapshot はまだ小標本だが、発火時 EV>=1.50 の成績は ROI 67.0%。closing odds backtest の ROI 132.5% とは大きく乖離している。
- したがって、docs は「真の edge」と断定せず、「closing odds backtest では robust。ただし発火時 odds drift の live 検証待ち」に修正するのがよい。

## 実行環境

- `.venv` を作成。
- `python -m venv .venv` は `ensurepip` が Temp 権限で失敗したため、workspace 内 Temp に向けて再実行。
- `pip install -r requirements.txt` は sandbox のネットワーク制限で失敗後、承認付きで成功。
- `uv` は PATH 上になかった。

## 検証 1: cutoff 感度

実行:

```powershell
.\.venv\Scripts\python.exe Opinion\baseline_audit\proposal_cutoff_sensitivity.py
```

固定閾値の主な結果:

| cutoff | thr | eval_months | n_bets | ROI | profit | month_ge_1 | min monthly ROI |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2024-01 | 1.30 | 28 | 3,385 | 118.4% | +62,310 | 28/28 | 102.0% |
| 2024-01 | 1.45 | 28 | 2,341 | 128.8% | +67,510 | 28/28 | 105.1% |
| 2024-01 | 1.50 | 28 | 2,050 | 133.8% | +69,360 | 28/28 | 101.7% |
| 2024-04 | 1.30 | 25 | 3,040 | 116.9% | +51,390 | 25/25 | 101.8% |
| 2024-04 | 1.45 | 25 | 2,107 | 126.9% | +56,680 | 25/25 | 105.5% |
| 2024-04 | 1.50 | 25 | 1,836 | 132.5% | +59,690 | 25/25 | 101.0% |
| 2024-06 | 1.30 | 23 | 2,808 | 116.9% | +47,490 | 23/23 | 101.7% |
| 2024-06 | 1.45 | 23 | 1,910 | 127.3% | +52,180 | 23/23 | 105.5% |
| 2024-06 | 1.50 | 23 | 1,700 | 131.9% | +54,220 | 23/23 | 100.8% |

追加 sweep では、2024-01 / 2024-04 / 2024-06 のいずれも profit 最大は `thr=1.50` 付近だった。

| cutoff | best thr | n | ROI | profit | month_ge_1 |
|---|---:|---:|---:|---:|---:|
| 2024-01 | 1.50 | 2,048 | 133.8% | +69,240 | 28/28 |
| 2024-04 | 1.50 | 1,832 | 132.6% | +59,650 | 25/25 |
| 2024-06 | 1.50 | 1,699 | 131.9% | +54,220 | 23/23 |

所見:

- cutoff を 1〜3 ヶ月ずらす程度では崩れない。
- `thr=1.50` は「評価期間だけで偶然選ばれた一点」というより、近傍でも強い。ただし独立未来 test ではないため、運用仮説扱いは維持。

## 検証 2: ev_avg と実払戻の比率

`cutoff=2024-04`, `thr=1.50`, top1 hit 限定で、`realized_odds = payout / 100` とし、`realized_odds / ((place_odds_min + place_odds_max) / 2)` を確認。

| 指標 | 値 |
|---|---:|
| count | 1,199 |
| mean | 0.751 |
| median | 0.692 |
| p25 | 0.554 |
| p75 | 0.866 |
| p95 | 1.429 |
| max | 1.943 |

所見:

- `ev_avg` は `ev_min` よりはましだが、実払戻の期待値としては「honest」と言い切れない。
- 複勝の実払戻は odds range の中央値より下に寄る傾向が強い。
- それでも backtest ROI が 132% 出ているなら、モデル選別 edge がかなり強い可能性はあるが、「ev_avg が保守的だから安全」という表現は避けるべき。

## 検証 3: live snapshot

実行:

```powershell
.\.venv\Scripts\python.exe scripts\odds_snapshot_eval.py --thr 1.50
```

対象:

- `odds_snapshots.csv`: 2026-04-30〜2026-05-03
- 発火 R 数: 79
- fns 確定済 R 数: 49

結果:

| 対象 | n | hit | payout | profit | ROI |
|---|---:|---:|---:|---:|---:|
| pred-top1 EV>=1.50 | 20 | 45.0% | 1,340 | -660 | 67.0% |
| pred-top1 全件 | 80 | 41.2% | 4,410 | -3,590 | 55.1% |
| EV>=1.50 全車 | 217 | 16.6% | 15,580 | -6,120 | 71.8% |

EV drift:

| 指標 | 値 |
|---|---:|
| snap EV 平均 | 1.602 |
| close EV 平均 | 1.188 |
| drift 平均 | -0.503 |
| drift 中央値 | 0.000 |
| q05 | -1.29 |
| q95 | +0.62 |
| snap EV>=1.50 が close でも残存 | 4/13 = 30.8% |

所見:

- まだ n=20 picks なので結論は出せない。
- ただし、発火時 odds は closing odds backtest とかなり違う可能性が高い。
- docs には「実運用の検証単位は `odds_snapshots.csv` ベース」と明記すべき。

## docs 修正方針

`docs/ev_strategy_findings.md` は以下のニュアンスに修正するのがよい。

1. 「真の edge」と断定している箇所を「closing odds backtest 上の edge」に変更。
2. `ev_avg` を「honest」と呼ぶ箇所に、実払戻中央値は odds_avg の約 0.69 倍だったという caveat を追加。
3. 25/25 は cutoff 感度では robust だが、閾値選定後の評価であるため future validation 待ちと書く。
4. live snapshot 初期値として `2026-04-30〜2026-05-03, n=20, ROI 67.0%` を追記し、小標本だが odds timing mismatch の監視対象とする。
5. 3点BUY 不採用は維持。理由は「baseline が美しい」ではなく「下振れ耐性が強く、live 検証しやすい」に寄せる。

