import os
import io
import sys
import json
import asyncio
import logging
from urllib.parse import quote
from datetime import datetime
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
# ۱. تنظیمات لاگ‌گیری آنی
# ---------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# ۲. خواندن متغیرهای محیطی از Render
# ---------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")
RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL", "https://soroush-bot-w2q5.onrender.com"
).strip()
PORT = int(os.environ.get("PORT", "8080") or "8080")

# ---------------------------------------------------------
# ۳. توابع کمکی پاک‌سازی نام و ساخت لینک جستجوی گوگل
# ---------------------------------------------------------
def clean_therapist_name(raw_name: str) -> str:
    name = raw_name.strip()
    prefixes = ["دکتر ", "آقای ", "خانم ", "استاد ", "دکتر", "آقای", "خانم"]
    for p in prefixes:
        if name.startswith(p):
            name = name[len(p):].strip()
    return name

def build_google_search_url(therapist_name: str, clinic_name: str) -> str:
    if clinic_name and clinic_name not in ["مطب شخصی / نامشخص", "نامشخص", ""]:
        query = f"{clinic_name} {therapist_name} مشهد آدرس و شماره تماس"
    else:
        query = f"دکتر {therapist_name} روانشناس مشهد آدرس و شماره تماس"
    return f"https://www.google.com/search?q={quote(query)}"

# ---------------------------------------------------------
# ۴. اتصال ناهمزمان (Async) به دیتابیس Turso و همگام‌سازی جداول
# ---------------------------------------------------------
def get_db():
    db_url = TURSO_DATABASE_URL.replace("libsql://", "https://")
    return libsql_client.create_client(
        url=db_url,
        auth_token=TURSO_AUTH_TOKEN,
    )

async def init_db():
    if not TURSO_DATABASE_URL:
        logger.error("TURSO_DATABASE_URL is missing!")
        return
    try:
        async with get_db() as client:
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

            try:
                t_pragma = await client.execute("PRAGMA table_info(therapists)")
                if t_pragma.rows:
                    existing_t_cols = {row[1] for row in t_pragma.rows}
                    if "full_name" not in existing_t_cols or "nezam_code" not in existing_t_cols:
                        await client.execute("ALTER TABLE therapists RENAME TO therapists_legacy_backup")
            except Exception as e:
                logger.warning(f"Therapists schema migration notice: {e}")

            await client.execute("""
                CREATE TABLE IF NOT EXISTS therapists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    full_name TEXT NOT NULL,
                    nezam_code TEXT,
                    license_number TEXT,
                    clinic_name TEXT,
                    clinic_address TEXT,
                    clinic_phone TEXT,
                    therapy_domain TEXT,
                    approach TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for col in [
                "nezam_code",
                "license_number",
                "clinic_name",
                "clinic_address",
                "clinic_phone",
                "therapy_domain",
                "approach",
                "status",
            ]:
                try:
                    await client.execute(f"ALTER TABLE therapists ADD COLUMN {col} TEXT")
                except Exception:
                    pass

            default_clinics = [
                # --- مراکز منتخب و کلیدی ---
                ("کلینیک هیمان", "مشهد - بلوار پیروزی، حد فاصل میدان هاشمیه تا چهارراه کوثر، بین پیروزی ۳۴ و ۳۶، پلاک ۱۱۲۶", "09921883558"),
                ("کلینیک پناه", "مشهد - بلوار کوهسنگی، کوهسنگی ۸، بهشتی ۱۱", "05138588221 - 09376716007"),
                ("پلی‌کلینیک دانشگاه فردوسی", "مشهد - بلوار کوثر، نرسیده به کوثر ۸/۱", "05138803676"),
                ("کلینیک روانشناسی ماهور", "مشهد - بلوار وکیل‌آباد، نبش وکیل‌آباد ۶۵", "09153027266"),
                ("خانه طرحواره درمانی", "مشهد - بلوار وکیل‌آباد، بین وکیل‌آباد ۱۶ و هاشمیه، پلاک ۲۱۲", "05138822784"),
                ("کلینیک مشاوره آذر", "مشهد - محدوده بهشت", "ثبت نشده"),
                ("کلینیک روانشناسی رسا", "مشهد - بلوار وکیل‌آباد، وکیل‌آباد ۹، مدرس (میرزا کوچک خان ۲)", "05136111328"),
                ("مرکز مشاوره آستان مهر", "مشهد - بلوار دانشجو، حدفاصل دانشجو ۱۸ و ۲۰، پلاک ۲۳۰", "05136018584 - 09150732750"),

                # --- محدوده وکیل‌آباد، هفت‌تیر، دانشجو و سیدرضی ---
                ("مرکز مشاوره سیب", "مشهد - بلوار وکیل‌آباد، بین وکیل‌آباد ۷ و ۹، پلاک ۶۷", "05136017193 - 09159200269"),
                ("کلینیک روان پژوه", "مشهد - بلوار وکیل‌آباد، ابتدای هفت‌تیر، پلاک ۱، واحد ۲", "05138694055 - 05138640212"),
                ("مرکز مشاوره روزبه", "مشهد - بلوار وکیل‌آباد، حدفاصل وکیل‌آباد ۳۵ و ۳۷", "05138905560"),
                ("مرکز مشاوره کریمی‌نسب", "مشهد - بلوار وکیل‌آباد، حدفاصل وکیل‌آباد ۱۰ و ۱۲، پلاک ۱۴۴، طبقه ۲", "05138800690 - 09308499715"),
                ("کلینیک مشاوره روان پلاس", "مشهد - بلوار وکیل‌آباد، نبش وکیل‌آباد ۴۶", "09012162832"),
                ("مرکز مشاوره شیرین حسنی", "مشهد - بلوار وکیل‌آباد، حدفاصل وکیل‌آباد ۲۲ و ۲۴", "05138681515 - 09150616757"),
                ("مرکز مشاوره نرگس باقرزاده", "مشهد - بلوار وکیل‌آباد، دانش‌آموز ۸، پلاک ۶۶", "05138932216"),
                ("مرکز مشاوره دکتر نوبخت و نیکونام", "مشهد - بلوار وکیل‌آباد، دانش‌آموز سوم", "ثبت نشده"),
                ("کلینیک بهقدم", "مشهد - تقاطع جلال آل احمد و فرهنگ، بین فرهنگ ۷ و ۹، پلاک ۱۲۳، طبقه ۱", "05136099499"),
                ("مرکز مشاوره صابر", "مشهد - کنارگذر وکیل‌آباد، بین بلوار هاشمیه و بلوار جلال آل احمد", "ثبت نشده"),
                ("مرکز خدمات روانشناسی کاوش", "مشهد - اقبال، کنارگذر وکیل‌آباد", "ثبت نشده"),
                ("دپارتمان روانشناسی آیرا", "مشهد - بلوار دانشجو، بین دانشجو ۲۶ و ۲۸", "ثبت نشده"),
                ("مرکز مشاوره دل‌آرا", "مشهد - بلوار معلم، بین بلوار دانشجو و پرورش شمالی", "ثبت نشده"),
                ("مرکز مشاوره شهید رضوی", "مشهد - بلوار سید رضی، سیدرضی ۵۹، بین قاسمی ۶۵ و ۶۶، پلاک ۱۸۲", "05136031449"),

                # --- محدوده بلوار سجاد، خیام و فرامرز عباسی ---
                ("پلی‌کلینیک روانشناسی بزرگمهر", "مشهد - حاشیه بلوار سجاد، بین گلریز و چهارراه خیام، پلاک ۱۲، طبقه ۱", "05137607922 - 09153052895"),
                ("کلینیک ترنم زندگی", "مشهد - بلوار سجاد، خیابان بهارستان ۲، مجتمع وصال، طبقه ۱", "09337398256"),
                ("مرکز مشاوره بینش نو", "مشهد - سجادشهر، بلوار فردوسی، بین ابریشم و بهارستان", "ثبت نشده"),
                ("مرکز مشاوره صبا", "مشهد - بین خیام شمالی ۵۴ و ۵۶، ساختمان باران، طبقه اول", "ثبت نشده"),
                ("مرکز مشاوره دوستکام", "مشهد - شهید فرامرز عباسی، فرامرز عباسی ۱۲", "ثبت نشده"),

                # --- محدوده احمدآباد، بعثت، راهنمایی و ملک‌آباد ---
                ("مرکز مشاوره اقبال لاهوری", "مشهد - بلوار بعثت، تقاطع بلوار رضا، جنب بانک سپه، پلاک ۶۷", "05138404041"),
                ("مرکز مشاوره آرامش ماندگار", "مشهد - احمدآباد، بعثت ۴، بین طالقانی ۲۱ و ۲۳، پلاک ۷۷", "09357845158"),
                ("مرکز مشاوره رایحه", "مشهد - خیابان راهنمایی، روبروی برج سلمان، پلاک ۷۷", "09357845158"),
                ("کلینیک روانشناسی شکوفه", "مشهد - بلوار احمدآباد، بعد از سه‌راه راهنمایی، ساختمان میر، طبقه ۲", "05138408516"),
                ("مرکز مشاوره عمومی خوارزمی", "مشهد - بلوار احمدآباد، بین بهشت و پاستور، ساختمان ۸۵، طبقه ۳", "ثبت نشده"),
                ("مرکز مشاوره ماندگار", "مشهد - احمدآباد، خیابان طالقانی، رضا بیست و یکم", "ثبت نشده"),
                ("کلینیک روانشناسی سبک زندگی", "مشهد - احمدآباد، بلوار ملاصدرا ۸، دادگر ۸، پلاک ۶/۷", "ثبت نشده"),
                ("مرکز مشاوره ذهن و زندگی", "مشهد - ملک‌آباد، بلوار خیام، خیام جنوبی ۴ (کمال‌الملک ۳)، پلاک ۹", "05137618089 - 09159166703"),
                ("کلینیک مشاوره آستان قدس (شماره ۲)", "مشهد - انتهای بلوار ملک‌آباد، حدفاصل خیابان گویا و مرجان، پلاک ۳/۳۵۸", "05136046302"),

                # --- محدوده هاشمیه، پیروزی و آزادشهر ---
                ("مرکز مشاوره مهر پایا", "مشهد - بلوار هاشمیه، هاشمیه ۳۹، پلاک ۷", "ثبت نشده"),
                ("مرکز مشاوره هما", "مشهد - بلوار پیروزی، بین هنرستان ۳۶/۶ و میدان هاشمیه", "ثبت نشده"),
                ("کلینیک زندگی ماهرانه", "مشهد - چهارراه آزادشهر، امامت ۲، ساختمان پزشکان پاستور، پلاک ۸، طبقه ۴", "ثبت نشده"),
                ("مرکز مشاوره پیوند", "مشهد - محدوده وکیل‌آباد و آزادشهر", "05138787034"),

                # --- محدوده کوهسنگی، دانشگاه، چمران و قاسم‌آباد ---
                ("مرکز مشاوره تخصصی پویش", "مشهد - کوهسنگی ۱/۸، پلاک ۳۵، واحد ۵", "05138534111"),
                ("مرکز مشاوره باقرپور", "مشهد - بلوار کوهسنگی، کوهسنگی دوم", "ثبت نشده"),
                ("مرکز روانشناسی جهاد دانشگاهی", "مشهد - سه‌راه ادبیات، مجتمع جهاد دانشگاهی", "ثبت نشده"),
                ("مرکز مشاوره جوانان آستان قدس", "مشهد - حدفاصل خیابان دانشگاه و چمران، خیابان گلستان، پلاک ۹۰", "05138513682 - 05138419435"),
                ("مرکز مشاوره مهرآفرین", "مشهد - قاسم‌آباد، بلوار امامیه، نبش امامیه ۱۸", "09052266920"),
            ]

            for c_name, c_addr, c_phone in default_clinics:
                try:
                    check_rs = await client.execute("SELECT id, address, phone FROM clinics WHERE name = ?", [c_name])
                    if not check_rs.rows:
                        await client.execute(
                            "INSERT INTO clinics (name, address, phone, status) VALUES (?, ?, ?, 'approved')",
                            [c_name, c_addr, c_phone],
                        )
                    else:
                        old_id, old_addr, old_phone = check_rs.rows[0]
                        if old_addr in [None, "", "ثبت نشده"] or "محدوده بلوار سجاد" in old_addr or "پردیس دانشگاه" in old_addr:
                            await client.execute(
                                "UPDATE clinics SET address = ?, phone = ?, status = 'approved' WHERE id = ?",
                                [c_addr, c_phone, old_id],
                            )
                except Exception:
                    pass
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.exception(f"Database init error (continuing startup): {e}")

# ---------------------------------------------------------
# ۵. تعریف مراحل سفر مراجع و سفر درمانگر
# ---------------------------------------------------------
(
    THERAPIST_NAME,
    CLINIC_SELECT,
    NEW_CLINIC_NAME,
    THERAPY_DOMAIN,
    EXPERIENCE_TEXT,
    SEARCH_TYPE,
    SEARCH_QUERY,
    GUIDE_MENU,
    QUIZ_Q1,
    QUIZ_Q2,
    QUIZ_Q3,
    QUIZ_Q4,
    DOC_NAME,
    DOC_NEZAM,
    DOC_LICENSE,
    DOC_CLINIC,
    DOC_ADDRESS,
    DOC_PHONE,
    DOC_DOMAIN,
    DOC_APPROACH,
) = range(20)

BACK_BTN = "🏠 بازگشت به منوی اصلی"
CLIENT_ROLE_BTN = "👤 مراجع هستم"
THERAPIST_ROLE_BTN = "🩺 درمانگر هستم"
GUIDE_BTN = "🧭 راهنمای انتخاب درمانگر"
QUIZ_BTN = "🧩 کدام سبک مشاوره به من می‌خورد؟"
BACKUP_BTN = "📥 دریافت فایل پشتیبان (Backup)"

ALLOWED_DOMAINS = [
    "فردی بزرگسال",
    "زوج‌درمانی",
    "خانواده درمانی",
    "کودک و نوجوان",
    "مشاوره پیش از ازدواج",
    "نمی‌دانم / سایر موارد",
]

ALLOWED_APPROACHES = [
    "انسان‌گرا و وجودی",
    "شناختی-رفتاری (CBT)",
    "پویشی و تحلیلی",
    "سیستمی و رابطه‌ای (EFT)",
    "طرحواره‌درمانی",
    "پذیرش و تعهد (ACT)",
    "سایر رویکردها / ترکیبی",
]

ROLE_KEYBOARD = ReplyKeyboardMarkup(
    [
        [CLIENT_ROLE_BTN, THERAPIST_ROLE_BTN],
    ],
    resize_keyboard=True,
)

CLIENT_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔍 پیدا کردن درمانگر در مشهد", "✍️ ثبت تجربه من از درمان"],
        [QUIZ_BTN],
        [GUIDE_BTN, "🔄 تغییر نقش (ورود درمانگران)"],
    ],
    resize_keyboard=True,
)

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔍 پیدا کردن درمانگر در مشهد", "✍️ ثبت تجربه من از درمان"],
        [QUIZ_BTN, THERAPIST_ROLE_BTN],
        ["📊 پنل مدیریت و تاییدها", BACKUP_BTN],
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

DOMAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["فردی بزرگسال", "زوج‌درمانی"],
        ["خانواده درمانی", "کودک و نوجوان"],
        ["مشاوره پیش از ازدواج", "نمی‌دانم / سایر موارد"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

APPROACH_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["انسان‌گرا و وجودی", "شناختی-رفتاری (CBT)"],
        ["پویشی و تحلیلی", "سیستمی و رابطه‌ای (EFT)"],
        ["طرحواره‌درمانی", "پذیرش و تعهد (ACT)"],
        ["سایر رویکردها / ترکیبی"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

SEARCH_TYPE_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["👤 جستجو با نام درمانگر", "🏥 جستجو با کلینیک"],
        ["🎯 جستجو با موضوع (فردی، زوج...)", "🧠 جستجو با سبک درمان"],
        ["📋 آخرین تجربه‌های ثبت‌شده", "🩺 لیست درمانگران تاییدشده"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

def get_client_menu(user_id: int):
    return ADMIN_KEYBOARD if user_id == ADMIN_ID else CLIENT_KEYBOARD

async def get_approved_clinic_names() -> list[str]:
    try:
        async with get_db() as client:
            rs = await client.execute("SELECT name FROM clinics WHERE status = 'approved' ORDER BY id ASC")
            return [row[0] for row in rs.rows]
    except Exception as e:
        logger.error(f"Error loading clinics: {e}")
        return []

async def build_clinics_keyboard(include_add_new: bool = True) -> ReplyKeyboardMarkup:
    clinic_names = await get_approved_clinic_names()
    buttons = [clinic_names[i : i + 2] for i in range(0, len(clinic_names), 2)]
    if include_add_new:
        buttons.append(["مطب شخصی / نامشخص", "➕ نام کلینیک در لیست نیست"])
    buttons.append([BACK_BTN])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# ---------------------------------------------------------
# ۶. نقطه شروع (/start) - معرفی جامع پلتفرم دووجهی سروش
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user = update.effective_user

    if user.id == ADMIN_ID:
        await update.message.reply_text(
            "سلام رضا جان، به پنل مدیریت و سامانه جامع **سروش** خوش آمدی 🌿",
            parse_mode="Markdown",
            reply_markup=ADMIN_KEYBOARD,
        )
        return ConversationHandler.END

    welcome_text = """سلام، به **سروش** خوش آمدید 🌿
پلتفرم هوشمند آشنایی با روان‌درمانگران مشهد و اشتراک تجربه‌های واقعی اتاق درمان.

**در سروش چه خبر است؟**
• **اگر مراجع هستید:** می‌توانید پروفایل رسمی درمانگران مشهد را ببینید، تجربه‌های واقعی مراجعان را بخوانید، تجربه خودتان را ناشناس ثبت کنید یا با چند سوال ساده با سبک‌های مشاوره آشنا شوید.
• **اگر درمانگر هستید:** می‌توانید با ثبت شماره نظام، پروانه اشتغال، رویکرد و اطلاعات مرکز فعالیت، پروفایل حرفه‌ای خود را به مراجعان معرفی کنید.

برای شروع، لطفاً نقش خود را انتخاب کنید 👇"""
    await update.message.reply_text(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=ROLE_KEYBOARD,
    )
    return ConversationHandler.END

async def enter_client_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    msg = """👤 **بخش مراجعان سروش**

اینجا همه چیز برای یک انتخاب آگاهانه و بدون سردرگمی آماده است:
• 🔍 بررسی پروفایل درمانگران تاییدشده و خواندن تجربه‌های واقعی مراجعان
• ✍️ ثبت ناشناس تجربه شخصی شما از جلسات درمان
• 🧩 آشنایی اولیه با سبک‌های اصلی مشاوره

از دکمه‌های پایین استفاده کنید 👇"""
    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=get_client_menu(user.id),
    )
    return ConversationHandler.END

# ---------------------------------------------------------
# ۷. سفر اختصاصی درمانگران: ثبت و معرفی پروفایل حرفه‌ای (قدم ۱ تا ۸)
# ---------------------------------------------------------
async def start_therapist_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = """🩺 `مسیر شما: ثبت پروفایل درمانگر ◀️ قدم ۱ از ۸`

همکار گرامی، خوش آمدید 🌿
تکمیل این پروفایل کمک می‌کند مراجعان مشهد بر اساس رویکرد، حوزه تخصصی و شماره پروانه معتبر، شناخت دقیقی از شما پیدا کنند.

👤 لطفاً **نام و نام خانوادگی** خود را بنویسید (بدون پیشوند دکتر):"""
    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return DOC_NAME

async def get_doc_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

    cleaned = clean_therapist_name(text)
    if len(cleaned) < 3:
        await update.message.reply_text("⚠️ لطفاً نام و نام خانوادگی را کامل وارد کنید:")
        return DOC_NAME

    context.user_data["doc_name"] = cleaned
    await update.message.reply_text(
        "🩺 `مسیر شما: ثبت پروفایل درمانگر ◀️ قدم ۲ از ۸`\n\n🪪 **شماره عضویت سازمان نظام روان‌شناسی** خود را وارد کنید (مثال: 61126):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return DOC_NEZAM

async def get_doc_nezam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

    context.user_data["doc_nezam"] = text
    await update.message.reply_text(
        "🩺 `مسیر شما: ثبت پروفایل درمانگر ◀️ قدم ۳ از ۸`\n\n📜 **شماره پروانه اشتغال تخصصی** خود را وارد کنید (در صورت نداشتن پروانه، عدد 0 را بفرستید):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return DOC_LICENSE

async def get_doc_license(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

    context.user_data["doc_license"] = "فاقد پروانه / در دست اقدام" if text == "0" else text
    await update.message.reply_text(
        "🩺 `مسیر شما: ثبت پروفایل درمانگر ◀️ قدم ۴ از ۸`\n\n🏥 **نام کلینیک یا مرکز مشاوره‌ای** که در آن فعالیت دارید را بنویسید (مثال: کلینیک هیمان یا مطب شخصی):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return DOC_CLINIC

async def get_doc_clinic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

    context.user_data["doc_clinic"] = text
    await update.message.reply_text(
        "🩺 `مسیر شما: ثبت پروفایل درمانگر ◀️ قدم ۵ از ۸`\n\n🗺 **آدرس دقیق کلینیک یا مطب** در مشهد را وارد کنید:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return DOC_ADDRESS

async def get_doc_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

    context.user_data["doc_address"] = text
    await update.message.reply_text(
        "🩺 `مسیر شما: ثبت پروفایل درمانگر ◀️ قدم ۶ از ۸`\n\n☎️ **شماره تماس پذیرش یا نوبت‌دهی** (یا آیدی تلگرام هماهنگی) را وارد کنید:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return DOC_PHONE

async def get_doc_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

    context.user_data["doc_phone"] = text
    await update.message.reply_text(
        "🩺 `مسیر شما: ثبت پروفایل درمانگر ◀️ قدم ۷ از ۸`\n\n🎯 **حوزه اصلی فعالیت** خود را از گزینه‌های زیر انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=DOMAIN_KEYBOARD,
    )
    return DOC_DOMAIN

async def get_doc_domain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

    if text not in ALLOWED_DOMAINS:
        await update.message.reply_text("⚠️ لطفاً حوزه فعالیت را از دکمه‌های زیر انتخاب کنید:", reply_markup=DOMAIN_KEYBOARD)
        return DOC_DOMAIN

    context.user_data["doc_domain"] = text
    await update.message.reply_text(
        "🩺 `مسیر شما: ثبت پروفایل درمانگر ◀️ قدم ۸ از ۸ (مرحله آخر)`\n\n🧠 **رویکرد درمانی** غالب خود را از گزینه‌ها انتخاب کنید (یا اگر رویکرد دیگری دارید، نام آن را بنویسید):",
        parse_mode="Markdown",
        reply_markup=APPROACH_KEYBOARD,
    )
    return DOC_APPROACH

async def save_therapist_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

    if len(text) < 2:
        await update.message.reply_text("⚠️ لطفاً رویکرد درمانی خود را انتخاب کنید یا بنویسید:", reply_markup=APPROACH_KEYBOARD)
        return DOC_APPROACH

    d = context.user_data
    d_name = d.get("doc_name") or "نامشخص"
    d_nezam = d.get("doc_nezam") or "ثبت نشده"
    d_license = d.get("doc_license") or "ثبت نشده"
    d_clinic = d.get("doc_clinic") or "نامشخص"
    d_addr = d.get("doc_address") or "ثبت نشده"
    d_phone = d.get("doc_phone") or "ثبت نشده"
    d_domain = d.get("doc_domain") or "نامشخص"

    final_kb = ADMIN_KEYBOARD if update.effective_user.id == ADMIN_ID else ROLE_KEYBOARD

    try:
        async with get_db() as client:
            res = await client.execute(
                """
                INSERT INTO therapists (
                    full_name, nezam_code, license_number, clinic_name,
                    clinic_address, clinic_phone, therapy_domain, approach, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                [d_name, d_nezam, d_license, d_clinic, d_addr, d_phone, d_domain, text],
            )
            doc_id = res.last_insert_rowid or 1

        await update.message.reply_text(
            "✅ **سپاس همکار گرامی!**\nاطلاعات پروفایل حرفه‌ای شما با موفقیت ثبت شد و پس از بررسی کد نظام و تایید نهایی، در سامانه «سروش» برای مراجعان قابل مشاهده خواهد بود 🌿",
            parse_mode="Markdown",
            reply_markup=final_kb,
        )

        if ADMIN_ID:
            admin_msg = f"""🩺 **درخواست ثبت پروفایل درمانگر جدید (کد #{doc_id})**

👤 **نام:** {d_name}
🪪 **کد نظام:** `{d_nezam}`
📜 **شماره پروانه:** `{d_license}`
🏥 **کلینیک/مرکز:** {d_clinic}
🗺 **آدرس:** {d_addr}
☎️ **شماره تماس:** `{d_phone}`
🎯 **حوزه فعالیت:** {d_domain}
🧠 **رویکرد:** {text}"""
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ تایید پروفایل درمانگر", callback_data=f"docok_{doc_id}"),
                        InlineKeyboardButton("❌ رد کردن", callback_data=f"docno_{doc_id}"),
                    ]
                ]
            )
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode="Markdown", reply_markup=kb)
            except Exception as e:
                logger.error(f"Error notifying admin about therapist: {e}")

    except Exception as e:
        logger.error(f"Error saving therapist profile: {e}")
        await update.message.reply_text(
            f"⚠️ خطایی در ثبت اطلاعات رخ داد:\n`{str(e)[:120]}`",
            parse_mode="Markdown",
            reply_markup=final_kb,
        )

    context.user_data.clear()
    return ConversationHandler.END

# ---------------------------------------------------------
# ۸. آزمون کاملاً ساده و روزمره ۴ رویکرد اساسی (غیرتشخیصی)
# ---------------------------------------------------------
async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["quiz_scores"] = {
        "پویشی و تحلیلی": 0,
        "شناختی-رفتاری (CBT)": 0,
        "انسان‌گرا و وجودی": 0,
        "سیستمی و رابطه‌ای (EFT)": 0,
    }
    intro_msg = """📍 `مسیر شما: آشنایی ساده با سبک‌های مشاوره ◀️ سوال ۱ از ۴`

⚠️ **یک نکته مهم قبل از شروع:**
این ۴ سوال خیلی ساده فقط برای این است که یک دید کلی و اولیه پیدا کنید. این پرسش‌ها **به هیچ عنوان جنبه تشخیصی یا پزشکی ندارند** و برای انتخاب دقیق سبک درمان، حتماً باید در جلسه اول با درمانگر مفصل صحبت کنید.

━━━━━━━━━━━━━━━━━━
**سوال ۱ از ۴:**
وقتی یک غصه یا مشکل ذهنی اذیتتان می‌کند، دوست دارید در جلسه مشاوره بیشتر درباره چه چیزی حرف بزنید؟"""

    kb = ReplyKeyboardMarkup(
        [
            ["۱. ریشه این حالِ بد در گذشته و اتفاقات تکراری زندگی‌ام"],
            ["۲. راهکار عملی برای کنترل فکرهای منفی و استرس‌های روزمره"],
            ["۳. پیدا کردن هدف، معنا و شناخت بهتر خودِ واقعی‌ام"],
            ["۴. درست کردن رابطه‌ام با همسر، خانواده یا اطرافیانم"],
            [BACK_BTN],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text(intro_msg, parse_mode="Markdown", reply_markup=kb)
    return QUIZ_Q1

async def handle_quiz_q1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await enter_client_mode(update, context)

    scores = context.user_data.get("quiz_scores", {})
    if text.startswith("۱"):
        scores["پویشی و تحلیلی"] += 1
    elif text.startswith("۲"):
        scores["شناختی-رفتاری (CBT)"] += 1
    elif text.startswith("۳"):
        scores["انسان‌گرا و وجودی"] += 1
    elif text.startswith("۴"):
        scores["سیستمی و رابطه‌ای (EFT)"] += 1
    else:
        await update.message.reply_text("👇 لطفاً یکی از ۴ گزینه پایین را لمس کنید:")
        return QUIZ_Q1

    q2_msg = """📍 `مسیر شما: آشنایی ساده با سبک‌های مشاوره ◀️ سوال ۲ از ۴`

**سوال ۲ از ۴:**
پیش کدام مدل مشاور احساس راحتی بیشتری می‌کنید؟"""

    kb = ReplyKeyboardMarkup(
        [
            ["۱. کسی که با حوصله گوش دهد و کمکم کند احساسات قدیمی‌ام را بفهمم"],
            ["۲. کسی که مثل یک مربی دلسوز، تمرین و راهکار مشخص بدهد"],
            ["۳. کسی که قضاوتم نکند و کمکم کند راه خودم را پیدا کنم"],
            ["۴. کسی که بلد باشد گره‌های عاطفی بین من و اطرافیانم را باز کند"],
            [BACK_BTN],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text(q2_msg, parse_mode="Markdown", reply_markup=kb)
    return QUIZ_Q2

async def handle_quiz_q2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await enter_client_mode(update, context)

    scores = context.user_data.get("quiz_scores", {})
    if text.startswith("۱"):
        scores["پویشی و تحلیلی"] += 1
    elif text.startswith("۲"):
        scores["شناختی-رفتاری (CBT)"] += 1
    elif text.startswith("۳"):
        scores["انسان‌گرا و وجودی"] += 1
    elif text.startswith("۴"):
        scores["سیستمی و رابطه‌ای (EFT)"] += 1
    else:
        await update.message.reply_text("👇 لطفاً یکی از ۴ گزینه پایین را لمس کنید:")
        return QUIZ_Q2

    q3_msg = """📍 `مسیر شما: آشنایی ساده با سبک‌های مشاوره ◀️ سوال ۳ از ۴`

**سوال ۳ از ۴:**
اگر این جلسات مشاوره خوب پیش برود، دوست دارید آخرش به چه چیزی برسید؟"""

    kb = ReplyKeyboardMarkup(
        [
            ["۱. بفهمم چرا این‌طوری رفتار می‌کنم و زخم‌های قدیمی‌ام خوب شود"],
            ["۲. افکار آزاردهنده، ترس‌ها یا بی‌حوصلگی الانم کم شود و آرامش بگیرم"],
            ["۳. برای زندگی‌ام هدف و معنا داشته باشم و راحت‌تر تصمیم بگیرم"],
            ["۴. در رابطه‌ام احساس امنیت، درک شدن و صمیمیت بیشتری داشته باشم"],
            [BACK_BTN],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text(q3_msg, parse_mode="Markdown", reply_markup=kb)
    return QUIZ_Q3

async def handle_quiz_q3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await enter_client_mode(update, context)

    scores = context.user_data.get("quiz_scores", {})
    if text.startswith("۱"):
        scores["پویشی و تحلیلی"] += 1
    elif text.startswith("۲"):
        scores["شناختی-رفتاری (CBT)"] += 1
    elif text.startswith("۳"):
        scores["انسان‌گرا و وجودی"] += 1
    elif text.startswith("۴"):
        scores["سیستمی و رابطه‌ای (EFT)"] += 1
    else:
        await update.message.reply_text("👇 لطفاً یکی از ۴ گزینه پایین را لمس کنید:")
        return QUIZ_Q3

    q4_msg = """📍 `مسیر شما: آشنایی ساده با سبک‌های مشاوره ◀️ سوال ۴ از ۴ (آخرین سوال)`

**سوال ۴ از ۴:**
الان کدام جمله بیشتر شبیه حرف دل شماست؟"""

    kb = ReplyKeyboardMarkup(
        [
            ["۱. حس می‌کنم یک چیزهایی از گذشته هنوز روی رفتار امروزم سایه انداخته"],
            ["۲. مغزم از فکر و خیال خسته شده؛ یک راه علمی و عملی می‌خواهم"],
            ["۳. حس می‌کنم وسط زندگی گم شده‌ام و دنبال آرامش درونی و خودشناسی‌ام"],
            ["۴. بیشترین چیزی که حالم را بد کرده، فاصله یا مشکل با همسر/خانواده است"],
            [BACK_BTN],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text(q4_msg, parse_mode="Markdown", reply_markup=kb)
    return QUIZ_Q4

async def handle_quiz_q4(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await enter_client_mode(update, context)

    scores = context.user_data.get("quiz_scores", {
        "پویشی و تحلیلی": 0,
        "شناختی-رفتاری (CBT)": 0,
        "انسان‌گرا و وجودی": 0,
        "سیستمی و رابطه‌ای (EFT)": 0,
    })
    if text.startswith("۱"):
        scores["پویشی و تحلیلی"] += 1
    elif text.startswith("۲"):
        scores["شناختی-رفتاری (CBT)"] += 1
    elif text.startswith("۳"):
        scores["انسان‌گرا و وجودی"] += 1
    elif text.startswith("۴"):
        scores["سیستمی و رابطه‌ای (EFT)"] += 1
    else:
        await update.message.reply_text("👇 لطفاً یکی از ۴ گزینه پایین را لمس کنید:")
        return QUIZ_Q4

    best_approach = max(scores, key=scores.get)

    descriptions = {
        "پویشی و تحلیلی": "در این سبک، مشاور کمک می‌کند ریشه احساسات و رفتارهای تکراری امروزتان را در تجربیات گذشته و لایه‌های عمیق‌تر ذهن پیدا کنید تا تغییرات ماندگاری شکل بگیرد.",
        "شناختی-رفتاری (CBT)": "در این سبک، مشاور مثل یک همراه علمی کمک می‌کند فکرهای منفی و عادت‌هایی که استرس یا غم را نگه می‌دارند بشناسید و با راهکارهای عملی آن‌ها را تغییر دهید.",
        "انسان‌گرا و وجودی": "در این سبک، مشاور بدون قضاوت و برچسب زدن کنارتان می‌نشیند تا خودتان را بهتر بشناسید، معنای تازه‌ای برای زندگی پیدا کنید و با خیال راحت‌تر تصمیم بگیرید.",
        "سیستمی و رابطه‌ای (EFT)": "در این سبک، تمرکز اصلی روی رابطه‌ها، احساسات عمیق بین شما و عزیزانتان و باز کردن گره‌های عاطفی برای ساختن یک رابطه امن و صمیمی است.",
    }

    result_msg = f"""📍 `مسیر شما: آشنایی ساده با سبک‌های مشاوره ◀️ نتیجه اولیه`

✨ **به نظر می‌رسد سبک «{best_approach}» به حال‌وهوای فعلی شما نزدیک‌تر است.**

{descriptions[best_approach]}

━━━━━━━━━━━━━━━━━━
⚠️ **دوباره یادآوری می‌کنیم:**
این نتیجه فقط یک **راهنمای خیلی ساده و محدود** برای آشنایی اولیه است و **به هیچ عنوان جنبه تشخیصی ندارد**. برای اینکه دقیق بفهمید چه نوع درمانی برایتان مناسب است، موضوع حتماً باید به صورت عمیق‌تر در جلسه اول توسط درمانگر بررسی شود."""

    inline_kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🔍 دیدن درمانگران سبک {best_approach} در مشهد", callback_data=f"quizfilter_{best_approach}")],
            [InlineKeyboardButton("📋 دیدن همه تجربه‌های ثبت‌شده", callback_data="quizfilter_all")],
        ]
    )

    await update.message.reply_text(
        result_msg,
        parse_mode="Markdown",
        reply_markup=get_client_menu(update.effective_user.id),
    )
    await update.message.reply_text(
        "👇 اگر مایل هستید، می‌توانید درمانگران مرتبط را ببینید:",
        reply_markup=inline_kb,
    )
    return ConversationHandler.END

async def handle_quiz_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data.replace("quizfilter_", "")
    if data == "all":
        await send_search_results_to_chat(query.message, column=None, query_str=None)
    else:
        short_key = data.split(" ")[0]
        await send_search_results_to_chat(query.message, column="approach", query_str=short_key)

# ---------------------------------------------------------
# ۹. سفر ثبت تجربه مراجع (۴ قدم ساده و خطی)
# ---------------------------------------------------------
async def start_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = """📍 `مسیر شما: ثبت تجربه ◀️ قدم ۱ از ۴`

👤 **نام و نام خانوادگی درمانگر** خود را بنویسید:
✍️ مثال: رضا سعادت‌پور"""
    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return THERAPIST_NAME

async def get_therapist_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await enter_client_mode(update, context)

    cleaned_name = clean_therapist_name(text)
    if len(cleaned_name) < 3:
        await update.message.reply_text("⚠️ لطفاً نام و نام خانوادگی درمانگر را کامل‌تر وارد کنید:")
        return THERAPIST_NAME

    context.user_data["therapist_name"] = cleaned_name
    kb = await build_clinics_keyboard(include_add_new=True)
    await update.message.reply_text(
        f"📍 `مسیر شما: ثبت تجربه ◀️ قدم ۲ از ۴`\n\n🏥 **{cleaned_name}** در کدام کلینیک یا مرکز بودند؟ از دکمه‌های زیر انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return CLINIC_SELECT

async def select_clinic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await enter_client_mode(update, context)

    if text == "➕ نام کلینیک در لیست نیست":
        context.user_data["is_new_clinic"] = True
        await update.message.reply_text(
            "🏥 فقط **نام کلینیک** را بنویسید (نیازی به نوشتن آدرس یا شماره تلفن نیست):",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
        )
        return NEW_CLINIC_NAME

    if text == "مطب شخصی / نامشخص":
        context.user_data["clinic_name"] = "مطب شخصی / نامشخص"
        context.user_data["clinic_address"] = "ثبت نشده"
        context.user_data["clinic_phone"] = "ثبت نشده"
        context.user_data["is_new_clinic"] = False
    else:
        approved_clinics = await get_approved_clinic_names()
        if text not in approved_clinics:
            kb = await build_clinics_keyboard(include_add_new=True)
            await update.message.reply_text(
                "⚠️ لطفاً یکی از کلینیک‌های زیر را انتخاب کنید یا گزینه «➕ نام کلینیک در لیست نیست» را بزنید:",
                reply_markup=kb,
            )
            return CLINIC_SELECT

        context.user_data["clinic_name"] = text
        context.user_data["is_new_clinic"] = False

    await update.message.reply_text(
        "📍 `مسیر شما: ثبت تجربه ◀️ قدم ۳ از ۴`\n\n🎯 جلسات شما بیشتر در چه زمینه‌ای بود؟",
        parse_mode="Markdown",
        reply_markup=DOMAIN_KEYBOARD,
    )
    return THERAPY_DOMAIN

async def get_new_clinic_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await enter_client_mode(update, context)

    context.user_data["clinic_name"] = text
    context.user_data["clinic_address"] = "ثبت نشده"
    context.user_data["clinic_phone"] = "ثبت نشده"

    await update.message.reply_text(
        "📍 `مسیر شما: ثبت تجربه ◀️ قدم ۳ از ۴`\n\n🎯 جلسات شما بیشتر در چه زمینه‌ای بود؟",
        parse_mode="Markdown",
        reply_markup=DOMAIN_KEYBOARD,
    )
    return THERAPY_DOMAIN

async def get_therapy_domain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await enter_client_mode(update, context)

    if text not in ALLOWED_DOMAINS:
        await update.message.reply_text(
            "⚠️ لطفاً موضوع جلسات را از دکمه‌های زیر انتخاب کنید:",
            reply_markup=DOMAIN_KEYBOARD,
        )
        return THERAPY_DOMAIN

    context.user_data["therapy_domain"] = text
    t_name = context.user_data.get("therapist_name", "درمانگر")

    await update.message.reply_text(
        f"📍 `مسیر شما: ثبت تجربه ◀️ قدم ۴ از ۴ (مرحله آخر)`\n\n📝 تجربه واقعی و سازنده خودتان را از جلسات با **{t_name}** بنویسید:\n\n🔒 *هویت شما کاملاً ناشناس می‌ماند.*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return EXPERIENCE_TEXT

async def save_experience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    exp_text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in exp_text:
        return await enter_client_mode(update, context)

    data = context.user_data
    t_name = data.get("therapist_name", "نامشخص")
    clinic_name = data.get("clinic_name", "نامشخص")
    is_new_clinic = data.get("is_new_clinic", False)

    try:
        async with get_db() as client:
            if is_new_clinic and clinic_name not in ["مطب شخصی / نامشخص", "نامشخص"]:
                check_rs = await client.execute("SELECT id FROM clinics WHERE name = ?", [clinic_name])
                if not check_rs.rows:
                    await client.execute(
                        "INSERT INTO clinics (name, address, phone, status) VALUES (?, 'ثبت نشده', 'ثبت نشده', 'pending')",
                        [clinic_name],
                    )

            result = await client.execute(
                """
                INSERT INTO experiences (
                    therapist_name, clinic_name, clinic_address, clinic_phone,
                    neighborhood, therapy_domain, approach, experience_text, status
                )
                VALUES (?, ?, 'ثبت نشده', 'ثبت نشده', 'مشهد', ?, 'ثبت توسط مراجع', ?, 'pending')
                """,
                [
                    t_name,
                    clinic_name,
                    data.get("therapy_domain", "نامشخص"),
                    exp_text,
                ],
            )
            exp_id = result.last_insert_rowid or 1

        await update.message.reply_text(
            "✅ **سپاس از همراهی شما!**\nتجربه ارزشمندتان با موفقیت و به صورت کاملاً ناشناس ثبت شد و پس از تایید، در سامانه نمایش داده می‌شود 🌿",
            parse_mode="Markdown",
            reply_markup=get_client_menu(update.effective_user.id),
        )

        if ADMIN_ID:
            admin_msg = f"""🆕 **تجربه جدید ثبت شد (کد #{exp_id})**

👤 **درمانگر:** {t_name}
🏥 **کلینیک:** {clinic_name}
🎯 **حوزه درمان:** {data.get('therapy_domain')}
📝 **متن تجربه:**
{exp_text}"""
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

    except Exception as e:
        logger.error(f"Database error in save_experience: {e}")
        await update.message.reply_text(
            "⚠️ خطایی در ذخیره‌سازی رخ داد. لطفاً دوباره /start را بزنید.",
            reply_markup=get_client_menu(update.effective_user.id),
        )

    context.user_data.clear()
    return ConversationHandler.END

# ---------------------------------------------------------
# ۱۰. نمایش مینیمال تجربه‌ها + دکمه باز کردن اطلاعات کامل درمانگر
# ---------------------------------------------------------
async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "📍 `مسیر شما: پیدا کردن درمانگر ◀️ قدم ۱ از ۲`\n\n🔍 بر اساس کدام معیار می‌خواهید جستجو کنید؟",
        parse_mode="Markdown",
        reply_markup=SEARCH_TYPE_KEYBOARD,
    )
    return SEARCH_TYPE

async def select_search_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    if "بازگشت به منوی اصلی" in choice:
        return await enter_client_mode(update, context)

    if choice == "📋 آخرین تجربه‌های ثبت‌شده":
        return await show_search_results(update, context, column=None, query_str=None)

    if choice == "🩺 لیست درمانگران تاییدشده":
        await show_approved_therapists_list(update.message)
        return ConversationHandler.END

    context.user_data["search_mode"] = choice

    if choice == "👤 جستجو با نام درمانگر":
        await update.message.reply_text(
            "📍 `مسیر شما: پیدا کردن درمانگر ◀️ قدم ۲ از ۲`\n\n👤 نام یا نام خانوادگی درمانگر مورد نظر را بنویسید:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
        )
    elif choice == "🏥 جستجو با کلینیک":
        kb = await build_clinics_keyboard(include_add_new=False)
        await update.message.reply_text(
            "📍 `مسیر شما: پیدا کردن درمانگر ◀️ قدم ۲ از ۲`\n\n🏥 کلینیک مورد نظر را انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
    elif choice == "🎯 جستجو با موضوع (فردی، زوج...)":
        await update.message.reply_text(
            "📍 `مسیر شما: پیدا کردن درمانگر ◀️ قدم ۲ از ۲`\n\n🎯 موضوع مورد نظر را انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=DOMAIN_KEYBOARD,
        )
    elif choice == "🧠 جستجو با سبک درمان":
        await update.message.reply_text(
            "📍 `مسیر شما: پیدا کردن درمانگر ◀️ قدم ۲ از ۲`\n\n🧠 سبک درمانی مورد نظر را انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=APPROACH_KEYBOARD,
        )
    else:
        return await show_search_results(update, context, column="all", query_str=choice)

    return SEARCH_QUERY

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await enter_client_mode(update, context)

    mode = context.user_data.get("search_mode", "")
    col_map = {
        "👤 جستجو با نام درمانگر": "therapist_name",
        "🏥 جستجو با کلینیک": "clinic_name",
        "🎯 جستجو با موضوع (فردی، زوج...)": "therapy_domain",
        "🧠 جستجو با سبک درمان": "approach",
    }
    column = col_map.get(mode, "all")
    search_val = clean_therapist_name(text) if column == "therapist_name" else text
    return await show_search_results(update, context, column=column, query_str=search_val)

async def send_search_results_to_chat(message_obj, column: str | None, query_str: str | None):
    try:
        async with get_db() as client:
            base_sql = """
                SELECT id, therapist_name, experience_text
                FROM experiences
                WHERE status = 'approved'
            """
            if not query_str:
                rs = await client.execute(f"{base_sql} ORDER BY id DESC LIMIT 5")
            elif column and column != "all":
                q = f"%{query_str}%"
                rs = await client.execute(
                    f"{base_sql} AND {column} LIKE ? ORDER BY id DESC LIMIT 5",
                    [q],
                )
            else:
                q = f"%{query_str}%"
                rs = await client.execute(
                    f"""{base_sql} AND (
                        therapist_name LIKE ? OR
                        clinic_name LIKE ? OR
                        therapy_domain LIKE ? OR
                        approach LIKE ?
                    ) ORDER BY id DESC LIMIT 5""",
                    [q, q, q, q],
                )
            rows = rs.rows

        if not rows:
            no_res_msg = """😔 هنوز تجربه‌ای مطابق با انتخاب شما ثبت نشده است.
می‌توانید لیست درمانگران تاییدشده را ببینید یا گزینه دیگری را امتحان کنید."""
            await message_obj.reply_text(
                no_res_msg,
                reply_markup=get_client_menu(message_obj.chat.id),
            )
            return

        await message_obj.reply_text(
            f"🔎 **تجربه‌های یافت‌شده ({len(rows)} مورد اخیر):**",
            parse_mode="Markdown",
            reply_markup=get_client_menu(message_obj.chat.id),
        )

        for exp_id, t_name, exp_txt in rows:
            clean_card = (
                f"👤 **درمانگر:** {t_name}\n\n"
                f"💬 **تجربه مراجع:**\n{exp_txt}"
            )
            detail_btn = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📋 مشاهده پروفایل، آدرس و اطلاعات تماس درمانگر", callback_data=f"docinfo_{exp_id}")]
                ]
            )
            await message_obj.reply_text(
                clean_card,
                parse_mode="Markdown",
                reply_markup=detail_btn,
            )

    except Exception as e:
        logger.error(f"Search error: {e}")
        await message_obj.reply_text(
            "⚠️ خطایی در جستجو رخ داد. لطفاً دوباره تلاش کنید.",
            reply_markup=get_client_menu(message_obj.chat.id),
        )

async def show_therapist_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    exp_id = query.data.replace("docinfo_", "")
    try:
        async with get_db() as client:
            exp_rs = await client.execute(
                """
                SELECT e.therapist_name, e.clinic_name,
                       COALESCE(NULLIF(e.clinic_address, 'ثبت نشده'), c.address, 'ثبت نشده'),
                       COALESCE(NULLIF(e.clinic_phone, 'ثبت نشده'), c.phone, 'ثبت نشده'),
                       e.therapy_domain, e.approach
                FROM experiences e
                LEFT JOIN clinics c ON e.clinic_name = c.name
                WHERE e.id = ?
                """,
                [exp_id],
            )
            if not exp_rs.rows:
                await query.message.reply_text("⚠️ اطلاعات این پرونده یافت نشد.")
                return

            t_name, c_name, c_addr, c_phone, t_domain, t_approach = exp_rs.rows[0]

            doc_rs = await client.execute(
                """
                SELECT nezam_code, license_number, clinic_name, clinic_address, clinic_phone, therapy_domain, approach
                FROM therapists
                WHERE full_name LIKE ? AND status = 'approved'
                ORDER BY id DESC LIMIT 1
                """,
                [f"%{t_name}%"],
            )

            if doc_rs.rows:
                nezam, lic, d_clinic, d_addr, d_phone, d_dom, d_app = doc_rs.rows[0]
                g_url = build_google_search_url(t_name, d_clinic)
                profile_msg = f"""🩺 **پروفایل رسمی و تاییدشده درمانگر**

👤 **نام درمانگر:** {t_name}
🪪 **کد نظام روان‌شناسی:** `{nezam}`
📜 **شماره پروانه اشتغال:** `{lic}`
🧠 **رویکرد درمانی:** {d_app}
🎯 **حوزه فعالیت:** {d_dom}
🏥 **محل فعالیت:** {d_clinic}
🗺 **آدرس:** {d_addr}
☎️ **شماره تماس / هماهنگی:** `{d_phone}`"""
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 جستجو در نقشه و گوگل", url=g_url)]])
                await query.message.reply_text(profile_msg, parse_mode="Markdown", reply_markup=kb)
            else:
                has_addr = c_addr and c_addr != "ثبت نشده"
                has_phone = c_phone and c_phone != "ثبت نشده"
                addr_str = c_addr if has_addr else "هنوز توسط درمانگر ثبت نشده است"
                phone_str = f"`{c_phone}`" if has_phone else "هنوز توسط درمانگر ثبت نشده است"
                app_str = t_approach if t_approach and t_approach != "ثبت توسط مراجع" else "ثبت نشده"

                info_msg = f"""📋 **اطلاعات تکمیلی ثبت‌شده برای {t_name}**

🏥 **کلینیک / مرکز:** {c_name}
🎯 **حوزه درمان:** {t_domain}
🧠 **رویکرد:** {app_str}
🗺 **آدرس:** {addr_str}
☎️ **شماره تماس:** {phone_str}

ℹ️ *این درمانگر هنوز پروفایل رسمی خود را در سروش تکمیل نکرده است.*"""
                g_url = build_google_search_url(t_name, c_name)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🌐 جستجوی آدرس و تماس {t_name} در گوگل", url=g_url)]])
                await query.message.reply_text(info_msg, parse_mode="Markdown", reply_markup=kb)

    except Exception as e:
        logger.error(f"Error loading therapist details: {e}")
        await query.message.reply_text("⚠️ خطایی در دریافت اطلاعات رخ داد.")

async def show_approved_therapists_list(message_obj):
    try:
        async with get_db() as client:
            rs = await client.execute(
                """
                SELECT full_name, nezam_code, license_number, clinic_name, clinic_address, clinic_phone, therapy_domain, approach
                FROM therapists WHERE status = 'approved' ORDER BY id DESC LIMIT 10
                """
            )
            rows = rs.rows

        if not rows:
            await message_obj.reply_text(
                "🩺 هنوز درمانگری پروفایل رسمی خود را ثبت نکرده است.",
                reply_markup=get_client_menu(message_obj.chat.id),
            )
            return

        await message_obj.reply_text(
            f"🩺 **لیست درمانگران تاییدشده در مشهد ({len(rows)} نفر):**",
            parse_mode="Markdown",
            reply_markup=get_client_menu(message_obj.chat.id),
        )
        for r in rows:
            d_name, nezam, lic, c_name, c_addr, c_phone, dom, app = r
            g_url = build_google_search_url(d_name, c_name)
            msg = f"""🩺 **{d_name}**
🪪 کد نظام: `{nezam}` | 📜 پروانه: `{lic}`
🧠 رویکرد: {app} | 🎯 حوزه: {dom}
🏥 کلینیک: {c_name}
🗺 آدرس: {c_addr}
☎️ تماس: `{c_phone}`"""
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 مشاهده در گوگل", url=g_url)]])
            await message_obj.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.error(f"Error listing therapists: {e}")

async def show_search_results(
    update: Update, context: ContextTypes.DEFAULT_TYPE, column: str | None, query_str: str | None
) -> int:
    await send_search_results_to_chat(update.message, column, query_str)
    return ConversationHandler.END

# ---------------------------------------------------------
# ۱۱. بخش آموزشی راهنمای انتخاب آگاهانه درمانگر
# ---------------------------------------------------------
async def open_guide_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    guide_intro = """📍 `مسیر شما: راهنمای انتخاب درمانگر`

🧭 **راهنمای انتخاب آگاهانه درمانگر**
برای اینکه با خیال راحت‌تر تصمیم بگیرید، یکی از موضوعات زیر را انتخاب کنید 👇"""
    await update.message.reply_text(
        guide_intro,
        parse_mode="Markdown",
        reply_markup=GUIDE_KEYBOARD,
    )
    return GUIDE_MENU

async def handle_guide_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    if "بازگشت به منوی اصلی" in choice:
        return await enter_client_mode(update, context)

    if "اخلاق" in choice:
        text = """⚖️ **اخلاق حرفه‌ای و خطوط قرمز در اتاق درمان**

🔒 **رازدارای مطلق:** تمام صحبت‌های شما در اتاق درمان کاملاً محرمانه است.
🚧 **مرزبندی حرفه‌ای:** درمانگر متعهد هیچ رابطه دوستی، عاطفی یا مالی خارج از جلسات با مراجع برقرار نمی‌کند.
🤝 **احترام و عدم قضاوت:** اتاق درمان جای سرزنش یا تحمیل عقاید شخصی نیست."""
    elif "چک‌لیست" in choice:
        text = """🎓 **چک‌لیست انتخاب یک درمانگر حرفه‌ای**

🪪 **۱. شماره نظام و پروانه اشتغال:** مطمئن شوید درمانگر دارای پروانه کار معتبر از سازمان نظام روانشناسی است.
💚 **۲. احساس امنیت در جلسات اول:** مهم‌ترین نشانه یک درمانگر خوب برای شما، احساس امنیت و درک شدن در ۲ تا ۳ جلسه اول است.
⏳ **۳. پرهیز از وعده‌های معجزه‌آسا:** تغییر روانی تدریجی است؛ از افرادی که قول حل قطعی مشکل در یک جلسه را می‌دهند فاصله بگیرید."""
    elif "رویکرد" in choice:
        text = """🧠 **آشنایی خیلی ساده با سبک‌های اصلی مشاوره**

🌱 **انسان‌گرا و وجودی:** کمک به شناخت خودِ واقعی، پیدا کردن معنا در زندگی و تصمیم‌گیری آزادانه بدون قضاوت.
🧩 **شناختی-رفتاری (CBT):** یادگیری راهکارهای عملی و علمی برای کنترل فکرهای منفی، استرس و اضطراب.
🌊 **پویشی و تحلیلی:** ریشه‌یابی رفتارها و احساسات تکراری امروز در تجربه‌های گذشته و لایه‌های عمیق ذهن.
❤️ **سیستمی و رابطه‌ای (EFT):** ترمیم پیوندهای عاطفی و حل تعارض‌ها در خانواده و رابطه زوجی."""
    elif "نحوه کار" in choice:
        text = """📖 **سروش چطور کار می‌کند؟**

✍️ **ثبت تجربه ناشناس:** در ۴ قدم ساده و بدون ثبت نام، تجربه مثبت خود را از درمانگر ثبت کنید.
🩺 **پروفایل درمانگران:** درمانگران با ثبت شماره نظام و پروانه اشتغال، اطلاعات دقیق کلینیک و رویکرد خود را معرفی می‌کنند.
📞 **پشتیبانی:** @Reza_saadatpour"""
    else:
        text = "👇 لطفاً یکی از موضوعات زیر را انتخاب کنید:"

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=GUIDE_KEYBOARD,
    )
    return GUIDE_MENU

# ---------------------------------------------------------
# ۱۲. پنل مدیریت (تایید تجربه‌ها + تایید پروفایل درمانگران + فایل پشتیبان)
# ---------------------------------------------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    async with get_db() as client:
        exp_rs = await client.execute("SELECT id, therapist_name, clinic_name, therapy_domain, experience_text FROM experiences WHERE status = 'pending'")
        pending_exps = exp_rs.rows

        doc_rs = await client.execute("SELECT id, full_name, nezam_code, license_number, clinic_name, clinic_address, clinic_phone, therapy_domain, approach FROM therapists WHERE status = 'pending'")
        pending_docs = doc_rs.rows

    if not pending_exps and not pending_docs:
        await update.message.reply_text(
            "🎉 هیچ تجربه یا پروفایل درمانگری در صف انتظار تایید نیست!",
            reply_markup=ADMIN_KEYBOARD,
        )
        return

    for item in pending_exps:
        msg = f"""⏳ **تجربه در انتظار تایید (کد #{item[0]})**

👤 درمانگر: {item[1]}
🏥 کلینیک: {item[2]}
🎯 موضوع: {item[3]}
📝 متن: {item[4]}"""
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ تایید تجربه", callback_data=f"approve_{item[0]}"),
                InlineKeyboardButton("❌ رد کردن", callback_data=f"reject_{item[0]}"),
            ]]
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

    for doc in pending_docs:
        msg = f"""🩺 **پروفایل درمانگر در انتظار تایید (کد #{doc[0]})**

👤 نام: {doc[1]}
🪪 کد نظام: `{doc[2]}` | 📜 پروانه: `{doc[3]}`
🏥 کلینیک: {doc[4]}
🗺 آدرس: {doc[5]}
☎️ تماس: `{doc[6]}`
🎯 حوزه: {doc[7]} | 🧠 رویکرد: {doc[8]}"""
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ تایید پروفایل درمانگر", callback_data=f"docok_{doc[0]}"),
                InlineKeyboardButton("❌ رد کردن", callback_data=f"docno_{doc[0]}"),
            ]]
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

async def export_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    try:
        async with get_db() as client:
            exp_rs = await client.execute("SELECT * FROM experiences ORDER BY id ASC")
            clinics_rs = await client.execute("SELECT * FROM clinics ORDER BY id ASC")
            docs_rs = await client.execute("SELECT * FROM therapists ORDER BY id ASC")

        backup_data = {
            "exported_at": datetime.utcnow().isoformat(),
            "experiences": [list(r) for r in exp_rs.rows],
            "clinics": [list(c) for c in clinics_rs.rows],
            "therapists": [list(d) for d in docs_rs.rows],
        }

        json_bytes = json.dumps(backup_data, ensure_ascii=False, indent=2).encode("utf-8")
        bio = io.BytesIO(json_bytes)
        bio.name = f"soroush_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json"

        await update.message.reply_document(
            document=bio,
            caption=f"📦 فایل پشتیبان کامل دیتابیس سروش\n✅ تجربه‌ها: {len(exp_rs.rows)} | درمانگران: {len(docs_rs.rows)}",
            reply_markup=ADMIN_KEYBOARD,
        )
    except Exception as e:
        logger.error(f"Backup export error: {e}")
        await update.message.reply_text("⚠️ خطا در تهیه فایل پشتیبان.")

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != ADMIN_ID:
        return

    action, item_id = query.data.split("_")
    async with get_db() as client:
        if action == "approve":
            rs = await client.execute("SELECT clinic_name FROM experiences WHERE id = ?", [item_id])
            if rs.rows and rs.rows[0][0]:
                await client.execute("UPDATE clinics SET status = 'approved' WHERE name = ?", [rs.rows[0][0]])
            await client.execute("UPDATE experiences SET status = 'approved' WHERE id = ?", [item_id])
            await query.edit_message_text(f"{query.message.text}\n\n✅ **تجربه تایید و منتشر شد.**", parse_mode="Markdown")

        elif action == "reject":
            await client.execute("UPDATE experiences SET status = 'rejected' WHERE id = ?", [item_id])
            await query.edit_message_text(f"{query.message.text}\n\n❌ **تجربه رد شد.**", parse_mode="Markdown")

        elif action == "docok":
            await client.execute("UPDATE therapists SET status = 'approved' WHERE id = ?", [item_id])
            await query.edit_message_text(f"{query.message.text}\n\n✅ **پروفایل درمانگر تایید و در سامانه فعال شد.**", parse_mode="Markdown")

        elif action == "docno":
            await client.execute("UPDATE therapists SET status = 'rejected' WHERE id = ?", [item_id])
            await query.edit_message_text(f"{query.message.text}\n\n❌ **پروفایل درمانگر رد شد.**", parse_mode="Markdown")

# ---------------------------------------------------------
# ۱۳. اجرای هم‌زمان وب‌سرور و Webhook تلگرام
# ---------------------------------------------------------
async def health_check(request):
    return web.Response(text="Soroush Bot is running!", status=200)

async def run_bot_and_server() -> None:
    logger.info("Starting Soroush Bot initialization...")
    await init_db()

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is missing!")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("ثبت تجربه"), start_submit),
            MessageHandler(filters.Regex("پیدا کردن درمانگر|جستجوی درمانگر"), start_search),
            MessageHandler(filters.Regex("راهنمای انتخاب درمانگر"), open_guide_menu),
            MessageHandler(filters.Regex("کدام سبک مشاوره"), start_quiz),
            MessageHandler(filters.Regex("درمانگر هستم|ورود درمانگران"), start_therapist_registration),
        ],
        states={
            THERAPIST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_therapist_name)],
            CLINIC_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_clinic)],
            NEW_CLINIC_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_clinic_name)],
            THERAPY_DOMAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_therapy_domain)],
            EXPERIENCE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_experience)],
            SEARCH_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_search_type)],
            SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, perform_search)],
            GUIDE_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_guide_topic)],
            QUIZ_Q1: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_quiz_q1)],
            QUIZ_Q2: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_quiz_q2)],
            QUIZ_Q3: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_quiz_q3)],
            QUIZ_Q4: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_quiz_q4)],
            DOC_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_doc_name)],
            DOC_NEZAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_doc_nezam)],
            DOC_LICENSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_doc_license)],
            DOC_CLINIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_doc_clinic)],
            DOC_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_doc_address)],
            DOC_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_doc_phone)],
            DOC_DOMAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_doc_domain)],
            DOC_APPROACH: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_therapist_profile)],
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("بازگشت به منوی اصلی"), start),
        ],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", open_guide_menu))
    application.add_handler(MessageHandler(filters.Regex("مراجع هستم"), enter_client_mode))
    application.add_handler(MessageHandler(filters.Regex("بازگشت به منوی اصلی"), start))
    application.add_handler(MessageHandler(filters.Regex("پنل مدیریت و تاییدها"), admin_panel))
    application.add_handler(MessageHandler(filters.Regex("دریافت فایل پشتیبان"), export_backup))
    application.add_handler(CallbackQueryHandler(handle_quiz_filter_callback, pattern="^quizfilter_"))
    application.add_handler(CallbackQueryHandler(show_therapist_details_callback, pattern="^docinfo_"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^(approve|reject|docok|docno)_"))
    application.add_handler(conv_handler)
    application.add_handler(
        MessageHandler(
            filters.Regex("اخلاق حرفه‌ای|چک‌لیست انتخاب|رویکردهای درمانی|نحوه کار با ربات"),
            handle_guide_topic,
        )
    )

    webhook_path = f"/{BOT_TOKEN}"
    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}{webhook_path}"

    async def telegram_webhook(request):
        try:
            data = await request.json()
            update = Update.de_json(data, application.bot)
            await application.process_update(update)
        except Exception as e:
            logger.error(f"Webhook update error: {e}")
        return web.Response(text="OK", status=200)

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_post(webhook_path, telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}.")

    try:
        await application.initialize()
        await application.start()
        await application.bot.set_webhook(url=webhook_url)
        logger.info("Telegram Webhook set and bot started successfully!")
    except Exception as e:
        logger.exception(f"Telegram startup warning: {e}")

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(run_bot_and_server())
