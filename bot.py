import os
import logging
import sqlite3
from uuid import uuid4
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    PreCheckoutQueryHandler,
    CallbackQueryHandler,
    ConversationHandler
)

# Загрузка конфигурации
load_dotenv()

# Настройки
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
DB = os.getenv("DB_PATH", "store.db")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PURCHASE_COOLDOWN_SECONDS = int(os.getenv("PURCHASE_COOLDOWN_SECONDS", "5"))

if not BOT_TOKEN:
    raise SystemExit("Set BOT_TOKEN env var")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# States for conversation handler
TITLE, DESC, PRICE = range(3)

# In-memory rate-limit
_last_purchase = {}

### База данных
def _conn():
    return sqlite3.connect(DB)

def init_db():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS offers(
        id TEXT PRIMARY KEY, title TEXT, description TEXT, price INTEGER)
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        offer_id TEXT,
        status TEXT,
        payload TEXT,
        is_demo INTEGER DEFAULT 0,
        paid_amount INTEGER DEFAULT 0,
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS demo_exceptions(
        user_id INTEGER PRIMARY KEY,
        granted_by INTEGER,
        granted_at TEXT
    )
    """)
    conn.commit()
    conn.close()

def add_sample_offers():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM offers")
    if cur.fetchone()[0] == 0:
        offers = [
            (str(uuid4()), "Экспресс 3 матча", "Три футбольных исхода", 70000),
            (str(uuid4()), "Экспресс 5 матчей", "Пять тщательно подобранных исходов", 120000)
        ]
        cur.executemany("INSERT INTO offers VALUES(?,?,?,?)", offers)
        conn.commit()
    conn.close()

### Админ-функции
def is_admin(user_id):
    return user_id in ADMIN_IDS

### Утилиты
def rate_limit_ok(user_id):
    now = datetime.utcnow()
    last = _last_purchase.get(user_id)
    if last and (now - last) < timedelta(seconds=PURCHASE_COOLDOWN_SECONDS):
        return False
    _last_purchase[user_id] = now
    return True

### Основное меню
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if is_admin(user.id):
        keyboard = [
            [InlineKeyboardButton("🎯 Доступные офферы", callback_data='show_offers')],
            [InlineKeyboardButton("📋 Мои заказы", callback_data='my_orders')],
            [InlineKeyboardButton("⚙️ Админ-панель", callback_data='admin_menu')],
            [InlineKeyboardButton("❓ Помощь", callback_data='help')]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("🎯 Доступные офферы", callback_data='show_offers')],
            [InlineKeyboardButton("📋 Мои заказы", callback_data='my_orders')],
            [InlineKeyboardButton("❓ Помощь", callback_data='help')]
        ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"Привет, {user.first_name}!\nДобро пожаловать в наш бот!\n\nВыберите интересующий вас раздел:"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

### Административное меню
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.message.reply_text("❌ Доступ запрещён")
        return
    
    keyboard = [
        [InlineKeyboardButton("📋 Управление офферами", callback_data='manage_offers')],
        [InlineKeyboardButton("📊 Статистика", callback_data='stats')],
        [InlineKeyboardButton("🔍 Управление демо", callback_data='manage_demo')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "⚙️ Административное меню:",
        reply_markup=reply_markup
    )

### Работа с офферами
# Показываем пользователям только название и цену, описание скрыто до покупки
async def show_offers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT id, title, price FROM offers")
    offers = cur.fetchall()
    conn.close()
    
    if not offers:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')]]
        await query.edit_message_text(
            "📭 Офферов пока нет",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    keyboard = []
    for offer_id, title, price in offers:
        keyboard.append([
            InlineKeyboardButton(
                f"{title} ({price/100:.0f} b)",
                callback_data=f'buy_{offer_id}'
            )
        ])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🎯 Доступные офферы:",
        reply_markup=reply_markup
    )

### Исправленный хендлер управления демо
async def manage_demo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.message.reply_text("❌ Доступ запрещён")
        return

    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, granted_by, granted_at FROM demo_exceptions")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        text = "📭 Нет выданных демо-доступов"
    else:
        text = "📋 Список выданных демо-доступов:\n\n"
        for user_id, granted_by, granted_at in rows:
            granted_at_fmt = granted_at[:19].replace('T', ' ') if granted_at else 'неизвестно'
            text += f"Пользователь ID {user_id}  выдан: {granted_at_fmt} (админ ID {granted_by})\n"

    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='admin_menu')]]

    # Вместо редактирования сообщения, присылаем reply
    await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

### Регистрация хендлеров

def setup_handlers(application: Application):
    # Основные команды
    application.add_handler(CommandHandler("start", start))

    # Conversation для добавления оффера (админ)
    conv_add = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_offer, pattern='^add_offer$')],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
        },
        fallbacks=[CommandHandler('cancel', cancel_add_offer), CommandHandler('back', back_add_offer)],
        allow_reentry=True
    )
    application.add_handler(conv_add)

    # Callback handlers
    application.add_handler(CallbackQueryHandler(show_offers, pattern='^show_offers$'))
    application.add_handler(CallbackQueryHandler(buy_offer, pattern='^buy_'))
    application.add_handler(CallbackQueryHandler(my_orders, pattern='^my_orders$'))
    application.add_handler(CallbackQueryHandler(help_command, pattern='^help$'))
    application.add_handler(CallbackQueryHandler(back_to_main, pattern='^back_to_main$'))

    # Админские хендлеры
    application.add_handler(CallbackQueryHandler(admin_menu, pattern='^admin_menu$'))
    application.add_handler(CallbackQueryHandler(manage_offers, pattern='^manage_offers$'))
    application.add_handler(CallbackQueryHandler(stats, pattern='^stats$'))

    # Добавляем исправленный хендлер управления демо
    application.add_handler(CallbackQueryHandler(manage_demo, pattern='^manage_demo$'))

    # Офферы: список/удаление/редактирование-заглушка
    application.add_handler(CallbackQueryHandler(list_offers_admin, pattern='^list_offers$'))
    application.add_handler(CallbackQueryHandler(delete_offer, pattern='^delete_offer_'))
    application.add_handler(CallbackQueryHandler(edit_offer_placeholder, pattern='^edit_offer_'))

    # Платежные хендлеры
    application.add_handler(PreCheckoutQueryHandler(checkout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

### Остальные функции и хендлеры остаются без изменений

### Инициализация бота
def main():
    # Инициализация базы данных
    init_db()
    add_sample_offers()
    
    # Инициализация приложения
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Регистрация хендлеров
    setup_handlers(application)
    
    logger.info("Bot started...")
    
    # Запуск бота
    application.run_polling(poll_interval=1.0)

if __name__ == '__main__':
    main()
