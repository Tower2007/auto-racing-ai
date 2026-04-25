# セッション引継ぎ(自宅PC → ノートPC)

作成: 2026-04-26 / 元セッション: Claude Code on 自宅PC

別 PC で続きの作業をするための引継ぎメモ。新しい Claude セッションは
**最初にこのファイルと [docs/url-structure.md](docs/url-structure.md) を読むこと。**

## ノート PC で最初にやること

```bash
cd <Auto_racing_AI repo path on this PC>
git pull origin main
gh auth status   # Tower2007 でログインされているか確認
```

OneDrive 同期だけに頼らず `git pull` で最新化すること(.git の OneDrive 同期は不確実)。

## これまでに完了したこと(2026-04-26)

1. ✓ HANDOFF_TO_AUTORACE.md を読んで方針確認
2. ✓ GitHub repo 作成: https://github.com/Tower2007/auto-racing-ai (Private)
3. ✓ autorace.jp URL/API 構造調査 → JSON API 直接取得が可能と判明
4. ✓ 主要 API エンドポイントを実コールで検証
5. ✓ 過去データ取得範囲確認(2006-10 以降 OK、5 年バックフィル現実的)
6. ✓ ローカル scaffold (.gitignore / README / .env.example / docs/url-structure.md)
7. ✓ Initial commit + push (commit `328de0c`)

## 確定した方針(ユーザー決定済 2026-04-26)

| 項目 | 決定 |
|---|---|
| DB | **Supabase** (NOT SQLite。INACTIVE な `Tower2007's Project` を削除して枠を空ける方針) |
| バックフィル範囲 | **直近 5 年**(2021-04 〜 現在) |
| GitHub repo | `Tower2007/auto-racing-ai` (Private、作成済) |
| Supabase 作成タイミング | schema 確定後(現時点では未作成) |
| 出力言語 | 日本語 |

## 重要な発見(HANDOFF_TO_AUTORACE.md の修正点)

**HANDOFF_TO_AUTORACE.md は boat-racing-ai 文脈で書かれており、いくつか実態と異なる:**

1. **場コード**: doc は `01=川口...05=山陽` だが、実 API は `2=kawaguchi, 3=isesaki, 4=hamamatsu, 5=iizuka, 6=sanyou`(`1=funabashi` は閉場済でデータ無し)
2. **データ取得手段**: doc は「HTML スクレイピング前提」だが、実態は **Laravel JSON API** で全部取れる(HTML パース不要)
3. **レース距離**: doc の「500m / 600m」は誤り。API は `3100` 等の総走行距離を返す

→ 詳細は [docs/url-structure.md](docs/url-structure.md) 参照。

## 次にやるべき作業(ユーザーが選択する候補)

私が提案した順序: **(b) → (d) → (c) → (a)**

- **(a)** Supabase: ユーザーが手動で INACTIVE プロジェクトをダッシュボード削除 → Claude が `auto-racing-ai` プロジェクトを `ap-northeast-1` で作成
- **(b)** `client.py` 雛形(JSON API ラッパ + CSRF 自動取得 + retry/backoff)
- **(c)** `schema.sql` 設計(API レスポンスから列を起こす)
- **(d)** 1 race-day スモークテスト(API 再現性確認、データ揺れ把握)

理由: client + スモークで実データを見てから schema を起こすと机上の空論にならない。Supabase は schema 固まってから(数分で作れる)。

**ユーザーの最終判断はまだ未受領** — ノート PC で続きを始める時にユーザーが (a)〜(d) のどれかを指定する想定。

## 即実装に必要な情報サマリー

### CSRF 認証フロー(POST API すべてに必要)

```python
# Pseudocode
import requests
s = requests.Session()
r = s.get("https://autorace.jp/race_info/Live/kawaguchi",
          headers={"User-Agent": "Mozilla/5.0"})
# Extract <meta name="csrf-token" content="...">
import re
token = re.search(r'csrf-token" content="([^"]+)"', r.text).group(1)
# XSRF-TOKEN cookie auto-stored in s.cookies

# Then any POST:
s.headers.update({
    "X-CSRF-TOKEN": token,
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
})
res = s.post("https://autorace.jp/race_info/Program",
             data={"placeCode": 5, "raceDate": "2026-04-24", "raceNo": 11})
```

### 主要 POST エンドポイント

すべて `data={"placeCode": int, "raceDate": "YYYY-MM-DD", "raceNo": int}` で叩く(Refund のみ raceNo 不要):

- `/race_info/Program` — 出走表(playerList[8] + 90/180日成績)
- `/race_info/Odds` — オッズ7券種 + AI予想
- `/race_info/RaceResult` — 結果 + 周回ランク変動 + 払戻
- `/race_info/RaceRefund` — 1日分払戻まとめ(raceNo 無視)
- `/race_info/Player` — 開催の出場選手リスト

### 認証不要 GET

- `/race_info/XML/Hold/Today` — 当日開催中の全場(daily_ingest 起点に最適)

### 過去データ範囲

2006-10-15 〜 現在(実測確認済)

## メモリ(別 PC で復元したい場合)

別 PC の `.claude` は同期されない可能性が高い。新セッションで以下を改めて
記憶させたい場合は明示的に伝えること(または無視して docs/ を読めば代替可):

- このプロジェクトでは Supabase を使う(SQLite ではない、HANDOFF doc と異なる)
- バックフィル範囲は直近 5 年
- autorace.jp は JSON API 直叩きで HTML スクレイピング不要
- 場コードは 2-6 (船橋=1 除く)、HANDOFF doc の 01-05 は誤り

## 現状の git 状態

- ブランチ: `main`
- 最新コミット: `328de0c` Initial scaffold: handoff doc, URL structure findings, baseline files
- 未コミットの変更: なし(このファイルを足してコミット予定)
- リモート: `origin` = https://github.com/Tower2007/auto-racing-ai.git

## ノート PC 側で `git pull` 後の確認チェックリスト

```
1. ファイルが揃っているか:
   README.md / HANDOFF_TO_AUTORACE.md / docs/url-structure.md / SESSION_HANDOFF.md
   .gitignore / .env.example
2. このファイル(SESSION_HANDOFF.md)を読む
3. docs/url-structure.md を読む
4. ユーザーに「(a)〜(d) のどれから進めるか」を確認
5. 選ばれたタスクを着手
```
