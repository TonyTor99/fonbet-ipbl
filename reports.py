"""Отчёты о прибыли в процентах (недельный и месячный).

Процент считается от фиксированного стартового банка (config.BANKROLL_START):
  день  % = сумма_прибыли_дня / BANKROLL_START * 100
  неделя % = сумма всех дней (= сумма_прибыли_недели / BANKROLL_START * 100)

Формат сообщений — по эталону клиента:
  Сигнал ТМ
  Всем доброго дня!
  За прошедшую неделю прибыль составила -3.55%
  20.07 ♻️0.00%
  21.07 ✖️-6.00%
  ...
Эмодзи по знаку дня: ♻️ = 0.00%, ✅ = плюс, ✖️ = минус. Знак «+» у плюса не пишем.
"""
from datetime import datetime, timedelta, timezone, date

import database
import prime_db
import sh_pair_db
import pq_db
import cage_strat_db
import prime_women_db
import pro_strat_db
from config import BANKROLL_START, IPBL_DIV_LABELS, IPBL_DIV_ORDER

MSK = timezone(timedelta(hours=3))

# Шапка публикации (стратегия ТМ).
REPORT_HEADER = "Сигнал ТМ"
GREETING = "Всем доброго дня!"


def _pct(profit: float) -> float:
    return profit / BANKROLL_START * 100.0


def _counts_line(c: dict) -> str:
    """'7✅/2✖️/0♻️' — счётчики исходов дня (выигрыши/проигрыши/возвраты),
    единый формат для всех стратегий (как в отчёте IPBL)."""
    return f"{c['wins']}✅/{c['losses']}✖️/{c['pushes']}♻️"


def _day_line(d: date, profit: float) -> str:
    """'20.07 ✖️-6.00%' — дата, эмодзи по знаку, процент без знака «+»."""
    p = _pct(profit)
    r = round(p, 2)
    if r > 0:
        emoji = "✅"
    elif r < 0:
        emoji = "✖️"
    else:
        emoji = "♻️"
    return f"{d.strftime('%d.%m')} {emoji}{p:.2f}%"


# --- вычисление периодов ---------------------------------------------------

def last_week_range(today: date) -> tuple[date, date]:
    """Прошедшая полная неделя (Пн–Вс) относительно `today`.
    В понедельник (авто-отправка) это неделя, что закончилась вчера."""
    monday_this = today - timedelta(days=today.weekday())
    monday_last = monday_this - timedelta(days=7)
    return monday_last, monday_last + timedelta(days=6)


def last_month_range(today: date) -> tuple[date, date]:
    """Прошедший календарный месяц относительно `today`."""
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return last_prev.replace(day=1), last_prev


# --- сборка текстов --------------------------------------------------------

def build_daily_text(now: datetime | None = None, day: date | None = None) -> str:
    """Дневной отчёт IPBL за один день (по умолчанию — вчера, как при авто-отправке
    в 09:00). Формат: заголовок с датой, счётчики исходов ✅/✖️/♻️, прибыль в %%."""
    now = now or datetime.now(MSK)
    day = day or (now.date() - timedelta(days=1))
    st = database.daily_stats(day.isoformat())
    return "\n".join([
        f"Статистика стратегии IPBL за {day.strftime('%d.%m.%Y')}",
        f"{st['wins']}✅/{st['losses']}✖️/{st['pushes']}♻️",
        f"Прибыль составила {_pct(st['profit']):.2f}%",
    ])


def build_weekly_text(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_week_range(now.date())
    by_day = database.profit_by_day(start.isoformat(), end.isoformat())
    total = database.profit_total(start.isoformat(), end.isoformat())

    lines = [
        REPORT_HEADER,
        GREETING,
        f"За прошедшую неделю прибыль составила {_pct(total):.2f}%",
    ]
    for i in range(7):
        d = start + timedelta(days=i)
        lines.append(_day_line(d, by_day.get(d.isoformat(), 0.0)))
    return "\n".join(lines)


def build_monthly_text(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_month_range(now.date())
    total = database.profit_total(start.isoformat(), end.isoformat())
    return "\n".join([
        REPORT_HEADER,
        GREETING,
        f"За прошедший месяц прибыль составила {_pct(total):.2f}%",
    ])


# ===========================================================================
# Отчёты НАБОРОВ стратегии IPBL (пер-наборно, в чат набора; по ipbl_sent).
# ===========================================================================

def _ipbl_rule_header(rule: dict) -> str:
    zaps = [f"{IPBL_DIV_LABELS[d]} {rule[f'zapas_{d}']:g}"
            for d in IPBL_DIV_ORDER if rule[f"zapas_{d}"] is not None]
    tail = "; ".join(zaps) if zaps else "дивизионы не заданы"
    return f"Стратегия IPBL · набор #{rule['id']} ({tail})"


def build_ipbl_rule_daily_text(rule: dict, now: datetime | None = None,
                               day: date | None = None) -> str:
    now = now or datetime.now(MSK)
    day = day or (now.date() - timedelta(days=1))
    total = database.ipbl_profit_total_rule(rule["id"], day.isoformat(), day.isoformat())
    cnt = database.ipbl_daily_counts_rule(rule["id"], day.isoformat())
    return "\n".join([
        _ipbl_rule_header(rule),
        GREETING,
        _counts_line(cnt),
        f"За {day.strftime('%d.%m')} прибыль составила {_pct(total):.2f}%",
    ])


def build_ipbl_rule_weekly_text(rule: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_week_range(now.date())
    by_day = database.ipbl_profit_by_day_rule(rule["id"], start.isoformat(), end.isoformat())
    total = database.ipbl_profit_total_rule(rule["id"], start.isoformat(), end.isoformat())
    lines = [
        _ipbl_rule_header(rule),
        GREETING,
        f"За прошедшую неделю прибыль составила {_pct(total):.2f}%",
    ]
    for i in range(7):
        d = start + timedelta(days=i)
        lines.append(_day_line(d, by_day.get(d.isoformat(), 0.0)))
    return "\n".join(lines)


def build_ipbl_rule_monthly_text(rule: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_month_range(now.date())
    total = database.ipbl_profit_total_rule(rule["id"], start.isoformat(), end.isoformat())
    return "\n".join([
        _ipbl_rule_header(rule),
        GREETING,
        f"За прошедший месяц прибыль составила {_pct(total):.2f}%",
    ])


# ===========================================================================
# Отчёты стратегии PRIME (отдельный источник — prime_db). ПЕР-НАБОРНЫЕ: каждый
# набор считается и уходит в СВОЙ чат (prime_rules.chat_id). День/нед/мес, %% от банка.
# ===========================================================================
PRIME_MARKETS = {"tm": "ТМ", "it1": "ИТМ1"}


def _prime_rule_header(rule: dict) -> str:
    label = PRIME_MARKETS.get(rule.get("market"), rule.get("market"))
    return f"Стратегия Prime · {label} · мин {rule.get('minute')}"


def build_prime_rule_daily_text(rule: dict, now: datetime | None = None,
                                day: date | None = None) -> str:
    """Отчёт набора за один день (по умолчанию — вчера, как при авто-отправке в 09:00)."""
    now = now or datetime.now(MSK)
    day = day or (now.date() - timedelta(days=1))
    total = prime_db.profit_total_rule(rule["id"], day.isoformat(), day.isoformat())
    cnt = prime_db.daily_counts_rule(rule["id"], day.isoformat())
    return "\n".join([
        _prime_rule_header(rule),
        GREETING,
        _counts_line(cnt),
        f"За {day.strftime('%d.%m')} прибыль составила {_pct(total):.2f}%",
    ])


def build_prime_rule_weekly_text(rule: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_week_range(now.date())
    by_day = prime_db.profit_by_day_rule(rule["id"], start.isoformat(), end.isoformat())
    total = prime_db.profit_total_rule(rule["id"], start.isoformat(), end.isoformat())
    lines = [
        _prime_rule_header(rule),
        GREETING,
        f"За прошедшую неделю прибыль составила {_pct(total):.2f}%",
    ]
    for i in range(7):
        d = start + timedelta(days=i)
        lines.append(_day_line(d, by_day.get(d.isoformat(), 0.0)))
    return "\n".join(lines)


def build_prime_rule_monthly_text(rule: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_month_range(now.date())
    total = prime_db.profit_total_rule(rule["id"], start.isoformat(), end.isoformat())
    return "\n".join([
        _prime_rule_header(rule),
        GREETING,
        f"За прошедший месяц прибыль составила {_pct(total):.2f}%",
    ])


# ===========================================================================
# Отчёты стратегии ШОРТ-ХОККЕЙ ПО ПАРАМ (отдельный источник — sh_pair_db).
# День / неделя / месяц, тот же формат %% от банка.
# ===========================================================================
SH_PAIR_HEADER = "Стратегия ШХ · Пары"


def build_sh_pair_daily_text(now: datetime | None = None, day: date | None = None) -> str:
    """Отчёт за один день (по умолчанию — вчера, как при авто-отправке в 09:00)."""
    now = now or datetime.now(MSK)
    day = day or (now.date() - timedelta(days=1))
    total = sh_pair_db.profit_total(day.isoformat(), day.isoformat())
    cnt = sh_pair_db.daily_counts(day.isoformat())
    return "\n".join([
        SH_PAIR_HEADER,
        GREETING,
        _counts_line(cnt),
        f"За {day.strftime('%d.%m')} прибыль составила {_pct(total):.2f}%",
    ])


def build_sh_pair_weekly_text(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_week_range(now.date())
    by_day = sh_pair_db.profit_by_day(start.isoformat(), end.isoformat())
    total = sh_pair_db.profit_total(start.isoformat(), end.isoformat())
    lines = [
        SH_PAIR_HEADER,
        GREETING,
        f"За прошедшую неделю прибыль составила {_pct(total):.2f}%",
    ]
    for i in range(7):
        d = start + timedelta(days=i)
        lines.append(_day_line(d, by_day.get(d.isoformat(), 0.0)))
    return "\n".join(lines)


def build_sh_pair_monthly_text(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_month_range(now.date())
    total = sh_pair_db.profit_total(start.isoformat(), end.isoformat())
    return "\n".join([
        SH_PAIR_HEADER,
        GREETING,
        f"За прошедший месяц прибыль составила {_pct(total):.2f}%",
    ])


# ===========================================================================
# Отчёты стратегии ЧЕТВЕРТИ Pro Жен (отдельный источник — pq_db).
# ===========================================================================
PQ_HEADER = "Стратегия Четверти Pro Ж"


def build_pq_daily_text(now: datetime | None = None, day: date | None = None) -> str:
    now = now or datetime.now(MSK)
    day = day or (now.date() - timedelta(days=1))
    total = pq_db.profit_total(day.isoformat(), day.isoformat())
    cnt = pq_db.daily_counts(day.isoformat())
    return "\n".join([
        PQ_HEADER,
        GREETING,
        _counts_line(cnt),
        f"За {day.strftime('%d.%m')} прибыль составила {_pct(total):.2f}%",
    ])


def build_pq_weekly_text(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_week_range(now.date())
    by_day = pq_db.profit_by_day(start.isoformat(), end.isoformat())
    total = pq_db.profit_total(start.isoformat(), end.isoformat())
    lines = [
        PQ_HEADER,
        GREETING,
        f"За прошедшую неделю прибыль составила {_pct(total):.2f}%",
    ]
    for i in range(7):
        d = start + timedelta(days=i)
        lines.append(_day_line(d, by_day.get(d.isoformat(), 0.0)))
    return "\n".join(lines)


def build_pq_monthly_text(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_month_range(now.date())
    total = pq_db.profit_total(start.isoformat(), end.isoformat())
    return "\n".join([
        PQ_HEADER,
        GREETING,
        f"За прошедший месяц прибыль составила {_pct(total):.2f}%",
    ])


# ===========================================================================
# Отчёты стратегии CAGE (отдельный источник — cage_strat_db, отдельный чат).
# ===========================================================================
# Шапка отчёта на каждый рынок CAGE (ТМ / ИТМ1 / ИТМ2 идут в свои чаты).
CAGE_HEADERS = {"tm": "Стратегия CAGE · ТМ", "it1": "Стратегия CAGE · ИТМ1",
                "it2": "Стратегия CAGE · ИТМ2"}


def _cage_header(market: str) -> str:
    return CAGE_HEADERS.get(market, "Стратегия CAGE")


def build_cage_strat_daily_text(now: datetime | None = None, day: date | None = None,
                                market: str = "tm") -> str:
    now = now or datetime.now(MSK)
    day = day or (now.date() - timedelta(days=1))
    total = cage_strat_db.profit_total(day.isoformat(), day.isoformat(), market)
    cnt = cage_strat_db.daily_counts(day.isoformat(), market)
    return "\n".join([
        _cage_header(market),
        GREETING,
        _counts_line(cnt),
        f"За {day.strftime('%d.%m')} прибыль составила {_pct(total):.2f}%",
    ])


def build_cage_strat_weekly_text(now: datetime | None = None, market: str = "tm") -> str:
    now = now or datetime.now(MSK)
    start, end = last_week_range(now.date())
    by_day = cage_strat_db.profit_by_day(start.isoformat(), end.isoformat(), market)
    total = cage_strat_db.profit_total(start.isoformat(), end.isoformat(), market)
    lines = [
        _cage_header(market),
        GREETING,
        f"За прошедшую неделю прибыль составила {_pct(total):.2f}%",
    ]
    for i in range(7):
        d = start + timedelta(days=i)
        lines.append(_day_line(d, by_day.get(d.isoformat(), 0.0)))
    return "\n".join(lines)


def build_cage_strat_monthly_text(now: datetime | None = None, market: str = "tm") -> str:
    now = now or datetime.now(MSK)
    start, end = last_month_range(now.date())
    total = cage_strat_db.profit_total(start.isoformat(), end.isoformat(), market)
    return "\n".join([
        _cage_header(market),
        GREETING,
        f"За прошедший месяц прибыль составила {_pct(total):.2f}%",
    ])


# ===========================================================================
# Отчёты стратегии Prime ЖЕНЩИНЫ (отдельный источник — prime_women_db).
# ===========================================================================
PW_HEADER = "Стратегия Prime Ж · ТМ"


def build_pw_daily_text(now: datetime | None = None, day: date | None = None) -> str:
    now = now or datetime.now(MSK)
    day = day or (now.date() - timedelta(days=1))
    total = prime_women_db.profit_total(day.isoformat(), day.isoformat())
    cnt = prime_women_db.daily_counts(day.isoformat())
    return "\n".join([
        PW_HEADER,
        GREETING,
        _counts_line(cnt),
        f"За {day.strftime('%d.%m')} прибыль составила {_pct(total):.2f}%",
    ])


def build_pw_weekly_text(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_week_range(now.date())
    by_day = prime_women_db.profit_by_day(start.isoformat(), end.isoformat())
    total = prime_women_db.profit_total(start.isoformat(), end.isoformat())
    lines = [
        PW_HEADER,
        GREETING,
        f"За прошедшую неделю прибыль составила {_pct(total):.2f}%",
    ]
    for i in range(7):
        d = start + timedelta(days=i)
        lines.append(_day_line(d, by_day.get(d.isoformat(), 0.0)))
    return "\n".join(lines)


def build_pw_monthly_text(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_month_range(now.date())
    total = prime_women_db.profit_total(start.isoformat(), end.isoformat())
    return "\n".join([
        PW_HEADER,
        GREETING,
        f"За прошедший месяц прибыль составила {_pct(total):.2f}%",
    ])


# ===========================================================================
# Отчёты стратегии Pro МУЖЧИНЫ (отдельный источник — pro_strat_db).
# ===========================================================================
PRO_HEADER = "Стратегия Pro М · ТМ"


def build_pro_daily_text(now: datetime | None = None, day: date | None = None) -> str:
    now = now or datetime.now(MSK)
    day = day or (now.date() - timedelta(days=1))
    total = pro_strat_db.profit_total(day.isoformat(), day.isoformat())
    cnt = pro_strat_db.daily_counts(day.isoformat())
    return "\n".join([
        PRO_HEADER,
        GREETING,
        _counts_line(cnt),
        f"За {day.strftime('%d.%m')} прибыль составила {_pct(total):.2f}%",
    ])


def build_pro_weekly_text(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_week_range(now.date())
    by_day = pro_strat_db.profit_by_day(start.isoformat(), end.isoformat())
    total = pro_strat_db.profit_total(start.isoformat(), end.isoformat())
    lines = [
        PRO_HEADER,
        GREETING,
        f"За прошедшую неделю прибыль составила {_pct(total):.2f}%",
    ]
    for i in range(7):
        d = start + timedelta(days=i)
        lines.append(_day_line(d, by_day.get(d.isoformat(), 0.0)))
    return "\n".join(lines)


def build_pro_monthly_text(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    start, end = last_month_range(now.date())
    total = pro_strat_db.profit_total(start.isoformat(), end.isoformat())
    return "\n".join([
        PRO_HEADER,
        GREETING,
        f"За прошедший месяц прибыль составила {_pct(total):.2f}%",
    ])
