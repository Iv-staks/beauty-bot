import os
import sqlite3
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

def build_calendar(year: int, month: int, free_dates: set) -> InlineKeyboardMarkup:
    today = date.today()
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
DB = "beauty_bot.db"

def db_init():
    with sqlite3.connect(DB) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            booked INTEGER DEFAULT 0,
            user_id INTEGER, username TEXT, name TEXT, phone TEXT
        );
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            caption TEXT
        );
        """)

def get_free_dates():
    today = date.today().strftime("%Y-%m-%d")
    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT DISTINCT date FROM slots WHERE booked=0 AND date>=?", (today,)
        ).fetchall()
    return {r[0] for r in rows}

def get_free_slots(d):
    with sqlite3.connect(DB) as c:
        return c.execute(
            "SELECT id, time FROM slots WHERE date=? AND booked=0 ORDER BY time", (d,)
        ).fetchall()

def add_slot(d, t):
    with sqlite3.connect(DB) as c:
        exists = c.execute(
            "SELECT id FROM slots WHERE date=? AND time=?", (d, t)
        ).fetchone()
        if not exists:
            c.execute("INSERT INTO slots(date,time) VALUES(?,?)", (d, t))
            return True
    return False

def book_slot(slot_id, user_id, username, name, phone):
    with sqlite3.connect(DB) as c:
        c.execute(
            "UPDATE slots SET booked=1,user_id=?,username=?,name=?,phone=? WHERE id=?",
            (user_id, username, name, phone, slot_id)
        )

def cancel_booking(slot_id):
    with sqlite3.connect(DB) as c:
        c.execute(
            "UPDATE slots SET booked=0,user_id=NULL,username=NULL,name=NULL,phone=NULL WHERE id=?",
            (slot_id,)
        )

def get_user_bookings(user_id):
    today = date.today().strftime("%Y-%m-%d")
    with sqlite3.connect(DB) as c:
        return c.execute(
            "SELECT id,date,time FROM slots WHERE user_id=? AND date>=? ORDER BY date,time",
            (user_id, today)
        ).fetchall()

def get_all_bookings():
    today = date.today().strftime("%Y-%m-%d")
    with sqlite3.connect(DB) as c:
        return c.execute(
            "SELECT id,date,time,name,username,phone FROM slots WHERE booked=1 AND date>=? ORDER BY date,time",
            (today,)
        ).fetchall()

def get_all_slots_admin():
    today = date.today().strftime("%Y-%m-%d")
    with sqlite3.connect(DB) as c:
        return c.execute(
            "SELECT id,date,time,booked FROM slots WHERE date>=? ORDER BY date,time", (today,)
        ).fetchall()

def delete_slot(slot_id):
    with sqlite3.connect(DB) as c:
        c.execute("DELETE FROM slots WHERE id=?", (slot_id,))

def get_portfolio():
    with sqlite3.connect(DB) as c:
        return c.execute("SELECT id,file_id,caption FROM portfolio ORDER BY id").fetchall()

def add_portfolio(file_id, caption):
    with sqlite3.connect(DB) as c:
        c.execute("INSERT INTO portfolio(file_id,caption) VALUES(?,?)", (file_id, caption))

def del_portfolio(pid):
    with sqlite3.connect(DB) as c:
        c.execute("DELETE FROM portfolio WHERE id=?", (pid,))

# ══════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOOK_TIME, BOOK_CONFIRM = range(2)
ADMIN_WAIT_DATE, ADMIN_WAIT_TIME, ADMIN_WAIT_PHOTO, ADMIN_WAIT_CAPTION = range(4, 8)

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
        [KeyboardButton("📋 Мои записи")],
    ]
    if admin:
        kb.append([KeyboardButton("⚙️ Панель мастера")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def is_admin(uid):
    return uid == ADMIN_ID

# ══════════════════════════════════════════════════════════════
#  /start и /cancel
# ══════════════════════════════════════════════════════════════
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
#  ПОРТФОЛИО
# ══════════════════════════════════════════════════════════════
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
    free = get_free_dates()
    if not free:
        await update.message.reply_text(
            "😔 Свободных слотов пока нет.\n"
            "Загляни позже или напиши мне напрямую!"
        )
        return ConversationHandler.END
    now = datetime.now()
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
    kb = [[InlineKeyboardButton(f"🕐 {t}", callback_data=f"slot:{sid}:{t}")] for sid, t in slots]
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
    parts = q.data.split(":")
    ctx.user_data["book_slot_id"] = int(parts[1])
    ctx.user_data["book_time"]    = parts[2]
    await q.edit_message_text(
        f"📋 *Почти готово!*\n\n"
        f"📅 {fmt_date(ctx.user_data['book_date'])}\n"
        f"🕐 {parts[2]}\n\n"
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
        kb.append([InlineKeyboardButton(f"❌ Отменить {fmt_date(d)} {t}", callback_data=f"mycancel:{rid}")])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))

async def my_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cancel_booking(int(q.data.split(":")[1]))
    await q.edit_message_text("✅ Запись отменена. Слот снова свободен.")

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
            [InlineKeyboardButton("➕ Добавить слот",        callback_data="adm:add_slot")],
            [InlineKeyboardButton("🗑 Удалить слот",         callback_data="adm:del_slot")],
            [InlineKeyboardButton("📋 Все записи",           callback_data="adm:bookings")],
            [InlineKeyboardButton("🖼 Добавить в портфолио", callback_data="adm:add_photo")],
            [InlineKeyboardButton("🗑 Удалить из портфолио", callback_data="adm:del_photo")],
        ])
    )

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
        if dt.date() < date.today():
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
    for t in [x.strip() for x in text.split(",")]:
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
    if   t == "🖼 Портфолио":        await show_portfolio(update, ctx)
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
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("slots",   cmd_slots))
    app.add_handler(CommandHandler("addslot", cmd_addslot))
    app.add_handler(booking_conv)
    app.add_handler(admin_slot_conv)
    app.add_handler(admin_photo_conv)
    app.add_handler(CallbackQueryHandler(admin_callback,    pattern="^adm:"))
    app.add_handler(CallbackQueryHandler(admin_del_slot_cb, pattern="^adm_del:|^adm_close$"))
    app.add_handler(CallbackQueryHandler(admin_del_pic_cb,  pattern="^adm_delpic:"))
    app.add_handler(CallbackQueryHandler(my_cancel_cb,      pattern="^mycancel:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("Бот запущен на Railway! 🚀")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
