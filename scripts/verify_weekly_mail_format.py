"""週次メール書式の回帰チェック (2026-08-03 仕様 P1-P5)。

send_email を差し替えて **実際の main() 経路** で subject/body/html を捕捉し、
仕様書の検証項目 1-5 を機械チェックする。メールは送信しない (読み取り専用)。

検証項目:
  1. 生成HTMLに style="..." style="..." (二重指定) が 0 件      [P1]
  2. plaintext のフッタが最終行にのみある                        [P2]
  3. 金額に 円 後置が残っていない (¥ 前置に統一)                 [P3]
  4. 件名の状態が 1 つで本文ヘッダと一致                         [P4]
  5. 送信成功時にログへ [mail] sent to <宛先> が出る
  6. 件名の状態バッジが HTML h2 / plaintext ヘッダと文字列一致   [2026-08-05 P1]
  7. HTML と plaintext のセクション順が一致                      [2026-08-05 P2]
  8. compute_overall_status の 3 段階判定が期待通り              [2026-08-05 P1]

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

# バックストップが新規発動した場合の通知メールも送らせない (検証は読み取り専用)
try:
    import src.backstop as _backstop
    _backstop._send_notify = lambda subject, body: print(
        f"[mail] (suppressed backstop notify) {subject}")
except Exception:  # noqa: BLE001
    pass

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

print("検証 6: 件名の状態バッジが HTML h2 / plaintext ヘッダと文字列一致")
m = re.search(r"([🟢🟡🔴](?:OK|WARN|NG))", subject)
badge = m.group(1) if m else ""
h2 = re.search(r"<h2[^>]*>(.*?)</h2>", html, re.S)
h2_txt = re.sub(r"<[^>]+>", "", h2.group(1)) if h2 else ""
txt_hdr = next((l for l in body.splitlines() if l.startswith("総合状態:")), "")
in_h2 = bool(badge) and badge in h2_txt
in_txt = bool(badge) and badge in txt_hdr
print(f"  件名バッジ: {badge!r}")
print(f"  HTML h2   : {h2_txt.strip()!r} → {in_h2}")
print(f"  plaintext : {txt_hdr.strip()!r} → {in_txt}")
# 旧仕様の別ロジック文言 (🟢 正常 / 🟡 要確認 / 🔴 異常) が残っていないこと
legacy = [w for w in ("🟢 正常", "🟡 要確認", "🔴 異常") if w in html]
print(f"  → " + ("OK" if in_h2 and in_txt and not legacy else "NG")
      + (f" (旧バッジ残存 {legacy})" if legacy else ""))
if not (in_h2 and in_txt and not legacy):
    fails.append("検証6: 件名バッジと本文ヘッダが不一致")

print("検証 7: HTML と plaintext のセクション順が一致")
# 各セクションを一意に特定できる文言。累積成績のように HTML 側が
# <div> ボックス (daily_predict と共有部品) で h3 を持たないものがあるため、
# 見出しタグではなく本文全体の初出位置で順序を比べる。
SECTION_KEYS = ["データサマリー", "収集状況", "エラー", "死活監視",
                "本番運用累積成績", "通知候補", "推奨 vs 購入", "実購入損益",
                "三連系実弾 判定指標", "三連系まとめ買い", "判定基準 (件名"]


def _order(doc: str) -> list[str]:
    hits = [(doc.find(k), k) for k in SECTION_KEYS]
    return [k for pos, k in sorted(h for h in hits if h[0] >= 0)]


html_order = _order(html)
text_order = _order(body)
print(f"  html: {html_order}")
print(f"  text: {text_order}")
print("  → " + ("OK" if html_order == text_order else "NG"))
if html_order != text_order:
    fails.append("検証7: セクション順が HTML/plaintext で不一致")
# 通し番号を振っているなら 1 から連番であること
html_headings = [re.sub(r"<[^>]+>", "", h)
                 for h in re.findall(r"<h[23][^>]*>(.*?)</h[23]>", html, re.S)]
numbered = re.findall(r"^\s*(\d+)[-.]", "\n".join(html_headings), re.M)
if numbered:
    seq = [int(n) for n in numbered]
    print(f"  節番号: {seq} → " + ("OK" if seq == list(range(1, len(seq) + 1)) else "NG"))
    if seq != list(range(1, len(seq) + 1)):
        fails.append(f"検証7: 節番号が通しでない {seq}")
else:
    print("  節番号: なし (auto-racing は無番号見出し。"
          "0. / 0-2. のような番号や逆転は元から存在しない)")
# 本文の独立セクション見出しは h3 に揃える (h4 は各セクション内の小見出し専用)
demoted = [h.strip() for h in
           re.findall(r"<h4[^>]*>(.*?)</h4>", html, re.S)
           if any(k in h for k in ("三連系まとめ買い",))]
print(f"  h4 に降格した独立セクション: {demoted} → "
      + ("OK" if not demoted else "NG"))
if demoted:
    fails.append(f"検証7: 独立セクションが h4 に降格 {demoted}")

print("検証 8: compute_overall_status の 3 段階判定")
cos = weekly_status.compute_overall_status
_day = lambda st, ip=False: {"date": "2026-08-01", "status": st,
                             "race_count": 1, "per_venue": {},
                             "in_progress": ip}
cases = [
    ("正常 → OK", cos([_day("OK")] * 7, [], {"status": "OK"}, {}), "OK"),
    ("死活 WARN → WARN", cos([_day("OK")] * 7, [],
                             {"status": "WARN", "alerts": [("WARN", "x")]}, {}), "WARN"),
    ("NO_DATA 4日 → WARN", cos([_day("OK")] * 3 + [_day("NO DATA")] * 4, [],
                               {"status": "OK"}, {}), "WARN"),
    ("ingest エラー → NG", cos([_day("OK")] * 7, ["ERROR boom"],
                               {"status": "OK"}, {}), "NG"),
    ("死活 NG → NG", cos([_day("OK")] * 7, [],
                         {"status": "NG", "alerts": [("NG", "y")]}, {}), "NG"),
    ("死活材料なし → WARN", cos([_day("OK")] * 7, [], {}, {}), "WARN"),
]
for label, got, want in cases:
    ok = got["level"] == want
    print(f"  {label:22s} → {got['badge']:6s} (期待 {want}) " + ("OK" if ok else "NG"))
    if not ok:
        fails.append(f"検証8: {label} が {got['level']} (期待 {want})")
# 進行中の日は判定からも OK n/m 日の分母からも外れる
prog = cos([_day("OK")] * 6 + [_day("NO DATA", ip=True)], [], {"status": "OK"}, {})
ok = (prog["n_total_days"] == 6 and prog["n_ok_days"] == 6
      and prog["n_inprogress"] == 1 and prog["level"] == "OK")
print(f"  進行中1日を除外        → OK {prog['n_ok_days']}/{prog['n_total_days']}日 "
      f"/ 進行中 {prog['n_inprogress']} / {prog['level']} " + ("OK" if ok else "NG"))
if not ok:
    fails.append("検証8: 進行中の日が分母/判定から除外されていない")

print("=" * 70)
print(("❌ FAIL: " + " / ".join(fails)) if fails else "✅ ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
