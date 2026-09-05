"""Движок стратегии ЧЕТВЕРТИ Pro Жен (тотал ТБ/ТМ текущей четверти по парам).

Наборы задаются кнопками бота (pq_db): сторона (ТБ|ТМ) + несколько игровых минут
(напр. 7,17,27) + галочки пар. Пары берутся из сборщика четвертей Pro жен.

Срабатывание: на КАЖДОЙ заданной минуте матча Pro жен по отмеченной паре берём
тотал ТЕКУЩЕЙ четверти (quarter = minute // PQ_QUARTER_MIN + 1) — линию и кф из
того же дочернего события, что пишет сборщик четвертей (collector_periods) — и
шлём один сигнал на (набор, матч, минута) в чат стратегии (config.PQ_STRAT_CODE).
Фильтра линии нет.

На финале — дорасчёт по счёту ЭТОЙ четверти (quarters[q-1]):
  ТБ (over):  зашло, если (q1+q2) > линия; пуш (Возврат) на целой линии;
  ТМ (under): зашло, если (q1+q2) < линия; пуш на целой линии.

Подцеплен к parser.py: process_match() каждый цикл для Pro жен, resolve() на финале.
"""
import html
import logging
from datetime import datetime, timezone, timedelta

import collector_periods
import database
import pq_db
import tg_notify
from config import (PQ_STRAT_CODE, PQ_SIDES, PQ_QUARTER_MIN, STAKE)

log = logging.getLogger("pq_signals")
MSK = timezone(timedelta(hours=3))

# Сторона правила -> поле кф в результате collector_periods._extract_period_markets.
SIDE_FIELD = {"over": "total_b_odds", "under": "total_m_odds"}


def side_label(code: str) -> str:
    return PQ_SIDES.get(code, code)


def fmt_num(o) -> str:
    if o is None:
        return "—"
    return f"{float(o):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_teams(team1: str, team2: str) -> str:
    return f"⚔️<code>{html.escape(team1)} - {html.escape(team2)}</code>"


def _now() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


# --- результат тотала четверти ---------------------------------------------

def _result(side: str, q_total: int, line: float) -> tuple[str, int | None, float | None]:
    """(текст, won 1/0/None, profit ₽). Пуш на целой линии = Возврат."""
    if q_total == line:
        return "Возврат", None, 0.0
    over_won = q_total > line
    won = over_won if side == "over" else not over_won
    if won:
        return "Выигрыш", 1, None            # profit проставим по кф в вызывающем коде
    return "Проигрыш", 0, float(-STAKE)


# --- рендер ----------------------------------------------------------------

def render_signal(sig: dict) -> str:
    lines = [
        "🏀 <b>ЧЕТВЕРТИ Pro Ж · СИГНАЛ</b>",
        fmt_teams(sig["team1"], sig["team2"]),
        "",
        f"⏱ <b>Минута {sig['minute']} · {sig['quarter']}-я четверть</b>",
        f"📊 <b>Счёт матча {sig['score1']}:{sig['score2']}</b>",
    ]
    if sig.get("q_live"):
        lines.append(f"🔢 <b>Счёт четверти {sig['q_live']}</b>")
    lines += [
        "",
        f"🎯 <b>Ставка {side_label(sig['side'])} {fmt_num(sig['line'])} @{fmt_num(sig['odds'])}</b> "
        f"(четверть)",
    ]
    if sig.get("q_final"):
        lines.append(f"🏁 <b>Итог четверти: {html.escape(str(sig['q_final']))}</b> "
                     f"(тотал {sig.get('q_total', '—')})")
        if sig.get("result") == "Выигрыш":
            lines.append("✅ <b>Выигрыш</b>")
        elif sig.get("result") == "Проигрыш":
            lines.append("❌ <b>Проигрыш</b>")
        elif sig.get("result") == "Возврат":
            lines.append("↩️ <b>Возврат</b>")
    return "\n".join(lines)


# --- точки входа -----------------------------------------------------------

def process_match(state: dict, api_data):
    """Каждый цикл parser для live-матча Pro жен."""
    eid = state["event_id"]
    minute = (state.get("ts") or 0) // 60

    rules = [r for r in pq_db.get_rules()
             if r["enabled"] and minute in pq_db.minutes_list(r)]
    if not rules:
        return

    quarter = minute // PQ_QUARTER_MIN + 1     # текущая четверть по игровому таймеру
    blocks = dict(collector_periods._period_blocks(api_data, eid))   # {номер_четверти: факторы}
    factors = blocks.get(quarter)
    if not factors:
        return  # рынков текущей четверти нет в этом цикле — попробуем на следующем

    markets = collector_periods._extract_period_markets(factors)
    line = markets.get("total_line")
    if line is None:
        return  # тотал этой четверти ещё не котируется

    quarters = state.get("quarters") or []
    q_live = None
    if 1 <= quarter <= len(quarters):
        ql1, ql2 = quarters[quarter - 1]
        q_live = f"{ql1}:{ql2}"

    pair = pq_db.norm_pair(state.get("team1") or "?", state.get("team2") or "?")

    for rule in rules:
        if pair not in pq_db.get_rule_pairs(rule["id"]):
            continue
        if pq_db.signal_exists(rule["id"], eid, minute):
            continue
        odds = markets.get(SIDE_FIELD[rule["side"]])
        if odds is None:
            continue
        try:
            _fire(rule, state, minute, quarter, line, odds, q_live)
        except Exception as e:
            log.warning("fire err rule=%s ev=%s: %s", rule["id"], eid, e)


def _fire(rule: dict, state: dict, minute: int, quarter: int, line: float,
          odds: float, q_live):
    chat_id = database.get_chat_id(PQ_STRAT_CODE)
    sig = {
        "rule_id": rule["id"],
        "event_id": state["event_id"],
        "league": state["league"],
        "side": rule["side"],
        "minute": minute,
        "quarter": quarter,
        "team1": state.get("team1") or "?",
        "team2": state.get("team2") or "?",
        "line": line,
        "odds": odds,
        "score1": state["score1"],
        "score2": state["score2"],
        "q_live": q_live,
        "chat_id": chat_id,
        "message_id": None,
        "status": "no_chat",
        "result": None,
        "won": None,
        "q_final": None,
        "q_total": None,
        "profit": None,
        "created_at": _now(),
    }
    if chat_id is not None:
        sig["message_id"] = tg_notify.send(chat_id, render_signal(sig))
        sig["status"] = "sent"
    sid = pq_db.insert_signal(sig)
    if sid is None:
        log.info("dup skipped rule=%s ev=%s min=%s", rule["id"], state["event_id"], minute)
    else:
        log.info("PQ rule=%s ev=%s %s Q%s min=%s line=%s odds=%s chat=%s",
                 rule["id"], state["event_id"], rule["side"], quarter, minute,
                 line, odds, chat_id)


def resolve(event_id: int, quarters: list[tuple[int, int]], s1: int, s2: int):
    """Дорасчёт сигналов матча по счёту ИХ четверти.

    quarters — счёт по четвертям из comment: [(24,24),(31,32),...]."""
    try:
        for sig in pq_db.get_signals_for_event(event_id):
            if sig["result"] is not None or sig["line"] is None:
                continue
            q = sig["quarter"]
            if q < 1 or q > len(quarters):
                continue  # счёта этой четверти нет — оставляем без итога
            q1, q2 = quarters[q - 1]
            q_total = q1 + q2
            q_final = f"{q1}:{q2}"
            result, won, profit = _result(sig["side"], q_total, sig["line"])
            if won == 1 and sig["odds"] is not None:
                profit = STAKE * (float(sig["odds"]) - 1.0)
            pq_db.update_signal_result(sig["id"], result, won, q_final, q_total, profit)
            if sig["status"] == "sent" and sig["message_id"] and sig["chat_id"] is not None:
                s2d = dict(sig)
                s2d.update(result=result, q_final=q_final, q_total=q_total)
                tg_notify.edit(sig["chat_id"], sig["message_id"], render_signal(s2d))
    except Exception as e:
        log.warning("resolve err ev=%s: %s", event_id, e)
