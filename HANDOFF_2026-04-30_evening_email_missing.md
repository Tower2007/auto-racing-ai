# 引継ぎ資料 (2026-04-30 夕方): 通知メールが 1 通も来ない件

別 PC (no28a, スケジューラ稼働中) で調査するためのスナップショット。
朝版 `HANDOFF_2026-04-30.md` (np.log1p 系)、昼版 `HANDOFF_2026-04-30_threshold.md`
(閾値検討) は別件。**本資料は新しく発覚した観測性問題**。

## 0. 一言で

2026-04-30 の運用で **通知メールが 1 通も来てない** が、
ホーム PC で 17:18 時点に再計算したところ **EV>=1.50 が 6 レース**該当していた。
これが「設計通りの時刻差」なのか「silent fail / task 不発」のバグなのか
別 PC でログ確認して切り分けてほしい。

## 1. 現象

- 17:18 時点でユーザー報告「今日 1 通もメール来てない」
- ホーム PC のライブ予想アプリで本日全 R を再計算
- → 24 R 中 6 R が **ev_avg_calib >= 1.50** 該当
  - うち 2 R は **EV 2.86 / 2.94** と非常に高い

### 1-1. 再計算結果 (ホーム PC, 17:18 取得オッズ)

| 場 | R | top1 車 | pred_calib | place_min | place_max | EV | 通知該当? |
|---|---:|---:|---:|---:|---:|---:|:---:|
| isesaki | R1 | 1 | 0.754 | 1.4 | 2.9 | **1.62** | ✅ |
| isesaki | R2 | 3 | 0.911 | 1.0 | 1.6 | 1.18 | — |
| isesaki | R3 | 7 | 0.874 | 1.2 | 2.2 | 1.49 | — |
| isesaki | R4 | 7 | 0.917 | 1.0 | 1.8 | 1.28 | — |
| isesaki | R5 | 5 | 0.816 | 1.0 | 1.4 | 0.98 | — |
| isesaki | R6 | 7 | 0.913 | 1.0 | 1.1 | 0.96 | — |
| isesaki | R7-R12 | — | — | nan | nan | nan | (オッズまだ未公開) |
| sanyou | R1 | 4 | 0.950 | 1.0 | 1.1 | 1.00 | — |
| sanyou | R2 | 4 | 0.880 | 1.0 | 1.8 | 1.23 | — |
| sanyou | R3 | 5 | 0.799 | 1.4 | 2.8 | **1.68** | ✅ |
| sanyou | R4 | 6 | 0.754 | 1.1 | 2.2 | 1.24 | — |
| sanyou | R5 | 7 | 0.901 | 1.0 | 1.1 | 0.95 | — |
| sanyou | R6 | 6 | 0.874 | 1.0 | 1.9 | 1.27 | — |
| **sanyou** | **R7** | 7 | 0.880 | 1.0 | 5.5 | **2.86** | **✅** |
| **sanyou** | **R8** | 5 | 0.775 | 1.0 | 6.6 | **2.94** | **✅** |
| sanyou | R9 | 7 | 0.794 | 1.0 | 1.0 | 0.79 | — |
| sanyou | R10 | 5 | 0.788 | 1.7 | 2.4 | **1.62** | ✅ |
| sanyou | R11 | 7 | 0.901 | 1.2 | 2.3 | **1.58** | ✅ |
| sanyou | R12 | 2 | 0.917 | 1.0 | 1.0 | 0.92 | — |

**最高 EV = sanyou R8 で 2.94** / **通知該当 6 R**

### 1-2. 期待される挙動

`AutoraceDyn_{venue}_R{n}` が発走 30 分前に発火し、
`daily_predict.py --venues {pc} --races {n} --suppress-noresult-email` で予測。
EV>=1.50 の場合 → メール送信。

→ 本日 6 通来てるはず → **0 通**

## 2. 仮説と切り分け

### 仮説 A: 発火タイミングのオッズと現在のオッズが違う(設計通り)

- dynamic_scheduler は発走 30 分前にオッズ取得 → そのオッズで予測
- 30 分前 → 発走 → 確定 までにオッズが大きく動いた可能性
- 特に発走直前は前売オッズ → 投票集中で本命オッズが下がる傾向
- 30 分前: EV<1.50 → 17:18 再計算: EV>=1.50

**この場合**: バグじゃなく時刻差。ただし「**閾値判定が時刻に敏感すぎる**」という設計課題が残る。

検証: ログから各 R 発火時の EV を抽出 → 当時 < 1.50 だったか確認。

### 仮説 B: メール送信が silent fail

- SMTP 認証失敗、ネットワーク不安定 等を try/except で握り潰し
- `daily_predict.py:551-555` 付近、`send_email` の例外を `logger.error` で記録のみして継続

```python
try:
    send_email(subject=subject, body=text, html=html)
    logger.info("メール送信完了: %s", subject)
except Exception as e:
    logger.error("メール送信失敗: %s", e)
```

検証: ログで「メール送信失敗」が出てないか / 「メール送信完了」が出てるが届いてないか。

### 仮説 C: dynamic task 自体が動いてない

- schtasks 登録ミス、コマンドライン構成ミス、--races の解釈失敗等で予測処理が起動しない
- ログにそもそも `=== daily_predict start ===` が出ない R がある

検証: ログに各 R の `=== daily_predict start ===` が 24 R 分あるか確認。

## 3. 別 PC で実行してほしい debug コマンド

```cmd
cd C:\Users\no28a\Claude-project\Auto_racing_AI\

:: 1. 当日ログ末尾 500 行
powershell -c "Get-Content data\daily_predict.log -Tail 500" > tail.txt
notepad tail.txt

:: 2. 該当 6 R の発火時ログ (前後 20 行)
powershell -c "Select-String -Path data\daily_predict.log -Pattern 'isesaki_R1\b|sanyou_R3\b|sanyou_R7\b|sanyou_R8\b|sanyou_R10\b|sanyou_R11\b' -Context 0,20"

:: 3. メール送信の成否一覧
powershell -c "Select-String -Path data\daily_predict.log -Pattern 'メール送信|候補数|--suppress|fatal|送信失敗' | Where-Object { $_ -like '*2026-04-30*' }"

:: 4. 各 R の EV 通知判定 (候補数行)
powershell -c "Select-String -Path data\daily_predict.log -Pattern '候補数:|R\d+ 車\d+ pred' | Where-Object { $_ -like '*2026-04-30*' }"

:: 5. dynamic task が登録されてるか
schtasks /query /fo LIST /v /tn "AutoraceDyn_*" 2>&1 | findstr /C:"タスク名" /C:"前回" /C:"次回" /C:"結果"
```

## 4. 切り分け後のアクション

### 仮説 A が真因の場合

- **オッズ取得タイミングが早すぎる** → 発走 15 分前等に変更検討
- **EV 閾値の時刻安定性** を再検証(walk-forward で発走 30 分前 vs 直前のオッズ差を測る)
- 報告書: `reports/threshold_time_sensitivity_YYYY-MM-DD.md`

### 仮説 B が真因の場合

- gmail SMTP 設定確認、認証、daily_predict.log の「送信失敗」を grep
- fatal 通知メールが来てるか確認(daily_predict には fatal 通知ロジックあり)
- 修正: send_email リトライ追加 or fatal メール強化

### 仮説 C が真因の場合

- `dynamic_scheduler.py` の task 登録ロジック確認
- schtasks の登録結果確認(該当 R が AutoraceDyn_{venue}_R{n} で存在するか)
- 修正: `register_one_shot()` のエラーハンドリング強化

## 5. 関連ファイル / 直近コミット

```
33baf50 Add start_app.bat + show invest/refund breakdown in hero card
d4408ea Add Streamlit live prediction app: 4-state race tracker
f941274 Add full-pattern simulation
6c77279 Add 14-month simulation 2025-03~2026-04
2c66fa0 Extend simulation to 2025-10~2026-04
dd25049 Add 2026-03~04 simulation
f3d0417 Add stale-model warning to ev_threshold_sweep
9eb0594 Add threshold review: thr=1.50 が最適
d0e2bd7 Fix daily_predict crash on unpublished odds
```

| 種別 | パス |
|---|---|
| 動的発火 | `dynamic_scheduler.py` |
| 予測 + メール送信 | `daily_predict.py` (特に L520-555) |
| メール実装 | `gmail_notify.py` |
| 戦略仕様 | `docs/ev_strategy_findings.md` |
| 本資料の検証ロジック | `app/streamlit_app.py:fetch_live_day` |

## 6. 改善提案(切り分けと並行で検討)

### 6-1. **digest メール**(1 日 1 回サマリ)

朝 or 夕の固定時刻に「**本日 N R 評価 / 候補 K / 送信 M / エラー L**」を送る。
これがあれば「メール 0 通」が「正常 0 通」か「異常 silent」かが翌朝までに判明する。

実装: 30 分。`weekly_status.py` のロジック流用 + Windows task 1 つ追加。

### 6-2. **発火後 EV ログ強化**

各 dynamic task の `daily_predict.log` に「R{n} EV={x.xx} threshold=1.50 → {SEND/SKIP}」
を 1 行で記録。後追い検証が一発で可能になる。

実装: 5 行修正。daily_predict.py の picks 計算後に1行追加。

### 6-3. **閾値の時刻安定性検証**

walk-forward の odds スナップショットを発走 30 分前 / 15 分前 / 直前で比較する
バックテスト。仮説 A の真偽と、適切な発火タイミングを判断する材料になる。

実装: 2-3 時間 (オッズ履歴データの確保が必要)。

## 7. 次セッションが最初にやること

1. 上記 §3 の debug コマンドを実行 → ログを貼って報告
2. 6 R のうち何件が「発火した / 候補ありと判定 / メール送信成功」を表化
3. 仮説 A/B/C のどれが真因か確定
4. §6-1 の digest メールは仮説に関係なく実装推奨

```bash
git pull origin main
cat HANDOFF_2026-04-30_evening_email_missing.md   # 本ファイル
```

---

## 8. 追加発見: pred-top1 vs max-EV 戦略の本日比較

ライブアプリで R7 isesaki を見ていたユーザーが「2 番目の EV 高い車が結局 1 着になってる気がする」と
気づいた。本日 20 R 確定分で検証したところ、**短期サンプルだが max-EV が大きく上回る**。

### 8-1. 本日 20 R の比較 (複勝ベース、¥100 ベット)

| 戦略 | hit | 投資 | 払戻 | ROI | 収支 |
|---|---:|---:|---:|---:|---:|
| pred-top1 (現本番) | 14/20 (70%) | ¥2,000 | ¥1,920 | **96.0%** | **-¥80** |
| **max-EV** (EV 最大車を 1 R 1 つ) | 8/20 (40%) | ¥2,000 | ¥2,940 | **147.0%** | **+¥940** |

- pred-top1: 的中率高いが オッズ薄くてトントン未満
- max-EV: 的中率低いが オッズ厚くて圧勝

### 8-2. R7 isesaki の典型例

| 順位 | 車 | pred_calib | 平均複勝オッズ | EV |
|---:|---:|---:|---:|---:|
| pred 1位 | 4 | 0.824 | 約 1.55 | 1.28 |
| **pred 2位** | **5** | 0.754 | 約 3.14 | **2.37** ⭐ |
| pred 3位 | 3 | 0.672 | 約 1.76 | 1.18 |

**実結果: 5→4→7** (#5 が 1 着) → max-EV が当たって +¥80, pred-top1 (#4) は 2 着で +¥20。

### 8-3. これが構造的勝ちか確認すべきこと

サンプル 20 R では結論できない。月次 walk-forward で検証必要:

1. **scripts/ev_strategy_compare.py** (本コミットで追加) を実行
   - eval set 25 ヶ月で top1 / max-EV / all-cars 戦略を比較
   - 月次対決、年次集計、ゼロ日割合で判定
2. 結果を `reports/ev_strategy_compare_YYYY-MM-DD.md` に出力
3. もし max-EV が **8 月以上勝ち** で **総 profit も上回る** なら戦略変更検討
4. 同時に検討: 戦略変更すると thr=1.50 の最適値が変わる(再 sweep 必要)

### 8-4. 朝の email 問題 との関連

- §1 の 6 R で EV>=1.50 だったが、これは pred-top1 ベース
- max-EV ベースだと **本日 全 20 R すべてで EV>=2 越え**(1 R 1 つは必ず value bet がある)
- もし戦略を切替えるなら、通知頻度が大幅に上がる(月数百件か?)
- **digest メールの必要性が増す** (個別通知だと埋もれる)

### 8-5. 21:00 着手予定の調査メニュー

```bash
git pull origin main

# A. 朝の email 問題 (§3)
powershell -c "Get-Content data\daily_predict.log -Tail 500" > /tmp/log_today.txt
# 仮説 A/B/C 切り分け

# B. max-EV 戦略の月次対決 (新規)
python scripts/ev_strategy_compare.py
# → reports/ev_strategy_compare_2026-04-30.md
# → top1 vs max-EV vs all_cars の月勝率, 総 profit, 月次 ROI std

# C. 統合判断
# A の結果と B の結果から、本番運用を変える価値があるか判断
```
