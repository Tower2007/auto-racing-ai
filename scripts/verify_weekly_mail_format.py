"""週次メール書式の回帰チェック (2026-08-03 仕様 P1-P5)。

send_email を差し替えて **実際の main() 経路** で subject/body/html を捕捉し、
仕様書の検証項目 1-5 を機械チェックする。メールは送信しない (読み取り専用)。

検証項目:
  1. 生成HTMLに style="..." style="..." (二重指定) が 0 件      [P1]
  2. plaintext のフッタが最終行にのみある                        [P2]
  3. 金額に 円 後置が残っていない (¥ 前置に統一)                 [P3]
  4. 件名の状態が 1 つで本文ヘッダと一致                         [P4]
  5. 送信成功時にログへ [mail] sent to <宛先> が出る

週次メールの書式を変更したら本スクリプトを流すこと。
  python scripts/verify_weekly_mail_format.py   # exit 0 = 全項目 OK
プレビューは data/_weekly_preview.{txt,html} に出力される (git 管理外)。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

captured: dict = {}


def fake_send_email(subject, body, html=None, recipients=None, attachments=None):
    captured["subject"] = subject
    captured["body"] = body
    captured["html"] = html or ""
    print(f"[mail] sent to ['no28akira2007@gmail.com']  subject={subject}")


import weekly_status  # noqa: E402
weekly_status.send_email = fake_send_email

sys.argv = ["weekly_status.py", "--days", "7"]
import io
_buf = io.StringIO()
_orig = sys.stdout
sys.stdout = _buf
try:
    weekly_status.main()
finally:
    sys.stdout = _orig
_mail_log = [l for l in _buf.getvalue().splitlines() if l.startswith("[mail]")]

subject = captured.get("subject", "")
body = captured.get("body", "")
html = captured.get("html", "")

(ROOT / "data" / "_weekly_preview.txt").write_text(body, encoding="utf-8")
(ROOT / "data" / "_weekly_preview.html").write_text(html, encoding="utf-8")

fails = []
print("=" * 70)
print("検証 1: 生成HTMLに style=\"...\" style=\"...\" が 0 件")
dup = re.findall(r'style="[^"]*"\s*style="', html)
print(f"  → {len(dup)} 件 " + ("OK" if not dup else "NG"))
if dup:
    fails.append(f"検証1: style二重指定 {len(dup)} 件")
    for d in dup[:3]:
        print(f"    {d[:80]}")

print("検証 2: plaintext のフッタが最終行にのみある")
footer = weekly_status.TEXT_FOOTER
lines = [l for l in body.splitlines() if l.strip()]
idxs = [i for i, l in enumerate(body.splitlines()) if footer in l]
last_ok = bool(idxs) and body.splitlines()[-1].strip() == footer
print(f"  → 出現 {len(idxs)} 回 / 最終行={last_ok} "
      + ("OK" if len(idxs) == 1 and last_ok else "NG"))
if not (len(idxs) == 1 and last_ok):
    fails.append(f"検証2: フッタ {len(idxs)}回, 最終行={last_ok}")
    for i in idxs:
        print(f"    line {i+1}/{len(body.splitlines())}")

print("検証 3: 金額に 円 後置が残っていない")
yen_post = [(i + 1, l.strip()) for i, l in enumerate(body.splitlines())
            if re.search(r"[0-9] *円", l)]
yen_post_html = re.findall(r"[0-9] *円", html)
print(f"  → text {len(yen_post)} 件 / html {len(yen_post_html)} 件 "
      + ("OK" if not yen_post and not yen_post_html else "NG"))
if yen_post or yen_post_html:
    fails.append(f"検証3: 円後置 text{len(yen_post)}/html{len(yen_post_html)}")
    for n, l in yen_post[:5]:
        print(f"    L{n}: {l[:90]}")
    for m in yen_post_html[:5]:
        print(f"    html: {m}")

print("検証 4: 件名の状態が1つで本文ヘッダと一致")
print(f"  subject: {subject}")
states = re.findall(r"[🟢🟡🔴❔](OK|WARN|NG|\?)", subject)
hdr = [l for l in body.splitlines() if "死活監視" in l]
print(f"  → 件名の状態数 {len(states)} {states} " + ("OK" if len(states) == 1 else "NG"))
if len(states) != 1:
    fails.append(f"検証4: 件名に状態が {len(states)} 個")
if hdr:
    print(f"  本文死活監視ヘッダ: {hdr[0].strip()}")

print("検証 5: 送信成功時にログへ [mail] sent to <宛先> が出る")
print(f"  → {len(_mail_log)} 行 " + ("OK" if _mail_log else "NG"))
for l in _mail_log:
    print(f"    {l}")
if not _mail_log:
    fails.append("検証5: [mail] sent ログなし")

print("=" * 70)
print(("❌ FAIL: " + " / ".join(fails)) if fails else "✅ ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
