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
                f"{title} ({price/100:.0f} 2)",
                callback_data=f'buy_{offer_id}'
            )
        ])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🎯 Доступные офферы:",
        reply_markup=reply_markup
    )

### Обработка покупки
async def buy_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    _, offer_id = query.data.split('_', 1)
    
    if not rate_limit_ok(query.from_user.id):
        await query.message.reply_text("⏰ Слишком частые запросы. Попробуйте позже.")
        return
    
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT title, description, price FROM offers WHERE id=?", (offer_id,))
    offer = cur.fetchone()
    conn.close()
    
    if not offer:
        await query.edit_message_text("❌ Оффер не найден")
        return
    
    title, description, price = offer
    
    # Проверка демо-доступа
    is_demo = False
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM demo_exceptions WHERE user_id=?", (query.from_user.id,))
    if cur.fetchone()[0] > 0:
        is_demo = True
    conn.close()
    
    payload = str(uuid4())
    
    if is_demo:
        # Демо-доступ - сразу предоставляем товар и описание
        order_id = str(uuid4())
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO orders (id, user_id, offer_id, status, payload, is_demo, created_at)
            VALUES (?, ?, ?, 'paid', ?, 1, ?)
        """, (order_id, query.from_user.id, offer_id, payload, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
        
        await query.edit_message_text(
            f"🎉 Демо-доступ предоставлен!\n"
            f"📦 Товар: {title}\n"
            f"📝 Описание: {description}\n"
            f"✅ Статус: Активен"
        )
    else:
        # Создание счета для оплаты. НЕ передаём полное описание в счёт — пользователь увидит его после оплаты.
        try:
            await context.bot.send_invoice(
                chat_id=query.from_user.id,
                title=title,
                description="Оплата товара",
                payload=payload,
                provider_token=PROVIDER_TOKEN,
                currency='RUB',
                prices=[LabeledPrice(title, price)],
                max_tip_amount=50000,
                suggested_tip_amounts=[5000, 10000, 20000, 50000]
            )
            
            # Сохраняем информацию о заказе
            context.user_data['offer_id'] = offer_id
            context.user_data['payload'] = payload
            
        except Exception as e:
            logger.error(f"Error sending invoice: {e}")
            await query.edit_message_text("❌ Ошибка при создании счета. Попробуйте позже.")

### Обработка пречека
async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    
    # Можно добавить дополнительные проверки
    await query.answer(ok=True)

### Обработка успешной оплаты
async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    
    # Логирование успешной оплаты
    logger.info(f"Успешная оплата: {payment.total_amount} {payment.currency} "
                f"от пользователя {update.effective_user.id}")
    
    # Создание записи о заказе
    order_id = str(uuid4())
    offer_id = context.user_data.get('offer_id')
    
    description = ''
    if offer_id:
        conn = _conn()
        cur = conn.cursor()
        # Получим описание оффера, чтобы показать его клиенту только после оплаты
        cur.execute("SELECT description FROM offers WHERE id = ?", (offer_id,))
        row = cur.fetchone()
        if row:
            description = row[0] or ''
        cur.execute("""
            INSERT INTO orders (id, user_id, offer_id, status, payload, paid_amount, created_at)
            VALUES (?, ?, ?, 'paid', ?, ?, ?)
        """, (order_id, update.effective_user.id, offer_id, payment.telegram_payment_charge_id, 
              payment.total_amount, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
    
    # Отправка подтверждения покупки и описания
    msg = (
        f"🎉 Покупка успешно завершена!\n"
        f"💰 Сумма: {payment.total_amount / 100:.0f} 2\n"
        f"🆔 ID транзакции: {payment.telegram_payment_charge_id}\n\n"
    )
    if description:
        msg += f"📝 Описание товара:\n{description}\n\n"
    msg += "✅ Доступ к товару активирован!"

    await update.message.reply_text(msg)

### Мои заказы (история покупок у клиента)
async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.id, of.title, o.status, o.created_at, o.paid_amount, o.is_demo, o.payload
        FROM orders o
        JOIN offers of ON o.offer_id = of.id
        WHERE o.user_id = ?
        ORDER BY o.created_at DESC
        LIMIT 50
    """, (query.from_user.id,))
    orders = cur.fetchall()
    conn.close()
    
    if not orders:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')]]
        await query.edit_message_text(
            "📭 У вас пока нет заказов",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    text = "📋 Ваша история покупок (последние 50):\n\n"
    for order_id, title, status, created_at, paid_amount, is_demo, payload in orders:
        demo_mark = "🎁 " if is_demo else ""
        status_emoji = "✅" if status == "paid" else "❌"
        amount = f"{paid_amount/100:.0f} 2" if paid_amount else "Бесплатно"
        date = created_at[:19].replace('T', ' ')
        text += f"{demo_mark}{status_emoji} {title}  {amount}\n"
        text += f"📅 {date} — ID заказа: {order_id}\n\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

### Админка - управление офферами
async def manage_offers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        return
    
    keyboard = [
        [InlineKeyboardButton("➕ Добавить оффер", callback_data='add_offer')],
        [InlineKeyboardButton("📝 Список офферов", callback_data='list_offers')],
        [InlineKeyboardButton("🔙 Назад", callback_data='admin_menu')]
    ]
    
    await query.edit_message_text(
        "📋 Управление офферами:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# --- Новые/обновлённые хендлеры для управления офферами ---
async def list_offers_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.message.reply_text("❌ Доступ запрещён")
        return

    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT id, title, price FROM offers")
    offers = cur.fetchall()
    conn.close()

    if not offers:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='manage_offers')]]
        await query.edit_message_text("📭 Офферов пока нет", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    keyboard = []
    for offer_id, title, price in offers:
        keyboard.append([
            InlineKeyboardButton(f"{title} ({price/100:.0f} 2)", callback_data=f'edit_offer_{offer_id}'),
            InlineKeyboardButton("🗑️ Удалить", callback_data=f'delete_offer_{offer_id}')
        ])
    keyboard.append([InlineKeyboardButton("➕ Добавить оффер", callback_data='add_offer')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='manage_offers')])

    await query.edit_message_text("📋 Список офферов (админ):", reply_markup=InlineKeyboardMarkup(keyboard))


async def delete_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    offer_id = query.data[len('delete_offer_'):]
    conn = _conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM offers WHERE id = ?", (offer_id,))
    conn.commit()
    conn.close()
    # Обновим список после удаления
    await list_offers_admin(update, context)


async def edit_offer_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    offer_id = query.data[len('edit_offer_'):]
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT title, description, price FROM offers WHERE id = ?", (offer_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        await query.edit_message_text("❌ Оффер не найден")
        return
    title, desc, price = row
    await query.edit_message_text(
        f"✏️ Оффер:\n\n{title}\nЦена: {price/100:.0f} 2\n\n"
        "Чтобы изменить описание, нужно редактировать запись в БД. Описание не показывается клиентам до покупки."
    )

# --- Conversation: Добавление оффера ---
async def start_add_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.message.reply_text("❌ Доступ запрещён")
        return ConversationHandler.END
    # Начинаем диалог: просим название
    context.user_data['add_offer_step'] = TITLE
    await query.edit_message_text("➕ Введите название оффера (или /cancel чтобы отменить, /back чтобы вернуться):")
    return TITLE

async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Получаем название
    context.user_data['new_offer_title'] = update.message.text.strip()
    context.user_data['add_offer_step'] = DESC
    await update.message.reply_text("✏️ Теперь отправьте описание оффера (его увидит клиент только после покупки):")
    return DESC

async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['new_offer_desc'] = update.message.text.strip()
    context.user_data['add_offer_step'] = PRICE
    await update.message.reply_text("💰 И последняя: цена в копейках. Например: 70000 для 700₽ (или /back чтобы вернуться)")
    return PRICE

async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Ошибка: отправьте цену числом в копейках, например: 70000")
        return PRICE
    price = int(text)
    title = context.user_data.pop('new_offer_title', '')
    desc = context.user_data.pop('new_offer_desc', '')
    context.user_data.pop('add_offer_step', None)
    offer_id = str(uuid4())
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO offers VALUES(?,?,?,?)", (offer_id, title, desc, price))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Оффер '{title}' добавлен. Цена: {price/100:.0f} 2")
    return ConversationHandler.END

async def cancel_add_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Отключение диалога
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Отменено")
    else:
        await update.message.reply_text("Отменено")
    context.user_data.pop('add_offer_step', None)
    return ConversationHandler.END

async def back_add_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Возврат на предыдущий шаг диалога добавления оффера
    step = context.user_data.get('add_offer_step')
    if step is None:
        # нет диалога
        if update.message:
            await update.message.reply_text("Нет активного диалога.")
        else:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text("Нет активного диалога.")
        return ConversationHandler.END

    if step == PRICE:
        context.user_data['add_offer_step'] = DESC
        if update.message:
            await update.message.reply_text("Возврат. Введите описание оффера:")
        else:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text("Возврат. Введите описание оффера:")
        return DESC
    elif step == DESC:
        context.user_data['add_offer_step'] = TITLE
        if update.message:
            await update.message.reply_text("Возврат. Введите название оффера:")
        else:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text("Возврат. Введите название оффера:")
        return TITLE
    else:
        # на шаге TITLE — отменяем
        context.user_data.pop('add_offer_step', None)
        if update.message:
            await update.message.reply_text("Отменено")
        else:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text("Отменено")
        return ConversationHandler.END

### Статистика
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        return
    
    conn = _conn()
    cur = conn.cursor()
    
    # Общая статистика
    cur.execute("SELECT COUNT(*), SUM(paid_amount) FROM orders WHERE status = 'paid'")
    total_orders, total_revenue = cur.fetchone()
    total_revenue = total_revenue or 0
    
    # Статистика за сегодня
    today = datetime.utcnow().date().isoformat()
    cur.execute("""
        SELECT COUNT(*), SUM(paid_amount) FROM orders 
        WHERE status = 'paid' AND date(created_at) = ?
    """, (today,))
    today_orders, today_revenue = cur.fetchone()
    today_revenue = today_revenue or 0
    
    # Количество офферов
    cur.execute("SELECT COUNT(*) FROM offers")
    offers_count = cur.fetchone()[0]
    
    conn.close()
    
    text = f"📊 Статистика:\n\n"
    text += f"📦 Офферов: {offers_count}\n"
    text += f"📋 Всего заказов: {total_orders}\n"
    text += f"💰 Общий доход: {total_revenue / 100:.0f} 2\n\n"
    text += f"📅 Сегодня:\n"
    text += f"📋 Заказов: {today_orders}\n"
    text += f"💰 Доход: {today_revenue / 100:.0f} 2"
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='admin_menu')]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

### Помощь
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    text = ("ℹ️ Помощь:\n\n"
            "🎯 Доступные офферы - просмотр и покупка товаров\n"
            "📋 Мои заказы - история ваших покупок\n\n"
            "💳 Для оплаты используются банковские карты\n"
            "🔐 Все платежи защищены Telegram Payments\n\n"
            "❓ Если у вас возникли вопросы, обратитесь к администратору")
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

### Обработчики навигации
async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

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
