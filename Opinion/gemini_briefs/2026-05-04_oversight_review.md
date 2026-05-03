# Gemini 依頼書: AI 協働サイクル R1-R3 のメタレビューと Phase A 大局観

依頼日: 2026-05-04
依頼者: ユーザー → Gemini(via Claude が起草)
出力先: `Opinion/GeminiOpinion.md` に追記

---

## なぜ Gemini を呼ぶか

GEMINI.md にある通り「決めるために呼ぶのではなく、決める前に大きな穴がないか
見るために呼ぶ」位置づけ。今回呼ぶのは Codex audit と Claude の対応が
**3 ラウンド回した結果に第三者の視点で穴を探してほしい**から。

Codex の領分(細かい数値検証・コード監査・代替案立案)はすでに完了済。
Gemini の独自視点として期待するのは:

1. **AI 協働プロセス自体のメタレビュー**: R1-R3 のサイクルは健全だったか
2. **Phase A 運用方針の大局観**: 続けるべきか、止める判断軸は何か
3. **第三者の穴探し**: Claude/Codex 二者ともに見落としている観点

---

## 背景(必要最小限)

詳細は以下 3 ファイルを順に読めば文脈が完結する:

1. `docs/ev_strategy_findings.md`(EV 戦略の全記録、2026-04-30 + 05-04 修正済)
2. `Opinion/CodexOpinion.md`(Codex の意見ログ、冒頭に 2026-05-04 P1 バグ撤回 note あり)
3. `Opinion/ClaudeFeedback.md`(Claude の返答ログ、最新は 2026-05-04 P1 バグ修正反省)

3 行に圧縮:

- auto-racing-ai は複勝 top-1 + EV ≥ 1.50 で Phase A 推奨提示型を運用中
- closing odds backtest 132.5% / live 発火時 snap 105.0% (n=16 と小標本) で drift あり
- 3 ラウンドの AI 協働サイクル(Codex 指摘 → Claude 反映 → Codex のコード監査でバグ発覚)を経て立場を 2 度更新した

---

## 依頼観点 1: AI 協働サイクル R1-R3 のメタレビュー

### 経緯のサマリ

- R1(2026-04-30): Codex が closing odds drift を懸念。Claude は odds_snapshot_eval を急いで実行 → 「ROI 132% → 67% に転落」と過剰反応 → docs を「崩壊」narrative で書き換え
- R2(2026-05-04): Codex が `.venv` で実データ検証 → cutoff 感度は invariant、edge は robust と所見更新
- R3(2026-05-04): Codex が **コード監査** で `odds_snapshot_eval.py` の P1 バグ発見 → ROI 67% は未確定レース混入による過小評価、正しくは 105.0% と判明
- 後処理: Claude が「Codex の指摘 → 急いで反映」のミスを認め、CodexOpinion.md 冒頭に撤回 note 追加

### Gemini に問いたいこと

- **R1 の Claude 反応(急いで反映、narrative 大転換)は妥当だったか?**
  測定スクリプトのバグから疑うべきだったが Claude は飛ばした。
  これは個別ミスか、それとも AI 協働の構造的問題か。
- **Codex の R3 コード監査が来なければ、誤った narrative がしばらく残った可能性。**
  もっと早くバグに気づける協働パターンはあるか?(例: 「数字が予想を大きく超えて
  動く時は、まず測定スクリプトのバグを疑うチェックポイント」を AGENTS.md /
  CLAUDE.md に書くべきか)
- **R1 → R2 → R3 という 3 ラウンドの収束は「健全な往復」か「冗長」か?**
  二者だけでこのサイクルを回すと、両者が同じ盲点にハマるリスクは無いか
- **AI 協働サイクル全体として、何を改善すれば「2 ラウンド以内で収束」できるか**

姉妹プロジェクト keiba / boat-racing-ai でも類似サイクルが起きているので、
そちらと比較した独自視点があれば歓迎(ただし Gemini が boat / keiba の
docs を読むかは Gemini 判断、必須ではない)。

---

## 依頼観点 2: Phase A 運用方針の大局観

### 現状

- backtest 132.5% / live snap 105.0%(n=16, 小標本)
- baseline_fns_only + thr=1.50 で Phase A 継続中
- 自動運用 5 タスク(daily_ingest / morning predict / noon predict / weekly retrain / weekly status)が稼働
- 期待利益: 年 ¥27K 程度(取りこぼし考慮)
- 投票はユーザー手動、自動投票は ToS グレー回避で実装せず

### Gemini に問いたいこと

- **「live n が増えるまで継続」の方針は妥当か?**
  逆に、「live n が増えても結論が出ないリスク」(常に小標本のまま、季節性の
  影響、対象場の開催スケジュールの偏り等)はないか?
- **止める判断軸を今のうちに定義すべきでは?**
  例: 直近 30 picks で ROI < 90% / 月勝率 < 50% / drift > -1.0pt 等の
  停止基準があった方が運用継続判断が機械的にできる
- **年 ¥27K の利益で運用負荷に見合うか、という根本問題**
  Phase A は推奨メールを毎日朝・昼・per-race で受け取り、ユーザーが手で
  vote.autorace.jp に投票するフロー。時間コスト × 年 ¥27K で評価して
  「やる意味」はどこにあるか?(エンタメか、技術検証か、本気で稼ぎたいか)
- **boat-racing-ai は 3点BUY 採用、auto は不採用で方針分岐**
  競技構造の差で説明できるか、それとも検証手法の差か。長期的に維持して
  良いのか、いずれ統一すべきか
- **代替方向性で見落としているものは?**
  例: 完全停止してデータ蓄積のみ続ける、別 target(1着固定 / 着順回帰)、
  別 venue(山陽だけ運用)、別券種、低リソース運用への切替 等

---

## 依頼観点 3: 第三者の穴探し

Claude / Codex 二者だけだとコンテキスト窓・思考特性が偏っている可能性。
Gemini の RLHF を経た独立視点で、以下を中心に「Claude も Codex も
触れていない観点で重要なものはあるか」を見てほしい。

候補例(Gemini が深掘りすべきは Gemini 判断):

- **規約・倫理・運用継続性のリスク**
  vote.autorace.jp の ToS グレーゾーン回避は妥当か。data ingest の負荷
  (毎日 6 時 30 分の API コール)は規約面で問題ないか。
- **市場前提の妥当性**
  「auto は競技人口・売上が小さいから市場効率性が低い」という仮説に
  立脚しているが、その前提自体は正しいか
- **姉妹プロジェクトとの整合性**
  keiba / boat / auto / horse-racing-ai で gmail / scheduler / 監査体制を
  共有している。横展開で生じている隠れた依存・矛盾はないか
- **長期的にこの体制が持続可能か**
  2 PC 運用、Windows Task Scheduler、ローカル CSV、グローバル Python …
  いずれも「家庭運用」の妥協が積もっている。これらが将来詰まる可能性

---

## 期待する成果物

`Opinion/GeminiOpinion.md` に以下構造で追記:

```markdown
## 2026-05-04: AI 協働サイクル R1-R3 + Phase A 大局観レビュー

### サマリ(3-5 行)

(全体所感を最初に)

### 観点 1: AI 協働サイクル R1-R3 のメタレビュー

(R1 反応の妥当性、収束パターン、改善案)

### 観点 2: Phase A 運用方針の大局観

(継続妥当性、止める判断軸、代替方向性)

### 観点 3: 第三者の穴探し

(Claude/Codex が触れていない、しかし重要と思える観点)

### 自分が Claude / Codex なら気付かなかった視点

(差別化された独自視点を意識的に。スポット参加の評価で重視されるポイント)

### Gemini からの提案(あれば)

- (実装提案は Claude へ依頼の形で)
```

---

## 制約と注意

- **編集権限は `Opinion/` 配下のみ**(GEMINI.md ルール厳守)
- **数値検証や代替案立案には深入りしない**(Codex の領分)
- **コード実装・ドキュメント直接編集はしない**(Claude の領分)
- **数値・主張には出典を添える**(ファイル名 / commit hash / docs 章番号)
- **忖度しない**: Claude の判断・Codex の audit 結論で「同意できない」点があれば
  率直に書く。「すべて同意」だけの返答は Gemini を呼んだ意味がない
- **大きな計算は事前承認**: walk-forward 再生成等は不要、既存 docs・data・log で
  読み解けるはず

---

## 依頼書の運用

この依頼書は Claude が起草、ユーザーが Gemini に渡す。Gemini は:

1. `GEMINI.md`(全体運用ルール)を読む
2. 本依頼書(`Opinion/gemini_briefs/2026-05-04_oversight_review.md`)を読む
3. 背景 3 ファイル(docs/ev_strategy_findings.md、Opinion/CodexOpinion.md、
   Opinion/ClaudeFeedback.md)を順に読む
4. `Opinion/GeminiOpinion.md` に意見を追記

順序通りに進めれば文脈ゼロでも作業可能。
