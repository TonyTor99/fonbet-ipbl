"""Движок стратегии ТОТАЛОВ шорт-хоккея (ТБ/ТМ).

Правила задаются кнопками бота (таблица sh_total_rules): лига + минута + сторона
(ТБ/ТМ) + диапазон ЛИНИИ тотала [line_min, line_max]. Бот ищет ТОЛЬКО настроенные
лиги.

Срабатывание: на СТРОГО заданной игровой минуте матча настроенной лиги, если
линия тотала матча (крайняя, из sh_parser.extract_markets) попала в диапазон —
шлём один сигнал на матч на правило (дедуп через БД) в общий чат стратегии
(config.SH_TOTAL_STRAT_CODE). На финале — дорасчёт по ИТОГОВОМУ тоталу основного
времени и редактирование сообщения (пуш на целой линии = Возврат).

Подцеплен к sh_parser.py: process_match() каждый цикл, resolve() на финале.
"""
import html
import logging

from datetime import datetime, timezone, timedelta

import database
import tg_notify
from config import SH_TOTAL_STRAT_CODE, sh_short_league

log = logging.getLogger("sh_total_signals")
MSK = timezone(timedelta(hours=3))

# Сторона правила -> (поле кф в markets, короткая подпись).
SIDE_FIELD = {"over": "total_b_odds", "under": "total_m_odds"}
SIDE_LABEL = {"over": "ТБ", "under": "ТМ"}


def _now() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


def fmt_num(o) -> str:
    if o is None:
        return "—"
    return f"{float(o):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_range(a: float, b: float) -> str:
    return f"{fmt_num(a)}–{fmt_num(b)}"


def side_label(code: str) -> str:
    return SIDE_LABEL.get(code, code)


def fmt_teams(team1: str, team2: str) -> str:
    """Обе команды в одном <code> — копируются одним тапом в Telegram."""
    return f"⚔️<code>{html.escape(team1)} - {html.escape(team2)}</code>"


# --- результат тотала ------------------------------------------------------

def _total_result(side: str, total: int, line: float) -> str:
    """Выигрыш / Проигрыш / Возврат для стороны ТБ(over)/ТМ(under)."""
    if total == line:
        return "Возврат"
    over_won = total > line
    if side == "over":
        return "Выигрыш" if over_won else "Проигрыш"
    return "Проигрыш" if over_won else "Выигрыш"


# --- рендер сообщения ------------------------------------------------------

def render_signal(sig: dict) -> str:
    league = html.escape(sh_short_league(sig["league"]))
    lines = [
        "🏒 <b>ШОРТ-ХОККЕЙ · ТОТАЛ · СИГНАЛ</b>",
        f"🏆 <b>{league}</b>",
        fmt_teams(sig["team1"], sig["team2"]),
        "",
        f"⏱ <b>Минута {sig['rule_minute']}</b>",
        f"📊 <b>Счёт {sig['score1']}:{sig['score2']}</b>",
        "",
        f"🎯 <b>Ставка {side_label(sig['side'])} {fmt_num(sig['line'])} @{fmt_num(sig['odds'])}</b>",
        f"📐 Диапазон линии: {fmt_range(sig['line_min'], sig['line_max'])}",
    ]
    if sig.get("final_score"):
        lines.append(f"🏁 <b>Итог: {html.escape(str(sig['final_score']))}</b> "
                     f"(тотал {sig.get('final_total', '—')})")
        if sig.get("shootout"):
            lines.append("🔸 <i>серия буллитов — расчёт по осн. времени</i>")
        if sig.get("result") == "Выигрыш":
            lines.append("✅ <b>Выигрыш</b>")
        elif sig.get("result") == "Проигрыш":
            lines.append("❌ <b>Проигрыш</b>")
        elif sig.get("result") == "Возврат":
            lines.append("↩️ <b>Возврат</b>")
    return "\n".join(lines)


# --- точки входа -----------------------------------------------------------

def process_match(state: dict, markets: dict):
    """Вызывается каждый цикл sh_parser для каждого live-матча шорт-хоккея."""
    if state.get("prematch"):
        return
    league = state["league"]
    minute = state["minute"]

    rules = [r for r in database.sh_total_get_rules()
             if r["enabled"] and r["sport_name"] == league]
    if not rules:
        return

    line = markets.get("total_line")
    if line is None:
        # рынок тотала на этом снимке отсутствует — попробуем на след. цикле,
        # пока игровая минута ещё равна заданной (дедуп не ставим)
        return

    for rule in rules:
        if minute != rule["minute"]:
            continue
        if database.sh_total_signal_exists(rule["id"], state["event_id"]):
            continue
        if not (rule["line_min"] <= line <= rule["line_max"]):
            # линия вне диапазона — ждём следующий цикл этой же минуты (линия двигается)
            continue
        odds = markets.get(SIDE_FIELD[rule["side"]])
        if odds is None:
            continue
        try:
            _fire(rule, state, line, odds)
        except Exception as e:
            log.warning("fire err rule=%s ev=%s: %s", rule["id"], state["event_id"], e)


def _fire(rule: dict, state: dict, line: float, odds: float):
    chat_id = database.get_chat_id(SH_TOTAL_STRAT_CODE)
    sig = {
        "rule_id": rule["id"],
        "event_id": state["event_id"],
        "league": state["league"],
        "team1": state.get("team1") or "?",
        "team2": state.get("team2") or "?",
        "rule_minute": rule["minute"],
        "fired_minute": state["minute"],
        "side": rule["side"],
        "line": line,
        "odds": odds,
        "line_min": rule["line_min"],
        "line_max": rule["line_max"],
        "score1": state["score1"],
        "score2": state["score2"],
        "chat_id": chat_id,
        "message_id": None,
        "status": "no_chat",
        "result": None,
        "final_score": None,
        "final_total": None,
        "created_at": _now(),
    }
    if chat_id is not None:
        sig["message_id"] = tg_notify.send(chat_id, render_signal(sig))
        sig["status"] = "sent"
    sid = database.sh_total_insert_signal(sig)
    if sid is None:
        log.info("dup skipped rule=%s ev=%s", rule["id"], state["event_id"])
    else:
        log.info("SH-TOTAL rule=%s ev=%s %s line=%s min=%s odds=%s score=%s:%s chat=%s",
                 rule["id"], state["event_id"], rule["side"], line, state["minute"],
                 odds, state["score1"], state["score2"], chat_id)


def resolve(event_id: int, s1: int, s2: int, displayed=None):
    """Дорасчёт сигналов тоталов по ИТОГОВОМУ тоталу ОСНОВНОГО времени.

    s1:s2 — счёт основного времени (без буллитов/ОТ). displayed — общий счёт с
    буллитом (если был): при расхождении помечаем сигнал как решённый серией."""
    final_score = f"{s1}:{s2}"
    final_total = s1 + s2
    shootout = displayed is not None and tuple(displayed) != (s1, s2)
    try:
        for sig in database.sh_total_get_signals_for_event(event_id):
            if sig["result"] is not None:
                continue
            if sig["line"] is None:
                continue
            result = _total_result(sig["side"], final_total, sig["line"])
            database.sh_total_update_signal_result(sig["id"], result, final_score, final_total)
            if sig["status"] == "sent" and sig["message_id"] and sig["chat_id"] is not None:
                s2d = dict(sig)
                s2d.update(result=result, final_score=final_score,
                           final_total=final_total, shootout=shootout)
                tg_notify.edit(sig["chat_id"], sig["message_id"], render_signal(s2d))
    except Exception as e:
        log.warning("resolve err ev=%s: %s", event_id, e)
