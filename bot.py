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
import export_prime
import export_signals
from config import BOT_TOKEN, STRATEGIES, BANKROLL_START, ADMIN_IDS

DIR = Path(__file__).parent
LOG_FILE = DIR / "parser.log"
MSK = timezone(timedelta(hours=3))
_proc: subprocess.Popen | None = None


# --- helpers ---------------------------------------------------------------

def parser_running() -> bool:
    if _proc is not None and _proc.poll() is None:
        return True
    try:
        r = subprocess.run(["pgrep", "-f", "parser.py"], capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False


def stop_parser():
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
        _proc = None
    subprocess.run(["pkill", "-f", "parser.py"], capture_output=True)


def money(v: float) -> str:
    return f"{v:+,.0f}".replace(",", " ") + "₽"


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
        [InlineKeyboardButton("📦 Сборщик Prime", callback_data="collector")],
        [InlineKeyboardButton("⚙️ Чаты стратегий", callback_data="chats")],
        [InlineKeyboardButton("⏰ Время работы", callback_data="sched")],
        [InlineKeyboardButton("🗑 Сбросить БД", callback_data="reset_ask")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back")]])


def chats_kb() -> InlineKeyboardMarkup:
    rows = []
    for code, name in STRATEGIES.items():
        cid = database.get_chat_id(code)
        rows.append([InlineKeyboardButton(f"{name}: {cid if cid is not None else 'не задан'}",
                                          callback_data=f"setchat:{code}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def sched_kb() -> InlineKeyboardMarkup:
    rows = []
    for code, name in STRATEGIES.items():
        rows.append([InlineKeyboardButton(f"{name}: {signals.fmt_windows(code)}",
                                          callback_data=f"setsched:{code}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Выгрузить сигналы (Excel)", callback_data="export_sig")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
    ])


def collector_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Выгрузить Excel", callback_data="export")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="collector")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
    ])


def confirm_reset_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data="reset_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="back"),
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
        else:
            lines.append(f"📌 Перерывов: {s['matches']} | Сигналов: {s['signals']}")
            lines.append(f"✅ Плюсовые: {s['wins']} | ❌ Минусовые: {s['losses']} | ⏸️ Без итога: {s['no_result']}")
            if s["wins"] + s["losses"] > 0:
                bal = f"{s['balance']:,.0f}".replace(",", " ")
                lines.append(f"📈 Винрейт: {s['winrate']:.0f}%")
                lines.append(f"🧮 ROI: {s['roi']:+.1f}%")
                lines.append(f"💰 Прибыль: {money(s['profit'])}")
                lines.append(f"🏦 Баланс: {bal}₽")
    return "\n".join(lines)


def collector_text() -> str:
    st = collector_db.stats()
    lines = [
        "📦 <b>Сборщик рынков</b> (Prime муж)",
        f"Парсер: {'🟢 работает' if parser_running() else '🔴 остановлен'}",
        "",
        f"Матчей собрано: <b>{st['events']}</b>",
        f"Строк (игровых минут): <b>{st['rows']}</b>",
        f"С результатом: <b>{st['resolved']}</b>",
        "",
    ]
    summ = collector_db.events_summary(15)
    if summ:
        lines.append("Последние матчи:")
        for e in summ:
            fin = e["final_score"] if e["final_score"] else "идёт"
            lines.append(f"• {e['team1']} — {e['team2']}: {e['minutes']} мин · {fin}")
    else:
        lines.append("Пока пусто — ждём Prime-муж матч.")
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
    global _proc
    q = update.callback_query
    if not _authorized(update):
        await q.answer("⛔ Нет доступа", show_alert=True)
        return
    await q.answer()
    data = q.data

    if data == "start":
        if not parser_running():
            f = open(LOG_FILE, "a")
            # -u: небуферизованный вывод, чтобы parser.log обновлялся в реальном времени
            _proc = subprocess.Popen([sys.executable, "-u", str(DIR / "parser.py")],
                                     cwd=str(DIR), stdout=f, stderr=subprocess.STDOUT)
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

    elif data == "collector":
        await q.edit_message_text(collector_text(), parse_mode="HTML",
                                  reply_markup=collector_kb())

    elif data == "export":
        st = collector_db.stats()
        if st["rows"] == 0:
            await q.edit_message_text("📦 Сборщик пока пуст — нечего выгружать.",
                                      parse_mode="HTML", reply_markup=collector_kb())
            return
        await q.edit_message_text("⏳ Генерирую Excel…", parse_mode="HTML")
        ts = datetime.now(MSK).strftime("%Y%m%d_%H%M%S")
        path = DIR / f"prime_markets_{ts}.xlsx"
        try:
            export_prime.build(str(path))
            with open(path, "rb") as fp:
                await ctx.bot.send_document(
                    chat_id=q.message.chat_id, document=fp, filename=path.name,
                    caption=f"📦 Рынки Prime · матчей {st['events']} · строк {st['rows']}")
        except Exception as e:
            await ctx.bot.send_message(q.message.chat_id, f"❌ Ошибка экспорта: {e}")
        finally:
            try:
                path.unlink()
            except Exception:
                pass
        await ctx.bot.send_message(q.message.chat_id, collector_text(),
                                   parse_mode="HTML", reply_markup=collector_kb())

    elif data == "chats":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(chats_text(), parse_mode="HTML", reply_markup=chats_kb())

    elif data == "sched":
        ctx.user_data.pop("await", None)
        await q.edit_message_text(sched_text(), parse_mode="HTML", reply_markup=sched_kb())

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

    elif data == "reset_ask":
        await q.edit_message_text("⚠️ <b>Удалить все сигналы из БД?</b>\nОтменить нельзя.",
                                  parse_mode="HTML", reply_markup=confirm_reset_kb())

    elif data == "reset_yes":
        database.clear_db()
        await q.edit_message_text("✅ БД очищена.\n\n" + panel_text(),
                                  parse_mode="HTML", reply_markup=main_kb())

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
