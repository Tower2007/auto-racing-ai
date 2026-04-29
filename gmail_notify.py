"""
Gmail SMTP 送信モジュール
-----------------------------------------------------------------------
boat-racing-ai 版から移植。.env に以下を設定:
  GMAIL_USER=no28akira2007@gmail.com
  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
  MAIL_TO=no28akira2007@gmail.com,other@example.com

使い方:
  from gmail_notify import send_email
  send_email(
      subject="[autorace] 週次レポート",
      body="プレーンテキスト",
      html="<html>...</html>",   # 任意
      recipients=None,            # None で .env の MAIL_TO を使う
  )

直接実行で動作確認:
  python gmail_notify.py
"""

from __future__ import annotations

import mimetypes
import os
import smtplib
import ssl
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path

from dotenv import load_dotenv


SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
TIMEOUT_SEC = 30


def _load_config() -> tuple[str, str, list[str]]:
    load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)
    user = os.environ.get("GMAIL_USER", "").strip()
    pwd = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to = os.environ.get("MAIL_TO", "").strip()
    if not (user and pwd):
        raise RuntimeError(
            ".env に GMAIL_USER / GMAIL_APP_PASSWORD を設定してください。\n"
            "アプリパスワード: https://myaccount.google.com/apppasswords"
        )
    recipients = [a.strip() for a in to.split(",") if a.strip()] or [user]
    return user, pwd, recipients


def send_email(
    subject: str,
    body: str,
    html: str | None = None,
    recipients: list[str] | None = None,
    attachments: list[str | Path] | None = None,
) -> None:
    """Gmail 経由でメール送信。attachments はファイルパスのリスト。"""
    user, pwd, default_to = _load_config()
    to_list = recipients or default_to

    if attachments:
        # mixed (attachment) > alternative (text/html)
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = ", ".join(to_list)
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))
        if html:
            alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)

        for path in attachments:
            p = Path(path)
            if not p.exists():
                raise FileNotFoundError(f"添付ファイルが見つかりません: {p}")
            ctype, encoding = mimetypes.guess_type(str(p))
            if ctype is None or encoding is not None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(p, "rb") as fh:
                part = MIMEBase(maintype, subtype)
                part.set_payload(fh.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{p.name}"',
            )
            msg.attach(part)
    else:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = ", ".join(to_list)
        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html:
            msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT_SEC) as s:
        s.starttls(context=ctx)
        s.login(user, pwd)
        s.sendmail(user, to_list, msg.as_string())
    print(f"[mail] sent to {to_list}  subject={subject}"
          + (f"  (添付 {len(attachments)} 個)" if attachments else ""))


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--to", default=None, help="送信先(カンマ区切り)、未指定で .env の MAIL_TO")
    args = p.parse_args()
    recipients = [a.strip() for a in args.to.split(",")] if args.to else None
    send_email(
        subject="[auto-racing-ai] テスト送信",
        body="gmail_notify.py の動作確認です。\n\nこのメールが届けば SMTP 設定 OK。",
        html="<h2>auto-racing-ai テスト送信</h2><p>このメールが届けば SMTP 設定 OK。</p>",
        recipients=recipients,
    )
    print("OK")
