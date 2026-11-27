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

            # بررسی و مهاجرت امن جدول therapists در صورت وجود نسخه قدیمی
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
        ["طرحواره‌درمانی", "پذیر
