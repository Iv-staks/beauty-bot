import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import ContextManagement

# --- НАСТРОЙКИ ---
BOT_TOKEN = "ТВОЙ_ТОКЕН_ИЗ_BOTFATHER"
ADMIN_ID = 12345678  # Твой ID, чтобы бот присылал тебе записи

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Состояния для записи
class Appointment(StatesGroup):
    choosing_service = State()
    choosing_date = State()
    leaving_phone = State()

# --- КЛАВИАТУРЫ ---
def main_menu():
    builder = ReplyKeyboardBuilder()
    builder.row(types.KeyboardButton(text="💅 Записаться"))
    builder.row(types.KeyboardButton(text="💰 Прайс"), types.KeyboardButton(text="📸 Работы"))
    builder.row(types.KeyboardButton(text="📍 Адрес"))
    return builder.as_markup(resize_keyboard=True)

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def start(message: types.Message):
    await message.answer(f"Привет, {message.from_user.first_name}! ✨\nЯ бот-помощник студии маникюра. Выбирай нужное действие в меню:", 
                         reply_markup=main_menu())

@dp.message(F.text == "💰 Прайс")
async def price(message: types.Message):
    text = "💳 **Наши услуги:**\n\n• Маникюр + гель-лак: 1500₽\n• Педикюр: 1800₽\n• Дизайн: от 100₽"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "📍 Адрес")
async def location(message: types.Message):
    await message.answer("📍 Мы находимся: ул. Красивая, д. 10\n⏰ Работаем с 10:00 до 21:00")
    await bot.send_location(message.chat.id, latitude=55.7558, longitude=37.6173) # Замени на свои координаты

# --- ПРОЦЕСС ЗАПИСИ ---
@dp.message(F.text == "💅 Записаться")
async def start_booking(message: types.Message, state: ContextManagement):
    kb = InlineKeyboardBuilder()
    kb.add(types.InlineKeyboardButton(text="Маникюр", callback_data="serv_manicure"))
    kb.add(types.InlineKeyboardButton(text="Педикюр", callback_data="serv_pedicure"))
    
    await message.answer("Что планируем делать?", reply_markup=kb.as_markup())
    await state.set_state(Appointment.choosing_service)

@dp.callback_query(Appointment.choosing_service)
async def service_chosen(callback: types.CallbackQuery, state: ContextManagement):
    service = "Маникюр" if callback.data == "serv_manicure" else "Педикюр"
    await state.update_data(service=service)
    await callback.message.answer(f"Отлично! Напиши желаемую дату и время (например: завтра в 14:00)")
    await state.set_state(Appointment.choosing_date)

@dp.message(Appointment.choosing_date)
async def date_chosen(message: types.Message, state: ContextManagement):
    await state.update_data(date=message.text)
    kb = ReplyKeyboardBuilder()
    kb.row(types.KeyboardButton(text="Отправить номер телефона", contact=True))
    
    await message.answer("Оставь свой номер для связи (нажми кнопку ниже):", 
                         reply_markup=kb.as_markup(resize_keyboard=True, one_time_keyboard=True))
    await state.set_state(Appointment.leaving_phone)

@dp.message(Appointment.leaving_phone, F.contact)
async def finish_booking(message: types.Message, state: ContextManagement):
    data = await state.get_data()
    phone = message.contact.phone_number
    
    # Сообщение клиенту
    await message.answer(f"✅ Спасибо! Заявка принята.\nУслуга: {data['service']}\nВремя: {data['date']}\nМы свяжемся с вами!",
                         reply_markup=main_menu())
    
    # Уведомление мастеру (тебе)
    admin_text = f"🔥 НОВАЯ ЗАПИСЬ!\n👤 Клиент: @{message.from_user.username}\n📞 Тел: {phone}\n💅 Услуга: {data['service']}\n📅 Время: {data['date']}"
    await bot.send_message(ADMIN_ID, admin_text)
    await state.clear()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
