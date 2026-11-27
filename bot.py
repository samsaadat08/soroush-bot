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
# ۱. تنظیمات لاگ‌گیری
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
    if clinic_name and clinic_name not in ["مطب شخصی / نامشخص", "نامشخص", ""]:
        query = f"{clinic_name} {therapist_name} مشهد آدرس و شماره تماس"
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

            default_clinics = [
                ("کلینیک هیمان", "مشهد - بلوار پیروزی، حد فاصل میدان هاشمیه تا چهارراه کوثر، بین پیروزی ۳۴ و ۳۶، پلاک ۱۱۲۶", "09921883558"),
                ("کلینیک پناه", "مشهد - محدوده بلوار سجاد / احمدآباد", "ثبت نشده"),
                ("پلی‌کلینیک دانشگاه فردوسی", "مشهد - پردیس دانشگاه فردوسی", "ثبت نشده"),
            ]
            for c_name, c_addr, c_phone in default_clinics:
                try:
                    check_rs = await client.execute("SELECT id FROM clinics
