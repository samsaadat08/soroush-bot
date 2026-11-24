import os
import asyncio
import logging
from aiohttp import web
import libsql_client
from telegram import (
    Update,
    ReplyKeyboardMarkup,
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
# ۲. خواندن متغیرهای محیطی از Render
# ---------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL", "https://soroush-bot-w2q5.onrender.com"
)
PORT = int(os.environ.get("PORT", "8080"))

# ---------------------------------------------------------
# ۳. اتصال ناهمزمان (Async) به دیتابیس Turso و همگام‌سازی جداول
# ---------------------------------------------------------
def get_db():
    db_url = TURSO_DATABASE_URL.replace("libsql://", "https://")
    return libsql_client.create_client(
        url=db_url,
        auth_token=TURSO_AUTH_TOKEN,
    )

async def init_db():
    async with get_db() as client:
        try:
            pragma_rs = await client.execute("PRAGMA table_info(experiences)")
            if pragma_rs.rows:
                expected_cols = {
                    "id", "therapist_name", "clinic_name", "clinic_address",
                    "clinic_phone", "neighborhood", "therapy_domain",
                    "approach", "experience_text", "status", "created_at"
                }
                has_legacy_notnull = any(
                    (row[1] not in expected_cols and row[3] == 1)
                    for row in pragma_rs.rows
                )
                if has_legacy_notnull:
                    await client.execute("ALTER TABLE experiences RENAME TO experiences_legacy_backup")
        except Exception as e:
            logger.error(f"Schema check info: {e}")

        await client.execute("""
            CREATE TABLE IF NOT EXISTS experiences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                therapist_name TEXT NOT NULL,
                clinic_name TEXT,
                clinic_address TEXT,
                clinic_phone TEXT,
                neighborhood TEXT,
                therapy_domain TEXT,
                approach TEXT,
                experience_text TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col in [
            "clinic_name",
            "clinic_address",
            "clinic_phone",
            "neighborhood",
            "therapy_domain",
            "approach",
            "status",
        ]:
            try:
                await client.execute(f"ALTER TABLE experiences ADD COLUMN {col} TEXT")
            except Exception:
                pass

        await client.execute("""
            CREATE TABLE IF NOT EXISTS clinics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                address TEXT,
                phone TEXT,
                status TEXT DEFAULT 'approved'
            )
        """)
        for col in ["address", "phone", "status"]:
            try:
                await client.execute(f"ALTER TABLE clinics ADD COLUMN {col} TEXT")
            except Exception:
                pass

        try:
            await client.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clinics_name ON clinics(name)")
        except Exception:
            pass

        default_clinics = [
            ("کلینیک هیمان", "مشهد - بلوار هاشمیه", "ثبت نشده"),
            ("کلینیک پناه", "مشهد - محدوده بلوار سجاد / احمدآباد", "ثبت نشده"),
            ("پلی‌کلینیک دانشگاه فردوسی", "مشهد - پردیس دانشگاه فردوسی", "ثبت نشده"),
        ]
        for c_name, c_addr, c_phone in default_clinics:
            try:
                check_rs = await client.execute("SELECT id FROM clinics WHERE name = ?", [c_name])
                if not check_rs.rows:
                    await client.execute(
                        "INSERT INTO clinics (name, address, phone, status) VALUES (?, ?, ?, 'approved')",
                        [c_name, c_addr, c_phone],
                    )
            except Exception:
                pass

# ---------------------------------------------------------
# ۴. تعریف مراحل گفتگو و کیبوردهای گزینه‌ای
# ---------------------------------------------------------
(
    THERAPIST_NAME,
    CLINIC_SELECT,
    NEW_CLINIC_NAME,
    NEW_CLINIC_ADDRESS,
    NEW_CLINIC_PHONE,
    CONFIRM_EXISTING_CLINIC,
    NEIGHBORHOOD,
    THERAPY_DOMAIN,
    APPROACH,
    EXPERIENCE_TEXT,
    SEARCH_TYPE,
    SEARCH_QUERY,
    GUIDE_MENU,
) = range(13)

BACK_BTN = "🔙 بازگشت به منوی اصلی"
GUIDE_BTN = "🧭 راهنمای انتخاب آگاهانه درمانگر"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✍️ ثبت تجربه", "🔍 جستجوی درمانگر"],
        [GUIDE_BTN],
    ],
    resize_keyboard=True,
)

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✍️ ثبت تجربه", "🔍 جستجوی درمانگر"],
        [GUIDE_BTN],
        ["📊 پنل مدیریت و گزارش‌ها"],
    ],
    resize_keyboard=True,
)

GUIDE_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["⚖️ اخلاق حرفه‌ای و خطوط قرمز درمانگر"],
        ["🎓 چک‌لیست انتخاب درمانگر حرفه‌ای"],
        ["🧠 رویکردهای درمانی به زبان ساده"],
        ["📖 نحوه کار با ربات"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

NEIGHBORHOOD_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["بلوار سجاد", "وکیل‌آباد", "احمدآباد"],
        ["هاشمیه", "کوهسنگی", "معلم و دانشجو"],
        ["قاسم‌آباد و الهیه", "سایر مناطق مشهد"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

DOMAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["فردی بزرگسال", "زوج‌درمانی"],
        ["خانواده درمانی", "کودک و نوجوان"],
        ["گروه درمانی", "مشاوره پیش از ازدواج"],
        ["تحصیلی و شغلی", "سایر حوزه‌ها"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

APPROACH_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["انسان‌گرا", "وجودی"],
        ["شناختی-رفتاری (CBT)", "طرحواره‌درمانی"],
        ["متمرکز بر هیجان (EFT)", "روان‌پویشی کوتاه‌مدت"],
        ["پذیرش و تعهد (ACT)", "روانکاوی و تحلیلی"],
        ["سایر رویکردها / نامشخص"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

SEARCH_TYPE_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["👤 جستجو با نام درمانگر", "🏥 جستجو با کلینیک"],
        ["🎯 جستجو با حوزه درمان", "🧠 جستجو با رویکرد"],
        ["📍 جستجو با محله", "📋 آخرین تجربه‌های ثبت‌شده"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

def get_menu_keyboard(user_id: int):
    return ADMIN_KEYBOARD if user_id == ADMIN_ID else MAIN_KEYBOARD

async def build_clinics_keyboard(include_add_new: bool = True) -> ReplyKeyboardMarkup:
    buttons = []
    try:
        async with get_db() as client:
            rs = await client.execute("SELECT name FROM clinics WHERE status = 'approved' ORDER BY id ASC")
            clinic_names = [row[0] for row in rs.rows]
            for i in range(0, len(clinic_names), 2):
                buttons.append(clinic_names[i : i + 2])
    except Exception as e:
        logger.error(f"Error loading clinics: {e}")

    if include_add_new:
        buttons.append(["➕ ثبت کلینیک جدید", "مطب شخصی / نامشخص"])
    buttons.append([BACK_BTN])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# ---------------------------------------------------------
# ۵. دستورات پایه (/start) و بخش آموزشی راهنمای انتخاب آگاهانه درمانگر
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user = update.effective_user
    welcome_text = (
        "سلام! به سروش خوش آمدید 🌱✨\n\u200c\n"
        "«سروش» فضایی امن و کاملاً ناشناس برای به اشتراک گذاشتن تجربه‌های واقعی و مثبت مراجعان در مشهد است، "
        "تا انتخاب درمانگر برای شما شفاف‌تر و آگاهانه‌تر باشد 💚\n\u200c\n"
        "👇 از دکمه‌های زیر استفاده کنید:"
    )
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_menu_keyboard(user.id),
    )
    return ConversationHandler.END

async def open_guide_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🧭 **راهنمای انتخاب آگاهانه درمانگر**\n\u200c\n"
        "انتخاب یک درمانگر خوب، مهم‌ترین قدم در مسیر رشد و حال خوب است.\n"
        "برای اینکه با آگاهی کامل تصمیم بگیرید، یکی از موضوعات زیر را انتخاب کنید 👇",
        parse_mode="Markdown",
        reply_markup
