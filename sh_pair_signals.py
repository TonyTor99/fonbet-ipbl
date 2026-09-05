"""Движок стратегии ШОРТ-ХОККЕЙ ПО ПАРАМ (тотал ТБ/ТМ по выбранным парам).

Наборы задаются кнопками бота (sh_pair_db): сторона (ТБ|ТМ) + время (Прематч или
строго заданная игровая минута) + диапазон ЛИНИИ тотала + галочки пар. Пары берутся
из сборщика шорт-хоккея, отфильтрованного по двум лигам MNHL / MNHL B
(config.SH_PAIR_LEAGUES).

Срабатывание: матч одной из двух лиг MNHL, в нужный момент (прематч ИЛИ заданная
минута), пара матча отмечена в наборе, а крайняя линия тотала (та же, что пишет
sh_parser.extract_markets) попала в диапазон — шлём один сигнал на матч на набор
(дедуп через БД) в чат стратегии (config.SH_PAIR_STRAT_CODE).

На финале — дорасчёт по ИТОГОВОМУ тоталу основного времени (буллиты/ОТ отброшены):
  ТБ (over):   зашло, если тотал > линия; пуш (Возврат) на целой линии;
  ТМ (under):  зашло, если тотал < линия; пуш на целой линии.

Подцеплен к sh_parser.py: process_match() каждый цикл, resolve() на финале.
"""
import html
import logging
from datetime import datetime, timezone, timedelta

import database
import sh_pair_db
import tg_notify
from config import (SH_PAIR_STRAT_CODE, SH_PAIR_SIDES, SH_PAIR_PREMATCH,
                    SH_PAIR_LEAGUES, STAKE, sh_short_league, sh_league_key)

log = logging.getLogger("sh_pair_signals")
MSK = timezone(timedelta(hours=3))

# Сторона правила -> поле кф в результате sh_parser.extract_markets.
SIDE_FIELD = {"over": "total_b_odds", "under": "total_m_odds"}

# Ключи лиг-источника (без хвостового формата 'NxM') — для сопоставления с живой лигой.
_LEAGUE_KEYS = {sh_league_key(n) for n in SH_PAIR_LEAGUES}


def side_label(code: str) -> str:
    return SH_PAIR_SIDES.get(code, code)


def time_label(minute: int) -> str:
    return "Прематч" if minute == SH_PAIR_PREMATCH else f"мин {minute}"


def fmt_num(o) -> str:
    if o is None:
        return "—"
    return f"{float(o):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_range(a: float, b: float) -> str:
    return f"{fmt_num(a)}–{fmt_num(b)}"


def fmt_teams(team1: str, team2: str) -> str:
    return f"⚔️<code>{html.escape(team1)} - {html.escape(team2)}</code>"


def _now() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


# --- результат тотала ------------------------------------------------------

def _result(side: str, total: int, line: float) -> tuple[str, int | None, float | None]:
    """(текст результата, won 1/0/None, profit ₽). Пуш на целой линии = Возврат."""
    if total == line:
        return "Возврат", None, 0.0
    over_won = total > line
    won = over_won if side == "over" else not over_won
    if won:
        return "Выигрыш", 1, None            # profit проставим по кф в вызывающем коде
    return "Проигрыш", 0, float(-STAKE)


# --- рендер ----------------------------------------------------------------

def render_signal(sig: dict) -> str:
    league = html.escape(sh_short_league(sig["league"]))
    lines = [
        "🏒 <b>ШОРТ-ХОККЕЙ · ПАРЫ · СИГНАЛ</b>",
        f"🏆 <b>{league}</b>",
        fmt_teams(sig["team1"], sig["team2"]),
        "",
        f"⏱ <b>{time_label(sig['minute'])}</b>",
        f"📊 <b>Счёт {sig['score1']}:{sig['score2']}</b>",
        "",
        f"🎯 <b>Ставка {side_label(sig['side'])} {fmt_num(sig['line'])} "
        f"@{fmt_num(sig['odds'])}</b>",
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
    """Каждый цикл sh_parser для каждого матча шорт-хоккея (в т.ч. прематч)."""
    if sh_league_key(state["league"]) not in _LEAGUE_KEYS:
        return

    # Момент срабатывания: прематч -> сентинел -1; иначе — текущая игровая минута.
    point = SH_PAIR_PREMATCH if state.get("prematch") else state["minute"]

    rules = [r for r in sh_pair_db.get_rules()
             if r["enabled"] and r["minute"] == point]
    if not rules:
        return

    line = markets.get("total_line")
    if line is None:
        return  # рынок тотала на этом снимке ещё не появился — ждём след. цикл

    pair = sh_pair_db.norm_pair(state.get("team1") or "?", state.get("team2") or "?")

    for rule in rules:
        if pair not in sh_pair_db.get_rule_pairs(rule["id"]):
            continue
        if sh_pair_db.signal_exists(rule["id"], state["event_id"]):
            continue
        if not (rule["line_min"] <= line <= rule["line_max"]):
            continue  # линия вне диапазона — ждём след. цикл того же момента
        odds = markets.get(SIDE_FIELD[rule["side"]])
        if odds is None:
            continue
        try:
            _fire(rule, state, line, odds)
        except Exception as e:
            log.warning("fire err rule=%s ev=%s: %s", rule["id"], state["event_id"], e)


def _fire(rule: dict, state: dict, line: float, odds: float):
    chat_id = database.get_chat_id(SH_PAIR_STRAT_CODE)
    fired_minute = SH_PAIR_PREMATCH if state.get("prematch") else state["minute"]
    sig = {
        "rule_id": rule["id"],
        "event_id": state["event_id"],
        "league": state["league"],
        "side": rule["side"],
        "minute": rule["minute"],
        "fired_minute": fired_minute,
        "team1": state.get("team1") or "?",
        "team2": state.get("team2") or "?",
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
        "won": None,
        "final_score": None,
        "final_total": None,
        "profit": None,
        "created_at": _now(),
    }
    if chat_id is not None:
        sig["message_id"] = tg_notify.send(chat_id, render_signal(sig))
        sig["status"] = "sent"
    sid = sh_pair_db.insert_signal(sig)
    if sid is None:
        log.info("dup skipped rule=%s ev=%s", rule["id"], state["event_id"])
    else:
        log.info("SH-PAIR rule=%s ev=%s %s line=%s min=%s odds=%s score=%s:%s chat=%s",
                 rule["id"], state["event_id"], rule["side"], line, rule["minute"],
                 odds, state["score1"], state["score2"], chat_id)


def resolve(event_id: int, s1: int, s2: int, displayed=None):
    """Дорасчёт сигналов матча по ИТОГОВОМУ тоталу основного времени.

    s1:s2 — счёт основного времени (без буллитов/ОТ). displayed — общий счёт с
    буллитом (если был): при расхождении помечаем сигнал как решённый серией."""
    final_score = f"{s1}:{s2}"
    final_total = s1 + s2
    shootout = displayed is not None and tuple(displayed) != (s1, s2)
    try:
        for sig in sh_pair_db.get_signals_for_event(event_id):
            if sig["result"] is not None or sig["line"] is None:
                continue
            result, won, profit = _result(sig["side"], final_total, sig["line"])
            if won == 1 and sig["odds"] is not None:
                profit = STAKE * (float(sig["odds"]) - 1.0)
            sh_pair_db.update_signal_result(sig["id"], result, won, final_score,
                                            final_total, profit)
            if sig["status"] == "sent" and sig["message_id"] and sig["chat_id"] is not None:
                s2d = dict(sig)
                s2d.update(result=result, final_score=final_score,
                           final_total=final_total, shootout=shootout)
                tg_notify.edit(sig["chat_id"], sig["message_id"], render_signal(s2d))
    except Exception as e:
        log.warning("resolve err ev=%s: %s", event_id, e)
