# Streamlit Community Cloud デプロイ手順 (2026-08-10)

iPhone / 外出先から予想アプリを見るための公開デプロイ手順。

## なぜ Vercel ではないか

Vercel では **Streamlit は動かない**。2 つの理由:

1. **アーキテクチャが合わない** — Streamlit は WebSocket の常時接続と常駐
   プロセスを前提とする。Vercel Functions はリクエスト単位でスピンアップして
   凍結するサーバーレスで、長時間の双方向接続を保持できない
2. **依存が重い** — `lightgbm` + `pyarrow` + `pandas` + `numpy` +
   `scikit-learn` で Vercel Hobby の解凍後 250MB 上限を超える

Vercel が向くのは Next.js 等のフロントエンド + 軽量 API であって、
Streamlit のホスティング先ではない。

代替として常駐プロセスを動かせるのは Streamlit Community Cloud /
Render / Railway / Fly.io、あるいは家 PC を Cloudflare Tunnel で公開する方法。
本プロジェクトは **Streamlit Community Cloud 前提で既に実装済み**のため、
それを使うのが最短。

---

## 事前検証の結果 (2026-08-10、出先 PC で実施)

`DEPLOY_MODE=cloud` を明示してローカル起動し、cloud モードが実際に動くことを
確認済み。**コード変更は不要**だった。

| 確認項目 | 結果 |
|---|---|
| 起動 | OK (`Uvicorn server started`) |
| `production_model.lgb` ロード | OK (git 管理下なのでクラウドにも届く) |
| 開催場の自動検出 | OK (2026-08-10 飯塚 12R を検出) |
| autorace.jp API アクセス | OK (`.env` 不要 — `AUTORACE_REQUEST_DELAY_SEC` は
  `src/client.py:48` でデフォルト 0.5 が効く) |
| 12R の予想レンダリング | OK (オッズ未公開帯なので公式 AI 予想にフォールバック) |
| ブラウザコンソールエラー | なし |

検証コマンド (再現用):

```bash
DEPLOY_MODE=cloud python -m streamlit run app/streamlit_app.py --server.port 8502
```

Windows PowerShell なら `$env:DEPLOY_MODE="cloud"` を先に実行。
**8502 は `buy_app.py` の既定ポートなので、検証時は buy_app が動いていないことを
確認すること**(動いていると別プロセスの応答を掴んで「動いた」と誤判定する)。

---

## デプロイ手順

1. https://share.streamlit.io/ を開き、GitHub アカウントでサインイン
2. **New app** → **Deploy a public app from GitHub**
3. 以下を指定:

   | 項目 | 値 |
   |---|---|
   | Repository | `Tower2007/auto-racing-ai` |
   | Branch | `main` |
   | Main file path | `app/streamlit_app.py` |

4. **Advanced settings** → Python version は **3.13**(ローカルと揃える)
5. **Deploy** を押す

`DEPLOY_MODE` の環境変数設定は**不要**。Streamlit Cloud は `/mount/src` 配下で
実行されるので `app/streamlit_app.py:54` が自動で cloud モードに切り替える。

デプロイ後の URL は `https://<app-name>.streamlit.app`。

---

## ビルドが失敗した場合

`requirements.txt` にアプリが使わない重い依存が 2 つ入っている:

- `playwright>=1.40` — `scripts/auto_login_autorace.py` 専用(家 PC の日次タスク)
- `browser_cookie3>=0.20` — 購入履歴取得の cookie fallback 専用

**まずはそのままデプロイを試す**(インストール自体は通るはずで、ビルドが
遅くなるだけ)。もし失敗したら、この 2 つを `requirements.txt` から外して
`requirements-tools.txt` に分離し、家 PC 側は

```bash
pip install -r requirements.txt -r requirements-tools.txt
```

に切り替える。**先回りで分離しないのは、家 PC の `AutoraceFetchOrderHistory`
(playwright auto-login) を壊すリスクの方が、ビルド時間より重いため**。

Python 3.13 で lightgbm の wheel が無くビルドが落ちる場合は 3.12 に下げる。

---

## 公開範囲についての注意

- **アプリに認証は無い**。URL を知っていれば誰でも閲覧できる
- **リポジトリは PUBLIC**。`.gitignore` で `data/*` を落としたうえで、以下だけ
  例外指定して公開している:
  - `production_model.lgb` / `production_calib.pkl` / `production_meta.json`(本番モデル)
  - `expected_votes.csv`
  - **`bet_history.csv` / `bet_history_detail.csv`** — vote.autorace.jp の
    実購入履歴(日付・場・R・賭け金・払戻・収支)。2026-05-01 以降 283 行。
    これは個人の金銭記録なので、公開したくないなら `.gitignore` の例外指定を
    外し、`git rm --cached` + 履歴からの除去が必要
- `.env` / `accounts.json` は追跡外(`.env.example` と `accounts.json.template`
  のみ commit)。認証情報の漏洩は無い
- cloud モードはリプレイ機能を無効化し、大型 CSV / parquet を読まない
  (`app/streamlit_app.py:1039`)。個人の予測ログは公開版には出ない

---

## 家 PC 常駐との使い分け

| | Streamlit Cloud | 家 PC + Cloudflare Tunnel |
|---|---|---|
| 家 PC が落ちても見られる | ✅ | ❌ |
| リプレイ / 過去検証 UI | ❌ (大型 CSV 非同梱) | ✅ フル機能 |
| 認証 | 無し | Cloudflare Access で付けられる |
| 手間 | GitHub 連携のみ | tunnel 設定が必要 |

両方を併用しても問題ない(Cloud は公開版、家 PC は 8501 でフル版)。
