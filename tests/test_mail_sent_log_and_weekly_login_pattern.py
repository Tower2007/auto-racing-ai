"""2026-09-04 監査 P2 の回帰テスト (実メールは送らない)。

  - gmail_notify: 送信成功/失敗の痕跡を data/mail_sent.log に追記する
    (`[mail] sent <subject>` / `[mail] FAILED <subject> <例外型>`)
  - weekly_status: fetch_order_history.log の "[auto_login] ログインが必要 (...)"
    (正常な自動ログイン進行) を「ログイン要求」NG と誤検知しない

pytest があれば `pytest tests/test_mail_sent_log_and_weekly_login_pattern.py`、
無くても `python tests/...py` で実行可能。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gmail_notify  # noqa: E402
import weekly_status  # noqa: E402


# ─── gmail_notify: 送信痕跡ログ ───────────────────────────────────

def _with_tmp_mail_log(fn):
    tmp = Path(tempfile.mkdtemp(prefix="mail_sent_"))
    orig_log = gmail_notify.MAIL_SENT_LOG
    orig_impl = gmail_notify._send_email_impl
    gmail_notify.MAIL_SENT_LOG = tmp / "data" / "mail_sent.log"
    try:
        return fn(gmail_notify.MAIL_SENT_LOG)
    finally:
        gmail_notify.MAIL_SENT_LOG = orig_log
        gmail_notify._send_email_impl = orig_impl


def test_mail_sent_log_on_success():
    def body(log_p: Path):
        calls = []
        gmail_notify._send_email_impl = lambda *a, **kw: calls.append(a)
        gmail_notify.send_email("[autorace] テスト件名", "本文 (ログに出ない)")
        assert len(calls) == 1
        lines = log_p.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        # 'YYYY-MM-DD HH:MM:SS [mail] sent <subject>'
        assert lines[0][19:] == " [mail] sent [autorace] テスト件名", lines[0]
        assert "本文" not in lines[0]
    _with_tmp_mail_log(body)


def test_mail_sent_log_on_failure_records_exception_type_and_reraises():
    def body(log_p: Path):
        class _Boom(RuntimeError):
            pass

        def _raise(*a, **kw):
            raise _Boom("password=SECRET should not be logged")
        gmail_notify._send_email_impl = _raise
        raised = False
        try:
            gmail_notify.send_email("[autorace] 失敗件名", "本文")
        except _Boom:
            raised = True
        assert raised, "例外は再送出される"
        lines = log_p.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert lines[0][19:] == " [mail] FAILED [autorace] 失敗件名 _Boom", lines[0]
        assert "SECRET" not in lines[0]  # 例外メッセージ (秘匿情報の可能性) は書かない
    _with_tmp_mail_log(body)


def test_mail_log_write_failure_does_not_break_send():
    def body(log_p: Path):
        # ログ先がファイルの下 (mkdir 不能) でも send_email 自体は成功する
        blocker = log_p.parent.parent / "blocker"
        blocker.write_text("x", encoding="utf-8")
        gmail_notify.MAIL_SENT_LOG = blocker / "sub" / "mail_sent.log"
        calls = []
        gmail_notify._send_email_impl = lambda *a, **kw: calls.append(a)
        gmail_notify.send_email("[autorace] x", "y")
        assert len(calls) == 1
    _with_tmp_mail_log(body)


# ─── weekly_status: ログイン要求 誤検知 ───────────────────────────

NORMAL_AUTO_LOGIN_BLOCK = """=== daily_fetch_order_history START 2026-08-31 02:30:01 ===
[auto_login] ログインが必要 (current URL: https://vote.autorace.jp/)
[auto_login] user_id input: input[name='userId']
[auto_login] submit: button[type='submit']
[auto_login] login 完了: https://vote.autorace.jp/
[auto_login] cookie 取得 (17 個)
OK: bet_history.csv merged 3 rows
=== daily_fetch_order_history END rc=0 ===
"""


def _run_check_with_log(text: str) -> list[tuple[str, str]]:
    tmp = Path(tempfile.mkdtemp(prefix="weekly_login_"))
    (tmp / "fetch_order_history.log").write_text(text, encoding="utf-8")
    orig = weekly_status.DATA
    weekly_status.DATA = tmp
    try:
        out = {"alerts": [], "log_last_success": None}  # check_bet_history_health と同じ初期形
        weekly_status._check_fetch_order_log(out)
        assert not any("読込失敗" in msg for _, msg in out["alerts"]), out["alerts"]
        return out["alerts"]
    finally:
        weekly_status.DATA = orig


def test_auto_login_progress_line_is_not_login_required_ng():
    alerts = _run_check_with_log(NORMAL_AUTO_LOGIN_BLOCK)
    labels = [msg for lvl, msg in alerts if lvl == "NG"]
    assert not any("ログイン要求" in m for m in labels), alerts


def test_real_login_failure_is_still_detected():
    bad = NORMAL_AUTO_LOGIN_BLOCK.replace(
        "OK: bet_history.csv merged 3 rows",
        "fetch_order_history: login required (redirected to /login)")
    alerts = _run_check_with_log(bad)
    assert any(lvl == "NG" and "ログイン要求" in msg for lvl, msg in alerts), alerts


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {fn.__name__}: {e!r}")
    print(f"\n{passed}/{len(fns)} passed")
    return passed == len(fns)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(0 if _run_all() else 1)
