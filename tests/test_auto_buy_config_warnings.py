"""当日上限と当日損失停止の設定ずれ警告 (2026-11-12 監査 指摘8) のテスト。

判定ロジックは変えていない (警告ログのみ)。実発注・メールは行わない。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_buy  # noqa: E402


def test_default_values_no_warning():
    # 既定 (上限 ¥3,000 / 損失停止 −¥3,000) では警告なし
    assert auto_buy.config_warnings(3000, -3000) == []
    assert auto_buy.config_warnings(2000, -3000) == []


def test_cap_above_loss_stop_warns():
    w = auto_buy.config_warnings(5000, -3000)
    assert len(w) == 1 and "¥5,000" in w[0] and "発動しない" in w[0]


def test_run_auto_buy_logs_warning_without_ordering(monkeypatch, caplog):
    """起動時に警告ログを出す (メールは出さない)。発注経路には入らない。"""
    monkeypatch.setattr(auto_buy, "AUTO_BUY_ENABLED", True)
    monkeypatch.setattr(auto_buy, "config_warnings", lambda: ["上限が閾値超え"])
    # 以降は発注結果不明フラグ経路で即 return させる (発注・ロックに入らない)
    monkeypatch.setattr(auto_buy, "abandoned_stop_active", lambda: True)
    monkeypatch.setattr(auto_buy, "_alert_abandoned_pending_once", lambda now, n: None)
    sent = []
    monkeypatch.setattr(auto_buy, "_notify", lambda s, b: sent.append(s))
    cand = {"venue": "iizuka", "race_no": 7, "amount": 100,
            "bets": [{"type": "fns", "cars": [5], "amount": 100}]}
    with caplog.at_level(logging.WARNING, logger="auto_buy"):
        out = auto_buy.run_auto_buy([cand])
    assert [r["verdict"] for r in out] == ["skip_abandoned_pending"]
    assert any("設定警告" in r.getMessage() for r in caplog.records)
    assert sent == []
