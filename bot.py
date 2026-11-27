import os
import io
import sys
import json
import asyncio
import logging
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
# ۳. اتصال ناهمزمان (Async) به دیتابیس Turso
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
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.exception(f"Database init error (continuing startup): {e}")

# ---------------------------------------------------------
# ۴. تعریف مراحل ساده و خطی سفر کاربر (UX)
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
) = range(12)

BACK_BTN = "🏠 بازگشت به منوی اصلی"
GUIDE_BTN = "🧭 راهنمای انتخاب درمانگر"
QUIZ_BTN = "🧩 کدام سبک مشاوره به من می‌خورد؟"
BACKUP_BTN = "📥 دریافت فایل پشتیبان (Backup)"

ALLOWED_NEIGHBORHOODS = [
    "بلوار سجاد",
    "وکیل‌آباد",
    "احمدآباد",
    "فلسطین و ملک‌آباد",
    "هاشمیه",
    "کوهسنگی",
    "معلم و دانشجو",
    "قاسم‌آباد و الهیه",
    "سایر مناطق مشهد",
]

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
]

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔍 پیدا کردن درمانگر در مشهد", "✍️ ثبت تجربه من از درمان"],
        [QUIZ_BTN],
        [GUIDE_BTN],
    ],
    resize_keyboard=True,
)

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔍 پیدا کردن درمانگر در مشهد", "✍️ ثبت تجربه من از درمان"],
        [QUIZ_BTN, GUIDE_BTN],
        ["📊 پنل مدیریت و گزارش‌ها", BACKUP_BTN],
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
        ["فلسطین و ملک‌آباد", "هاشمیه", "کوهسنگی"],
        ["معلم و دانشجو", "قاسم‌آباد و الهیه"],
        ["سایر مناطق مشهد"],
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
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

SEARCH_TYPE_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["👤 جستجو با نام درمانگر", "🏥 جستجو با کلینیک"],
        ["🎯 جستجو با موضوع (فردی، زوج...)", "🧠 جستجو با سبک درمان"],
        ["📍 جستجو با محله در مشهد", "📋 آخرین تجربه‌های ثبت‌شده"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

def get_menu_keyboard(user_id: int):
    return ADMIN_KEYBOARD if user_id == ADMIN_ID else MAIN_KEYBOARD

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
# ۵. نقطه شروع (/start) - شفاف و بدون شلوغی
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user = update.effective_user
    welcome_text = """سلام، به **سروش** خوش آمدید 🌿
مرجع اشتراک تجربه واقعی مراجعان و انتخاب آگاهانه درمانگر در مشهد (کاملاً ناشناس).

الان چطور می‌توانم کمکتان کنم؟ یکی از گزینه‌های پایین را لمس کنید 👇"""
    await update.message.reply_text(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=get_menu_keyboard(user.id),
    )
    return ConversationHandler.END

# ---------------------------------------------------------
# ۶. سفر اول: آزمون کاملاً ساده و روزمره ۴ رویکرد اساسی (غیرتشخیصی)
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
        return await start(update, context)

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
        return await start(update, context)

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
        return await start(update, context)

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
        return await start(update, context)

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
            [InlineKeyboardButton("📋 دیدن همه درمانگران مشهد", callback_data="quizfilter_all")],
        ]
    )

    await update.message.reply_text(
        result_msg,
        parse_mode="Markdown",
        reply_markup=get_menu_keyboard(update.effective_user.id),
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
# ۷. سفر دوم: ثبت تجربه ساده و سریع (فقط ۳ قدم شفاف بدون درگیر کردن کاربر با آدرس و تلفن)
# ---------------------------------------------------------
async def start_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = """📍 `مسیر شما: ثبت تجربه ◀️ قدم ۱ از ۳`

👤 **نام و نام خانوادگی درمانگر** خود را بنویسید (بدون پیشوند دکتر):
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
        return await start(update, context)

    if len(text) < 3:
        await update.message.reply_text("⚠️ لطفاً نام و نام خانوادگی درمانگر را کامل‌تر بنویسید:")
        return THERAPIST_NAME

    context.user_data["therapist_name"] = text
    kb = await build_clinics_keyboard(include_add_new=True)
    await update.message.reply_text(
        f"📍 `مسیر شما: ثبت تجربه ◀️ قدم ۲ از ۳`\n\n🏥 **دکتر/آقای {text}** در کدام کلینیک یا مرکز بودند؟ از دکمه‌های زیر انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return CLINIC_SELECT

async def select_clinic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

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
        try:
            async with get_db() as client:
                rs = await client.execute("SELECT address, phone FROM clinics WHERE name = ?", [text])
                if rs.rows:
                    context.user_data["clinic_address"] = rs.rows[0][0] or "ثبت نشده"
                    context.user_data["clinic_phone"] = rs.rows[0][1] or "ثبت نشده"
        except Exception:
            pass

    await update.message.reply_text(
        "📍 `مسیر شما: ثبت تجربه ◀️ قدم ۲ از ۳ (انتخاب موضوع)`\n\n🎯 جلسات شما بیشتر در چه زمینه‌ای بود؟",
        parse_mode="Markdown",
        reply_markup=DOMAIN_KEYBOARD,
    )
    return THERAPY_DOMAIN

async def get_new_clinic_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

    context.user_data["clinic_name"] = text
    context.user_data["clinic_address"] = "ثبت نشده"
    context.user_data["clinic_phone"] = "ثبت نشده"

    await update.message.reply_text(
        "📍 `مسیر شما: ثبت تجربه ◀️ قدم ۲ از ۳ (انتخاب موضوع)`\n\n🎯 جلسات شما بیشتر در چه زمینه‌ای بود؟",
        parse_mode="Markdown",
        reply_markup=DOMAIN_KEYBOARD,
    )
    return THERAPY_DOMAIN

async def get_therapy_domain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

    if text not in ALLOWED_DOMAINS:
        await update.message.reply_text(
            "⚠️ لطفاً موضوع جلسات را از دکمه‌های زیر انتخاب کنید:",
            reply_markup=DOMAIN_KEYBOARD,
        )
        return THERAPY_DOMAIN

    context.user_data["therapy_domain"] = text
    t_name = context.user_data.get("therapist_name", "درمانگر")

    await update.message.reply_text(
        f"📍 `مسیر شما: ثبت تجربه ◀️ قدم ۳ از ۳ (پایان)`\n\n📝 تجربه واقعی و سازنده خودتان را از جلسات با **{t_name}** بنویسید (مثلاً: فضای امن، میزان همدلی، نتیجه‌ای که گرفتید و...):\n\n🔒 *هویت شما کاملاً ناشناس می‌ماند.*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return EXPERIENCE_TEXT

async def save_experience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    exp_text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in exp_text:
        return await start(update, context)

    data = context.user_data
    clinic_name = data.get("clinic_name", "نامشخص")
    clinic_address = data.get("clinic_address", "ثبت نشده")
    clinic_phone = data.get("clinic_phone", "ثبت نشده")
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
                VALUES (?, ?, ?, ?, 'مشهد', ?, 'ثبت توسط مراجع', ?, 'pending')
                """,
                [
                    data.get("therapist_name", "نامشخص"),
                    clinic_name,
                    clinic_address,
                    clinic_phone,
                    data.get("therapy_domain", "نامشخص"),
                    exp_text,
                ],
            )
            exp_id = result.last_insert_rowid or 1

        await update.message.reply_text(
            "✅ **سپاس از همراهی شما!**\nتجربه ارزشمندتان با موفقیت و به صورت کاملاً ناشناس ثبت شد و پس از تایید، در سامانه نمایش داده می‌شود 🌿",
            parse_mode="Markdown",
            reply_markup=get_menu_keyboard(update.effective_user.id),
        )

        if ADMIN_ID:
            new_clinic_tag = "🆕 (کلینیک جدید)" if is_new_clinic else ""
            admin_msg = f"""🆕 **تجربه جدید ثبت شد (کد #{exp_id})**

👤 **درمانگر:** {data.get('therapist_name')}
🏥 **کلینیک:** {clinic_name} {new_clinic_tag}
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
            reply_markup=get_menu_keyboard(update.effective_user.id),
        )

    context.user_data.clear()
    return ConversationHandler.END

# ---------------------------------------------------------
# ۸. سفر سوم: جستجوی سریع و شفاف درمانگر (۲ قدم تا نتیجه)
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
        return await start(update, context)

    if choice == "📋 آخرین تجربه‌های ثبت‌شده":
        return await show_search_results(update, context, column=None, query_str=None)

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
    elif choice == "📍 جستجو با محله در مشهد":
        await update.message.reply_text(
            "📍 `مسیر شما: پیدا کردن درمانگر ◀️ قدم ۲ از ۲`\n\n📍 محله مورد نظر را انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=NEIGHBORHOOD_KEYBOARD,
        )
    else:
        return await show_search_results(update, context, column="all", query_str=choice)

    return SEARCH_QUERY

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in text:
        return await start(update, context)

    mode = context.user_data.get("search_mode", "")
    col_map = {
        "👤 جستجو با نام درمانگر": "therapist_name",
        "🏥 جستجو با کلینیک": "clinic_name",
        "🎯 جستجو با موضوع (فردی، زوج...)": "therapy_domain",
        "🧠 جستجو با سبک درمان": "approach",
        "📍 جستجو با محله در مشهد": "neighborhood",
    }
    column = col_map.get(mode, "all")
    return await show_search_results(update, context, column=column, query_str=text)

async def send_search_results_to_chat(message_obj, column: str | None, query_str: str | None):
    try:
        async with get_db() as client:
            base_sql = """
                SELECT e.therapist_name, e.clinic_name,
                       COALESCE(NULLIF(e.clinic_address, 'ثبت نشده'), c.address, 'ثبت نشده'),
                       COALESCE(NULLIF(e.clinic_phone, 'ثبت نشده'), c.phone, 'ثبت نشده'),
                       e.neighborhood, e.therapy_
