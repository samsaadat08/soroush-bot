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
# ۳. توابع کمکی پاک‌سازی نام و لینک جستجوی گوگل
# ---------------------------------------------------------
def clean_therapist_name(raw_name: str) -> str:
    name = raw_name.strip()
    prefixes = ["دکتر ", "آقای ", "خانم ", "استاد ", "دکتر", "آقای", "خانم"]
    for p in prefixes:
        if name.startswith(p):
            name = name[len(p):].strip()
    return name

def build_google_search_url(therapist_name: str, clinic_name: str) -> str:
    if clinic_name and clinic_name not in ["مطب شخصی / نامشخص", "نامشخص"]:
        query = f"{clinic_name} مشهد آدرس و شماره تماس"
    else:
        query = f"دکتر {therapist_name} روانشناس مشهد آدرس و شماره تماس"
    return f"https://www.google.com/search?q={quote(query)}"

# ---------------------------------------------------------
# ۴. اتصال ناهمزمان (Async) به دیتابیس Turso
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
                ("کلینیک هیمان", "مشهد - بلوار پیروزی، حد فاصل میدان هاشمیه تا چهارراه کوثر، بین پیروزی ۳۴ و ۳۶، پلاک ۱۱۲۶", "09921883558"),
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
# ۵. تعریف مراحل ساده و خطی سفر کاربر (UX)
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
# ۶. نقطه شروع (/start) - شفاف و بدون شلوغی
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
# ۷. سفر اول: آزمون کاملاً ساده و روزمره ۴ رویکرد اساسی (غیرتشخیصی)
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
# ۸. سفر دوم: ثبت تجربه ساده و خطی (۴ قدم کاملاً شفاف: ۱ از ۴ تا ۴ از ۴)
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
        return await start(update, context)

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
        "📍 `مسیر شما: ثبت تجربه ◀️ قدم ۳ از ۴`\n\n🎯 جلسات شما بیشتر در چه زمینه‌ای بود؟",
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
        "📍 `مسیر شما: ثبت تجربه ◀️ قدم ۳ از ۴`\n\n🎯 جلسات شما بیشتر در چه زمینه‌ای بود؟",
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
        f"📍 `مسیر شما: ثبت تجربه ◀️ قدم ۴ از ۴ (مرحله آخر)`\n\n📝 تجربه واقعی و سازنده خودتان را از جلسات با **{t_name}** بنویسید (مثلاً: فضای امن، میزان همدلی، نتیجه‌ای که گرفتید و...):\n\n🔒 *هویت شما کاملاً ناشناس می‌ماند.*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return EXPERIENCE_TEXT

async def save_experience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    exp_text = update.message.text.strip()
    if "بازگشت به منوی اصلی" in exp_text:
        return await start(update, context)

    data = context.user_data
    t_name = data.get("therapist_name", "نامشخص")
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
                    t_name,
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
            google_url = build_google_search_url(t_name, clinic_name)
            admin_msg = f"""🆕 **تجربه جدید ثبت شد (کد #{exp_id})**

👤 **درمانگر:** {t_name}
🏥 **کلینیک:** {clinic_name} {new_clinic_tag}
🎯 **حوزه درمان:** {data.get('therapy_domain')}
📝 **متن تجربه:**
{exp_text}

💡 *برای ثبت آدرس کلینیک در دیتابیس:*
`/setclinic {clinic_name} | آدرس | تلفن`"""
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ تایید و انتشار", callback_data=f"approve_{exp_id}"),
                        InlineKeyboardButton("❌ رد کردن", callback_data=f"reject_{exp_id}"),
                    ],
                    [
                        InlineKeyboardButton("🔍 جستجوی آدرس در گوگل", url=google_url)
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
# ۹. سفر سوم: جستجوی سریع و شفاف درمانگر + دکمه هوشمند گوگل برای آدرس‌های ثبت‌نشده
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
    search_val = clean_therapist_name(text) if column == "therapist_name" else text
    return await show_search_results(update, context, column=column, query_str=search_val)

async def send_search_results_to_chat(message_obj, column: str | None, query_str: str | None):
    try:
        async with get_db() as client:
            base_sql = """
                SELECT e.therapist_name, e.clinic_name,
                       COALESCE(NULLIF(e.clinic_address, 'ثبت نشده'), c.address, 'ثبت نشده'),
                       COALESCE(NULLIF(e.clinic_phone, 'ثبت نشده'), c.phone, 'ثبت نشده'),
                       e.neighborhood, e.therapy_domain, e.approach, e.experience_text
                FROM experiences e
                LEFT JOIN clinics c ON e.clinic_name = c.name
                WHERE e.status = 'approved'
            """
            if not query_str:
                rs = await client.execute(f"{base_sql} ORDER BY e.id DESC LIMIT 5")
            elif column and column != "all":
                q = f"%{query_str}%"
                rs = await client.execute(
                    f"{base_sql} AND e.{column} LIKE ? ORDER BY e.id DESC LIMIT 5",
                    [q],
                )
            else:
                q = f"%{query_str}%"
                rs = await client.execute(
                    f"""{base_sql} AND (
                        e.therapist_name LIKE ? OR
                        e.clinic_name LIKE ? OR
                        e.neighborhood LIKE ? OR
                        e.therapy_domain LIKE ? OR
                        e.approach LIKE ?
                    ) ORDER BY e.id DESC LIMIT 5""",
                    [q, q, q, q, q],
                )
            rows = rs.rows

        if not rows:
            no_res_msg = """😔 هنوز تجربه‌ای مطابق با انتخاب شما ثبت نشده است.
می‌توانید گزینه دیگری را امتحان کنید یا اگر خودتان تجربه‌ای دارید ثبت نمایید."""
            await message_obj.reply_text(
                no_res_msg,
                reply_markup=get_menu_keyboard(message_obj.chat.id),
            )
            return

        await message_obj.reply_text(
            f"🔎 **نتایج یافت‌شده ({len(rows)} مورد اخیر):**",
            parse_mode="Markdown",
            reply_markup=get_menu_keyboard(message_obj.chat.id),
        )

        for r in rows:
            t_name, c_name, c_addr, c_phone, _, t_domain, t_approach, exp_txt = r
            has_addr = c_addr and c_addr != "ثبت نشده"
            has_phone = c_phone and c_phone != "ثبت نشده"

            addr_line = f"\n🗺 **آدرس:** {c_addr}" if has_addr else ""
            phone_line = f"\n☎️ **تلفن:** `{c_phone}`" if has_phone else ""
            approach_str = f" | 🧠 **سبک:** {t_approach}" if t_approach and t_approach != "ثبت توسط مراجع" else ""

            card_text = (
                f"👤 **درمانگر:** {t_name}\n"
                f"🏥 **مرکز/کلینیک:** {c_name}{addr_line}{phone_line}\n"
                f"🎯 **موضوع:** {t_domain}{approach_str}\n"
                f"💬 **تجربه مراجع:**\n{exp_txt}"
            )

            # اگر آدرس یا تلفن در دیتابیس ثبت نشده بود، دکمه جستجوی خودکار در گوگل نمایش داده می‌شود
            inline_buttons = []
            if not has_addr or not has_phone:
                g_url = build_google_search_url(t_name, c_name)
                btn_label = f"🌐 جستجوی آدرس و تماس «{c_name}» در گوگل" if c_name not in ["مطب شخصی / نامشخص", "نامشخص"] else f"🌐 جستجوی اطلاعات «{t_name}» در گوگل"
                inline_buttons.append([InlineKeyboardButton(btn_label, url=g_url)])

            reply_markup_inline = InlineKeyboardMarkup(inline_buttons) if inline_buttons else None
            await message_obj.reply_text(
                card_text,
                parse_mode="Markdown",
                reply_markup=reply_markup_inline,
            )

    except Exception as e:
        logger.error(f"Search error: {e}")
        await message_obj.reply_text(
            "⚠️ خطایی در جستجو رخ داد. لطفاً دوباره تلاش کنید.",
            reply_markup=get_menu_keyboard(message_obj.chat.id),
        )

async def show_search_results(
    update: Update, context: ContextTypes.DEFAULT_TYPE, column: str | None, query_str: str | None
) -> int:
    await send_search_results_to_chat(update.message, column, query_str)
    return ConversationHandler.END

# ---------------------------------------------------------
# ۱۰. بخش آموزشی راهنمای انتخاب آگاهانه درمانگر
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
        return await start(update, context)

    if "اخلاق" in choice:
        text = """⚖️ **اخلاق حرفه‌ای و خطوط قرمز در اتاق درمان**

🔒 **رازدارای مطلق:** تمام صحبت‌های شما در اتاق درمان کاملاً محرمانه است.
🚧 **مرزبندی حرفه‌ای:** درمانگر متعهد هیچ رابطه دوستی، عاطفی یا مالی خارج از جلسات با مراجع برقرار نمی‌کند.
🤝 **احترام و عدم قضاوت:** اتاق درمان جای سرزنش یا تحمیل عقاید شخصی نیست."""
    elif "چک‌لیست" in choice:
        text = """🎓 **چک‌لیست انتخاب یک درمانگر حرفه‌ای**

🪪 **۱. شماره نظام روانشناسی:** مطمئن شوید درمانگر دارای پروانه کار معتبر از سازمان نظام روانشناسی است.
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
🔍 **جستجوی هوشمند:** بر اساس نام درمانگر، کلینیک یا موضوع، تجربه‌های واقعی مراجعان مشهد را بخوانید.
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
# ۱۱. پنل مدیریت، دستور سریع ثبت آدرس کلینیک و فایل پشتیبان
# ---------------------------------------------------------
async def set_clinic_info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    raw_text = update.message.text.replace("/setclinic", "").strip()
    parts = [p.strip() for p in raw_text.split("|")]
    if len(parts) < 2:
        await update.message.reply_text(
            "فرمت دستور:\n`/setclinic نام کلینیک | آدرس دقیق | شماره تلفن`",
            parse_mode="Markdown",
        )
        return
    c_name = parts[0]
    c_addr = parts[1] if len(parts) > 1 and parts[1] else "ثبت نشده"
    c_phone = parts[2] if len(parts) > 2 and parts[2] else "ثبت نشده"

    async with get_db() as client:
        rs = await client.execute("SELECT id FROM clinics WHERE name = ?", [c_name])
        if rs.rows:
            await client.execute(
                "UPDATE clinics SET address = ?, phone = ?, status = 'approved' WHERE name = ?",
                [c_addr, c_phone, c_name],
            )
        else:
            await client.execute(
                "INSERT INTO clinics (name, address, phone, status) VALUES (?, ?, ?, 'approved')",
                [c_name, c_addr, c_phone],
            )
    await update.message.reply_text(
        f"✅ اطلاعات **{c_name}** در دیتابیس ذخیره شد و از این پس زیر کارت درمانگران این مرکز نمایش داده می‌شود.",
        parse_mode="Markdown",
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    async with get_db() as client:
        count_rs = await client.execute("SELECT COUNT(*) FROM experiences WHERE status = 'approved'")
        approved_count = count_rs.rows[0][0] if count_rs.rows else 0

        rs = await client.execute(
            """
            SELECT id, therapist_name, clinic_name, therapy_domain, experience_text
            FROM experiences WHERE status = 'pending'
            """
        )
        pending = rs.rows

    if not pending:
        await update.message.reply_text(
            f"🎉 هیچ تجربه‌ای در صف انتظار نیست!\n📚 تعداد کل تجربه‌های تاییدشده در دیتابیس: {approved_count} مورد.",
            reply_markup=ADMIN_KEYBOARD,
        )
        return

    for item in pending:
        g_url = build_google_search_url(item[1], item[2])
        msg = f"""⏳ **در انتظار تایید (کد #{item[0]})**

👤 درمانگر: {item[1]}
🏥 کلینیک: {item[2]}
🎯 موضوع: {item[3]}
📝 متن: {item[4]}"""
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ تایید و انتشار", callback_data=f"approve_{item[0]}"),
                    InlineKeyboardButton("❌ رد کردن", callback_data=f"reject_{item[0]}"),
                ],
                [
                    InlineKeyboardButton("🔍 جستجوی آدرس در گوگل", url=g_url)
                ]
            ]
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

async def export_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    try:
        async with get_db() as client:
            exp_rs = await client.execute(
                """
                SELECT id, therapist_name, clinic_name, clinic_address, clinic_phone,
                       neighborhood, therapy_domain, approach, experience_text, status, created_at
                FROM experiences ORDER BY id ASC
                """
            )
            clinics_rs = await client.execute(
                "SELECT id, name, address, phone, status FROM clinics ORDER BY id ASC"
            )

        backup_data = {
            "exported_at": datetime.utcnow().isoformat(),
            "total_experiences": len(exp_rs.rows),
            "experiences": [
                {
                    "id": r[0],
                    "therapist_name": r[1],
                    "clinic_name": r[2],
                    "clinic_address": r[3],
                    "clinic_phone": r[4],
                    "neighborhood": r[5],
                    "therapy_domain": r[6],
                    "approach": r[7],
                    "experience_text": r[8],
                    "status": r[9],
                    "created_at": r[10],
                }
                for r in exp_rs.rows
            ],
            "clinics": [
                {
                    "id": c[0],
                    "name": c[1],
                    "address": c[2],
                    "phone": c[3],
                    "status": c[4],
                }
                for c in clinics_rs.rows
            ],
        }

        json_bytes = json.dumps(backup_data, ensure_ascii=False, indent=2).encode("utf-8")
        bio = io.BytesIO(json_bytes)
        bio.name = f"soroush_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json"

        await update.message.reply_document(
            document=bio,
            caption=f"📦 فایل پشتیبان کامل دیتابیس سروش\n✅ تعداد کل تجربه‌های ثبت‌شده: {len(exp_rs.rows)} مورد",
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

    action, exp_id = query.data.split("_")
    async with get_db() as client:
        if action == "approve":
            rs = await client.execute("SELECT clinic_name FROM experiences WHERE id = ?", [exp_id])
            if rs.rows and rs.rows[0][0]:
                c_name = rs.rows[0][0]
                await client.execute("UPDATE clinics SET status = 'approved' WHERE name = ?", [c_name])

            await client.execute("UPDATE experiences SET status = 'approved' WHERE id = ?", [exp_id])
            await query.edit_message_text(
                f"{query.message.text}\n\n✅ **تایید و منتشر شد.**",
                parse_mode="Markdown",
            )
        elif action == "reject":
            await client.execute("UPDATE experiences SET status = 'rejected' WHERE id = ?", [exp_id])
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ **رد شد.**", parse_mode="Markdown"
            )

# ---------------------------------------------------------
# ۱۲. اجرای هم‌زمان وب‌سرور و Webhook تلگرام
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
            MessageHandler(filters.Regex("راهنمای انتخاب درمانگر|راهنمای انتخاب آگاهانه"), open_guide_menu),
            MessageHandler(filters.Regex("کدام سبک مشاوره"), start_quiz),
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
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("بازگشت به منوی اصلی"), start),
        ],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", open_guide_menu))
    application.add_handler(CommandHandler("setclinic", set_clinic_info_cmd))
    application.add_handler(MessageHandler(filters.Regex("بازگشت به منوی اصلی"), start))
    application.add_handler(MessageHandler(filters.Regex("پنل مدیریت و گزارش"), admin_panel))
    application.add_handler(MessageHandler(filters.Regex("دریافت فایل پشتیبان"), export_backup))
    application.add_handler(CallbackQueryHandler(handle_quiz_filter_callback, pattern="^quizfilter_"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^(approve|reject)_"))
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
