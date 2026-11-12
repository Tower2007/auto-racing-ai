"""テスト全体の安全装置 (2026-11-12)。

テストからの実メール送信を全面的に禁止する。テストが通知関数の差し替えを忘れても
gmail_notify.send_email が呼ばれた時点で例外にする (2026-11-12、差し替え漏れの
テストから要確認メールが 7 通実送信された事故の再発防止)。
send_email 自体を検査するテスト (gmail_notify の送信ログ) は send_email を残し、
SMTP 接続 (smtplib) だけを禁止する。
"""

from __future__ import annotations

import smtplib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# send_email の中身を検査するテスト (自前で送信実体を差し替える)
_SEND_EMAIL_UNDER_TEST = {"test_mail_sent_log_and_weekly_login_pattern"}


@pytest.fixture(autouse=True)
def _no_real_mail(monkeypatch, request):
    def _blocked(*a, **kw):
        raise RuntimeError("テスト中の実メール送信は禁止 (tests/conftest.py)")

    # SMTP 接続は全テストで禁止
    monkeypatch.setattr(smtplib, "SMTP", _blocked)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _blocked)
    if request.module.__name__.rsplit(".", 1)[-1] not in _SEND_EMAIL_UNDER_TEST:
        import gmail_notify
        monkeypatch.setattr(gmail_notify, "send_email", _blocked)
    yield
