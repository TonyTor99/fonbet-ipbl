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
from config import (STRATEGIES, KF_MIN, KF_MAX, STAKE, IPBL_DIV_BY_SPORT)

log = logging.getLogger("signals")

MSK = timezone(timedelta(hours=3))   # расписание стратегий — по Москве (UTC+3)

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def msk_now() -> datetime:
    return datetime.now(MSK)

# event_id -> состояние в памяти процесса
_state: dict[int, dict] = {}


def _new_state() -> dict:
    return {"tm_done": False, "info_done": False, "rules_done": False, "line_hist": []}


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


# --- расписание/дни недели НАБОРА IPBL (свои у каждого набора) --------------

def _parse_windows_str(s: str | None) -> list[tuple]:
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if "-" not in part:
            continue
        a, b = part.split("-", 1)
        sa, sb = _parse_hhmm(a.strip()), _parse_hhmm(b.strip())
        if sa and sb:
            out.append((sa, sb))
    return out


def parse_weekdays(s: str | None) -> set:
    """CSV '0,1,..6' (Пн..Вс) -> множество индексов."""
    days = set()
    for x in (s or "").split(","):
        x = x.strip()
        if x.isdigit() and 0 <= int(x) <= 6:
            days.add(int(x))
    return days


def ipbl_rule_active_now(rule: dict) -> bool:
    """Набор активен СЕЙЧАС (МСК): день недели в наборе (пусто/все 7 = все дни) И
    время в одном из окон работы (пусто = круглосуточно)."""
    now = msk_now()
    days = parse_weekdays(rule.get("weekdays"))
    if days and len(days) < 7 and now.weekday() not in days:
        return False
    wins = _parse_windows_str(rule.get("windows"))
    if wins:
        t = now.time()
        if not any(_in_interval(t, s, e) for s, e in wins):
            return False
    return True


def fmt_rule_windows(rule: dict) -> str:
    s = rule.get("windows")
    if not s:
        return "круглосуточно"
    return ", ".join(p.strip() for p in s.split(",")) + " МСК"


def fmt_rule_weekdays(rule: dict) -> str:
    days = parse_weekdays(rule.get("weekdays"))
    if not days or len(days) == 7:
        return "все дни"
    return ", ".join(WEEKDAYS_RU[i] for i in sorted(days))


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
    if sig.get("final_score"):
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
        "muted": 0,
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


def _store_and_send(sig: dict, render_fn, muted: bool = False):
    """Пишет сигнал в БД. Если muted (лига выключена кнопкой) — строка сохраняется
    как обычно, но в Telegram НЕ уходит (status='muted', в статистику не идёт)."""
    chat_id = database.get_chat_id(sig["strategy"])
    sig["chat_id"] = chat_id
    if muted:
        sig["status"] = "muted"        # лига выключена — фиксируем, но не шлём
    elif chat_id is not None:
        sig["status"] = "sent" if sig["strategy"] != "prime_info" else "info"
        sig["message_id"] = tg_notify.send(chat_id, render_fn(sig))
    sid = database.insert_signal(sig)
    if sid is None:
        log.info("dup skipped %s ev=%s", sig["strategy"], sig["event_id"])
    else:
        log.info("SIGNAL %s ev=%s line=%s odds=%s score=%s:%s chat=%s muted=%s",
                 sig["strategy"], sig["event_id"], sig["line"], sig["odds"],
                 sig["fixed_score1"], sig["fixed_score2"], chat_id, muted)
    return sid


# --- стратегии -------------------------------------------------------------

def _process_signal_tm(st: dict, state: dict):
    """СНИМОК перерыва: на большом перерыве пишем строку для КАЖДОГО матча (с линией
    ТМ, кф и формулой) в таблицу signals — это историческая база («сборщик») для
    гипотетической статистики наборов. В Telegram отсюда НЕ шлём: рассылка — через
    наборы (_process_ipbl_rules). Один снимок на матч (дедуп по strategy+event_id)."""
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

    sig = _base_sig("signal_tm", st, state)
    sig["line"] = line
    sig["odds"] = odds
    sig["formula_value"] = formula
    sig["line_move"] = fmt_line_move(st["line_hist"], line)
    sig["qualified"] = 0
    sig["in_window"] = 1
    sig["muted"] = 0
    sig["status"] = "snapshot"
    database.insert_signal(sig)
    log.info("snapshot signal_tm ev=%s line=%s formula=%s", state["event_id"], line, formula)
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
    sig["muted"] = 0
    _store_and_send(sig, render_info)
    st["info_done"] = True


# --- наборы IPBL (сигнал ТМ по условиям набора в свой чат) ------------------

def render_ipbl_rule_signal(s: dict) -> str:
    """Текст сигнала набора — тот же формат «ТМ СИГНАЛ», что и у старой стратегии."""
    d = {
        "league": s["league"], "team1": s["team1"], "team2": s["team2"],
        "fixed_score1": s["score1"], "fixed_score2": s["score2"],
        "fixed_quarters": s.get("quarters"), "line": s["line"], "odds": s["odds"],
        "final_score": s.get("final_score"), "final_total": s.get("final_total"),
        "result": s.get("result"), "profit": s.get("profit"),
    }
    return render_signal(d)


def _process_ipbl_rules(st: dict, state: dict, line, odds, formula):
    """На перерыве: считаем формулу ОДИН раз — на первом цикле перерыва, где уже есть
    линия ТМ (снимок начала перерыва). Прогоняем по этому снимку все наборы разом: если
    дивизион матча включён (запас задан), формула проходит запас набора этого дивизиона,
    пара проходит белый+чёрный список и сейчас график/дни набора — сразу шлём сигнал ТМ
    в чат набора. После этого ставим флаг rules_done и больше на этом перерыве НЕ
    пересчитываем (иначе строгий набор «догонял» бы сползающую линию и уходил с задержкой).
    Дедуп по (набор, матч)."""
    if st.get("rules_done"):
        return                                     # уже посчитали на этом перерыве
    if line is None or formula is None:
        return                                     # тоталов ещё нет — ждём следующий цикл
    eid = state["event_id"]
    div = IPBL_DIV_BY_SPORT.get(state.get("sport_id"))
    if div is None:
        return
    pair = database._ipbl_norm(state["team1"], state["team2"])
    for rule in database.ipbl_get_rules():
        if not rule["enabled"] or rule["chat_id"] is None:
            continue
        zap = rule.get(f"zapas_{div}")
        if zap is None or formula > zap:
            continue                               # дивизион выключен / формула не прошла
        if not database.ipbl_pair_allowed(rule["id"], div, pair):
            continue
        if not ipbl_rule_active_now(rule):
            continue                               # вне графика/дней набора
        if database.ipbl_sent_exists(rule["id"], eid):
            continue
        try:
            _fire_ipbl_rule(rule, state, div, line, odds, formula)
        except Exception as e:
            log.warning("ipbl rule fire err rule=%s ev=%s: %s", rule["id"], eid, e)
    st["rules_done"] = True                        # снимок перерыва зафиксирован


def _fire_ipbl_rule(rule: dict, state: dict, div: str, line, odds, formula):
    chat_id = rule["chat_id"]
    sig = {
        "rule_id": rule["id"], "event_id": state["event_id"],
        "league": state["league"], "div": div,
        "team1": state["team1"], "team2": state["team2"],
        "line": line, "odds": odds, "formula_value": formula,
        "score1": state["score1"], "score2": state["score2"],
        "quarters": fmt_quarters(state["quarters"]),
        "chat_id": chat_id, "message_id": None, "status": "sent",
        "result": None, "final_score": None, "final_total": None, "profit": None,
        "created_at": msk_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    sig["message_id"] = tg_notify.send(chat_id, render_ipbl_rule_signal(sig))
    sid = database.ipbl_insert_sent(sig)
    if sid is None:
        log.info("ipbl dup skipped rule=%s ev=%s", rule["id"], state["event_id"])
    else:
        log.info("IPBL rule=%s ev=%s div=%s line=%s odds=%s formula=%s chat=%s",
                 rule["id"], state["event_id"], div, line, odds, formula, chat_id)


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

    # линия/кф/формула на перерыве — считаем один раз для снимка и наборов
    line = chosen["line"] if chosen else None
    odds = chosen["odds"] if chosen else None
    formula = (2 * state["half_total"] - line) if line is not None else None

    try:
        _process_signal_tm(st, state)        # снимок в историю (signals) — без отправки
    except Exception as e:
        log.warning("signal_tm snapshot err ev=%s: %s", eid, e)
    try:
        _process_ipbl_rules(st, state, line, odds, formula)   # рассылка по наборам
    except Exception as e:
        log.warning("ipbl rules err ev=%s: %s", eid, e)
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

    # дорасчёт отправленных сигналов НАБОРОВ IPBL + правка их сообщений
    try:
        for s in database.ipbl_get_sent_for_event(event_id):
            if s["result"] is not None or s["line"] is None:
                continue
            won = final_total < s["line"]
            result = "Выигрыш" if won else "Проигрыш"
            profit = (STAKE * (float(s["odds"]) - 1) if won else -STAKE) if s["odds"] is not None else None
            database.ipbl_update_sent_result(s["id"], result, final_score, final_total, profit)
            if s["status"] == "sent" and s["message_id"] and s["chat_id"] is not None:
                d = dict(s)
                d.update(result=result, final_score=final_score, final_total=final_total, profit=profit)
                tg_notify.edit(s["chat_id"], s["message_id"], render_ipbl_rule_signal(d))
    except Exception as e:
        log.warning("ipbl resolve err ev=%s: %s", event_id, e)
    finally:
        _state.pop(event_id, None)
