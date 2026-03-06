import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor
import logging
import calendar
from datetime import datetime, date
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler,
)
from telegram.constants import ParseMode

# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ — берутся из переменных окружения Railway
# ══════════════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("❌ Переменная BOT_TOKEN не задана! Добавь её в настройках Railway.")
if not ADMIN_ID:
    raise ValueError("❌ Переменная ADMIN_ID не задана! Добавь её в настройках Railway.")

# ══════════════════════════════════════════════════════════════
#  КАЛЕНДАРЬ
# ══════════════════════════════════════════════════════════════
MONTHS_RU = ["","Январь","Февраль","Март","Апрель","Май","Июнь",
             "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
DAYS_RU   = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]

# ══════════════════════════════════════════════════════════════
#  ПРАЙС-ЛИСТ — редактируй здесь!
# ══════════════════════════════════════════════════════════════
PRICE_LIST_DEFAULT = """💅 *Прайс-лист*

*Маникюр*
• Маникюр без покрытия — 800 ₽
• Маникюр + гель-лак — 1 500 ₽
• Снятие гель-лака — 300 ₽

*Педикюр*
• Педикюр без покрытия — 1 200 ₽
• Педикюр + гель-лак — 1 800 ₽

*Комплексы*
• Маникюр + педикюр — 2 500 ₽
• Маникюр + педикюр + гель-лак — 3 500 ₽

*Дизайн*
• Простой дизайн — от 100 ₽
• Сложный дизайн — от 300 ₽

📍 Запись через кнопку 📅 Записаться"""

def build_calendar(year: int, month: int, free_dates: set) -> InlineKeyboardMarkup:
    today = local_today()
    kb = []
    prev_y, prev_m = (year, month-1) if month > 1 else (year-1, 12)
    next_y, next_m = (year, month+1) if month < 12 else (year+1, 1)
    kb.append([
        InlineKeyboardButton("◀", callback_data=f"cal_nav:{prev_y}:{prev_m}"),
        InlineKeyboardButton(f"  {MONTHS_RU[month]} {year}  ", callback_data="cal_ignore"),
        InlineKeyboardButton("▶", callback_data=f"cal_nav:{next_y}:{next_m}"),
    ])
    kb.append([InlineKeyboardButton(d, callback_data="cal_ignore") for d in DAYS_RU])
    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal_ignore"))
                continue
            d = date(year, month, day)
            key = d.strftime("%Y-%m-%d")
            if d < today:
                row.append(InlineKeyboardButton("·", callback_data="cal_ignore"))
            elif key in free_dates:
                row.append(InlineKeyboardButton(f"✦{day}✦", callback_data=f"cal_pick:{key}"))
            else:
                row.append(InlineKeyboardButton(f"· {day} ·", callback_data="cal_busy"))
        kb.append(row)
    kb.append([InlineKeyboardButton("❌ Отмена", callback_data="cal_cancel")])
    return InlineKeyboardMarkup(kb)

# ══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════
def get_db():
    """Подключение к PostgreSQL через DATABASE_URL"""
    return psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require")


def db_init():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            id SERIAL PRIMARY KEY,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            booked INTEGER DEFAULT 0,
            user_id BIGINT, username TEXT, name TEXT, phone TEXT
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS reminders_sent (
            slot_id INTEGER NOT NULL,
            reminder_type TEXT NOT NULL,
            PRIMARY KEY (slot_id, reminder_type)
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id SERIAL PRIMARY KEY,
            file_id TEXT NOT NULL,
            caption TEXT
        )""")
        conn.commit()


def get_price_list():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key='price_list'")
        row = cur.fetchone()
    return row[0] if row else PRICE_LIST_DEFAULT

def set_price_list(text):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO settings(key,value) VALUES('price_list',%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
        """, (text,))
        conn.commit()

def get_free_dates():
    today = local_today().strftime("%Y-%m-%d")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT date FROM slots WHERE booked=0 AND date>=%s", (today,))
        rows = cur.fetchall()
    return {r[0] for r in rows}

def get_free_slots(d):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, time FROM slots WHERE date=%s AND booked=0 ORDER BY time", (d,))
        return cur.fetchall()

def add_slot(d, t):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM slots WHERE date=%s AND time=%s", (d, t))
        if cur.fetchone():
            return False
        cur.execute("INSERT INTO slots(date,time) VALUES(%s,%s)", (d, t))
        conn.commit()
        return True

def book_slot(slot_id, user_id, username, name, phone):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE slots SET booked=1,user_id=%s,username=%s,name=%s,phone=%s WHERE id=%s",
            (user_id, username, name, phone, slot_id)
        )
        conn.commit()

def cancel_booking(slot_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE slots SET booked=0,user_id=NULL,username=NULL,name=NULL,phone=NULL WHERE id=%s",
            (slot_id,)
        )
        conn.commit()

def get_user_bookings(user_id):
    today = local_today().strftime("%Y-%m-%d")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id,date,time FROM slots WHERE user_id=%s AND date>=%s ORDER BY date,time",
            (user_id, today)
        )
        return cur.fetchall()

def get_all_bookings():
    today = local_today().strftime("%Y-%m-%d")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id,date,time,name,username,phone FROM slots WHERE booked=1 AND date>=%s ORDER BY date,time",
            (today,)
        )
        return cur.fetchall()

def get_all_slots_admin():
    today = local_today().strftime("%Y-%m-%d")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id,date,time,booked FROM slots WHERE date>=%s ORDER BY date,time", (today,)
        )
        return cur.fetchall()

def delete_slot(slot_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM slots WHERE id=%s", (slot_id,))
        conn.commit()

def get_portfolio():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id,file_id,caption FROM portfolio ORDER BY id")
        return cur.fetchall()

def add_portfolio(file_id, caption):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO portfolio(file_id,caption) VALUES(%s,%s)", (file_id, caption))
        conn.commit()

def del_portfolio(pid):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM portfolio WHERE id=%s", (pid,))
        conn.commit()

def get_bookings_for_reminder(hours_before):
    from datetime import timedelta
    now = local_now()
    reminder_type = f"{hours_before}h"
    window_from = now + timedelta(hours=hours_before, minutes=-30)
    window_to   = now + timedelta(hours=hours_before, minutes=30)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.id, s.date, s.time, s.user_id, s.name, s.phone
            FROM slots s
            WHERE s.booked=1
              AND s.user_id IS NOT NULL
              AND s.date >= %s AND s.date <= %s
              AND NOT EXISTS (
                  SELECT 1 FROM reminders_sent r
                  WHERE r.slot_id=s.id AND r.reminder_type=%s
              )
        """, (
            window_from.strftime("%Y-%m-%d"),
            window_to.strftime("%Y-%m-%d"),
            reminder_type
        ))
        rows = cur.fetchall()
    result = []
    for row in rows:
        slot_id, d, t, user_id, name, phone = row
        try:
            slot_dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M")
            diff_hours = (slot_dt - now).total_seconds() / 3600
            if hours_before - 0.5 <= diff_hours <= hours_before + 0.5:
                result.append(row)
        except Exception:
            pass
    return result

def mark_reminder_sent(slot_id, hours_before):
    reminder_type = f"{hours_before}h"
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO reminders_sent(slot_id,reminder_type) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (slot_id, reminder_type)
        )
        conn.commit()

def get_all_bookings_with_userid():
    today = local_today().strftime("%Y-%m-%d")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id,date,time,name,username,phone,user_id FROM slots WHERE booked=1 AND date>=%s ORDER BY date,time",
            (today,)
        )
        return cur.fetchall()

# ══════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
# Часовой пояс мастера (Railway работает в UTC)
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "12"))

def local_now():
    """Текущее время в часовом поясе мастера"""
    from datetime import timezone, timedelta
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET)

def local_today():
    """Сегодняшняя дата в часовом поясе мастера"""
    return local_now().date()

logger = logging.getLogger(__name__)

BOOK_TIME, BOOK_CONFIRM, RESCHEDULE_PICK = range(3)
ADMIN_WAIT_DATE, ADMIN_WAIT_TIME, ADMIN_WAIT_PHOTO, ADMIN_WAIT_CAPTION, ADMIN_WAIT_PRICE, ADMIN_WAIT_MONTH_DATE, ADMIN_WAIT_MONTH_TIME, ADMIN_RESCHEDULE_DATE, ADMIN_RESCHEDULE_TIME = range(4, 13)

def fmt_date(d):
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        months = ["янв","фев","мар","апр","мая","июн","июл","авг","сен","окт","ноя","дек"]
        days   = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
        return f"{days[dt.weekday()]}, {dt.day} {months[dt.month-1]}"
    except:
        return d

def main_menu_kb(admin=False):
    kb = [
        [KeyboardButton("📅 Записаться"), KeyboardButton("🖼 Портфолио")],
        [KeyboardButton("📋 Мои записи"), KeyboardButton("💰 Прайс")],
    ]
    if admin:
        kb.append([KeyboardButton("⚙️ Панель мастера")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def is_admin(uid):
    return uid == ADMIN_ID

# ══════════════════════════════════════════════════════════════
#  /start и /cancel
# ══════════════════════════════════════════════════════════════

async def send_reminders(context):
    """Фоновая задача: отправляет напоминания за 24ч и за 2ч"""
    for hours in [24, 2]:
        rows = get_bookings_for_reminder(hours)
        for slot_id, d, t, user_id, name, phone in rows:
            try:
                if hours == 24:
                    text = (
                        f"⏰ *Напоминание!*\n\n"
                        f"Вы записаны *завтра*:\n"
                        f"📅 {fmt_date(d)} в 🕐 {t}\n\n"
                        f"Ждём вас! 💅\n"
                        f"Если планы изменились — отмените в разделе 📋 Мои записи"
                    )
                else:
                    text = (
                        f"⏰ *Скоро ваша запись!*\n\n"
                        f"📅 {fmt_date(d)} в 🕐 {t}\n\n"
                        f"Осталось всего 2 часа 💅"
                    )
                await context.bot.send_message(user_id, text, parse_mode="Markdown")
                mark_reminder_sent(slot_id, hours)
                logger.info(f"Reminder {hours}h sent to {user_id} for slot {slot_id}")
            except Exception as e:
                logger.warning(f"Failed to send reminder to {user_id}: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ctx.user_data.clear()
    await update.message.reply_text(
        f"💅 *Привет, {user.first_name}!*\n\n"
        "Добро пожаловать в студию красоты.\n"
        "• 📅 Запись на удобное время\n"
        "• 🖼 Портфолио работ\n"
        "• 📋 Управление записями\n\n"
        "_Выбери действие в меню ниже_ 👇",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(is_admin(user.id))
    )
    return ConversationHandler.END

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "↩️ Отменено. Выбери действие в меню.",
        reply_markup=main_menu_kb(is_admin(update.effective_user.id))
    )
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════
#  /slots — просмотр всех слотов (только для мастера)
# ══════════════════════════════════════════════════════════════
async def cmd_slots(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = get_all_slots_admin()
    if not rows:
        await update.message.reply_text(
            "📋 В базе нет ни одного слота.\n\n"
            "Добавь командой:\n`/addslot 10.03.2026 10:00 12:00 14:00`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    text = f"📋 *Все слоты в базе ({len(rows)} шт):*\n\n"
    for sid, d, t, booked in rows:
        status = "🔴 занят" if booked else "🟢 свободен"
        text += f"#{sid} | {fmt_date(d)} | {t} | {status}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════
#  /addslot — быстрое добавление слота
#  Пример: /addslot 15.06.2026 10:00 12:00 14:30
# ══════════════════════════════════════════════════════════════
async def cmd_addslot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "⚙️ *Использование:*\n`/addslot 15.06.2026 10:00 12:00 14:30`\n\n"
            "Первый аргумент — дата, остальные — время.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    try:
        dt = datetime.strptime(args[0], "%d.%m.%Y")
        d  = dt.strftime("%Y-%m-%d")
    except ValueError:
        await update.message.reply_text(
            "⚠️ Неверный формат даты.\nПример: `/addslot 15.06.2026 10:00 12:00`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    added, skipped, errors = [], [], []
    for t in args[1:]:
        try:
            datetime.strptime(t, "%H:%M")
            if add_slot(d, t):
                added.append(t)
            else:
                skipped.append(t)
        except ValueError:
            errors.append(t)

    parts = []
    if added:
        parts.append("✅ *Добавлены:*\n" + "\n".join(f"  🕐 {t}" for t in added))
    if skipped:
        parts.append("⚠️ *Уже существуют:*\n" + "\n".join(f"  · {t}" for t in skipped))
    if errors:
        parts.append("❌ *Не распознаны:*\n" + "\n".join(f"  · {t}" for t in errors))

    await update.message.reply_text(
        f"📅 *{fmt_date(d)}*\n\n" + "\n\n".join(parts),
        parse_mode=ParseMode.MARKDOWN
    )


# ══════════════════════════════════════════════════════════════
#  /addmonth — добавить слоты на весь месяц
#  Пример: /addmonth 06.2026 10:00 12:40 14:20 17:00 19:40
#  Или с выходными: /addmonth 06.2026 10:00 12:00 --no-weekend
# ══════════════════════════════════════════════════════════════
async def cmd_addmonth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Использование:\n/addmonth 06.2026 10:00 12:40 14:20 17:00 19:40\n\n"
            "Добавляет время на каждый день месяца.\n\n"
            "Чтобы пропустить выходные (сб/вс):\n/addmonth 06.2026 10:00 14:00 --no-weekend",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    no_weekend = "--no-weekend" in args
    time_args = [a for a in args[1:] if a != "--no-weekend"]
    try:
        month_dt = datetime.strptime(args[0], "%m.%Y")
    except ValueError:
        await update.message.reply_text(
            "Неверный формат. Пример: /addmonth 06.2026 10:00 14:00",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    times, bad_times = [], []
    for t in time_args:
        try:
            datetime.strptime(t, "%H:%M")
            times.append(t)
        except ValueError:
            bad_times.append(t)
    if not times:
        await update.message.reply_text("Не указано ни одного времени.")
        return
    year = month_dt.year
    month = month_dt.month
    _, days_count = calendar.monthrange(year, month)
    today = local_today()
    added_days = skipped_days = skipped_weekend = 0
    for day in range(1, days_count + 1):
        d = date(year, month, day)
        if d < today:
            continue
        if no_weekend and d.weekday() in (5, 6):
            skipped_weekend += 1
            continue
        d_str = d.strftime("%Y-%m-%d")
        day_added = False
        for t in times:
            if add_slot(d_str, t):
                day_added = True
        if day_added:
            added_days += 1
        else:
            skipped_days += 1
    month_name = MONTHS_RU[month]
    msg = f"*{month_name} {year}*\n\n"
    msg += f"Добавлено дней: *{added_days}*\n"
    msg += f"Время: {', '.join(times)}\n"
    if skipped_weekend:
        msg += f"Пропущено выходных: {skipped_weekend}\n"
    if skipped_days:
        msg += f"Уже были: {skipped_days} дн.\n"
    if bad_times:
        msg += f"Не распознаны: {', '.join(bad_times)}\n"
    msg += "\nКлиенты видят свободные даты!"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def show_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_price_list(), parse_mode=ParseMode.MARKDOWN)

async def show_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = get_portfolio()
    if not items:
        await update.message.reply_text("🖼 Портфолио пока пусто. Скоро здесь появятся работы! ✨")
        return
    await update.message.reply_text("🖼 *Моё портфолио:*", parse_mode=ParseMode.MARKDOWN)
    for i, (_, file_id, caption) in enumerate(items, 1):
        cap = f"✨ *Работа #{i}*" + (f"\n\n{caption}" if caption else "")
        await update.message.reply_photo(photo=file_id, caption=cap, parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════
#  ЗАПИСЬ — КАЛЕНДАРЬ
# ══════════════════════════════════════════════════════════════
async def booking_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Сбрасываем старый диалог если был
    ctx.user_data.clear()
    free = get_free_dates()
    if not free:
        await update.message.reply_text(
            "😔 Свободных слотов пока нет.\n"
            "Загляни позже или напиши мне напрямую!"
        )
        return ConversationHandler.END
    now = local_now()
    ctx.user_data["cal_year"]  = now.year
    ctx.user_data["cal_month"] = now.month
    await update.message.reply_text(
        "📅 *Выбери дату:*\n\n✦число✦ — свободно\n· число · — занято",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_calendar(now.year, now.month, free)
    )
    return BOOK_TIME

async def calendar_navigate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, year, month = q.data.split(":")
    year, month = int(year), int(month)
    ctx.user_data["cal_year"]  = year
    ctx.user_data["cal_month"] = month
    await q.edit_message_reply_markup(reply_markup=build_calendar(year, month, get_free_dates()))

async def calendar_busy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("🔒 Эта дата занята", show_alert=False)

async def calendar_ignore(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

async def calendar_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chosen_date = q.data.split(":")[1]
    ctx.user_data["book_date"] = chosen_date
    slots = get_free_slots(chosen_date)
    if not slots:
        await q.edit_message_text("😔 Слоты только что заняли. Выбери другую дату.")
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"🕐 {t}", callback_data=f"slot:{sid}|{t}")] for sid, t in slots]
    kb.append([InlineKeyboardButton("⬅️ Назад к календарю", callback_data="back_calendar")])
    await q.edit_message_text(
        f"📅 *{fmt_date(chosen_date)}*\n\nВыбери удобное время:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return BOOK_TIME

async def back_to_calendar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    year  = ctx.user_data.get("cal_year",  datetime.now().year)
    month = ctx.user_data.get("cal_month", datetime.now().month)
    await q.edit_message_text(
        "📅 *Выбери дату:*\n\n✦число✦ — свободно\n· число · — занято",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_calendar(year, month, get_free_dates())
    )
    return BOOK_TIME

async def booking_time_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # format: slot:{sid}|{time}
    _, rest = q.data.split(":", 1)
    sid_str, t = rest.split("|", 1)
    ctx.user_data["book_slot_id"] = int(sid_str)
    ctx.user_data["book_time"]    = t
    await q.edit_message_text(
        f"📋 *Почти готово!*\n\n"
        f"📅 {fmt_date(ctx.user_data['book_date'])}\n"
        f"🕐 {t}\n\n"
        "Введи своё *имя* и *номер телефона* через запятую:\n"
        "_Например: Анна, +79001234567_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cal_cancel")]])
    )
    return BOOK_CONFIRM

async def booking_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Запись отменена.")
    return ConversationHandler.END

async def booking_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "," not in text:
        await update.message.reply_text(
            "⚠️ Введи через запятую: *Имя, +7телефон*",
            parse_mode=ParseMode.MARKDOWN
        )
        return BOOK_CONFIRM
    name, phone = [x.strip() for x in text.split(",", 1)]
    user    = update.effective_user
    slot_id = ctx.user_data["book_slot_id"]
    d       = ctx.user_data["book_date"]
    t       = ctx.user_data["book_time"]
    book_slot(slot_id, user.id, user.username or "", name, phone)
    await update.message.reply_text(
        f"🎉 *Запись подтверждена!*\n\n📅 {fmt_date(d)}\n🕐 {t}\n👤 {name}\n📞 {phone}\n\nБуду ждать! 💅",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(is_admin(user.id))
    )
    await ctx.bot.send_message(
        ADMIN_ID,
        f"🔔 *Новая запись!*\n📅 {fmt_date(d)} | 🕐 {t}\n👤 {name}\n📞 {phone}\n🆔 @{user.username or user.first_name}",
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════
#  МОИ ЗАПИСИ
# ══════════════════════════════════════════════════════════════
async def my_bookings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_user_bookings(update.effective_user.id)
    if not rows:
        await update.message.reply_text("📋 У тебя пока нет активных записей.")
        return
    text = "📋 *Твои записи:*\n\n"
    kb   = []
    for rid, d, t in rows:
        text += f"• {fmt_date(d)} в {t}\n"
        kb.append([
            InlineKeyboardButton(f"🔄 Перенести", callback_data=f"myreschedule:{rid}"),
            InlineKeyboardButton(f"❌ Отменить",  callback_data=f"mycancel:{rid}"),
        ])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))

async def my_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cancel_booking(int(q.data.split(":")[1]))
    await q.edit_message_text("✅ Запись отменена. Слот снова свободен.")
async def my_reschedule_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Клиент нажал Перенести — отменяем старый слот и открываем календарь"""
    q = update.callback_query
    await q.answer()
    old_slot_id = int(q.data.split(":")[1])
    ctx.user_data["reschedule_old_id"] = old_slot_id
    free = get_free_dates()
    if not free:
        await q.edit_message_text("😔 Свободных слотов нет для переноса.")
        return ConversationHandler.END
    now = datetime.now()
    ctx.user_data["cal_year"]  = now.year
    ctx.user_data["cal_month"] = now.month
    await q.edit_message_text(
        "🔄 *Перенос записи*\n\nВыбери новую дату:\n\n✦число✦ — свободно\n· число · — занято",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_calendar(now.year, now.month, free)
    )
    return RESCHEDULE_PICK

async def reschedule_navigate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, year, month = q.data.split(":")
    year, month = int(year), int(month)
    ctx.user_data["cal_year"]  = year
    ctx.user_data["cal_month"] = month
    await q.edit_message_reply_markup(reply_markup=build_calendar(year, month, get_free_dates()))

async def reschedule_pick_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chosen_date = q.data.split(":")[1]
    ctx.user_data["reschedule_date"] = chosen_date
    slots = get_free_slots(chosen_date)
    if not slots:
        await q.edit_message_text("😔 Слоты только что заняли. Выбери другую дату.")
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"🕐 {t}", callback_data=f"rslot:{sid}|{t}")] for sid, t in slots]
    kb.append([InlineKeyboardButton("⬅️ Назад к календарю", callback_data="rback_calendar")])
    await q.edit_message_text(
        f"📅 *{fmt_date(chosen_date)}*\n\nВыбери новое время:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return RESCHEDULE_PICK

async def reschedule_back_calendar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    year  = ctx.user_data.get("cal_year",  datetime.now().year)
    month = ctx.user_data.get("cal_month", datetime.now().month)
    await q.edit_message_text(
        "🔄 *Перенос записи*\n\nВыбери новую дату:\n\n✦число✦ — свободно\n· число · — занято",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_calendar(year, month, get_free_dates())
    )
    return RESCHEDULE_PICK

async def reschedule_pick_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, rest = q.data.split(":", 1)
    sid_str, t = rest.split("|", 1)
    new_slot_id  = int(sid_str)
    new_date     = ctx.user_data["reschedule_date"]
    old_slot_id  = ctx.user_data["reschedule_old_id"]
    user         = q.from_user

    # Получаем данные старой записи
    with get_db() as c:
        cur2 = conn.cursor()
        cur2.execute("SELECT name, phone FROM slots WHERE id=%s", (old_slot_id,))
        old = cur2.fetchone()

    if not old:
        await q.edit_message_text("⚠️ Старая запись не найдена.")
        return ConversationHandler.END

    name, phone = old
    cancel_booking(old_slot_id)
    book_slot(new_slot_id, user.id, user.username or "", name, phone)

    await q.edit_message_text(
        f"🔄 *Запись перенесена!*\n\n"
        f"📅 {fmt_date(new_date)}\n"
        f"🕐 {t}\n"
        f"👤 {name}\n📞 {phone}\n\nБуду ждать! 💅",
        parse_mode=ParseMode.MARKDOWN
    )
    await ctx.bot.send_message(
        ADMIN_ID,
        f"🔄 *Перенос записи!*\n📅 {fmt_date(new_date)} | 🕐 {t}\n👤 {name} | 📞 {phone}\n🆔 @{user.username or user.first_name}",
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END



# ══════════════════════════════════════════════════════════════
#  ПАНЕЛЬ МАСТЕРА
# ══════════════════════════════════════════════════════════════
async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Нет доступа.")
        return
    await update.message.reply_text(
        "⚙️ *Панель мастера*\n\nЧто хочешь сделать?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить один день",   callback_data="adm:add_slot")],
            [InlineKeyboardButton("📆 Добавить весь месяц",    callback_data="adm:add_month")],
            [InlineKeyboardButton("🗑 Удалить слот",         callback_data="adm:del_slot")],
            [InlineKeyboardButton("📋 Все записи",           callback_data="adm:bookings")],
            [InlineKeyboardButton("🔄 Перенести запись",       callback_data="adm:reschedule")],
            [InlineKeyboardButton("🖼 Добавить в портфолио", callback_data="adm:add_photo")],
            [InlineKeyboardButton("🗑 Удалить из портфолио", callback_data="adm:del_photo")],
            [InlineKeyboardButton("💰 Редактировать прайс",   callback_data="adm:edit_price")],
        ])
    )

async def admin_add_month_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "*📆 Добавить весь месяц*\n\n"
        "Введи месяц и год в формате *ММ.ГГГГ*\n\n"
        "_Например: 06.2026_\n\n"
        "/cancel — отмена",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADMIN_WAIT_MONTH_DATE

async def admin_got_month_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        month_dt = datetime.strptime(text, "%m.%Y")
        ctx.user_data["adm_month"] = text
        ctx.user_data["adm_month_dt"] = month_dt
        month_name = MONTHS_RU[month_dt.month]
        await update.message.reply_text(
            f"✅ Месяц: *{month_name} {month_dt.year}*\n\n"
            "⏰ Введи время(а) через запятую:\n"
            "_Например: 10:00, 12:40, 14:20, 17:00, 19:40_\n\n"
            "Чтобы пропустить выходные, добавь в конце: *без выходных*\n"
            "_Например: 10:00, 14:00, без выходных_\n\n"
            "/cancel — отмена",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADMIN_WAIT_MONTH_TIME
    except ValueError:
        await update.message.reply_text(
            "⚠️ Неверный формат!\nВведи месяц так: *06.2026*",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADMIN_WAIT_MONTH_DATE

async def admin_got_month_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    no_weekend = "без выходных" in text.lower()
    clean = text.lower().replace("без выходных", "")
    time_parts = [t.strip() for t in re.split(r"[,\s]+", clean) if t.strip()]

    times, bad_times = [], []
    for t in time_parts:
        try:
            datetime.strptime(t, "%H:%M")
            times.append(t)
        except ValueError:
            if t:
                bad_times.append(t)

    if not times:
        await update.message.reply_text(
            "⚠️ Не распознано ни одного времени.\nВведи: *10:00, 12:40, 14:20*",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADMIN_WAIT_MONTH_TIME

    month_dt = ctx.user_data["adm_month_dt"]
    year = month_dt.year
    month = month_dt.month
    _, days_count = calendar.monthrange(year, month)
    today = local_today()
    added_days = skipped_days = skipped_weekend = 0

    for day in range(1, days_count + 1):
        d = date(year, month, day)
        if d < today:
            continue
        if no_weekend and d.weekday() in (5, 6):
            skipped_weekend += 1
            continue
        d_str = d.strftime("%Y-%m-%d")
        day_added = False
        for t in times:
            if add_slot(d_str, t):
                day_added = True
        if day_added:
            added_days += 1
        else:
            skipped_days += 1

    month_name = MONTHS_RU[month]
    msg = f"📆 *{month_name} {year}*\n\n"
    msg += f"✅ Добавлено дней: *{added_days}*\n"
    msg += "🕐 Время: " + ", ".join(times) + "\n"
    if skipped_weekend:
        msg += f"📴 Пропущено выходных: {skipped_weekend}\n"
    if skipped_days:
        msg += f"⚠️ Уже были: {skipped_days} дн.\n"
    if bad_times:
        msg += f"❌ Не распознаны: {', '.join(bad_times)}\n"
    msg += "\nКлиенты видят свободные даты! 🎉"

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=main_menu_kb(True))
    return ConversationHandler.END

async def admin_edit_price_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    current = get_price_list()
    await q.edit_message_text(
        f"💰 *Текущий прайс:*\n\n{current}\n\n"
        "✏️ Отправь новый прайс следующим сообщением.\n\n"
        "/cancel — отмена",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADMIN_WAIT_PRICE

async def admin_got_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    set_price_list(text)
    await update.message.reply_text(
        "✅ Прайс обновлён! Клиенты уже видят новые цены 💰",
        reply_markup=main_menu_kb(True)
    )
    return ConversationHandler.END


async def admin_rs_pick_slot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Мастер выбрал запись для переноса"""
    q = update.callback_query
    await q.answer()
    old_slot_id = int(q.data.split(":")[1])
    ctx.user_data["adm_rs_old_id"] = old_slot_id
    with get_db() as c:
        cur2 = conn.cursor()
        cur2.execute("SELECT date,time,name,phone FROM slots WHERE id=%s", (old_slot_id,))
        row = cur2.fetchone()
    if not row:
        await q.edit_message_text("⚠️ Запись не найдена.")
        return ConversationHandler.END
    d, t, name, phone = row
    ctx.user_data["adm_rs_name"]  = name
    ctx.user_data["adm_rs_phone"] = phone
    await q.edit_message_text(
        f"🔄 *Перенос записи*\n\n"
        f"👤 {name} | 📞 {phone}\n"
        f"📅 {fmt_date(d)} в 🕐 {t}\n\n"
        "Введи *новую дату* в формате ДД.ММ.ГГГГ:\n"
        "_Например: 15.06.2026_\n\n"
        "/cancel — отмена",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADMIN_RESCHEDULE_DATE

async def admin_rs_got_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        dt = datetime.strptime(text, "%d.%m.%Y")
        if dt.date() < local_today():
            await update.message.reply_text("⚠️ Дата уже прошла. Введи будущую дату:")
            return ADMIN_RESCHEDULE_DATE
        ctx.user_data["adm_rs_date"] = dt.strftime("%Y-%m-%d")
        slots = get_free_slots(ctx.user_data["adm_rs_date"])
        if not slots:
            await update.message.reply_text(
                f"😔 На *{fmt_date(ctx.user_data['adm_rs_date'])}* нет свободных слотов.\n\nВведи другую дату:",
                parse_mode=ParseMode.MARKDOWN
            )
            return ADMIN_RESCHEDULE_DATE
        kb = [[InlineKeyboardButton(f"🕐 {t}", callback_data=f"adm_rs_time:{sid}|{t}")] for sid, t in slots]
        kb.append([InlineKeyboardButton("❌ Отмена", callback_data="adm_rs_cancel")])
        await update.message.reply_text(
            f"📅 *{fmt_date(ctx.user_data['adm_rs_date'])}*\n\nВыбери новое время:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ADMIN_RESCHEDULE_TIME
    except ValueError:
        await update.message.reply_text("⚠️ Неверный формат. Введи дату так: *15.06.2026*", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_RESCHEDULE_DATE

async def admin_rs_got_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "adm_rs_cancel":
        await q.edit_message_text("Перенос отменён.")
        return ConversationHandler.END
    _, rest = q.data.split(":", 1)
    sid_str, new_time = rest.split("|", 1)
    new_slot_id  = int(sid_str)
    new_date     = ctx.user_data["adm_rs_date"]
    old_slot_id  = ctx.user_data["adm_rs_old_id"]
    name         = ctx.user_data["adm_rs_name"]
    phone        = ctx.user_data["adm_rs_phone"]

    # Получаем user_id клиента
    with get_db() as c:
        cur2 = conn.cursor()
        cur2.execute("SELECT user_id, username FROM slots WHERE id=%s", (old_slot_id,))
        row = cur2.fetchone()
    user_id, username = row if row else (None, None)

    cancel_booking(old_slot_id)
    book_slot(new_slot_id, user_id, username or "", name, phone)

    await q.edit_message_text(
        f"✅ *Запись перенесена!*\n\n"
        f"👤 {name} | 📞 {phone}\n"
        f"📅 {fmt_date(new_date)} в 🕐 {new_time}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(True)
    )
    # Уведомляем клиента
    if user_id:
        try:
            await q.bot.send_message(
                user_id,
                f"🔄 *Ваша запись перенесена мастером*\n\n"
                f"📅 {fmt_date(new_date)} в 🕐 {new_time}\n\n"
                f"Если есть вопросы — напишите нам! 💅",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    return ConversationHandler.END

async def admin_add_slot_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "📅 *Введи дату* в формате ДД.ММ.ГГГГ\n\n"
        "_Например: 15.06.2026_\n\n"
        "/cancel — отмена",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADMIN_WAIT_DATE

async def admin_got_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        dt = datetime.strptime(text, "%d.%m.%Y")
        if dt.date() < local_today():
            await update.message.reply_text("⚠️ Эта дата уже прошла. Введи будущую дату:")
            return ADMIN_WAIT_DATE
        ctx.user_data["adm_date"] = dt.strftime("%Y-%m-%d")
        await update.message.reply_text(
            f"✅ Дата: *{fmt_date(ctx.user_data['adm_date'])}*\n\n"
            "⏰ Введи время(а) через запятую:\n"
            "_Например: 10:00, 11:30, 14:00_\n\n"
            "/cancel — отмена",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADMIN_WAIT_TIME
    except ValueError:
        await update.message.reply_text(
            "⚠️ Неверный формат!\nВведи дату так: *15.06.2026*",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADMIN_WAIT_DATE

async def admin_got_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    d    = ctx.user_data.get("adm_date")
    added, skipped, errors = [], [], []
    # Принимаем запятую и пробел как разделитель
    raw_times = re.split(r"[,\s]+", text)
    for t in [x.strip() for x in raw_times if x.strip()]:
        try:
            datetime.strptime(t, "%H:%M")
            if add_slot(d, t):
                added.append(t)
            else:
                skipped.append(t)
        except ValueError:
            errors.append(t)

    if not added and not skipped:
        await update.message.reply_text(
            "⚠️ Ни одно время не распознано.\nВведи в формате: *10:00, 11:30*",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADMIN_WAIT_TIME

    parts = []
    if added:
        parts.append("✅ *Добавлены:*\n" + "\n".join(f"  🕐 {t}" for t in added))
    if skipped:
        parts.append("⚠️ *Уже были:*\n" + "\n".join(f"  · {t}" for t in skipped))
    if errors:
        parts.append("❌ *Не распознаны:*\n" + "\n".join(f"  · {t}" for t in errors))

    await update.message.reply_text(
        f"📅 *{fmt_date(d)}*\n\n" + "\n\n".join(parts) + "\n\nКлиенты видят эту дату! 🎉",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(True)
    )
    return ConversationHandler.END

async def admin_add_photo_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🖼 Отправь фото для портфолио.\n\n/cancel — отмена")
    return ADMIN_WAIT_PHOTO

async def admin_got_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["adm_photo_id"] = update.message.photo[-1].file_id
    await update.message.reply_text(
        "📝 Введи описание к фото\n_(или отправь *—* чтобы пропустить)_\n\n/cancel — отмена",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADMIN_WAIT_CAPTION

async def admin_got_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    add_portfolio(ctx.user_data.get("adm_photo_id"), "" if text == "—" else text)
    await update.message.reply_text("✅ Фото добавлено в портфолио! 🖼",
                                    reply_markup=main_menu_kb(True))
    return ConversationHandler.END

async def admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Нет доступа", show_alert=True)
        return
    action = q.data.split(":")[1]

    if action == "bookings":
        rows = get_all_bookings()
        if not rows:
            await q.edit_message_text("📋 Записей пока нет.")
            return
        text = "📋 *Все предстоящие записи:*\n\n"
        for _, d, t, name, uname, phone in rows:
            text += f"📅 {fmt_date(d)} | 🕐 {t}\n👤 {name or '—'} | 📞 {phone or '—'}\n🆔 @{uname or '—'}\n\n"
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

    elif action == "reschedule":
        rows = get_all_bookings()
        if not rows:
            await q.edit_message_text("📋 Записей пока нет.")
            return
        kb = [[InlineKeyboardButton(
            f"👤 {name or '?'} | 📅 {fmt_date(d)} {t}",
            callback_data=f"adm_rs_pick:{sid}"
        )] for sid, d, t, name, uname, phone in rows]
        kb.append([InlineKeyboardButton("❌ Закрыть", callback_data="adm_close")])
        await q.edit_message_text(
            "🔄 *Перенести запись клиента*\n\nВыбери запись:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif action == "del_slot":
        slots = get_all_slots_admin()
        if not slots:
            await q.edit_message_text("Слотов нет.")
            return
        kb = [[InlineKeyboardButton(
            f"{'🔴' if b else '🟢'} {fmt_date(d)} {t}",
            callback_data=f"adm_del:{sid}"
        )] for sid, d, t, b in slots]
        kb.append([InlineKeyboardButton("❌ Закрыть", callback_data="adm_close")])
        await q.edit_message_text("🗑 Выбери слот для удаления:\n🟢 свободен | 🔴 занят",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "edit_price":
        pass  # handled by ConversationHandler

    elif action == "add_month":
        pass  # handled by ConversationHandler

    elif action == "del_photo":
        items = get_portfolio()
        if not items:
            await q.edit_message_text("Портфолио пусто.")
            return
        kb = [[InlineKeyboardButton(f"🗑 #{pid} {(cap or '')[:30]}",
                                    callback_data=f"adm_delpic:{pid}")]
              for pid, _, cap in items]
        kb.append([InlineKeyboardButton("❌ Закрыть", callback_data="adm_close")])
        await q.edit_message_text("Выбери фото для удаления:", reply_markup=InlineKeyboardMarkup(kb))

async def admin_del_slot_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "adm_close":
        await q.edit_message_text("Закрыто.")
        return
    delete_slot(int(q.data.split(":")[1]))
    await q.edit_message_text("✅ Слот удалён.")

async def admin_del_pic_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    del_portfolio(int(q.data.split(":")[1]))
    await q.edit_message_text("✅ Фото удалено.")

# ══════════════════════════════════════════════════════════════
#  РОУТЕР
# ══════════════════════════════════════════════════════════════
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text
    if   t == "💰 Прайс":            await show_price(update, ctx)
    elif t == "🖼 Портфолио":        await show_portfolio(update, ctx)
    elif t == "📋 Мои записи":        await my_bookings(update, ctx)
    elif t == "⚙️ Панель мастера":    await admin_panel(update, ctx)

# ══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════
def main():
    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    booking_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📅 Записаться$"), booking_start)],
        states={
            BOOK_TIME: [
                CallbackQueryHandler(calendar_navigate, pattern="^cal_nav:"),
                CallbackQueryHandler(calendar_busy,     pattern="^cal_busy$"),
                CallbackQueryHandler(calendar_ignore,   pattern="^cal_ignore$"),
                CallbackQueryHandler(calendar_pick,     pattern="^cal_pick:"),
                CallbackQueryHandler(back_to_calendar,  pattern="^back_calendar$"),
                CallbackQueryHandler(booking_time_pick, pattern="^slot:"),
                CallbackQueryHandler(booking_cancel,    pattern="^cal_cancel$"),
            ],
            BOOK_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, booking_contact),
                CallbackQueryHandler(booking_cancel, pattern="^cal_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("start",  cmd_start),
            CommandHandler("cancel", cmd_cancel),
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
        allow_reentry=True,
        conversation_timeout=300,  # 5 минут — сброс если клиент бросил
    )

    admin_slot_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_slot_start, pattern="^adm:add_slot$")],
        states={
            ADMIN_WAIT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_got_date)],
            ADMIN_WAIT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_got_time)],
        },
        fallbacks=[
            CommandHandler("start",  cmd_start),
            CommandHandler("cancel", cmd_cancel),
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
    )

    admin_photo_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_photo_start, pattern="^adm:add_photo$")],
        states={
            ADMIN_WAIT_PHOTO:   [MessageHandler(filters.PHOTO, admin_got_photo)],
            ADMIN_WAIT_CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_got_caption)],
        },
        fallbacks=[
            CommandHandler("start",  cmd_start),
            CommandHandler("cancel", cmd_cancel),
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
    )

    admin_month_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_month_start, pattern="^adm:add_month$")],
        states={
            ADMIN_WAIT_MONTH_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_got_month_date)],
            ADMIN_WAIT_MONTH_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_got_month_time)],
        },
        fallbacks=[
            CommandHandler("start",  cmd_start),
            CommandHandler("cancel", cmd_cancel),
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
    )

    admin_price_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_edit_price_start, pattern="^adm:edit_price$")],
        states={
            ADMIN_WAIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_got_price)],
        },
        fallbacks=[
            CommandHandler("start",  cmd_start),
            CommandHandler("cancel", cmd_cancel),
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
    )

    reschedule_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(my_reschedule_cb, pattern="^myreschedule:")],
        states={
            RESCHEDULE_PICK: [
                CallbackQueryHandler(reschedule_navigate,     pattern="^cal_nav:"),
                CallbackQueryHandler(calendar_busy,           pattern="^cal_busy$"),
                CallbackQueryHandler(calendar_ignore,         pattern="^cal_ignore$"),
                CallbackQueryHandler(reschedule_pick_date,    pattern="^cal_pick:"),
                CallbackQueryHandler(reschedule_back_calendar,pattern="^rback_calendar$"),
                CallbackQueryHandler(reschedule_pick_time,    pattern="^rslot:"),
            ],
        },
        fallbacks=[
            CommandHandler("start",  cmd_start),
            CommandHandler("cancel", cmd_cancel),
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
    )

    admin_rs_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_rs_pick_slot, pattern="^adm_rs_pick:")],
        states={
            ADMIN_RESCHEDULE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_rs_got_date)],
            ADMIN_RESCHEDULE_TIME: [
                CallbackQueryHandler(admin_rs_got_time, pattern="^adm_rs_time:|^adm_rs_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("start",  cmd_start),
            CommandHandler("cancel", cmd_cancel),
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("slots",   cmd_slots))
    app.add_handler(CommandHandler("addmonth", cmd_addmonth))
    app.add_handler(CommandHandler("addslot", cmd_addslot))
    app.add_handler(admin_rs_conv)
    app.add_handler(reschedule_conv)
    app.add_handler(booking_conv)
    app.add_handler(admin_slot_conv)
    app.add_handler(admin_photo_conv)
    app.add_handler(admin_price_conv)
    app.add_handler(admin_month_conv)
    app.add_handler(CallbackQueryHandler(admin_callback,    pattern="^adm:"))
    app.add_handler(CallbackQueryHandler(admin_del_slot_cb, pattern="^adm_del:|^adm_close$"))
    app.add_handler(CallbackQueryHandler(admin_del_pic_cb,  pattern="^adm_delpic:"))
    app.add_handler(CallbackQueryHandler(my_cancel_cb,      pattern="^mycancel:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # Напоминания клиентам каждые 10 минут
    app.job_queue.run_repeating(send_reminders, interval=600, first=10)

    logger.info("Бот запущен на Railway! 🚀")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
