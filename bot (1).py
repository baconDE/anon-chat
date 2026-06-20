import os
import logging
import sqlite3
import asyncio
from datetime import datetime, timedelta
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
    premium_until = datetime.fromisoformat(user[6])
    return datetime.now() < premium_until

def get_daily_chats(user_id):
    user = get_user(user_id)
    if not user:
        return 0
    last_date = user[8]
    today = datetime.now().strftime('%Y-%m-%d')
    if last_date != today:
        conn = sqlite3.connect('chatbot.db')
        c = conn.cursor()
        c.execute("UPDATE users SET chats_today = 0, last_chat_date = ? WHERE user_id = ?", (today, user_id))
        conn.commit()
        conn.close()
        return 0
    return user[7]

def increment_chats(user_id):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    c.execute("UPDATE users SET chats_today = chats_today + 1, total_chats = total_chats + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def add_to_queue(user_id, gender, age, city, looking_for):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("INSERT OR REPLACE INTO queue (user_id, gender, age, city, looking_for, joined_at) VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, gender, age, city, looking_for, now))
    conn.commit()
    conn.close()

def remove_from_queue(user_id):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    c.execute("DELETE FROM queue WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def find_partner(user_id, user_gender, user_age, user_city, looking_for, is_premium_user):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()

    if is_premium_user and looking_for:
        c.execute("""SELECT user_id FROM queue
                     WHERE user_id != ? AND gender = ?
                     ORDER BY joined_at LIMIT 1""", (user_id, looking_for))
    else:
        c.execute("SELECT user_id FROM queue WHERE user_id != ? ORDER BY joined_at LIMIT 1", (user_id,))

    partner = c.fetchone()
    conn.close()
    return partner[0] if partner else None

def create_chat(user_a, user_b):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("INSERT INTO chats (user_a_id, user_b_id, started_at) VALUES (?, ?, ?)", (user_a, user_b, now))
    chat_id = c.lastrowid
    conn.commit()
    conn.close()
    return chat_id

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
    premium_until = (datetime.now() + timedelta(days=days)).isoformat()
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
        'profile_ready': "✅ Профиль настроен!\n\nТеперь вы можете начать общение.",
        'main_menu': "🔍 Найти собеседника\n👤 Профиль\n⭐ Премиум",
        'searching': "🔍 Ищем собеседника...",
        'partner_found': "✅ Собеседник найден! Можете начинать общение.\n\n❌ /next — сменить собеседника\n🛑 /stop — завершить чат",
        'partner_left': "❌ Собеседник покинул чат.",
        'chat_ended': "🛑 Чат завершён.",
        'limit_reached': "❌ Лимит чатов на сегодня исчерпан (5/5).\n\n⭐ Купите Премиум для безлимитного общения!",
        'premium_info': "⭐ Премиум — 500 ₸/мес\n\n✅ Безлимитные чаты\n✅ Фильтр по полу\n✅ Приоритет в очереди\n✅ Отправка фото и голоса",
        'premium_active': "✅ Премиум активен до: {}",
        'premium_buy': "💳 Оплатить через Stars",
        'premium_manual': "💬 Написать в поддержку (500 ₸/мес)",
        'payment_success': "🎉 Премиум активирован! Приятного общения!",
        'profile': "👤 Ваш профиль:\nПол: {}\nВозраст: {}\nГород: {}\nЧатов сегодня: {}/{}\nВсего чатов: {}",
        'no_partner': "❌ У вас нет активного чата.",
        'language_changed': "✅ Язык изменён на русский.",
        'choose_language': "🌐 Выберите язык / Тілді таңдаңыз:",
        'support_info': "💬 Для покупки Премиума напишите: {}\n\nСтоимость: 500 ₸/мес\n\n✅ Безлимитные чаты\n✅ Фильтр по полу\n✅ Приоритет в очереди\n✅ Отправка фото и голоса",
    },
    'kz': {
        'welcome': "👋 Анонимді Чатқа қош келдіңіз!\n\nМұнда сіз кездейсоқ адамдармен анонимді түрде сөйлесе аласыз.",
        'setup_profile': "Профильді баптайық. Жынысты таңдаңыз:",
        'choose_age': "Жасыңызды таңдаңыз:",
        'choose_city': "Қалаңызды енгізіңіз:",
        'profile_ready': "✅ Профиль бапталды!\n\nЕнді сөйлесуді бастай аласыз.",
        'main_menu': "🔍 Сөйлескенді табу\n👤 Профиль\n⭐ Премиум",
        'searching': "🔍 Сөйлескенді іздеу...",
        'partner_found': "✅ Сөйлескен табылды! Сөйлесуді бастай аласыз.\n\n❌ /next — ауыстыру\n🛑 /stop — аяқтау",
        'partner_left': "❌ Сөйлескен чаттан шықты.",
        'chat_ended': "🛑 Чат аяқталды.",
        'limit_reached': "❌ Бүгінгі чат лимиті таусылды (5/5).\n\n⭐ Шексіз сөйлесу үшін Премиум сатып алыңыз!",
        'premium_info': "⭐ Премиум — 500 ₸/ай\n\n✅ Шексіз чаттар\n✅ Жыныс бойынша сүзгі\n✅ Кезекте басымдық\n✅ Фото және дауыс жіберу",
        'premium_active': "✅ Премиум {} дейін белсенді",
        'premium_buy': "💳 Stars арқылы төлеу",
        'premium_manual': "💬 Қолдауға жазу (500 ₸/ай)",
        'payment_success': "🎉 Премиум белсендірілді! Сәтті сөйлесу!",
        'profile': "👤 Сіздің профильіңіз:\nЖыныс: {}\nЖас: {}\nҚала: {}\nБүгінгі чаттар: {}/{}\nБарлық чаттар: {}",
        'no_partner': "❌ Белсенді чат жоқ.",
        'language_changed': "✅ Тіл қазақшаға ауыстырылды.",
        'choose_language': "🌐 Тілді таңдаңыз / Выберите язык:",
        'support_info': "💬 Премиум сатып алу үшін жазыңыз: {}\n\nБағасы: 500 ₸/ай\n\n✅ Шексіз чаттар\n✅ Жыныс бойынша сүзгі\n✅ Кезекте басымдық\n✅ Фото және дауыс жіберу",
    }
}

def get_text(user_id, key, *args):
    user = get_user(user_id)
    lang = user[5] if user and user[5] else 'ru'
    text = TEXTS.get(lang, TEXTS['ru']).get(key, key)
    if args:
        text = text.format(*args)
    return text

# ============ КЛАВИАТУРЫ ============

def language_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇷🇺 Русский", callback_data='lang_ru'),
         InlineKeyboardButton("🇰🇿 Қазақша", callback_data='lang_kz')]
    ])

def gender_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👨 Мужской / Ер", callback_data='gender_male'),
         InlineKeyboardButton("👩 Женский / Әйел", callback_data='gender_female')]
    ])

def age_keyboard():
    buttons = []
    row = []
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Следующий / Келесі", callback_data='next'),
         InlineKeyboardButton("🛑 Завершить / Аяқтау", callback_data='stop')]
    ])

def premium_keyboard():
    buttons = [
        [InlineKeyboardButton("💳 Stars", callback_data='buy_premium')],
        [InlineKeyboardButton("💬 Поддержка / Қолдау", callback_data='support_premium')],
        [InlineKeyboardButton("🔙 Назад / Артқа", callback_data='back_menu')]
    ]
    return InlineKeyboardMarkup(buttons)

def filter_gender_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👨 Мужской / Ер", callback_data='look_male'),
         InlineKeyboardButton("👩 Женский / Әйел", callback_data='look_female')],
        [InlineKeyboardButton("🎲 Без разницы / Бәрібір", callback_data='look_any')]
    ])

# ============ ОБРАБОТЧИКИ ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username

    create_user(user_id, username)

    await update.message.reply_text(
        get_text(user_id, 'choose_language'),
        reply_markup=language_keyboard()
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data.startswith('lang_'):
        lang = data.split('_')[1]
        update_user_profile(user_id, language=lang)
        await query.edit_message_text(get_text(user_id, 'language_changed'))
        await asyncio.sleep(0.5)
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
        user = get_user(user_id)
        daily_chats = get_daily_chats(user_id)
        premium = is_premium(user_id)

        if not premium and daily_chats >= 5:
            await query.edit_message_text(get_text(user_id, 'limit_reached'), reply_markup=premium_keyboard())
            return

        remove_from_queue(user_id)
        end_chat(user_id)

        await query.edit_message_text(get_text(user_id, 'searching'))

        gender = user[2] if user else None
        age = user[3] if user else None
        city = user[4] if user else None

        if premium:
            await query.message.reply_text("Выберите, кого ищете:", reply_markup=filter_gender_keyboard())
            context.user_data['searching'] = True
        else:
            add_to_queue(user_id, gender, age, city, None)
            await try_find_partner(query.message, user_id)

    elif data.startswith('look_'):
        looking_for = data.split('_')[1]
        if looking_for == 'any':
            looking_for = None

        user = get_user(user_id)
        gender = user[2] if user else None
        age = user[3] if user else None
        city = user[4] if user else None

        add_to_queue(user_id, gender, age, city, looking_for)
        await try_find_partner(query.message, user_id)

    elif data == 'next':
        end_chat(user_id)
        remove_from_queue(user_id)
        await query.edit_message_text(get_text(user_id, 'searching'))

        user = get_user(user_id)
        gender = user[2] if user else None
        age = user[3] if user else None
        city = user[4] if user else None
        add_to_queue(user_id, gender, age, city, None)
        await try_find_partner(query.message, user_id)

    elif data == 'stop':
        partner = get_partner(user_id)
        if partner:
            end_chat(user_id)
            remove_from_queue(user_id)
            await context.bot.send_message(partner, get_text(partner, 'partner_left'), reply_markup=main_menu_keyboard())
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
            title="Премиум — 1 ай / 1 ай",
            description="Шексіз чаттар + сүзгілер / Безлимитные чаты + фильтры",
            payload=f"premium_{user_id}_{int(datetime.now().timestamp())}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice("Премиум", 500)]
        )

    elif data == 'support_premium':
        # Уведомляем админа о запросе на премиум
        if ADMIN_ID:
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"💰 Запрос на Премиум!\n\n"
                    f"👤 Пользователь: @{query.from_user.username or 'нет username'}\n"
                    f"🆔 ID: `{user_id}`\n"
                    f"📅 Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
                    f"Для активации отправьте:\n"
                    f"`/givepremium {user_id} 30`",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить админа: {e}")

        support = SUPPORT_USERNAME if SUPPORT_USERNAME else "администратору"
        await query.edit_message_text(
            get_text(user_id, 'support_info', support),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад / Артқа", callback_data='back_menu')]])
        )

    elif data == 'language':
        await query.edit_message_text(get_text(user_id, 'choose_language'), reply_markup=language_keyboard())

    elif data == 'back_menu':
        await query.edit_message_text(get_text(user_id, 'main_menu'), reply_markup=main_menu_keyboard())

async def try_find_partner(message, user_id):
    await asyncio.sleep(1)

    user = get_user(user_id)
    if not user:
        return

    gender = user[2]
    age = user[3]
    city = user[4]
    premium = is_premium(user_id)

    partner_id = find_partner(user_id, gender, age, city, None, premium)

    if partner_id:
        remove_from_queue(user_id)
        remove_from_queue(partner_id)
        create_chat(user_id, partner_id)
        increment_chats(user_id)
        increment_chats(partner_id)

        await message.reply_text(get_text(user_id, 'partner_found'), reply_markup=chat_keyboard())
        await message.bot.send_message(partner_id, get_text(partner_id, 'partner_found'), reply_markup=chat_keyboard())
    else:
        await asyncio.sleep(2)
        await try_find_partner(message, user_id)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.user_data.get('setting_city'):
        city = update.message.text
        update_user_profile(user_id, city=city)
        context.user_data['setting_city'] = False
        await update.message.reply_text(get_text(user_id, 'profile_ready'))
        await update.message.reply_text(get_text(user_id, 'main_menu'), reply_markup=main_menu_keyboard())
        return

    partner_id = get_partner(user_id)

    if partner_id:
        if update.message.text:
            await context.bot.send_message(partner_id, update.message.text)
        elif update.message.voice:
            if is_premium(user_id):
                await context.bot.send_voice(partner_id, update.message.voice.file_id)
            else:
                await update.message.reply_text("❌ Голосовые сообщения только в Премиум!")
        elif update.message.photo:
            if is_premium(user_id):
                await context.bot.send_photo(partner_id, update.message.photo[-1].file_id, caption=update.message.caption)
            else:
                await update.message.reply_text("❌ Фото только в Премиум!")
        elif update.message.sticker:
            await context.bot.send_sticker(partner_id, update.message.sticker.file_id)
    else:
        await update.message.reply_text(get_text(user_id, 'no_partner'), reply_markup=main_menu_keyboard())

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    add_payment(user_id, 500, 500, 'success')
    activate_premium(user_id, 30)

    await update.message.reply_text(get_text(user_id, 'payment_success'), reply_markup=main_menu_keyboard())

# ============ КОМАНДЫ АДМИНА ============

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
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

    stats = f"""📊 Статистика:
👥 Всего пользователей: {total_users}
💬 Активных чатов: {active_chats}
⭐ Премиум пользователей: {premium_users}
💰 Заработано: {total_earned} ₸"""

    await update.message.reply_text(stats)

async def give_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная активация премиума: /givepremium user_id дни"""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /givepremium user_id дни\nПример: /givepremium 123456789 30")
        return

    try:
        target_id = int(args[0])
        days = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ Неверные аргументы. Используйте числа.")
        return

    activate_premium(target_id, days)
    add_payment(target_id, 500, 0, 'manual')

    await update.message.reply_text(f"✅ Премиум активирован для {target_id} на {days} дней!")

    try:
        await context.bot.send_message(
            target_id,
            "🎉 Вам активирован Премиум! Приятного общения!",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {target_id}: {e}")

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    help_text = """🔧 Админ-команды:

/admin — статистика
/adminhelp — список команд
/givepremium user_id дни — выдать премиум

Примеры:
/givepremium 123456789 30
/givepremium 987654321 7"""

    await update.message.reply_text(help_text)

# ============ ЗАПУСК ============

async def main():
    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_stats))
    app.add_handler(CommandHandler("givepremium", give_premium))
    app.add_handler(CommandHandler("adminhelp", admin_help))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE | filters.PHOTO | filters.STICKER, message_handler))

    port = int(os.getenv("PORT", 8080))
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")

    if render_url:
        webhook_url = f"{render_url}/webhook"
        await app.initialize()
        await app.start()
        await app.updater.start_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
            url_path="webhook"
        )
        logger.info(f"Бот запущен на {webhook_url}")

        import signal
        stop_event = asyncio.Event()

        def shutdown():
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_event_loop().add_signal_handler(sig, shutdown)

        await stop_event.wait()
        await app.stop()
    else:
        logger.info("Запуск в polling режиме...")
        await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
