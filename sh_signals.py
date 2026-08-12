"""Движок стратегии ШОРТ-ХОККЕЙ.

Правила задаются кнопками бота (таблица sh_rules): лига + минута + исход
(П1/X/П2) + диапазон кф. Бот ищет ТОЛЬКО настроенные лиги.

Срабатывание: на СТРОГО заданной игровой минуте матча настроенной лиги, если кф
выбранного исхода попал в диапазон [kf_min, kf_max] — шлём один сигнал на матч на
правило (дедуп через БД) в общий чат стратегии (config.SH_STRAT_CODE в bot_config).
На финале матча — дорасчёт итога и редактирование сообщения.

Подцеплен к sh_parser.py: process_match() каждый цикл, resolve() на финале.
"""
import html
import logging

from datetime import datetime, timezone, timedelta

import database
import tg_notify
from config import SH_STRAT_CODE, sh_short_league

log = logging.getLogger("sh_signals")
MSK = timezone(timedelta(hours=3))

# Исход правила -> (поле кф в markets, короткая подпись).
OUTCOME_FIELD = {"win1": "win1_odds", "draw": "draw_odds", "win2": "win2_odds"}
OUTCOME_LABEL = {"win1": "П1", "draw": "X", "win2": "П2"}


def _now() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


def fmt_odds(o) -> str:
    if o is None:
        return "—"
    return f"{float(o):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_range(a: float, b: float) -> str:
    return f"{fmt_odds(a)}–{fmt_odds(b)}"


def outcome_label(code: str) -> str:
    return OUTCOME_LABEL.get(code, code)


def fmt_teams(team1: str, team2: str) -> str:
    """Обе команды в одном <code> — копируются одним тапом в Telegram."""
    return f"⚔️<code>{html.escape(team1)} - {html.escape(team2)}</code>"


# --- результат исхода 1X2 --------------------------------------------------

def _outcome_won(outcome: str, s1: int, s2: int) -> bool:
    if outcome == "win1":
        return s1 > s2
    if outcome == "win2":
        return s2 > s1
    return s1 == s2          # draw


# --- рендер сообщения ------------------------------------------------------

def render_signal(sig: dict) -> str:
    league = html.escape(sh_short_league(sig["league"]))
    lines = [
        "🏒 <b>ШОРТ-ХОККЕЙ · СИГНАЛ</b>",
        f"🏆 <b>{league}</b>",
        fmt_teams(sig["team1"], sig["team2"]),
        "",
        f"⏱ <b>Минута {sig['rule_minute']}</b>",
        f"📊 <b>Счёт {sig['score1']}:{sig['score2']}</b>",
        "",
        f"🎯 <b>Ставка {outcome_label(sig['outcome'])} @{fmt_odds(sig['odds'])}</b>",
        f"📐 Диапазон кф: {fmt_range(sig['kf_min'], sig['kf_max'])}",
    ]
    if sig.get("final_score"):
        lines.append(f"🏁 <b>Итог: {html.escape(str(sig['final_score']))}</b>")
        if sig.get("result") == "Выигрыш":
            lines.append("✅ <b>Выигрыш</b>")
        elif sig.get("result") == "Проигрыш":
            lines.append("❌ <b>Проигрыш</b>")
    return "\n".join(lines)


# --- точки входа -----------------------------------------------------------

def process_match(state: dict, markets: dict):
    """Вызывается каждый цикл sh_parser для каждого live-матча шорт-хоккея.

    state: event_id, league (полное название), team1, team2, score1, score2,
           prematch (bool), minute (игровая минута = timerSeconds//60).
    markets: словарь из sh_parser.extract_markets (win1_odds/draw_odds/win2_odds ...).
    """
    if state.get("prematch"):
        return
    league = state["league"]
    minute = state["minute"]

    rules = [r for r in database.sh_get_rules()
             if r["enabled"] and r["sport_name"] == league]
    if not rules:
        return

    for rule in rules:
        # строго на заданной минуте
        if minute != rule["minute"]:
            continue
        if database.sh_signal_exists(rule["id"], state["event_id"]):
            continue
        odds = markets.get(OUTCOME_FIELD[rule["outcome"]])
        if odds is None:
            # рынок исхода на этом снимке отсутствует — попробуем на след. цикле,
            # пока игровая минута ещё равна заданной (дедуп не ставим)
            continue
        if not (rule["kf_min"] <= odds <= rule["kf_max"]):
            # кф вне диапазона — ждём следующий цикл этой же минуты (кф двигается)
            continue
        try:
            _fire(rule, state, odds)
        except Exception as e:
            log.warning("fire err rule=%s ev=%s: %s", rule["id"], state["event_id"], e)


def _fire(rule: dict, state: dict, odds: float):
    chat_id = database.get_chat_id(SH_STRAT_CODE)
    sig = {
        "rule_id": rule["id"],
        "event_id": state["event_id"],
        "league": state["league"],
        "team1": state.get("team1") or "?",
        "team2": state.get("team2") or "?",
        "rule_minute": rule["minute"],
        "fired_minute": state["minute"],
        "outcome": rule["outcome"],
        "odds": odds,
        "kf_min": rule["kf_min"],
        "kf_max": rule["kf_max"],
        "score1": state["score1"],
        "score2": state["score2"],
        "chat_id": chat_id,
        "message_id": None,
        "status": "no_chat",
        "result": None,
        "final_score": None,
        "created_at": _now(),
    }
    if chat_id is not None:
        sig["message_id"] = tg_notify.send(chat_id, render_signal(sig))
        sig["status"] = "sent"
    sid = database.sh_insert_signal(sig)
    if sid is None:
        log.info("dup skipped rule=%s ev=%s", rule["id"], state["event_id"])
    else:
        log.info("SH-SIGNAL rule=%s ev=%s %s min=%s odds=%s score=%s:%s chat=%s",
                 rule["id"], state["event_id"], rule["outcome"], state["minute"],
                 odds, state["score1"], state["score2"], chat_id)


def resolve(event_id: int, s1: int, s2: int):
    """Дорасчёт итога сигналов стратегии и редактирование сообщений."""
    final_score = f"{s1}:{s2}"
    try:
        for sig in database.sh_get_signals_for_event(event_id):
            if sig["result"] is not None:
                continue
            won = _outcome_won(sig["outcome"], s1, s2)
            result = "Выигрыш" if won else "Проигрыш"
            database.sh_update_signal_result(sig["id"], result, final_score)
            if sig["status"] == "sent" and sig["message_id"] and sig["chat_id"] is not None:
                s2d = dict(sig)
                s2d.update(result=result, final_score=final_score)
                tg_notify.edit(sig["chat_id"], sig["message_id"], render_signal(s2d))
    except Exception as e:
        log.warning("resolve err ev=%s: %s", event_id, e)
