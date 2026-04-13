import os
import re
import io
import json
import logging
from datetime import datetime, date
from dotenv import load_dotenv

load_dotenv()

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)
from supabase import create_client, Client
import openpyxl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ──
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "https://zykqqqtjedsvegtvrapz.supabase.co")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inp5a3FxcXRqZWRzdmVndHZyYXB6Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYwNjMxNTksImV4cCI6MjA5MTYzOTE1OX0.h93e9I9jN3iPlqSl1lVyfvLYbzpY_NeJsEWk26tLOZs")

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ConversationHandler states
WAITING_EMAIL    = 1
WAITING_PASSWORD = 2
WAITING_ADD_DESC = 3
WAITING_ADD_AMT  = 4
WAITING_ADD_CAT  = 5

# ── User session (in-memory) ──
# { telegram_id: { user_id, email, company_id, company_name, add_type } }
sessions = {}

def fmt(v):
    """Format number nicely"""
    if v is None: return "0"
    v = float(v)
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"{v/1_000:.0f}K"
    return f"{v:,.0f}"

def today_str():
    return date.today().isoformat()

def get_session(telegram_id):
    return sessions.get(str(telegram_id))

def set_session(telegram_id, data):
    sessions[str(telegram_id)] = data

# ── Get user's company_id from Supabase ──
async def get_user_company(user_id: str):
    res = sb.from_("companies").select("*").eq("owner_id", user_id).order("created_at").limit(1).execute()
    if res.data:
        return res.data[0]
    return None

# ── Auth via Supabase ──
async def auth_user(email: str, password: str):
    try:
        res = sb.auth.sign_in_with_password({"email": email, "password": password})
        return res.user, None
    except Exception as e:
        return None, str(e)

# ═══════════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    sess = get_session(tid)

    if sess:
        await show_main_menu(update, context, sess)
    else:
        await update.message.reply_text(
            "👋 Добро пожаловать в *Hisobchi.uz Bot*!\n\n"
            "Для входа введите ваш email с сайта hisobchi.github.io/hisobchi",
            parse_mode="Markdown"
        )
        return WAITING_EMAIL

async def show_main_menu(update, context, sess):
    name = sess.get("company_name", "Компания")
    keyboard = [
        [KeyboardButton("📊 Сводка за сегодня")],
        [KeyboardButton("➕ Приход"), KeyboardButton("➖ Расход")],
        [KeyboardButton("📋 Последние операции")],
        [KeyboardButton("📥 Импорт Excel")],
        [KeyboardButton("⚙️ Сменить компанию")],
    ]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    msg = f"✅ Вы вошли как *{sess['email']}*\n🏢 Компания: *{name}*\n\nВыберите действие:"
    if update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown", reply_markup=markup)
    else:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=markup)

# ── Login flow ──
async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = update.message.text.strip().lower()
    context.user_data["email"] = email
    await update.message.reply_text("🔑 Введите пароль:")
    return WAITING_PASSWORD

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    email = context.user_data.get("email", "")
    tid = update.effective_user.id

    await update.message.reply_text("⏳ Проверяем...")

    user, error = await auth_user(email, password)
    if error or not user:
        await update.message.reply_text(
            "❌ Неверный email или пароль.\n\nПопробуйте снова — введите email:"
        )
        return WAITING_EMAIL

    # Get company
    company = await get_user_company(user.id)
    if not company:
        await update.message.reply_text("❌ Компания не найдена. Зарегистрируйтесь на сайте.")
        return ConversationHandler.END

    set_session(tid, {
        "user_id":      user.id,
        "email":        email,
        "company_id":   company["id"],
        "company_name": company["name"],
    })

    await show_main_menu(update, context, get_session(tid))
    return ConversationHandler.END

# ── Today summary ──
async def today_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    sess = get_session(tid)
    if not sess:
        await update.message.reply_text("❗ Войдите сначала — /start")
        return

    today = today_str()
    res = sb.from_("transactions").select("*")\
        .eq("company_id", sess["company_id"])\
        .gte("date", today)\
        .lte("date", today)\
        .execute()

    txs = res.data or []
    income  = sum(float(t.get("amount_uzs") or t.get("amount", 0)) for t in txs if float(t.get("amount_uzs") or t.get("amount", 0)) > 0)
    expense = sum(abs(float(t.get("amount_uzs") or t.get("amount", 0))) for t in txs if float(t.get("amount_uzs") or t.get("amount", 0)) < 0)
    profit  = income - expense

    # All time balance
    all_res = sb.from_("transactions").select("amount_uzs, amount")\
        .eq("company_id", sess["company_id"]).execute()
    all_txs = all_res.data or []
    balance = sum(float(t.get("amount_uzs") or t.get("amount", 0)) for t in all_txs)

    emoji_profit = "🟢" if profit >= 0 else "🔴"
    text = (
        f"📊 *Сводка за {today}*\n"
        f"🏢 {sess['company_name']}\n\n"
        f"💚 Приход:   *{fmt(income)} сум*\n"
        f"❤️ Расход:   *{fmt(expense)} сум*\n"
        f"{emoji_profit} Прибыль: *{fmt(profit)} сум*\n\n"
        f"💰 Остаток (всего): *{fmt(balance)} сум*\n\n"
        f"📝 Операций сегодня: {len(txs)}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ── Recent transactions ──
async def recent_ops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    sess = get_session(tid)
    if not sess:
        await update.message.reply_text("❗ Войдите сначала — /start")
        return

    res = sb.from_("transactions").select("*")\
        .eq("company_id", sess["company_id"])\
        .order("date", desc=True).order("created_at", desc=True)\
        .limit(10).execute()

    txs = res.data or []
    if not txs:
        await update.message.reply_text("📭 Операций пока нет.")
        return

    lines = ["📋 *Последние 10 операций:*\n"]
    for t in txs:
        amt = float(t.get("amount_uzs") or t.get("amount", 0))
        sign = "➕" if amt > 0 else "➖"
        desc = t.get("desc") or t.get("description") or "—"
        cat  = t.get("category") or ""
        lines.append(f"{sign} *{fmt(abs(amt))}* — {desc} [{cat}]\n_{t.get('date','')}_\n")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── Add operation flow ──
async def start_add(update: Update, context: ContextTypes.DEFAULT_TYPE, op_type: str):
    tid = update.effective_user.id
    sess = get_session(tid)
    if not sess:
        await update.message.reply_text("❗ Войдите сначала — /start")
        return ConversationHandler.END

    sess["add_type"] = op_type
    set_session(tid, sess)
    sign = "💚 приход" if op_type == "income" else "❤️ расход"
    await update.message.reply_text(
        f"Вносим {sign}.\n\n"
        f"Введите описание операции:\n"
        f"_(например: Продажа товара, Аренда офиса)_",
        parse_mode="Markdown"
    )
    return WAITING_ADD_DESC

async def start_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start_add(update, context, "income")

async def start_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start_add(update, context, "expense")

async def handle_add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add_desc"] = update.message.text.strip()
    op_type = get_session(update.effective_user.id).get("add_type", "income")
    sign = "приход" if op_type == "income" else "расход"
    await update.message.reply_text(
        f"💰 Введите сумму в сумах для операции «{context.user_data['add_desc']}»:\n"
        f"_(например: 500000)_",
        parse_mode="Markdown"
    )
    return WAITING_ADD_AMT

async def handle_add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "").replace(",", "")
    try:
        amount = float(text)
    except:
        await update.message.reply_text("❌ Неверная сумма. Введите число, например: 500000")
        return WAITING_ADD_AMT

    context.user_data["add_amount"] = amount

    # Quick category buttons
    keyboard = [
        [InlineKeyboardButton("🛒 Выручка от продаж", callback_data="cat_Выручка от продаж")],
        [InlineKeyboardButton("🔧 Услуги", callback_data="cat_Выручка от услуг")],
        [InlineKeyboardButton("💼 Зарплата", callback_data="cat_Зарплата")],
        [InlineKeyboardButton("🏠 Аренда", callback_data="cat_Аренда")],
        [InlineKeyboardButton("📦 Себестоимость", callback_data="cat_Себестоимость товара")],
        [InlineKeyboardButton("📣 Маркетинг", callback_data="cat_Маркетинг")],
        [InlineKeyboardButton("🔄 Прочее", callback_data="cat_Прочее")],
    ]
    await update.message.reply_text(
        "📂 Выберите статью или напишите свою:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_ADD_CAT

async def handle_category_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.replace("cat_", "")
    context.user_data["add_cat"] = cat
    await save_operation(update, context)
    return ConversationHandler.END

async def handle_add_cat_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add_cat"] = update.message.text.strip()
    await save_operation(update, context)
    return ConversationHandler.END

async def save_operation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    sess = get_session(tid)
    if not sess:
        return

    desc   = context.user_data.get("add_desc", "—")
    amount = float(context.user_data.get("add_amount", 0))
    cat    = context.user_data.get("add_cat", "Прочее")
    op_type = sess.get("add_type", "income")

    if op_type == "expense":
        amount = -abs(amount)
    else:
        amount = abs(amount)

    row = {
        "id":          f"tg_{tid}_{int(datetime.now().timestamp())}",
        "company_id":  sess["company_id"],
        "user_id":     sess["user_id"],
        "date":        today_str(),
        "desc":        desc,
        "amount":      amount,
        "currency":    "UZS",
        "amount_uzs":  amount,
        "category":    cat,
        "type":        "PNL",
    }

    try:
        sb.from_("transactions").insert(row).execute()
        sign = "➕" if amount > 0 else "➖"
        text = (
            f"✅ *Операция добавлена!*\n\n"
            f"{sign} *{fmt(abs(amount))} сум*\n"
            f"📝 {desc}\n"
            f"📂 {cat}\n"
            f"📅 {today_str()}"
        )
    except Exception as e:
        text = f"❌ Ошибка сохранения: {e}"

    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

# ── Quick text input: "+ 500000 Описание" or "- 300000 Описание" ──
async def handle_quick_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    sess = get_session(tid)
    if not sess:
        return

    text = update.message.text.strip()
    # Pattern: + 500000 Описание категория  OR  - 300000 Описание
    match = re.match(r'^([+\-])\s*([\d\s,]+)\s+(.+)$', text)
    if not match:
        return

    sign_char, amt_str, desc = match.groups()
    try:
        amount = float(amt_str.replace(" ", "").replace(",", ""))
    except:
        return

    if sign_char == "-":
        amount = -abs(amount)
    else:
        amount = abs(amount)

    # Try to extract category from end of desc
    cat = "Прочее"
    known_cats = ["Выручка от продаж", "Выручка от услуг", "Зарплата", "Аренда",
                  "Себестоимость", "Маркетинг", "Налоги", "Логистика"]
    for kc in known_cats:
        if kc.lower() in desc.lower():
            cat = kc
            break

    row = {
        "id":          f"tg_{tid}_{int(datetime.now().timestamp())}",
        "company_id":  sess["company_id"],
        "user_id":     sess["user_id"],
        "date":        today_str(),
        "desc":        desc,
        "amount":      amount,
        "currency":    "UZS",
        "amount_uzs":  amount,
        "category":    cat,
        "type":        "PNL",
    }

    try:
        sb.from_("transactions").insert(row).execute()
        sign_emoji = "➕" if amount > 0 else "➖"
        await update.message.reply_text(
            f"✅ {sign_emoji} *{fmt(abs(amount))} сум* — {desc}\n📂 {cat}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ── Excel import ──
async def handle_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    sess = get_session(tid)
    if not sess:
        await update.message.reply_text("❗ Войдите сначала — /start")
        return

    doc = update.message.document
    if not doc:
        await update.message.reply_text("📎 Прикрепите Excel файл (.xlsx или .csv)")
        return

    fname = doc.file_name or ""
    if not (fname.endswith(".xlsx") or fname.endswith(".xls") or fname.endswith(".csv")):
        await update.message.reply_text("❌ Поддерживаются только .xlsx и .csv файлы")
        return

    await update.message.reply_text("⏳ Обрабатываем файл...")

    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()

    try:
        wb = openpyxl.load_workbook(io.BytesIO(bytes(file_bytes)))
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка чтения файла: {e}")
        return

    # Detect header row
    start = 0
    if rows and any(str(c or "").lower() in ["дата", "date", "сана"] for c in rows[0]):
        start = 1

    added = 0
    errors = 0
    batch = []

    for row in rows[start:]:
        if not row or len(row) < 3:
            continue
        try:
            date_val = str(row[0] or "").strip()
            desc_val = str(row[1] or "—").strip()
            inc_val  = float(str(row[2] or "0").replace(" ", "").replace(",", "") or "0")
            exp_val  = float(str(row[3] or "0").replace(" ", "").replace(",", "") or "0") if len(row) > 3 else 0
            cat_val  = str(row[4] or "Прочее").strip() if len(row) > 4 else "Прочее"
            type_val = str(row[5] or "PNL").strip().upper() if len(row) > 5 else "PNL"

            if not date_val or (not inc_val and not exp_val):
                continue

            amount = inc_val if inc_val else -exp_val

            batch.append({
                "id":          f"xl_{tid}_{added}_{int(datetime.now().timestamp())}",
                "company_id":  sess["company_id"],
                "user_id":     sess["user_id"],
                "date":        date_val[:10],
                "desc":        desc_val,
                "amount":      amount,
                "currency":    "UZS",
                "amount_uzs":  amount,
                "category":    cat_val,
                "type":        type_val if type_val in ["PNL", "CF", "BAL"] else "PNL",
            })
            added += 1
        except:
            errors += 1

    if batch:
        # Insert in chunks of 100
        for i in range(0, len(batch), 100):
            try:
                sb.from_("transactions").insert(batch[i:i+100]).execute()
            except Exception as e:
                errors += len(batch[i:i+100])
                added  -= len(batch[i:i+100])

    await update.message.reply_text(
        f"✅ *Импорт завершён!*\n\n"
        f"📥 Добавлено: *{added}* операций\n"
        f"❌ Пропущено: {errors}\n\n"
        f"Формат файла:\n"
        f"`Дата | Описание | Приход | Расход | Статья | Тип`",
        parse_mode="Markdown"
    )

# ── Change company ──
async def change_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    sess = get_session(tid)
    if not sess:
        await update.message.reply_text("❗ Войдите сначала — /start")
        return

    res = sb.from_("companies").select("*").eq("owner_id", sess["user_id"]).execute()
    companies = res.data or []

    if len(companies) <= 1:
        await update.message.reply_text("У вас только одна компания.")
        return

    keyboard = [[InlineKeyboardButton(c["name"], callback_data=f"co_{c['id']}_{c['name']}")] for c in companies]
    await update.message.reply_text(
        "🏢 Выберите компанию:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_company_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = update.effective_user.id
    sess = get_session(tid)
    if not sess:
        return

    parts = query.data.split("_", 2)
    co_id   = parts[1]
    co_name = parts[2] if len(parts) > 2 else "Компания"

    sess["company_id"]   = co_id
    sess["company_name"] = co_name
    set_session(tid, sess)

    await query.message.reply_text(f"✅ Переключено на *{co_name}*", parse_mode="Markdown")
    await show_main_menu(update, context, sess)

# ── Handle menu buttons ──
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    tid  = update.effective_user.id
    sess = get_session(tid)

    if text == "📊 Сводка за сегодня":
        await today_summary(update, context)
    elif text == "📋 Последние операции":
        await recent_ops(update, context)
    elif text == "➕ Приход":
        await start_income(update, context)
        return WAITING_ADD_DESC
    elif text == "➖ Расход":
        await start_expense(update, context)
        return WAITING_ADD_DESC
    elif text == "⚙️ Сменить компанию":
        await change_company(update, context)
    elif text == "📥 Импорт Excel":
        await update.message.reply_text(
            "📎 Прикрепите Excel файл (.xlsx)\n\n"
            "Формат колонок:\n"
            "`Дата | Описание | Приход | Расход | Статья | Тип(PNL/CF/BAL)`",
            parse_mode="Markdown"
        )
    elif not sess:
        await update.message.reply_text("❗ Войдите сначала — /start")

# ── Main ──
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Login conversation
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_EMAIL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_email)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    # Add operation conversation
    add_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Приход$"), start_income),
            MessageHandler(filters.Regex("^➖ Расход$"), start_expense),
        ],
        states={
            WAITING_ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_desc)],
            WAITING_ADD_AMT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_amount)],
            WAITING_ADD_CAT:  [
                CallbackQueryHandler(handle_category_button, pattern="^cat_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_cat_text),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(login_conv)
    app.add_handler(add_conv)
    app.add_handler(CallbackQueryHandler(handle_company_select, pattern="^co_"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_excel))
    app.add_handler(MessageHandler(filters.Regex(r'^[+\-]\s*[\d]'), handle_quick_add))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    print("🤖 Hisobchi Bot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
