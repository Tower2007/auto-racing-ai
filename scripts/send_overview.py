"""docs/project_overview.md をメール送信(添付付き)

使い方:
  python scripts/send_overview.py
  python scripts/send_overview.py --to friend@example.com
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gmail_notify import send_email

DOC = ROOT / "docs" / "project_overview.md"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--to", default=None, help="送信先(カンマ区切り)、未指定で .env の MAIL_TO")
    args = p.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not DOC.exists():
        print(f"エラー: {DOC} が見つかりません")
        sys.exit(1)

    md = DOC.read_text(encoding="utf-8")
    today = dt.date.today().isoformat()

    intro = (
        f"auto-racing-ai プロジェクトの全体ドキュメントです。\n"
        f"({today} 時点 / 添付: project_overview.md)\n"
        f"\n"
        f"以下、本文(添付ファイルと同じ内容):\n"
        f"\n"
        f"=" * 60 + "\n\n"
    )
    body = intro + md

    html = f"""
<div style="font-family:Arial,sans-serif; font-size:14px; color:#222; line-height:1.55;">
  <p>auto-racing-ai プロジェクトの全体ドキュメントです。<br>
  <span style="color:#666;">({today} 時点 / 添付: <code>project_overview.md</code>)</span></p>
  <p>添付ファイルにプロジェクトの経緯・データ仕様・ML パイプライン・運用設計・
  リスクなどを詳細にまとめています。Markdown 形式なので任意のエディタや
  Markdown viewer で読めます。</p>
  <p style="color:#666; font-size:13px;">概要(添付の章立て):</p>
  <ol style="color:#444;">
    <li>3 行サマリー</li>
    <li>プロジェクトの目的と背景</li>
    <li>データソース・仕様</li>
    <li>ML パイプライン</li>
    <li>EV-based 選別戦略の発見プロセス</li>
    <li>運用設計(Phase A: 推奨提示型)</li>
    <li>運用結果モニタリング</li>
    <li>リスク・限界</li>
    <li>技術スタック / ファイル構成</li>
    <li>経緯タイムライン</li>
    <li>関連プロジェクト</li>
    <li>リポジトリ参照</li>
  </ol>
  <hr style="border:none; border-top:1px solid #ddd; margin:18px 0 8px 0;">
  <p style="color:#999; font-size:11px; margin:0;">auto-racing-ai documentation export</p>
</div>
"""

    recipients = None
    if args.to:
        recipients = [a.strip() for a in args.to.split(",") if a.strip()]

    subject = f"[autorace] プロジェクト全体ドキュメント({today})"
    send_email(
        subject=subject,
        body=body,
        html=html,
        recipients=recipients,
        attachments=[DOC],
    )
    print(f"送信完了: {subject}")


if __name__ == "__main__":
    main()
