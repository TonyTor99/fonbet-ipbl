"""Сборщик рынков ПО ЧЕТВЕРТЯМ (Pro муж / Pro жен): раз в игровую минуту пишет
крайнюю линию каждого рынка КАЖДОЙ доступной четверти в отдельную БД, на финале
проставляет результат каждого исхода по счёту КОНКРЕТНОЙ четверти.

Рынки четверти лежат в дочерних событиях матча («N-я четверть»): у каждого свой
блок customFactors с теми же factorId, что и у матча, плюс ничья X (WINX_FID).
Дочерние блоки приходят в том же ответе /events/event — доп. запросов к API нет.

Точки входа:
  process(state, api_data, db) — вызывать каждый цикл парсера для нужного матча;
  resolve(event_id, quarters, s1, s2, db) — на финале матча.
"""
import logging
import re
from datetime import datetime, timezone, timedelta

import collector          # переиспользуем extract_markets / _by_line / _odds
import collector_periods_db
from config import (WINX_FID, TOTAL_B_FIDS, TOTAL_M_FIDS,
                    IT1_B_FIDS, IT1_M_FIDS, IT2_B_FIDS, IT2_M_FIDS)

log = logging.getLogger("collector_periods")
MSK = timezone(timedelta(hours=3))

# event_id -> последняя записанная игровая минута (дедуп в памяти)
_last_minute: dict[int, int] = {}

_QUARTER_RE = re.compile(r"(\d+)")


# --- разбор дочерних событий четвертей --------------------------------------

def _period_blocks(api_data, root_id: int) -> list[tuple[int, list[dict]]]:
    """[(номер_четверти, факторы), ...] по дочерним событиям «N-я четверть».

    Сопоставляем дочерние events (level=2, parentId=root, name='N-я четверть')
    с их блоками customFactors по event_id."""
    if not api_data:
        return []
    # child_event_id -> номер четверти
    quarter_of: dict[int, int] = {}
    for ev in api_data.get("events", []):
        if ev.get("parentId") != root_id:
            continue
        name = ev.get("name") or ""
        if "четверт" not in name.lower():
            continue
        m = _QUARTER_RE.search(name)
        if m:
            quarter_of[ev["id"]] = int(m.group(1))
    if not quarter_of:
        return []
    out = []
    for cf in api_data.get("customFactors", []):
        eid = cf.get("e")
        if eid in quarter_of:
            out.append((quarter_of[eid], cf.get("factors", [])))
    out.sort(key=lambda x: x[0])
    return out


def _mid_market(factors: list[dict], b_fids, m_fids):
    """Средняя РЕАЛЬНАЯ линия рынка (тотал/инд.тотал) четверти + её реальные кф.

    Берём линии, где котируются обе стороны (Б и М) — это ровно линии из
    выпадающего списка, — и выбираем среднюю по позиции (медиану). При чётном
    числе линий берём верхнюю из двух центральных. Возвращает
    (линия, кф_Б, кф_М) или (None, None, None), если рынок не котируется."""
    bb = collector._by_line(factors, b_fids)
    mm = collector._by_line(factors, m_fids)
    both = sorted(set(bb) & set(mm))
    if not both:
        return None, None, None
    line = both[len(both) // 2]
    return line, bb[line], mm[line]


def _extract_period_markets(factors: list[dict]) -> dict:
    """Рынки четверти: фора/победа — крайняя линия (как в матче) + ничья X;
    тотал / инд.тотал1 / инд.тотал2 — СРЕДНЯЯ реальная линия из выпадающего списка."""
    m = collector.extract_markets(factors)
    m["winx_odds"] = collector._odds(factors, WINX_FID)
    # Тотал и инд.тоталы — средняя реальная линия из всего списка.
    for pref, b_fids, m_fids in (
        ("total", TOTAL_B_FIDS, TOTAL_M_FIDS),
        ("it1", IT1_B_FIDS, IT1_M_FIDS),
        ("it2", IT2_B_FIDS, IT2_M_FIDS),
    ):
        line, b_odds, m_odds = _mid_market(factors, b_fids, m_fids)
        if line is not None:
            m[f"{pref}_line"] = line
            m[f"{pref}_b_odds"] = b_odds
            m[f"{pref}_m_odds"] = m_odds
    return m


# --- запись по игровой минуте ----------------------------------------------

def process(state: dict, api_data, db: str):
    """Пишет по строке на каждую доступную четверть, если игровая минута сменилась.

    db — файл БД лиги (config.PERIOD_COLLECTOR_LEAGUES)."""
    eid = state["event_id"]
    ts = state.get("ts") or 0
    minute = ts // 60

    if _last_minute.get(eid) == minute:
        return

    blocks = _period_blocks(api_data, eid)
    if not blocks:
        return  # нет рынков четвертей в этом цикле — попробуем на следующем

    now = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")
    quarters = state.get("quarters") or []   # живой счёт по четвертям из comment
    wrote = 0
    for quarter, factors in blocks:
        if collector_periods_db.snapshot_exists(db, eid, minute, quarter):
            continue
        markets = _extract_period_markets(factors)
        # живой счёт ИМЕННО этой четверти на момент снимка (если она уже началась)
        q_live = None
        if 1 <= quarter <= len(quarters):
            ql1, ql2 = quarters[quarter - 1]
            q_live = f"{ql1}:{ql2}"
        row = {
            "event_id": eid,
            "league": state["league"],
            "team1": state.get("team1") or "?",
            "team2": state.get("team2") or "?",
            "snap_dt_msk": now,
            "game_minute": minute,
            "quarter": quarter,
            "score1": state["score1"],
            "score2": state["score2"],
            "q_live_score": q_live,
            "created_at": now,
            **markets,
        }
        if collector_periods_db.insert_snapshot(db, row) is not None:
            wrote += 1
    _last_minute[eid] = minute
    if wrote:
        log.info("collect periods ev=%s min=%s quarters=%s",
                 eid, minute, [q for q, _ in blocks])


# --- дорасчёт результата на финале -----------------------------------------

def _wl(win: bool) -> str:
    return "Выигрыш" if win else "Проигрыш"


def _resolve_row(r: dict, q1: int, q2: int) -> dict:
    """Результаты рынков четверти считаются по счёту ЭТОЙ четверти (q1:q2).
    Все линии реальные (.5) → без пуша."""
    res = {}
    # Победа / ничья (в четверти возможен ничейный исход)
    if any(r.get(k) is not None for k in ("win1_odds", "winx_odds", "win2_odds")):
        res["r_win1"] = _wl(q1 > q2)
        res["r_winx"] = _wl(q1 == q2)
        res["r_win2"] = _wl(q2 > q1)
    # Фора (линия со стороны К1, напр. -2.5): К1 проходит если q1 + line > q2
    fl = r.get("fora_line")
    if fl is not None:
        c1 = (q1 + fl) > q2
        res["r_fora1"] = _wl(c1)
        res["r_fora2"] = _wl(not c1)
    # Тотал четверти
    tl = r.get("total_line")
    if tl is not None:
        over = (q1 + q2) > tl
        res["r_total_b"] = _wl(over)
        res["r_total_m"] = _wl(not over)
    # Инд. тотал К1
    i1 = r.get("it1_line")
    if i1 is not None:
        over = q1 > i1
        res["r_it1_b"] = _wl(over)
        res["r_it1_m"] = _wl(not over)
    # Инд. тотал К2
    i2 = r.get("it2_line")
    if i2 is not None:
        over = q2 > i2
        res["r_it2_b"] = _wl(over)
        res["r_it2_m"] = _wl(not over)
    return res


def resolve(event_id: int, quarters: list[tuple[int, int]], s1: int, s2: int, db: str):
    """На финале проставляет результат каждой строки по счёту её четверти.

    quarters — счёт по четвертям из comment: [(24,24),(31,32),...].
    Строки четвертей, по которым нет сыгранного счёта (напр. не было ОТ), пропускаем.
    db — файл БД лиги (config.PERIOD_COLLECTOR_LEAGUES)."""
    final_score = f"{s1}:{s2}"
    final_total = s1 + s2
    try:
        for r in collector_periods_db.get_event_rows(db, event_id):
            if r.get("final_score") is not None:
                continue
            q = r["quarter"]
            if q < 1 or q > len(quarters):
                continue  # нет счёта этой четверти — не резолвим
            q1, q2 = quarters[q - 1]
            res = _resolve_row(r, q1, q2)
            if res:
                collector_periods_db.update_results(
                    db, r["id"], res, f"{q1}:{q2}", final_score, final_total)
        log.info("periods resolve ev=%s %s четверти=%s", event_id, final_score, quarters)
    except Exception as e:
        log.warning("periods resolve err ev=%s: %s", event_id, e)
    finally:
        _last_minute.pop(event_id, None)
