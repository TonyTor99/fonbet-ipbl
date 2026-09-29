"""Движок стратегии CAGE (сигнал ТМ / ИТМ1 / ИТМ2 по «ровной» линии).

Наборы задаются кнопками бота (cage_strat_db): рынок (ТМ|ИТМ1|ИТМ2) + момент входа
(Прематч ИЛИ строго заданная игровая минута) + флаг «все пары» + галочки пар.

Срабатывание: матч лиги CAGE Division, в нужный момент (прематч ИЛИ заданная
минута), пара матча отмечена в наборе (или включён режим «все пары»), а среди
живых линий нужного вида (тотал/инд.тотал К1/инд.тотал К2) есть «меньше» — берём
«ровную» линию (кф ≈ 2.0, окно CAGE_STRAT_KF_MIN..KF_MAX, как pick_tm_line в
стратегии IPBL) и шлём один сигнал на матч на набор (дедуп через БД) в чат СВОЕГО
рынка (config.CAGE_STRAT_CHAT).

На финале — дорасчёт по ИТОГОВОМУ счёту основного времени:
  ТМ:   зашло, если (s1 + s2) < линия;
  ИТМ1: зашло, если s1 < линия;
  ИТМ2: зашло, если s2 < линия. Пуш (Возврат) на целой линии.

Подцеплен к cage_parser.py: process_match() каждый цикл, resolve() на финале.
Извлечение факторов — локальное (без импорта cage_parser), чтобы не было цикла.
"""
import html
import logging
from datetime import datetime, timezone, timedelta

import cage_strat_db
import database
import tg_notify
from cage_config import TOTAL_M_FIDS, IT1_M_FIDS, IT2_M_FIDS
from config import (CAGE_STRAT_CODE_TM, CAGE_STRAT_CHAT, CAGE_STRAT_MARKETS,
                    CAGE_STRAT_PREMATCH, CAGE_STRAT_KF_MIN, CAGE_STRAT_KF_MAX, STAKE)

log = logging.getLogger("cage_strat_signals")
MSK = timezone(timedelta(hours=3))

# Рынок -> набор factorId «меньше» соответствующего вида в root customFactors.
MARKET_FIDS = {"tm": TOTAL_M_FIDS, "it1": IT1_M_FIDS, "it2": IT2_M_FIDS}


def market_label(code: str) -> str:
    return CAGE_STRAT_MARKETS.get(code, code)


# --- извлечение живой линии ТМ из факторов матча ----------------------------

def _root_factors(api_data, event_id) -> list[dict]:
    if not api_data:
        return []
    for cf in api_data.get("customFactors", []):
        if cf.get("e") == event_id:
            return cf.get("factors", [])
    return []


def _market_lines(factors: list[dict], fids: set) -> dict[float, float]:
    """{линия: кф «меньше»} по всем факторам вида рынка (p/100 — линия)."""
    out = {}
    for f in factors:
        if f.get("f") in fids and f.get("p") is not None and f.get("v") is not None:
            out[round(f["p"] / 100.0, 1)] = f["v"]
    return out


def pick_even_tm(tm_lines: dict[float, float]):
    """«Ровная» линия рынка: кф ≈ 2.0 (наибольший кф в окне, иначе наибольший доступный).

    То же правило, что pick_tm_line в стратегии IPBL. Возвращает (линия, кф) или
    (None, None), если рынок ещё не котируется."""
    items = [(ln, od) for ln, od in tm_lines.items() if od is not None]
    if not items:
        return None, None
    in_range = [it for it in items if CAGE_STRAT_KF_MIN <= it[1] <= CAGE_STRAT_KF_MAX]
    pool = in_range or items
    line, odds = max(pool, key=lambda it: it[1])
    return line, odds


# --- форматирование --------------------------------------------------------

def time_label(minute: int) -> str:
    return "Прематч" if minute == CAGE_STRAT_PREMATCH else f"мин {minute}"


def fmt_num(o) -> str:
    if o is None:
        return "—"
    return f"{float(o):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_teams(team1: str, team2: str) -> str:
    return f"⚔️<code>{html.escape(team1)} - {html.escape(team2)}</code>"


def fmt_stats(st: dict) -> str:
    """'📈 Статистика count/roi%/profit' по встречам пары (отправленные сигналы)."""
    return f"📈 <b>Статистика {st['count']}/{st['roi']:.0f}%/{st['profit']:.0f}</b>"


def _now() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


# --- результат -------------------------------------------------------------

def _result(market: str, s1: int, s2: int, line: float) -> tuple[str, int | None, float | None]:
    """(текст, won 1/0/None, profit ₽). ТМ по сумме, ИТМ1 по К1, ИТМ2 по К2.
    Пуш на целой линии = Возврат."""
    value = s1 if market == "it1" else s2 if market == "it2" else (s1 + s2)
    if value == line:
        return "Возврат", None, 0.0
    won = value < line
    if won:
        return "Выигрыш", 1, None            # profit проставим по кф в вызывающем коде
    return "Проигрыш", 0, float(-STAKE)


# --- рендер ----------------------------------------------------------------

def render_signal(sig: dict) -> str:
    st = cage_strat_db.pair_stats(sig["team1"], sig["team2"])
    mk = market_label(sig.get("market", "tm"))
    lines = [
        f"🏀 <b>CAGE · СИГНАЛ {mk}</b>",
        fmt_teams(sig["team1"], sig["team2"]),
        fmt_stats(st),
        "",
        f"⏱ <b>{time_label(sig['minute'])}</b>",
        f"📊 <b>Счёт {sig['score1']}:{sig['score2']}</b>",
        "",
        f"🎯 <b>Ставка {mk} {fmt_num(sig['line'])} @{fmt_num(sig['odds'])}</b>",
    ]
    if sig.get("final_score"):
        lines.append(f"🏁 <b>Итог: {html.escape(str(sig['final_score']))}</b> "
                     f"(тотал {sig.get('final_total', '—')})")
        if sig.get("result") == "Выигрыш":
            lines.append("✅ <b>Зашло</b>")
        elif sig.get("result") == "Проигрыш":
            lines.append("❌ <b>Не зашло</b>")
        elif sig.get("result") == "Возврат":
            lines.append("↩️ <b>Возврат</b>")
    return "\n".join(lines)


# --- точки входа -----------------------------------------------------------

def process_match(state: dict, api_data):
    """Каждый цикл cage_parser для live-матча CAGE Division (в т.ч. прематч)."""
    eid = state["event_id"]
    # Момент срабатывания: прематч -> сентинел -1; иначе — текущая игровая минута.
    point = CAGE_STRAT_PREMATCH if state.get("prematch") else (state.get("mark") or 0)

    rules = [r for r in cage_strat_db.get_rules()
             if r["enabled"] and r["minute"] == point]
    if not rules:
        return

    factors = _root_factors(api_data, eid)
    pair = cage_strat_db.norm_pair(state.get("team1") or "?", state.get("team2") or "?")

    for rule in rules:
        if not rule["all_pairs"] and pair not in cage_strat_db.get_rule_pairs(rule["id"]):
            continue
        if cage_strat_db.signal_exists(rule["id"], eid):
            continue
        fids = MARKET_FIDS.get(rule["market"], TOTAL_M_FIDS)
        line, odds = pick_even_tm(_market_lines(factors, fids)) if factors else (None, None)
        if line is None or odds is None:
            continue  # рынок ещё не котируется в этом цикле — попробуем на след., момент тот же
        try:
            _fire(rule, state, line, odds)
        except Exception as e:
            log.warning("fire err rule=%s ev=%s: %s", rule["id"], eid, e)


def _fire(rule: dict, state: dict, line: float, odds: float):
    chat_id = database.get_chat_id(CAGE_STRAT_CHAT.get(rule["market"], CAGE_STRAT_CODE_TM))
    fired_minute = CAGE_STRAT_PREMATCH if state.get("prematch") else (state.get("mark") or 0)
    sig = {
        "rule_id": rule["id"],
        "event_id": state["event_id"],
        "league": state["league"],
        "market": rule["market"],
        "minute": rule["minute"],
        "fired_minute": fired_minute,
        "team1": state.get("team1") or "?",
        "team2": state.get("team2") or "?",
        "line": line,
        "odds": odds,
        "score1": state["score1"],
        "score2": state["score2"],
        "chat_id": chat_id,
        "message_id": None,
        "status": "no_chat",
        "result": None,
        "won": None,
        "final_score": None,
        "final_total": None,
        "profit": None,
        "created_at": _now(),
    }
    if chat_id is not None:
        sig["message_id"] = tg_notify.send(chat_id, render_signal(sig))
        sig["status"] = "sent"
    sid = cage_strat_db.insert_signal(sig)
    if sid is None:
        log.info("dup skipped rule=%s ev=%s", rule["id"], state["event_id"])
    else:
        log.info("CAGE-STRAT rule=%s ev=%s %s line=%s min=%s odds=%s score=%s:%s chat=%s",
                 rule["id"], state["event_id"], rule["market"], line, rule["minute"], odds,
                 state["score1"], state["score2"], chat_id)


def resolve(event_id: int, s1: int, s2: int):
    """Дорасчёт сигналов матча по ИТОГОВОМУ тоталу основного времени."""
    final_score = f"{s1}:{s2}"
    final_total = s1 + s2
    try:
        for sig in cage_strat_db.get_signals_for_event(event_id):
            if sig["result"] is not None or sig["line"] is None:
                continue
            result, won, profit = _result(sig.get("market", "tm"), s1, s2, sig["line"])
            if won == 1 and sig["odds"] is not None:
                profit = STAKE * (float(sig["odds"]) - 1.0)
            cage_strat_db.update_signal_result(sig["id"], result, won, final_score,
                                               final_total, profit)
            if sig["status"] == "sent" and sig["message_id"] and sig["chat_id"] is not None:
                s2d = dict(sig)
                s2d.update(result=result, final_score=final_score, final_total=final_total)
                tg_notify.edit(sig["chat_id"], sig["message_id"], render_signal(s2d))
    except Exception as e:
        log.warning("resolve err ev=%s: %s", event_id, e)
