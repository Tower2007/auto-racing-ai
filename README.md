# auto-racing-ai

オートレース(autorace.jp)のデータ蓄積・分析・ML 検証アプリ。

姉妹プロジェクト [boat-racing-ai](https://github.com/Tower2007/boat-racing-ai) の知見を引き継いで構築。

## 前提

- **賭け運用は非推奨**(オートレース控除率 ~30%、boat-racing-ai で市場越え不可と立証済)
- 主目的: データ蓄積・予想表示・ML 検証(娯楽用途)
- ユーザー: 個人 / Windows 11 / Python 3.13

## ステータス

調査フェーズ完了(2026-04-26)。

- ✓ GitHub repo 作成
- ✓ autorace.jp の URL / API 構造調査 → [docs/url-structure.md](docs/url-structure.md)
- ☐ Supabase プロジェクト作成
- ☐ schema.sql 設計
- ☐ client.py / parser.py 実装
- ☐ 1 race-day テスト投入
- ☐ 直近 5 年 backfill
- ☐ ML パイプライン

## 重要な発見

1. **autorace.jp は JSON API で完結** — HTML スクレイピングは不要
2. 場コードは `2-6` (船橋=1 は閉場済)、HANDOFF doc の `01-05` は誤り
3. 過去データは 2006-10 まで取得可能(直近 5 年は確実に取得可)

## 関連ドキュメント

- [HANDOFF_TO_AUTORACE.md](HANDOFF_TO_AUTORACE.md) — boat-racing-ai からの引継ぎ資料
- [docs/url-structure.md](docs/url-structure.md) — autorace.jp API 仕様
