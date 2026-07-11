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
