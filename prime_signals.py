"""Движок стратегии PRIME (сигналы ТМ / ИТМ1).

Наборы задаются кнопками бота (prime_db): рынок (ТМ|ИТМ1) + минута + СВОЙ чат
рассылки + СВОЙ график работы по часам + СВОИ дни недели + галочки пар.
Срабатывание: на СТРОГО заданной игровой минуте матча Prime муж, если пара матча
отмечена в наборе (или у набора «все пары») И сейчас попадает в график/дни набора
(rule_active_now) — берём текущую КРАЙНЮЮ линию рынка (та же, что пишет сборщик:
collector.extract_markets) и шлём один сигнал на матч на набор (дедуп через БД) в ЧАТ
НАБОРА (prime_rules.chat_id). Фильтра по значению линии нет. Без чата сигнал пишется
в БД, но не отправляется.

Фильтр темпа (prime_rules.zapas): сигнал, только если счёт / минута × 48 − линия ≤ zapas
(prime_db.calc_pace). Решение принимается ОДИН раз на матч — через SETTLE_SEC после
появления линии на минуте набора (линия на старте перерыва ещё «дрожит»).
«Один сигнал на матч» (one_per_match): не слать, если по матчу в этот же чат уже ушёл
сигнал набора IPBL или другого набора Prime.

На финале — дорасчёт по счёту основного времени:
  ТМ:   зашло, если (s1 + s2) < линия; пуш (Возврат), если равно;
  ИТМ1: зашло, если s1 < линия; пуш, если равно.

Подцеплен к parser.py: process_match() каждый цикл для Prime муж, resolve() на финале.
"""
import html
import logging
import time
from datetime import datetime, timezone, timedelta

import collector
import database
import prime_db
import signals
import tg_notify
from config import PRIME_MARKETS, STAKE

log = logging.getLogger("prime_signals")
MSK = timezone(timedelta(hours=3))

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Скрытое зеркало: сигналы набора, чей чат рассылки = ключ, дублируются ещё и в
# служебный канал (значение). Нигде в UI бота не отображается и не упоминается —
# дубль шлётся и редактируется (итог) параллельно основной рассылке набора.
MIRROR_CHATS = {
    -1002262925865: -1003909646330,
}

# Рынок -> (поле линии, поле кф) в результате collector.extract_markets.
MARKET_FIELD = {
    "tm":  ("total_line", "total_m_odds"),
    "it1": ("it1_line", "it1_m_odds"),
}

# Наборы с фильтром темпа: ждём столько секунд после появления линии на минуте
# набора, потом считаем темп ОДИН раз и больше по этому матчу не пересчитываем.
SETTLE_SEC = 5.0
_first_seen: dict[tuple[int, int], float] = {}   # (набор, матч) -> monotonic первой линии
_decided: set[tuple[int, int]] = set()           # (набор, матч) — решение по матчу принято


def market_label(code: str) -> str:
    return PRIME_MARKETS.get(code, code)


# --- график работы и дни недели набора (свои у каждого набора) --------------

def _parse_windows(s: str | None) -> list[tuple]:
    """'10:00-12:00,16:00-18:00' -> [(time,time), ...] (через signals._parse_hhmm)."""
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if "-" not in part:
            continue
        a, b = part.split("-", 1)
        sa, sb = signals._parse_hhmm(a.strip()), signals._parse_hhmm(b.strip())
        if sa and sb:
            out.append((sa, sb))
    return out


def parse_weekdays(s: str | None) -> set:
    """CSV '0,1,..6' (Пн..Вс) -> множество индексов. Пусто = пусто (= все дни)."""
    days = set()
    for x in (s or "").split(","):
        x = x.strip()
        if x.isdigit() and 0 <= int(x) <= 6:
            days.add(int(x))
    return days


def rule_active_now(rule: dict) -> bool:
    """Набор активен СЕЙЧАС (МСК): день недели в наборе (пусто/все 7 = все дни) И
    время попадает в одно из окон работы (пусто = круглосуточно)."""
    now = signals.msk_now()
    days = parse_weekdays(rule.get("weekdays"))
    if days and len(days) < 7 and now.weekday() not in days:
        return False
    wins = _parse_windows(rule.get("windows"))
    if wins:
        t = now.time()
        if not any(signals._in_interval(t, s, e) for s, e in wins):
            return False
    return True


def fmt_windows(rule: dict) -> str:
    s = rule.get("windows")
    if not s:
        return "круглосуточно"
    return ", ".join(p.strip() for p in s.split(",")) + " МСК"


def fmt_weekdays(rule: dict) -> str:
    days = parse_weekdays(rule.get("weekdays"))
    if not days or len(days) == 7:
        return "все дни"
    return ", ".join(WEEKDAYS_RU[i] for i in sorted(days))


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

def fmt_stats(st: dict) -> str:
    """'📈 Статистика count/roi%/profit' по встречам пары в рынке."""
    return f"📈 <b>Статистика {st['count']}/{st['roi']:.0f}%/{st['profit']:.0f}</b>"


def fmt_pace(v) -> str:
    return f"{float(v):+.1f}".replace(".", ",")


def render_signal(sig: dict) -> str:
    st = prime_db.pair_stats_from_collector(
        sig["market"], sig["team1"], sig["team2"], sig["minute"], sig.get("zapas"))
    lines = [
        "🏀 <b>PRIME · СИГНАЛ</b>",
        fmt_teams(sig["team1"], sig["team2"]),
        fmt_stats(st),
        "",
        f"⏱ <b>Минута {sig['minute']}</b>",
        f"📊 <b>Счёт {sig['score1']}:{sig['score2']}</b>",
    ]
    if sig.get("pace") is not None:
        lines.append(f"📐 <b>Темп {fmt_pace(sig['pace'])}</b> (порог ≤ {fmt_pace(sig['zapas'])})")
    lines += [
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
        if not rule_active_now(rule):
            continue  # вне графика работы / дня недели набора — молчим
        if not rule.get("all_pairs") and pair not in prime_db.get_rule_pairs(rule["id"]):
            continue
        key = (rule["id"], eid)
        if key in _decided or prime_db.signal_exists(rule["id"], eid):
            continue
        if rule.get("one_per_match") and _match_signaled(rule, eid):
            _decided.add(key)
            log.info("PRIME rule=%s ev=%s skip: по матчу уже был сигнал в этот чат", rule["id"], eid)
            continue
        if not markets:
            continue  # рынков нет в этом цикле — попробуем на след., минута ещё та же
        line_f, odds_f = MARKET_FIELD[rule["market"]]
        line = markets.get(line_f)
        odds = markets.get(odds_f)
        if line is None or odds is None:
            continue  # нужный рынок ещё не появился — ждём следующий цикл этой минуты
        pace = None
        if rule.get("zapas") is not None:
            t0 = _first_seen.setdefault(key, time.monotonic())
            if time.monotonic() - t0 < SETTLE_SEC:
                continue  # линия на старте перерыва ещё «дрожит» — ждём
            _decided.add(key)  # темп считаем один раз на матч
            pace = prime_db.calc_pace(rule["market"], state.get("score1"), state.get("score2"),
                                      minute, line)
            if pace is None or pace > rule["zapas"]:
                log.info("PRIME rule=%s ev=%s skip: темп %s > %s (линия %s, счёт %s:%s)",
                         rule["id"], eid, pace, rule["zapas"], line,
                         state.get("score1"), state.get("score2"))
                continue
        try:
            _fire(rule, state, line, odds, pace)
        except Exception as e:
            log.warning("fire err rule=%s ev=%s: %s", rule["id"], eid, e)


def _match_signaled(rule: dict, event_id: int) -> bool:
    """По матчу уже уходил сигнал в чат набора: от набора IPBL или другого набора Prime."""
    chat_id = rule.get("chat_id")
    if chat_id is None:
        return False
    return (database.ipbl_event_sent_to_chat(event_id, chat_id)
            or prime_db.event_signaled_to_chat(event_id, chat_id, rule["id"]))


def _fire(rule: dict, state: dict, line: float, odds: float, pace: float | None = None):
    # чат — СВОЙ у каждого набора (prime_rules.chat_id); без чата сигнал пишется в БД, но не шлётся
    chat_id = rule.get("chat_id")
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
        "pace": pace,
        "zapas": rule.get("zapas") if pace is not None else None,
        "created_at": _now(),
    }
    if chat_id is not None:
        sig["message_id"] = tg_notify.send(chat_id, render_signal(sig))
    sid = prime_db.insert_signal(sig)
    if sid is None:
        log.info("dup skipped rule=%s ev=%s", rule["id"], state["event_id"])
        return
    log.info("PRIME rule=%s ev=%s %s line=%s min=%s odds=%s score=%s:%s chat=%s",
             rule["id"], state["event_id"], rule["market"], line, sig["minute"],
             odds, state["score1"], state["score2"], chat_id)
    # скрытое зеркало набора — дубль в служебный канал (не упоминается в боте)
    mirror = MIRROR_CHATS.get(chat_id)
    if mirror is not None:
        try:
            mid = tg_notify.send(mirror, render_signal(sig))
            prime_db.set_mirror(sid, mirror, mid)
        except Exception as e:
            log.warning("mirror send err sig=%s chat=%s: %s", sid, mirror, e)


def resolve(event_id: int, s1: int, s2: int):
    """Дорасчёт сигналов матча по счёту основного времени."""
    final_score = f"{s1}:{s2}"
    final_total = s1 + s2
    for key in [k for k in _first_seen if k[1] == event_id]:
        _first_seen.pop(key, None)
    for key in [k for k in _decided if k[1] == event_id]:
        _decided.discard(key)
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
            # скрытое зеркало — правим итог той же копии в служебном канале
            if sig.get("mirror_message_id") and sig.get("mirror_chat_id") is not None:
                m2d = dict(sig)
                m2d.update(result=result, final_score=final_score, final_total=final_total)
                tg_notify.edit(sig["mirror_chat_id"], sig["mirror_message_id"],
                               render_signal(m2d))
    except Exception as e:
        log.warning("resolve err ev=%s: %s", event_id, e)
