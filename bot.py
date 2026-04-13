import os
import re
import io
import threading
import logging
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
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

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "8522649443:AAErAdlER36lmtF-f5KQngS_S9PI_4v7f54")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://zykqqqtjedsvegtvrapz.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inp5a3FxcXRqZWRzdmVndHZyYXB6Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYwNjMxNTksImV4cCI6MjA5MTYzOTE1OX0.h93e9I9jN3iPlqSl1lVyfvLYbzpY_NeJsEWk26tLOZs")
PORT         = int(os.environ.get("PORT", 10000))

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# States
WAITING_EMAIL    = 1
WAITING_PASSWORD = 2
WAITING_ADD_DESC = 3
WAITING_ADD_AMT  = 4
WAITING_ADD_CAT  = 5

sessions = {}

def fmt(v):
    if v is None: return "0"
    v = float(v)
    if abs(v) >= 1_000_000: return f"{v/1_000_000:.1f}M"
    if abs(v) >= 1_000: return f"{v/1_000:.0f}K"
    return f"{v:,.0f}"

def today_str():
    return date.today().isoformat()

def get_session(tid):
    return sessions.get(str(tid))

def set_session(tid, data):
    sessions[str(tid)] = data

async def get_user_company(user_id):
    res = sb.from_("companies").select("*").eq("owner_id", user_id).order("created_at").limit(1).execute()
    return res.data[0] if res.data else None

async def auth_user(email, password):
    try:
        res = sb.auth.sign_in_with_password({"email": email, "password": password})
        return res.user, None
    except Exception as e:
        return None, str(e)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    sess = get_session(tid)
    if sess:
        await show_main_menu(update, context, sess)
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 Добро пожаловать в *Hisobchi.uz Bot*!\n\n"
        "Введите ваш email с сайта:",
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
    ]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    msg = f"✅ Вошли как *{sess['email']}*\n🏢 *{name}*\n\nВыберите действие:"
    if update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown", reply_markup=markup)
    else:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=markup)

async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["email"] = update.message.text.strip().lower()
    await update.message.reply_text("🔑 Введите пароль:")
    return WAITING_PASSWORD

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    email = context.user_data.get("email", "")
    tid = update.effective_user.id
    await update.message.reply_text("⏳ Проверяем...")
    user, error = await auth_user(email, password)
    if error or not user:
        await update.message.reply_text("❌ Неверный email или пароль.\n\nВведите email заново:")
        return WAITING_EMAIL
    company = await get_user_company(user.id)
    if not company:
        await update.message.reply_text("❌ Компания не найдена. Зарегистрируйтесь на сайте.")
        return ConversationHandler.END
    set_session(tid, {
        "user_id": user.id, "email": email,
        "company_id": company["id"], "company_name": company["name"],
    })
    await show_main_menu(update, context, get_session(tid))
    return ConversationHandler.END

async def today_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(update.effective_user.id)
    if not sess: await update.message.reply_text("❗ Войдите — /start"); return
    today = today_str()
    res = sb.from_("transactions").select("*").eq("company_id", sess["company_id"]).gte("date", today).lte("date", today).execute()
    txs = res.data or []
    income  = sum(float(t.get("amount_uzs") or t.get("amount", 0)) for t in txs if float(t.get("amount_uzs") or t.get("amount", 0)) > 0)
    expense = sum(abs(float(t.get("amount_uzs") or t.get("amount", 0))) for t in txs if float(t.get("amount_uzs") or t.get("amount", 0)) < 0)
    profit  = income - expense
    all_res = sb.from_("transactions").select("amount_uzs,amount").eq("company_id", sess["company_id"]).execute()
    balance = sum(float(t.get("amount_uzs") or t.get("amount", 0)) for t in (all_res.data or []))
    ep = "🟢" if profit >= 0 else "🔴"
    await update.message.reply_text(
        f"📊 *Сводка за {today}*\n🏢 {sess['company_name']}\n\n"
        f"💚 Приход:  *{fmt(income)} сум*\n"
        f"❤️ Расход:  *{fmt(expense)} сум*\n"
        f"{ep} Прибыль: *{fmt(profit)} сум*\n\n"
        f"💰 Остаток: *{fmt(balance)} сум*\n"
        f"📝 Операций сегодня: {len(txs)}",
        parse_mode="Markdown"
    )

async def recent_ops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(update.effective_user.id)
    if not sess: await update.message.reply_text("❗ Войдите — /start"); return
    res = sb.from_("transactions").select("*").eq("company_id", sess["company_id"]).order("date", desc=True).limit(10).execute()
    txs = res.data or []
    if not txs: await update.message.reply_text("📭 Операций пока нет."); return
    lines = ["📋 *Последние 10 операций:*\n"]
    for t in txs:
        amt = float(t.get("amount_uzs") or t.get("amount", 0))
        sign = "➕" if amt > 0 else "➖"
        desc = t.get("desc") or "—"
        lines.append(f"{sign} *{fmt(abs(amt))}* — {desc}\n_{t.get('date','')}_ | {t.get('category','')}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def start_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(update.effective_user.id)
    if not sess: await update.message.reply_text("❗ Войдите — /start"); return ConversationHandler.END
    sess["add_type"] = "income"; set_session(update.effective_user.id, sess)
    await update.message.reply_text("💚 *Добавить приход*\n\nВведите описание:", parse_mode="Markdown")
    return WAITING_ADD_DESC

async def start_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(update.effective_user.id)
    if not sess: await update.message.reply_text("❗ Войдите — /start"); return ConversationHandler.END
    sess["add_type"] = "expense"; set_session(update.effective_user.id, sess)
    await update.message.reply_text("❤️ *Добавить расход*\n\nВведите описание:", parse_mode="Markdown")
    return WAITING_ADD_DESC

async def handle_add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add_desc"] = update.message.text.strip()
    await update.message.reply_text("💰 Введите сумму (например: 500000):")
    return WAITING_ADD_AMT

async def handle_add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(" ", "").replace(",", ""))
    except:
        await update.message.reply_text("❌ Введите число, например: 500000"); return WAITING_ADD_AMT
    context.user_data["add_amount"] = amount
    keyboard = [
        [InlineKeyboardButton("🛒 Выручка от продаж", callback_data="cat_Выручка от продаж")],
        [InlineKeyboardButton("🔧 Услуги", callback_data="cat_Выручка от услуг")],
        [InlineKeyboardButton("💼 Зарплата", callback_data="cat_Зарплата")],
        [InlineKeyboardButton("🏠 Аренда", callback_data="cat_Аренда")],
        [InlineKeyboardButton("📦 Себестоимость", callback_data="cat_Себестоимость товара")],
        [InlineKeyboardButton("📣 Маркетинг", callback_data="cat_Маркетинг")],
        [InlineKeyboardButton("🔄 Прочее", callback_data="cat_Прочее")],
    ]
    await update.message.reply_text("📂 Выберите статью:", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAITING_ADD_CAT

async def handle_category_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data["add_cat"] = update.callback_query.data.replace("cat_", "")
    await save_operation(update, context)
    return ConversationHandler.END

async def handle_add_cat_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add_cat"] = update.message.text.strip()
    await save_operation(update, context)
    return ConversationHandler.END

async def save_operation(update, context):
    tid = update.effective_user.id
    sess = get_session(tid)
    if not sess: return
    desc   = context.user_data.get("add_desc", "—")
    amount = float(context.user_data.get("add_amount", 0))
    cat    = context.user_data.get("add_cat", "Прочее")
    amount = abs(amount) if sess.get("add_type") == "income" else -abs(amount)
    row = {
        "id": f"tg_{tid}_{int(datetime.now().timestamp())}",
        "company_id": sess["company_id"], "user_id": sess["user_id"],
        "date": today_str(), "desc": desc, "amount": amount,
        "currency": "UZS", "amount_uzs": amount, "category": cat, "type": "PNL",
    }
    try:
        sb.from_("transactions").insert(row).execute()
        sign = "➕" if amount > 0 else "➖"
        text = f"✅ *Сохранено!*\n\n{sign} *{fmt(abs(amount))} сум*\n📝 {desc}\n📂 {cat}"
    except Exception as e:
        text = f"❌ Ошибка: {e}"
    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text(text, parse_mode="Markdown")

async def handle_quick_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(update.effective_user.id)
    if not sess: return
    text = update.message.text.strip()
    match = re.match(r'^([+\-])\s*([\d\s,]+)\s+(.+)$', text)
    if not match: return
    sign_char, amt_str, desc = match.groups()
    try:
        amount = float(amt_str.replace(" ", "").replace(",", ""))
    except:
        return
    amount = abs(amount) if sign_char == "+" else -abs(amount)
    cat = "Прочее"
    for kc in ["Выручка от продаж", "Выручка от услуг", "Зарплата", "Аренда", "Себестоимость", "Маркетинг", "Налоги"]:
        if kc.lower() in desc.lower(): cat = kc; break
    tid = update.effective_user.id
    row = {
        "id": f"tg_{tid}_{int(datetime.now().timestamp())}",
        "company_id": sess["company_id"], "user_id": sess["user_id"],
        "date": today_str(), "desc": desc, "amount": amount,
        "currency": "UZS", "amount_uzs": amount, "category": cat, "type": "PNL",
    }
    try:
        sb.from_("transactions").insert(row).execute()
        sign = "➕" if amount > 0 else "➖"
        await update.message.reply_text(f"✅ {sign} *{fmt(abs(amount))} сум* — {desc}\n📂 {cat}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def handle_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(update.effective_user.id)
    if not sess: await update.message.reply_text("❗ Войдите — /start"); return
    doc = update.message.document
    if not doc or not (doc.file_name or "").endswith((".xlsx", ".xls")):
        await update.message.reply_text("📎 Прикрепите .xlsx файл\n\nФормат: `Дата | Описание | Приход | Расход | Статья`", parse_mode="Markdown"); return
    await update.message.reply_text("⏳ Обрабатываем...")
    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(bytes(file_bytes)))
        rows = list(wb.active.iter_rows(values_only=True))
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}"); return
    start = 1 if rows and any(str(c or "").lower() in ["дата","date"] for c in rows[0]) else 0
    tid = update.effective_user.id
    batch, added = [], 0
    for i, row in enumerate(rows[start:]):
        if not row or len(row) < 3: continue
        try:
            dv = str(row[0] or "").strip()
            desc = str(row[1] or "—").strip()
            inc = float(str(row[2] or "0").replace(" ","").replace(",","") or "0")
            exp = float(str(row[3] or "0").replace(" ","").replace(",","") or "0") if len(row) > 3 else 0
            cat = str(row[4] or "Прочее").strip() if len(row) > 4 else "Прочее"
            if not dv or (not inc and not exp): continue
            amount = inc if inc else -exp
            batch.append({"id": f"xl_{tid}_{i}_{int(datetime.now().timestamp())}", "company_id": sess["company_id"],
                "user_id": sess["user_id"], "date": dv[:10], "desc": desc, "amount": amount,
                "currency": "UZS", "amount_uzs": amount, "category": cat, "type": "PNL"})
            added += 1
        except: pass
    for i in range(0, len(batch), 100):
        sb.from_("transactions").insert(batch[i:i+100]).execute()
    await update.message.reply_text(f"✅ *Импорт завершён!*\n📥 Добавлено: *{added}* операций", parse_mode="Markdown")

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    sess = get_session(update.effective_user.id)
    if text == "📊 Сводка за сегодня": await today_summary(update, context)
    elif text == "📋 Последние операции": await recent_ops(update, context)
    elif text == "➕ Приход": return await start_income(update, context)
    elif text == "➖ Расход": return await start_expense(update, context)
    elif text == "📥 Импорт Excel":
        await update.message.reply_text("📎 Прикрепите .xlsx файл\n\nФормат колонок:\n`Дата | Описание | Приход | Расход | Статья`", parse_mode="Markdown")
    elif not sess:
        await update.message.reply_text("❗ Войдите — /start")

# ── Simple HTTP server for Render health check ──
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Hisobchi Bot is running!")
    def log_message(self, *args): pass

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

def main():
    # Start health server in background thread
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    logger.info(f"Health server running on port {PORT}")

    app = Application.builder().token(BOT_TOKEN).build()

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_EMAIL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_email)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )
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
    app.add_handler(CallbackQueryHandler(handle_category_button, pattern="^cat_"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_excel))
    app.add_handler(MessageHandler(filters.Regex(r'^[+\-]\s*\d'), handle_quick_add))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    logger.info("🤖 Hisobchi Bot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
