"""auto_buy プロセス間排他 (Windows named mutex) の回帰テスト
(2026-07-12 Codex艦隊監査 P1-1)。

旧実装 (O_CREAT|O_EXCL ロックファイル + 600s stale 破棄) には「生存中プロセスの
ロックでも 10 分経過で他者が unlink できる」削除経路があり、重複投票・日次 cap
二重通過が起こり得た。named mutex 置換 (統合マネジメントシステム
monitor/snapshot.py の実証済みパターン移植) を検証する:

  1. mutex 名にプロジェクトパスのハッシュが入る (固定名でない)
  2. 排他: 保持中は別スレッドの取得が None (所有はスレッド単位)
  3. 解放後は別スレッドから取得できる (ReleaseMutex が機能)
  4. クラッシュ回復: 子プロセスが解放せず即死 → abandoned mutex を
     引き継いで取得できる (待ち続け・永久ブロックにならない)
  5. プロセス間排他: 親保持中は子プロセスの取得が WAIT_TIMEOUT (0x102)
  6. run_auto_buy: 取得不能時は skip_lock verdict で発注経路に入らない /
     取得成功時はロック下で本体を呼び、終了後にロックが解放されている

発注経路 (execute_purchase / _run_auto_buy_locked 実体) には一切到達しない。
pytest があれば `pytest tests/test_auto_buy_lock.py`、
無くても `python tests/test_auto_buy_lock.py` で実行可能。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_buy  # noqa: E402

IS_WINDOWS = os.name == "nt"


def _test_name() -> str:
    """テスト専用 mutex 名 (本番名と干渉しない)。"""
    return f"Global\\AutoRacingAITest_{uuid.uuid4().hex[:8]}"


def _child_code(name: str, wait_ms: int) -> str:
    """子プロセス: mutex を取得試行して rc を印字、Release せず即死 (abandoned)。

    注意 (統合管理 tests/test_detection.py の教訓): {name!r} の repr は
    バックスラッシュを二重化するため、生成後の文字列への .replace は効かない。
    名前ごとにこのヘルパで生成し直すこと。
    """
    return (
        "import ctypes, os, sys\n"
        "from ctypes import wintypes\n"
        "k32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]\n"
        "k32.CreateMutexW.restype = wintypes.HANDLE\n"
        "k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]\n"
        "k32.WaitForSingleObject.restype = wintypes.DWORD\n"
        f"h = k32.CreateMutexW(None, False, {name!r})\n"
        f"rc = k32.WaitForSingleObject(h, {wait_ms})\n"
        "print(f'ACQ:{rc}', flush=True)\n"
        "os._exit(0)\n"   # ReleaseMutex せず即死 = abandoned
    )


def test_mutex_name_contains_project_hash():
    name = auto_buy._mutex_name()
    assert name.startswith("Global\\AutoRacingAI_auto_buy_")
    assert not name.endswith("_auto_buy_")   # ハッシュが空でない
    # 統合管理システムの mutex 名前空間と衝突しない
    assert "PublicRaceMgmt" not in name


def test_thread_exclusion_and_release():
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    name = _test_name()
    results: dict[str, bool] = {}

    def _try(key: str) -> None:
        lk = auto_buy._acquire_lock(wait_sec=0, name=name)
        results[key] = lk is not None
        if lk is not None:
            auto_buy._release_lock(lk)

    lock = auto_buy._acquire_lock(wait_sec=5, name=name)
    assert lock is not None, "初回取得が成功する"
    try:
        # 保持中: 別スレッド (所有はスレッド単位) の取得は None
        t = threading.Thread(target=_try, args=("held",))
        t.start(); t.join(timeout=15)
        assert results.get("held") is False, "保持中の取得は None (skip_lock 相当)"
    finally:
        auto_buy._release_lock(lock)
    # 解放後: 別スレッドから取得できる (同一スレッドは再入可能なので証明にならない)
    t2 = threading.Thread(target=_try, args=("after",))
    t2.start(); t2.join(timeout=15)
    assert results.get("after") is True, "解放後は別スレッドから取得できる"


def test_abandoned_mutex_recovery():
    """子プロセスが取得したまま即死 → 親が abandoned を引き継いで取得できる
    (旧ファイルロックの「10分待たないと回復しない」問題も同時に解消)。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    name = _test_name()
    # 親が未所有ハンドルでオブジェクトを生存させる → 子の死で abandoned 化
    k32 = auto_buy._kernel32()
    keepalive = k32.CreateMutexW(None, False, name)
    assert keepalive, "keepalive ハンドル作成"
    try:
        proc = subprocess.run([sys.executable, "-c", _child_code(name, 5000)],
                              timeout=30, capture_output=True, text=True)
        assert proc.returncode == 0 and "ACQ:0" in proc.stdout, \
            f"子プロセスが mutex を実取得した (stdout={proc.stdout.strip()!r})"
        # abandoned を引き継いで取得できる (None にならない・ブロックしない)
        lock = auto_buy._acquire_lock(wait_sec=10, name=name)
        assert lock is not None, "abandoned mutex を引き継いで取得できる"
        auto_buy._release_lock(lock)
    finally:
        k32.CloseHandle(keepalive)


def test_interprocess_exclusion():
    """親が保持している間、子プロセスの取得は WAIT_TIMEOUT (0x102=258)。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    name = _test_name()
    lock = auto_buy._acquire_lock(wait_sec=5, name=name)
    assert lock is not None
    try:
        proc = subprocess.run([sys.executable, "-c", _child_code(name, 500)],
                              timeout=30, capture_output=True, text=True)
        assert proc.returncode == 0 and "ACQ:258" in proc.stdout, \
            f"親保持中の子取得は WAIT_TIMEOUT (stdout={proc.stdout.strip()!r})"
    finally:
        auto_buy._release_lock(lock)


def test_run_auto_buy_skip_lock_and_locked_path():
    """run_auto_buy 統合: 取得不能 → skip_lock verdict (発注経路に入らない) /
    取得成功 → ロック下で本体 (stub) を呼び、終了後にロックが解放されている。"""
    if not IS_WINDOWS:
        print("  (skip: Windows 専用)")
        return
    name = _test_name()
    candidates = [{"race_date": "2099-01-01", "place_code": 4, "venue": "hamamatsu",
                   "venue_jp": "浜松", "race_no": 7, "car_no": 5, "ev": 2.0,
                   "bets": [{"type": "fns", "cars": [5], "amount": 100}],
                   "amount": 100}]
    calls: list = []
    orig = (auto_buy.AUTO_BUY_ENABLED, auto_buy.LOCK_WAIT_SEC,
            auto_buy._mutex_name, auto_buy._run_auto_buy_locked)
    try:
        auto_buy.AUTO_BUY_ENABLED = True
        auto_buy.LOCK_WAIT_SEC = 0            # 待たずに即 skip 判定
        auto_buy._mutex_name = lambda: name   # テスト専用名を注入
        auto_buy._run_auto_buy_locked = (
            lambda c, n, d: calls.append(len(c)) or [{"verdict": "stub_called"}])

        # ① 別スレッドが保持 → skip_lock verdict、本体 (stub) は呼ばれない
        holding = threading.Event()
        release = threading.Event()

        def _holder() -> None:
            lk = auto_buy._acquire_lock(wait_sec=5, name=name)
            holding.set()
            try:
                release.wait(timeout=30)
            finally:
                if lk is not None:
                    auto_buy._release_lock(lk)

        t = threading.Thread(target=_holder)
        t.start()
        assert holding.wait(timeout=15), "holder スレッドが取得した"
        try:
            out = auto_buy.run_auto_buy(candidates)
            assert [r["verdict"] for r in out] == ["skip_lock"], out
            assert out[0]["amount"] == 100
            assert calls == [], "skip_lock 時は発注経路 (本体) に入らない"
        finally:
            release.set()
            t.join(timeout=15)

        # ② 解放後: 取得成功 → 本体 (stub) がロック下で 1 回呼ばれる
        out2 = auto_buy.run_auto_buy(candidates)
        assert out2 == [{"verdict": "stub_called"}]
        assert calls == [1]

        # ③ 終了後にロックが解放されている (別スレッドから取得できる)
        got: dict[str, bool] = {}

        def _try_after() -> None:
            lk = auto_buy._acquire_lock(wait_sec=0, name=name)
            got["ok"] = lk is not None
            if lk is not None:
                auto_buy._release_lock(lk)

        t2 = threading.Thread(target=_try_after)
        t2.start(); t2.join(timeout=15)
        assert got.get("ok") is True, "run_auto_buy 終了後にロックは解放済み"
    finally:
        (auto_buy.AUTO_BUY_ENABLED, auto_buy.LOCK_WAIT_SEC,
         auto_buy._mutex_name, auto_buy._run_auto_buy_locked) = orig


def test_no_lock_file_artifacts():
    """ファイル削除型ロックの残骸 (LOCK_FILE / stale 破棄) が全廃されている。"""
    src = Path(auto_buy.__file__).read_text(encoding="utf-8")
    assert "auto_buy.lock" not in src
    assert "LOCK_STALE_SEC" not in src
    assert "unlink" not in src.split("def _release_lock")[1].split("def ")[0], \
        "_release_lock にファイル削除が残っていない"


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
