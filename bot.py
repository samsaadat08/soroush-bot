import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import libsql_client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- سرور سبک برای باز نگه داشتن پورت در پلن رایگان Render ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Soroush Bot is alive!")

    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- متغیرهای محیطی ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

def get_db():
    return libsql_client.create_client_sync(url=TURSO_URL, auth_token=TURSO_TOKEN)

def init_db():
    client = get_db()
    client.execute("""
        CREATE TABLE IF NOT EXISTS therapists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            clinic_name TEXT,
            district TEXT,
            approach TEXT DEFAULT 'نامشخص'
        );
    """)
    client.execute("""
        CREATE TABLE IF NOT EXISTS experiences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            therapist_id INTEGER NOT NULL,
            user_telegram_id INTEGER NOT NULL,
            issue_category TEXT NOT NULL,
            sessions_count TEXT NOT NULL,
            experience_text TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (therapist_id) REFERENCES therapists(id)
        );
    """)
    client.close()

(
    SEARCH_THERAPIST,
    SELECT_OR_NEW,
    NEW_CLINIC,
    NEW_DISTRICT,
    NEW_APPROACH,
    SELECT_ISSUE,
    SELECT_SESSIONS,
    WRITE_EXPERIENCE,
) = range(8)

DISTRICTS = ["احمدآباد", "سجاد", "وکیل‌آباد", "کوهسنگی", "هاشمیه", "سایر مناطق مشهد"]
ISSUES = ["اضطراب و افسردگی", "زوج و خانواده", "توسعه فردی / معناجویی", "کودک و نوجوان", "سایر"]
APPROACHES = ["انسان‌گرا / وجودی", "شناختی-رفتاری (CBT)", "روانکاوی / پویشی", "طرح‌واره‌درمانی", "نمی‌دانم"]
SESSIONS = ["۱ تا ۳ جلسه", "۴ تا ۱۰ جلسه", "بیش از ۱۰ جلسه"]

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 جستجوی درمانگر در مشهد", callback_data="browse_start")],
        [InlineKeyboardButton("✍️ معرفی درمانگر و ثبت تجربه", callback_data="submit_start")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "سلام! به **سروش** خوش آمدید 🌱\n\n"
        "در فرهنگ ایرانی، «سروش» نماد **شنیدن، آگاهی و راهنمایی در مسیر درست** است. "
        "این ربات هم دقیقاً با همین هدف ساخته شده؛ فضایی برای شنیدن تجربه‌های واقعی و مثبت مراجعان در مشهد، "
        "تا انتخاب درمانگر برای شما شفاف‌تر و آگاهانه‌تر باشد.\n\n"
        "چطور می‌توانم کمکتان کنم؟"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    return ConversationHandler.END

async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(
        "نام یا نام خانوادگی درمانگر را بنویسید (بدون پیشوند دکتر):\n"
        "مثال: علی رضایی"
    )
    return SEARCH_THERAPIST

async def search_therapist_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name_query = update.message.text.strip().replace("دکتر ", "").replace("دکتر", "")
    context.user_data["temp_name"] = name_query

    client = get_db()
    res = client.execute(
        "SELECT id, full_name, clinic_name, district FROM therapists WHERE full_name LIKE ?",
        [f"%{name_query}%"],
    )
    client.close()

    keyboard = []
    for row in res.rows:
        t_id, f_name, clinic, dist = row
        label = f"{f_name} ({clinic or 'مطب شخصی'} - {dist or 'مشهد'})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"select_t_{t_id}")])

    keyboard.append([InlineKeyboardButton(f"➕ ثبت درمانگر جدید: «{name_query}»", callback_data="new_therapist")])
    keyboard.append([InlineKeyboardButton("🔙 انصراف", callback_data="cancel_flow")])

    await update.message.reply_text(
        "اگر درمانگر مورد نظر شما در لیست زیر هست انتخاب کنید، در غیر این صورت گزینه ثبت جدید را بزنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_OR_NEW

async def select_or_new_therapist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel_flow":
        return await start(update, context)

    if data.startswith("select_t_"):
        t_id = int(data.split("_")[2])
        context.user_data["therapist_id"] = t_id
        context.user_data["is_new"] = False
        return await ask_issue(query)

    if data == "new_therapist":
        context.user_data["is_new"] = True
        await query.edit_message_text("نام کلینیک یا مرکز مشاوره‌ای که ایشان فعالیت دارند را بنویسید (یا بنویسید مطب شخصی):")
        return NEW_CLINIC

async def get_new_clinic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["clinic_name"] = update.message.text.strip()
    keyboard = [[InlineKeyboardButton(d, callback_data=f"dist_{d}")] for d in DISTRICTS]
    await update.message.reply_text("محدوده کلینیک یا مطب در مشهد کجاست؟", reply_markup=InlineKeyboardMarkup(keyboard))
    return NEW_DISTRICT

async def get_new_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["district"] = query.data.replace("dist_", "")
    keyboard = [[InlineKeyboardButton(a, callback_data=f"app_{a}")] for a in APPROACHES]
    await query.edit_message_text("رویکرد درمانی ایشان چیست؟ (اگر مطمئن نیستید گزینه آخر را بزنید)", reply_markup=InlineKeyboardMarkup(keyboard))
    return NEW_APPROACH

async def get_new_approach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["approach"] = query.data.replace("app_", "")
    return await ask_issue(query)

async def ask_issue(query):
    keyboard = [[InlineKeyboardButton(i, callback_data=f"issue_{i}")] for i in ISSUES]
    await query.edit_message_text("بیشتر برای چه موضوعی به ایشان مراجعه کردید؟", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_ISSUE

async def get_issue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["issue"] = query.data.replace("issue_", "")
    keyboard = [[InlineKeyboardButton(s, callback_data=f"sess_{s}")] for s in SESSIONS]
    await query.edit_message_text("تا الان حدوداً چند جلسه با ایشان داشته‌اید؟", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_SESSIONS

async def get_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["sessions"] = query.data.replace("sess_", "")
    await query.edit_message_text(
        "در چند خط تجربه‌ی خودتان را بنویسید (مثلاً چه ویژگی یا روشی در جلسات ایشان برای شما مفید و راهگشا بود؟):\n\n"
        "⚠️ لطفاً از نوشتن اطلاعات خصوصی یا تشخیص‌های پزشکی خودداری کنید."
    )
    return WRITE_EXPERIENCE

async def save_experience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    exp_text = update.message.text.strip()
    user_id = update.effective_user.id
    data = context.user_data

    client = get_db()
    if data.get("is_new"):
        res = client.execute(
            "INSERT INTO therapists (full_name, clinic_name, district, approach) VALUES (?, ?, ?, ?) RETURNING id",
            [data["temp_name"], data["clinic_name"], data["district"], data["approach"]],
        )
        t_id = res.rows[0][0]
        t_info = f"{data['temp_name']} ({data['clinic_name']} - {data['district']}) | رویکرد: {data['approach']}"
    else:
        t_id = data["therapist_id"]
        res = client.execute("SELECT full_name, clinic_name, district, approach FROM therapists WHERE id = ?", [t_id])
        row = res.rows[0]
        t_info = f"{row[0]} ({row[1]} - {row[2]}) | رویکرد: {row[3]}"

    exp_res = client.execute(
        """INSERT INTO experiences (therapist_id, user_telegram_id, issue_category, sessions_count, experience_text, status)
           VALUES (?, ?, ?, ?, ?, 'pending') RETURNING id""",
        [t_id, user_id, data["issue"], data["sessions"], exp_text],
    )
    exp_id = exp_res.rows[0][0]
    client.close()

    await update.message.reply_text(
        "✅ تجربه‌ی شما با موفقیت ثبت شد و پس از بررسی ادمین منتشر می‌شود. ممنون از مشارکت شما!",
        reply_markup=main_menu_keyboard(),
    )

    if ADMIN_ID:
        admin_msg = (
            f"📩 **تجربه جدید در انتظار تایید (کد #{exp_id})**\n\n"
            f"👤 **درمانگر:** {t_info}\n"
            f"📌 **موضوع:** {data['issue']}\n"
            f"🗓 **تعداد جلسات:** {data['sessions']}\n"
            f"🆔 **آیدی کاربر:** `{user_id}`\n\n"
            f"💬 **متن تجربه:**\n{exp_text}"
        )
        admin_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ تایید و انتشار", callback_data=f"adm_approve_{exp_id}"),
                InlineKeyboardButton("❌ رد کردن", callback_data=f"adm_reject_{exp_id}"),
            ]
        ])
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, reply_markup=admin_kb, parse_mode="Markdown")

    return ConversationHandler.END

async def admin_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return

    await query.answer()
    action, exp_id = query.data.split("_")[1], int(query.data.split("_")[2])
    new_status = "approved" if action == "approve" else "rejected"

    client = get_db()
    client.execute("UPDATE experiences SET status = ? WHERE id = ?", [new_status, exp_id])
    client.close()

    status_text = "✅ تایید و منتشر شد." if action == "approve" else "❌ رد شد."
    await query.edit_message_text(f"{query.message.text}\n\n---\nنتیجه بررسی: {status_text}")

async def browse_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(i, callback_data=f"filter_issue_{i}")] for i in ISSUES]
    keyboard.append([InlineKeyboardButton("📋 نمایش همه درمانگران تاییدشده", callback_data="filter_all")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")])

    await query.edit_message_text(
        "برای چه موضوعی دنبال درمانگر در مشهد هستید؟",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def show_therapists_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_main":
        return await start(update, context)

    client = get_db()
    if data == "filter_all":
        res = client.execute("""
            SELECT t.id, t.full_name, t.clinic_name, t.district, t.approach, COUNT(e.id) as exp_count
            FROM therapists t
            JOIN experiences e ON t.id = e.therapist_id
            WHERE e.status = 'approved'
            GROUP BY t.id
            ORDER BY exp_count DESC
            LIMIT 15
        """)
    else:
        issue = data.replace("filter_issue_", "")
        res = client.execute("""
            SELECT t.id, t.full_name, t.clinic_name, t.district, t.approach, COUNT(e.id) as exp_count
            FROM therapists t
            JOIN experiences e ON t.id = e.therapist_id
            WHERE e.status = 'approved' AND e.issue_category = ?
            GROUP BY t.id
            ORDER BY exp_count DESC
            LIMIT 15
        """, [issue])
    client.close()

    if not res.rows:
        await query.edit_message_text(
            "هنوز تجربه تاییدشده‌ای در این دسته‌بندی ثبت نشده است. شما می‌توانید اولین نفر باشید!",
            reply_markup=main_menu_keyboard(),
        )
        return

    keyboard = []
    for row in res.rows:
        t_id, f_name, clinic, dist, app, count = row
        btn_text = f"👤 {f_name} | {dist} ({count} تجربه)"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"view_t_{t_id}")])

    keyboard.append([InlineKeyboardButton("🔙 بازگشت به دسته‌بندی‌ها", callback_data="browse_start")])
    await query.edit_message_text(
        "درمانگران معرفی‌شده بر اساس تعداد تجربه‌های ثبت‌شده:\nبرای دیدن جزئیات و نظرات روی هر نام کلیک کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def view_therapist_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    t_id = int(query.data.replace("view_t_", ""))

    client = get_db()
    t_res = client.execute("SELECT full_name, clinic_name, district, approach FROM therapists WHERE id = ?", [t_id])
    e_res = client.execute(
        "SELECT issue_category, sessions_count, experience_text FROM experiences WHERE therapist_id = ? AND status = 'approved' ORDER BY id DESC LIMIT 5",
        [t_id],
    )
    client.close()

    if not t_res.rows:
        await query.edit_message_text("اطلاعات یافت نشد.", reply_markup=main_menu_keyboard())
        return

    f_name, clinic, dist, app = t_res.rows[0]
    msg = (
        f"👤 **نام درمانگر:** {f_name}\n"
        f"🏥 **مرکز/کلینیک:** {clinic} (محدوده {dist})\n"
        f"🧠 **رویکرد:** {app}\n"
        f"📊 **تعداد تجربه‌های ثبت‌شده:** {len(e_res.rows)} مورد\n"
        f"━━━━━━━━━━━━━━\n"
        f"💬 **تجربه‌ی مراجعان:**\n\n"
    )

    for idx, exp in enumerate(e_res.rows, 1):
        issue, sess, text = exp
        msg += f"**{idx}. موضوع: {issue}** ({sess})\n«{text}»\n\n"

    keyboard = [
        [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="browse_start")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main")],
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(submit_start, pattern="^submit_start$")],
        states={
            SEARCH_THERAPIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_therapist_name)],
            SELECT_OR_NEW: [CallbackQueryHandler(select_or_new_therapist)],
            NEW_CLINIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_clinic)],
            NEW_DISTRICT: [CallbackQueryHandler(get_new_district, pattern="^dist_")],
            NEW_APPROACH: [CallbackQueryHandler(get_new_approach, pattern="^app_")],
            SELECT_ISSUE: [CallbackQueryHandler(get_issue, pattern="^issue_")],
            SELECT_SESSIONS: [CallbackQueryHandler(get_sessions, pattern="^sess_")],
            WRITE_EXPERIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_experience)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(admin_moderation, pattern="^adm_(approve|reject)_"))
    app.add_handler(CallbackQueryHandler(browse_start, pattern="^browse_start$"))
    app.add_handler(CallbackQueryHandler(show_therapists_list, pattern="^(filter_|back_main)"))
    app.add_handler(CallbackQueryHandler(view_therapist_detail, pattern="^view_t_"))

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
