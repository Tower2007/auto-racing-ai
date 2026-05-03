# TODO: 「複勝 1 番人気の過小評価」仮説検証

提案者: Gemini(`Opinion/GeminiOpinion.md` 2026-05-04 観点 3)
ステータス: 保留(将来検証 TODO、実装はしない)

---

## 仮説

cutoff invariant な月次 25/25 が示す edge は、autorace 市場全体に存在する
**「複勝 1 番人気の過小評価」** という構造的欠陥を捉えている可能性がある。
Gemini の表現: "オートレース市場全体に存在する『複勝 1 番人気の過小評価』
という構造的欠陥を捉えている可能性が高い"。

## 何故これを検証する価値があるか

- 構造仮説が正しい → edge は overfit ではなく持続性が見込める
- 構造仮説が間違っている → edge は eval 期間の特殊性 / overfit の疑いに戻る
- どちらでも、Phase A 運用判断の根拠が強化される

## 検証方法(将来 Codex への実装依頼想定)

1. 全レースを `pred_calib` ランクでグルーピング
2. 各グループで「実際の top3 入り率」と「市場 implied 確率(複勝オッズ逆数等)」
   を比較
3. 系統的に **モデル予測 > 市場 implied** が見られるか確認
4. もし見られるなら、それは「市場が複勝 1 番人気を過小評価している = edge」を
   構造として確認したことになる
5. 場別 / 開催時間帯別(daytime / nighter / midnight)での差分も見る

## 着手条件

- live n が 50 picks 以上に達したら(現在 16)
- または baseline_fns_only の停止条件(`docs/ev_strategy_findings.md` 参照)に
  該当しそうな兆候が出たら

## 期待アウトプット

- `Opinion/baseline_audit/underprice_hypothesis_results.md` に検証レポート
- 構造仮説の支持 / 反証の判定
- もし支持されれば docs/ev_strategy_findings.md の仮説セクションに追加
- もし反証されれば baseline 運用の継続根拠を再考

---

メモ: この TODO は実装ではなく **保持** が目的。Gemini 提案を docs に直接書かず、
Opinion/ 配下に隔離することで、仮説段階のものが docs を汚染しないようにする。
