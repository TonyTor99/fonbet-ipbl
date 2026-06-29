"""Движок стратегий IPBL-баскетбол.

Срабатывание — на большом перерыве (таймер замер на 1200/1440).
  signal_tm  : если 2*(сумма очков к перерыву) - линия_ТМ <= -16 -> сигнал ТМ.
  prime_info : по дивизиону Prime — безусловное инфо-уведомление в перерыве.

Линия = ТМ матча с самым высоким кф в [KF_MIN, KF_MAX] (root customFactors).
Один сигнал на матч на стратегию (дедуп через БД).
Расписание и chat_id — на стратегию, из БД (кнопки бота). Вне окна работы не шлём.
"""
import html
import logging
from datetime import datetime, time as dtime, timezone, timedelta

import database
import tg_notify
from config import (STRATEGIES, KF_MIN, KF_MAX, THRESHOLD, STAKE)

log = logging.getLogger("signals")

MSK = timezone(timedelta(hours=3))   # расписание стратегий — по Москве (UTC+3)


def msk_now() -> datetime:
    return datetime.now(MSK)

# event_id -> состояние в памяти процесса
_state: dict[int, dict] = {}


def _new_state() -> dict:
    return {"tm_done": False, "info_done": False, "line_hist": []}


# --- расписание ------------------------------------------------------------

def _parse_hhmm(s: str | None) -> dtime | None:
    if not s:
        return None
    try:
        h, m = s.split(":")
        return dtime(int(h), int(m))
    except Exception:
        return None


def _in_interval(now: dtime, start: dtime, end: dtime) -> bool:
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end          # окно через полночь (22:00-06:00)


def in_schedule(strategy: str) -> bool:
    """True если сейчас попадает в любое из окон работы (или окон нет = круглосуточно)."""
    wins = database.get_windows(strategy)
    if not wins:
        return True
    now = msk_now().time()
    for s, e in wins:
        st, en = _parse_hhmm(s), _parse_hhmm(e)
        if st and en and _in_interval(now, st, en):
            return True
    return False


def fmt_windows(strategy: str) -> str:
    """Окна для показа: '10:00–12:00, 16:00–18:00 МСК' или 'круглосуточно'."""
    wins = database.get_windows(strategy)
    if not wins:
        return "круглосуточно"
    return ", ".join(f"{s}–{e}" for s, e in wins) + " МСК"


def window_status(strategy: str) -> str:
    """Статус сейчас: '🟢 ищет ...' / '⏸ пауза (след. 16:00)'."""
    wins = database.get_windows(strategy)
    if not wins:
        return "🟢 ищет (круглосуточно)"
    now = msk_now().time()
    for s, e in wins:
        st, en = _parse_hhmm(s), _parse_hhmm(e)
        if st and en and _in_interval(now, st, en):
            return f"🟢 ищет (до {e})"
    future = sorted(s for s, _ in wins if _parse_hhmm(s) and _parse_hhmm(s) > now)
    nxt = future[0] if future else sorted(s for s, _ in wins)[0]
    return f"⏸ пауза (след. {nxt})"


# --- выбор линии -----------------------------------------------------------

def pick_tm_line(totals: list[dict]) -> dict | None:
    """Линия сигнала = ТМ с самым высоким кф в [KF_MIN, KF_MAX].
    Если в этом диапазоне линий нет — фоллбэк: ТМ с самым высоким доступным кф
    (нижняя линия блока, «ровная», которую Fonbet чуть недотянул до 1.95)."""
    tm = [t for t in totals if t["side"] == "ТМ"]
    if not tm:
        return None
    in_range = [t for t in tm if KF_MIN <= t["odds"] <= KF_MAX]
    if in_range:
        return max(in_range, key=lambda t: t["odds"])
    return max(tm, key=lambda t: t["odds"])


# --- форматирование --------------------------------------------------------

def fmt_line(line: float | None) -> str:
    if line is None:
        return "—"
    if line == int(line):
        return str(int(line))
    return f"{line:.1f}".replace(".", ",")


def fmt_odds(o) -> str:
    if o is None:
        return "—"
    return f"{float(o):.2f}".rstrip("0").rstrip(".").replace(".", ",")


# Варианты с точкой (для строки ставки «ТМ» и «Запас» — по эталону клиента).
def fmt_line_dot(line: float | None) -> str:
    if line is None:
        return "—"
    if line == int(line):
        return str(int(line))
    return f"{line:.1f}"


def fmt_odds_dot(o) -> str:
    if o is None:
        return "—"
    return f"{float(o):.2f}".rstrip("0").rstrip(".")


def fmt_signed1_dot(v) -> str:
    return f"{float(v):.1f}"


def fmt_quarters(quarters: list) -> str:
    """Кварталы для строки 🔢: '18:29 | 18:18'."""
    return " | ".join(f"{a}:{b}" for a, b in quarters[:2])


def fmt_league(name: str) -> str:
    """'Россия. IPBL. Pro Division' -> 'Россия • IPBL Pro Division'."""
    parts = [p.strip() for p in name.split(".") if p.strip()]
    if not parts:
        return name
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} • {' '.join(parts[1:])}"


def fmt_signed1(v) -> str:
    """Запас: '-19.5' -> '-19,5'."""
    return f"{float(v):.1f}".replace(".", ",")


def fmt_profit(v) -> str:
    """Прибыль без валюты: '+950', '-1000'."""
    return f"{float(v):+.0f}"


def quarter_str(quarters: list, idx: int) -> str | None:
    """Счёт четверти idx (0-based) как '24:24' или None, если четверти ещё нет."""
    if 0 <= idx < len(quarters):
        a, b = quarters[idx]
        return f"{a}:{b}"
    return None


def prematch_line(hist: list) -> float | None:
    """Первая увиденная (лайв-старт) линия ТМ или None."""
    for v in hist:
        if v is not None:
            return v
    return None


def fmt_line_move(hist: list, current: float | None) -> str:
    """Движение линии ставки: '188,5 → 185,5'. Без движения — одно значение."""
    vals = [h for h in hist if h is not None]
    start = vals[0] if vals else None
    if start is not None and current is not None and start != current:
        return f"{fmt_line(start)} → {fmt_line(current)}"
    if current is not None:
        return fmt_line(current)
    return "—"


def fmt_totals_snapshot(totals: list[dict]) -> str:
    """Весь блок ТМ на перерыве: '219.5@2.1|220.5@1.87|221.5@1.68'."""
    tm = sorted((t for t in totals if t["side"] == "ТМ"), key=lambda t: t["line"])
    return "|".join(f"{fmt_line(t['line'])}@{fmt_odds(t['odds'])}" for t in tm)


def fmt_teams(team1: str, team2: str) -> str:
    """Обе команды в одном <code> — копируются одним тапом в Telegram."""
    return f"⚔️<code>{html.escape(team1)} - {html.escape(team2)}</code>"


def render_signal(sig: dict) -> str:
    league = html.escape(fmt_league(sig["league"]))
    lines = [
        "🏀 <b>ТМ СИГНАЛ</b>",
        f"🏆 <b>{league}</b>",
        fmt_teams(sig["team1"], sig["team2"]),
        "",
        "⏸️ <b>Перерыв</b>",
        f"📊 <b>Счёт {sig['fixed_score1']}:{sig['fixed_score2']}</b>",
    ]
    if sig.get("fixed_quarters"):
        lines.append(f"🔢 {html.escape(sig['fixed_quarters'])}")
    lines.append("")
    odds_part = f" @{fmt_odds_dot(sig['odds'])}" if sig.get("odds") is not None else ""
    lines.append(f"🎯 <b>ТМ {fmt_line_dot(sig['line'])}{odds_part}</b>")
    if sig.get("line_move"):
        lines.append(f"📈 Линия: {html.escape(sig['line_move'])}")
    if sig.get("formula_value") is not None:
        lines.append(f"🧮 <b>Запас:  {fmt_signed1_dot(sig['formula_value'])}</b>")
    if sig.get("final_score"):
        lines.append("")
        tot = sig.get("final_total")
        tot_part = f"  ({tot})" if tot is not None else ""
        lines.append(f"🏁 <b>Итог: {html.escape(str(sig['final_score']))}{tot_part}</b>")
        if sig.get("result") == "Выигрыш":
            lines.append(f"✅ <b>Выигрыш</b>  {fmt_profit(sig['profit'])}")
        elif sig.get("result") == "Проигрыш":
            lines.append(f"❌ <b>Проигрыш</b>  {fmt_profit(sig['profit'])}")
    return "\n".join(lines)


def render_info(sig: dict) -> str:
    league = html.escape(fmt_league(sig["league"]))
    lines = [
        "🏀 <b>PRIME • ПЕРЕРЫВ</b>",
        f"🏆 <b>{league}</b>",
        fmt_teams(sig["team1"], sig["team2"]),
        "",
        "⏸️ <b>Перерыв</b>",
        f"📊 <b>Счёт {sig['fixed_score1']}:{sig['fixed_score2']}</b>",
    ]
    if sig.get("fixed_quarters"):
        lines.append(f"🔢 {html.escape(sig['fixed_quarters'])}")
    lines.append("")
    if sig.get("line") is not None:
        odds_part = f" @{fmt_odds_dot(sig['odds'])}" if sig.get("odds") is not None else ""
        lines.append(f"🎯 <b>ТМ {fmt_line_dot(sig['line'])}{odds_part}</b>")
    if sig.get("line_move"):
        lines.append(f"📈 Линия: {html.escape(sig['line_move'])}")
    if sig.get("formula_value") is not None:
        lines.append(f"🧮 <b>Запас:  {fmt_signed1_dot(sig['formula_value'])}</b>")
    return "\n".join(lines)


# --- отправка --------------------------------------------------------------

def _base_sig(strategy: str, st: dict, state: dict) -> dict:
    return {
        "strategy": strategy,
        "event_id": state["event_id"],
        "league": state["league"],
        "division": state["division"],
        "team1": state["team1"],
        "team2": state["team2"],
        "side": "ТМ",
        "line": None,
        "odds": None,
        "half_total": state["half_total"],
        "formula_value": None,
        "qualified": 0,
        "in_window": 1,
        "totals_snapshot": fmt_totals_snapshot(state["totals"]),
        "fixed_score1": state["score1"],
        "fixed_score2": state["score2"],
        "fixed_quarters": fmt_quarters(state["quarters"]),
        "q1": quarter_str(state["quarters"], 0),   # Q1 — известен на сигнале
        "q2": quarter_str(state["quarters"], 1),   # Q2 — известен на сигнале
        "q3": None,                                # Q3/Q4 — заполнятся на финале
        "q4": None,
        "line_move": None,
        "line_prematch": prematch_line(st["line_hist"]),
        "chat_id": None,
        "message_id": None,
        "status": "not_sent",
        "result": None,
        "final_score": None,
        "final_total": None,
        "profit": None,
        "created_at": msk_now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _store_and_send(sig: dict, render_fn):
    chat_id = database.get_chat_id(sig["strategy"])
    sig["chat_id"] = chat_id
    if chat_id is not None:
        sig["status"] = "sent" if sig["strategy"] != "prime_info" else "info"
        sig["message_id"] = tg_notify.send(chat_id, render_fn(sig))
    sid = database.insert_signal(sig)
    if sid is None:
        log.info("dup skipped %s ev=%s", sig["strategy"], sig["event_id"])
    else:
        log.info("SIGNAL %s ev=%s line=%s odds=%s score=%s:%s chat=%s",
                 sig["strategy"], sig["event_id"], sig["line"], sig["odds"],
                 sig["fixed_score1"], sig["fixed_score2"], chat_id)
    return sid


# --- стратегии -------------------------------------------------------------

def _process_signal_tm(st: dict, state: dict):
    """На перерыве пишем строку для КАЖДОГО матча (с тоталами), независимо от условия.
    В Telegram уходит только прошедший формулу (qualified) и только в окне работы."""
    if st["tm_done"] or database.signal_exists("signal_tm", state["event_id"]):
        st["tm_done"] = True
        return
    tm_block = [t for t in state["totals"] if t["side"] == "ТМ"]
    if not tm_block:
        return  # тоталов ещё нет — ждём следующий цикл внутри перерыва (~2 мин)

    chosen = pick_tm_line(state["totals"])
    line = chosen["line"] if chosen else None
    odds = chosen["odds"] if chosen else None
    formula = (2 * state["half_total"] - line) if line is not None else None
    qualified = 1 if (formula is not None and formula <= THRESHOLD) else 0
    active = in_schedule("signal_tm")   # стратегия в окне работы?

    sig = _base_sig("signal_tm", st, state)
    sig["line"] = line
    sig["odds"] = odds
    sig["formula_value"] = formula
    sig["line_move"] = fmt_line_move(st["line_hist"], line)
    sig["qualified"] = qualified
    sig["in_window"] = 1 if active else 0   # пауза → в статистику не идёт

    # отправляем в TG только прошедший формулу, в окне работы, при заданном чате
    if qualified and active:
        _store_and_send(sig, render_signal)
    else:
        sig["status"] = "skipped"   # снимок перерыва без отправки (анализ)
        database.insert_signal(sig)
        log.info("snapshot signal_tm ev=%s line=%s formula=%s qualified=%s in_window=%s",
                 state["event_id"], line, formula, qualified, sig["in_window"])
    st["tm_done"] = True


def _process_prime_info(st: dict, state: dict):
    # только МУЖСКАЯ Prime (женская — в названии "Женщины" — не нужна)
    if state["division"] != "prime" or "Женщин" in state["league"]:
        return
    if st["info_done"] or database.signal_exists("prime_info", state["event_id"]):
        st["info_done"] = True
        return
    if not in_schedule("prime_info"):
        return
    chosen = pick_tm_line(state["totals"])
    sig = _base_sig("prime_info", st, state)
    line = chosen["line"] if chosen else None
    if chosen is not None:
        sig["line"] = line
        sig["odds"] = chosen["odds"]
        sig["formula_value"] = 2 * state["half_total"] - line
    # движение линии ставки за матч
    sig["line_move"] = fmt_line_move(st["line_hist"], line)
    _store_and_send(sig, render_info)
    st["info_done"] = True


# --- точки входа -----------------------------------------------------------

def process_match(state: dict):
    """Вызывается каждый цикл парсера для каждого live IPBL-матча.

    state: event_id, league, division, team1, team2, score1, score2, ts, comment,
           quarters (list[(a,b)]), totals (list[{side,line,odds}]), at_break (bool),
           half_total (int, валиден на перерыве)."""
    eid = state["event_id"]
    st = _state.get(eid)
    if st is None:
        st = _state[eid] = _new_state()

    # копим историю линии ставки для движения (все дивизионы, до и в перерыве)
    chosen = pick_tm_line(state["totals"])
    st["line_hist"].append(chosen["line"] if chosen else None)

    if not state.get("at_break"):
        return

    try:
        _process_signal_tm(st, state)
    except Exception as e:
        log.warning("signal_tm err ev=%s: %s", eid, e)
    try:
        _process_prime_info(st, state)
    except Exception as e:
        log.warning("prime_info err ev=%s: %s", eid, e)


def resolve(event_id: int, final_score: str, final_total: int, quarters: list | None = None):
    """Дорасчёт итогов сигналов ТМ и редактирование сообщений.
    quarters — полный список четвертей из итогового comment (для Q3/Q4)."""
    q3 = quarter_str(quarters or [], 2)
    q4 = quarter_str(quarters or [], 3)
    try:
        for sig in database.get_signals_for_event(event_id):
            if sig["strategy"] != "signal_tm":
                continue
            if sig["result"] is not None or sig["line"] is None:
                continue
            won = final_total < sig["line"]
            result = "Выигрыш" if won else "Проигрыш"
            # прибыль — только по прошедшим формулу в окне работы; снимки/паузы — без прибыли
            if sig["qualified"] and sig["in_window"]:
                profit = STAKE * (sig["odds"] - 1) if won else -STAKE
            else:
                profit = None
            database.update_signal_result(sig["id"], result, final_score, final_total, profit, q3, q4)
            if sig["status"] == "sent" and sig["message_id"] and sig["chat_id"] is not None:
                s2 = dict(sig)
                s2.update(result=result, final_score=final_score,
                          final_total=final_total, profit=profit)
                tg_notify.edit(sig["chat_id"], sig["message_id"], render_signal(s2))
    except Exception as e:
        log.warning("resolve err ev=%s: %s", event_id, e)
    finally:
        _state.pop(event_id, None)
