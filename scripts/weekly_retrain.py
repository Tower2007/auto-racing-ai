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
LOG_FILE = ROOT / "data" / "weekly_retrain.log"

# (module, 説明, タイムアウト秒)。順序が依存関係。
STEPS = [
    ("ml.features", "特徴量生成 (6 CSV -> ml_features.parquet)", 900),
    ("ml.walkforward_morning", "walk-forward 校正 OOF 再生成", 5400),
    ("ml.train_production", "本番モデル学習 + 品質ゲート", 1800),
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

    _log("=== weekly_retrain done (3 ステップ完走) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
