"""週次再学習チェーン wrapper (2026-06-14)。

背景 (2026-06-14 診断):
  AutoraceWeeklyRetrain は `python -m ml.train_production` だけを実行して
  いたが、train_production は ml_features.parquet と
  walkforward_predictions_morning_top3.parquet を **読むだけで再生成しない**。
  これらは別ステップ (ml/features.py / ml/walkforward_morning.py) で、
  再学習チェーンに含まれていなかった。結果、特徴量 parquet が 2026-05-14、
  校正 parquet が 2026-04-28 で凍結し、5/24〜6/14 の 4 回の再学習が
  すべて同一データ (valid_auc 0.823355 が 6 桁完全一致) を食って no-op 化、
  本番モデルは 2026-04-29 vintage のまま塩漬けになっていた。

修正 (本 wrapper):
  再学習の前に学習入力を必ず再生成する。順序:
    1. ml.features            → data/ml_features.parquet (学習データ)
    2. ml.walkforward_morning → walkforward_predictions_morning_top3.parquet
                                (isotonic 校正の OOF 入力)
    3. ml.train_production    → 学習 + 品質ゲート判定 (NG なら旧モデル維持)

  上流 (1/2) が失敗したら **学習に進まず中断** する。古い/壊れた parquet で
  学習して塩漬けモデルを上書きするリスクを避けるため。失敗時のみ Gmail 通知。

  ※ 品質ゲート自体の永久凍結問題 (凍結した高値 AUC を恒久ベースラインに
     する設計) は本 wrapper の対象外。別途 train_production 側で対応する。

使い方:
  python scripts/weekly_retrain.py            # 通常 (ゲート判定あり)
  python scripts/weekly_retrain.py --force    # train_production に渡す (強制採用)
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# タスクは `python scripts/weekly_retrain.py` (作業フォルダ指定なし) で起動されるため、sys.path[0] は
# scripts/ になり、ルート直下の gmail_notify / src が import できず通知が無音で失敗していた (2026-11-15)。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LOG_FILE = ROOT / "data" / "weekly_retrain.log"

# 試走入り本番モデルの学習ステップ。PRODUCTION_USE_TRIAL=True (実弾の予測がこの系統) の間は、
# 失敗を必須ステップと同じく通知し終了コードを非ゼロにする (試走なし系統の更新は止めない)。
TRIAL_MODULE = "ml.production_trial"

# (module, 説明, タイムアウト秒)。順序が依存関係。
STEPS = [
    ("ml.features", "特徴量生成 (6 CSV -> ml_features.parquet)", 900),
    ("ml.walkforward_morning", "walk-forward 校正 OOF 再生成", 5400),
    ("ml.train_production", "本番モデル学習 + 品質ゲート", 1800),
]

# 任意ステップ (2026-11-12): 本番の後に走らせるシャドー。失敗しても本番の再学習・採用には
# 影響させない (ログだけ残し、終了コードも通知も変えない)。例外: ml.production_trial は
# PRODUCTION_USE_TRIAL=True の間は失敗を通知し終了コードを 1 にする (2026-11-15、TRIAL_MODULE 参照)。
OPTIONAL_STEPS = [
    # 試走入り本番モデル (第 2 系統, 2026-11-14)。walk-forward → 学習 → 校正 → 採用ゲート v3 (自系統の現役と比較)。
    # 失敗しても試走なし系統 (上の必須 3 ステップ = フォールバック) の学習・採用には影響しない。
    ("ml.production_trial", "試走入り本番モデル (第 2 系統) の walk-forward・学習・採用", 5400),
    ("ml.shadow_trial", "試走ありシャドーモデルの学習 (記録のみ・購入判断に不使用)", 1800),
    ("ml.shadow_full", "全期間シャドー 2 本の学習 (記録のみ・購入判断に不使用)", 1800),
]


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _notify_failure(step: str, detail: str) -> None:
    try:
        from gmail_notify import send_email
        send_email(
            subject=f"[autorace] 週次再学習 失敗: {step}",
            body=(f"週次再学習チェーンが {step} で失敗しました。\n\n"
                  f"本番モデルは更新されていません (旧モデル維持)。\n\n"
                  f"詳細(末尾):\n{detail[-1500:]}"),
        )
    except Exception as e:
        _log(f"[notify] Gmail 通知失敗: {e}")


def _trial_is_production() -> bool:
    """src/strategy_config.PRODUCTION_USE_TRIAL。読めなければ True (失敗を無音にしない安全側)。"""
    try:
        from src import strategy_config as sc
        return bool(getattr(sc, "PRODUCTION_USE_TRIAL", False))
    except Exception as e:  # noqa: BLE001
        _log(f"[WARN] PRODUCTION_USE_TRIAL を読めない ({type(e).__name__}: {e}) — 本番扱いで続行")
        return True


def _notify_trial_failure(detail: str) -> None:
    try:
        from gmail_notify import send_email
        send_email(
            subject=f"[autorace] 週次再学習 失敗: 試走入りモデルの再学習 失敗 ({TRIAL_MODULE})",
            body=("試走入り本番モデル (PRODUCTION_USE_TRIAL=True で実弾の予測に使用中) の週次再学習が"
                  "失敗しました。\n\n"
                  "実弾の予測は前回採用の試走入りモデルのまま残ります。学習日から一定日数 "
                  "(ml/production_trial.TRIAL_MAX_AGE_DAYS) を過ぎると、予測側で試走なしモデルへ"
                  "自動フォールバックします。試走なし系統 (必須ステップ) の再学習は完了しています。\n\n"
                  f"詳細(末尾):\n{detail[-1500:]}"),
        )
    except Exception as e:  # noqa: BLE001
        _log(f"[notify] Gmail 通知失敗: {e}")


def run_optional_step(module: str, desc: str, timeout: int) -> tuple[bool, str]:
    """任意ステップ。(成功, 失敗詳細) を返す。ここでは通知しない (呼び出し側が試走入り本番時だけ通知)。"""
    _log(f"[start] {module} — {desc} (任意)")
    try:
        r = subprocess.run(
            [sys.executable, "-m", module], cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        _log(f"[WARN] {module} 失敗: {detail}")
        return False, detail
    if r.returncode != 0:
        tail = (r.stdout or "")[-600:] + (r.stderr or "")[-600:]
        _log(f"[WARN] {module} exit={r.returncode}\n{tail}")
        return False, f"exit={r.returncode}\n{tail}"
    _log(f"[ok] {module} done (任意)")
    return True, ""


def run_step(module: str, desc: str, timeout: int, extra_args: list[str]) -> bool:
    """1 ステップ実行。成功で True。train_production のみ追加引数を渡す。"""
    cmd = [sys.executable, "-m", module]
    if module == "ml.train_production":
        cmd += extra_args
    _log(f"[start] {module} — {desc}")
    try:
        r = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _log(f"[FAIL] {module} timeout >{timeout}s")
        _notify_failure(module, f"timeout >{timeout}s")
        return False
    tail = (r.stdout or "")[-600:] + (r.stderr or "")[-600:]
    if r.returncode != 0:
        _log(f"[FAIL] {module} exit={r.returncode}\n{tail}")
        _notify_failure(module, tail)
        return False
    _log(f"[ok] {module} done")
    return True


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    extra_args = sys.argv[1:]  # --force 等は train_production に渡す
    _log("=== weekly_retrain start ===")

    for module, desc, timeout in STEPS:
        ok = run_step(module, desc, timeout, extra_args)
        if not ok:
            _log(f"=== weekly_retrain ABORTED at {module} "
                 f"(本番モデルは未更新) ===")
            return 1

    rc = 0
    for module, desc, timeout in OPTIONAL_STEPS:
        ok, detail = run_optional_step(module, desc, timeout)
        # 試走入りが本番の間は、その学習失敗 (ゲート不採用は exit 0 なので含まない) を通知 + rc=1。
        # 残りの任意ステップは続ける。
        if not ok and module == TRIAL_MODULE and _trial_is_production():
            _log(f"[FAIL] {module} (PRODUCTION_USE_TRIAL=True — 実弾の予測モデルが未更新)")
            _notify_trial_failure(detail)
            rc = 1

    if rc:
        _log("=== weekly_retrain done (必須 3 ステップ完走、試走入り本番モデルの再学習 失敗) ===")
    else:
        _log("=== weekly_retrain done (3 ステップ完走) ===")
    return rc


if __name__ == "__main__":
    sys.exit(main())
