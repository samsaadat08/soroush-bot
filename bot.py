import os
import asyncio
import logging
from aiohttp import web
import libsql_experimental as libsql
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
# ۳. اتصال و راه‌اندازی دیتابیس Turso
# ---------------------------------------------------------
def get_db():
    return libsql.connect(
        database=TURSO_DATABASE_URL,
        auth_token=TURSO_AUTH_TOKEN,
    )

def init_db():
    conn = get_db()
    conn.execute("""
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
    conn.commit()

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
    context.user_data["clinic_name"] = update.message.text.strip
