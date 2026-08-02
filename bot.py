"""Telegram-бот управления fonbet-ipbl (кнопочная инлайн-панель).

/start — панель. Управление парсером, статистика стратегий (винрейт+прибыль),
chat_id и окно работы (МСК) на каждую стратегию, сброс БД.
"""
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, ContextTypes, filters)
from telegram.request import HTTPXRequest

import database
import signals
import collector_db
import collector_periods_db
import export_prime
import export_periods
import export_signals
import sh_collector_db
import export_shorthockey
from config import (BOT_TOKEN, STRATEGIES, BANKROLL_START, ADMIN_IDS, LEAGUES,
                    COLLECTOR_LEAGUES, PERIOD_COLLECTOR_LEAGUES)

DIR = Path(__file__).parent
LOG_FILE = DIR / "parser.log"
SH_LOG_FILE = DIR / "sh_parser.log"
MSK = timezone(timedelta(hours=3))
_proc: subprocess.Popen | None = None
_sh_proc: subprocess.Popen | None = None


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
    toggle = (InlineKeyboardButton("⏹ Остановить парсер", callback_data="stop")
              if parser_running() else
              InlineKeyboardButton("▶️ Запустить парсер", callback_data="start"))
    return InlineKeyboardMarkup([
        [toggle],
        [InlineKeyboardButton("📊 Статус", callback_data="status")],
        [InlineKeyboardButton("🤖 Статистика стратегий", callback_data="stats")],
        [InlineKeyboardButton("📦 Сборщики", callback_data="collectors")],
        [InlineKeyboardButton("🎯 Стратегия", callback_data="strat")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back")]])


# --- хаб «Сборщики»: все сборщики в одном месте ----------------------------

def collectors_kb() -> InlineKeyboardMarkup:
    """Список всех сборщиков: 4 лиги IPBL (за матч) + четверти Pro + шорт-хоккей."""
    rows = []
    for sid, (name, _db) in COLLECTOR_LEAGUES.items():
        rows.append([InlineKeyboardButton(f"🏀 IPBL · {name}", callback_data=f"col:{sid}")])
    for sid, (name, _db) in PERIOD_COLLECTOR_LEAGUES.items():
        rows.append([InlineKeyboardButton(f"🏀 Четверти · {name}", callback_data=f"pcol:{sid}")])
    rows.append([InlineKeyboardButton("🏒 Шорт-хоккей", callback_data="sh_collector")])
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


# --- хаб «Стратегия»: настройки сигналов в одном месте ---------------------

def strategy_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎚 Запас сигнала (по лигам)", callback_data="thr")],
        [InlineKeyboardButton("🏀 Лиги (вкл/выкл)", callback_data="leagues")],
        [InlineKeyboardButton("⚙️ Чаты стратегий", callback_data="chats")],
        [InlineKeyboardButton("⏰ Время работы", callback_data="sched")],
        [InlineKeyboardButton("🗑 Сбросить БД стратегий", callback_data="reset_ask")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Выгрузить сигналы (Excel)", callback_data="export_sig")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
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
    st = "🟢 работает" if parser_running() else "🔴 остановлен"
    return f"🏀 <b>IPBL Bot</b>\nПарсер: {st}"


def stats_text() -> str:
    bal0 = f"{BANKROLL_START:,.0f}".replace(",", " ")
    lines = ["📊 <b>СТАТИСТИКА СТРАТЕГИЙ</b>", "", f"💰 Стартовый баланс: {bal0}₽"]
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


def collectors_text() -> str:
    """Хаб сборщиков: сводка по всем в одном экране."""
    lines = [
        "📦 <b>Сборщики рынков</b>",
        f"Парсер IPBL: {'🟢 работает' if parser_running() else '🔴 остановлен'}",
        f"Шорт-хоккей: {'🟢 работает' if sh_parser_running() else '🔴 остановлен'}",
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
    return ("🎯 <b>Стратегия ТМ</b>\n"
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
        await q.edit_message_text("✅ Парсер запущен.\n\n" + panel_text(),
                                  parse_mode="HTML", reply_markup=main_kb())

    elif data == "stop":
        stop_parser()
        await q.edit_message_text("⏹ Парсер остановлен.\n\n" + panel_text(),
                                  parse_mode="HTML", reply_markup=main_kb())

    elif data == "status":
        now = datetime.now(MSK).strftime("%H:%M:%S")
        st = "🟢 работает" if parser_running() else "🔴 остановлен"
        active = database.active_count()
        lines = [f"🏀 <b>Статус</b> — {now} МСК", f"Парсер: {st}",
                 f"Активных сигналов (ждут итога): {active}", ""]
        for code, name in STRATEGIES.items():
            lines.append(f"• <b>{name}</b>: {signals.window_status(code)}")
        await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=back_kb())

    elif data == "stats":
        await q.edit_message_text(stats_text(), parse_mode="HTML", reply_markup=stats_kb())

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
        await ctx.bot.send_message(q.message.chat_id, stats_text(),
                                   parse_mode="HTML", reply_markup=stats_kb())

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
            "Сборщики IPBL и шорт-хоккея не затрагиваются.\nОтменить нельзя.",
            parse_mode="HTML", reply_markup=confirm_reset_kb())

    elif data == "reset_yes":
        database.clear_db()
        await q.edit_message_text("✅ БД стратегий очищена.\n\n" + strategy_text(),
                                  parse_mode="HTML", reply_markup=strategy_kb())

    elif data == "back":
        ctx.user_data.pop("await", None)
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


def _valid_hhmm(s: str) -> bool:
    try:
        h, mm = s.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(mm) <= 59
    except Exception:
        return False


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    print(f"[BOT ERROR] {ctx.error}")


def main():
    database.init_db()
    for _name, _db in COLLECTOR_LEAGUES.values():
        collector_db.init_db(_db)
    for _name, _db in PERIOD_COLLECTOR_LEAGUES.values():
        collector_periods_db.init_db(_db)
    sh_collector_db.init_db()
    # Автозапуск парсеров при старте бота (в т.ч. после рестарта сервиса).
    start_parser()
    start_sh_parser()
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0,
                           write_timeout=30.0, pool_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)
    print("IPBL bot running. Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True, timeout=30, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
