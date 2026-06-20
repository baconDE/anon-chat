import os
import logging
import sqlite3
import asyncio
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler,
    CommandHandler, CallbackQueryHandler, PreCheckoutQueryHandler, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")

telegram_app = None
active_searches = {}  # user_id -> True (для отмены поиска)

# ============ БАЗА ДАННЫХ ============

def init_db():
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        gender TEXT,
        age INTEGER,
        city TEXT,
        language TEXT DEFAULT 'ru',
        premium_until TEXT,
        chats_today INTEGER DEFAULT 0,
        last_chat_date TEXT,
        total_chats INTEGER DEFAULT 0,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS queue (
        user_id INTEGER PRIMARY KEY,
        gender TEXT,
        age INTEGER,
        city TEXT,
        looking_for TEXT,
        joined_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_a_id INTEGER,
        user_b_id INTEGER,
        started_at TEXT,
        ended_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        stars INTEGER,
        status TEXT,
        created_at TEXT
    )""")
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def create_user(user_id, username):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
              (user_id, username, now))
    conn.commit()
    conn.close()

def update_user_profile(user_id, gender=None, age=None, city=None, language=None):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    if gender:
        c.execute("UPDATE users SET gender = ? WHERE user_id = ?", (gender, user_id))
    if age:
        c.execute("UPDATE users SET age = ? WHERE user_id = ?", (age, user_id))
    if city:
        c.execute("UPDATE users SET city = ? WHERE user_id = ?", (city, user_id))
    if language:
        c.execute("UPDATE users SET language = ? WHERE user_id = ?", (language, user_id))
    conn.commit()
    conn.close()

def is_premium(user_id):
    user = get_user(user_id)
    if not user or not user[6]:
        return False
    return datetime.now() < datetime.fromisoformat(user[6])

def get_daily_chats(user_id):
    user = get_user(user_id)
    if not user:
        return 0
    today = datetime.now().strftime('%Y-%m-%d')
    if user[8] != today:
        conn = sqlite3.connect('chatbot.db')
        c = conn.cursor()
        c.execute("UPDATE users SET chats_today = 0, last_chat_date = ? WHERE user_id = ?", (today, user_id))
        conn.commit()
        conn.close()
        return 0
    return user[7] or 0

def increment_chats(user_id):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    c.execute("""UPDATE users SET chats_today = chats_today + 1,
                 total_chats = total_chats + 1, last_chat_date = ?
                 WHERE user_id = ?""", (today, user_id))
    conn.commit()
    conn.close()

def add_to_queue(user_id, gender, age, city, looking_for):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""INSERT OR REPLACE INTO queue
                 (user_id, gender, age, city, looking_for, joined_at)
                 VALUES (?, ?, ?, ?, ?, ?)""",
              (user_id, gender, age, city, looking_for, now))
    conn.commit()
    conn.close()

def remove_from_queue(user_id):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    c.execute("DELETE FROM queue WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def find_partner(user_id, looking_for, is_premium_user):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    if is_premium_user and looking_for:
        c.execute("""SELECT user_id FROM queue
                     WHERE user_id != ? AND gender = ?
                     ORDER BY joined_at LIMIT 1""", (user_id, looking_for))
    else:
        c.execute("SELECT user_id FROM queue WHERE user_id != ? ORDER BY joined_at LIMIT 1",
                  (user_id,))
    partner = c.fetchone()
    conn.close()
    return partner[0] if partner else None

def create_chat(user_a, user_b):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("INSERT INTO chats (user_a_id, user_b_id, started_at) VALUES (?, ?, ?)",
              (user_a, user_b, now))
    conn.commit()
    conn.close()

def get_partner(user_id):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    c.execute("""SELECT user_a_id, user_b_id FROM chats
                 WHERE (user_a_id = ? OR user_b_id = ?) AND ended_at IS NULL
                 ORDER BY id DESC LIMIT 1""", (user_id, user_id))
    chat = c.fetchone()
    conn.close()
    if chat:
        return chat[1] if chat[0] == user_id else chat[0]
    return None

def end_chat(user_id):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""UPDATE chats SET ended_at = ?
                 WHERE (user_a_id = ? OR user_b_id = ?) AND ended_at IS NULL""",
              (now, user_id, user_id))
    conn.commit()
    conn.close()

def add_payment(user_id, amount, stars, status):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("INSERT INTO payments (user_id, amount, stars, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, amount, stars, status, now))
    conn.commit()
    conn.close()

def activate_premium(user_id, days=30):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    user = get_user(user_id)
    # Продлеваем от текущего срока если уже есть премиум
    if user and user[6] and datetime.now() < datetime.fromisoformat(user[6]):
        base = datetime.fromisoformat(user[6])
    else:
        base = datetime.now()
    premium_until = (base + timedelta(days=days)).isoformat()
    c.execute("UPDATE users SET premium_until = ? WHERE user_id = ?", (premium_until, user_id))
    conn.commit()
    conn.close()

# ============ ТЕКСТЫ ============

TEXTS = {
    'ru': {
        'welcome': "👋 Добро пожаловать в Анонимный Чат!\n\nЗдесь вы можете общаться анонимно с случайными собеседниками.",
        'setup_profile': "Давайте настроим ваш профиль. Выберите пол:",
        'choose_age': "Выберите ваш возраст:",
        'choose_city': "Введите ваш город:",
        'profile_ready': "✅ Профиль настроен! Теперь можно начать общение.",
        'main_menu': "Выберите действие:",
        'searching': "🔍 Ищем собеседника...\n\nОтмените командой /stop",
        'partner_found': "✅ Собеседник найден! Начинайте общение.\n\n/next — сменить\n/stop — завершить",
        'partner_left': "❌ Собеседник покинул чат.",
        'chat_ended': "🛑 Чат завершён.",
        'search_cancelled': "🚫 Поиск отменён.",
        'search_timeout': "⏱ Собеседник не найден за 2 минуты. Попробуйте позже.",
        'limit_reached': "❌ Лимит чатов на сегодня (5/5).\n\n⭐ Купите Премиум для безлимитного общения!",
        'premium_info': "⭐ Премиум — 500 ₸/мес\n\n✅ Безлимитные чаты\n✅ Фильтр по полу\n✅ Приоритет в очереди\n✅ Фото и голосовые",
        'premium_active': "✅ Премиум активен до: {}",
        'payment_success': "🎉 Премиум активирован! Приятного общения!",
        'profile': "👤 Ваш профиль:\nПол: {}\nВозраст: {}\nГород: {}\nЧатов сегодня: {}/{}\nВсего чатов: {}",
        'no_partner': "❌ У вас нет активного чата.",
        'language_changed': "✅ Язык изменён на русский.",
        'choose_language': "🌐 Выберите язык / Тілді таңдаңыз:",
        'support_info': "💬 Для покупки Премиума напишите: {}\n\nСтоимость: 500 ₸/мес",
        'voice_premium': "❌ Голосовые — только для Премиум!\n⭐ /premium для покупки.",
        'photo_premium': "❌ Фото — только для Премиум!\n⭐ /premium для покупки.",
        'choose_partner': "Кого ищете?",
    },
    'kz': {
        'welcome': "👋 Анонимді Чатқа қош келдіңіз!\n\nКездейсоқ адамдармен анонимді сөйлесіңіз.",
        'setup_profile': "Профильді баптайық. Жынысты таңдаңыз:",
        'choose_age': "Жасыңызды таңдаңыз:",
        'choose_city': "Қалаңызды енгізіңіз:",
        'profile_ready': "✅ Профиль бапталды! Сөйлесуді бастай аласыз.",
        'main_menu': "Әрекетті таңдаңыз:",
        'searching': "🔍 Сөйлескенді іздеу...\n\n/stop арқылы тоқтатыңыз",
        'partner_found': "✅ Сөйлескен табылды!\n\n/next — ауыстыру\n/stop — аяқтау",
        'partner_left': "❌ Сөйлескен чаттан шықты.",
        'chat_ended': "🛑 Чат аяқталды.",
        'search_cancelled': "🚫 Іздеу тоқтатылды.",
        'search_timeout': "⏱ 2 минут ішінде сөйлескен табылмады.",
        'limit_reached': "❌ Бүгінгі лимит таусылды (5/5).\n\n⭐ Шексіз үшін Премиум сатып алыңыз!",
        'premium_info': "⭐ Премиум — 500 ₸/ай\n\n✅ Шексіз чаттар\n✅ Жыныс сүзгісі\n✅ Басымдық\n✅ Фото және дауыс",
        'premium_active': "✅ Премиум {} дейін белсенді",
        'payment_success': "🎉 Премиум белсендірілді! Сәтті сөйлесу!",
        'profile': "👤 Профильіңіз:\nЖыныс: {}\nЖас: {}\nҚала: {}\nБүгін: {}/{}\nБарлығы: {}",
        'no_partner': "❌ Белсенді чат жоқ.",
        'language_changed': "✅ Тіл қазақшаға ауыстырылды.",
        'choose_language': "🌐 Тілді таңдаңыз / Выберите язык:",
        'support_info': "💬 Премиум үшін жазыңыз: {}\n\nБағасы: 500 ₸/ай",
        'voice_premium': "❌ Дауыс — тек Премиум!\n⭐ /premium",
        'photo_premium': "❌ Фото — тек Премиум!\n⭐ /premium",
        'choose_partner': "Кімді іздейсіз?",
    }
}

def get_text(user_id, key, *args):
    user = get_user(user_id)
    lang = user[5] if user and user[5] else 'ru'
    text = TEXTS.get(lang, TEXTS['ru']).get(key, key)
    return text.format(*args) if args else text

# ============ КЛАВИАТУРЫ ============

def language_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🇷🇺 Русский", callback_data='lang_ru'),
        InlineKeyboardButton("🇰🇿 Қазақша", callback_data='lang_kz')
    ]])

def gender_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👨 Мужской / Ер", callback_data='gender_male'),
        InlineKeyboardButton("👩 Женский / Әйел", callback_data='gender_female')
    ]])

def age_keyboard():
    buttons, row = [], []
    for age in range(16, 41, 2):
        row.append(InlineKeyboardButton(str(age), callback_data=f'age_{age}'))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Найти / Табу", callback_data='search')],
        [InlineKeyboardButton("👤 Профиль", callback_data='profile'),
         InlineKeyboardButton("⭐ Премиум", callback_data='premium')],
        [InlineKeyboardButton("🌐 Язык / Тіл", callback_data='language')]
    ])

def chat_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Следующий / Келесі", callback_data='next'),
        InlineKeyboardButton("🛑 Завершить / Аяқтау", callback_data='stop')
    ]])

def premium_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатить Stars", callback_data='buy_premium')],
        [InlineKeyboardButton("💬 Поддержка / Қолдау", callback_data='support_premium')],
        [InlineKeyboardButton("🔙 Назад / Артқа", callback_data='back_menu')]
    ])

def filter_gender_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👨 Мужской / Ер", callback_data='look_male'),
         InlineKeyboardButton("👩 Женский / Әйел", callback_data='look_female')],
        [InlineKeyboardButton("🎲 Без разницы / Бәрібір", callback_data='look_any')]
    ])

# ============ ПОИСК (FIX: цикл вместо рекурсии) ============

def cancel_search(user_id):
    """Отменяет поиск пользователя."""
    active_searches.pop(user_id, None)
    remove_from_queue(user_id)

async def try_find_partner(bot, user_id, looking_for=None):
    """Ищет партнёра в цикле. Максимум 2 минуты ожидания."""
    active_searches[user_id] = True
    max_wait = 120
    waited = 0

    try:
        while active_searches.get(user_id) and waited < max_wait:
            premium = is_premium(user_id)
            partner_id = find_partner(user_id, looking_for, premium)

            if partner_id:
                # Нашли — убираем обоих из поиска
                active_searches.pop(user_id, None)
                active_searches.pop(partner_id, None)
                remove_from_queue(user_id)
                remove_from_queue(partner_id)
                create_chat(user_id, partner_id)
                increment_chats(user_id)
                increment_chats(partner_id)

                try:
                    await bot.send_message(user_id, get_text(user_id, 'partner_found'), reply_markup=chat_keyboard())
                    await bot.send_message(partner_id, get_text(partner_id, 'partner_found'), reply_markup=chat_keyboard())
                except Exception as e:
                    logger.error(f"Ошибка уведомления о партнёре: {e}")
                return

            await asyncio.sleep(3)
            waited += 3

    except asyncio.CancelledError:
        pass
    finally:
        active_searches.pop(user_id, None)

    # Таймаут — партнёр не найден
    if waited >= max_wait:
        remove_from_queue(user_id)
        try:
            await bot.send_message(user_id, get_text(user_id, 'search_timeout'), reply_markup=main_menu_keyboard())
        except Exception:
            pass

def start_search(bot, user_id, looking_for=None):
    """Запускает поиск партнёра в фоне."""
    asyncio.create_task(try_find_partner(bot, user_id, looking_for))

# ============ ОБРАБОТЧИКИ КОМАНД ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id, update.effective_user.username)
    await update.message.reply_text(get_text(user_id, 'choose_language'), reply_markup=language_keyboard())

async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    partner = get_partner(user_id)
    cancel_search(user_id)
    if partner:
        end_chat(user_id)
        try:
            await context.bot.send_message(partner, get_text(partner, 'partner_left'), reply_markup=main_menu_keyboard())
        except Exception:
            pass
    user = get_user(user_id)
    add_to_queue(user_id, user[2] if user else None, user[3] if user else None, user[4] if user else None, None)
    await update.message.reply_text(get_text(user_id, 'searching'))
    start_search(context.bot, user_id)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    partner = get_partner(user_id)
    cancel_search(user_id)
    if partner:
        end_chat(user_id)
        try:
            await context.bot.send_message(partner, get_text(partner, 'partner_left'), reply_markup=main_menu_keyboard())
        except Exception:
            pass
        await update.message.reply_text(get_text(user_id, 'chat_ended'), reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text(get_text(user_id, 'search_cancelled'), reply_markup=main_menu_keyboard())

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data.startswith('lang_'):
        lang = data.split('_')[1]
        update_user_profile(user_id, language=lang)
        await query.edit_message_text(get_text(user_id, 'language_changed'))
        await asyncio.sleep(0.3)
        await query.message.reply_text(get_text(user_id, 'welcome'))
        await query.message.reply_text(get_text(user_id, 'setup_profile'), reply_markup=gender_keyboard())

    elif data.startswith('gender_'):
        gender = data.split('_')[1]
        update_user_profile(user_id, gender=gender)
        await query.edit_message_text(get_text(user_id, 'choose_age'), reply_markup=age_keyboard())

    elif data.startswith('age_'):
        age = int(data.split('_')[1])
        update_user_profile(user_id, age=age)
        await query.edit_message_text(get_text(user_id, 'choose_city'))
        context.user_data['setting_city'] = True

    elif data == 'search':
        daily = get_daily_chats(user_id)
        premium = is_premium(user_id)
        if not premium and daily >= 5:
            await query.edit_message_text(get_text(user_id, 'limit_reached'), reply_markup=premium_keyboard())
            return
        cancel_search(user_id)
        end_chat(user_id)
        user = get_user(user_id)
        if premium:
            await query.edit_message_text(get_text(user_id, 'choose_partner'), reply_markup=filter_gender_keyboard())
        else:
            add_to_queue(user_id, user[2] if user else None, user[3] if user else None, user[4] if user else None, None)
            await query.edit_message_text(get_text(user_id, 'searching'))
            start_search(context.bot, user_id)

    elif data.startswith('look_'):
        looking_for = data.split('_')[1]
        if looking_for == 'any':
            looking_for = None
        user = get_user(user_id)
        add_to_queue(user_id, user[2] if user else None, user[3] if user else None, user[4] if user else None, looking_for)
        await query.edit_message_text(get_text(user_id, 'searching'))
        start_search(context.bot, user_id, looking_for)

    elif data == 'next':
        partner = get_partner(user_id)
        cancel_search(user_id)
        if partner:
            end_chat(user_id)
            try:
                await context.bot.send_message(partner, get_text(partner, 'partner_left'), reply_markup=main_menu_keyboard())
            except Exception:
                pass
        user = get_user(user_id)
        add_to_queue(user_id, user[2] if user else None, user[3] if user else None, user[4] if user else None, None)
        await query.edit_message_text(get_text(user_id, 'searching'))
        start_search(context.bot, user_id)

    elif data == 'stop':
        partner = get_partner(user_id)
        cancel_search(user_id)
        if partner:
            end_chat(user_id)
            try:
                await context.bot.send_message(partner, get_text(partner, 'partner_left'), reply_markup=main_menu_keyboard())
            except Exception:
                pass
        await query.edit_message_text(get_text(user_id, 'chat_ended'), reply_markup=main_menu_keyboard())

    elif data == 'profile':
        user = get_user(user_id)
        if user:
            gender = user[2] or "—"
            age = user[3] or "—"
            city = user[4] or "—"
            daily = get_daily_chats(user_id)
            limit = "∞" if is_premium(user_id) else "5"
            total = user[9] or 0
            await query.edit_message_text(
                get_text(user_id, 'profile', gender, age, city, daily, limit, total),
                reply_markup=main_menu_keyboard()
            )

    elif data == 'premium':
        if is_premium(user_id):
            user = get_user(user_id)
            until = datetime.fromisoformat(user[6]).strftime('%d.%m.%Y')
            await query.edit_message_text(get_text(user_id, 'premium_active', until), reply_markup=main_menu_keyboard())
        else:
            await query.edit_message_text(get_text(user_id, 'premium_info'), reply_markup=premium_keyboard())

    elif data == 'buy_premium':
        await query.message.reply_invoice(
            title="Премиум — 1 ай / 1 месяц",
            description="Шексіз чаттар + сүзгілер / Безлимитные чаты + фильтры",
            payload=f"premium_{user_id}_{int(datetime.now().timestamp())}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice("Премиум", 500)]
        )

    elif data == 'support_premium':
        if ADMIN_ID:
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"💰 Запрос на Премиум!\n\n"
                    f"👤 @{query.from_user.username or 'нет username'}\n"
                    f"🆔 `{user_id}`\n"
                    f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
                    f"Активировать:\n`/givepremium {user_id} 30`",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить админа: {e}")
        support = SUPPORT_USERNAME if SUPPORT_USERNAME else "администратору"
        await query.edit_message_text(
            get_text(user_id, 'support_info', support),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад / Артқа", callback_data='back_menu')
            ]])
        )

    elif data == 'language':
        await query.edit_message_text(get_text(user_id, 'choose_language'), reply_markup=language_keyboard())

    elif data == 'back_menu':
        await query.edit_message_text(get_text(user_id, 'main_menu'), reply_markup=main_menu_keyboard())

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Ввод города
    if context.user_data.get('setting_city'):
        city = update.message.text.strip()
        if len(city) > 50:
            await update.message.reply_text("❌ Слишком длинное название города.")
            return
        update_user_profile(user_id, city=city)
        context.user_data['setting_city'] = False
        await update.message.reply_text(get_text(user_id, 'profile_ready'))
        await update.message.reply_text(get_text(user_id, 'main_menu'), reply_markup=main_menu_keyboard())
        return

    partner_id = get_partner(user_id)

    if partner_id:
        try:
            if update.message.text:
                await context.bot.send_message(partner_id, update.message.text)
            elif update.message.voice:
                if is_premium(user_id):
                    await context.bot.send_voice(partner_id, update.message.voice.file_id)
                else:
                    await update.message.reply_text(get_text(user_id, 'voice_premium'))
            elif update.message.photo:
                if is_premium(user_id):
                    await context.bot.send_photo(partner_id, update.message.photo[-1].file_id,
                                                  caption=update.message.caption)
                else:
                    await update.message.reply_text(get_text(user_id, 'photo_premium'))
        except Exception as e:
            logger.error(f"Ошибка пересылки: {e}")
            end_chat(user_id)
            await update.message.reply_text(get_text(user_id, 'partner_left'), reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text(get_text(user_id, 'no_partner'), reply_markup=main_menu_keyboard())

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_payment(user_id, 500, 500, 'success')
    activate_premium(user_id, 30)
    await update.message.reply_text(get_text(user_id, 'payment_success'), reply_markup=main_menu_keyboard())

# ============ КОМАНДЫ АДМИНА ============

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM chats WHERE ended_at IS NULL")
    active_chats = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE premium_until > ?", (datetime.now().isoformat(),))
    premium_users = c.fetchone()[0]
    c.execute("SELECT SUM(amount) FROM payments WHERE status = 'success'")
    total_earned = c.fetchone()[0] or 0
    conn.close()
    await update.message.reply_text(
        f"📊 Статистика:\n"
        f"👥 Пользователей: {total_users}\n"
        f"💬 Активных чатов: {active_chats}\n"
        f"🔍 В поиске: {len(active_searches)}\n"
        f"⭐ Премиум: {premium_users}\n"
        f"💰 Заработано: {total_earned} ₸"
    )

async def give_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /givepremium user_id дни")
        return
    try:
        target_id, days = int(args[0]), int(args[1])
    except ValueError:
        await update.message.reply_text("❌ Используйте числа.")
        return
    activate_premium(target_id, days)
    add_payment(target_id, 500, 0, 'manual')
    await update.message.reply_text(f"✅ Премиум активирован для {target_id} на {days} дней!")
    try:
        await context.bot.send_message(target_id, "🎉 Вам активирован Премиум! Приятного общения!",
                                        reply_markup=main_menu_keyboard())
    except Exception:
        pass

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "🔧 Админ-команды:\n\n"
        "/admin — статистика\n"
        "/adminhelp — список команд\n"
        "/givepremium user_id дни — выдать премиум\n\n"
        "Пример: /givepremium 123456789 30"
    )

# ============ WEBHOOK + HEALTH CHECK ============

async def webhook_handler(request):
    global telegram_app
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500, text="Error")

async def health_handler(request):
    return web.Response(text="OK")

# ============ ЗАПУСК ============

async def main():
    global telegram_app
    init_db()

    telegram_app = ApplicationBuilder().token(TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("next", next_command))
    telegram_app.add_handler(CommandHandler("stop", stop_command))
    telegram_app.add_handler(CommandHandler("admin", admin_stats))
    telegram_app.add_handler(CommandHandler("givepremium", give_premium))
    telegram_app.add_handler(CommandHandler("adminhelp", admin_help))
    telegram_app.add_handler(CallbackQueryHandler(callback_handler))
    telegram_app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    telegram_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT | filters.VOICE | filters.PHOTO, message_handler))

    port = int(os.getenv("PORT", 8080))
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")

    await telegram_app.initialize()
    await telegram_app.start()

    if render_url:
        webhook_url = f"{render_url}/webhook"
        await telegram_app.bot.set_webhook(webhook_url)
        logger.info(f"Webhook: {webhook_url}")

        aio_app = web.Application()
        aio_app.router.add_post("/webhook", webhook_handler)
        aio_app.router.add_get("/", health_handler)
        aio_app.router.add_get("/health", health_handler)

        runner = web.AppRunner(aio_app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        logger.info(f"Сервер запущен на порту {port}")

        await asyncio.Event().wait()
        await runner.cleanup()
    else:
        logger.info("Polling режим...")
        await telegram_app.updater.start_polling()
        await asyncio.Event().wait()
        await telegram_app.updater.stop()

    await telegram_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
