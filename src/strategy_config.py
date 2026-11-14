"""三連系 購入ポリシーの正本 (single source of truth, 2026-07-11)。

背景 (監査 P1-3): 購入対象 (daily_predict) と停止監視 (weekly_status) が別々に
venue リストをハードコードしていたため、6/9 の RF3 4 場拡大時に監視側の更新が
漏れ、飯塚 RF3 の 17 連敗が kill-switch の対象外で進行した。

対策: 「場ごとに購入可能な券種」をここ 1 箇所で定義し、daily_predict も
weekly_status もこの集合から対象を導出する。購入対象を変えれば監視対象も
自動追従するため、スコープのドリフトが構造的に起きない。

kill-switch は「現役ポリシーの健全性」を判定する (選択肢 B)。
廃止した場の過去損失は全体 P&L / 資金管理には残るが、現役ポリシーの
ROI・n・停止基準には持ち込まない (過去の廃止判断を現在の停止条件に混ぜない)。
"""

from __future__ import annotations

# 場コード: 2=川口, 3=伊勢崎, 4=浜松, 5=飯塚, 6=山陽
# 場ごとに購入可能な三連系券種 (これが正本)。
#   2026-06-09: rt2_rf4 policy 導入 (RT3=浜松・山陽, RF3=伊勢崎・浜松・飯塚・山陽)
#   2026-07-11: 飯塚(5) を RF3 から除外 (実弾 0/17, sim でも 4 場中最弱)
THREE_POINT_POLICY: dict[int, frozenset[str]] = {
    3: frozenset({"rf3"}),          # 伊勢崎: 三連複
    4: frozenset({"rt3", "rf3"}),   # 浜松:   三連単 + 三連複
    6: frozenset({"rt3", "rf3"}),   # 山陽:   三連単 + 三連複
    # 5 (飯塚): 2026-07-11 除外。2 (川口): 三連系対象外。
}


def places_for(bet_type: str) -> frozenset[int]:
    """指定券種を購入する場コード集合。"""
    return frozenset(pc for pc, bts in THREE_POINT_POLICY.items()
                     if bet_type in bts)


# 派生集合 (daily_predict / weekly_status が参照)
RT3_ELIGIBLE_PLACES: frozenset[int] = places_for("rt3")   # {4, 6}
RF3_ELIGIBLE_PLACES: frozenset[int] = places_for("rf3")   # {3, 4, 6}
# 三連系を購入する全場 = kill-switch の監視スコープ (購入 ⊆ 監視 を保証)
THREE_POINT_BUY_PLACES: frozenset[int] = frozenset(THREE_POINT_POLICY.keys())

# 現役ポリシーの正確な (place_code, bet_type_code) ペア集合 (2026-07-11 Codex 追加指摘)。
# kill-switch の現役集計はこのペアで厳密に絞る。「現役場にある rt3/rf3 全て」を
# 数えると、廃止済みの組 (例: 伊勢崎 rt3) の過去損益が現役損益に混入するため。
THREE_POINT_POLICY_PAIRS: frozenset[tuple[int, str]] = frozenset(
    (pc, bt) for pc, bts in THREE_POINT_POLICY.items() for bt in bts)

# ── 三連系 全場・全期間 絶対損失バックストップ (2026-07-11 監査 P2 + Codex 承認) ──
# kill-switch (選択肢 B) は現役ポリシーのみを監視するため、負けた場を廃止するたびに
# 損失基盤がリセットされる (ラチェット構造)。その抜け穴を塞ぐ常設の安全弁として、
# 場の廃止/追加に関係なく「全場・全期間」の三連系 (rt3+rf3) 累積損益がこの値を
# 下回ったら三連系購入を停止する (実装: src/backstop.py、sticky フラグ方式)。
# ポリシー変更 (THREE_POINT_POLICY の場の廃止/追加) ではリセットされない。
THREE_POINT_BACKSTOP_LOSS_YEN: int = -10_000


# ── 発火時 EV の drift 割引係数 (案A、2026-09-06 導入) ──────────────────────────
# 発走 -4 分の複勝オッズは締切までの late money で本命側に縮む (=発火時 EV は
# 系統的に過大)。odds_ts の 7 時点スナップ (7/5〜9/5, 最人気車 n=1331) で
# ratio_med = odds(-4min)/odds(確定) = 1.0938、>1.0 率 59.4% (8/8 再測定 60.1% と不動)。
# 1/ratio_med を発火時 EV に掛けて「確定 EV の中央値推定」に補正する。
# 実効閾値: 複勝 1.50 → 1.64 相当、三連系 1.80 → 1.97 相当。
# 実績検証 (shadow_picks × payouts 複勝): 補正で落ちる 1.50-1.64 帯は ROI 89%、
# 補正後 ROI 98.8% → 101.4% (n=242→191)。効果は +2〜3pt の見込み (控除率の壁は越えない)。
# 再測定: scripts/ev_drift_curve.py --thr 1.50 --snapshots (レポート reports/ev_drift_curve_*.md)。
# 1.0 にすれば旧挙動 (割引なし) に戻る。
FIRE_EV_DISCOUNT: float = 0.9143


# ── kill-switch (停止条件①②) の評価起点 = 再開基準日 (2026-11-03 導入) ──────────────
# 停止フラグ (data/rt3_stop.flag) を人間が削除して再開する時、①② を全期間の累計で
# 評価し続けると「再開した瞬間に過去の損失で即再停止」になる (2026-09-28 停止時の
# 現役累計 -¥5,890 は再開後も ≤ -¥5,000 のまま)。再開時はこの日付を更新し、
# ①② は「この日以降の購入」だけで評価する (再開後の新しい損失で再び止まる)。
# ⑤ 全場・全期間バックストップ (-¥10,000, src/backstop.py) はこの日付と無関係に
# 全期間で評価し続ける (再開を繰り返しても損失が無制限に積み上がらない安全弁)。
# None なら従来どおり全期間で評価。
#   2026-11-03: 9/28 の②発動 (現役累計 -¥5,890) からユーザー判断で再開。
THREE_POINT_KILLSWITCH_SINCE: str | None = "2026-11-03"


# ── 試走入り本番モデル (第 2 系統) の切替 (2026-11-14) ─────────────────────────────
# True: 発火時に R の試走が全車揃えば試走入り系統 (data/production_trial_*, ml/production_trial.py) の
#       pred / pred_calib を使う。揃わない・モデル無し/破損・例外の R は試走なし (data/production_*) に戻る。
# False: 従来どおり試走なしだけを使う。戻すときはここを False にするだけ (次の発火から即反映)。
# EV しきい値・券種・上限・停止条件は系統と無関係に共通。
# 2026-11-14 16:1x に True へ (利用者指示「オートは試走も入れて」)。根拠: 同じ比較窓 2,007 R で
#   3 着内 logloss 0.5090 → 0.5003、発火時点で試走が全車揃う R は 99.5%、候補数の見込みは 312 → 297。
PRODUCTION_USE_TRIAL: bool = True
