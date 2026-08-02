"""Отчёты о прибыли в процентах (недельный и месячный).

Процент считается от фиксированного стартового банка (config.BANKROLL_START):
  день  % = сумма_прибыли_дня / BANKROLL_START * 100
  неделя % = сумма всех дней (= сумма_прибыли_недели / BANKROLL_START * 100)

Формат сообщений — по эталону клиента:
  Pbd 1 Fon
  Всем доброго дня!
  За прошедшую неделю прибыль составила -3.55%
  20.07 ♻️0.00%
  21.07 ✖️-6.00%
  ...
Эмодзи по знаку дня: ♻️ = 0.00%, ✅ = плюс, ✖️ = минус. Знак «+» у плюса не пишем.
"""
from datetime import datetime, timedelta, timezone, date

import database
from config import BANKROLL_START

MSK = timezone(timedelta(hours=3))

# Шапка публикации (как в эталоне клиента).
REPORT_HEADER = "Pbd 1 Fon"
GREETING = "Всем доброго дня!"


def _pct(profit: float) -> float:
    return profit / BANKROLL_START * 100.0


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
