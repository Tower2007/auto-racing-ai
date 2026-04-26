"""API JSON レスポンス → CSV 用フラット dict への変換

全角→半角正規化、NULL 正規化を含む。
"""

import unicodedata
from typing import Any


def _zen2han(s: str | None) -> str | None:
    """全角英数字を半角に変換。"""
    if s is None:
        return None
    return unicodedata.normalize("NFKC", s)


def _clean_str(v: Any) -> str | None:
    """空文字・None を None に統一。"""
    if v is None or v == "" or v == "null":
        return None
    return _zen2han(str(v).strip())


def _clean_float(v: Any) -> float | None:
    """数値文字列を float に。変換不可は None。"""
    s = _clean_str(v)
    if s is None:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _clean_int(v: Any) -> int | None:
    """数値を int に。変換不可は None。"""
    if v is None or v == "" or v == "null":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


# ─── Program (出走表) ──────────────────────────────────

def parse_program_entries(
    place_code: int, race_date: str, race_no: int, body: dict,
) -> list[dict]:
    """Program API body → race_entries 行リスト。"""
    rows = []
    for p in body.get("playerList", []):
        rows.append({
            "race_date": race_date,
            "place_code": place_code,
            "race_no": race_no,
            "car_no": p["carNo"],
            "player_code": _clean_str(p.get("playerCode")),
            "player_name": _clean_str(p.get("playerName")),
            "player_place_code": _clean_int(p.get("placeCode")),
            "player_place_name": _clean_str(p.get("placeName")),
            "graduation_code": _clean_str(p.get("graduationCode")),
            "age": _clean_int(p.get("age")),
            "bike_class": _clean_int(p.get("bikeClass")),
            "bike_name": _clean_str(p.get("bikeName")),
            "rank": _clean_str(p.get("rank")),
            "handicap": _clean_int(p.get("handicap")),
            "trial_run_time": _clean_float(p.get("trialRunTime")),
            "trial_retry_code": _clean_str(p.get("trialRetryCode")),
            "absent": _clean_str(p.get("absent")),
            "sunny_expect_code": _clean_str(p.get("sunnyExpectCode")),
            "rain_expect_code": _clean_str(p.get("rainExpectCode")),
            "race_dev": _clean_str(p.get("raceDev")),
            "rate2": _clean_float(p.get("rate2")),
            "rate3": _clean_float(p.get("rate3")),
        })
    return rows


def parse_program_stats(
    place_code: int, race_date: str, race_no: int, body: dict,
) -> list[dict]:
    """Program API body → 選手集計成績(90日/180日/通算)行リスト。"""
    rows = []
    latest90 = body.get("latest90List", {})
    latest180 = body.get("latest180List", {})
    win_list = body.get("winList", {})

    for p in body.get("playerList", []):
        pc = p.get("playerCode", "")
        s90 = latest90.get(pc, {})
        s180 = latest180.get(pc, {})
        win = win_list.get(pc, {})
        l10 = s90.get("latest10OrderCount", {})
        gt_odds = s180.get("goodTrackOdds", {})
        wt_odds = s180.get("wetTrackOdds", {})

        rows.append({
            "race_date": race_date,
            "place_code": place_code,
            "race_no": race_no,
            "car_no": p["carNo"],
            "player_code": _clean_str(pc),
            # 90日集計
            "run_count_90d": _clean_int(s90.get("runCount")),
            "advance_final_count_90d": _clean_int(s90.get("advanceFinalCount")),
            "win_count_90d": _clean_int(s90.get("winCount")),
            "st_ave_90d": _clean_float(s90.get("stAve")),
            "order1_count_90d": _clean_int(l10.get("1")),
            "order2_count_90d": _clean_int(l10.get("2")),
            "order3_count_90d": _clean_int(l10.get("3")),
            "order_other_count_90d": _clean_int(l10.get("ohter")),  # API typo
            "good_track_trial_ave": _clean_float(s90.get("goodTrackTraialAve")),
            "good_track_race_ave": _clean_float(s90.get("goodTrackRaceAve")),
            "good_track_race_best": _clean_float(s90.get("goodTrackRaceBest")),
            "good_track_race_best_place": _clean_str(s90.get("goodTrackRaceBestPlace")),
            # 180日集計
            "good_track_rate2_180d": _clean_float(gt_odds.get("rate2")),
            "good_track_run_count_180d": _clean_int(gt_odds.get("runCount")),
            "wet_track_rate2_180d": _clean_float(wt_odds.get("rate2")),
            "wet_track_run_count_180d": _clean_int(wt_odds.get("runCount")),
            # 通算
            "this_year_win_count": _clean_int(win.get("thisYearWinCount")),
            "this_year_advance_final": _clean_int(win.get("thisYearAdvanceFinalCount")),
            "total_win_count": _clean_int(win.get("totalWinCount")),
            "win_rate1": _clean_float(win.get("rate1")),
            "win_rate2": _clean_float(win.get("rate2")),
            "win_rate3": _clean_float(win.get("rate3")),
        })
    return rows


# ─── RaceResult (結果) ─────────────────────────────────

def parse_race_results(
    place_code: int, race_date: str, race_no: int, body: dict,
) -> list[dict]:
    """RaceResult API body → race_results 行リスト。"""
    rows = []
    for r in body.get("raceResult", []):
        order = _clean_int(r.get("order"))
        # 失格系 (order >= 9) は着順 NULL
        if order is not None and order >= 9:
            order = None

        rows.append({
            "race_date": race_date,
            "place_code": place_code,
            "race_no": race_no,
            "car_no": r["carNo"],
            "order": order,
            "accident_code": _clean_int(r.get("accidentCode")),
            "accident_name": _clean_str(r.get("accidentName")),
            "player_code": _clean_str(r.get("playerCode")),
            "player_name": _clean_str(r.get("playerName")),
            "motorcycle_name": _clean_str(r.get("motorcycleName")),
            "handicap": _clean_int(r.get("handicap")),
            "trial_time": _clean_float(r.get("traialTime")),
            "race_time": _clean_float(r.get("raceTime")),
            "st": _clean_float(r.get("st")),
            "foul_code": _clean_str(r.get("foulCode")),
        })
    return rows


def parse_race_laps(
    place_code: int, race_date: str, race_no: int, body: dict,
) -> list[dict]:
    """RaceResult API body → race_laps 行リスト (周回ランク変動)。"""
    rows = []
    for lap in body.get("grandNoteList", []):
        lap_no = lap.get("lapNo")
        for entry in lap.get("rankList", []):
            rows.append({
                "race_date": race_date,
                "place_code": place_code,
                "race_no": race_no,
                "lap_no": lap_no,
                "car_no": _clean_int(entry.get("carNo")),
                "rank": entry.get("rank"),
            })
    return rows


# ─── RaceRefund (払戻) ─────────────────────────────────

_BET_TYPE_MAP = {
    "tns": "単勝",
    "fns": "複勝",
    "rtw": "2連単",
    "rfw": "2連複",
    "wid": "ワイド",
    "rt3": "3連単",
    "rf3": "3連複",
}


def parse_payouts(place_code: int, race_date: str, refund_body: list) -> list[dict]:
    """RaceRefund API body → payouts 行リスト (1日分全レース)。"""
    rows = []
    for race in refund_body:
        race_no = race.get("raceNo")
        info = race.get("refundInfo", {})

        for bet_key, bet_name in _BET_TYPE_MAP.items():
            section = info.get(bet_key, {})
            for item in section.get("list", []):
                car1 = item.get("1thCarNo") or item.get("carNo")
                car2 = item.get("2thCarNo")
                car3 = item.get("3thCarNo")

                rows.append({
                    "race_date": race_date,
                    "place_code": place_code,
                    "race_no": race_no,
                    "bet_type": bet_key,
                    "bet_name": bet_name,
                    "car_no_1": _clean_int(car1),
                    "car_no_2": _clean_int(car2),
                    "car_no_3": _clean_int(car3),
                    "refund": _clean_int(item.get("refund")),
                    "pop": _clean_int(item.get("pop")),
                    "refund_votes": _clean_str(item.get("refundVotes")),
                })
    return rows


# ─── Odds (オッズ要約) ─────────────────────────────────

def parse_odds_summary(
    place_code: int, race_date: str, race_no: int, body: dict,
) -> list[dict]:
    """Odds API body → odds_summary 行リスト (1車ごとの単勝/複勝オッズ)。"""
    rows = []
    tns = body.get("tnsOddsList", {})
    fns = body.get("fnsOddsList", {})

    for p in body.get("playerList", []):
        car_str = str(p["carNo"])
        fns_entry = fns.get(car_str, {})

        rows.append({
            "race_date": race_date,
            "place_code": place_code,
            "race_no": race_no,
            "car_no": p["carNo"],
            "player_code": _clean_str(p.get("playerCode")),
            "win_odds": _clean_float(tns.get(car_str)),
            "place_odds_min": _clean_float(fns_entry.get("min") if isinstance(fns_entry, dict) else None),
            "place_odds_max": _clean_float(fns_entry.get("max") if isinstance(fns_entry, dict) else None),
            "st_ave": _clean_float(p.get("stAve")),
            "good_track_trial_ave": _clean_float(p.get("goodTrackTraialAve")),
            "good_track_race_ave": _clean_float(p.get("goodTrackRaceAve")),
            "good_track_race_best": _clean_float(p.get("goodTrackRaceBest")),
            "ai_expect_code": _clean_int(p.get("aiExpectCode")),
        })
    return rows
