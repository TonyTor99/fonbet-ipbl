"""Сборщик рынков Prime (муж): раз в игровую минуту пишет крайнюю линию каждого
рынка полного матча в отдельную БД, на финале проставляет результат каждого исхода.

Точки входа:
  process(state, api_data) — вызывать каждый цикл парсера для нужного матча;
  resolve(event_id, s1, s2) — на финале матча.
"""
import logging
from datetime import datetime, timezone, timedelta

import collector_db
from config import (FORA1_FIDS, FORA2_FIDS, TOTAL_B_FIDS, TOTAL_M_FIDS,
                    IT1_B_FIDS, IT1_M_FIDS, IT2_B_FIDS, IT2_M_FIDS,
                    WIN1_FID, WIN2_FID)

log = logging.getLogger("collector")
MSK = timezone(timedelta(hours=3))

# event_id -> последняя записанная игровая минута (дедуп в памяти)
_last_minute: dict[int, int] = {}


# --- разбор факторов -------------------------------------------------------

def _root_factors(api_data, event_id) -> list[dict]:
    if not api_data:
        return []
    for cf in api_data.get("customFactors", []):
        if cf.get("e") == event_id:
            return cf.get("factors", [])
    return []


def _by_line(factors, fids) -> dict[float, float]:
    """{линия: кф} по факторам семейства. Линия — числовое поле p (×100),
    т.к. pt приходит строкой ('+6.5', '-7.5')."""
    out = {}
    for f in factors:
        if f.get("f") in fids and f.get("p") is not None and f.get("v") is not None:
            out[round(f["p"] / 100.0, 1)] = f["v"]
    return out


def _odds(factors, fid):
    for f in factors:
        if f.get("f") == fid:
            return f.get("v")
    return None


def extract_markets(factors: list[dict]) -> dict:
    """Крайняя (верхняя) линия каждого рынка:
       тоталы/инд.тоталы — минимальная линия; фора — самая отрицательная (К1)."""
    m = {
        "fora_line": None, "fora1_odds": None, "fora2_odds": None,
        "total_line": None, "total_b_odds": None, "total_m_odds": None,
        "it1_line": None, "it1_b_odds": None, "it1_m_odds": None,
        "it2_line": None, "it2_b_odds": None, "it2_m_odds": None,
        "win1_odds": None, "win2_odds": None,
    }

    # Фора: крайняя = самая отрицательная линия К1; парный К2 на -line
    f1 = _by_line(factors, FORA1_FIDS)
    f2 = _by_line(factors, FORA2_FIDS)
    if f1:
        line = min(f1)                       # напр. -7.5
        m["fora_line"] = line
        m["fora1_odds"] = f1.get(line)
        m["fora2_odds"] = f2.get(-line)

    # Тотал / инд.тоталы: крайняя = минимальная линия, где есть обе стороны
    for pref, b_fids, m_fids in (
        ("total", TOTAL_B_FIDS, TOTAL_M_FIDS),
        ("it1", IT1_B_FIDS, IT1_M_FIDS),
        ("it2", IT2_B_FIDS, IT2_M_FIDS),
    ):
        bb = _by_line(factors, b_fids)
        mm = _by_line(factors, m_fids)
        both = sorted(set(bb) & set(mm))
        if both:
            line = both[0]
            m[f"{pref}_line"] = line
            m[f"{pref}_b_odds"] = bb.get(line)
            m[f"{pref}_m_odds"] = mm.get(line)

    # Победа
    m["win1_odds"] = _odds(factors, WIN1_FID)
    m["win2_odds"] = _odds(factors, WIN2_FID)
    return m


# --- запись по игровой минуте ----------------------------------------------

def process(state: dict, api_data):
    """Пишет снимок, если игровая минута сменилась (≤1 строка в игр. минуту)."""
    eid = state["event_id"]
    ts = state.get("ts") or 0
    minute = ts // 60

    if _last_minute.get(eid) == minute:
        return
    if collector_db.snapshot_exists(eid, minute):
        _last_minute[eid] = minute
        return

    factors = _root_factors(api_data, eid)
    if not factors:
        return  # нет рынков в этом цикле — попробуем на следующем

    markets = extract_markets(factors)
    quarters = state.get("quarters") or []
    row = {
        "event_id": eid,
        "league": state["league"],
        "team1": state.get("team1") or "?",
        "team2": state.get("team2") or "?",
        "snap_dt_msk": datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"),
        "game_minute": minute,
        "quarter": len(quarters) if quarters else None,
        "score1": state["score1"],
        "score2": state["score2"],
        "created_at": datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"),
        **markets,
    }
    sid = collector_db.insert_snapshot(row)
    _last_minute[eid] = minute
    if sid is not None:
        log.info("collect ev=%s min=%s score=%s:%s tot=%s fora=%s",
                 eid, minute, row["score1"], row["score2"],
                 markets["total_line"], markets["fora_line"])


# --- дорасчёт результата на финале -----------------------------------------

def _wl(win: bool) -> str:
    return "Выигрыш" if win else "Проигрыш"


def _resolve_row(r: dict, s1: int, s2: int) -> dict:
    res = {}
    # Победа
    if r.get("win1_odds") is not None or r.get("win2_odds") is not None:
        res["r_win1"] = _wl(s1 > s2)
        res["r_win2"] = _wl(s2 > s1)
    # Фора (линия со стороны К1, напр. -7.5): К1 проходит если s1 + line > s2
    fl = r.get("fora_line")
    if fl is not None:
        c1 = (s1 + fl) > s2
        res["r_fora1"] = _wl(c1)
        res["r_fora2"] = _wl(not c1)   # линии .5 → без пуша
    # Тотал
    tl = r.get("total_line")
    if tl is not None:
        over = (s1 + s2) > tl
        res["r_total_b"] = _wl(over)
        res["r_total_m"] = _wl(not over)
    # Инд. тотал К1
    i1 = r.get("it1_line")
    if i1 is not None:
        over = s1 > i1
        res["r_it1_b"] = _wl(over)
        res["r_it1_m"] = _wl(not over)
    # Инд. тотал К2
    i2 = r.get("it2_line")
    if i2 is not None:
        over = s2 > i2
        res["r_it2_b"] = _wl(over)
        res["r_it2_m"] = _wl(not over)
    return res


def resolve(event_id: int, s1: int, s2: int):
    """На финале матча проставляет результат каждого исхода во всех строках матча."""
    final_score = f"{s1}:{s2}"
    final_total = s1 + s2
    try:
        for r in collector_db.get_event_rows(event_id):
            if r.get("final_score") is not None:
                continue
            res = _resolve_row(r, s1, s2)
            if res:
                collector_db.update_results(r["id"], res, final_score, final_total)
        log.info("collector resolve ev=%s %s (тотал %s)", event_id, final_score, final_total)
    except Exception as e:
        log.warning("collector resolve err ev=%s: %s", event_id, e)
    finally:
        _last_minute.pop(event_id, None)
