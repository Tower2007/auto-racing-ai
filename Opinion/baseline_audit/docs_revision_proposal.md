# docs 修正案: `docs/ev_strategy_findings.md`

Codex は `docs/` を直接編集しないため、Claude への反映依頼用に修正案を置く。

## 1. TL;DR の表現

現状の「真のエッジ約 +5%」「調整次第で +30%」は、以下のように弱める。

```markdown
walk-forward LightGBM 予測を使って「期待値ベースで複勝を選別すれば ROI > 100% にできるか」を 5 段階で検証した記録。
**結論: closing odds backtest では複勝 top-1 に robust な edge が観測された。**
ただし、実運用では発火時 odds と closing odds の drift があり、live / paper 検証での確認が必要。
```

## 2. Step 5 の honest 表現

現状:

```markdown
- ev_avg(min/max 中央値)で payout 推定を honest 化
```

修正案:

```markdown
- ev_avg(min/max 中央値)で ev_min より過度な保守バイアスを緩和
- ただし hit 時の実払戻 / odds_avg は中央値 0.69、平均 0.75 程度で、ev_avg を実払戻の honest proxy と断定するのは強い
```

## 3. baseline 25/25 の表現

現状:

```markdown
baseline は異常な安定性を持つ ... (複勝 top-1 EV ≥ 1.45 の真の edge が広く薄く存在している証拠)
```

修正案:

```markdown
baseline は closing odds backtest 上では異常な安定性を持つ。cutoff を 2024-01〜2024-06 にずらしても `thr=1.50` 近傍の月次全勝は崩れず、edge は単純な cutoff 偶然だけでは説明しにくい。
一方で、閾値は eval 期間の sweep で選ばれており、実運用では発火時 odds drift があるため、「真の edge の証拠」と断定せず live / paper 検証待ちとする。
```

## 4. live snapshot 初期結果の追記

追加節案:

```markdown
### live snapshot 初期検証 (2026-05-04 Codex audit)

`odds_snapshots.csv` の 2026-04-30〜2026-05-03 分を確認。
fns 確定済 49R / pred-top1 EV>=1.50 は 20 picks と小標本だが、ROI は 67.0%、hit 45.0%。
発火時 EV>=1.50 が closing odds でも EV>=1.50 として残った比率は 4/13 = 30.8%。

これは結論を覆すサンプル数ではないが、closing odds backtest と live 発火時 odds の乖離は最重要監視項目。
今後は `odds_snapshots.csv` ベースで週次に paper ROI / EV drift を確認する。
```

## 5. 3点BUY 不採用理由の修正

現状の方針は維持。ただし理由の主語を変える。

```markdown
3点BUY 不採用は維持。
理由は「baseline の 25/25 が真の edge の証拠だから」ではなく、
「baseline は stakes が単純で下振れ耐性が高く、live/paper 検証で崩れた時に原因分解しやすいから」。
3連系は過去 backtest の利益額こそ大きいが、外れ値依存・月次赤字・券種別 drift の検証負荷が大きい。
```

