"""Движок стратегии PRIME (сигналы ТМ / ИТМ1).

Наборы задаются кнопками бота (prime_db): рынок (ТМ|ИТМ1) + минута + галочки пар.
Срабатывание: на СТРОГО заданной игровой минуте матча Prime муж, если пара матча
отмечена в наборе — берём текущую КРАЙНЮЮ линию рынка (та же, что пишет сборщик:
collector.extract_markets) и шлём один сигнал на матч на набор (дедуп через БД) в
чат стратегии (config.PRIME_STRAT_CODE). Фильтра по значению линии нет.

На финале — дорасчёт по счёту основного времени:
  ТМ:   зашло, если (s1 + s2) < линия; пуш (Возврат), если равно;
  ИТМ1: зашло, если s1 < линия; пуш, если равно.

Подцеплен к parser.py: process_match() каждый цикл для Prime муж, resolve() на финале.
"""
import html
import logging
from datetime import datetime, timezone, timedelta

import collector
import database
import prime_db
import tg_notify
from config import PRIME_STRAT_CODE, PRIME_MARKETS, STAKE

log = logging.getLogger("prime_signals")
MSK = timezone(timedelta(hours=3))

# Рынок -> (поле линии, поле кф) в результате collector.extract_markets.
MARKET_FIELD = {
    "tm":  ("total_line", "total_m_odds"),
    "it1": ("it1_line", "it1_m_odds"),
}


def market_label(code: str) -> str:
    return PRIME_MARKETS.get(code, code)


def fmt_num(o) -> str:
    if o is None:
        return "—"
    return f"{float(o):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_teams(team1: str, team2: str) -> str:
    return f"⚔️<code>{html.escape(team1)} - {html.escape(team2)}</code>"


def _now() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


# --- результат -------------------------------------------------------------

def _result(market: str, s1: int, s2: int, line: float) -> tuple[str, int | None, float | None]:
    """(текст результата, won 1/0/None, profit ₽). ТМ считаем по сумме, ИТМ1 по К1."""
    value = (s1 + s2) if market == "tm" else s1
    if value == line:
        return "Возврат", None, 0.0            # пуш на целой линии — ставка возвращается
    won = value < line                          # ставка на «меньше»
    if won:
        return "Выигрыш", 1, None               # profit проставим по кф в вызывающем коде
    return "Проигрыш", 0, float(-STAKE)


# --- рендер ----------------------------------------------------------------

def render_signal(sig: dict) -> str:
    lines = [
        "🏀 <b>PRIME · СИГНАЛ</b>",
        fmt_teams(sig["team1"], sig["team2"]),
        "",
        f"⏱ <b>Минута {sig['minute']}</b>",
        f"📊 <b>Счёт {sig['score1']}:{sig['score2']}</b>",
        "",
        f"🎯 <b>Ставка {market_label(sig['market'])} {fmt_num(sig['line'])} "
        f"@{fmt_num(sig['odds'])}</b>",
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
    """Каждый цикл парсера для live-матча Prime муж."""
    eid = state["event_id"]
    minute = (state.get("ts") or 0) // 60

    rules = [r for r in prime_db.get_rules() if r["enabled"] and r["minute"] == minute]
    if not rules:
        return

    pair = prime_db.norm_pair(state.get("team1") or "?", state.get("team2") or "?")

    factors = collector._root_factors(api_data, eid)
    markets = collector.extract_markets(factors) if factors else None

    for rule in rules:
        if pair not in prime_db.get_rule_pairs(rule["id"]):
            continue
        if prime_db.signal_exists(rule["id"], eid):
            continue
        if not markets:
            continue  # рынков нет в этом цикле — попробуем на след., минута ещё та же
        line_f, odds_f = MARKET_FIELD[rule["market"]]
        line = markets.get(line_f)
        odds = markets.get(odds_f)
        if line is None or odds is None:
            continue  # нужный рынок ещё не появился — ждём следующий цикл этой минуты
        try:
            _fire(rule, state, line, odds)
        except Exception as e:
            log.warning("fire err rule=%s ev=%s: %s", rule["id"], eid, e)


def _fire(rule: dict, state: dict, line: float, odds: float):
    chat_id = database.get_chat_id(PRIME_STRAT_CODE)
    sig = {
        "rule_id": rule["id"],
        "event_id": state["event_id"],
        "league": state["league"],
        "market": rule["market"],
        "minute": rule["minute"],
        "fired_minute": (state.get("ts") or 0) // 60,
        "team1": state.get("team1") or "?",
        "team2": state.get("team2") or "?",
        "line": line,
        "odds": odds,
        "score1": state["score1"],
        "score2": state["score2"],
        "chat_id": chat_id,
        "message_id": None,
        "status": "sent",
        "result": None,
        "won": None,
        "final_score": None,
        "final_total": None,
        "profit": None,
        "created_at": _now(),
    }
    if chat_id is not None:
        sig["message_id"] = tg_notify.send(chat_id, render_signal(sig))
    sid = prime_db.insert_signal(sig)
    if sid is None:
        log.info("dup skipped rule=%s ev=%s", rule["id"], state["event_id"])
    else:
        log.info("PRIME rule=%s ev=%s %s line=%s min=%s odds=%s score=%s:%s chat=%s",
                 rule["id"], state["event_id"], rule["market"], line, sig["minute"],
                 odds, state["score1"], state["score2"], chat_id)


def resolve(event_id: int, s1: int, s2: int):
    """Дорасчёт сигналов матча по счёту основного времени."""
    final_score = f"{s1}:{s2}"
    final_total = s1 + s2
    try:
        for sig in prime_db.get_signals_for_event(event_id):
            if sig["result"] is not None or sig["line"] is None:
                continue
            result, won, profit = _result(sig["market"], s1, s2, sig["line"])
            if won == 1 and sig["odds"] is not None:
                profit = STAKE * (float(sig["odds"]) - 1.0)
            prime_db.update_signal_result(sig["id"], result, won, final_score,
                                          final_total, profit)
            if sig["status"] == "sent" and sig["message_id"] and sig["chat_id"] is not None:
                s2d = dict(sig)
                s2d.update(result=result, final_score=final_score, final_total=final_total)
                tg_notify.edit(sig["chat_id"], sig["message_id"], render_signal(s2d))
    except Exception as e:
        log.warning("resolve err ev=%s: %s", event_id, e)
