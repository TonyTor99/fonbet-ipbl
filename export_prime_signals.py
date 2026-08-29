"""Выгрузка ОТПРАВЛЕННЫХ сигналов стратегии Prime в Excel (из prime_strategy.db).

Строка = отправленный сигнал (рынок ТМ или ИТМ1). Можно выгрузить отдельно ТМ,
отдельно ИТМ1 или всё сразу — фильтр по рынку задаёт вызывающий код.

Запуск:
    python export_prime_signals.py                 # всё
    python export_prime_signals.py file.xlsx tm    # только ТМ
"""
import sys
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

import prime_db
from config import PRIME_MARKETS

# (Заголовок, ключ строки БД). None-ключ -> спец-обработка в _value.
COLUMNS = [
    ("Дата МСК", "__date"),
    ("Время МСК", "__time"),
    ("Лига", "league"),
    ("Команда 1", "team1"),
    ("Команда 2", "team2"),
    ("Рынок", "__market"),
    ("Минута", "minute"),
    ("Факт. мин.", "fired_minute"),
    ("Линия", "line"),
    ("Кф", "odds"),
    ("Счёт (сигнал)", "__score"),
    ("Итог счёт", "final_score"),
    ("Итог тотал", "final_total"),
    ("Результат", "result"),
    ("Прибыль", "profit"),
]

HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
HEAD_FONT = Font(bold=True, color="FFFFFF")
WIN_FILL = PatternFill("solid", fgColor="C6EFCE")    # зелёный
LOSE_FILL = PatternFill("solid", fgColor="FFC7CE")   # красный
PUSH_FILL = PatternFill("solid", fgColor="FFEB9C")   # жёлтый (возврат)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _value(row: dict, key: str):
    if key == "__market":
        return PRIME_MARKETS.get(row.get("market"), row.get("market"))
    if key == "__score":
        return f"{row['score1']}:{row['score2']}"
    if key == "__date":
        return (row.get("created_at") or "").split(" ")[0] or None
    if key == "__time":
        parts = (row.get("created_at") or "").split(" ")
        return parts[1] if len(parts) > 1 else None
    return row.get(key)


def build(path: str, market: str | None = None, title: str = "Сигналы Prime") -> int:
    """Выгружает сигналы стратегии (фильтр по рынку) в Excel. Возвращает число строк."""
    rows = prime_db.signals_for_export(market)
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]   # лимит имени листа Excel

    for c, (head, _) in enumerate(COLUMNS, 1):
        cell = ws.cell(1, c, head)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.freeze_panes = "A2"

    for r, row in enumerate(rows, 2):
        result = row.get("result")
        for c, (head, key) in enumerate(COLUMNS, 1):
            val = _value(row, key)
            cell = ws.cell(r, c, val)
            cell.border = BORDER
            if head == "Результат" and val:
                if result == "Выигрыш":
                    cell.fill = WIN_FILL
                elif result == "Проигрыш":
                    cell.fill = LOSE_FILL
                elif result == "Возврат":
                    cell.fill = PUSH_FILL
                cell.alignment = Alignment(horizontal="center")

    for c, (head, _) in enumerate(COLUMNS, 1):
        letter = ws.cell(1, c).column_letter
        ws.column_dimensions[letter].width = max(9, min(22, len(head) + 2))

    wb.save(path)
    return len(rows)


def main():
    prime_db.init_db()
    path = sys.argv[1] if len(sys.argv) > 1 else \
        f"export_prime_signals_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    market = sys.argv[2] if len(sys.argv) > 2 else None
    n = build(path, market)
    print(f"Сохранено: {path} | строк: {n}")


if __name__ == "__main__":
    main()
