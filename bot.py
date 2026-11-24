import os
import asyncio
import logging
from aiohttp import web
import libsql_client
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------
# ۱. تنظیمات لاگ‌گیری (مخفی‌سازی کامل توکن ربات در لاگ‌های Render)
# ---------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# ۲. خواندن متغیرهای محیطی (Environment Variables) از Render
# ---------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL", "https://soroush-bot-w2q5.onrender.com"
)
PORT = int(os.environ.get("PORT", "8080"))

# ---------------------------------------------------------
# ۳. اتصال و راه‌اندازی دیتابیس Turso با libsql-client
# ---------------------------------------------------------
def get_db():
    db_url = TURSO_DATABASE_URL.replace("libsql://", "https://")
    return libsql_client.create_client_sync(
        url=db_url,
        auth_token=TURSO_AUTH_TOKEN,
    )

def init_db():
    with get_db() as client:
        client.execute("""
            CREATE TABLE IF NOT EXISTS experiences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                therapist_name TEXT NOT NULL,
                clinic_name TEXT,
                neighborhood TEXT,
                approach TEXT,
                experience_text TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

# ---------------------------------------------------------
# ۴. تعریف مراحل گفتگو (Conversation States) و کیبوردها
# ---------------------------------------------------------
(
    THERAPIST_NAME,
    CLINIC_NAME,
    NEIGHBORHOOD,
    APPROACH,
    EXPERIENCE_TEXT,
    SEARCH_QUERY,
) = range(6)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✍️ ثبت تجربه", "🔍 جستجوی درمانگر"],
        ["ℹ️ راهنمای سروش"],
    ],
    resize_keyboard=True,
)

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✍️ ثبت تجربه", "🔍 جستجوی درمانگر"],
        ["ℹ️ راهنمای سروش"],
        ["📊 پنل مدیریت و گزارش‌ها"],
    ],
    resize_keyboard=True,
)

def get_menu_keyboard(user_id: int):
    return ADMIN_KEYBOARD if user_id == ADMIN_ID else MAIN_KEYBOARD

# ---------------------------------------------------------
# ۵. دستورات پایه (/start و /help)
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user = update.effective_user
    welcome_text = (
        "سلام! به سروش خوش آمدید 🌱✨\n\n"
        "«سروش» فضایی امن و کاملاً ناشناس برای به اشتراک گذاشتن تجربه‌های واقعی و مثبت مراجعان در مشهد است، "
        "تا انتخاب درمانگر برای شما شفاف‌تر و آگاهانه‌تر باشد 💚\n\n"
        "👇 از دکمه‌های زیر استفاده کنید:"
    )
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_menu_keyboard(user.id),
    )
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "ℹ️ **راهنمای استفاده از سروش**\n\n"
        "• **✍️ ثبت تجربه:** تجربه مثبت خود از جلسات درمان در مشهد را به صورت کاملاً ناشناس ثبت کنید. "
        "پس از بررسی، تجربه شما برای کمک به سایر مراجعان منتشر می‌شود.\n"
        "• **🔍 جستجوی درمانگر:** بر اساس نام درمانگر، کلینیک، محله (مثل وکیل‌آباد، سجاد، احمدآباد...) "
        "یا رویکرد درمانی (مثل خانواده درمانی، انسان‌گرا، شناختی-رفتاری...) جستجو کنید.\n\n"
        "📞 پشتیبانی: @Reza_saadatpour"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

# ---------------------------------------------------------
# ۶. فرآیند ثبت تجربه جدید
# ---------------------------------------------------------
async def start_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👤 نام یا نام خانوادگی درمانگر را بنویسید (بدون پیشوند دکتر):\n"
        "✍️ مثال: علی رضایی\n\n"
        "💡 برای انصراف در هر مرحله می‌توانید /start را بزنید.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return THERAPIST_NAME

async def get_therapist_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["therapist_name"] = update.message.text.strip()
    await update.message.reply_text(
        "🏥 نام کلینیک یا مرکز مشاوره را بنویسید (اگر نمی‌دانید یا مطب شخصی است بنویسید: نامشخص):"
    )
    return CLINIC_NAME

async def get_clinic_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["clinic_name"] = update.message.text.strip()
    await update.message.reply_text(
        "📍 محله یا محدوده کلینیک در مشهد را بنویسید (مثال: بلوار سجاد، احمدآباد، وکیل‌آباد):"
    )
    return NEIGHBORHOOD

async def get_neighborhood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["neighborhood"] = update.message.text.strip()
    await update.message.reply_text(
        "🧠 رویکرد یا حوزه تخصصی درمانگر چیست؟ (مثال: خانواده درمانی، فردی، اضطراب، انسان‌گرا، نامشخص):"
    )
    return APPROACH

async def get_approach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["approach"] = update.message.text.strip()
    await update.message.reply_text(
        "📝 تجربه مثبت و سازنده خود را از روند درمان با این درمانگر بنویسید (هویت شما کاملاً ناشناس می‌ماند):"
    )
    return EXPERIENCE_TEXT

async def save_experience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    exp_text = update.message.text.strip()
    data = context.user_data

    with get_db() as client:
        result = client.execute(
            """
            INSERT INTO experiences (therapist_name, clinic_name, neighborhood, approach, experience_text, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            [
                data["therapist_name"],
                data["clinic_name"],
                data["neighborhood"],
                data["approach"],
                exp_text,
            ],
        )
        exp_id = result.last_insert_rowid

    await update.message.reply_text(
        "✅ سپاس از همراهی شما! تجربه ارزشمندتان به صورت ناشناس ثبت شد و پس از تایید، در سامانه نمایش داده خواهد شد.",
        reply_markup=get_menu_keyboard(update.effective_user.id),
    )

    if ADMIN_ID:
        admin_msg = (
            f"🆕 **تجربه جدید ثبت شد (کد #{exp_id})**\n\n"
            f"👤 درمانگر: {data['therapist_name']}\n"
            f"🏥 کلینیک: {data['clinic_name']}\n"
            f"📍 محله: {data['neighborhood']}\n"
            f"🧠 رویکرد: {data['approach']}\n"
            f"📝 متن تجربه:\n{exp_text}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ تایید و انتشار", callback_data=f"approve_{exp_id}"),
                    InlineKeyboardButton("❌ رد کردن", callback_data=f"reject_{exp_id}"),
                ]
            ]
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_msg,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"Error notifying admin: {e}")

    context.user_data.clear()
    return ConversationHandler.END

# ---------------------------------------------------------
# ۷. فرآیند جستجوی درمانگر
# ---------------------------------------------------------
async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🔍 نام درمانگر، نام کلینیک، محله (مثلاً سجاد) یا رویکرد (مثلاً خانواده درمانی) را بنویسید:\n\n"
        "💡 برای بازگشت به منوی اصلی /start را بزنید.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return SEARCH_QUERY

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = f"%{update.message.text.strip()}%"
    with get_db() as client:
        rs = client.execute(
            """
            SELECT therapist_name, clinic_name, neighborhood, approach, experience_text
            FROM experiences
            WHERE status = 'approved' AND (
                therapist_name LIKE ? OR
                clinic_name LIKE ? OR
                neighborhood LIKE ? OR
                approach LIKE ?
            )
            ORDER BY id DESC LIMIT 10
            """,
            [query, query, query, query],
        )
        rows = rs.rows

    if not rows:
        await update.message.reply_text(
            "😔 هنوز تجربه‌ای مطابق با جستجوی شما ثبت نشده است.\n"
            "می‌توانید با عبارت کوتاه‌تری جستجو کنید یا اولین نفری باشید که تجربه خود را ثبت می‌کند.",
            reply_markup=get_menu_keyboard(update.effective_user.id),
        )
        return ConversationHandler.END

    response = f"🔎 **نتایج یافت‌شده ({len(rows)} مورد):**\n\n"
    for r in rows:
        response += (
            f"👤 **درمانگر:** {r[0]}\n"
            f"🏥 **کلینیک:** {r[1]} | 📍 **محله:** {r[2]}\n"
            f"🧠 **رویکرد:** {r[3]}\n"
            f"💬 **تجربه مراجع:** {r[4]}\n"
            "────────────────────\n"
        )

    await update.message.reply_text(
        response,
        parse_mode="Markdown",
        reply_markup=get_menu_keyboard(update.effective_user.id),
    )
    return ConversationHandler.END

# ---------------------------------------------------------
# ۸. پنل مدیریت و تایید تجربه‌ها
# ---------------------------------------------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    with get_db() as client:
        rs = client.execute(
            "SELECT id, therapist_name, clinic_name, neighborhood, approach, experience_text FROM experiences WHERE status = 'pending'"
        )
        pending = rs.rows

    if not pending:
        await update.message.reply_text(
            "🎉 هیچ تجربه‌ای در صف انتظار نیست! همه چیز بررسی شده است.",
            reply_markup=ADMIN_KEYBOARD,
        )
        return

    for item in pending:
        msg = (
            f"⏳ **در انتظار تایید (کد #{item[0]})**\n\n"
            f"👤 درمانگر: {item[1]}\n"
            f"🏥 کلینیک: {item[2]} | 📍 {item[3]}\n"
            f"🧠 رویکرد: {item[4]}\n"
            f"📝 متن: {item[5]}"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ تایید", callback_data=f"approve_{item[0]}"),
                    InlineKeyboardButton("❌ رد", callback_data=f"reject_{item[0]}"),
                ]
            ]
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != ADMIN_ID:
        return

    action, exp_id = query.data.split("_")
    with get_db() as client:
        if action == "approve":
            client.execute("UPDATE experiences SET status = 'approved' WHERE id = ?", [exp_id])
            await query.edit_message_text(
                f"{query.message.text}\n\n✅ **تایید و منتشر شد.**", parse_mode="Markdown"
            )
        elif action == "reject":
            client.execute("UPDATE experiences SET status = 'rejected' WHERE id = ?", [exp_id])
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ **رد شد.**", parse_mode="Markdown"
            )

# ---------------------------------------------------------
# ۹. اجرای هم‌زمان وب‌سرور (پاسخ 200 OK برای UptimeRobot) و Webhook تلگرام
# ---------------------------------------------------------
async def health_check(request):
    return web.Response(text="Soroush Bot is running!", status=200)

async def run_bot_and_server() -> None:
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^✍️ ثبت تجربه$"), start_submit),
            MessageHandler(filters.Regex("^🔍 جستجوی درمانگر$"), start_search),
        ],
        states={
            THERAPIST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_therapist_name)],
            CLINIC_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_clinic_name)],
            NEIGHBORHOOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_neighborhood)],
            APPROACH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_approach)],
            EXPERIENCE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_experience)],
            SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, perform_search)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.Regex("^ℹ️ راهنمای سروش$"), help_command))
    application.add_handler(MessageHandler(filters.Regex("^📊 پنل مدیریت و گزارش‌ها$"), admin_panel))
    application.add_handler(CallbackQueryHandler(handle_admin_callback))
    application.add_handler(conv_handler)

    webhook_path = f"/{BOT_TOKEN}"
    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}{webhook_path}"

    async def telegram_webhook(request):
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return web.Response(text="OK", status=200)

    await application.initialize()
    await application.start()
    await application.bot.set_webhook(url=webhook_url)

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_post(webhook_path, telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info("Soroush Bot & Health Check Server started successfully.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(run_bot_and_server())
