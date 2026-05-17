import logging
import threading
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import sqlite3
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
from config import TELEGRAM_TOKEN

class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, format, *args):
        pass

def keep_alive():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

ENTER_NAME, ENTER_DOSE, ENTER_TIMES, ENTER_DAYS = range(4)

# Популярные часовые пояса России и СНГ
TIMEZONES = {
    "🏙 Калининград (UTC+2)": 2,
    "🏙 Москва (UTC+3)": 3,
    "🏙 Самара (UTC+4)": 4,
    "🏙 Екатеринбург (UTC+5)": 5,
    "🏙 Омск (UTC+6)": 6,
    "🏙 Томск / Красноярск (UTC+7)": 7,
    "🏙 Иркутск (UTC+8)": 8,
    "🏙 Якутск (UTC+9)": 9,
    "🏙 Владивосток (UTC+10)": 10,
    "🏙 Магадан (UTC+11)": 11,
    "🏙 Камчатка (UTC+12)": 12,
}

# ── База данных ───────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("pills.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            timezone_offset INTEGER DEFAULT 3
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS pills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            dose TEXT,
            times TEXT,
            days_total INTEGER,
            days_left INTEGER,
            active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS taken_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            pill_id INTEGER,
            taken_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_conn():
    return sqlite3.connect("pills.db")


def get_user_tz(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT timezone_offset FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 3


def set_user_tz(user_id, offset):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, timezone_offset) VALUES (?, ?)", (user_id, offset))
    conn.commit()
    conn.close()


def add_pill(user_id, name, dose, times, days_total):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO pills (user_id, name, dose, times, days_total, days_left, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, name, dose, times, days_total, days_total, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_user_pills(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM pills WHERE user_id=? AND active=1", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_pill_by_id(pill_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM pills WHERE id=?", (pill_id,))
    row = c.fetchone()
    conn.close()
    return row


def mark_taken(user_id, pill_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO taken_log (user_id, pill_id, taken_at) VALUES (?, ?, ?)",
              (user_id, pill_id, datetime.now().isoformat()))
    c.execute("UPDATE pills SET days_left = MAX(0, days_left - 1) WHERE id=? AND days_total > 0", (pill_id,))
    c.execute("UPDATE pills SET active=0 WHERE id=? AND days_left=0 AND days_total > 0", (pill_id,))
    conn.commit()
    conn.close()


def delete_pill(pill_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE pills SET active=0 WHERE id=?", (pill_id,))
    conn.commit()
    conn.close()


def parse_times(times_str):
    times = []
    for t in times_str.split(","):
        t = t.strip()
        try:
            h, m = t.split(":")
            times.append((int(h), int(m)))
        except:
            pass
    return times


def local_to_utc(h, m, tz_offset):
    """Конвертируем локальное время в UTC"""
    total_minutes = h * 60 + m - tz_offset * 60
    total_minutes = total_minutes % (24 * 60)
    return total_minutes // 60, total_minutes % 60


def format_pill(pill):
    pid, uid, name, dose, times, days_total, days_left, active, created = pill
    times_str = ", ".join(t.strip() for t in times.split(","))
    text = f"💊 *{name}*"
    if dose:
        text += f" — {dose}"
    text += f"\n🕐 Время: {times_str}"
    if days_total > 0:
        text += f"\n📅 Осталось дней: {days_left} из {days_total}"
    else:
        text += f"\n📅 Постоянный приём"
    return text


# ── Команды ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    exists = c.fetchone()
    conn.close()

   if exists:
        await update.message.reply_text(
            "Привет! 💊 Выбери действие 👇",
            reply_markup=ReplyKeyboardMarkup(
                [
                    ["➕ Добавить таблетку", "📋 Мои таблетки"],
                    ["✅ Выпил", "🗑 Удалить"],
                    ["🌍 Часовой пояс"],
                ],
                resize_keyboard=True,
            )
        )
    else:
        await ask_timezone(update, context)


async def ask_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for name, offset in TIMEZONES.items():
        keyboard.append([InlineKeyboardButton(name, callback_data=f"tz_{offset}")])
    await update.message.reply_text(
        "Привет! 💊 Я помогу не забывать пить таблетки.\n\n"
        "Сначала выбери свой часовой пояс 👇",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def timezone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for name, offset in TIMEZONES.items():
        keyboard.append([InlineKeyboardButton(name, callback_data=f"tz_{offset}")])
    await update.message.reply_text(
        "Выбери свой часовой пояс 👇",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def timezone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    offset = int(query.data.split("_")[1])
    user_id = update.effective_user.id
    set_user_tz(user_id, offset)

    tz_name = next((k for k, v in TIMEZONES.items() if v == offset), f"UTC+{offset}")
    await query.edit_message_text(
        f"✅ Часовой пояс сохранён: {tz_name}\n\n"
        "Теперь добавь первую таблетку через /add 💊"
    )


async def list_pills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pills = get_user_pills(user_id)
    if not pills:
        await update.message.reply_text("Нет активных таблеток. Добавь через /add 💊")
        return
    text = "Твои таблетки:\n\n"
    for pill in pills:
        text += format_pill(pill) + "\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")


async def took_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pills = get_user_pills(user_id)
    if not pills:
        await update.message.reply_text("Нет активных таблеток. Добавь через /add 💊")
        return
    keyboard = []
    for pill in pills:
        pid, uid, name, dose, *_ = pill
        label = f"✅ {name}" + (f" ({dose})" if dose else "")
        keyboard.append([InlineKeyboardButton(label, callback_data=f"took_{pid}")])
    await update.message.reply_text("Что выпил? 👇", reply_markup=InlineKeyboardMarkup(keyboard))


async def took_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pill_id = int(query.data.split("_")[1])
    user_id = update.effective_user.id
    pill = get_pill_by_id(pill_id)
    if not pill:
        await query.edit_message_text("Таблетка не найдена 🤔")
        return
    mark_taken(user_id, pill_id)
    name = pill[2]
    updated = get_pill_by_id(pill_id)
    if updated and updated[6] == 0 and updated[5] > 0:
        await query.edit_message_text(f"✅ {name} отмечена!\n\n🎉 Курс завершён, молодец!")
    else:
        await query.edit_message_text(f"✅ {name} отмечена! Так держать 💪")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pills = get_user_pills(user_id)
    if not pills:
        await update.message.reply_text("Нет активных таблеток.")
        return
    keyboard = []
    for pill in pills:
        pid, uid, name, *_ = pill
        keyboard.append([InlineKeyboardButton(f"🗑 {name}", callback_data=f"del_{pid}")])
    await update.message.reply_text("Какую удалить?", reply_markup=InlineKeyboardMarkup(keyboard))


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pill_id = int(query.data.split("_")[1])
    pill = get_pill_by_id(pill_id)
    if pill:
        delete_pill(pill_id)
        await query.edit_message_text(f"🗑 {pill[2]} удалена.")
    else:
        await query.edit_message_text("Не найдено.")


# ── Добавление таблетки ───────────────────────────────────────────────────────

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Как называется таблетка или лекарство?\n\nНапример: *Аугментин*, *Витамин Д*, *Сироп от кашля*",
        parse_mode="Markdown"
    )
    return ENTER_NAME


async def enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        "Какая доза?\n\nНапример: *1 таблетка*, *5 мл*, *2 капсулы*\nИли напиши *—* если не важно",
        parse_mode="Markdown"
    )
    return ENTER_DOSE


async def enter_dose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dose = update.message.text.strip()
    context.user_data["dose"] = "" if dose == "—" else dose
    await update.message.reply_text(
        "В какое время принимать? (по твоему местному времени)\n\n"
        "Напиши через запятую:\n"
        "*8:00* — один раз утром\n"
        "*8:00, 20:00* — утром и вечером\n"
        "*8:00, 14:00, 20:00* — три раза в день",
        parse_mode="Markdown"
    )
    return ENTER_TIMES


async def enter_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    times_str = update.message.text.strip()
    times = parse_times(times_str)
    if not times:
        await update.message.reply_text(
            "Не понял формат 🤔 Напиши вот так: *8:00* или *8:00, 20:00*",
            parse_mode="Markdown"
        )
        return ENTER_TIMES
    context.user_data["times"] = times_str
    await update.message.reply_text(
        "Сколько дней принимать?\n\nНапиши число, например *7* или *14*\nЕсли постоянно — напиши *0*",
        parse_mode="Markdown"
    )
    return ENTER_DAYS


async def enter_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(update.message.text.strip())
    except:
        await update.message.reply_text("Напиши число, например *7* или *0*", parse_mode="Markdown")
        return ENTER_DAYS

    user_id = update.effective_user.id
    name = context.user_data["name"]
    dose = context.user_data["dose"]
    times_str = context.user_data["times"]
    tz_offset = get_user_tz(user_id)

    add_pill(user_id, name, dose, times_str, days)

    # Планируем напоминания с учётом часового пояса
    times = parse_times(times_str)
    for h, m in times:
        utc_h, utc_m = local_to_utc(h, m, tz_offset)
        context.job_queue.run_daily(
            send_reminder,
            time=__import__("datetime").time(hour=utc_h, minute=utc_m, tzinfo=timezone.utc),
            data={"user_id": user_id, "name": name, "dose": dose},
            name=f"pill_{user_id}_{name}_{h}_{m}"
        )

    days_text = f"{days} дней" if days > 0 else "постоянно"
    await update.message.reply_text(
        f"✅ Добавлено!\n\n"
        f"💊 *{name}*" + (f" — {dose}" if dose else "") + f"\n"
        f"🕐 Время: {times_str}\n"
        f"📅 Курс: {days_text}\n\n"
        f"Буду напоминать вовремя 🌸",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ── Напоминания ───────────────────────────────────────────────────────────────

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    user_id = data["user_id"]
    name = data["name"]
    dose = data["dose"]

    # Проверяем что таблетка ещё активна
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM pills WHERE user_id=? AND name=? AND active=1", (user_id, name))
    pill = c.fetchone()
    conn.close()

    if not pill:
        context.job.schedule_removal()
        return

    text = f"💊 Время выпить *{name}*!"
    if dose:
        text += f"\nДоза: {dose}"

    keyboard = [[InlineKeyboardButton("✅ Выпил", callback_data=f"took_{pill[0]}")]]
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logging.error(f"Ошибка напоминания {user_id}: {e}")



async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "➕ Добавить таблетку":
        return await add_start(update, context)
    elif text == "📋 Мои таблетки":
        return await list_pills(update, context)
    elif text == "✅ Выпил":
        return await took_cmd(update, context)
    elif text == "🗑 Удалить":
        return await delete_cmd(update, context)
    elif text == "🌍 Часовой пояс":
        return await timezone_cmd(update, context)


# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    add_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ENTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_name)],
            ENTER_DOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_dose)],
            ENTER_TIMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_times)],
            ENTER_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_days)],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_pills))
    app.add_handler(CommandHandler("took", took_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("timezone", timezone_cmd))
    app.add_handler(add_handler)
    app.add_handler(CallbackQueryHandler(timezone_callback, pattern="^tz_"))
    app.add_handler(CallbackQueryHandler(took_callback, pattern="^took_"))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern="^del_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    keep_alive()
    print("💊 Выпил? — бот запущен...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
