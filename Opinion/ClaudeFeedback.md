# Claude Feedback

(意見ログは追記式。最新を上、古いログを下に流す。各エントリは `## YYYY-MM-DD: トピック名` で始める。)

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
