import asyncio
import logging
import os
import json
import sqlite3
import datetime
import threading
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from flask import Flask

# ---- Настройки (берём из переменных окружения) ----
API_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8887870966:AAFX_TmR8BlbByl1C6ma7WlWiyh11XlM6Ck')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 6873691042))  # ЗАМЕНИТЕ НА СВОЙ ID
PROVIDER_TOKEN = os.environ.get('PROVIDER_TOKEN', '390540012:LIVE:99229')  # Токен от @BotFather (для платежей)

# ---- Инициализация Firebase ----
firebase_creds_json = os.environ.get('FIREBASE_CREDENTIALS')
if firebase_creds_json:
    cred_dict = json.loads(firebase_creds_json)
    cred = credentials.Certificate(cred_dict)
else:
    # Для локальной разработки – файл должен лежать рядом
    cred = credentials.Certificate("firebase-key.json")

firebase_admin.initialize_app(cred)
db = firestore.client()

# ---- Локальная SQLite ----
def init_db():
    conn = sqlite3.connect('vpn_bot.db')
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            tariff TEXT,
            expires_at TEXT,
            is_active INTEGER DEFAULT 0
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            tariff TEXT,
            created_at TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS support_sessions (
            user_id INTEGER PRIMARY KEY,
            in_support INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ---- Функции работы с БД ----
def get_user(user_id):
    conn = sqlite3.connect('vpn_bot.db')
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def add_user(user_id, username):
    conn = sqlite3.connect('vpn_bot.db')
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()

def update_tariff(user_id, tariff, expires_at):
    conn = sqlite3.connect('vpn_bot.db')
    cur = conn.cursor()
    cur.execute('UPDATE users SET tariff = ?, expires_at = ?, is_active = 1 WHERE user_id = ?', (tariff, expires_at, user_id))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect('vpn_bot.db')
    cur = conn.cursor()
    cur.execute('SELECT user_id, username, tariff, expires_at, is_active FROM users')
    rows = cur.fetchall()
    conn.close()
    return rows

def set_support_mode(user_id, in_support):
    conn = sqlite3.connect('vpn_bot.db')
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO support_sessions (user_id, in_support) VALUES (?, ?)', (user_id, 1 if in_support else 0))
    conn.commit()
    conn.close()

def get_support_mode(user_id):
    conn = sqlite3.connect('vpn_bot.db')
    cur = conn.cursor()
    cur.execute('SELECT in_support FROM support_sessions WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] == 1 if row else False

def add_payment_record(user_id, amount, tariff):
    conn = sqlite3.connect('vpn_bot.db')
    cur = conn.cursor()
    cur.execute('INSERT INTO payments (user_id, amount, tariff, created_at) VALUES (?, ?, ?, ?)',
                (user_id, amount, tariff, datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()

# ---- Получение цен из Firebase ----
def get_prices_from_firebase():
    monthly = db.collection('settings').document('monthly_price').get()
    yearly = db.collection('settings').document('yearly_price').get()
    monthly_price = float(monthly.to_dict().get('value', '500')) if monthly.exists else 500.0
    yearly_price = float(yearly.to_dict().get('value', '5000')) if yearly.exists else 5000.0
    return monthly_price, yearly_price

# ---- Клавиатуры ----
def main_keyboard():
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text='🛒 Купить подписку'), KeyboardButton(text='📊 Мой статус')],
            [KeyboardButton(text='📞 Поддержка'), KeyboardButton(text='ℹ️ Инструкция'), KeyboardButton(text='🔄 Продлить')]
        ],
        resize_keyboard=True
    )
    return kb

def tariff_keyboard():
    monthly, yearly = get_prices_from_firebase()
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=f'1 месяц – {int(monthly)} ₽', callback_data='tariff_month'),
        InlineKeyboardButton(text=f'3 месяца – {int(monthly*3)} ₽', callback_data='tariff_3months')
    )
    builder.row(
        InlineKeyboardButton(text=f'6 месяцев – {int(monthly*6)} ₽', callback_data='tariff_6months'),
        InlineKeyboardButton(text=f'1 год – {int(yearly)} ₽', callback_data='tariff_year')
    )
    builder.row(InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_main'))
    return builder.as_markup()

# ---- Инициализация бота ----
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ---- Обработчики ----
@dp.message(Command('start'))
async def start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    add_user(user_id, username)
    set_support_mode(user_id, False)
    await message.answer(
        f"👋 Привет, {username}!\n\n"
        "Добро пожаловать в VPN-сервис. Здесь вы можете купить подписку на безопасный и быстрый VPN.\n"
        "Используйте кнопки ниже для навигации.",
        reply_markup=main_keyboard()
    )

@dp.message(lambda message: message.text == '🛒 Купить подписку')
async def buy_subscription(message: types.Message):
    set_support_mode(message.from_user.id, False)
    await message.answer(
        "Выберите подходящий тариф:",
        reply_markup=tariff_keyboard()
    )

@dp.callback_query(lambda c: c.data.startswith('tariff_'))
async def process_tariff(callback: types.CallbackQuery):
    tariff_map = {
        'tariff_month': ('1 месяц', 1, 30),
        'tariff_3months': ('3 месяца', 3, 90),
        'tariff_6months': ('6 месяцев', 6, 180),
        'tariff_year': ('1 год', 12, 365)
    }
    tariff_key = callback.data
    if tariff_key not in tariff_map:
        await callback.answer('Неизвестный тариф')
        return
    tariff_name, months, days = tariff_map[tariff_key]
    user_id = callback.from_user.id
    username = callback.from_user.username or callback.from_user.first_name
    monthly_price, yearly_price = get_prices_from_firebase()
    if tariff_key == 'tariff_year':
        amount = yearly_price
    else:
        amount = monthly_price * months

    # Отправляем счёт
    await callback.bot.send_invoice(
        chat_id=user_id,
        title=f"Подписка {tariff_name}",
        description=f"Оплата подписки на VPN-сервис на {days} дней.",
        payload=f"vpn_{tariff_key}_{user_id}",
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label=tariff_name, amount=int(amount * 100))],
        start_parameter="vpn_subscription"
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == 'back_to_main')
async def back_to_main(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=main_keyboard()
    )
    await callback.answer()

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True, error_message="Все хорошо, оплата проходит.")

@dp.message(lambda message: message.successful_payment is not None)
async def process_successful_payment(message: types.Message):
    payment_info = message.successful_payment
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name

    payload = payment_info.invoice_payload
    parts = payload.split('_')
    if len(parts) >= 3:
        tariff_key = parts[1]
        user_id_from_payload = int(parts[2])
        if user_id_from_payload != user_id:
            await message.answer("Ошибка: несоответствие пользователя.")
            return
        tariff_map = {
            'tariff_month': ('1 месяц', 30),
            'tariff_3months': ('3 месяца', 90),
            'tariff_6months': ('6 месяцев', 180),
            'tariff_year': ('1 год', 365)
        }
        if tariff_key in tariff_map:
            tariff_name, days = tariff_map[tariff_key]
            expires_at = (datetime.datetime.now() + datetime.timedelta(days=days)).isoformat()
            update_tariff(user_id, tariff_name, expires_at)
            add_payment_record(user_id, payment_info.total_amount / 100, tariff_name)
            transaction_data = {
                'amount': payment_info.total_amount / 100,
                'type': 'income',
                'description': f'Оплата подписки {tariff_name} от @{username}',
                'created_at': datetime.datetime.now().isoformat(),
                'user_id': str(user_id),
                'username': username,
                'payment_id': payment_info.provider_payment_charge_id
            }
            db.collection('transactions').add(transaction_data)
            await message.answer(
                f"✅ Оплата прошла успешно!\n"
                f"Тариф: {tariff_name}\n"
                f"Действует до: {expires_at}\n"
                f"Скоро вы получите свой VPN-ключ."
            )
        else:
            await message.answer("Ошибка: неизвестный тариф.")
    else:
        await message.answer("Ошибка: неверный формат платежа.")

@dp.message(lambda message: message.text == '📊 Мой статус')
async def my_status(message: types.Message):
    set_support_mode(message.from_user.id, False)
    user_id = message.from_user.id
    user = get_user(user_id)
    if user and user[3]:
        expires_at = datetime.datetime.fromisoformat(user[3])
        now = datetime.datetime.now()
        if expires_at > now:
            days_left = (expires_at - now).days
            await message.answer(
                f"✅ Ваша подписка активна.\n"
                f"Тариф: {user[2]}\n"
                f"Действует до: {expires_at.strftime('%d.%m.%Y')}\n"
                f"Осталось дней: {days_left}"
            )
        else:
            await message.answer(
                "❌ Ваша подписка истекла.\n"
                "Чтобы продлить, нажмите '🔄 Продлить'."
            )
    else:
        await message.answer(
            "❌ У вас нет активной подписки.\n"
            "Нажмите '🛒 Купить подписку', чтобы выбрать тариф."
        )

@dp.message(lambda message: message.text == 'ℹ️ Инструкция')
async def instruction(message: types.Message):
    set_support_mode(message.from_user.id, False)
    await message.answer(
        "📱 **Инструкция по подключению:**\n\n"
        "1. Скачайте приложение для вашего устройства:\n"
        "   - Android: V2RayTun (Google Play) или Happ\n"
        "   - iOS: Happ, Shadowrocket\n"
        "   - Windows: v2rayN\n"
        "   - macOS: V2RayX\n\n"
        "2. После оплаты вы получите ссылку-подписку (начинается с vless:// или vmess://).\n"
        "3. Скопируйте её и вставьте в приложение (обычно есть кнопка 'Импорт из буфера').\n"
        "4. Нажмите 'Подключиться' — и вы в безопасном интернете.\n\n"
        "Если возникнут вопросы — пишите в поддержку."
    )

@dp.message(lambda message: message.text == '📞 Поддержка')
async def support_start(message: types.Message):
    user_id = message.from_user.id
    set_support_mode(user_id, True)
    await message.answer(
        "✉️ Напишите ваше сообщение для администратора.\n"
        "После отправки мы свяжемся с вами в ближайшее время.\n"
        "Чтобы выйти из режима поддержки, нажмите /start."
    )

@dp.message(lambda message: message.text == '🔄 Продлить')
async def renew(message: types.Message):
    set_support_mode(message.from_user.id, False)
    await buy_subscription(message)

@dp.message()
async def handle_all_messages(message: types.Message):
    user_id = message.from_user.id
    if get_support_mode(user_id):
        username = message.from_user.username or message.from_user.first_name
        ticket_data = {
            'user_id': user_id,
            'username': username,
            'message': message.text,
            'status': 'new',
            'created_at': datetime.datetime.now().isoformat()
        }
        db.collection('tickets').add(ticket_data)
        await message.answer(
            "✅ Ваше сообщение отправлено администратору.\n"
            "Ожидайте ответа. Чтобы выйти из режима поддержки, нажмите /start."
        )
        set_support_mode(user_id, False)
    else:
        await message.answer(
            "Используйте кнопки ниже для навигации.\n"
            "Если хотите написать в поддержку, нажмите кнопку '📞 Поддержка'.",
            reply_markup=main_keyboard()
        )

@dp.message(Command('admin'))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа.")
        return
    users = get_all_users()
    if not users:
        await message.answer("Нет пользователей.")
        return
    text = "📋 **Список пользователей:**\n\n"
    for user_id, username, tariff, expires_at, is_active in users:
        status = "✅ Активен" if is_active else "❌ Неактивен"
        text += f"ID: {user_id} | @{username} | {tariff or 'без тарифа'} | {expires_at or '—'} | {status}\n"
    await message.answer(text)

# ---- Flask-сервер для пинга (keep-alive) ----
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    flask_app.run(host='0.0.0.0', port=port)

# ---- Запуск бота и веб-сервера параллельно ----
async def main():
    # Запускаем Flask в отдельном потоке
    threading.Thread(target=run_flask, daemon=True).start()
    # Запускаем бота (polling)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())