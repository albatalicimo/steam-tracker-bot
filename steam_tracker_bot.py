import logging
import os
import sqlite3
import asyncio
from datetime import datetime, timedelta

import aiohttp

from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ForceReply
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.request import HTTPXRequest

load_dotenv()

# ====================== НАСТРОЙКИ ======================
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

STEAM_API_KEY = os.getenv("STEAM_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not STEAM_API_KEY or not TELEGRAM_BOT_TOKEN:
    raise ValueError("❌ Не найдены STEAM_API_KEY или TELEGRAM_BOT_TOKEN в .env / переменных окружения!")

CHECK_INTERVAL = 180          # 3 минуты — оптимально для хостинга
DB_NAME = "steam_tracker.db"

# Увеличенные таймауты + защита
request = HTTPXRequest(connect_timeout=60.0, read_timeout=60.0, write_timeout=60.0, pool_timeout=60.0)

print("✅ .env загружен. Бот запускается...")

# ====================== БАЗА ДАННЫХ ======================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tracked_users (
        chat_id INTEGER, steam_id TEXT, name TEXT, is_active INTEGER DEFAULT 1,
        PRIMARY KEY (chat_id, steam_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER, steam_id TEXT, status INTEGER,
        start_time TEXT, duration_sec INTEGER
    )''')
    conn.commit()
    conn.close()

init_db()

user_tracking = {}  # chat_id -> {steam_id: {...}}

# ====================== ВСПОМОГАТЕЛЬНЫЕ ======================
def get_tracked_users(chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT steam_id, name FROM tracked_users WHERE chat_id = ? AND is_active = 1", (chat_id,))
    users = c.fetchall()
    conn.close()
    return users

def get_user_history(chat_id: int, steam_id: str, hours: int = 24):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""SELECT status, start_time, duration_sec FROM status_history 
                 WHERE chat_id = ? AND steam_id = ? AND start_time >= datetime('now', ?)
                 ORDER BY start_time""", 
              (chat_id, steam_id, f'-{hours} hours'))
    rows = c.fetchall()
    conn.close()
    return rows

def get_status_name(status: int) -> str:
    statuses = {0:"🔴 Оффлайн", 1:"🟢 Онлайн", 2:"🟡 Занят", 3:"🟠 Отошёл", 4:"💤 Спит", 5:"💰 Торговля", 6:"🎮 Хочет играть"}
    return statuses.get(status, "❓")

# ====================== STEAM API С ЗАЩИТОЙ ======================
async def resolve_steam_id(text: str) -> str | None:
    text = text.strip()
    if text.isdigit() and len(text) == 17:
        return text
    vanity = text.split("/")[-1] if "steamcommunity.com" in text else text
    url = f"https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/?key={STEAM_API_KEY}&vanityurl={vanity}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url) as r:
                data = await r.json()
                if data.get("response", {}).get("success") == 1:
                    return data["response"].get("steamid")
    except Exception as e:
        logging.error(f"Resolve error: {e}")
    return None

async def get_steam_summary(steam_id: str):
    url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={steam_id}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url) as r:
                data = await r.json()
                players = data.get("response", {}).get("players")
                return players[0] if players else None
    except asyncio.TimeoutError:
        logging.warning(f"Timeout summary для {steam_id}")
    except Exception as e:
        logging.error(f"Summary error {steam_id}: {e}")
    return None

# ====================== КЛАВИАТУРЫ ======================
def get_main_keyboard():
    return ReplyKeyboardMarkup([
        ["📋 Мои отслеживания", "➕ Добавить"],
        ["📊 Отчёты"]
    ], resize_keyboard=True)

# ====================== ХЕНДЛЕРЫ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 <b>Steam Tracker Bot</b>\n\nОтслеживаю статусы Steam пользователей.",
        reply_markup=get_main_keyboard(), parse_mode="HTML"
    )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if context.user_data.get("awaiting_steam"):
        await add_user(update, context)
        return

    if text == "📋 Мои отслеживания":
        await list_tracking(update, context)
    elif text == "➕ Добавить":
        await update.message.reply_text("Отправь SteamID, ник или ссылку на профиль:", reply_markup=ForceReply())
        context.user_data["awaiting_steam"] = True
    elif text == "📊 Отчёты":
        await show_reports_menu(update, context)

# ====================== ДОБАВЛЕНИЕ ======================
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    context.user_data["awaiting_steam"] = False

    steam_id = await resolve_steam_id(text)
    if not steam_id:
        await update.message.reply_text("❌ Не удалось найти профиль.")
        return

    summary = await get_steam_summary(steam_id)
    if not summary:
        await update.message.reply_text("❌ Не удалось получить данные Steam.")
        return

    name = summary.get("personaname", "Unknown")

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO tracked_users (chat_id, steam_id, name) VALUES (?, ?, ?)", (chat_id, steam_id, name))
    conn.commit()
    conn.close()

    if chat_id not in user_tracking:
        user_tracking[chat_id] = {}
    user_tracking[chat_id][steam_id] = {
        "name": name,
        "last_status": summary.get("personastate", 0),
        "status_start_time": datetime.now()
    }

    await update.message.reply_text(f"✅ Добавлен: {name}", reply_markup=get_main_keyboard())

    asyncio.create_task(check_user_status(chat_id, steam_id, context.application.bot))

# ====================== СПИСОК ======================
async def list_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    users = get_tracked_users(chat_id)

    if not users:
        await update.message.reply_text("Нет отслеживаемых пользователей.", reply_markup=get_main_keyboard())
        return

    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{steam_id}")] for steam_id, name in users]
    await update.message.reply_text("📋 <b>Мои отслеживания</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# ====================== ДЕТАЛИ ПОЛЬЗОВАТЕЛЯ ======================
async def user_detail(query, context, steam_id):
    chat_id = query.message.chat_id
    users = get_tracked_users(chat_id)
    name = next((n for sid, n in users if sid == steam_id), "Unknown")

    keyboard = [
        [InlineKeyboardButton("📊 Текущий отчёт (24ч)", callback_data=f"report_current_{steam_id}")],
        [InlineKeyboardButton("📈 Отчёт за 7 дней", callback_data=f"report_7_{steam_id}")],
        [InlineKeyboardButton("⏸ Поставить на паузу", callback_data=f"pause_{steam_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{steam_id}")],
        [InlineKeyboardButton("← Назад к списку", callback_data="back_to_list")]
    ]

    await query.edit_message_text(
        f"👤 <b>{name}</b>\nSteamID: <code>{steam_id}</code>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )

# ====================== ОТЧЁТЫ ======================
async def show_reports_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Отчёты пока в разработке.\nИспользуй кнопки в профиле пользователя.")

async def generate_current_report(query, context, steam_id):
    chat_id = query.message.chat_id
    rows = get_user_history(chat_id, steam_id, hours=24)
    name = "Пользователь"

    if not rows:
        await query.edit_message_text("Нет данных за последние 24 часа.")
        return

    text = f"📊 Текущий отчёт (24ч) — {name}\n\n"
    for status, start_time, duration_sec in rows:
        start = datetime.fromisoformat(start_time)
        end = start + timedelta(seconds=duration_sec)
        duration_str = f"{duration_sec//60} мин" if duration_sec < 3600 else f"{duration_sec//3600} ч {duration_sec%3600//60} мин"
        text += f"{start.strftime('%H:%M')} — {end.strftime('%H:%M')} ({duration_str}) — {get_status_name(status)}\n"

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data=f"user_{steam_id}")]]))

# ====================== КНОПКИ ======================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("user_"):
        await user_detail(query, context, data[5:])
    elif data.startswith("report_current_"):
        await generate_current_report(query, context, data[14:])
    elif data == "back_to_list":
        await list_tracking_from_callback(query, context)
    # Можно расширять дальше (пауза, удаление и т.д.)

async def list_tracking_from_callback(query, context):
    chat_id = query.message.chat_id
    users = get_tracked_users(chat_id)
    if not users:
        await query.edit_message_text("Нет отслеживаемых пользователей.")
        return
    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{steam_id}")] for steam_id, name in users]
    await query.edit_message_text("📋 <b>Мои отслеживания</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# ====================== ФОНОВАЯ ПРОВЕРКА С ЗАЩИТОЙ ======================
async def check_user_status(chat_id: int, steam_id: str, bot):
    while True:
        try:
            if chat_id not in user_tracking or steam_id not in user_tracking[chat_id]:
                break

            summary = await get_steam_summary(steam_id)
            if not summary:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            current_status = summary.get("personastate", 0)
            data = user_tracking[chat_id][steam_id]
            last_status = data.get("last_status")

            if current_status != last_status:
                duration = int((datetime.now() - data.get("status_start_time", datetime.now())).total_seconds())
                hours, rem = divmod(duration, 3600)
                minutes, _ = divmod(rem, 60)
                duration_str = f"{int(hours)} ч {int(minutes)} мин" if hours > 0 else f"{int(minutes)} мин"

                await bot.send_message(
                    chat_id=chat_id,
                    text=f"🔄 Изменение статуса **{data['name']}**:\n"
                         f"Был: {get_status_name(last_status)}\n"
                         f"В статусе: {duration_str}\n"
                         f"Стал: {get_status_name(current_status)}",
                    parse_mode="Markdown"
                )

                # Сохраняем в историю
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("INSERT INTO status_history (chat_id, steam_id, status, start_time, duration_sec) VALUES (?, ?, ?, ?, ?)",
                          (chat_id, steam_id, last_status, data["status_start_time"].isoformat(), duration))
                conn.commit()
                conn.close()

                data["last_status"] = current_status
                data["status_start_time"] = datetime.now()

            await asyncio.sleep(CHECK_INTERVAL)

        except asyncio.TimeoutError:
            logging.warning(f"Timeout при проверке {steam_id}")
            await asyncio.sleep(15)
        except Exception as e:
            logging.error(f"Ошибка проверки {steam_id}: {e}")
            await asyncio.sleep(30)

# ====================== ЗАПУСК ======================
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).request(request).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(CallbackQueryHandler(button_handler))

    print("🚀 Steam Tracker Bot запущен успешно!")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
