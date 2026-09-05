"""Выгрузка ОТПРАВЛЕННЫХ сигналов стратегии ШОРТ-ХОККЕЙ ПО ПАРАМ в Excel
(из sh_pair_strategy.db). Строка = отправленный сигнал (ТБ/ТМ по паре).

Запуск:
    python export_sh_pair.py                 # всё
    python export_sh_pair.py file.xlsx
"""
import sys
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

import sh_pair_db
from config import SH_PAIR_SIDES, SH_PAIR_PREMATCH, sh_short_league

# (Заголовок, ключ строки БД). None-ключ -> спец-обработка в _value.
COLUMNS = [
    ("Дата МСК", "__date"),
    ("Время МСК", "__time"),
    ("Лига", "__league"),
    ("Команда 1", "team1"),
    ("Команда 2", "team2"),
    ("Сторона", "__side"),
    ("Время сигнала", "__when"),
    ("Диапазон линии", "__range"),
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


def _fmt_num(o) -> str:
    if o is None:
        return ""
    return f"{float(o):.2f}".rstrip("0").rstrip(".")


def _value(row: dict, key: str):
    if key == "__league":
        return sh_short_league(row.get("league") or "")
    if key == "__side":
        return SH_PAIR_SIDES.get(row.get("side"), row.get("side"))
    if key == "__when":
        return "Прематч" if row.get("minute") == SH_PAIR_PREMATCH else f"мин {row.get('minute')}"
    if key == "__range":
        return f"{_fmt_num(row.get('line_min'))}–{_fmt_num(row.get('line_max'))}"
    if key == "__score":
        return f"{row['score1']}:{row['score2']}"
    if key == "__date":
        return (row.get("created_at") or "").split(" ")[0] or None
    if key == "__time":
        parts = (row.get("created_at") or "").split(" ")
        return parts[1] if len(parts) > 1 else None
    return row.get(key)


def build(path: str, title: str = "Сигналы ШХ пары") -> int:
    """Выгружает сигналы стратегии в Excel. Возвращает число строк."""
    rows = sh_pair_db.signals_for_export()
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]

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
    sh_pair_db.init_db()
    path = sys.argv[1] if len(sys.argv) > 1 else \
        f"export_sh_pair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    n = build(path)
    print(f"Сохранено: {path} | строк: {n}")


if __name__ == "__main__":
    main()
