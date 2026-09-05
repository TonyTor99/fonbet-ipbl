"""Telegram-бот управления fonbet-ipbl (кнопочная инлайн-панель).

/start — панель. Управление парсером, статистика стратегий (винрейт+прибыль),
chat_id и окно работы (МСК) на каждую стратегию, сброс БД.
"""
import asyncio
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, ContextTypes, filters)
from telegram.request import HTTPXRequest

import database
import signals
import reports
import collector_db
import collector_periods_db
import export_prime
import export_prime_signals
import export_periods
import export_signals
import sh_collector_db
import sh_signals
import sh_total_signals
import export_shorthockey
import cyber_collector_db
import export_cyber
import prime_db
import prime_signals
import sh_pair_db
import sh_pair_signals
import export_sh_pair
import pq_db
import pq_signals
import export_pq
from config import (BOT_TOKEN, STRATEGIES, BANKROLL_START, ADMIN_IDS, LEAGUES,
                    COLLECTOR_LEAGUES, PERIOD_COLLECTOR_LEAGUES,
                    SH_STRAT_CODE, SH_STRAT_LEAGUES, SH_TOTAL_STRAT_CODE,
                    PRIME_STRAT_CODE, PRIME_STRAT_CODE_TM, PRIME_STRAT_CODE_IT1,
                    PRIME_STRAT_CHAT, PRIME_MARKETS, sh_short_league,
                    SH_PAIR_STRAT_CODE, SH_PAIR_SIDES, SH_PAIR_PREMATCH,
                    PQ_STRAT_CODE, PQ_SIDES)

DIR = Path(__file__).parent
LOG_FILE = DIR / "parser.log"
SH_LOG_FILE = DIR / "sh_parser.log"
CYBER_LOG_FILE = DIR / "cyber_parser.log"
MSK = timezone(timedelta(hours=3))
_proc: subprocess.Popen | None = None
_sh_proc: subprocess.Popen | None = None
_cyber_proc: subprocess.Popen | None = None

# Веб-панель fonbet-dashboard (on-demand systemd-сервис на этом же VPS)
PANEL_SERVICE = "fonbet-dashboard"
PANEL_URL = "http://147.45.41.7:8080"


# --- helpers ---------------------------------------------------------------

def parser_running() -> bool:
    if _proc is not None and _proc.poll() is None:
        return True
    try:
        # "/parser.py" — не матчит sh_parser.py (там "_parser.py")
        r = subprocess.run(["pgrep", "-f", "/parser.py"], capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False


def start_parser():
    """Запускает основной parser.py (баскетбол) subprocess'ом, если не запущен."""
    global _proc
    if parser_running():
        return
    f = open(LOG_FILE, "a")
    # -u: небуферизованный вывод, чтобы parser.log обновлялся в реальном времени
    _proc = subprocess.Popen([sys.executable, "-u", str(DIR / "parser.py")],
                             cwd=str(DIR), stdout=f, stderr=subprocess.STDOUT)


def stop_parser():
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
        _proc = None
    subprocess.run(["pkill", "-f", "/parser.py"], capture_output=True)


def sh_parser_running() -> bool:
    if _sh_proc is not None and _sh_proc.poll() is None:
        return True
    try:
        r = subprocess.run(["pgrep", "-f", "sh_parser.py"], capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False


def start_sh_parser():
    """Запускает sh_parser.py (шорт-хоккей) subprocess'ом, если не запущен."""
    global _sh_proc
    if sh_parser_running():
        return
    f = open(SH_LOG_FILE, "a")
    _sh_proc = subprocess.Popen([sys.executable, "-u", str(DIR / "sh_parser.py")],
                                cwd=str(DIR), stdout=f, stderr=subprocess.STDOUT)


def stop_sh_parser():
    global _sh_proc
    if _sh_proc and _sh_proc.poll() is None:
        _sh_proc.terminate()
        try:
            _sh_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _sh_proc.kill()
        _sh_proc = None
    subprocess.run(["pkill", "-f", "sh_parser.py"], capture_output=True)


def cyber_parser_running() -> bool:
    if _cyber_proc is not None and _cyber_proc.poll() is None:
        return True
    try:
        r = subprocess.run(["pgrep", "-f", "cyber_parser.py"], capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False


def start_cyber_parser():
    """Запускает cyber_parser.py (киберфутбол FC 26) subprocess'ом, если не запущен."""
    global _cyber_proc
    if cyber_parser_running():
        return
    f = open(CYBER_LOG_FILE, "a")
    _cyber_proc = subprocess.Popen([sys.executable, "-u", str(DIR / "cyber_parser.py")],
                                   cwd=str(DIR), stdout=f, stderr=subprocess.STDOUT)


def stop_cyber_parser():
    global _cyber_proc
    if _cyber_proc and _cyber_proc.poll() is None:
        _cyber_proc.terminate()
        try:
            _cyber_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _cyber_proc.kill()
        _cyber_proc = None
    subprocess.run(["pkill", "-f", "cyber_parser.py"], capture_output=True)


def panel_running() -> bool:
    """Запущен ли systemd-сервис веб-панели."""
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", PANEL_SERVICE], timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def start_panel():
    subprocess.run(["systemctl", "start", PANEL_SERVICE], capture_output=True, timeout=15)


def stop_panel():
    subprocess.run(["systemctl", "stop", PANEL_SERVICE], capture_output=True, timeout=15)


def money(v: float) -> str:
    return f"{v:+,.0f}".replace(",", " ") + "₽"


def _fmt_thr(v: float) -> str:
    """Запас со знаком, как в config (например -16 или -16,5)."""
    return str(int(v)) if v == int(v) else f"{v:.1f}".replace(".", ",")


def thr_label(sport_id: int) -> str:
    """Текущий запас конкретной лиги для кнопки."""
    return _fmt_thr(database.get_league_threshold(sport_id))


def _norm_hhmm(s: str) -> str:
    h, m = s.split(":")
    return f"{int(h):02d}:{int(m):02d}"


def parse_windows_input(raw: str):
    """Возвращает (нормализованная_строка|None, ok). None = круглосуточно."""
    low = raw.lower().strip()
    if low in ("off", "круглосуточно", "-", "всегда", "24/7"):
        return None, True
    norm = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            return None, False
        s, e = [x.strip() for x in part.split("-", 1)]
        if not _valid_hhmm(s) or not _valid_hhmm(e):
            return None, False
        norm.append(f"{_norm_hhmm(s)}-{_norm_hhmm(e)}")
    if not norm:
        return None, False
    return ",".join(norm), True


# --- клавиатуры ------------------------------------------------------------

def main_kb() -> InlineKeyboardMarkup:
    panel_btn = (InlineKeyboardButton("⏹ Остановить веб-панель", callback_data="panel_stop")
                 if panel_running() else
                 InlineKeyboardButton("🖥 Запустить веб-панель", callback_data="panel_start"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статус", callback_data="status")],
        [InlineKeyboardButton("🤖 Статистика стратегий", callback_data="stats")],
        [InlineKeyboardButton("📦 Сборщики", callback_data="collectors")],
        [InlineKeyboardButton("🎯 Стратегии", callback_data="strats")],
        [panel_btn],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back")]])


# --- хаб «Стратегии»: все стратегии в одном месте --------------------------

def strats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏀 Стратегия IPBL", callback_data="strat")],
        [InlineKeyboardButton("🏀 Стратегия Prime", callback_data="pmstrat")],
        [InlineKeyboardButton("🏒 Стратегия хоккея", callback_data="shstrat")],
        [InlineKeyboardButton("🏒 Стратегия тоталов", callback_data="shtstrat")],
        [InlineKeyboardButton("🏒 Стратегия ШХ пары", callback_data="spstrat")],
        [InlineKeyboardButton("🏀 Четверти Pro Жен", callback_data="pqstrat")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
    ])


def strats_text() -> str:
    return "🎯 <b>Стратегии</b>\nВыбери стратегию для настройки и статистики:"


# --- хаб «Сборщики»: все сборщики в одном месте ----------------------------

def collectors_kb() -> InlineKeyboardMarkup:
    """Список всех сборщиков: 4 лиги IPBL (за матч) + четверти Pro + шорт-хоккей."""
    rows = []
    for sid, (name, _db) in COLLECTOR_LEAGUES.items():
        rows.append([InlineKeyboardButton(f"🏀 IPBL · {name}", callback_data=f"col:{sid}")])
    for sid, (name, _db) in PERIOD_COLLECTOR_LEAGUES.items():
        rows.append([InlineKeyboardButton(f"🏀 Четверти · {name}", callback_data=f"pcol:{sid}")])
    rows.append([InlineKeyboardButton("🏒 Шорт-хоккей", callback_data="sh_collector")])
    rows.append([InlineKeyboardButton("🎮 Киберфутбол FC 26", callback_data="cyber_collector")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def collector_kb(sport_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Выгрузить Excel", callback_data=f"colx:{sport_id}")],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"col:{sport_id}")],
        [InlineKeyboardButton("🗑 Сбросить БД лиги", callback_data=f"colr:{sport_id}")],
        [InlineKeyboardButton("⬅️ К сборщикам", callback_data="collectors")],
    ])


def confirm_col_reset_kb(sport_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"colry:{sport_id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"col:{sport_id}"),
    ]])


def period_collector_kb(sport_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Выгрузить Excel", callback_data=f"pcolx:{sport_id}")],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"pcol:{sport_id}")],
        [InlineKeyboardButton("🗑 Сбросить БД четвертей", callback_data=f"pcolr:{sport_id}")],
        [InlineKeyboardButton("⬅️ К сборщикам", callback_data="collectors")],
    ])


def confirm_period_reset_kb(sport_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"pcolry:{sport_id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"pcol:{sport_id}"),
    ]])


def sh_collector_kb() -> InlineKeyboardMarkup:
    toggle = (InlineKeyboardButton("⏹ Остановить сборщик", callback_data="sh_stop")
              if sh_parser_running() else
              InlineKeyboardButton("▶️ Запустить сборщик", callback_data="sh_start"))
    return InlineKeyboardMarkup([
        [toggle],
        [InlineKeyboardButton("📥 Выгрузить Excel", callback_data="sh_export")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="sh_collector")],
        [InlineKeyboardButton("🗑 Сбросить БД хоккея", callback_data="sh_reset_ask")],
        [InlineKeyboardButton("⬅️ К сборщикам", callback_data="collectors")],
    ])


def confirm_sh_reset_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data="sh_reset_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="sh_collector"),
    ]])


def cyber_collector_kb() -> InlineKeyboardMarkup:
    toggle = (InlineKeyboardButton("⏹ Остановить сборщик", callback_data="cyber_stop")
              if cyber_parser_running() else
              InlineKeyboardButton("▶️ Запустить сборщик", callback_data="cyber_start"))
    return InlineKeyboardMarkup([
        [toggle],
        [InlineKeyboardButton("📥 Выгрузить Excel", callback_data="cyber_export")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="cyber_collector")],
        [InlineKeyboardButton("🗑 Сбросить БД киберфутбола", callback_data="cyber_reset_ask")],
        [InlineKeyboardButton("⬅️ К сборщикам", callback_data="collectors")],
    ])


def confirm_cyber_reset_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data="cyber_reset_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="cyber_collector"),
    ]])


# --- хаб «Стратегия»: настройки сигналов в одном месте ---------------------

def strategy_kb() -> InlineKeyboardMarkup:
    toggle = (InlineKeyboardButton("⏹ Остановить парсер IPBL", callback_data="stop")
              if parser_running() else
              InlineKeyboardButton("▶️ Запустить парсер IPBL", callback_data="start"))
    return InlineKeyboardMarkup([
        [toggle],
        [InlineKeyboardButton("🎚 Запас сигнала (по лигам)", callback_data="thr")],
        [InlineKeyboardButton("🏀 Лиги (вкл/выкл)", callback_data="leagues")],
        [InlineKeyboardButton("⚙️ Чаты стратегий", callback_data="chats")],
        [InlineKeyboardButton("⏰ Время работы", callback_data="sched")],
        [InlineKeyboardButton("📈 Отчёты прибыли", callback_data="reports")],
        [InlineKeyboardButton("📥 Выгрузить сигналы (Excel)", callback_data="export_sig")],
        [InlineKeyboardButton("🗑 Сбросить БД стратегий", callback_data="reset_ask")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="strats")],
    ])


def thr_kb() -> InlineKeyboardMarkup:
    """Запас формулы отдельно на каждую лигу IPBL."""
    rows = []
    for sid, (name, _div) in LEAGUES.items():
        rows.append([InlineKeyboardButton(f"{league_short(name)}: {thr_label(sid)}",
                                          callback_data=f"setthr:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="strat")])
    return InlineKeyboardMarkup(rows)


def chats_kb() -> InlineKeyboardMarkup:
    rows = []
    for code, name in STRATEGIES.items():
        cid = database.get_chat_id(code)
        rows.append([InlineKeyboardButton(f"{name}: {cid if cid is not None else 'не задан'}",
                                          callback_data=f"setchat:{code}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="strat")])
    return InlineKeyboardMarkup(rows)


def sched_kb() -> InlineKeyboardMarkup:
    rows = []
    for code, name in STRATEGIES.items():
        rows.append([InlineKeyboardButton(f"{name}: {signals.fmt_windows(code)}",
                                          callback_data=f"setsched:{code}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="strat")])
    return InlineKeyboardMarkup(rows)


def stats_kb() -> InlineKeyboardMarkup:
    """Меню статистики: по кнопке на каждую стратегию."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏀 Стратегия IPBL", callback_data="stats_tm")],
        [InlineKeyboardButton("🏀 Стратегия Prime", callback_data="stats_prime")],
        [InlineKeyboardButton("🏒 Стратегия хоккея", callback_data="stats_sh")],
        [InlineKeyboardButton("🏒 Стратегия тоталов", callback_data="stats_sht")],
        [InlineKeyboardButton("🏒 Стратегия ШХ пары", callback_data="stats_shp")],
        [InlineKeyboardButton("🏀 Четверти Pro Жен", callback_data="stats_pq")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
    ])


def stats_sub_kb() -> InlineKeyboardMarkup:
    """Клавиатура экрана статистики отдельной стратегии."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К статистике", callback_data="stats")]])


def reports_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Недельный отчёт → канал", callback_data="rep_week")],
        [InlineKeyboardButton("📤 Месячный отчёт → канал", callback_data="rep_month")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="strat")],
    ])


def league_short(name: str) -> str:
    """'Россия. IPBL. Женщины. Pro Division' -> 'Женщины. Pro Division'."""
    return name.replace("Россия.", "").replace("IPBL.", "").strip(" .")


def leagues_kb() -> InlineKeyboardMarkup:
    rows = []
    for sid, (name, _div) in LEAGUES.items():
        en = database.league_enabled(sid)
        mark = "✅" if en else "🚫"
        rows.append([InlineKeyboardButton(f"{mark} {league_short(name)}",
                                          callback_data=f"togglelg:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="strat")])
    return InlineKeyboardMarkup(rows)


def confirm_reset_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data="reset_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="strat"),
    ]])


# --- тексты ----------------------------------------------------------------

def panel_text() -> str:
    return "🏀 <b>IPBL Bot</b>"


def stats_menu_text() -> str:
    """Экран-меню статистики: выбор стратегии кнопкой."""
    bal0 = f"{BANKROLL_START:,.0f}".replace(",", " ")
    return ("🤖 <b>Статистика стратегий</b>\n\n"
            f"💰 Стартовый баланс: {bal0}₽\n\n"
            "Выбери стратегию, чтобы посмотреть её статистику ⤵️")


def _bal_line() -> str:
    bal0 = f"{BANKROLL_START:,.0f}".replace(",", " ")
    return f"💰 Стартовый баланс: {bal0}₽"


def stats_tm_text() -> str:
    """Статистика стратегии «Сигнал ТМ» (+ уведомления Prime-перерыва)."""
    lines = ["📊 <b>СТАТИСТИКА · СТРАТЕГИЯ IPBL</b>", "", _bal_line()]
    for code, name in STRATEGIES.items():
        s = database.bot_stats(code)
        lines += ["", "", f"🤖 <b>{name.upper()}</b>", ""]
        if code == "prime_info":
            lines.append(f"🔔 Уведомлений в перерыве: {s['matches']}")
            continue

        lines.append("<b>Общая статистика</b>")
        lines.append(f"📌 Перерывов: {s['matches']} | Сигналов: {s['signals']}")
        lines.append(f"✅ Плюсовые: {s['wins']} | ❌ Минусовые: {s['losses']} | ⏸️ Без итога: {s['no_result']}")
        if s["wins"] + s["losses"] > 0:
            bal = f"{s['balance']:,.0f}".replace(",", " ")
            lines.append(f"📈 Винрейт: {s['winrate']:.0f}%")
            lines.append(f"🧮 ROI: {s['roi']:+.1f}%")
            lines.append(f"💰 Прибыль: {money(s['profit'])}")
            lines.append(f"🏦 Баланс: {bal}₽")

        # разбивка по лигам (порядок как в LEAGUES)
        by_lg = database.bot_stats_by_league(code)
        for _sid, (lg_name, _div) in LEAGUES.items():
            ls = by_lg.get(lg_name)
            if not ls or ls["matches"] == 0:
                continue
            lines += ["", f"🏀 <b>{league_short(lg_name)}</b>"]
            lines.append(f"📌 Перерывов: {ls['matches']} | Сигналов: {ls['signals']}")
            lines.append(f"✅ {ls['wins']} | ❌ {ls['losses']} | ⏸️ {ls['no_result']}")
            if ls["wins"] + ls["losses"] > 0:
                lines.append(f"🎯 Винрейт: {ls['winrate']:.0f}% | ROI: {ls['roi']:+.1f}%")
                lines.append(f"💰 Прибыль: {money(ls['profit'])}")
    return "\n".join(lines)


def stats_prime_text() -> str:
    return _bal_line() + prime_stats_section()


def stats_sh_text() -> str:
    return _bal_line() + sh_stats_section()


def stats_sht_text() -> str:
    return _bal_line() + sh_total_stats_section()


def _rule_strat_status(rules, chat_code: str, unit: str) -> str:
    """Статус rule-based стратегии (Prime/хоккей/тоталы) для экрана «Статус».
    У них нет окон по времени — активность определяется наличием правил и чата."""
    n = len(rules)
    cid = database.get_chat_id(chat_code)
    if n == 0:
        return f"⚪ нет {unit}"
    if cid is None:
        return f"⚠️ {unit} есть ({n}), чат не задан"
    return f"🟢 активна ({n} {unit})"


def _prime_market_block(market: str) -> list[str]:
    """Блок статистики одного рынка Prime (ТМ или ИТМ1) — свой чат/сброс/отчёт."""
    label = PRIME_MARKETS.get(market, market)
    cid = database.get_chat_id(PRIME_STRAT_CHAT[market])
    lines = ["", f"🏀 <b>PRIME · {label}</b>  "
             f"(чат: {'<code>' + str(cid) + '</code>' if cid is not None else 'не задан'})"]
    rules = prime_db.get_rules(market)
    if not rules:
        lines.append("Наборов ещё нет.")
        return lines
    tot = prime_db.overall_stats(market)
    lines.append(f"📌 Сигналов: {tot['signals']} | ✅ {tot['wins']} | ❌ {tot['losses']} | "
                 f"↩️ {tot['pushes']} | ⏸️ {tot['no_result']}")
    if tot["wins"] + tot["losses"] > 0:
        lines.append(f"📈 Винрейт: {tot['winrate']:.0f}% | 🧮 ROI: {tot['roi']:+.1f}% | "
                     f"💰 {money(tot['profit'])}")
    for r in rules:
        st = prime_db.rule_stats(r["id"])
        if st["signals"] == 0:
            continue
        lines += ["", f"• мин {r['minute']} · пар {prime_db.count_pairs(r['id'])}: "
                  f"сигналов {st['signals']} | ✅ {st['wins']} | ❌ {st['losses']} | "
                  f"↩️ {st['pushes']} | ⏸️ {st['no_result']}"]
        if st["wins"] + st["losses"] > 0:
            lines.append(f"  🎯 WR {st['winrate']:.0f}% · ROI {st['roi']:+.1f}% · {money(st['profit'])}")
    return lines


def prime_stats_section() -> str:
    """Блок статистики Prime для общего экрана «Статистика стратегий» — по рынкам."""
    lines = ["", "", "🏀 <b>СТРАТЕГИЯ PRIME</b>"]
    for market in PRIME_MARKETS:
        lines += _prime_market_block(market)
    return "\n".join(lines)


def sh_stats_section() -> str:
    """Блок статистики стратегии хоккея для общего экрана «Статистика стратегий»."""
    rules = database.sh_get_rules()
    lines = ["", "", "🏒 <b>СТРАТЕГИЯ ХОККЕЯ</b>", ""]
    if not rules:
        lines.append("Правил ещё нет — добавь в «🏒 Стратегия хоккея».")
        return "\n".join(lines)
    tot = database.sh_overall_stats()
    lines.append("<b>Общая статистика</b>")
    lines.append(f"📌 Сигналов: {tot['signals']}")
    lines.append(f"✅ Плюсовые: {tot['wins']} | ❌ Минусовые: {tot['losses']} | ⏸️ Без итога: {tot['no_result']}")
    if tot["wins"] + tot["losses"] > 0:
        bal = f"{tot['balance']:,.0f}".replace(",", " ")
        lines.append(f"📈 Винрейт: {tot['winrate']:.0f}%")
        lines.append(f"🧮 ROI: {tot['roi']:+.1f}%")
        lines.append(f"💰 Прибыль: {money(tot['profit'])}")
        lines.append(f"🏦 Баланс: {bal}₽")
    # разбивка по правилам (лигам)
    for r in rules:
        st = database.sh_rule_stats(r["id"])
        if st["signals"] == 0:
            continue
        lines += ["", f"🏒 <b>{sh_short_league(r['sport_name'])}</b> "
                  f"(мин {r['minute']} · {sh_signals.outcome_label(r['outcome'])} · "
                  f"{sh_signals.fmt_range(r['kf_min'], r['kf_max'])})"]
        lines.append(f"📌 Сигналов: {st['signals']}")
        lines.append(f"✅ {st['wins']} | ❌ {st['losses']} | ⏸️ {st['no_result']}")
        if st["wins"] + st["losses"] > 0:
            lines.append(f"🎯 Винрейт: {st['winrate']:.0f}% | ROI: {st['roi']:+.1f}%")
            lines.append(f"💰 Прибыль: {money(st['profit'])}")
    return "\n".join(lines)


def sh_total_stats_section() -> str:
    """Блок статистики стратегии тоталов для общего экрана «Статистика стратегий»."""
    rules = database.sh_total_get_rules()
    lines = ["", "", "🏒 <b>СТРАТЕГИЯ ТОТАЛОВ</b>", ""]
    if not rules:
        lines.append("Правил ещё нет — добавь в «🏒 Стратегия тоталов».")
        return "\n".join(lines)
    tot = database.sh_total_overall_stats()
    lines.append("<b>Общая статистика</b>")
    lines.append(f"📌 Сигналов: {tot['signals']}")
    lines.append(f"✅ Плюсовые: {tot['wins']} | ❌ Минусовые: {tot['losses']} | "
                 f"↩️ Возвраты: {tot['pushes']} | ⏸️ Без итога: {tot['no_result']}")
    if tot["wins"] + tot["losses"] > 0:
        bal = f"{tot['balance']:,.0f}".replace(",", " ")
        lines.append(f"📈 Винрейт: {tot['winrate']:.0f}%")
        lines.append(f"🧮 ROI: {tot['roi']:+.1f}%")
        lines.append(f"💰 Прибыль: {money(tot['profit'])}")
        lines.append(f"🏦 Баланс: {bal}₽")
    for r in rules:
        st = database.sh_total_rule_stats(r["id"])
        if st["signals"] == 0:
            continue
        lines += ["", f"🏒 <b>{sh_short_league(r['sport_name'])}</b> "
                  f"(мин {r['minute']} · {sh_total_signals.side_label(r['side'])} · "
                  f"{sh_total_signals.fmt_range(r['line_min'], r['line_max'])})"]
        lines.append(f"📌 Сигналов: {st['signals']}")
        lines.append(f"✅ {st['wins']} | ❌ {st['losses']} | ↩️ {st['pushes']} | ⏸️ {st['no_result']}")
        if st["wins"] + st["losses"] > 0:
            lines.append(f"🎯 Винрейт: {st['winrate']:.0f}% | ROI: {st['roi']:+.1f}%")
            lines.append(f"💰 Прибыль: {money(st['profit'])}")
    return "\n".join(lines)


def reports_text() -> str:
    cid = database.get_chat_id("signal_tm")
    target = f"<code>{cid}</code>" if cid is not None else "❗️ не задан (задай chat_id «Сигнал ТМ»)"
    return (
        "📈 <b>Отчёты прибыли</b>\n\n"
        "Процент прибыли считается от банка "
        f"{BANKROLL_START:,.0f}".replace(",", " ") + "₽.\n"
        "• <b>Недельный</b> — автоматически в понедельник 09:00 МСК (за прошедшую неделю Пн–Вс, с разбивкой по дням).\n"
        "• <b>Месячный</b> — автоматически 1-го числа 09:00 МСК (итог за прошедший месяц).\n\n"
        f"Отчёты уходят в канал «Сигнал ТМ»: {target}\n\n"
        "Кнопки ниже — отправить вручную прямо сейчас."
    )


async def _send_report(bot, text: str):
    """Публикует отчёт в канал стратегии «Сигнал ТМ». (ok, err_text)."""
    cid = database.get_chat_id("signal_tm")
    if cid is None:
        return False, "chat_id стратегии «Сигнал ТМ» не задан (задай в «Стратегия → Чаты стратегий»)."
    try:
        await bot.send_message(chat_id=cid, text=text, disable_web_page_preview=True)
        return True, None
    except Exception as e:
        return False, str(e)


async def send_weekly_report(bot):
    return await _send_report(bot, reports.build_weekly_text())


async def send_monthly_report(bot):
    return await _send_report(bot, reports.build_monthly_text())


async def _send_prime_report(bot, text: str, market: str):
    """Публикует отчёт Prime в чат нужного рынка (ТМ/ИТМ1). (ok, err_text)."""
    cid = database.get_chat_id(PRIME_STRAT_CHAT[market])
    if cid is None:
        label = PRIME_MARKETS.get(market, market)
        return False, f"chat_id Prime {label} не задан (задай в «🏀 Стратегия Prime → Чат {label}»)."
    try:
        await bot.send_message(chat_id=cid, text=text, disable_web_page_preview=True)
        return True, None
    except Exception as e:
        return False, str(e)


async def _send_sh_pair_report(bot, text: str):
    """Публикует отчёт стратегии ШХ · Пары в её чат. (ok, err_text)."""
    cid = database.get_chat_id(SH_PAIR_STRAT_CODE)
    if cid is None:
        return False, "chat_id ШХ · Пары не задан (задай в «🏒 Стратегия ШХ пары → Чат стратегии»)."
    try:
        await bot.send_message(chat_id=cid, text=text, disable_web_page_preview=True)
        return True, None
    except Exception as e:
        return False, str(e)


async def _send_pq_report(bot, text: str):
    """Публикует отчёт стратегии Четверти Pro Ж в её чат. (ok, err_text)."""
    cid = database.get_chat_id(PQ_STRAT_CODE)
    if cid is None:
        return False, "chat_id Четверти Pro Ж не задан (задай в «🏀 Четверти Pro Жен → Чат стратегии»)."
    try:
        await bot.send_message(chat_id=cid, text=text, disable_web_page_preview=True)
        return True, None
    except Exception as e:
        return False, str(e)


# --- планировщик отчётов (без JobQueue: лёгкий asyncio-таск) ----------------
# JobQueue у PTB требует extra [job-queue]; чтобы не тянуть зависимость на VPS,
# проверяем время сами раз в минуту. Маркер уже отправленного периода лежит в БД
# (report_state), поэтому рестарт сервиса не приводит к повторной отправке, а
# запуск бота позже 09:00 в нужный день всё равно доотправит отчёт (catch-up).

async def _report_scheduler(app):
    while True:
        try:
            now = datetime.now(MSK)
            # Недельный: понедельник, начиная с 09:00 МСК.
            if now.weekday() == 0 and now.hour >= 9:
                marker = now.strftime("%Y-%m-%d")          # дата этого понедельника
                if database.get_report_marker("weekly") != marker:
                    ok, err = await send_weekly_report(app.bot)
                    if ok:
                        database.set_report_marker("weekly", marker)
                        print(f"[REPORT] weekly sent for {marker}")
                    else:
                        print(f"[REPORT] weekly NOT sent: {err}")
            # Месячный: 1-е число, начиная с 09:00 МСК.
            if now.day == 1 and now.hour >= 9:
                marker = now.strftime("%Y-%m")             # этот месяц
                if database.get_report_marker("monthly") != marker:
                    ok, err = await send_monthly_report(app.bot)
                    if ok:
                        database.set_report_marker("monthly", marker)
                        print(f"[REPORT] monthly sent for {marker}")
                    else:
                        print(f"[REPORT] monthly NOT sent: {err}")
            # Prime: дневной каждый день, недельный (Пн) и месячный (1-е) — с 09:00 МСК.
            # Отдельно на каждый рынок (ТМ / ИТМ1) в свой чат, свои маркеры.
            for mk in PRIME_MARKETS:
                if now.hour >= 9:
                    marker = now.strftime("%Y-%m-%d")
                    key = f"prime_daily_{mk}"
                    if database.get_report_marker(key) != marker:
                        ok, err = await _send_prime_report(
                            app.bot, reports.build_prime_daily_text(now, market=mk), mk)
                        if ok:
                            database.set_report_marker(key, marker)
                            print(f"[REPORT] prime {mk} daily sent for {marker}")
                        else:
                            print(f"[REPORT] prime {mk} daily NOT sent: {err}")
                if now.weekday() == 0 and now.hour >= 9:
                    marker = now.strftime("%Y-%m-%d")
                    key = f"prime_weekly_{mk}"
                    if database.get_report_marker(key) != marker:
                        ok, err = await _send_prime_report(
                            app.bot, reports.build_prime_weekly_text(now, market=mk), mk)
                        if ok:
                            database.set_report_marker(key, marker)
                            print(f"[REPORT] prime {mk} weekly sent for {marker}")
                if now.day == 1 and now.hour >= 9:
                    marker = now.strftime("%Y-%m")
                    key = f"prime_monthly_{mk}"
                    if database.get_report_marker(key) != marker:
                        ok, err = await _send_prime_report(
                            app.bot, reports.build_prime_monthly_text(now, market=mk), mk)
                        if ok:
                            database.set_report_marker(key, marker)
                            print(f"[REPORT] prime {mk} monthly sent for {marker}")
            # ШХ · Пары: дневной каждый день, недельный (Пн) и месячный (1-е) — с 09:00 МСК.
            if now.hour >= 9:
                marker = now.strftime("%Y-%m-%d")
                if database.get_report_marker("sh_pair_daily") != marker:
                    ok, err = await _send_sh_pair_report(
                        app.bot, reports.build_sh_pair_daily_text(now))
                    if ok:
                        database.set_report_marker("sh_pair_daily", marker)
                        print(f"[REPORT] sh_pair daily sent for {marker}")
                    else:
                        print(f"[REPORT] sh_pair daily NOT sent: {err}")
            if now.weekday() == 0 and now.hour >= 9:
                marker = now.strftime("%Y-%m-%d")
                if database.get_report_marker("sh_pair_weekly") != marker:
                    ok, err = await _send_sh_pair_report(
                        app.bot, reports.build_sh_pair_weekly_text(now))
                    if ok:
                        database.set_report_marker("sh_pair_weekly", marker)
                        print(f"[REPORT] sh_pair weekly sent for {marker}")
            if now.day == 1 and now.hour >= 9:
                marker = now.strftime("%Y-%m")
                if database.get_report_marker("sh_pair_monthly") != marker:
                    ok, err = await _send_sh_pair_report(
                        app.bot, reports.build_sh_pair_monthly_text(now))
                    if ok:
                        database.set_report_marker("sh_pair_monthly", marker)
                        print(f"[REPORT] sh_pair monthly sent for {marker}")
            # Четверти Pro Жен: дневной каждый день, недельный (Пн), месячный (1-е) — 09:00 МСК.
            if now.hour >= 9:
                marker = now.strftime("%Y-%m-%d")
                if database.get_report_marker("pq_daily") != marker:
                    ok, err = await _send_pq_report(app.bot, reports.build_pq_daily_text(now))
                    if ok:
                        database.set_report_marker("pq_daily", marker)
                        print(f"[REPORT] pq daily sent for {marker}")
                    else:
                        print(f"[REPORT] pq daily NOT sent: {err}")
            if now.weekday() == 0 and now.hour >= 9:
                marker = now.strftime("%Y-%m-%d")
                if database.get_report_marker("pq_weekly") != marker:
                    ok, err = await _send_pq_report(app.bot, reports.build_pq_weekly_text(now))
                    if ok:
                        database.set_report_marker("pq_weekly", marker)
                        print(f"[REPORT] pq weekly sent for {marker}")
            if now.day == 1 and now.hour >= 9:
                marker = now.strftime("%Y-%m")
                if database.get_report_marker("pq_monthly") != marker:
                    ok, err = await _send_pq_report(app.bot, reports.build_pq_monthly_text(now))
                    if ok:
                        database.set_report_marker("pq_monthly", marker)
                        print(f"[REPORT] pq monthly sent for {marker}")
        except Exception as e:
            print(f"[REPORT sched error] {e}")
        await asyncio.sleep(60)


def collectors_text() -> str:
    """Хаб сборщиков: сводка по всем в одном экране."""
    lines = [
        "📦 <b>Сборщики рынков</b>",
        f"Парсер IPBL: {'🟢 работает' if parser_running() else '🔴 остановлен'}",
        f"Шорт-хоккей: {'🟢 работает' if sh_parser_running() else '🔴 остановлен'}",
        f"Киберфутбол FC 26: {'🟢 работает' if cyber_parser_running() else '🔴 остановлен'}",
        "",
        "Лиги IPBL (сбор идёт вместе с парсером, каждая в свой файл):",
    ]
    for sid, (name, db) in COLLECTOR_LEAGUES.items():
        st = collector_db.stats(db)
        lines.append(f"• <b>{name}</b>: матчей {st['events']} · строк {st['rows']}")
    lines.append("")
    lines.append("Рынки по четвертям (только Pro, строка на четверть):")
    for sid, (name, db) in PERIOD_COLLECTOR_LEAGUES.items():
        st = collector_periods_db.stats(db)
        lines.append(f"• <b>{name}</b>: матчей {st['events']} · строк {st['rows']}")
    lines.append("")
    lines.append("Выбери сборщик для выгрузки/сброса ⤵️")
    return "\n".join(lines)


def collector_text(sport_id: int) -> str:
    name, db = COLLECTOR_LEAGUES[sport_id]
    st = collector_db.stats(db)
    lines = [
        f"🏀 <b>Сборщик IPBL · {name}</b>",
        f"Парсер: {'🟢 работает' if parser_running() else '🔴 остановлен'}",
        f"Файл: <code>{db}</code>",
        "",
        f"Матчей собрано: <b>{st['events']}</b>",
        f"Строк (игровых минут): <b>{st['rows']}</b>",
        f"С результатом: <b>{st['resolved']}</b>",
        "",
    ]
    summ = collector_db.events_summary(db, 15)
    if summ:
        lines.append("Последние матчи:")
        for e in summ:
            fin = e["final_score"] if e["final_score"] else "идёт"
            lines.append(f"• {e['team1']} — {e['team2']}: {e['minutes']} мин · {fin}")
    else:
        lines.append("Пока пусто — ждём live-матч этой лиги.")
    return "\n".join(lines)


def period_collector_text(sport_id: int) -> str:
    name, db = PERIOD_COLLECTOR_LEAGUES[sport_id]
    st = collector_periods_db.stats(db)
    lines = [
        f"🏀 <b>Сборщик четвертей · {name}</b>",
        f"Парсер: {'🟢 работает' if parser_running() else '🔴 остановлен'}",
        f"Файл: <code>{db}</code>",
        "Рынки каждой четверти отдельно (фора/тотал/ИТ/1X2), строка на четверть.",
        "",
        f"Матчей собрано: <b>{st['events']}</b>",
        f"Строк (четвертей × минут): <b>{st['rows']}</b>",
        f"С результатом: <b>{st['resolved']}</b>",
        "",
    ]
    summ = collector_periods_db.events_summary(db, 15)
    if summ:
        lines.append("Последние матчи:")
        for e in summ:
            fin = e["final_score"] if e["final_score"] else "идёт"
            lines.append(f"• {e['team1']} — {e['team2']}: {e['minutes']} стр · {fin}")
    else:
        lines.append("Пока пусто — ждём live-матч Pro-дивизиона.")
    return "\n".join(lines)


def sh_collector_text() -> str:
    st = sh_collector_db.stats()
    lines = [
        "🏒 <b>Сборщик рынков шорт-хоккея</b>",
        f"Сборщик: {'🟢 работает' if sh_parser_running() else '🔴 остановлен'}",
        "Лиги: все «Шорт-хоккей…» (авто-подхват)",
        "",
        f"Матчей собрано: <b>{st['events']}</b>",
        f"Строк (снимков): <b>{st['rows']}</b>",
        f"С результатом: <b>{st['resolved']}</b>",
        "",
    ]
    summ = sh_collector_db.events_summary(15)
    if summ:
        lines.append("Последние матчи:")
        for e in summ:
            fin = e["final_score"] if e["final_score"] else "идёт"
            lines.append(f"• {e['team1']} — {e['team2']}: {e['minutes']} стр · {fin}")
    else:
        lines.append("Пока пусто — ждём live-матч шорт-хоккея.")
    return "\n".join(lines)


def cyber_collector_text() -> str:
    st = cyber_collector_db.stats()
    lines = [
        "🎮 <b>Сборщик рынков киберфутбола FC 26</b>",
        f"Сборщик: {'🟢 работает' if cyber_parser_running() else '🔴 остановлен'}",
        "Лиги: все «FC 26…» (авто-подхват, Volta исключена)",
        "Снимки: до матча + каждые 5 игровых минут (0-90).",
        "",
        f"Матчей собрано: <b>{st['events']}</b>",
        f"Строк (снимков): <b>{st['rows']}</b>",
        f"С результатом: <b>{st['resolved']}</b>",
        "",
    ]
    summ = cyber_collector_db.events_summary(15)
    if summ:
        lines.append("Последние матчи:")
        for e in summ:
            fin = e["final_score"] if e["final_score"] else "идёт"
            lines.append(f"• {e['team1']} — {e['team2']}: {e['marks']} стр · {fin}")
    else:
        lines.append("Пока пусто — ждём live-матч киберфутбола FC 26.")
    return "\n".join(lines)


def chats_text() -> str:
    lines = ["⚙️ <b>Чаты стратегий</b>", "Нажми на стратегию и пришли chat_id одним сообщением.", ""]
    for code, name in STRATEGIES.items():
        cid = database.get_chat_id(code)
        lines.append(f"• <b>{name}</b> → {cid if cid is not None else '—'}")
    return "\n".join(lines)


def sched_text() -> str:
    lines = [
        "⏰ <b>Время работы</b> (МСК)",
        "Нажми на стратегию и пришли одно или несколько окон через запятую:",
        "<code>10:00-12:00, 16:00-18:00, 20:00-22:00</code>",
        "или <code>off</code> — круглосуточно.", "",
    ]
    for code, name in STRATEGIES.items():
        lines.append(f"• <b>{name}</b>")
        lines.append(f"   {signals.fmt_windows(code)}  ·  {signals.window_status(code)}")
    return "\n".join(lines)


def leagues_text() -> str:
    lines = [
        "🏀 <b>Лиги</b>",
        "Тап по лиге переключает её. Выключенная лига (🚫) не даёт сигналов.",
        "",
    ]
    for sid, (name, _div) in LEAGUES.items():
        en = database.league_enabled(sid)
        lines.append(f"{'✅ включена' if en else '🚫 выключена'} — <b>{league_short(name)}</b>")
    return "\n".join(lines)


def strategy_text() -> str:
    st = "🟢 работает" if parser_running() else "🔴 остановлен"
    return ("🏀 <b>Стратегия IPBL</b>\n"
            f"Парсер IPBL: {st}\n"
            "Настройки сигналов в перерыве: запас формулы по каждой лиге, "
            "вкл/выкл лиг, чаты, время работы, сброс БД.")


def thr_text() -> str:
    lines = [
        "🎚 <b>Запас сигнала</b> (по каждой лиге)",
        "Сигнал даётся при <code>2×сумма − линия ≤ запас</code>.",
        "Тап по лиге — прислать новое значение со знаком (например <code>-16</code>).",
        "",
    ]
    for sid, (name, _div) in LEAGUES.items():
        lines.append(f"• <b>{league_short(name)}</b>: {thr_label(sid)}")
    return "\n".join(lines)


# --- хаб «Стратегия хоккея»: правила по лигам шорт-хоккея -------------------

# приём разных написаний исхода в одну строку
_OUTCOME_ALIASES = {
    "1": "win1", "п1": "win1", "p1": "win1", "win1": "win1",
    "x": "draw", "х": "draw", "0": "draw", "draw": "draw", "ничья": "draw",
    "2": "win2", "п2": "win2", "p2": "win2", "win2": "win2",
}


def parse_sh_rule_input(raw: str):
    """'15 X 1.01 2' -> (minute, outcome, kf_min, kf_max) или None при ошибке.

    Порядок: минута, исход (1/X/2 или П1/П2), кф_от, кф_до. Кф с запятой или точкой,
    границы можно в любом порядке — упорядочим сами."""
    parts = raw.replace(",", ".").split()
    if len(parts) != 4:
        return None
    try:
        minute = int(parts[0])
    except ValueError:
        return None
    if minute < 0:
        return None
    outcome = _OUTCOME_ALIASES.get(parts[1].lower())
    if outcome is None:
        return None
    try:
        a, b = float(parts[2]), float(parts[3])
    except ValueError:
        return None
    if a <= 0 or b <= 0:
        return None
    kf_min, kf_max = (a, b) if a <= b else (b, a)
    return minute, outcome, kf_min, kf_max


def sh_rule_label(rule: dict) -> str:
    """Короткая подпись правила для кнопки/списка."""
    mark = "✅" if rule["enabled"] else "🚫"
    return (f"{mark} {sh_short_league(rule['sport_name'])} · мин {rule['minute']} · "
            f"{sh_signals.outcome_label(rule['outcome'])} · "
            f"{sh_signals.fmt_range(rule['kf_min'], rule['kf_max'])}")


def shstrat_kb() -> InlineKeyboardMarkup:
    toggle = (InlineKeyboardButton("⏹ Остановить сборщик", callback_data="sh_stop")
              if sh_parser_running() else
              InlineKeyboardButton("▶️ Запустить сборщик", callback_data="sh_start"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Правила / лиги", callback_data="shrules")],
        [InlineKeyboardButton("⚙️ Чат стратегии", callback_data="shchat")],
        [InlineKeyboardButton("📊 Статистика", callback_data="shstats")],
        [toggle],
        [InlineKeyboardButton("🗑 Сбросить БД сигналов", callback_data="shreset_ask")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="strats")],
    ])


def shstrat_text() -> str:
    cid = database.get_chat_id(SH_STRAT_CODE)
    rules = database.sh_get_rules()
    on = sum(1 for r in rules if r["enabled"])
    return (
        "🏒 <b>Стратегия шорт-хоккея</b>\n"
        f"Сборщик: {'🟢 работает' if sh_parser_running() else '🔴 остановлен'}\n"
        f"Чат отправки: {'<code>' + str(cid) + '</code>' if cid is not None else '❗️ не задан'}\n"
        f"Правил: {len(rules)} (включено {on})\n\n"
        "Бот ищет сигналы ТОЛЬКО по настроенным лигам: на заданной минуте матча "
        "проверяет кф выбранного исхода (П1/X/П2) и, если он в диапазоне, шлёт сигнал.\n"
        "⚠️ Сбор данных должен быть запущен (сборщик шорт-хоккея)."
    )


def shrules_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(sh_rule_label(r), callback_data=f"shrule:{r['id']}")]
            for r in database.sh_get_rules()]
    rows.append([InlineKeyboardButton("➕ Добавить лигу", callback_data="shadd")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="shstrat")])
    return InlineKeyboardMarkup(rows)


def shrules_text() -> str:
    rules = database.sh_get_rules()
    lines = ["📋 <b>Правила стратегии</b>", ""]
    if not rules:
        lines.append("Пока пусто. Нажми «➕ Добавить лигу».")
    else:
        lines.append("Тап по правилу — открыть/изменить/удалить.")
    return "\n".join(lines)


def shadd_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(sh_short_league(name), callback_data=f"shaddlg:{i}")]
            for i, name in enumerate(SH_STRAT_LEAGUES)]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="shrules")])
    return InlineKeyboardMarkup(rows)


def shrule_kb(rule_id: int, enabled: bool) -> InlineKeyboardMarkup:
    toggle = ("🚫 Выключить" if enabled else "✅ Включить")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data=f"shtgl:{rule_id}")],
        [InlineKeyboardButton("✏️ Изменить параметры", callback_data=f"shedit:{rule_id}")],
        [InlineKeyboardButton("🗑 Удалить правило", callback_data=f"shdel_ask:{rule_id}")],
        [InlineKeyboardButton("⬅️ К правилам", callback_data="shrules")],
    ])


def shrule_text(rule: dict) -> str:
    st = database.sh_rule_stats(rule["id"])
    lines = [
        f"🏒 <b>{sh_short_league(rule['sport_name'])}</b>",
        f"{'✅ включено' if rule['enabled'] else '🚫 выключено'}",
        "",
        f"⏱ Минута сигнала: <b>{rule['minute']}</b>",
        f"🎯 Ставка: <b>{sh_signals.outcome_label(rule['outcome'])}</b>",
        f"📐 Диапазон кф: <b>{sh_signals.fmt_range(rule['kf_min'], rule['kf_max'])}</b>",
        "",
        "<b>Статистика</b>",
        f"Сигналов: {st['signals']} | ✅ {st['wins']} | ❌ {st['losses']} | ⏸️ {st['no_result']}",
    ]
    if st["wins"] + st["losses"] > 0:
        lines.append(f"Винрейт: {st['winrate']:.0f}% | ROI: {st['roi']:+.1f}%")
        lines.append(f"Прибыль: {money(st['profit'])}")
    return "\n".join(lines)


def confirm_shdel_kb(rule_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"shdel_yes:{rule_id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"shrule:{rule_id}"),
    ]])


def confirm_shreset_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data="shreset_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="shstrat"),
    ]])


def shstats_text() -> str:
    rules = database.sh_get_rules()
    lines = ["📊 <b>Статистика стратегии хоккея</b>", ""]
    if not rules:
        lines.append("Правил ещё нет.")
        return "\n".join(lines)
    for r in rules:
        st = database.sh_rule_stats(r["id"])
        lines.append(f"🏒 <b>{sh_short_league(r['sport_name'])}</b> "
                     f"(мин {r['minute']} · {sh_signals.outcome_label(r['outcome'])} · "
                     f"{sh_signals.fmt_range(r['kf_min'], r['kf_max'])})")
        extra = ""
        if st['wins'] + st['losses']:
            extra = f" · WR {st['winrate']:.0f}% · {money(st['profit'])}"
        lines.append(f"   Сигналов: {st['signals']} | ✅ {st['wins']} | ❌ {st['losses']} | ⏸️ {st['no_result']}{extra}")
    tot = database.sh_overall_stats()
    settled = tot["wins"] + tot["losses"]
    line = f"<b>ИТОГО:</b> сигналов {tot['signals']} | ✅ {tot['wins']} | ❌ {tot['losses']}"
    if settled:
        line += f" | Винрейт {tot['winrate']:.0f}% | ROI {tot['roi']:+.1f}% | {money(tot['profit'])}"
    lines += ["", line]
    return "\n".join(lines)


# --- стратегия ТОТАЛОВ (ТБ/ТМ) ---------------------------------------------

_SIDE_ALIASES = {
    "тб": "over", "б": "over", "over": "over", "o": "over", "tb": "over",
    "тм": "under", "м": "under", "under": "under", "u": "under", "tm": "under",
}


def parse_sh_total_rule_input(raw: str):
    """'15 ТБ 8.5 12.5' -> (minute, side, line_min, line_max) или None при ошибке.

    Порядок: минута, сторона (ТБ/ТМ), линия_от, линия_до. Границы можно в любом
    порядке — упорядочим сами."""
    parts = raw.replace(",", ".").split()
    if len(parts) != 4:
        return None
    try:
        minute = int(parts[0])
    except ValueError:
        return None
    if minute < 0:
        return None
    side = _SIDE_ALIASES.get(parts[1].lower())
    if side is None:
        return None
    try:
        a, b = float(parts[2]), float(parts[3])
    except ValueError:
        return None
    if a <= 0 or b <= 0:
        return None
    line_min, line_max = (a, b) if a <= b else (b, a)
    return minute, side, line_min, line_max


def sh_total_rule_label(rule: dict) -> str:
    mark = "✅" if rule["enabled"] else "🚫"
    return (f"{mark} {sh_short_league(rule['sport_name'])} · мин {rule['minute']} · "
            f"{sh_total_signals.side_label(rule['side'])} · "
            f"{sh_total_signals.fmt_range(rule['line_min'], rule['line_max'])}")


def shtstrat_kb() -> InlineKeyboardMarkup:
    toggle = (InlineKeyboardButton("⏹ Остановить сборщик", callback_data="sh_stop")
              if sh_parser_running() else
              InlineKeyboardButton("▶️ Запустить сборщик", callback_data="sh_start"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Правила / лиги", callback_data="shtrules")],
        [InlineKeyboardButton("⚙️ Чат стратегии", callback_data="shtchat")],
        [InlineKeyboardButton("📊 Статистика", callback_data="shtstats")],
        [toggle],
        [InlineKeyboardButton("🗑 Сбросить БД сигналов", callback_data="shtreset_ask")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="strats")],
    ])


def shtstrat_text() -> str:
    cid = database.get_chat_id(SH_TOTAL_STRAT_CODE)
    rules = database.sh_total_get_rules()
    on = sum(1 for r in rules if r["enabled"])
    return (
        "🏒 <b>Стратегия тоталов (ТБ/ТМ)</b>\n"
        f"Сборщик: {'🟢 работает' if sh_parser_running() else '🔴 остановлен'}\n"
        f"Чат отправки: {'<code>' + str(cid) + '</code>' if cid is not None else '❗️ не задан'}\n"
        f"Правил: {len(rules)} (включено {on})\n\n"
        "Бот ищет сигналы ТОЛЬКО по настроенным лигам: на заданной минуте матча "
        "проверяет линию тотала и, если она в диапазоне, шлёт сигнал ТБ/ТМ.\n"
        "⚠️ Сбор данных должен быть запущен (сборщик шорт-хоккея — общий с 1X2)."
    )


def shtrules_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(sh_total_rule_label(r), callback_data=f"shtrule:{r['id']}")]
            for r in database.sh_total_get_rules()]
    rows.append([InlineKeyboardButton("➕ Добавить лигу", callback_data="shtadd")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="shtstrat")])
    return InlineKeyboardMarkup(rows)


def shtrules_text() -> str:
    rules = database.sh_total_get_rules()
    lines = ["📋 <b>Правила стратегии тоталов</b>", ""]
    if not rules:
        lines.append("Пока пусто. Нажми «➕ Добавить лигу».")
    else:
        lines.append("Тап по правилу — открыть/изменить/удалить.")
    return "\n".join(lines)


def shtadd_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(sh_short_league(name), callback_data=f"shtaddlg:{i}")]
            for i, name in enumerate(SH_STRAT_LEAGUES)]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="shtrules")])
    return InlineKeyboardMarkup(rows)


def shtrule_kb(rule_id: int, enabled: bool) -> InlineKeyboardMarkup:
    toggle = ("🚫 Выключить" if enabled else "✅ Включить")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data=f"shttgl:{rule_id}")],
        [InlineKeyboardButton("✏️ Изменить параметры", callback_data=f"shtedit:{rule_id}")],
        [InlineKeyboardButton("🗑 Удалить правило", callback_data=f"shtdel_ask:{rule_id}")],
        [InlineKeyboardButton("⬅️ К правилам", callback_data="shtrules")],
    ])


def shtrule_text(rule: dict) -> str:
    st = database.sh_total_rule_stats(rule["id"])
    lines = [
        f"🏒 <b>{sh_short_league(rule['sport_name'])}</b>",
        f"{'✅ включено' if rule['enabled'] else '🚫 выключено'}",
        "",
        f"⏱ Минута сигнала: <b>{rule['minute']}</b>",
        f"🎯 Ставка: <b>{sh_total_signals.side_label(rule['side'])}</b>",
        f"📐 Диапазон линии: <b>{sh_total_signals.fmt_range(rule['line_min'], rule['line_max'])}</b>",
        "",
        "<b>Статистика</b>",
        f"Сигналов: {st['signals']} | ✅ {st['wins']} | ❌ {st['losses']} | "
        f"↩️ {st['pushes']} | ⏸️ {st['no_result']}",
    ]
    if st["wins"] + st["losses"] > 0:
        lines.append(f"Винрейт: {st['winrate']:.0f}% | ROI: {st['roi']:+.1f}%")
        lines.append(f"Прибыль: {money(st['profit'])}")
    return "\n".join(lines)


def confirm_shtdel_kb(rule_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"shtdel_yes:{rule_id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"shtrule:{rule_id}"),
    ]])


def confirm_shtreset_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data="shtreset_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="shtstrat"),
    ]])


def shtstats_text() -> str:
    rules = database.sh_total_get_rules()
    lines = ["📊 <b>Статистика стратегии тоталов</b>", ""]
    if not rules:
        lines.append("Правил ещё нет.")
        return "\n".join(lines)
    for r in rules:
        st = database.sh_total_rule_stats(r["id"])
        lines.append(f"🏒 <b>{sh_short_league(r['sport_name'])}</b> "
                     f"(мин {r['minute']} · {sh_total_signals.side_label(r['side'])} · "
                     f"{sh_total_signals.fmt_range(r['line_min'], r['line_max'])})")
        extra = ""
        if st['wins'] + st['losses']:
            extra = f" · WR {st['winrate']:.0f}% · {money(st['profit'])}"
        lines.append(f"   Сигналов: {st['signals']} | ✅ {st['wins']} | ❌ {st['losses']} | "
                     f"↩️ {st['pushes']} | ⏸️ {st['no_result']}{extra}")
    tot = database.sh_total_overall_stats()
    settled = tot["wins"] + tot["losses"]
    line = f"<b>ИТОГО:</b> сигналов {tot['signals']} | ✅ {tot['wins']} | ❌ {tot['losses']} | ↩️ {tot['pushes']}"
    if settled:
        line += f" | Винрейт {tot['winrate']:.0f}% | ROI {tot['roi']:+.1f}% | {money(tot['profit'])}"
    lines += ["", line]
    return "\n".join(lines)


# --- Prime-стратегия -------------------------------------------------------

PM_PAGE = 8   # пар на страницу в экране галочек


def pmstrat_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Наборы (рынок+минута+пары)", callback_data="pmrules")],
        [InlineKeyboardButton("⚙️ Чат ТМ", callback_data="pmchat:tm"),
         InlineKeyboardButton("⚙️ Чат ИТМ1", callback_data="pmchat:it1")],
        [InlineKeyboardButton("📊 Статистика", callback_data="pmstats")],
        [InlineKeyboardButton("📈 Отчёты (день/нед/мес)", callback_data="pmreports")],
        [InlineKeyboardButton("📥 Excel ТМ", callback_data="pmexport:tm"),
         InlineKeyboardButton("📥 Excel ИТМ1", callback_data="pmexport:it1")],
        [InlineKeyboardButton("🗑 Сброс ТМ", callback_data="pmreset_ask:tm"),
         InlineKeyboardButton("🗑 Сброс ИТМ1", callback_data="pmreset_ask:it1")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="strats")],
    ])


def _prime_chat_line(market: str) -> str:
    label = PRIME_MARKETS.get(market, market)
    cid = database.get_chat_id(PRIME_STRAT_CHAT[market])
    val = f"<code>{cid}</code>" if cid is not None else "❗️ не задан"
    return f"Чат {label}: {val}"


def pmstrat_text() -> str:
    rules = prime_db.get_rules()
    on = sum(1 for r in rules if r["enabled"])
    return (
        "🏀 <b>Стратегия Prime (ТМ / ИТМ1)</b>\n"
        f"Парсер: {'🟢 работает' if parser_running() else '🔴 остановлен'}\n"
        f"{_prime_chat_line('tm')}\n"
        f"{_prime_chat_line('it1')}\n"
        f"Наборов: {len(rules)} (включено {on})\n\n"
        "ТМ и ИТМ1 — два независимых варианта: шлют в свои чаты, считаются и "
        "сбрасываются раздельно (БД одна, разделение по рынку).\n"
        "Набор = рынок + минута + галочки пар. На заданной игровой минуте матча "
        "Prime муж по отмеченной паре шлётся сигнал по текущей крайней линии рынка.\n"
        "⚠️ Список пар берётся из сборщика Prime муж — он должен собирать матчи."
    )


def pmrules_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(pm_rule_label(r), callback_data=f"pmrule:{r['id']}")]
            for r in prime_db.get_rules()]
    rows.append([InlineKeyboardButton("➕ Добавить набор", callback_data="pmadd")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="pmstrat")])
    return InlineKeyboardMarkup(rows)


def pm_rule_label(rule: dict) -> str:
    mark = "✅" if rule["enabled"] else "🚫"
    return (f"{mark} {prime_signals.market_label(rule['market'])} · мин {rule['minute']} · "
            f"пар {prime_db.count_pairs(rule['id'])}")


def pmrules_text() -> str:
    rules = prime_db.get_rules()
    lines = ["📋 <b>Наборы стратегии Prime</b>", ""]
    if not rules:
        lines.append("Пока пусто. Нажми «➕ Добавить набор».")
    else:
        lines.append("Тап по набору — рынок/минута/пары/удаление.")
    return "\n".join(lines)


def pmadd_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"pmaddmk:{code}")]
            for code, label in PRIME_MARKETS.items()]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="pmrules")])
    return InlineKeyboardMarkup(rows)


def pmrule_kb(rule: dict) -> InlineKeyboardMarkup:
    rid = rule["id"]
    toggle = ("🚫 Выключить" if rule["enabled"] else "✅ Включить")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data=f"pmtgl:{rid}")],
        [InlineKeyboardButton(f"🔀 Рынок: {prime_signals.market_label(rule['market'])} → сменить",
                              callback_data=f"pmmk:{rid}")],
        [InlineKeyboardButton(f"✏️ Минута ({rule['minute']})", callback_data=f"pmmin:{rid}")],
        [InlineKeyboardButton(f"☑️ Пары (отмечено {prime_db.count_pairs(rid)})",
                              callback_data=f"pmpairs:{rid}")],
        [InlineKeyboardButton("🗑 Удалить набор", callback_data=f"pmdel_ask:{rid}")],
        [InlineKeyboardButton("⬅️ К наборам", callback_data="pmrules")],
    ])


def pmrule_text(rule: dict) -> str:
    st = prime_db.rule_stats(rule["id"])
    lines = [
        f"🏀 <b>Набор · {prime_signals.market_label(rule['market'])}</b>",
        f"{'✅ включено' if rule['enabled'] else '🚫 выключено'}",
        "",
        f"⏱ Минута сигнала: <b>{rule['minute']}</b>",
        f"🎯 Рынок: <b>{prime_signals.market_label(rule['market'])}</b>",
        f"☑️ Отмечено пар: <b>{prime_db.count_pairs(rule['id'])}</b>",
        "",
        "<b>Статистика</b>",
        f"Сигналов: {st['signals']} | ✅ {st['wins']} | ❌ {st['losses']} | "
        f"↩️ {st['pushes']} | ⏸️ {st['no_result']}",
    ]
    if st["wins"] + st["losses"] > 0:
        lines.append(f"Винрейт: {st['winrate']:.0f}% | ROI: {st['roi']:+.1f}%")
        lines.append(f"Прибыль: {money(st['profit'])}")
    return "\n".join(lines)


def confirm_pmdel_kb(rule_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"pmdel_yes:{rule_id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"pmrule:{rule_id}"),
    ]])


def confirm_pmreset_kb(market: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"pmreset_yes:{market}"),
        InlineKeyboardButton("❌ Отмена", callback_data="pmstrat"),
    ]])


def pmreports_kb() -> InlineKeyboardMarkup:
    """Отчёты по каждому рынку отдельно — уходят в чат своего рынка."""
    rows = []
    for mk, label in PRIME_MARKETS.items():
        rows.append([
            InlineKeyboardButton(f"📤 {label}: день", callback_data=f"pmrep:{mk}:day"),
            InlineKeyboardButton("неделя", callback_data=f"pmrep:{mk}:week"),
            InlineKeyboardButton("месяц", callback_data=f"pmrep:{mk}:month"),
        ])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="pmstrat")])
    return InlineKeyboardMarkup(rows)


def pmreports_text() -> str:
    return (
        "📈 <b>Отчёты стратегии Prime</b>\n\n"
        "Процент прибыли — от банка "
        f"{BANKROLL_START:,.0f}".replace(",", " ") + "₽.\n"
        "• <b>Дневной</b> — авто ежедневно 09:00 МСК (за вчера).\n"
        "• <b>Недельный</b> — авто в понедельник 09:00 МСК (Пн–Вс).\n"
        "• <b>Месячный</b> — авто 1-го числа 09:00 МСК.\n\n"
        "ТМ и ИТМ1 считаются и уходят раздельно, каждый в свой чат:\n"
        f"{_prime_chat_line('tm')}\n"
        f"{_prime_chat_line('it1')}\n\n"
        "Кнопки ниже — отправить вручную сейчас."
    )


def pmstats_text() -> str:
    lines = ["📊 <b>Статистика стратегии Prime</b>"]
    for market in PRIME_MARKETS:
        lines += _prime_market_block(market)
    return "\n".join(lines)


# --- экран галочек пар (с поиском/фильтром/пагинацией) ----------------------

def _pm_view(ctx, rid: int) -> dict:
    """Состояние экрана пар в user_data: rid, фильтр, поисковая строка, команда, страница."""
    v = ctx.user_data.get("pm_view")
    if not v or v.get("rid") != rid:
        v = {"rid": rid, "filter": None, "search": "", "team": "", "page": 0}
        ctx.user_data["pm_view"] = v
    return v


def _pm_filtered_pairs(view: dict) -> list[tuple[str, str]]:
    pairs = prime_db.distinct_pairs()
    f = view.get("filter")
    if f == "team":
        t = view["team"].lower()
        pairs = [p for p in pairs if p[0].lower() == t or p[1].lower() == t]
    elif f == "search":
        s = view["search"].lower()
        pairs = [p for p in pairs if s in p[0].lower() or s in p[1].lower()]
    elif f == "selected":
        sel = prime_db.get_rule_pairs(view["rid"])
        pairs = [p for p in pairs if p in sel]
    return pairs


def _pm_filter_name(view: dict) -> str:
    f = view.get("filter")
    if f == "team":
        return f"команда «{view['team']}»"
    if f == "search":
        return f"поиск «{view['search']}»"
    if f == "selected":
        return "только отмеченные"
    return "все пары"


def pmpairs_text(ctx, rid: int) -> str:
    view = _pm_view(ctx, rid)
    rule = prime_db.get_rule(rid)
    pairs = _pm_filtered_pairs(view)
    total = len(prime_db.distinct_pairs())
    sel = prime_db.count_pairs(rid)
    mk = prime_signals.market_label(rule["market"]) if rule else "?"
    lines = [
        f"☑️ <b>Пары набора · {mk} · мин {rule['minute'] if rule else '?'}</b>",
        f"Отмечено: <b>{sel}</b> из {total} пар",
        f"Фильтр: {_pm_filter_name(view)} — найдено {len(pairs)}",
        "",
        "Тап по паре — поставить/снять ✅.",
    ]
    if not pairs:
        lines.append("\nПод фильтр ничего не попало (или сборщик ещё пуст).")
    return "\n".join(lines)


def pmpairs_kb(ctx, rid: int) -> InlineKeyboardMarkup:
    view = _pm_view(ctx, rid)
    pairs = _pm_filtered_pairs(view)
    sel = prime_db.get_rule_pairs(rid)

    pages = max(1, (len(pairs) + PM_PAGE - 1) // PM_PAGE)
    page = max(0, min(view["page"], pages - 1))
    view["page"] = page
    start = page * PM_PAGE
    chunk = pairs[start:start + PM_PAGE]

    rows = []
    for pos, (a, b) in enumerate(chunk, start=start):
        mark = "✅" if (a, b) in sel else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {prime_db.pair_label(a, b)}",
                                          callback_data=f"pmtog:{pos}")])
    # навигация по страницам
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data="pmpg:prev"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="pmnop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data="pmpg:next"))
    if len(nav) > 1:
        rows.append(nav)
    # фильтры
    rows.append([
        InlineKeyboardButton("🔤 По команде", callback_data="pmflt_team"),
        InlineKeyboardButton("🔍 Поиск", callback_data="pmflt_search"),
    ])
    sel_btn = ("❌ Снять фильтр" if view["filter"] else "☑️ Только отмеченные")
    sel_cb = ("pmflt_none" if view["filter"] else "pmflt_sel")
    rows.append([InlineKeyboardButton(sel_btn, callback_data=sel_cb)])
    # массовые действия по текущему фильтру
    rows.append([
        InlineKeyboardButton("✔️ Отметить (фильтр)", callback_data="pmall_on"),
        InlineKeyboardButton("✖️ Снять (фильтр)", callback_data="pmall_off"),
    ])
    rows.append([InlineKeyboardButton("⬅️ К набору", callback_data=f"pmrule:{rid}")])
    return InlineKeyboardMarkup(rows)


def pmteams_kb(rid: int) -> InlineKeyboardMarkup:
    teams = prime_db.distinct_teams()
    rows, row = [], []
    for i, t in enumerate(teams):
        row.append(InlineKeyboardButton(t, callback_data=f"pmteam:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ К парам", callback_data=f"pmpairs:{rid}")])
    return InlineKeyboardMarkup(rows)


# --- стратегия ШОРТ-ХОККЕЙ ПО ПАРАМ (тотал ТБ/ТМ, время прематч/минута) -----

SP_PAGE = 8   # пар на страницу в экране галочек

_SP_TIME_ALIASES = {"pre", "прематч", "премат", "п", "pm", "-1"}


def parse_sp_time_line(raw: str):
    """'pre 8.5 12.5' или '15 8.5 12.5' -> (minute, line_min, line_max) или None.

    Время: 'pre'/'прематч'/'-1' = прематч (минута -1); иначе целое ≥ 0. Границы
    линии можно в любом порядке — упорядочим сами."""
    parts = raw.replace(",", ".").split()
    if len(parts) != 3:
        return None
    t = parts[0].lower()
    if t in _SP_TIME_ALIASES:
        minute = SH_PAIR_PREMATCH
    else:
        try:
            minute = int(parts[0])
        except ValueError:
            return None
        if minute < 0:
            return None
    try:
        a, b = float(parts[1]), float(parts[2])
    except ValueError:
        return None
    if a <= 0 or b <= 0:
        return None
    line_min, line_max = (a, b) if a <= b else (b, a)
    return minute, line_min, line_max


def spstrat_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Наборы (сторона+время+линия+пары)", callback_data="sprules")],
        [InlineKeyboardButton("⚙️ Чат стратегии", callback_data="spchat")],
        [InlineKeyboardButton("📊 Статистика", callback_data="spstats")],
        [InlineKeyboardButton("📈 Отчёты (день/нед/мес)", callback_data="spreports")],
        [InlineKeyboardButton("📥 Excel", callback_data="spexport")],
        [InlineKeyboardButton("🗑 Сброс сигналов", callback_data="spreset_ask")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="strats")],
    ])


def _sp_chat_line() -> str:
    cid = database.get_chat_id(SH_PAIR_STRAT_CODE)
    val = f"<code>{cid}</code>" if cid is not None else "❗️ не задан"
    return f"Чат отправки: {val}"


def spstrat_text() -> str:
    rules = sh_pair_db.get_rules()
    on = sum(1 for r in rules if r["enabled"])
    return (
        "🏒 <b>Стратегия ШХ · Пары (ТБ/ТМ)</b>\n"
        f"Сборщик: {'🟢 работает' if sh_parser_running() else '🔴 остановлен'}\n"
        f"{_sp_chat_line()}\n"
        f"Наборов: {len(rules)} (включено {on})\n\n"
        "Набор = сторона (ТБ/ТМ) + время (Прематч или минута) + диапазон линии "
        "тотала + галочки пар. На заданном моменте матча лиг MNHL / MNHL B по "
        "отмеченной паре, если линия тотала в диапазоне, шлётся сигнал.\n"
        "⚠️ Список пар берётся из сборщика шорт-хоккея (лиги MNHL) — он должен "
        "собирать матчи."
    )


def sp_rule_label(rule: dict) -> str:
    mark = "✅" if rule["enabled"] else "🚫"
    return (f"{mark} {sh_pair_signals.side_label(rule['side'])} · "
            f"{sh_pair_signals.time_label(rule['minute'])} · "
            f"{sh_pair_signals.fmt_range(rule['line_min'], rule['line_max'])} · "
            f"пар {sh_pair_db.count_pairs(rule['id'])}")


def sprules_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(sp_rule_label(r), callback_data=f"sprule:{r['id']}")]
            for r in sh_pair_db.get_rules()]
    rows.append([InlineKeyboardButton("➕ Добавить набор", callback_data="spadd")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="spstrat")])
    return InlineKeyboardMarkup(rows)


def sprules_text() -> str:
    rules = sh_pair_db.get_rules()
    lines = ["📋 <b>Наборы стратегии ШХ · Пары</b>", ""]
    if not rules:
        lines.append("Пока пусто. Нажми «➕ Добавить набор».")
    else:
        lines.append("Тап по набору — сторона/время-линия/пары/удаление.")
    return "\n".join(lines)


def spadd_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"spaddside:{code}")]
            for code, label in SH_PAIR_SIDES.items()]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="sprules")])
    return InlineKeyboardMarkup(rows)


def sprule_kb(rule: dict) -> InlineKeyboardMarkup:
    rid = rule["id"]
    toggle = ("🚫 Выключить" if rule["enabled"] else "✅ Включить")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data=f"sptgl:{rid}")],
        [InlineKeyboardButton(f"🔀 Сторона: {sh_pair_signals.side_label(rule['side'])} → сменить",
                              callback_data=f"spside:{rid}")],
        [InlineKeyboardButton("✏️ Время и линия", callback_data=f"spedit:{rid}")],
        [InlineKeyboardButton(f"☑️ Пары (отмечено {sh_pair_db.count_pairs(rid)})",
                              callback_data=f"sppairs:{rid}")],
        [InlineKeyboardButton("🗑 Удалить набор", callback_data=f"spdel_ask:{rid}")],
        [InlineKeyboardButton("⬅️ К наборам", callback_data="sprules")],
    ])


def sprule_text(rule: dict) -> str:
    st = sh_pair_db.rule_stats(rule["id"])
    lines = [
        f"🏒 <b>Набор · {sh_pair_signals.side_label(rule['side'])}</b>",
        f"{'✅ включено' if rule['enabled'] else '🚫 выключено'}",
        "",
        f"⏱ Время: <b>{sh_pair_signals.time_label(rule['minute'])}</b>",
        f"🎯 Сторона: <b>{sh_pair_signals.side_label(rule['side'])}</b>",
        f"📐 Диапазон линии: <b>{sh_pair_signals.fmt_range(rule['line_min'], rule['line_max'])}</b>",
        f"☑️ Отмечено пар: <b>{sh_pair_db.count_pairs(rule['id'])}</b>",
        "",
        "<b>Статистика</b>",
        f"Сигналов: {st['signals']} | ✅ {st['wins']} | ❌ {st['losses']} | "
        f"↩️ {st['pushes']} | ⏸️ {st['no_result']}",
    ]
    if st["wins"] + st["losses"] > 0:
        lines.append(f"Винрейт: {st['winrate']:.0f}% | ROI: {st['roi']:+.1f}%")
        lines.append(f"Прибыль: {money(st['profit'])}")
    return "\n".join(lines)


def confirm_spdel_kb(rule_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"spdel_yes:{rule_id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"sprule:{rule_id}"),
    ]])


def confirm_spreset_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data="spreset_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="spstrat"),
    ]])


def spreports_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 День", callback_data="sprep:day"),
         InlineKeyboardButton("неделя", callback_data="sprep:week"),
         InlineKeyboardButton("месяц", callback_data="sprep:month")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="spstrat")],
    ])


def spreports_text() -> str:
    return (
        "📈 <b>Отчёты стратегии ШХ · Пары</b>\n\n"
        "Процент прибыли — от банка "
        f"{BANKROLL_START:,.0f}".replace(",", " ") + "₽.\n"
        "• <b>Дневной</b> — авто ежедневно 09:00 МСК (за вчера).\n"
        "• <b>Недельный</b> — авто в понедельник 09:00 МСК (Пн–Вс).\n"
        "• <b>Месячный</b> — авто 1-го числа 09:00 МСК.\n\n"
        f"{_sp_chat_line()}\n\n"
        "Кнопки ниже — отправить вручную сейчас."
    )


def sh_pair_stats_section() -> str:
    """Блок статистики стратегии ШХ · Пары (для общего экрана и экрана статистики)."""
    cid = database.get_chat_id(SH_PAIR_STRAT_CODE)
    lines = ["", "", "🏒 <b>СТРАТЕГИЯ ШХ · ПАРЫ</b>  "
             f"(чат: {'<code>' + str(cid) + '</code>' if cid is not None else 'не задан'})"]
    rules = sh_pair_db.get_rules()
    if not rules:
        lines.append("Наборов ещё нет — добавь в «🏒 Стратегия ШХ пары».")
        return "\n".join(lines)
    tot = sh_pair_db.overall_stats()
    lines.append(f"📌 Сигналов: {tot['signals']} | ✅ {tot['wins']} | ❌ {tot['losses']} | "
                 f"↩️ {tot['pushes']} | ⏸️ {tot['no_result']}")
    if tot["wins"] + tot["losses"] > 0:
        lines.append(f"📈 Винрейт: {tot['winrate']:.0f}% | 🧮 ROI: {tot['roi']:+.1f}% | "
                     f"💰 {money(tot['profit'])}")
    for r in rules:
        st = sh_pair_db.rule_stats(r["id"])
        if st["signals"] == 0:
            continue
        lines += ["", f"• {sh_pair_signals.side_label(r['side'])} · "
                  f"{sh_pair_signals.time_label(r['minute'])} · "
                  f"{sh_pair_signals.fmt_range(r['line_min'], r['line_max'])}: "
                  f"сигналов {st['signals']} | ✅ {st['wins']} | ❌ {st['losses']} | "
                  f"↩️ {st['pushes']} | ⏸️ {st['no_result']}"]
        if st["wins"] + st["losses"] > 0:
            lines.append(f"  🎯 WR {st['winrate']:.0f}% · ROI {st['roi']:+.1f}% · {money(st['profit'])}")
    return "\n".join(lines)


def spstats_text() -> str:
    return _bal_line() + sh_pair_stats_section()


# --- экран галочек пар ШХ (с поиском/фильтром/пагинацией) -------------------

def _sp_view(ctx, rid: int) -> dict:
    v = ctx.user_data.get("sp_view")
    if not v or v.get("rid") != rid:
        v = {"rid": rid, "filter": None, "search": "", "team": "", "page": 0}
        ctx.user_data["sp_view"] = v
    return v


def _sp_filtered_pairs(view: dict) -> list[tuple[str, str]]:
    pairs = sh_pair_db.distinct_pairs()
    f = view.get("filter")
    if f == "team":
        t = view["team"].lower()
        pairs = [p for p in pairs if p[0].lower() == t or p[1].lower() == t]
    elif f == "search":
        s = view["search"].lower()
        pairs = [p for p in pairs if s in p[0].lower() or s in p[1].lower()]
    elif f == "selected":
        sel = sh_pair_db.get_rule_pairs(view["rid"])
        pairs = [p for p in pairs if p in sel]
    return pairs


def _sp_filter_name(view: dict) -> str:
    f = view.get("filter")
    if f == "team":
        return f"команда «{view['team']}»"
    if f == "search":
        return f"поиск «{view['search']}»"
    if f == "selected":
        return "только отмеченные"
    return "все пары"


def sppairs_text(ctx, rid: int) -> str:
    view = _sp_view(ctx, rid)
    rule = sh_pair_db.get_rule(rid)
    pairs = _sp_filtered_pairs(view)
    total = len(sh_pair_db.distinct_pairs())
    sel = sh_pair_db.count_pairs(rid)
    head = (f"{sh_pair_signals.side_label(rule['side'])} · "
            f"{sh_pair_signals.time_label(rule['minute'])}" if rule else "?")
    lines = [
        f"☑️ <b>Пары набора · {head}</b>",
        f"Отмечено: <b>{sel}</b> из {total} пар",
        f"Фильтр: {_sp_filter_name(view)} — найдено {len(pairs)}",
        "",
        "Тап по паре — поставить/снять ✅.",
    ]
    if not pairs:
        lines.append("\nПод фильтр ничего не попало (или сборщик MNHL ещё пуст).")
    return "\n".join(lines)


def sppairs_kb(ctx, rid: int) -> InlineKeyboardMarkup:
    view = _sp_view(ctx, rid)
    pairs = _sp_filtered_pairs(view)
    sel = sh_pair_db.get_rule_pairs(rid)

    pages = max(1, (len(pairs) + SP_PAGE - 1) // SP_PAGE)
    page = max(0, min(view["page"], pages - 1))
    view["page"] = page
    start = page * SP_PAGE
    chunk = pairs[start:start + SP_PAGE]

    rows = []
    for pos, (a, b) in enumerate(chunk, start=start):
        mark = "✅" if (a, b) in sel else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {sh_pair_db.pair_label(a, b)}",
                                          callback_data=f"sptog:{pos}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data="sppg:prev"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="spnop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data="sppg:next"))
    if len(nav) > 1:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("🔤 По команде", callback_data="spflt_team"),
        InlineKeyboardButton("🔍 Поиск", callback_data="spflt_search"),
    ])
    sel_btn = ("❌ Снять фильтр" if view["filter"] else "☑️ Только отмеченные")
    sel_cb = ("spflt_none" if view["filter"] else "spflt_sel")
    rows.append([InlineKeyboardButton(sel_btn, callback_data=sel_cb)])
    rows.append([
        InlineKeyboardButton("✔️ Отметить (фильтр)", callback_data="spall_on"),
        InlineKeyboardButton("✖️ Снять (фильтр)", callback_data="spall_off"),
    ])
    rows.append([InlineKeyboardButton("⬅️ К набору", callback_data=f"sprule:{rid}")])
    return InlineKeyboardMarkup(rows)


def spteams_kb(rid: int) -> InlineKeyboardMarkup:
    teams = sh_pair_db.distinct_teams()
    rows, row = [], []
    for i, t in enumerate(teams):
        row.append(InlineKeyboardButton(t, callback_data=f"spteam:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ К парам", callback_data=f"sppairs:{rid}")])
    return InlineKeyboardMarkup(rows)


# --- стратегия ЧЕТВЕРТИ Pro Жен (тотал ТБ/ТМ текущей четверти по парам) ------

PQ_PAGE = 8   # пар на страницу в экране галочек


def pqstrat_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Наборы (сторона+минуты+пары)", callback_data="pqrules")],
        [InlineKeyboardButton("⚙️ Чат стратегии", callback_data="pqchat")],
        [InlineKeyboardButton("📊 Статистика", callback_data="pqstats")],
        [InlineKeyboardButton("📈 Отчёты (день/нед/мес)", callback_data="pqreports")],
        [InlineKeyboardButton("📥 Excel", callback_data="pqexport")],
        [InlineKeyboardButton("🗑 Сброс сигналов", callback_data="pqreset_ask")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="strats")],
    ])


def _pq_chat_line() -> str:
    cid = database.get_chat_id(PQ_STRAT_CODE)
    val = f"<code>{cid}</code>" if cid is not None else "❗️ не задан"
    return f"Чат отправки: {val}"


def pqstrat_text() -> str:
    rules = pq_db.get_rules()
    on = sum(1 for r in rules if r["enabled"])
    return (
        "🏀 <b>Четверти Pro Жен (ТБ/ТМ четверти)</b>\n"
        f"Парсер: {'🟢 работает' if parser_running() else '🔴 остановлен'}\n"
        f"{_pq_chat_line()}\n"
        f"Наборов: {len(rules)} (включено {on})\n\n"
        "Набор = сторона (ТБ/ТМ) + минуты (напр. 7,17,27) + галочки пар. На каждой "
        "минуте по отмеченной паре берём тотал ТЕКУЩЕЙ четверти (7→1-я, 17→2-я, "
        "27→3-я) и шлём сигнал; расчёт — по счёту этой четверти.\n"
        "⚠️ Список пар берётся из сборщика четвертей Pro жен — он должен собирать "
        "матчи (парсер IPBL запущен)."
    )


def pq_rule_label(rule: dict) -> str:
    mark = "✅" if rule["enabled"] else "🚫"
    return (f"{mark} {pq_signals.side_label(rule['side'])} · "
            f"мин {pq_db.minutes_label(rule)} · пар {pq_db.count_pairs(rule['id'])}")


def pqrules_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(pq_rule_label(r), callback_data=f"pqrule:{r['id']}")]
            for r in pq_db.get_rules()]
    rows.append([InlineKeyboardButton("➕ Добавить набор", callback_data="pqadd")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="pqstrat")])
    return InlineKeyboardMarkup(rows)


def pqrules_text() -> str:
    rules = pq_db.get_rules()
    lines = ["📋 <b>Наборы стратегии Четверти Pro Ж</b>", ""]
    if not rules:
        lines.append("Пока пусто. Нажми «➕ Добавить набор».")
    else:
        lines.append("Тап по набору — сторона/минуты/пары/удаление.")
    return "\n".join(lines)


def pqadd_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"pqaddside:{code}")]
            for code, label in PQ_SIDES.items()]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="pqrules")])
    return InlineKeyboardMarkup(rows)


def pqrule_kb(rule: dict) -> InlineKeyboardMarkup:
    rid = rule["id"]
    toggle = ("🚫 Выключить" if rule["enabled"] else "✅ Включить")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data=f"pqtgl:{rid}")],
        [InlineKeyboardButton(f"🔀 Сторона: {pq_signals.side_label(rule['side'])} → сменить",
                              callback_data=f"pqside:{rid}")],
        [InlineKeyboardButton(f"✏️ Минуты ({pq_db.minutes_label(rule)})",
                              callback_data=f"pqmins:{rid}")],
        [InlineKeyboardButton(f"☑️ Пары (отмечено {pq_db.count_pairs(rid)})",
                              callback_data=f"pqpairs:{rid}")],
        [InlineKeyboardButton("🗑 Удалить набор", callback_data=f"pqdel_ask:{rid}")],
        [InlineKeyboardButton("⬅️ К наборам", callback_data="pqrules")],
    ])


def pqrule_text(rule: dict) -> str:
    st = pq_db.rule_stats(rule["id"])
    lines = [
        f"🏀 <b>Набор · {pq_signals.side_label(rule['side'])}</b>",
        f"{'✅ включено' if rule['enabled'] else '🚫 выключено'}",
        "",
        f"⏱ Минуты: <b>{pq_db.minutes_label(rule)}</b>",
        f"🎯 Сторона: <b>{pq_signals.side_label(rule['side'])}</b> (тотал четверти)",
        f"☑️ Отмечено пар: <b>{pq_db.count_pairs(rule['id'])}</b>",
        "",
        "<b>Статистика</b>",
        f"Сигналов: {st['signals']} | ✅ {st['wins']} | ❌ {st['losses']} | "
        f"↩️ {st['pushes']} | ⏸️ {st['no_result']}",
    ]
    if st["wins"] + st["losses"] > 0:
        lines.append(f"Винрейт: {st['winrate']:.0f}% | ROI: {st['roi']:+.1f}%")
        lines.append(f"Прибыль: {money(st['profit'])}")
    return "\n".join(lines)


def confirm_pqdel_kb(rule_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"pqdel_yes:{rule_id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"pqrule:{rule_id}"),
    ]])


def confirm_pqreset_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data="pqreset_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="pqstrat"),
    ]])


def pqreports_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 День", callback_data="pqrep:day"),
         InlineKeyboardButton("неделя", callback_data="pqrep:week"),
         InlineKeyboardButton("месяц", callback_data="pqrep:month")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="pqstrat")],
    ])


def pqreports_text() -> str:
    return (
        "📈 <b>Отчёты стратегии Четверти Pro Ж</b>\n\n"
        "Процент прибыли — от банка "
        f"{BANKROLL_START:,.0f}".replace(",", " ") + "₽.\n"
        "• <b>Дневной</b> — авто ежедневно 09:00 МСК (за вчера).\n"
        "• <b>Недельный</b> — авто в понедельник 09:00 МСК (Пн–Вс).\n"
        "• <b>Месячный</b> — авто 1-го числа 09:00 МСК.\n\n"
        f"{_pq_chat_line()}\n\n"
        "Кнопки ниже — отправить вручную сейчас."
    )


def pq_stats_section() -> str:
    cid = database.get_chat_id(PQ_STRAT_CODE)
    lines = ["", "", "🏀 <b>ЧЕТВЕРТИ Pro ЖЕН</b>  "
             f"(чат: {'<code>' + str(cid) + '</code>' if cid is not None else 'не задан'})"]
    rules = pq_db.get_rules()
    if not rules:
        lines.append("Наборов ещё нет — добавь в «🏀 Четверти Pro Жен».")
        return "\n".join(lines)
    tot = pq_db.overall_stats()
    lines.append(f"📌 Сигналов: {tot['signals']} | ✅ {tot['wins']} | ❌ {tot['losses']} | "
                 f"↩️ {tot['pushes']} | ⏸️ {tot['no_result']}")
    if tot["wins"] + tot["losses"] > 0:
        lines.append(f"📈 Винрейт: {tot['winrate']:.0f}% | 🧮 ROI: {tot['roi']:+.1f}% | "
                     f"💰 {money(tot['profit'])}")
    for r in rules:
        st = pq_db.rule_stats(r["id"])
        if st["signals"] == 0:
            continue
        lines += ["", f"• {pq_signals.side_label(r['side'])} · мин {pq_db.minutes_label(r)}: "
                  f"сигналов {st['signals']} | ✅ {st['wins']} | ❌ {st['losses']} | "
                  f"↩️ {st['pushes']} | ⏸️ {st['no_result']}"]
        if st["wins"] + st["losses"] > 0:
            lines.append(f"  🎯 WR {st['winrate']:.0f}% · ROI {st['roi']:+.1f}% · {money(st['profit'])}")
    return "\n".join(lines)


def pqstats_text() -> str:
    return _bal_line() + pq_stats_section()


# --- экран галочек пар Четвертей (фильтр/поиск/пагинация) -------------------

def _pq_view(ctx, rid: int) -> dict:
    v = ctx.user_data.get("pq_view")
    if not v or v.get("rid") != rid:
        v = {"rid": rid, "filter": None, "search": "", "team": "", "page": 0}
        ctx.user_data["pq_view"] = v
    return v


def _pq_filtered_pairs(view: dict) -> list[tuple[str, str]]:
    pairs = pq_db.distinct_pairs()
    f = view.get("filter")
    if f == "team":
        t = view["team"].lower()
        pairs = [p for p in pairs if p[0].lower() == t or p[1].lower() == t]
    elif f == "search":
        s = view["search"].lower()
        pairs = [p for p in pairs if s in p[0].lower() or s in p[1].lower()]
    elif f == "selected":
        sel = pq_db.get_rule_pairs(view["rid"])
        pairs = [p for p in pairs if p in sel]
    return pairs


def _pq_filter_name(view: dict) -> str:
    f = view.get("filter")
    if f == "team":
        return f"команда «{view['team']}»"
    if f == "search":
        return f"поиск «{view['search']}»"
    if f == "selected":
        return "только отмеченные"
    return "все пары"


def pqpairs_text(ctx, rid: int) -> str:
    view = _pq_view(ctx, rid)
    rule = pq_db.get_rule(rid)
    pairs = _pq_filtered_pairs(view)
    total = len(pq_db.distinct_pairs())
    sel = pq_db.count_pairs(rid)
    head = (f"{pq_signals.side_label(rule['side'])} · мин {pq_db.minutes_label(rule)}"
            if rule else "?")
    lines = [
        f"☑️ <b>Пары набора · {head}</b>",
        f"Отмечено: <b>{sel}</b> из {total} пар",
        f"Фильтр: {_pq_filter_name(view)} — найдено {len(pairs)}",
        "",
        "Тап по паре — поставить/снять ✅.",
    ]
    if not pairs:
        lines.append("\nПод фильтр ничего не попало (или сборщик четвертей ещё пуст).")
    return "\n".join(lines)


def pqpairs_kb(ctx, rid: int) -> InlineKeyboardMarkup:
    view = _pq_view(ctx, rid)
    pairs = _pq_filtered_pairs(view)
    sel = pq_db.get_rule_pairs(rid)

    pages = max(1, (len(pairs) + PQ_PAGE - 1) // PQ_PAGE)
    page = max(0, min(view["page"], pages - 1))
    view["page"] = page
    start = page * PQ_PAGE
    chunk = pairs[start:start + PQ_PAGE]

    rows = []
    for pos, (a, b) in enumerate(chunk, start=start):
        mark = "✅" if (a, b) in sel else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {pq_db.pair_label(a, b)}",
                                          callback_data=f"pqtog:{pos}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data="pqpg:prev"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="pqnop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data="pqpg:next"))
    if len(nav) > 1:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("🔤 По команде", callback_data="pqflt_team"),
        InlineKeyboardButton("🔍 Поиск", callback_data="pqflt_search"),
    ])
    sel_btn = ("❌ Снять фильтр" if view["filter"] else "☑️ Только отмеченные")
    sel_cb = ("pqflt_none" if view["filter"] else "pqflt_sel")
    rows.append([InlineKeyboardButton(sel_btn, callback_data=sel_cb)])
    rows.append([
        InlineKeyboardButton("✔️ Отметить (фильтр)", callback_data="pqall_on"),
        InlineKeyboardButton("✖️ Снять (фильтр)", callback_data="pqall_off"),
    ])
    rows.append([InlineKeyboardButton("⬅️ К набору", callback_data=f"pqrule:{rid}")])
    return InlineKeyboardMarkup(rows)


def pqteams_kb(rid: int) -> InlineKeyboardMarkup:
    teams = pq_db.distinct_teams()
    rows, row = [], []
    for i, t in enumerate(teams):
        row.append(InlineKeyboardButton(t, callback_data=f"pqteam:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ К парам", callback_data=f"pqpairs:{rid}")])
    return InlineKeyboardMarkup(rows)


# --- handlers --------------------------------------------------------------

def _authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in ADMIN_IDS


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await update.message.reply_text("⛔ Нет доступа к управлению этим ботом.")
        return
    await update.message.reply_text(panel_text(), parse_mode="HTML", reply_markup=main_kb())


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _proc, _sh_proc
    q = update.callback_query
    if not _authorized(update):
        await q.answer("⛔ Нет доступа", show_alert=True)
        return
    await q.answer()
    data = q.data

    if data == "start":
        start_parser()
        await q.edit_message_text("✅ Парсер IPBL запущен.\n\n" + strategy_text(),
                                  parse_mode="HTML", reply_markup=strategy_kb())

    elif data == "stop":
        stop_parser()
        await q.edit_message_text("⏹ Парсер IPBL остановлен.\n\n" + strategy_text(),
                                  parse_mode="HTML", reply_markup=strategy_kb())

    elif data == "panel_start":
        start_panel()
        await q.edit_message_text(
            f"🖥 <b>Веб-панель запущена</b>\n{PANEL_URL}\n\n"
            "Сама выключится через 20 мин простоя (или кнопкой ниже).\n\n" + panel_text(),
            parse_mode="HTML", reply_markup=main_kb(), disable_web_page_preview=True)

    elif data == "panel_stop":
        stop_panel()
        await q.edit_message_text("⏹ Веб-панель остановлена.\n\n" + panel_text(),
                                  parse_mode="HTML", reply_markup=main_kb())

    elif data == "status":
        now = datetime.now(MSK).strftime("%H:%M:%S")
        on = lambda ok: "🟢 работает" if ok else "🔴 остановлен"
        active = database.active_count()
        lines = [f"🏀 <b>Статус</b> — {now} МСК", "",
                 "<b>Инструменты</b>",
                 f"• Парсер IPBL: {on(parser_running())}",
                 f"• Сборщик шорт-хоккея: {on(sh_parser_running())}",
                 f"• Сборщик киберфутбола FC 26: {on(cyber_parser_running())}",
                 f"• Веб-панель: {on(panel_running())}",
                 "",
                 f"Активных сигналов (ждут итога): {active}",
                 "",
                 "<b>Стратегии</b>",
                 f"• 🏀 Стратегия IPBL: {signals.window_status('signal_tm')}",
                 f"• 🏀 Prime ТМ: "
                 f"{_rule_strat_status(prime_db.get_rules('tm'), PRIME_STRAT_CODE_TM, 'наборов')}",
                 f"• 🏀 Prime ИТМ1: "
                 f"{_rule_strat_status(prime_db.get_rules('it1'), PRIME_STRAT_CODE_IT1, 'наборов')}",
                 f"• 🏒 Стратегия хоккея: "
                 f"{_rule_strat_status(database.sh_get_rules(), SH_STRAT_CODE, 'правил')}",
                 f"• 🏒 Стратегия тоталов: "
                 f"{_rule_strat_status(database.sh_total_get_rules(), SH_TOTAL_STRAT_CODE, 'правил')}",
                 f"• 🏒 ШХ пары: "
                 f"{_rule_strat_status(sh_pair_db.get_rules(), SH_PAIR_STRAT_CODE, 'наборов')}",
                 f"• 🏀 Четверти Pro Ж: "
                 f"{_rule_strat_status(pq_db.get_rules(), PQ_STRAT_CODE, 'наборов')}"]
        await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=back_kb())

    elif data == "strats":
        ctx.user_data.pop("await", None)
        ctx.user_data.pop("pm_view", None)
        ctx.user_data.pop("sp_view", None)
        await q.edit_message_text(strats_text(), parse_mode="HTML", reply_markup=strats_kb())

    elif data == "stats":
        await q.edit_message_text(stats_menu_text(), parse_mode="HTML", reply_markup=stats_kb())

    elif data == "stats_tm":
        await q.edit_message_text(stats_tm_text(), parse_mode="HTML", reply_markup=stats_sub_kb())

    elif data == "stats_prime":
        await q.edit_message_text(stats_prime_text(), parse_mode="HTML", reply_markup=stats_sub_kb())

    elif data == "stats_sh":
        await q.edit_message_text(stats_sh_text(), parse_mode="HTML", reply_markup=stats_sub_kb())

    elif data == "stats_sht":
        await q.edit_message_text(stats_sht_text(), parse_mode="HTML", reply_markup=stats_sub_kb())

    elif data == "stats_shp":
        await q.edit_message_text(spstats_text(), parse_mode="HTML", reply_markup=stats_sub_kb())

    elif data == "stats_pq":
        await q.edit_message_text(pqstats_text(), parse_mode="HTML", reply_markup=stats_sub_kb())

    elif data == "export_sig":
        await q.edit_message_text("⏳ Генерирую Excel…", parse_mode="HTML")
        ts = datetime.now(MSK).strftime("%Y%m%d_%H%M%S")
        path = DIR / f"signals_tm_{ts}.xlsx"
        try:
            n = export_signals.build(str(path))
            if n == 0:
                await ctx.bot.send_message(q.message.chat_id, "📊 Сигналов пока нет — нечего выгружать.")
            else:
                with open(path, "rb") as fp:
                    await ctx.bot.send_document(
                        chat_id=q.message.chat_id, document=fp, filename=path.name,
                        caption=f"📊 ТМ-сигналы · записей {n}")
        except Exception as e:
            await ctx.bot.send_message(q.message.chat_id, f"❌ Ошибка экспорта: {e}")
        finally:
            try:
                path.unlink()
            except Exception:
                pass
        await ctx.bot.send_message(q.message.chat_id, strategy_text(),
                                   parse_mode="HTML", reply_markup=strategy_kb())

    elif data == "reports":
        await q.edit_message_text(reports_text(), parse_mode="HTML", reply_markup=reports_kb())

    elif data in ("rep_week", "rep_month"):
        weekly = data == "rep_week"
        text = reports.build_weekly_text() if weekly else reports.build_monthly_text()
        ok, err = await (send_weekly_report(ctx.bot) if weekly else send_monthly_report(ctx.bot))
        title = "Недельный" if weekly else "Месячный"
        if ok:
            head = f"✅ {title} отчёт отправлен в канал. Текст:\n\n<code>{text}</code>"
        else:
            head = f"❌ Не отправлено: {err}\n\nТекст отчёта:\n\n<code>{text}</code>"
        await q.edit_message_text(head, parse_mode="HTML", reply_markup=reports_kb())

    elif data == "collectors":
        await q.edit_message_text(collectors_text(), parse_mode="HTML",
                                  reply_markup=collectors_kb())

    elif data.startswith("col:"):
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if sid not in COLLECTOR_LEAGUES:
            return
        await q.edit_message_text(collector_text(sid), parse_mode="HTML",
                                  reply_markup=collector_kb(sid))

    elif data.startswith("colx:"):
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if sid not in COLLECTOR_LEAGUES:
            return
        name, db = COLLECTOR_LEAGUES[sid]
        st = collector_db.stats(db)
        if st["rows"] == 0:
            await q.edit_message_text(f"📦 {name}: пока пусто — нечего выгружать.",
                                      parse_mode="HTML", reply_markup=collector_kb(sid))
            return
        await q.edit_message_text("⏳ Генерирую Excel…", parse_mode="HTML")
        ts = datetime.now(MSK).strftime("%Y%m%d_%H%M%S")
        fname = Path(db).stem
        path = DIR / f"{fname}_{ts}.xlsx"
        try:
            export_prime.build(str(path), db, f"IPBL {name}")
            with open(path, "rb") as fp:
                await ctx.bot.send_document(
                    chat_id=q.message.chat_id, document=fp, filename=path.name,
                    caption=f"📦 Рынки IPBL · {name} · матчей {st['events']} · строк {st['rows']}")
        except Exception as e:
            await ctx.bot.send_message(q.message.chat_id, f"❌ Ошибка экспорта: {e}")
        finally:
            try:
                path.unlink()
            except Exception:
                pass
        await ctx.bot.send_message(q.message.chat_id, collector_text(sid),
                                   parse_mode="HTML", reply_markup=collector_kb(sid))

    elif data.startswith("colr:"):
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if sid not in COLLECTOR_LEAGUES:
            return
        name = COLLECTOR_LEAGUES[sid][0]
        await q.edit_message_text(
            f"⚠️ <b>Удалить все снимки сборщика IPBL · {name}?</b>\nОтменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_col_reset_kb(sid))

    elif data.startswith("colry:"):
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if sid not in COLLECTOR_LEAGUES:
            return
        name, db = COLLECTOR_LEAGUES[sid]
        collector_db.clear_db(db)
        await q.edit_message_text(f"✅ БД сборщика IPBL · {name} очищена.\n\n" + collector_text(sid),
                                  parse_mode="HTML", reply_markup=collector_kb(sid))

    elif data.startswith("pcol:"):
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if sid not in PERIOD_COLLECTOR_LEAGUES:
            return
        await q.edit_message_text(period_collector_text(sid), parse_mode="HTML",
                                  reply_markup=period_collector_kb(sid))

    elif data.startswith("pcolx:"):
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if sid not in PERIOD_COLLECTOR_LEAGUES:
            return
        name, db = PERIOD_COLLECTOR_LEAGUES[sid]
        st = collector_periods_db.stats(db)
        if st["rows"] == 0:
            await q.edit_message_text(f"📦 Четверти {name}: пока пусто — нечего выгружать.",
                                      parse_mode="HTML", reply_markup=period_collector_kb(sid))
            return
        await q.edit_message_text("⏳ Генерирую Excel…", parse_mode="HTML")
        ts = datetime.now(MSK).strftime("%Y%m%d_%H%M%S")
        fname = Path(db).stem
        path = DIR / f"{fname}_{ts}.xlsx"
        try:
            export_periods.build(str(path), db, f"Четверти {name}")
            with open(path, "rb") as fp:
                await ctx.bot.send_document(
                    chat_id=q.message.chat_id, document=fp, filename=path.name,
                    caption=f"📦 Четверти IPBL · {name} · матчей {st['events']} · строк {st['rows']}")
        except Exception as e:
            await ctx.bot.send_message(q.message.chat_id, f"❌ Ошибка экспорта: {e}")
        finally:
            try:
                path.unlink()
            except Exception:
                pass
        await ctx.bot.send_message(q.message.chat_id, period_collector_text(sid),
                                   parse_mode="HTML", reply_markup=period_collector_kb(sid))

    elif data.startswith("pcolr:"):
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if sid not in PERIOD_COLLECTOR_LEAGUES:
            return
        name = PERIOD_COLLECTOR_LEAGUES[sid][0]
        await q.edit_message_text(
            f"⚠️ <b>Удалить все снимки сборщика четвертей · {name}?</b>\nОтменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_period_reset_kb(sid))

    elif data.startswith("pcolry:"):
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if sid not in PERIOD_COLLECTOR_LEAGUES:
            return
        name, db = PERIOD_COLLECTOR_LEAGUES[sid]
        collector_periods_db.clear_db(db)
        await q.edit_message_text(f"✅ БД сборщика четвертей · {name} очищена.\n\n"
                                  + period_collector_text(sid),
                                  parse_mode="HTML", reply_markup=period_collector_kb(sid))

    elif data == "sh_collector":
        await q.edit_message_text(sh_collector_text(), parse_mode="HTML",
                                  reply_markup=sh_collector_kb())

    elif data == "sh_start":
        start_sh_parser()
        await q.edit_message_text("✅ Сборщик шорт-хоккея запущен.\n\n" + sh_collector_text(),
                                  parse_mode="HTML", reply_markup=sh_collector_kb())

    elif data == "sh_stop":
        stop_sh_parser()
        await q.edit_message_text("⏹ Сборщик шорт-хоккея остановлен.\n\n" + sh_collector_text(),
                                  parse_mode="HTML", reply_markup=sh_collector_kb())

    elif data == "sh_export":
        st = sh_collector_db.stats()
        if st["rows"] == 0:
            await q.edit_message_text("🏒 Сборщик пока пуст — нечего выгружать.",
                                      parse_mode="HTML", reply_markup=sh_collector_kb())
            return
        await q.edit_message_text("⏳ Генерирую Excel…", parse_mode="HTML")
        ts = datetime.now(MSK).strftime("%Y%m%d_%H%M%S")
        path = DIR / f"shorthockey_markets_{ts}.xlsx"
        try:
            export_shorthockey.build(str(path))
            with open(path, "rb") as fp:
                await ctx.bot.send_document(
                    chat_id=q.message.chat_id, document=fp, filename=path.name,
                    caption=f"🏒 Рынки шорт-хоккей · матчей {st['events']} · строк {st['rows']}")
        except Exception as e:
            await ctx.bot.send_message(q.message.chat_id, f"❌ Ошибка экспорта: {e}")
        finally:
            try:
                path.unlink()
            except Exception:
                pass
        await ctx.bot.send_message(q.message.chat_id, sh_collector_text(),
                                   parse_mode="HTML", reply_markup=sh_collector_kb())

    elif data == "sh_reset_ask":
        await q.edit_message_text("⚠️ <b>Удалить все снимки шорт-хоккея из БД?</b>\nОтменить нельзя.",
                                  parse_mode="HTML", reply_markup=confirm_sh_reset_kb())

    elif data == "sh_reset_yes":
        sh_collector_db.clear_db()
        await q.edit_message_text("✅ БД шорт-хоккея очищена.\n\n" + sh_collector_text(),
                                  parse_mode="HTML", reply_markup=sh_collector_kb())

    elif data == "cyber_collector":
        await q.edit_message_text(cyber_collector_text(), parse_mode="HTML",
                                  reply_markup=cyber_collector_kb())

    elif data == "cyber_start":
        start_cyber_parser()
        await q.edit_message_text("✅ Сборщик киберфутбола запущен.\n\n" + cyber_collector_text(),
                                  parse_mode="HTML", reply_markup=cyber_collector_kb())

    elif data == "cyber_stop":
        stop_cyber_parser()
        await q.edit_message_text("⏹ Сборщик киберфутбола остановлен.\n\n" + cyber_collector_text(),
                                  parse_mode="HTML", reply_markup=cyber_collector_kb())

    elif data == "cyber_export":
        st = cyber_collector_db.stats()
        if st["rows"] == 0:
            await q.edit_message_text("🎮 Сборщик пока пуст — нечего выгружать.",
                                      parse_mode="HTML", reply_markup=cyber_collector_kb())
            return
        await q.edit_message_text("⏳ Генерирую Excel…", parse_mode="HTML")
        ts = datetime.now(MSK).strftime("%Y%m%d_%H%M%S")
        path = DIR / f"cyberfootball_markets_{ts}.xlsx"
        try:
            export_cyber.build(str(path))
            with open(path, "rb") as fp:
                await ctx.bot.send_document(
                    chat_id=q.message.chat_id, document=fp, filename=path.name,
                    caption=f"🎮 Рынки киберфутбол FC 26 · матчей {st['events']} · строк {st['rows']}")
        except Exception as e:
            await ctx.bot.send_message(q.message.chat_id, f"❌ Ошибка экспорта: {e}")
        finally:
            try:
                path.unlink()
            except Exception:
                pass
        await ctx.bot.send_message(q.message.chat_id, cyber_collector_text(),
                                   parse_mode="HTML", reply_markup=cyber_collector_kb())

    elif data == "cyber_reset_ask":
        await q.edit_message_text("⚠️ <b>Удалить все снимки киберфутбола из БД?</b>\nОтменить нельзя.",
                                  parse_mode="HTML", reply_markup=confirm_cyber_reset_kb())

    elif data == "cyber_reset_yes":
        cyber_collector_db.clear_db()
        await q.edit_message_text("✅ БД киберфутбола очищена.\n\n" + cyber_collector_text(),
                                  parse_mode="HTML", reply_markup=cyber_collector_kb())

    elif data == "chats":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(chats_text(), parse_mode="HTML", reply_markup=chats_kb())

    elif data == "sched":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(sched_text(), parse_mode="HTML", reply_markup=sched_kb())

    elif data == "strat":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(strategy_text(), parse_mode="HTML", reply_markup=strategy_kb())

    elif data == "thr":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(thr_text(), parse_mode="HTML", reply_markup=thr_kb())

    elif data == "leagues":
        await q.edit_message_text(leagues_text(), parse_mode="HTML", reply_markup=leagues_kb())

    elif data.startswith("togglelg:"):
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        database.toggle_league(sid)
        await q.edit_message_text(leagues_text(), parse_mode="HTML", reply_markup=leagues_kb())

    elif data.startswith("setchat:"):
        code = data.split(":", 1)[1]
        ctx.user_data["await"] = ("chat", code)
        await q.edit_message_text(
            f"Пришли <b>chat_id</b> для <b>{STRATEGIES.get(code, code)}</b>.\n"
            f"Например: <code>-1001234567890</code>\nОтмена — /start", parse_mode="HTML")

    elif data.startswith("setsched:"):
        code = data.split(":", 1)[1]
        ctx.user_data["await"] = ("sched", code)
        await q.edit_message_text(
            f"Пришли окна работы (МСК) для <b>{STRATEGIES.get(code, code)}</b>.\n"
            f"Одно или несколько через запятую:\n"
            f"<code>10:00-12:00, 16:00-18:00, 20:00-22:00</code>\n"
            f"или <code>off</code> — круглосуточно.\nОтмена — /start",
            parse_mode="HTML")

    elif data.startswith("setthr:"):
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if sid not in LEAGUES:
            return
        ctx.user_data["await"] = ("lthr", sid)
        name = league_short(LEAGUES[sid][0])
        await q.edit_message_text(
            f"🎚 <b>Запас сигнала · {name}</b>\n"
            f"Сейчас: <b>{thr_label(sid)}</b>  "
            f"(сигнал при 2×сумма − линия ≤ {thr_label(sid)})\n\n"
            f"Пришли новое значение со знаком, например <code>-16</code> или <code>-18</code>.\n"
            f"Отмена — /start", parse_mode="HTML")

    elif data == "reset_ask":
        await q.edit_message_text(
            "⚠️ <b>Удалить все сигналы из БД стратегий?</b>\n"
            "Сборщики IPBL, шорт-хоккея и киберфутбола не затрагиваются.\nОтменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_reset_kb())

    elif data == "reset_yes":
        database.clear_db()
        await q.edit_message_text("✅ БД стратегий очищена.\n\n" + strategy_text(),
                                  parse_mode="HTML", reply_markup=strategy_kb())

    # --- стратегия хоккея ---------------------------------------------------
    elif data == "shstrat":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(shstrat_text(), parse_mode="HTML", reply_markup=shstrat_kb())

    elif data == "shrules":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(shrules_text(), parse_mode="HTML", reply_markup=shrules_kb())

    elif data == "shadd":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(
            "➕ <b>Добавить лигу</b>\nВыбери лигу шорт-хоккея:",
            parse_mode="HTML", reply_markup=shadd_kb())

    elif data.startswith("shaddlg:"):
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            return
        if not (0 <= idx < len(SH_STRAT_LEAGUES)):
            return
        ctx.user_data["await"] = ("shrule_new", idx)
        name = sh_short_league(SH_STRAT_LEAGUES[idx])
        await q.edit_message_text(
            f"🏒 <b>{name}</b>\n\n"
            "Пришли параметры <b>одной строкой</b>:\n"
            "<code>минута исход кф_от кф_до</code>\n\n"
            "Исход: <code>1</code> (П1), <code>X</code> (ничья), <code>2</code> (П2).\n"
            "Пример: <code>15 X 1.01 2</code>\nОтмена — /start",
            parse_mode="HTML")

    elif data.startswith("shrule:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = database.sh_get_rule(rid)
        if not rule:
            await q.edit_message_text(shrules_text(), parse_mode="HTML", reply_markup=shrules_kb())
            return
        ctx.user_data.pop("await", None)
        await q.edit_message_text(shrule_text(rule), parse_mode="HTML",
                                  reply_markup=shrule_kb(rid, bool(rule["enabled"])))

    elif data.startswith("shtgl:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        database.sh_toggle_rule(rid)
        rule = database.sh_get_rule(rid)
        if not rule:
            await q.edit_message_text(shrules_text(), parse_mode="HTML", reply_markup=shrules_kb())
            return
        await q.edit_message_text(shrule_text(rule), parse_mode="HTML",
                                  reply_markup=shrule_kb(rid, bool(rule["enabled"])))

    elif data.startswith("shedit:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = database.sh_get_rule(rid)
        if not rule:
            return
        ctx.user_data["await"] = ("shrule_edit", rid)
        await q.edit_message_text(
            f"✏️ <b>Изменить · {sh_short_league(rule['sport_name'])}</b>\n"
            f"Сейчас: мин {rule['minute']} · {sh_signals.outcome_label(rule['outcome'])} · "
            f"{sh_signals.fmt_range(rule['kf_min'], rule['kf_max'])}\n\n"
            "Пришли новые параметры одной строкой:\n"
            "<code>минута исход кф_от кф_до</code>\n"
            "Пример: <code>20 1 1.5 2.3</code>\nОтмена — /start",
            parse_mode="HTML")

    elif data.startswith("shdel_ask:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = database.sh_get_rule(rid)
        if not rule:
            return
        await q.edit_message_text(
            f"⚠️ <b>Удалить правило?</b>\n{sh_rule_label(rule)}\nОтменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_shdel_kb(rid))

    elif data.startswith("shdel_yes:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        database.sh_delete_rule(rid)
        await q.edit_message_text("✅ Правило удалено.\n\n" + shrules_text(),
                                  parse_mode="HTML", reply_markup=shrules_kb())

    elif data == "shchat":
        ctx.user_data["await"] = ("shchat", None)
        cid = database.get_chat_id(SH_STRAT_CODE)
        await q.edit_message_text(
            "⚙️ <b>Чат стратегии хоккея</b>\n"
            f"Сейчас: {cid if cid is not None else 'не задан'}\n\n"
            "Пришли <b>chat_id</b> одним сообщением, например <code>-1001234567890</code>.\n"
            "Отмена — /start", parse_mode="HTML")

    elif data == "shstats":
        await q.edit_message_text(shstats_text(), parse_mode="HTML", reply_markup=shstrat_kb())

    elif data == "shreset_ask":
        await q.edit_message_text(
            "⚠️ <b>Удалить все сигналы стратегии хоккея?</b>\n"
            "Правила лиг и собранные рынки не затрагиваются.\nОтменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_shreset_kb())

    elif data == "shreset_yes":
        database.sh_clear_signals()
        await q.edit_message_text("✅ Сигналы стратегии хоккея очищены.\n\n" + shstrat_text(),
                                  parse_mode="HTML", reply_markup=shstrat_kb())

    # --- стратегия ТОТАЛОВ (ТБ/ТМ) ---
    elif data == "shtstrat":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(shtstrat_text(), parse_mode="HTML", reply_markup=shtstrat_kb())

    elif data == "shtrules":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(shtrules_text(), parse_mode="HTML", reply_markup=shtrules_kb())

    elif data == "shtadd":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(
            "➕ <b>Добавить лигу</b>\nВыбери лигу шорт-хоккея:",
            parse_mode="HTML", reply_markup=shtadd_kb())

    elif data.startswith("shtaddlg:"):
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            return
        if not (0 <= idx < len(SH_STRAT_LEAGUES)):
            return
        ctx.user_data["await"] = ("shtrule_new", idx)
        name = sh_short_league(SH_STRAT_LEAGUES[idx])
        await q.edit_message_text(
            f"🏒 <b>{name}</b>\n\n"
            "Пришли параметры <b>одной строкой</b>:\n"
            "<code>минута сторона линия_от линия_до</code>\n\n"
            "Сторона: <code>ТБ</code> (тотал больше) или <code>ТМ</code> (тотал меньше).\n"
            "Пример: <code>15 ТБ 8.5 12.5</code>\nОтмена — /start",
            parse_mode="HTML")

    elif data.startswith("shtrule:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = database.sh_total_get_rule(rid)
        if not rule:
            await q.edit_message_text(shtrules_text(), parse_mode="HTML", reply_markup=shtrules_kb())
            return
        ctx.user_data.pop("await", None)
        await q.edit_message_text(shtrule_text(rule), parse_mode="HTML",
                                  reply_markup=shtrule_kb(rid, bool(rule["enabled"])))

    elif data.startswith("shttgl:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        database.sh_total_toggle_rule(rid)
        rule = database.sh_total_get_rule(rid)
        if not rule:
            await q.edit_message_text(shtrules_text(), parse_mode="HTML", reply_markup=shtrules_kb())
            return
        await q.edit_message_text(shtrule_text(rule), parse_mode="HTML",
                                  reply_markup=shtrule_kb(rid, bool(rule["enabled"])))

    elif data.startswith("shtedit:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = database.sh_total_get_rule(rid)
        if not rule:
            return
        ctx.user_data["await"] = ("shtrule_edit", rid)
        await q.edit_message_text(
            f"✏️ <b>Изменить · {sh_short_league(rule['sport_name'])}</b>\n"
            f"Сейчас: мин {rule['minute']} · {sh_total_signals.side_label(rule['side'])} · "
            f"{sh_total_signals.fmt_range(rule['line_min'], rule['line_max'])}\n\n"
            "Пришли новые параметры одной строкой:\n"
            "<code>минута сторона линия_от линия_до</code>\n"
            "Пример: <code>20 ТМ 9.5 11.5</code>\nОтмена — /start",
            parse_mode="HTML")

    elif data.startswith("shtdel_ask:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = database.sh_total_get_rule(rid)
        if not rule:
            return
        await q.edit_message_text(
            f"⚠️ <b>Удалить правило?</b>\n{sh_total_rule_label(rule)}\nОтменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_shtdel_kb(rid))

    elif data.startswith("shtdel_yes:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        database.sh_total_delete_rule(rid)
        await q.edit_message_text("✅ Правило удалено.\n\n" + shtrules_text(),
                                  parse_mode="HTML", reply_markup=shtrules_kb())

    elif data == "shtchat":
        ctx.user_data["await"] = ("shtchat", None)
        cid = database.get_chat_id(SH_TOTAL_STRAT_CODE)
        await q.edit_message_text(
            "⚙️ <b>Чат стратегии тоталов</b>\n"
            f"Сейчас: {cid if cid is not None else 'не задан'}\n\n"
            "Пришли <b>chat_id</b> одним сообщением, например <code>-1001234567890</code>.\n"
            "Отмена — /start", parse_mode="HTML")

    elif data == "shtstats":
        await q.edit_message_text(shtstats_text(), parse_mode="HTML", reply_markup=shtstrat_kb())

    elif data == "shtreset_ask":
        await q.edit_message_text(
            "⚠️ <b>Удалить все сигналы стратегии тоталов?</b>\n"
            "Правила лиг и собранные рынки не затрагиваются.\nОтменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_shtreset_kb())

    elif data == "shtreset_yes":
        database.sh_total_clear_signals()
        await q.edit_message_text("✅ Сигналы стратегии тоталов очищены.\n\n" + shtstrat_text(),
                                  parse_mode="HTML", reply_markup=shtstrat_kb())

    # --- Prime-стратегия ---------------------------------------------------
    elif data == "pmstrat":
        ctx.user_data.pop("await", None)
        ctx.user_data.pop("pm_view", None)
        await q.edit_message_text(pmstrat_text(), parse_mode="HTML", reply_markup=pmstrat_kb())

    elif data == "pmrules":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(pmrules_text(), parse_mode="HTML", reply_markup=pmrules_kb())

    elif data == "pmadd":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(
            "➕ <b>Новый набор</b>\nВыбери рынок ставки:",
            parse_mode="HTML", reply_markup=pmadd_kb())

    elif data.startswith("pmaddmk:"):
        market = data.split(":", 1)[1]
        if market not in PRIME_MARKETS:
            return
        ctx.user_data["await"] = ("pm_rule_new", market)
        await q.edit_message_text(
            f"🏀 <b>{PRIME_MARKETS[market]}</b>\n\n"
            "Пришли <b>игровую минуту</b> сигнала одним числом, например <code>8</code>.\n"
            "Отмена — /start", parse_mode="HTML")

    elif data.startswith("pmrule:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = prime_db.get_rule(rid)
        if not rule:
            await q.edit_message_text(pmrules_text(), parse_mode="HTML", reply_markup=pmrules_kb())
            return
        ctx.user_data.pop("await", None)
        await q.edit_message_text(pmrule_text(rule), parse_mode="HTML", reply_markup=pmrule_kb(rule))

    elif data.startswith("pmtgl:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        prime_db.toggle_rule(rid)
        rule = prime_db.get_rule(rid)
        if rule:
            await q.edit_message_text(pmrule_text(rule), parse_mode="HTML", reply_markup=pmrule_kb(rule))

    elif data.startswith("pmmk:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = prime_db.get_rule(rid)
        if not rule:
            return
        other = "it1" if rule["market"] == "tm" else "tm"
        prime_db.update_rule(rid, other, rule["minute"])
        rule = prime_db.get_rule(rid)
        await q.edit_message_text(pmrule_text(rule), parse_mode="HTML", reply_markup=pmrule_kb(rule))

    elif data.startswith("pmmin:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = prime_db.get_rule(rid)
        if not rule:
            return
        ctx.user_data["await"] = ("pm_rule_min", rid)
        await q.edit_message_text(
            f"✏️ <b>Минута набора · {prime_signals.market_label(rule['market'])}</b>\n"
            f"Сейчас: {rule['minute']}\n\n"
            "Пришли новую минуту одним числом, например <code>10</code>.\nОтмена — /start",
            parse_mode="HTML")

    elif data.startswith("pmdel_ask:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = prime_db.get_rule(rid)
        if not rule:
            return
        await q.edit_message_text(
            f"⚠️ <b>Удалить набор?</b>\n{pm_rule_label(rule)}\n"
            "Галочки пар набора тоже удалятся. Отменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_pmdel_kb(rid))

    elif data.startswith("pmdel_yes:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        prime_db.delete_rule(rid)
        await q.edit_message_text("✅ Набор удалён.\n\n" + pmrules_text(),
                                  parse_mode="HTML", reply_markup=pmrules_kb())

    # экран галочек пар
    elif data.startswith("pmpairs:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if not prime_db.get_rule(rid):
            return
        ctx.user_data.pop("await", None)
        ctx.user_data["pm_view"] = {"rid": rid, "filter": None, "search": "", "team": "", "page": 0}
        await q.edit_message_text(pmpairs_text(ctx, rid), parse_mode="HTML",
                                  reply_markup=pmpairs_kb(ctx, rid))

    elif data.startswith("pmtog:"):
        view = ctx.user_data.get("pm_view")
        if not view:
            return
        try:
            pos = int(data.split(":", 1)[1])
        except ValueError:
            return
        pairs = _pm_filtered_pairs(view)
        if not (0 <= pos < len(pairs)):
            return
        a, b = pairs[pos]
        prime_db.toggle_pair(view["rid"], a, b)
        await q.edit_message_text(pmpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pmpairs_kb(ctx, view["rid"]))

    elif data in ("pmpg:prev", "pmpg:next"):
        view = ctx.user_data.get("pm_view")
        if not view:
            return
        view["page"] += (-1 if data.endswith("prev") else 1)
        await q.edit_message_text(pmpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pmpairs_kb(ctx, view["rid"]))

    elif data == "pmnop":
        pass

    elif data == "pmflt_none":
        view = ctx.user_data.get("pm_view")
        if not view:
            return
        view.update(filter=None, search="", team="", page=0)
        await q.edit_message_text(pmpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pmpairs_kb(ctx, view["rid"]))

    elif data == "pmflt_sel":
        view = ctx.user_data.get("pm_view")
        if not view:
            return
        view.update(filter="selected", page=0)
        await q.edit_message_text(pmpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pmpairs_kb(ctx, view["rid"]))

    elif data == "pmflt_search":
        view = ctx.user_data.get("pm_view")
        if not view:
            return
        ctx.user_data["await"] = ("pm_search", view["rid"])
        await q.edit_message_text(
            "🔍 <b>Поиск пары</b>\nПришли часть названия команды, например <code>кем</code>.\n"
            "Отмена — /start", parse_mode="HTML")

    elif data == "pmflt_team":
        view = ctx.user_data.get("pm_view")
        if not view:
            return
        await q.edit_message_text(
            "🔤 <b>Фильтр по команде</b>\nВыбери команду:",
            parse_mode="HTML", reply_markup=pmteams_kb(view["rid"]))

    elif data.startswith("pmteam:"):
        view = ctx.user_data.get("pm_view")
        if not view:
            return
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            return
        teams = prime_db.distinct_teams()
        if not (0 <= idx < len(teams)):
            return
        view.update(filter="team", team=teams[idx], page=0)
        await q.edit_message_text(pmpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pmpairs_kb(ctx, view["rid"]))

    elif data in ("pmall_on", "pmall_off"):
        view = ctx.user_data.get("pm_view")
        if not view:
            return
        pairs = _pm_filtered_pairs(view)
        prime_db.set_pairs(view["rid"], pairs, enabled=(data == "pmall_on"))
        await q.edit_message_text(pmpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pmpairs_kb(ctx, view["rid"]))

    elif data.startswith("pmchat:"):
        market = data.split(":", 1)[1]
        if market not in PRIME_MARKETS:
            return
        ctx.user_data["await"] = ("pmchat", market)
        cid = database.get_chat_id(PRIME_STRAT_CHAT[market])
        label = PRIME_MARKETS[market]
        await q.edit_message_text(
            f"⚙️ <b>Чат Prime · {label}</b>\n"
            f"Сейчас: {cid if cid is not None else 'не задан'}\n\n"
            "Пришли <b>chat_id</b> одним сообщением, например <code>-1001234567890</code>.\n"
            "Отмена — /start", parse_mode="HTML")

    elif data == "pmstats":
        await q.edit_message_text(pmstats_text(), parse_mode="HTML", reply_markup=pmstrat_kb())

    elif data == "pmreports":
        await q.edit_message_text(pmreports_text(), parse_mode="HTML", reply_markup=pmreports_kb())

    elif data.startswith("pmrep:"):
        _, market, period = data.split(":", 2)
        if market not in PRIME_MARKETS:
            return
        if period == "day":
            text, title = reports.build_prime_daily_text(market=market), "Дневной"
        elif period == "week":
            text, title = reports.build_prime_weekly_text(market=market), "Недельный"
        else:
            text, title = reports.build_prime_monthly_text(market=market), "Месячный"
        ok, err = await _send_prime_report(ctx.bot, text, market)
        label = PRIME_MARKETS[market]
        if ok:
            head = f"✅ {title} отчёт Prime {label} отправлен. Текст:\n\n<code>{text}</code>"
        else:
            head = f"❌ Не отправлено: {err}\n\nТекст отчёта:\n\n<code>{text}</code>"
        await q.edit_message_text(head, parse_mode="HTML", reply_markup=pmreports_kb())

    elif data.startswith("pmexport:"):
        market = data.split(":", 1)[1]
        if market not in PRIME_MARKETS:
            return
        label = PRIME_MARKETS[market]
        await q.edit_message_text(f"⏳ Генерирую Excel Prime {label}…", parse_mode="HTML")
        ts = datetime.now(MSK).strftime("%Y%m%d_%H%M%S")
        path = DIR / f"prime_signals_{market}_{ts}.xlsx"
        try:
            n = export_prime_signals.build(str(path), market, f"Prime {label}")
            if n == 0:
                await ctx.bot.send_message(q.message.chat_id,
                                           f"📊 Сигналов Prime {label} пока нет — нечего выгружать.")
            else:
                with open(path, "rb") as fp:
                    await ctx.bot.send_document(
                        chat_id=q.message.chat_id, document=fp, filename=path.name,
                        caption=f"📊 Prime {label} · сигналов {n}")
        except Exception as e:
            await ctx.bot.send_message(q.message.chat_id, f"❌ Ошибка экспорта: {e}")
        finally:
            try:
                path.unlink()
            except Exception:
                pass
        await ctx.bot.send_message(q.message.chat_id, pmstrat_text(),
                                   parse_mode="HTML", reply_markup=pmstrat_kb())

    elif data.startswith("pmreset_ask:"):
        market = data.split(":", 1)[1]
        if market not in PRIME_MARKETS:
            return
        label = PRIME_MARKETS[market]
        await q.edit_message_text(
            f"⚠️ <b>Удалить сигналы Prime {label}?</b>\n"
            f"Удалятся только сигналы рынка {label}. Наборы и галочки пар не "
            "затрагиваются.\nОтменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_pmreset_kb(market))

    elif data.startswith("pmreset_yes:"):
        market = data.split(":", 1)[1]
        if market not in PRIME_MARKETS:
            return
        prime_db.clear_signals(market)
        label = PRIME_MARKETS[market]
        await q.edit_message_text(f"✅ Сигналы Prime {label} очищены.\n\n" + pmstrat_text(),
                                  parse_mode="HTML", reply_markup=pmstrat_kb())

    # --- стратегия ШХ · Пары -----------------------------------------------
    elif data == "spstrat":
        ctx.user_data.pop("await", None)
        ctx.user_data.pop("sp_view", None)
        await q.edit_message_text(spstrat_text(), parse_mode="HTML", reply_markup=spstrat_kb())

    elif data == "sprules":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(sprules_text(), parse_mode="HTML", reply_markup=sprules_kb())

    elif data == "spadd":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(
            "➕ <b>Новый набор</b>\nВыбери сторону тотала:",
            parse_mode="HTML", reply_markup=spadd_kb())

    elif data.startswith("spaddside:"):
        side = data.split(":", 1)[1]
        if side not in SH_PAIR_SIDES:
            return
        ctx.user_data["await"] = ("sp_rule_new", side)
        await q.edit_message_text(
            f"🏒 <b>{sh_pair_signals.side_label(side)}</b>\n\n"
            "Пришли <b>одной строкой</b>: <code>время линия_от линия_до</code>\n\n"
            "Время: <code>pre</code> (прематч) или игровая минута числом.\n"
            "Примеры: <code>pre 8.5 12.5</code> · <code>15 9.5 11.5</code>\nОтмена — /start",
            parse_mode="HTML")

    elif data.startswith("sprule:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = sh_pair_db.get_rule(rid)
        if not rule:
            await q.edit_message_text(sprules_text(), parse_mode="HTML", reply_markup=sprules_kb())
            return
        ctx.user_data.pop("await", None)
        await q.edit_message_text(sprule_text(rule), parse_mode="HTML", reply_markup=sprule_kb(rule))

    elif data.startswith("sptgl:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        sh_pair_db.toggle_rule(rid)
        rule = sh_pair_db.get_rule(rid)
        if rule:
            await q.edit_message_text(sprule_text(rule), parse_mode="HTML", reply_markup=sprule_kb(rule))

    elif data.startswith("spside:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = sh_pair_db.get_rule(rid)
        if not rule:
            return
        other = "under" if rule["side"] == "over" else "over"
        sh_pair_db.update_rule(rid, other, rule["minute"], rule["line_min"], rule["line_max"])
        rule = sh_pair_db.get_rule(rid)
        await q.edit_message_text(sprule_text(rule), parse_mode="HTML", reply_markup=sprule_kb(rule))

    elif data.startswith("spedit:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = sh_pair_db.get_rule(rid)
        if not rule:
            return
        ctx.user_data["await"] = ("sp_rule_edit", rid)
        await q.edit_message_text(
            f"✏️ <b>Время и линия · {sh_pair_signals.side_label(rule['side'])}</b>\n"
            f"Сейчас: {sh_pair_signals.time_label(rule['minute'])} · "
            f"{sh_pair_signals.fmt_range(rule['line_min'], rule['line_max'])}\n\n"
            "Пришли новые параметры одной строкой:\n"
            "<code>время линия_от линия_до</code>\n"
            "Примеры: <code>pre 8.5 12.5</code> · <code>15 9.5 11.5</code>\nОтмена — /start",
            parse_mode="HTML")

    elif data.startswith("spdel_ask:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = sh_pair_db.get_rule(rid)
        if not rule:
            return
        await q.edit_message_text(
            f"⚠️ <b>Удалить набор?</b>\n{sp_rule_label(rule)}\n"
            "Галочки пар набора тоже удалятся. Отменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_spdel_kb(rid))

    elif data.startswith("spdel_yes:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        sh_pair_db.delete_rule(rid)
        await q.edit_message_text("✅ Набор удалён.\n\n" + sprules_text(),
                                  parse_mode="HTML", reply_markup=sprules_kb())

    # экран галочек пар
    elif data.startswith("sppairs:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if not sh_pair_db.get_rule(rid):
            return
        ctx.user_data.pop("await", None)
        ctx.user_data["sp_view"] = {"rid": rid, "filter": None, "search": "", "team": "", "page": 0}
        await q.edit_message_text(sppairs_text(ctx, rid), parse_mode="HTML",
                                  reply_markup=sppairs_kb(ctx, rid))

    elif data.startswith("sptog:"):
        view = ctx.user_data.get("sp_view")
        if not view:
            return
        try:
            pos = int(data.split(":", 1)[1])
        except ValueError:
            return
        pairs = _sp_filtered_pairs(view)
        if not (0 <= pos < len(pairs)):
            return
        a, b = pairs[pos]
        sh_pair_db.toggle_pair(view["rid"], a, b)
        await q.edit_message_text(sppairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=sppairs_kb(ctx, view["rid"]))

    elif data in ("sppg:prev", "sppg:next"):
        view = ctx.user_data.get("sp_view")
        if not view:
            return
        view["page"] += (-1 if data.endswith("prev") else 1)
        await q.edit_message_text(sppairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=sppairs_kb(ctx, view["rid"]))

    elif data == "spnop":
        pass

    elif data == "spflt_none":
        view = ctx.user_data.get("sp_view")
        if not view:
            return
        view.update(filter=None, search="", team="", page=0)
        await q.edit_message_text(sppairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=sppairs_kb(ctx, view["rid"]))

    elif data == "spflt_sel":
        view = ctx.user_data.get("sp_view")
        if not view:
            return
        view.update(filter="selected", page=0)
        await q.edit_message_text(sppairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=sppairs_kb(ctx, view["rid"]))

    elif data == "spflt_search":
        view = ctx.user_data.get("sp_view")
        if not view:
            return
        ctx.user_data["await"] = ("sp_search", view["rid"])
        await q.edit_message_text(
            "🔍 <b>Поиск пары</b>\nПришли часть названия команды, например <code>вулв</code>.\n"
            "Отмена — /start", parse_mode="HTML")

    elif data == "spflt_team":
        view = ctx.user_data.get("sp_view")
        if not view:
            return
        await q.edit_message_text(
            "🔤 <b>Фильтр по команде</b>\nВыбери команду:",
            parse_mode="HTML", reply_markup=spteams_kb(view["rid"]))

    elif data.startswith("spteam:"):
        view = ctx.user_data.get("sp_view")
        if not view:
            return
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            return
        teams = sh_pair_db.distinct_teams()
        if not (0 <= idx < len(teams)):
            return
        view.update(filter="team", team=teams[idx], page=0)
        await q.edit_message_text(sppairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=sppairs_kb(ctx, view["rid"]))

    elif data in ("spall_on", "spall_off"):
        view = ctx.user_data.get("sp_view")
        if not view:
            return
        pairs = _sp_filtered_pairs(view)
        sh_pair_db.set_pairs(view["rid"], pairs, enabled=(data == "spall_on"))
        await q.edit_message_text(sppairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=sppairs_kb(ctx, view["rid"]))

    elif data == "spchat":
        ctx.user_data["await"] = ("spchat", None)
        cid = database.get_chat_id(SH_PAIR_STRAT_CODE)
        await q.edit_message_text(
            "⚙️ <b>Чат стратегии ШХ · Пары</b>\n"
            f"Сейчас: {cid if cid is not None else 'не задан'}\n\n"
            "Пришли <b>chat_id</b> одним сообщением, например <code>-1001234567890</code>.\n"
            "Отмена — /start", parse_mode="HTML")

    elif data == "spstats":
        await q.edit_message_text(spstats_text(), parse_mode="HTML", reply_markup=spstrat_kb())

    elif data == "spreports":
        await q.edit_message_text(spreports_text(), parse_mode="HTML", reply_markup=spreports_kb())

    elif data.startswith("sprep:"):
        period = data.split(":", 1)[1]
        if period == "day":
            text, title = reports.build_sh_pair_daily_text(), "Дневной"
        elif period == "week":
            text, title = reports.build_sh_pair_weekly_text(), "Недельный"
        else:
            text, title = reports.build_sh_pair_monthly_text(), "Месячный"
        ok, err = await _send_sh_pair_report(ctx.bot, text)
        if ok:
            head = f"✅ {title} отчёт ШХ · Пары отправлен. Текст:\n\n<code>{text}</code>"
        else:
            head = f"❌ Не отправлено: {err}\n\nТекст отчёта:\n\n<code>{text}</code>"
        await q.edit_message_text(head, parse_mode="HTML", reply_markup=spreports_kb())

    elif data == "spexport":
        await q.edit_message_text("⏳ Генерирую Excel ШХ · Пары…", parse_mode="HTML")
        ts = datetime.now(MSK).strftime("%Y%m%d_%H%M%S")
        path = DIR / f"sh_pair_signals_{ts}.xlsx"
        try:
            n = export_sh_pair.build(str(path))
            if n == 0:
                await ctx.bot.send_message(q.message.chat_id,
                                           "📊 Сигналов ШХ · Пары пока нет — нечего выгружать.")
            else:
                with open(path, "rb") as fp:
                    await ctx.bot.send_document(
                        chat_id=q.message.chat_id, document=fp, filename=path.name,
                        caption=f"📊 ШХ · Пары · сигналов {n}")
        except Exception as e:
            await ctx.bot.send_message(q.message.chat_id, f"❌ Ошибка экспорта: {e}")
        finally:
            try:
                path.unlink()
            except Exception:
                pass
        await ctx.bot.send_message(q.message.chat_id, spstrat_text(),
                                   parse_mode="HTML", reply_markup=spstrat_kb())

    elif data == "spreset_ask":
        await q.edit_message_text(
            "⚠️ <b>Удалить все сигналы стратегии ШХ · Пары?</b>\n"
            "Наборы и галочки пар не затрагиваются.\nОтменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_spreset_kb())

    elif data == "spreset_yes":
        sh_pair_db.clear_signals()
        await q.edit_message_text("✅ Сигналы стратегии ШХ · Пары очищены.\n\n" + spstrat_text(),
                                  parse_mode="HTML", reply_markup=spstrat_kb())

    # --- стратегия Четверти Pro Жен ----------------------------------------
    elif data == "pqstrat":
        ctx.user_data.pop("await", None)
        ctx.user_data.pop("pq_view", None)
        await q.edit_message_text(pqstrat_text(), parse_mode="HTML", reply_markup=pqstrat_kb())

    elif data == "pqrules":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(pqrules_text(), parse_mode="HTML", reply_markup=pqrules_kb())

    elif data == "pqadd":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(
            "➕ <b>Новый набор</b>\nВыбери сторону тотала:",
            parse_mode="HTML", reply_markup=pqadd_kb())

    elif data.startswith("pqaddside:"):
        side = data.split(":", 1)[1]
        if side not in PQ_SIDES:
            return
        ctx.user_data["await"] = ("pq_rule_new", side)
        await q.edit_message_text(
            f"🏀 <b>{pq_signals.side_label(side)}</b>\n\n"
            "Пришли <b>минуты</b> через пробел или запятую, например <code>7,17,27</code>.\n"
            "На каждой минуте берётся тотал текущей четверти (7→1-я, 17→2-я, 27→3-я).\n"
            "Отмена — /start", parse_mode="HTML")

    elif data.startswith("pqrule:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = pq_db.get_rule(rid)
        if not rule:
            await q.edit_message_text(pqrules_text(), parse_mode="HTML", reply_markup=pqrules_kb())
            return
        ctx.user_data.pop("await", None)
        await q.edit_message_text(pqrule_text(rule), parse_mode="HTML", reply_markup=pqrule_kb(rule))

    elif data.startswith("pqtgl:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        pq_db.toggle_rule(rid)
        rule = pq_db.get_rule(rid)
        if rule:
            await q.edit_message_text(pqrule_text(rule), parse_mode="HTML", reply_markup=pqrule_kb(rule))

    elif data.startswith("pqside:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = pq_db.get_rule(rid)
        if not rule:
            return
        other = "under" if rule["side"] == "over" else "over"
        pq_db.update_rule(rid, other, pq_db.minutes_list(rule))
        rule = pq_db.get_rule(rid)
        await q.edit_message_text(pqrule_text(rule), parse_mode="HTML", reply_markup=pqrule_kb(rule))

    elif data.startswith("pqmins:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = pq_db.get_rule(rid)
        if not rule:
            return
        ctx.user_data["await"] = ("pq_rule_mins", rid)
        await q.edit_message_text(
            f"✏️ <b>Минуты набора · {pq_signals.side_label(rule['side'])}</b>\n"
            f"Сейчас: {pq_db.minutes_label(rule)}\n\n"
            "Пришли новые минуты через пробел или запятую, например <code>7,17,27</code>.\n"
            "Отмена — /start", parse_mode="HTML")

    elif data.startswith("pqdel_ask:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        rule = pq_db.get_rule(rid)
        if not rule:
            return
        await q.edit_message_text(
            f"⚠️ <b>Удалить набор?</b>\n{pq_rule_label(rule)}\n"
            "Галочки пар набора тоже удалятся. Отменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_pqdel_kb(rid))

    elif data.startswith("pqdel_yes:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        pq_db.delete_rule(rid)
        await q.edit_message_text("✅ Набор удалён.\n\n" + pqrules_text(),
                                  parse_mode="HTML", reply_markup=pqrules_kb())

    # экран галочек пар
    elif data.startswith("pqpairs:"):
        try:
            rid = int(data.split(":", 1)[1])
        except ValueError:
            return
        if not pq_db.get_rule(rid):
            return
        ctx.user_data.pop("await", None)
        ctx.user_data["pq_view"] = {"rid": rid, "filter": None, "search": "", "team": "", "page": 0}
        await q.edit_message_text(pqpairs_text(ctx, rid), parse_mode="HTML",
                                  reply_markup=pqpairs_kb(ctx, rid))

    elif data.startswith("pqtog:"):
        view = ctx.user_data.get("pq_view")
        if not view:
            return
        try:
            pos = int(data.split(":", 1)[1])
        except ValueError:
            return
        pairs = _pq_filtered_pairs(view)
        if not (0 <= pos < len(pairs)):
            return
        a, b = pairs[pos]
        pq_db.toggle_pair(view["rid"], a, b)
        await q.edit_message_text(pqpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pqpairs_kb(ctx, view["rid"]))

    elif data in ("pqpg:prev", "pqpg:next"):
        view = ctx.user_data.get("pq_view")
        if not view:
            return
        view["page"] += (-1 if data.endswith("prev") else 1)
        await q.edit_message_text(pqpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pqpairs_kb(ctx, view["rid"]))

    elif data == "pqnop":
        pass

    elif data == "pqflt_none":
        view = ctx.user_data.get("pq_view")
        if not view:
            return
        view.update(filter=None, search="", team="", page=0)
        await q.edit_message_text(pqpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pqpairs_kb(ctx, view["rid"]))

    elif data == "pqflt_sel":
        view = ctx.user_data.get("pq_view")
        if not view:
            return
        view.update(filter="selected", page=0)
        await q.edit_message_text(pqpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pqpairs_kb(ctx, view["rid"]))

    elif data == "pqflt_search":
        view = ctx.user_data.get("pq_view")
        if not view:
            return
        ctx.user_data["await"] = ("pq_search", view["rid"])
        await q.edit_message_text(
            "🔍 <b>Поиск пары</b>\nПришли часть названия команды.\nОтмена — /start",
            parse_mode="HTML")

    elif data == "pqflt_team":
        view = ctx.user_data.get("pq_view")
        if not view:
            return
        await q.edit_message_text(
            "🔤 <b>Фильтр по команде</b>\nВыбери команду:",
            parse_mode="HTML", reply_markup=pqteams_kb(view["rid"]))

    elif data.startswith("pqteam:"):
        view = ctx.user_data.get("pq_view")
        if not view:
            return
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            return
        teams = pq_db.distinct_teams()
        if not (0 <= idx < len(teams)):
            return
        view.update(filter="team", team=teams[idx], page=0)
        await q.edit_message_text(pqpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pqpairs_kb(ctx, view["rid"]))

    elif data in ("pqall_on", "pqall_off"):
        view = ctx.user_data.get("pq_view")
        if not view:
            return
        pairs = _pq_filtered_pairs(view)
        pq_db.set_pairs(view["rid"], pairs, enabled=(data == "pqall_on"))
        await q.edit_message_text(pqpairs_text(ctx, view["rid"]), parse_mode="HTML",
                                  reply_markup=pqpairs_kb(ctx, view["rid"]))

    elif data == "pqchat":
        ctx.user_data["await"] = ("pqchat", None)
        cid = database.get_chat_id(PQ_STRAT_CODE)
        await q.edit_message_text(
            "⚙️ <b>Чат стратегии Четверти Pro Ж</b>\n"
            f"Сейчас: {cid if cid is not None else 'не задан'}\n\n"
            "Пришли <b>chat_id</b> одним сообщением, например <code>-1001234567890</code>.\n"
            "Отмена — /start", parse_mode="HTML")

    elif data == "pqstats":
        await q.edit_message_text(pqstats_text(), parse_mode="HTML", reply_markup=pqstrat_kb())

    elif data == "pqreports":
        await q.edit_message_text(pqreports_text(), parse_mode="HTML", reply_markup=pqreports_kb())

    elif data.startswith("pqrep:"):
        period = data.split(":", 1)[1]
        if period == "day":
            text, title = reports.build_pq_daily_text(), "Дневной"
        elif period == "week":
            text, title = reports.build_pq_weekly_text(), "Недельный"
        else:
            text, title = reports.build_pq_monthly_text(), "Месячный"
        ok, err = await _send_pq_report(ctx.bot, text)
        if ok:
            head = f"✅ {title} отчёт Четверти Pro Ж отправлен. Текст:\n\n<code>{text}</code>"
        else:
            head = f"❌ Не отправлено: {err}\n\nТекст отчёта:\n\n<code>{text}</code>"
        await q.edit_message_text(head, parse_mode="HTML", reply_markup=pqreports_kb())

    elif data == "pqexport":
        await q.edit_message_text("⏳ Генерирую Excel Четверти Pro Ж…", parse_mode="HTML")
        ts = datetime.now(MSK).strftime("%Y%m%d_%H%M%S")
        path = DIR / f"pq_signals_{ts}.xlsx"
        try:
            n = export_pq.build(str(path))
            if n == 0:
                await ctx.bot.send_message(q.message.chat_id,
                                           "📊 Сигналов Четверти Pro Ж пока нет — нечего выгружать.")
            else:
                with open(path, "rb") as fp:
                    await ctx.bot.send_document(
                        chat_id=q.message.chat_id, document=fp, filename=path.name,
                        caption=f"📊 Четверти Pro Ж · сигналов {n}")
        except Exception as e:
            await ctx.bot.send_message(q.message.chat_id, f"❌ Ошибка экспорта: {e}")
        finally:
            try:
                path.unlink()
            except Exception:
                pass
        await ctx.bot.send_message(q.message.chat_id, pqstrat_text(),
                                   parse_mode="HTML", reply_markup=pqstrat_kb())

    elif data == "pqreset_ask":
        await q.edit_message_text(
            "⚠️ <b>Удалить все сигналы стратегии Четверти Pro Ж?</b>\n"
            "Наборы и галочки пар не затрагиваются.\nОтменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_pqreset_kb())

    elif data == "pqreset_yes":
        pq_db.clear_signals()
        await q.edit_message_text("✅ Сигналы стратегии Четверти Pro Ж очищены.\n\n" + pqstrat_text(),
                                  parse_mode="HTML", reply_markup=pqstrat_kb())

    elif data == "back":
        ctx.user_data.pop("await", None)
        ctx.user_data.pop("pm_view", None)
        ctx.user_data.pop("sp_view", None)
        ctx.user_data.pop("pq_view", None)
        await q.edit_message_text(panel_text(), parse_mode="HTML", reply_markup=main_kb())


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    pending = ctx.user_data.get("await")
    if not pending:
        return
    kind, code = pending
    raw = (update.message.text or "").strip()

    if kind == "chat":
        try:
            cid = int(raw)
        except ValueError:
            await update.message.reply_text("❌ chat_id должен быть числом. Ещё раз или /start.")
            return
        database.set_chat_id(code, cid)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(
            f"✅ {STRATEGIES.get(code, code)} → chat_id <code>{cid}</code>.",
            parse_mode="HTML", reply_markup=main_kb())

    elif kind == "lthr":
        try:
            num = float(raw.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Нужно число со знаком, например -16 или -18. Ещё раз или /start.")
            return
        sid = code                                   # code здесь = sportId лиги
        database.set_league_threshold(sid, num)      # сохраняем ровно как введено (со знаком)
        ctx.user_data.pop("await", None)
        name = league_short(LEAGUES[sid][0]) if sid in LEAGUES else str(sid)
        await update.message.reply_text(
            f"✅ Запас сигнала · <b>{name}</b> → <b>{thr_label(sid)}</b>.\n"
            f"Новые матчи этой лиги считаются по нему.",
            parse_mode="HTML", reply_markup=thr_kb())

    elif kind == "sched":
        value, ok = parse_windows_input(raw)
        if not ok:
            await update.message.reply_text(
                "❌ Формат: <code>10:00-12:00, 16:00-18:00</code> или <code>off</code>.",
                parse_mode="HTML")
            return
        database.set_windows(code, value)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(
            f"✅ {STRATEGIES.get(code, code)} → {signals.fmt_windows(code)}.",
            parse_mode="HTML", reply_markup=main_kb())

    elif kind == "shchat":
        try:
            cid = int(raw)
        except ValueError:
            await update.message.reply_text("❌ chat_id должен быть числом. Ещё раз или /start.")
            return
        database.set_chat_id(SH_STRAT_CODE, cid)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(
            f"✅ Стратегия хоккея → chat_id <code>{cid}</code>.",
            parse_mode="HTML", reply_markup=shstrat_kb())

    elif kind == "shrule_new":
        parsed = parse_sh_rule_input(raw)
        if not parsed:
            await update.message.reply_text(
                "❌ Формат: <code>минута исход кф_от кф_до</code>, например "
                "<code>15 X 1.01 2</code>. Ещё раз или /start.", parse_mode="HTML")
            return
        idx = code                                   # code здесь = индекс лиги
        minute, outcome, kf_min, kf_max = parsed
        if not (0 <= idx < len(SH_STRAT_LEAGUES)):
            ctx.user_data.pop("await", None)
            return
        database.sh_add_rule(SH_STRAT_LEAGUES[idx], minute, outcome, kf_min, kf_max)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(
            f"✅ Правило добавлено:\n<b>{sh_short_league(SH_STRAT_LEAGUES[idx])}</b> · "
            f"мин {minute} · {sh_signals.outcome_label(outcome)} · "
            f"{sh_signals.fmt_range(kf_min, kf_max)}",
            parse_mode="HTML", reply_markup=shrules_kb())

    elif kind == "shrule_edit":
        parsed = parse_sh_rule_input(raw)
        if not parsed:
            await update.message.reply_text(
                "❌ Формат: <code>минута исход кф_от кф_до</code>, например "
                "<code>15 X 1.01 2</code>. Ещё раз или /start.", parse_mode="HTML")
            return
        rid = code                                   # code здесь = id правила
        minute, outcome, kf_min, kf_max = parsed
        database.sh_update_rule(rid, minute, outcome, kf_min, kf_max)
        ctx.user_data.pop("await", None)
        rule = database.sh_get_rule(rid)
        if rule:
            await update.message.reply_text(
                "✅ Правило изменено.\n\n" + shrule_text(rule), parse_mode="HTML",
                reply_markup=shrule_kb(rid, bool(rule["enabled"])))
        else:
            await update.message.reply_text("✅ Готово.", reply_markup=shrules_kb())

    elif kind == "shtchat":
        try:
            cid = int(raw)
        except ValueError:
            await update.message.reply_text("❌ chat_id должен быть числом. Ещё раз или /start.")
            return
        database.set_chat_id(SH_TOTAL_STRAT_CODE, cid)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(
            f"✅ Стратегия тоталов → chat_id <code>{cid}</code>.",
            parse_mode="HTML", reply_markup=shtstrat_kb())

    elif kind == "shtrule_new":
        parsed = parse_sh_total_rule_input(raw)
        if not parsed:
            await update.message.reply_text(
                "❌ Формат: <code>минута сторона линия_от линия_до</code>, например "
                "<code>15 ТБ 8.5 12.5</code>. Ещё раз или /start.", parse_mode="HTML")
            return
        idx = code                                   # code здесь = индекс лиги
        minute, side, line_min, line_max = parsed
        if not (0 <= idx < len(SH_STRAT_LEAGUES)):
            ctx.user_data.pop("await", None)
            return
        database.sh_total_add_rule(SH_STRAT_LEAGUES[idx], minute, side, line_min, line_max)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(
            f"✅ Правило добавлено:\n<b>{sh_short_league(SH_STRAT_LEAGUES[idx])}</b> · "
            f"мин {minute} · {sh_total_signals.side_label(side)} · "
            f"{sh_total_signals.fmt_range(line_min, line_max)}",
            parse_mode="HTML", reply_markup=shtrules_kb())

    elif kind == "shtrule_edit":
        parsed = parse_sh_total_rule_input(raw)
        if not parsed:
            await update.message.reply_text(
                "❌ Формат: <code>минута сторона линия_от линия_до</code>, например "
                "<code>15 ТБ 8.5 12.5</code>. Ещё раз или /start.", parse_mode="HTML")
            return
        rid = code                                   # code здесь = id правила
        minute, side, line_min, line_max = parsed
        database.sh_total_update_rule(rid, minute, side, line_min, line_max)
        ctx.user_data.pop("await", None)
        rule = database.sh_total_get_rule(rid)
        if rule:
            await update.message.reply_text(
                "✅ Правило изменено.\n\n" + shtrule_text(rule), parse_mode="HTML",
                reply_markup=shtrule_kb(rid, bool(rule["enabled"])))
        else:
            await update.message.reply_text("✅ Готово.", reply_markup=shtrules_kb())

    elif kind == "pmchat":
        try:
            cid = int(raw)
        except ValueError:
            await update.message.reply_text("❌ chat_id должен быть числом. Ещё раз или /start.")
            return
        market = code if code in PRIME_MARKETS else "tm"
        database.set_chat_id(PRIME_STRAT_CHAT[market], cid)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(
            f"✅ Prime {PRIME_MARKETS[market]} → chat_id <code>{cid}</code>.",
            parse_mode="HTML", reply_markup=pmstrat_kb())

    elif kind == "pm_rule_new":
        market = code                                # code здесь = рынок ('tm'|'it1')
        try:
            minute = int(raw)
        except ValueError:
            await update.message.reply_text("❌ Минута — целое число, например 8. Ещё раз или /start.")
            return
        if minute < 0 or market not in PRIME_MARKETS:
            ctx.user_data.pop("await", None)
            return
        rid = prime_db.add_rule(market, minute)
        ctx.user_data.pop("await", None)
        rule = prime_db.get_rule(rid)
        await update.message.reply_text(
            f"✅ Набор создан: <b>{prime_signals.market_label(market)}</b> · мин {minute}.\n"
            "Теперь отметь пары кнопкой «☑️ Пары».",
            parse_mode="HTML", reply_markup=pmrule_kb(rule))

    elif kind == "pm_rule_min":
        rid = code                                   # code здесь = id набора
        try:
            minute = int(raw)
        except ValueError:
            await update.message.reply_text("❌ Минута — целое число, например 10. Ещё раз или /start.")
            return
        rule = prime_db.get_rule(rid)
        if not rule or minute < 0:
            ctx.user_data.pop("await", None)
            return
        prime_db.update_rule(rid, rule["market"], minute)
        ctx.user_data.pop("await", None)
        rule = prime_db.get_rule(rid)
        await update.message.reply_text(
            "✅ Минута изменена.\n\n" + pmrule_text(rule),
            parse_mode="HTML", reply_markup=pmrule_kb(rule))

    elif kind == "pm_search":
        rid = code                                   # code здесь = id набора
        if not prime_db.get_rule(rid):
            ctx.user_data.pop("await", None)
            return
        view = _pm_view(ctx, rid)
        view.update(filter="search", search=raw, page=0)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(pmpairs_text(ctx, rid), parse_mode="HTML",
                                        reply_markup=pmpairs_kb(ctx, rid))

    # --- стратегия ШХ · Пары ---
    elif kind == "spchat":
        try:
            cid = int(raw)
        except ValueError:
            await update.message.reply_text("❌ chat_id должен быть числом. Ещё раз или /start.")
            return
        database.set_chat_id(SH_PAIR_STRAT_CODE, cid)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(
            f"✅ Стратегия ШХ · Пары → chat_id <code>{cid}</code>.",
            parse_mode="HTML", reply_markup=spstrat_kb())

    elif kind == "sp_rule_new":
        side = code                                  # code здесь = сторона ('over'|'under')
        parsed = parse_sp_time_line(raw)
        if not parsed:
            await update.message.reply_text(
                "❌ Формат: <code>время линия_от линия_до</code>, например "
                "<code>pre 8.5 12.5</code> или <code>15 9.5 11.5</code>. Ещё раз или /start.",
                parse_mode="HTML")
            return
        if side not in SH_PAIR_SIDES:
            ctx.user_data.pop("await", None)
            return
        minute, line_min, line_max = parsed
        rid = sh_pair_db.add_rule(side, minute, line_min, line_max)
        ctx.user_data.pop("await", None)
        rule = sh_pair_db.get_rule(rid)
        await update.message.reply_text(
            f"✅ Набор создан: <b>{sh_pair_signals.side_label(side)}</b> · "
            f"{sh_pair_signals.time_label(minute)} · "
            f"{sh_pair_signals.fmt_range(line_min, line_max)}.\n"
            "Теперь отметь пары кнопкой «☑️ Пары».",
            parse_mode="HTML", reply_markup=sprule_kb(rule))

    elif kind == "sp_rule_edit":
        rid = code                                   # code здесь = id набора
        parsed = parse_sp_time_line(raw)
        if not parsed:
            await update.message.reply_text(
                "❌ Формат: <code>время линия_от линия_до</code>, например "
                "<code>pre 8.5 12.5</code> или <code>15 9.5 11.5</code>. Ещё раз или /start.",
                parse_mode="HTML")
            return
        rule = sh_pair_db.get_rule(rid)
        if not rule:
            ctx.user_data.pop("await", None)
            return
        minute, line_min, line_max = parsed
        sh_pair_db.update_rule(rid, rule["side"], minute, line_min, line_max)
        ctx.user_data.pop("await", None)
        rule = sh_pair_db.get_rule(rid)
        await update.message.reply_text(
            "✅ Набор изменён.\n\n" + sprule_text(rule),
            parse_mode="HTML", reply_markup=sprule_kb(rule))

    elif kind == "sp_search":
        rid = code                                   # code здесь = id набора
        if not sh_pair_db.get_rule(rid):
            ctx.user_data.pop("await", None)
            return
        view = _sp_view(ctx, rid)
        view.update(filter="search", search=raw, page=0)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(sppairs_text(ctx, rid), parse_mode="HTML",
                                        reply_markup=sppairs_kb(ctx, rid))

    # --- стратегия Четверти Pro Жен ---
    elif kind == "pqchat":
        try:
            cid = int(raw)
        except ValueError:
            await update.message.reply_text("❌ chat_id должен быть числом. Ещё раз или /start.")
            return
        database.set_chat_id(PQ_STRAT_CODE, cid)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(
            f"✅ Четверти Pro Ж → chat_id <code>{cid}</code>.",
            parse_mode="HTML", reply_markup=pqstrat_kb())

    elif kind == "pq_rule_new":
        side = code                                  # code здесь = сторона ('over'|'under')
        minutes = pq_db.parse_minutes(raw)
        if not minutes:
            await update.message.reply_text(
                "❌ Минуты — числа через пробел/запятую, например <code>7,17,27</code>. "
                "Ещё раз или /start.", parse_mode="HTML")
            return
        if side not in PQ_SIDES:
            ctx.user_data.pop("await", None)
            return
        rid = pq_db.add_rule(side, minutes)
        ctx.user_data.pop("await", None)
        rule = pq_db.get_rule(rid)
        await update.message.reply_text(
            f"✅ Набор создан: <b>{pq_signals.side_label(side)}</b> · "
            f"мин {pq_db.minutes_label(rule)}.\n"
            "Теперь отметь пары кнопкой «☑️ Пары».",
            parse_mode="HTML", reply_markup=pqrule_kb(rule))

    elif kind == "pq_rule_mins":
        rid = code                                   # code здесь = id набора
        minutes = pq_db.parse_minutes(raw)
        if not minutes:
            await update.message.reply_text(
                "❌ Минуты — числа через пробел/запятую, например <code>7,17,27</code>. "
                "Ещё раз или /start.", parse_mode="HTML")
            return
        rule = pq_db.get_rule(rid)
        if not rule:
            ctx.user_data.pop("await", None)
            return
        pq_db.update_rule(rid, rule["side"], minutes)
        ctx.user_data.pop("await", None)
        rule = pq_db.get_rule(rid)
        await update.message.reply_text(
            "✅ Минуты изменены.\n\n" + pqrule_text(rule),
            parse_mode="HTML", reply_markup=pqrule_kb(rule))

    elif kind == "pq_search":
        rid = code                                   # code здесь = id набора
        if not pq_db.get_rule(rid):
            ctx.user_data.pop("await", None)
            return
        view = _pq_view(ctx, rid)
        view.update(filter="search", search=raw, page=0)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(pqpairs_text(ctx, rid), parse_mode="HTML",
                                        reply_markup=pqpairs_kb(ctx, rid))


def _valid_hhmm(s: str) -> bool:
    try:
        h, mm = s.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(mm) <= 59
    except Exception:
        return False


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    print(f"[BOT ERROR] {ctx.error}")


async def _post_init(app):
    """Стартует фоновый планировщик отчётов на общем event loop бота."""
    app.create_task(_report_scheduler(app))
    print("Report scheduler started (weekly Mon 09:00, monthly 1st 09:00; "
          "Prime daily/weekly/monthly 09:00 MSK).")


def main():
    database.init_db()
    prime_db.init_db()
    sh_pair_db.init_db()
    pq_db.init_db()
    # Миграция: старый единый чат Prime (prime_strat) переносим в чат ТМ, если тот
    # ещё не задан. Чтобы после раздельных чатов не потерять текущую настройку.
    _old_prime_chat = database.get_chat_id(PRIME_STRAT_CODE)
    if _old_prime_chat is not None and database.get_chat_id(PRIME_STRAT_CODE_TM) is None:
        database.set_chat_id(PRIME_STRAT_CODE_TM, _old_prime_chat)
        print(f"[MIGRATE] prime_strat chat {_old_prime_chat} -> prime_strat_tm")
    for _name, _db in COLLECTOR_LEAGUES.values():
        collector_db.init_db(_db)
    for _name, _db in PERIOD_COLLECTOR_LEAGUES.values():
        collector_periods_db.init_db(_db)
    sh_collector_db.init_db()
    cyber_collector_db.init_db()
    # Подчищаем «зависшие» парсеры от прошлого инстанса: при рестарте сервиса они
    # умирают не мгновенно, и pgrep внутри start_parser() видит их как живые →
    # запуск пропускается (гонка, из-за которой парсеры не поднимались). Форс-kill
    # гарантирует чистый старт нового набора парсеров.
    subprocess.run(["pkill", "-9", "-f", "/parser.py"], capture_output=True)
    subprocess.run(["pkill", "-9", "-f", "sh_parser.py"], capture_output=True)
    subprocess.run(["pkill", "-9", "-f", "cyber_parser.py"], capture_output=True)
    time.sleep(1.5)
    # Автозапуск парсеров при старте бота (в т.ч. после рестарта сервиса).
    start_parser()
    start_sh_parser()
    start_cyber_parser()
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0,
                           write_timeout=30.0, pool_timeout=30.0)
    app = (Application.builder().token(BOT_TOKEN).request(request)
           .post_init(_post_init).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)
    print("IPBL bot running. Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True, timeout=30, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
