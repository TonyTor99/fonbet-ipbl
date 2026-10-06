"""Движок стратегии Pro МУЖЧИНЫ (сигнал ТМ).

Полный аналог prime_women_signals.py, но ТОЛЬКО рынок ТМ (тотал матча меньше) и
лига Pro муж. Наборы задаются кнопками бота (pro_strat_db): минута + галочки пар;
можно несколько наборов с разными минутами.

Срабатывание: на СТРОГО заданной игровой минуте матча Pro муж, если пара матча
отмечена в наборе И сейчас окно работы (signals.in_schedule) — берём текущую
КРАЙНЮЮ линию ТМ (та же, что пишет сборщик: collector.extract_markets) и шлём один
сигнал на матч на набор (дедуп через БД) в чат стратегии (config.PRO_STRAT_CODE).
Фильтра по значению линии нет. Вне окон работы стратегия молчит.

На финале — дорасчёт по счёту основного времени:
  ТМ: зашло, если (s1 + s2) < линия; пуш (Возврат), если равно.

Подцеплен к parser.py: process_match() каждый цикл для Pro муж, resolve() на финале.
"""
import html
import logging
from datetime import datetime, timezone, timedelta

import collector
import database
import pro_strat_db
import signals
import tg_notify
from config import PRO_STRAT_CODE, STAKE

log = logging.getLogger("pro_strat_signals")
MSK = timezone(timedelta(hours=3))


def fmt_num(o) -> str:
    if o is None:
        return "—"
    return f"{float(o):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_teams(team1: str, team2: str) -> str:
    return f"⚔️<code>{html.escape(team1)} - {html.escape(team2)}</code>"


def _now() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


# --- результат -------------------------------------------------------------

def _result(s1: int, s2: int, line: float) -> tuple[str, int | None, float | None]:
    """(текст, won 1/0/None, profit ₽) для ТМ по сумме очков. Пуш на целой линии."""
    value = s1 + s2
    if value == line:
        return "Возврат", None, 0.0
    won = value < line
    if won:
        return "Выигрыш", 1, None               # profit проставим по кф в вызывающем коде
    return "Проигрыш", 0, float(-STAKE)


# --- рендер ----------------------------------------------------------------

def fmt_stats(st: dict) -> str:
    """'📈 Статистика count/roi%/profit' по встречам пары в рынке ТМ."""
    return f"📈 <b>Статистика {st['count']}/{st['roi']:.0f}%/{st['profit']:.0f}</b>"


def render_signal(sig: dict) -> str:
    st = pro_strat_db.pair_stats_from_collector(sig["team1"], sig["team2"], sig["minute"])
    lines = [
        "🏀 <b>PRO М · СИГНАЛ ТМ</b>",
        fmt_teams(sig["team1"], sig["team2"]),
        fmt_stats(st),
        "",
        f"⏱ <b>Минута {sig['minute']}</b>",
        f"📊 <b>Счёт {sig['score1']}:{sig['score2']}</b>",
        "",
        f"🎯 <b>Ставка ТМ {fmt_num(sig['line'])} @{fmt_num(sig['odds'])}</b>",
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
    """Каждый цикл парсера для live-матча Pro муж."""
    eid = state["event_id"]
    minute = (state.get("ts") or 0) // 60

    rules = [r for r in pro_strat_db.get_rules() if r["enabled"] and r["minute"] == minute]
    if not rules:
        return

    # Окно работы (время суток МСК): вне окон стратегия молчит.
    if not signals.in_schedule(PRO_STRAT_CODE):
        return

    pair = pro_strat_db.norm_pair(state.get("team1") or "?", state.get("team2") or "?")

    factors = collector._root_factors(api_data, eid)
    markets = collector.extract_markets(factors) if factors else None

    for rule in rules:
        if pair not in pro_strat_db.get_rule_pairs(rule["id"]):
            continue
        if pro_strat_db.signal_exists(rule["id"], eid):
            continue
        if not markets:
            continue  # рынков нет в этом цикле — попробуем на след., минута ещё та же
        line = markets.get("total_line")
        odds = markets.get("total_m_odds")
        if line is None or odds is None:
            continue  # ТМ ещё не котируется — ждём следующий цикл этой минуты
        try:
            _fire(rule, state, line, odds)
        except Exception as e:
            log.warning("fire err rule=%s ev=%s: %s", rule["id"], eid, e)


def _fire(rule: dict, state: dict, line: float, odds: float):
    chat_id = database.get_chat_id(PRO_STRAT_CODE)
    sig = {
        "rule_id": rule["id"],
        "event_id": state["event_id"],
        "league": state["league"],
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
    sid = pro_strat_db.insert_signal(sig)
    if sid is None:
        log.info("dup skipped rule=%s ev=%s", rule["id"], state["event_id"])
    else:
        log.info("PRO-M rule=%s ev=%s ТМ line=%s min=%s odds=%s score=%s:%s chat=%s",
                 rule["id"], state["event_id"], line, sig["minute"], odds,
                 state["score1"], state["score2"], chat_id)


def resolve(event_id: int, s1: int, s2: int):
    """Дорасчёт сигналов матча по счёту основного времени."""
    final_score = f"{s1}:{s2}"
    final_total = s1 + s2
    try:
        for sig in pro_strat_db.get_signals_for_event(event_id):
            if sig["result"] is not None or sig["line"] is None:
                continue
            result, won, profit = _result(s1, s2, sig["line"])
            if won == 1 and sig["odds"] is not None:
                profit = STAKE * (float(sig["odds"]) - 1.0)
            pro_strat_db.update_signal_result(sig["id"], result, won, final_score,
                                              final_total, profit)
            if sig["status"] == "sent" and sig["message_id"] and sig["chat_id"] is not None:
                s2d = dict(sig)
                s2d.update(result=result, final_score=final_score, final_total=final_total)
                tg_notify.edit(sig["chat_id"], sig["message_id"], render_signal(s2d))
    except Exception as e:
        log.warning("resolve err ev=%s: %s", event_id, e)
