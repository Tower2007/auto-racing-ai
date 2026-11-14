"""dynamic_scheduler のログオン時救済登録 (2026-11-12 監査 指摘4) のテスト。

実際のタスクスケジューラには触れない: schtasks 呼び出し (register_one_shot_ex /
existing_task_start / register_alt_task / cleanup_stale_tasks)、現在時刻 (_now)、
マーカー (STATE_FILE)、API クライアント、通知、ログ設定を全て差し替える。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dynamic_scheduler as ds  # noqa: E402

DAY = dt.date(2026, 11, 12)
# 飯塚ミッドナイト 3R だけの開催 (R1 21:28 / R2 21:54 / R3 24:04=翌 00:04)
TIMES = {1: "21:28", 2: "21:54", 3: "24:04"}


class _FakeClient:
    def get_program_print_times(self, venue_key, date_str):
        return dict(TIMES)


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = types.SimpleNamespace(created=[], alt=[], cleanup=0, notified=[],
                               existing={}, deny=set(), alt_fail=set(),
                               other_fail=set())
    monkeypatch.setattr(ds, "STATE_FILE", tmp_path / "dynamic_scheduler_state.json")
    monkeypatch.setattr(ds, "setup_logging", lambda: None)
    monkeypatch.setattr(ds, "AutoraceClient", _FakeClient)
    monkeypatch.setattr(ds, "fetch_today_schedule",
                        lambda client: {5: {"cancelFlg": "0"}})
    # main は取得失敗と開催なしを区別する _ex を使う (2026-11-12)
    monkeypatch.setattr(ds, "fetch_today_schedule_ex",
                        lambda client: ({5: {"cancelFlg": "0"}}, None))
    ns.problems = []
    monkeypatch.setattr(ds, "notify_problem",
                        lambda today, what, body: ns.problems.append(what))

    def _cleanup():
        ns.cleanup += 1
        return 0
    monkeypatch.setattr(ds, "cleanup_stale_tasks", _cleanup)

    def _create(name, fire_at, command):
        if name in ns.deny:
            return False, "ERROR: Access is denied."
        if name in ns.other_fail:
            return False, "ERROR: 想定外のエラー"
        ns.created.append((name, fire_at))
        return True, ""
    monkeypatch.setattr(ds, "register_one_shot_ex", _create)
    monkeypatch.setattr(ds, "existing_task_start", lambda name: ns.existing.get(name))

    def _alt(name, fire_at, command):
        if name in ns.alt_fail:
            return False, "ERROR: Access is denied."
        ns.alt.append((name, fire_at))
        return True, ""
    monkeypatch.setattr(ds, "register_alt_task", _alt)
    monkeypatch.setattr(ds, "notify_failures",
                        lambda today, failed: ns.notified.append(list(failed)))

    def _forbid(*a, **kw):
        raise AssertionError("実 schtasks 呼び出し禁止")
    monkeypatch.setattr(ds.subprocess, "run", _forbid)
    ns.mp = monkeypatch
    ns.tmp = tmp_path
    return ns


def _at(env, h, m):
    env.mp.setattr(ds, "_now", lambda: dt.datetime(2026, 11, 12, h, m, 5))


def _marker(env):
    p = env.tmp / "dynamic_scheduler_state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def test_marker_present_does_nothing(env):
    ds.write_marker(DAY, dt.datetime(2026, 11, 12, 7, 0), [{"race": "x"}],
                    path=env.tmp / "dynamic_scheduler_state.json")
    _at(env, 9, 0)
    assert ds.main() == 0
    assert env.cleanup == 0 and env.created == [] and env.alt == []


def test_old_marker_is_ignored(env):
    ds.write_marker(DAY - dt.timedelta(days=1), dt.datetime(2026, 11, 11, 7, 0), [],
                    path=env.tmp / "dynamic_scheduler_state.json")
    _at(env, 9, 0)
    assert ds.main() == 0
    assert len(env.created) == 3      # 前日のマーカーでは止まらない


def test_before_main_schedule_does_nothing(env):
    """定時 (07:00) 前・マーカーなし → 本体に任せる (前日ミッドナイトのタスクを消さない)。"""
    _at(env, 5, 45)
    assert ds.main() == 0
    assert env.cleanup == 0 and env.created == []
    assert _marker(env) is None


def test_main_schedule_run_registers_normally(env):
    """本体の定時実行 (07:00:05) → 通常名で全 R 登録・マーカー記録。"""
    _at(env, 7, 0)
    assert ds.main() == 0
    assert env.cleanup == 1
    names = [n for n, _ in env.created]
    assert names == ["AutoraceDyn_iizuka_R1", "AutoraceDyn_iizuka_R2",
                     "AutoraceDyn_iizuka_R3"]
    # 日跨ぎ R3 (24:04) は翌日 00:00 発火 (飯塚も 4 分前。2026-11-14 に 3 分前から変更)
    assert env.created[2][1] == dt.datetime(2026, 11, 13, 0, 0)
    mk = _marker(env)
    assert mk["date"] == "2026-11-12" and mk["n_registered"] == 3
    assert env.notified == []


def test_rescue_uses_alt_name_on_access_denied(env):
    """定時後・マーカーなし (本体が飛んだ) → 前日の同名タスクが消せず Access denied
    → 別名で登録。既に同じ時刻で登録済みの同名タスクは既存を使う。"""
    _at(env, 14, 50)
    env.deny = {"AutoraceDyn_iizuka_R1", "AutoraceDyn_iizuka_R2"}
    # R1: 前日の同名タスク (時刻違い) → 別名、R2: 本日の同時刻で登録済み → 既存
    env.existing = {
        "AutoraceDyn_iizuka_R1": dt.datetime(2026, 11, 11, 21, 24),
        "AutoraceDyn_iizuka_R2": dt.datetime(2026, 11, 12, 21, 50),
    }
    assert ds.main() == 0
    assert [n for n, _ in env.alt] == ["AutoraceDyn_iizuka_R1_alt"]
    assert [n for n, _ in env.created] == ["AutoraceDyn_iizuka_R3"]
    mk = _marker(env)
    st = {r["race"]: (r["status"], r["task"]) for r in mk["races"]}
    assert st["iizuka_R1"] == ("registered_alt", "AutoraceDyn_iizuka_R1_alt")
    assert st["iizuka_R2"][0] == "existing"
    assert all(t.startswith("AutoraceDyn_") for _, t in st.values())
    assert env.notified == []


def test_rescue_failure_exits_nonzero_and_notifies(env):
    _at(env, 14, 50)
    env.deny = {"AutoraceDyn_iizuka_R1"}
    env.alt_fail = {"AutoraceDyn_iizuka_R1_alt"}
    env.other_fail = {"AutoraceDyn_iizuka_R2"}     # 判別できない失敗は別名を試さず失敗
    assert ds.main() == 2
    assert len(env.notified) == 1
    failed = env.notified[0]
    assert len(failed) == 2
    assert failed[0].startswith("iizuka_R1") and failed[1].startswith("iizuka_R2")
    assert env.alt == []                             # R2 は別名を試していない
    assert _marker(env) is None                      # 次回ログオン時に再試行できる


def test_past_races_are_not_targets(env):
    """発走済み (発火時刻が過ぎた) R は登録対象外・失敗にも数えない。"""
    _at(env, 22, 0)      # R1 21:24 / R2 21:50 発火は過去、R3 (翌 00:00) のみ
    assert ds.main() == 0
    assert [n for n, _ in env.created] == ["AutoraceDyn_iizuka_R3"]
    assert env.notified == []
    assert _marker(env)["n_registered"] == 1


def test_fetch_failure_does_not_write_marker_and_notifies(env):
    """開催一覧の取得失敗ではマーカーを書かず、非 0 + 要確認メール (2026-11-12)。
    書くと通信エラーの日に再実行・救済が「登録済み」で止まる。"""
    env.mp.setattr(ds, "fetch_today_schedule_ex",
                   lambda client: ({}, "Hold/Today 取得失敗: timeout"))
    _at(env, 7, 0)
    assert ds.main() == 1
    assert _marker(env) is None
    assert env.problems == ["開催一覧の取得失敗"]
    assert env.created == []


def test_confirmed_no_meeting_writes_marker(env):
    """取得に成功して開催 0 件 = 開催なし確定 → マーカーを書く (同日の再実行は何もしない)。"""
    env.mp.setattr(ds, "fetch_today_schedule_ex", lambda client: ({}, None))
    _at(env, 7, 0)
    assert ds.main() == 0
    assert _marker(env)["n_registered"] == 0
    assert env.problems == []


def test_client_init_failure_notifies(env):
    def _boom():
        raise RuntimeError("CSRF 取得失敗")
    env.mp.setattr(ds, "AutoraceClient", _boom)
    _at(env, 7, 0)
    assert ds.main() == 1
    assert env.problems == ["API クライアント初期化失敗"]
    assert _marker(env) is None


def test_gate_opens_five_minutes_before_main_schedule(env):
    """定時判定の余裕: 06:54 は本体に任せる / 06:55 以降は登録する
    (本体の起動時刻が時計のずれで 07:00 より僅かに前と読まれても登録が飛ばない)。"""
    _at(env, 6, 54)
    assert ds.main() == 0
    assert env.created == [] and _marker(env) is None
    _at(env, 6, 59)
    assert ds.main() == 0
    assert len(env.created) == 3 and _marker(env)["n_registered"] == 3


def test_access_denied_detection():
    assert ds.is_access_denied("ERROR: Access is denied.")
    assert ds.is_access_denied("エラー: アクセスが拒否されました。")
    assert not ds.is_access_denied("ERROR: The parameter is incorrect.")
    assert not ds.is_access_denied("")


def test_alt_task_xml_expires_and_keeps_command():
    cmd = ds.CMD_TEMPLATE.format(vbs=ds.VBS_WRAPPER, pc=5, race_no=8,
                                 label="iizuka_R8", date="2026-11-12")
    xml = ds.build_alt_task_xml(dt.datetime(2026, 11, 13, 0, 27), cmd)
    assert "<StartBoundary>2026-11-13T00:27:00</StartBoundary>" in xml
    assert "<EndBoundary>2026-11-13T02:27:00</EndBoundary>" in xml
    assert "<DeleteExpiredTaskAfter>PT1H</DeleteExpiredTaskAfter>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<Command>wscript.exe</Command>" in xml
    assert '5 8 "iizuka_R8" 2026-11-12</Arguments>' in xml
    assert ("AutoraceDyn_iizuka_R8" + ds.ALT_SUFFIX).startswith(ds.TASK_PREFIX)
