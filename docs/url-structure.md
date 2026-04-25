# autorace.jp URL / API 構造調査結果

調査日: 2026-04-26

## 重要な結論: HTML スクレイピング不要

**HANDOFF_TO_AUTORACE.md には「HTML スクレイピング前提」と書かれていたが、
実態は JSON API がそのまま使える。** ページ HTML はテンプレートにすぎず、
すべてのデータは Laravel ベースの内部 API から axios POST で取得されている。

→ `parser.py` で HTML を BeautifulSoup でパースする必要はなく、JSON を扱うだけで良い。
boat-racing-ai より工数大とは限らない(むしろ楽な可能性が高い)。

## 場コード(HANDOFF doc は誤り、要修正)

| placeCode | placeKey   | placeName | 状態 |
|-----------|------------|-----------|------|
| 1         | funabashi  | 船橋      | 2016年閉場、API ではデータ取得不可 |
| 2         | kawaguchi  | 川口      | 稼働 |
| 3         | isesaki    | 伊勢崎    | 稼働 |
| 4         | hamamatsu  | 浜松      | 稼働 |
| 5         | iizuka     | 飯塚      | 稼働 |
| 6         | sanyou     | 山陽      | 稼働 |

HANDOFF doc の `01=川口, 02=伊勢崎, 03=浜松, 04=飯塚, 05=山陽` は**誤り**。
実コードはオフセット +1 で `2-6` (船橋を `1` として含む全6コード)。

## レース距離

HANDOFF doc に「500m / 600m」とあったが、API レスポンスは `distance: 3100` 等の値で、
これは「総走行距離」(レース全体の距離)。1周あたりの距離(コース全長)は別。
→ スキーマでは `total_distance_m` として保存推奨。

## 認証 (CSRF)

すべての POST API は Laravel CSRF トークンが必須。

**手順:**
1. 任意の HTML ページ(例: `/race_info/Live/kawaguchi`)を GET
2. レスポンスから `XSRF-TOKEN` Cookie を保存
3. HTML 内 `<meta name="csrf-token" content="...">` の値を抽出
4. POST 時に以下のヘッダを付与:
   - `X-CSRF-TOKEN: {meta値}`
   - `X-Requested-With: XMLHttpRequest`
   - `Content-Type: application/x-www-form-urlencoded`
   - `Cookie: {Cookie jar}` (XSRF-TOKEN が乗る)

トークンは長時間有効。session 1 個あたり 1 トークンで全 API を叩ける。

## 過去データ取得可能範囲

実測:
- `2006-10-15` Iizuka → 14KB データ取得成功 ✓
- `2006-09-30` 以前 → 不明(2006-11-15 は空 = 開催無しの日と推定)
- 2007-04-01 / 2010-04-01 / 2026-04-25 はすべて取得可

→ HANDOFF doc の「2006年10月以降」記述は正しい。
**直近5年の方針(2021-04-01〜現在)は API で完全カバー可能。**

## 主要 API エンドポイント

### 1. `/race_info/XML/Hold/Today` (GET) — 当日開催情報

認証不要。当日開催中の全場の概要(発走時刻、現在レース、グレード、距離等)。

```bash
curl https://autorace.jp/race_info/XML/Hold/Today
```

レスポンス: `body[]` 各場ごとに placeCode/placeKey/title/grade/distance/raceStartTime/nowRaceNo/oddsRaceNo/resultRaceNo/temp/humid/weather 等。

→ daily_ingest の起点に最適(今日どの場で開催中か即取得可)。

### 2. `/race_info/XML/Hold/Recent` (POST) — 直近開催情報

```
params: { placeCode: 5 }
```

その場の直近開催(複数開催分)を返す。各開催に `periodStartDate, periodEndDate, raceList: [{raceDate, finalRaceNo}]`。

→ backfill で各場の開催日リストを取得するのに使える。

### 3. `/race_info/XML/Calendar` (GET) — カレンダー

```
?yearMonth=YYYYMM (調査要、実際の正解パラメータは未確定)
```

※ `?date=YYYY-MM-DD`、`?yearMonth=202604` で試したが両方 4001 エラー。要追加調査。

### 4. `/race_info/Player` (POST) — 開催の出場選手リスト

```
params: { placeCode: 5, raceDate: "2026-04-24" }
```

レスポンス body[0]:
- placeCode, placeKey, placeName
- gradeCode, gradeName, periodStartDate, periodEndDate, title
- `playerRank[]`: ランク別 (S/A/B), 各 `playerLg[]` (ロッカーグループ), 各 `playerList[]` で
  `playerCode, playerName, graduationCode, recommendCode, recommendName`

### 5. `/race_info/Program` (POST) — 出走表(per レース)

```
params: { placeCode, raceDate, raceNo }
```

レスポンス body:
- `twicePlayerExist`: 1日2回出走選手フラグ
- `playerList[]` (8車):
  - `carNo` (1-8 枠番)
  - `playerCode, playerName, graduationCode, age, retireFlag`
  - `placeCode/placeKey/placeName` (選手所属場)
  - `bikeClass, bikeName` (競走車クラス/名)
  - `rank` (例: "S-12", "A-116")
  - `handicap` (m): スタート位置からの距離(0/10/20/30...)
  - `anotherRaceNo, anotherRaceNo2`: 同日2回目出走時の前/次レース番号
  - `trialRunTime` ("3.42"), `trialRetryCode`: 試走タイム + 再試走コード
  - `absent`: 欠車コード
  - `sunnyExpectCode, rainExpectCode`: AI予想印(晴/雨)
  - `raceDev` ("062"): 偏差(競走T - 試走T の良走路平均)
  - `rate2, rate3` ("50.0"/"60.0"): 2連対率/3連対率
- `latest3List, latest10List, latest90List, latest180List`: 過去成績(3走/10走/90日/180日)
- `latest5GoodTrackList, latest5WetTrackList, latest5SpotsTrackList`: 良走路/雨走路/特定場別の直近5走
- `winList`: 優勝戦成績

### 6. `/race_info/Odds` (POST) — オッズ + AI予想

```
params: { placeCode, raceDate, raceNo }
```

レスポンス body:
- `statusCode, twicePlayerExist, aiExpectConfirmFlag`
- `playerList[]` (8車): 出走表サブセット + `stAve, goodTrackTraialAve, goodTrackRaceAve, goodTrackRaceBest, goodTrackRaceBestPlace`
- `rtwOddsList, rfwOddsList, rt3OddsList, rf3OddsList, widOddsList, tnsOddsList, fnsOddsList`:
  券種別オッズ。トップキーは `1`〜`8` (1着車番)、各値は dict (ネスト構造)
- `salesInfo`: { updateDate, oldFlg, rtw, rfw, rt3, rf3, wid, tns, fns } 売上情報
- `popInfo`: rt3/rf3/rtw/rfw の人気順
- `expectInfo`: { sunny, rain, st, ai } AI予想印詳細
- `comment`: 本部コメント等

### 7. `/race_info/RaceResult` (POST) — レース結果

```
params: { placeCode, raceDate, raceNo }
```

レスポンス body:
- `twicePlayerExist`
- `raceResult[]` (8車):
  - `order` (着順 1-8 / 失格は別)
  - `accidentCode, accidentName`: 事故コード(欠車/転倒/失格等)
  - `carNo, playerCode, playerName, playerNameEn`
  - `motorcycleName`: 競走車名
  - `handicap, traialRetryCode, traialTime`: ハンデ・試走
  - `raceTime`: 競走タイム
  - `st`: スタートタイミング
  - `foulCode`: F/L/B/W/A 等のスタート異常
  - `anotherRaceNo, anotherRaceNo2`
  - `retireFlag`
- `warningPlayerList, trialWarningPlayerList`: 戒告対象
- `grandNoteList[]`: 周回ごとのランク変動(`lapNo, rankList`)
  → **競艇には無いユニークな ML 特徴量**(ペース・展開分析に使える)
- `refundInfo`: 払戻(下記 RaceRefund と同じ schema)

### 8. `/race_info/RaceRefund` (POST) — 払戻金(1日分まとめて)

```
params: { placeCode, raceDate }
※ raceNo パラメータは送っても無視される(全レース返る)
```

レスポンス body[]: 各レース(12件)に対して
- `raceNo`
- `refundInfo`:
  - `rtw` (2連単): `[{ "1thCarNo", "2thCarNo", refund, pop, refundVotes }]`
  - `rfw` (2連複): 同上
  - `rt3` (3連単): `[{ "1thCarNo", "2thCarNo", "3thCarNo", refund, pop, refundVotes }]`
  - `rf3` (3連複): 同上
  - `wid` (ワイド): 3点
  - `tns` (単勝): `[{ carNo, refund, pop, refundVotes }]`
  - `fns` (複勝): 3点(各 `order: 1/2/3` 付き)
  - `absent`: 欠車情報

### 9. `/race_info/Player/{playerCode}` (GET, HTML) — 選手プロフィール HTML

選手詳細ページ。生年月日・通算成績などの追加情報があれば。ここは HTML パース必要かも。

### 10. `/race_info/SearchPlayerResult` (POST) — 選手別レース履歴検索

```
params: {
  playerCode, startDate, endDate, page,
  placeCodeList[], gradeList[], handicapList[], raceClassList[],
  bikeClassList[], orderList[], holdTypeList[], openraceList[],
  situationList[], carNoList[]
}
```

詳細フィルタ付き履歴検索。選手単位の deep dive 用。

### 11. その他確認済みエンドポイント

| Path | Method | 用途 |
|------|--------|------|
| `/race_info/OtherRaceInfo` | POST | その他のレース情報(要 placeCode/raceDate/raceNo) |
| `/race_info/FcAiExpect` | POST | FC用AI予想 (要 raceDate, token) |
| `/race_info/SearchMotorcycle` | POST | 競走車検索 |
| `/race_info/Ranking/{Player\|Prize\|Rate\|Stave\|refund}` | GET | 各種ランキング |
| `/race_info/SpecialRace/resultList` | POST | 特別記念レース結果 |
| `/web_odds/timer/api` | POST | オッズタイマー次ページ |
| `/latest10` | POST | 直近10走着順(POST 不可、要追加調査) |

## バックフィル計画(直近5年)

### コール数試算

- 期間: 2021-04-26 〜 2026-04-26 (5年)
- 場数: 5(船橋除く)
- 開催率: 各場 ~250 開催日/年(粗推定)
- 総開催日数: 5 場 × 250 日 × 5 年 ≒ **6,250 race-days**

各 race-day で必要な API コール:
- Player: 1(meeting 単位だが各日に呼んでも OK)
- RaceRefund: 1(その日の全レース払戻)
- Program × 12 race
- Odds × 12 race
- RaceResult × 12 race

→ 1 race-day = **38 コール**
→ 総数 = 6,250 × 38 ≒ **237,500 コール**

### 所要時間

| 1コールあたり間隔 | 総所要時間 |
|------------------|----------|
| 0.3 秒 | 約 20 時間 |
| 0.5 秒 | 約 33 時間 |
| 1.0 秒 | 約 66 時間 |

→ 0.5 秒間隔(マナー良)で 2〜3 日に分割して並列実行が現実的。
→ サーバ負荷が見えれば調整(WinError 10035 対策の retry/backoff は必須)。

### 段階的実行戦略

1. まず 1 場 × 1 開催 (5〜6 race-day) を投入してスキーマ・パイプライン検証
2. 1 場 × 1 ヶ月で動作確認
3. 1 場 × 1 年でデータ品質確認(NULL 率/欠損レース率/エラー率)
4. 全期間 × 5 場で本番 backfill
5. 並行して daily_ingest を走らせ、追加分を毎日取り込み

## オッズ過去保持の懸念

**未確認事項**: Odds API が過去日付に対してオッズを返すか?
払戻金は確実に取れるが、確定前のオッズ(売上情報)は時間制限があるかも。
→ 直近 1 ヶ月以内、1 年以内、3 年以内、5 年以内 で各 1 サンプル取得して比較すべき。

(※ 競艇では確定オッズは何年前でも取れたが、autorace.jp は別仕様)

## 次のアクション

1. ~~GitHub repo 作成~~ (完了)
2. ローカル git + baseline (CLAUDE.md, README, .gitignore, .env.example)
3. Supabase プロジェクト `auto-racing-ai` 作成 (ap-northeast-1, INACTIVE 削除後)
4. `schema.sql` 設計 — 主要テーブル: venues, racers, motorcycles, race_meetings, races, race_entries, race_results, race_lap_grand_notes, payouts
5. `client.py` (autorace.jp の JSON API ラッパ、CSRF 自動取得+セッション管理+retry)
6. `parser.py` (JSON → Python dict 整形、文字コード/null/型変換)
7. 1 race-day のテスト投入 → スキーマ検証
8. backfill 段階実行
9. Walk-forward ML パイプライン(boat-racing-ai 流用)
