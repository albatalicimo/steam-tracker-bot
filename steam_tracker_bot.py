import logging
import os
import sqlite3
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict

import aiohttp

from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ForceReply
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    filters, CallbackQueryHandler
)
from telegram.request import HTTPXRequest

load_dotenv()

# ====================== НАСТРОЙКИ ======================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "56716E5D4FE456305205C86778E0824E")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7643881318:AAF-vT733q8-LJEa59guE9U7fE3vpBaU2mM")
CHECK_INTERVAL = 60
DB_NAME = "steam_tracker.db"

request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)

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

# ====================== ГЛОБАЛЬНЫЕ ДАННЫЕ ======================
user_tracking = {}

# ====================== ВСПОМОГАТЕЛЬНЫЕ ======================
def get_tracked_users(chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT steam_id, name FROM tracked_users WHERE chat_id = ? AND is_active = 1", (chat_id,))
    users = c.fetchall()
    conn.close()
    return users

def get_status_name(status: int) -> str:
    m = {0:"🔴 Оффлайн",1:"🟢 Онлайн",2:"🟡 Занят",3:"🟠 Отошёл",4:"💤 Спит",5:"💰 Торговля",6:"🎮 Хочет играть"}
    return m.get(status, "❓")

def get_user_history(chat_id: int, steam_id: str, hours: int = 24):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    c.execute("""SELECT status, start_time, duration_sec 
                 FROM status_history 
                 WHERE chat_id = ? AND steam_id = ? AND start_time >= ? 
                 ORDER BY start_time""", (chat_id, steam_id, since))
    return c.fetchall()

# ====================== STEAM ======================
async def resolve_steam_id(text: str) -> str | None:
    text = text.strip()
    if text.isdigit() and len(text) == 17:
        return text
    vanity = text.split("/")[-1] if "steamcommunity.com" in text else text
    url = f"https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/?key={STEAM_API_KEY}&vanityurl={vanity}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url) as r:
                data = await r.json()
                if data.get("response", {}).get("success") == 1:
                    return data["response"]["steamid"]
    except Exception as e:
        logging.error(f"Resolve error: {e}")
    return None

async def get_steam_summary(steam_id: str):
    url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={steam_id}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url) as r:
                data = await r.json()
                players = data.get("response", {}).get("players")
                return players[0] if players else None
    except Exception as e:
        logging.error(f"Summary error: {e}")
        return None

# ====================== КЛАВИАТУРЫ ======================
def get_main_keyboard():
    return ReplyKeyboardMarkup([["📋 Мои отслеживания", "➕ Добавить"], ["📊 Отчёты", "❓ Помощь"]], resize_keyboard=True)

# ====================== ХЕНДЛЕРЫ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 <b>Steam Tracker Bot</b>\n\nОтслеживаю статусы Steam и строю аналитику.\nВыбери действие ниже:",
        reply_markup=get_main_keyboard(), parse_mode="HTML"
    )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if context.user_data.get("awaiting_steam"):
        await add_user(update, context)
        return
    if context.user_data.get("awaiting_pause_hours"):
        await set_custom_pause(update, context)
        return

    if text == "📋 Мои отслеживания":
        await list_tracking(update, context)
    elif text == "➕ Добавить":
        await update.message.reply_text("Отправь SteamID (17 цифр), ник или ссылку на профиль:", reply_markup=get_main_keyboard())
        context.user_data["awaiting_steam"] = True
    elif text == "📊 Отчёты":
        await show_reports_menu(update, context)
    elif text == "❓ Помощь":
        await update.message.reply_text("Бот отслеживает друзей в Steam и строит графики активности.", reply_markup=get_main_keyboard())

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_steam"] = False
    input_text = update.message.text.strip()
    chat_id = update.effective_chat.id

    steam_id = await resolve_steam_id(input_text)
    if not steam_id:
        await update.message.reply_text("❌ Не удалось распознать SteamID.")
        return

    user_info = await get_steam_summary(steam_id)
    if not user_info:
        await update.message.reply_text("⚠️ Не удалось получить данные из Steam.")
        return

    name = user_info.get("personaname", "Неизвестный")
    current_status = user_info.get('personastate', 0)

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO tracked_users (chat_id, steam_id, name) VALUES (?, ?, ?)", (chat_id, steam_id, name))
    conn.commit()
    conn.close()

    if chat_id not in user_tracking:
        user_tracking[chat_id] = {}
    user_tracking[chat_id][steam_id] = {
        'name': name,
        'last_status': current_status,
        'status_start_time': datetime.now(),
        'paused_until': None
    }

    await update.message.reply_text(
        f"✅ Успешно добавлено!\n\n👤 {name}\n🆔 {steam_id}\nТекущий статус: {get_status_name(current_status)}",
        reply_markup=get_main_keyboard()
    )

    asyncio.create_task(check_user_status(chat_id, steam_id, context.application.bot))

async def list_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    users = get_tracked_users(chat_id)
    if not users:
        await update.message.reply_text("📋 Пока нет отслеживаемых пользователей.", reply_markup=get_main_keyboard())
        return

    keyboard = [[InlineKeyboardButton(f"👤 {name}", callback_data=f"user_{steam_id}")] for steam_id, name in users]
    keyboard.append([InlineKeyboardButton("🔙 Главное меню", callback_data="back_main")])
    await update.message.reply_text("📋 Отслеживаемые пользователи:\nВыберите пользователя:", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_list_from_callback(query):
    chat_id = query.message.chat_id
    users = get_tracked_users(chat_id)
    if not users:
        await query.edit_message_text("📋 Пока нет отслеживаемых пользователей.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_main")]]))
        return
    keyboard = [[InlineKeyboardButton(f"👤 {name}", callback_data=f"user_{steam_id}")] for steam_id, name in users]
    keyboard.append([InlineKeyboardButton("🔙 Главное меню", callback_data="back_main")])
    await query.edit_message_text("📋 Отслеживаемые пользователи:\nВыберите пользователя:", reply_markup=InlineKeyboardMarkup(keyboard))

async def user_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    steam_id = query.data.split("_")[1]
    chat_id = query.message.chat_id
    users = dict(get_tracked_users(chat_id))
    if steam_id not in users:
        await query.edit_message_text("Пользователь не найден.")
        return

    name = users[steam_id]
    data = user_tracking.get(chat_id, {}).get(steam_id, {})

    if data:
        current_status = get_status_name(data['last_status'])
        paused_until = data.get('paused_until')
        if paused_until and paused_until > datetime.now():
            remaining = paused_until - datetime.now()
            hours, rem = divmod(remaining.total_seconds(), 3600)
            minutes, _ = divmod(rem, 60)
            pause_info = f"\n⏸️ На паузе — осталось ~{int(hours)}ч {int(minutes)}м"
            pause_btn = InlineKeyboardButton("▶️ Снять паузу", callback_data=f"unpause_{steam_id}")
        else:
            pause_info = ""
            pause_btn = InlineKeyboardButton("⏸ Поставить на паузу", callback_data=f"pause_{steam_id}")

        time_in_status = datetime.now() - data['status_start_time']
        hours, rem = divmod(time_in_status.total_seconds(), 3600)
        minutes, _ = divmod(rem, 60)
        time_str = f"{int(hours)}ч {int(minutes)}м" if hours > 0 else f"{int(minutes)}м"
    else:
        current_status = "❓"
        time_str = "—"
        pause_info = ""
        pause_btn = InlineKeyboardButton("⏸ Поставить на паузу", callback_data=f"pause_{steam_id}")

    text = f"👤 **{name}**\n🆔 `{steam_id}`\n\n📊 Текущий статус: **{current_status}**\n⏱ В этом статусе: **{time_str}**{pause_info}"

    keyboard = [
        [InlineKeyboardButton("📊 Текущий отчёт (24ч)", callback_data=f"report_current_{steam_id}")],
        [InlineKeyboardButton("📈 Отчёт за 7 дней", callback_data=f"report_7_{steam_id}")],
        [pause_btn],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"remove_{steam_id}")],
        [InlineKeyboardButton("🔙 Назад к списку", callback_data="back_to_list")]
    ]

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_reports_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Отчёт за сегодня", callback_data="report_global_1")],
        [InlineKeyboardButton("📈 Отчёт за 7 дней", callback_data="report_global_7")],
        [InlineKeyboardButton("⚖️ Сравнить пользователей", callback_data="compare_global")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="back_main")]
    ]
    await update.message.reply_text("📊 Выберите тип отчёта:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("user_"):
        await user_detail(update, context)
    elif data.startswith("report_current_"):
        await generate_current_report(query, context, data.split("_")[2])
    elif data.startswith("report_7_"):
        await generate_period_report_for_user(query, context, data.split("_")[2], 7)
    elif data.startswith("pause_"):
        await show_pause_menu(query, data.split("_")[1])
    elif data.startswith("pause_time_"):
        parts = data.split("_")
        steam_id = parts[2]
        hours = int(parts[3])
        await set_pause(query, steam_id, hours)
    elif data.startswith("custom_pause_"):
        steam_id = data.split("_")[2]
        context.user_data["awaiting_pause_hours"] = True
        context.user_data["current_pause_steam_id"] = steam_id
        await query.edit_message_text("Введите время паузы (например: 2h 30m или 90m):", reply_markup=ForceReply())
    elif data.startswith("unpause_"):
        await unpause_user(query, data.split("_")[1])
    elif data.startswith("remove_"):
        await remove_user(query, context, data.split("_")[1])
    elif data == "back_to_list":
        await show_list_from_callback(query)
    elif data == "back_main":
        await start_from_callback(query)
    elif data.startswith("report_global_"):
        if data == "report_global_1":
            await query.edit_message_text("📊 Отчёт за сегодня\n\nФункция в разработке.")
        elif data == "report_global_7":
            await query.edit_message_text("📈 Отчёт за 7 дней\n\nФункция в разработке.")
        elif data == "compare_global":
            await generate_global_comparison(query, context)

async def start_from_callback(query):
    await query.message.reply_text(
        "🚀 <b>Steam Tracker Bot</b>\n\nОтслеживаю статусы Steam и строю аналитику.\nВыбери действие ниже:",
        reply_markup=get_main_keyboard(), parse_mode="HTML"
    )
    try:
        await query.message.delete()
    except:
        pass

async def show_pause_menu(query, steam_id):
    keyboard = [
        [InlineKeyboardButton("1 час", callback_data=f"pause_time_{steam_id}_1")],
        [InlineKeyboardButton("6 часов", callback_data=f"pause_time_{steam_id}_6")],
        [InlineKeyboardButton("12 часов", callback_data=f"pause_time_{steam_id}_12")],
        [InlineKeyboardButton("24 часа", callback_data=f"pause_time_{steam_id}_24")],
        [InlineKeyboardButton("Ввести своё время", callback_data=f"custom_pause_{steam_id}")],
        [InlineKeyboardButton("🔙 Отмена", callback_data=f"user_{steam_id}")]
    ]
    await query.edit_message_text("⏸ Выберите время паузы:", reply_markup=InlineKeyboardMarkup(keyboard))

async def set_pause(query, steam_id, hours):
    chat_id = query.message.chat_id
    paused_until = datetime.now() + timedelta(hours=hours)
    if chat_id in user_tracking and steam_id in user_tracking[chat_id]:
        user_tracking[chat_id][steam_id]['paused_until'] = paused_until
    await query.edit_message_text(f"✅ Пауза установлена на {hours} часов.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"user_{steam_id}")]]))

async def set_custom_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip().lower().replace(" ", "")

    try:
        if "h" in text or "ч" in text:
            parts = text.replace("ч", "h").split("h")
            hours = int(parts[0])
            minutes = int(parts[1].replace("m", "").replace("мин", "")) if len(parts) > 1 and parts[1] else 0
            total_hours = hours + minutes / 60
        elif "m" in text or "мин" in text:
            total_hours = int(''.join(filter(str.isdigit, text))) / 60
        else:
            total_hours = int(text)
    except:
        await update.message.reply_text("❌ Неправильный формат.\nПримеры:\n• 2h 30m\n• 90m\n• 3")
        return

    context.user_data["awaiting_pause_hours"] = False
    steam_id = context.user_data.pop("current_pause_steam_id", None)
    if not steam_id:
        await update.message.reply_text("Ошибка. Попробуйте снова.")
        return

    paused_until = datetime.now() + timedelta(hours=total_hours)
    if chat_id in user_tracking and steam_id in user_tracking[chat_id]:
        user_tracking[chat_id][steam_id]['paused_until'] = paused_until

    await update.message.reply_text(f"✅ Пауза установлена на {total_hours:.1f} часов.")
    await user_detail_from_id(chat_id, steam_id, update, context)

async def unpause_user(query, steam_id):
    chat_id = query.message.chat_id
    if chat_id in user_tracking and steam_id in user_tracking[chat_id]:
        user_tracking[chat_id][steam_id]['paused_until'] = None
    await query.edit_message_text("▶️ Пауза снята досрочно.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"user_{steam_id}")]]))

async def remove_user(query, context, steam_id):
    chat_id = query.message.chat_id
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM tracked_users WHERE chat_id = ? AND steam_id = ?", (chat_id, steam_id))
    conn.commit()
    conn.close()
    if chat_id in user_tracking and steam_id in user_tracking[chat_id]:
        del user_tracking[chat_id][steam_id]
    await query.edit_message_text("⏹ Пользователь удалён.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад к списку", callback_data="back_to_list")]]))

async def user_detail_from_id(chat_id, steam_id, update, context):
    pass

# ====================== ФОНОВОЕ ОТСЛЕЖИВАНИЕ ======================
async def check_user_status(chat_id: int, steam_id: str, bot):
    while True:
        try:
            if chat_id not in user_tracking or steam_id not in user_tracking[chat_id]:
                break
            data = user_tracking[chat_id][steam_id]
            if data.get('paused_until') and data['paused_until'] > datetime.now():
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            user_info = await get_steam_summary(steam_id)
            if not user_info:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            current_status = user_info.get('personastate', 0)
            last_status = data.get('last_status')
            name = data.get('name')

            if current_status != last_status:
                duration_sec = int((datetime.now() - data['status_start_time']).total_seconds())
                hours, rem = divmod(duration_sec, 3600)
                minutes, _ = divmod(rem, 60)
                duration_str = f"{int(hours)}ч {int(minutes)}м" if hours > 0 else f"{int(minutes)}м"

                await bot.send_message(
                    chat_id=chat_id,
                    text=f"🔄 Изменение статуса **{name}**:\n"
                         f"Был: {get_status_name(last_status)}\n"
                         f"В статусе: {duration_str}\n"
                         f"Стал: {get_status_name(current_status)}",
                    parse_mode="Markdown"
                )

                # Сохраняем в историю
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("""INSERT INTO status_history 
                             (chat_id, steam_id, status, start_time, duration_sec)
                             VALUES (?, ?, ?, ?, ?)""",
                          (chat_id, steam_id, last_status, data['status_start_time'].isoformat(), duration_sec))
                conn.commit()
                conn.close()

                data['last_status'] = current_status
                data['status_start_time'] = datetime.now()

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            logging.error(f"Check error {steam_id}: {e}")
            await asyncio.sleep(10)

# ====================== ОТЧЁТЫ ======================
async def generate_current_report(query, context, steam_id):
    chat_id = query.message.chat_id
    users = dict(get_tracked_users(chat_id))
    if steam_id not in users:
        await query.edit_message_text("Пользователь не найден.")
        return

    name = users[steam_id]
    rows = get_user_history(chat_id, steam_id, hours=24)
    if not rows:
        text = f"📊 Текущий отчёт (24ч) — **{name}**\n\nПока нет записей."
    else:
        text = f"📊 История за последние 24 часа — **{name}**\n\n"
        for status, start_str, duration_sec in rows:
            start = datetime.fromisoformat(start_str)
            end = start + timedelta(seconds=duration_sec)
            h, m = divmod(duration_sec, 3600)
            m = m // 60
            duration_str = f"{int(h)}ч {int(m)}м" if h > 0 else f"{int(m)}м"
            text += f"{start.strftime('%H:%M')} — {end.strftime('%H:%M')} ({duration_str}) {get_status_name(status)}\n"

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"user_{steam_id}")]]))

async def generate_period_report_for_user(query, context, steam_id, days: int):
    chat_id = query.message.chat_id
    users = dict(get_tracked_users(chat_id))
    if steam_id not in users:
        await query.edit_message_text("Пользователь не найден.")
        return
    name = users[steam_id]
    text = f"📊 Отчёт за {days} дней — <b>{name}</b>\n\nПока нет сохранённой истории статусов."
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"user_{steam_id}")]]))

async def generate_global_comparison(query, context):
    chat_id = query.message.chat_id
    users = get_tracked_users(chat_id)
    if len(users) < 2:
        await query.edit_message_text("Для сравнения нужно минимум 2 пользователя.")
        return

    text = "⚖️ Сравнение пользователей:\n\n"
    for steam_id, name in users[:6]:
        data = user_tracking.get(chat_id, {}).get(steam_id, {})
        if data:
            status = get_status_name(data['last_status'])
            time_in_status = datetime.now() - data['status_start_time']
            hours, rem = divmod(time_in_status.total_seconds(), 3600)
            minutes, _ = divmod(rem, 60)
            time_str = f"{int(hours)}ч {int(minutes)}м" if hours > 0 else f"{int(minutes)}м"
            text += f"👤 **{name}** — {status} ({time_str})\n"
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_main")]]))

# ====================== ЗАПУСК ======================
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).request(request).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    print("🚀 Steam Tracker Bot запущен успешно!")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()