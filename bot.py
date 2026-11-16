import os
import re
import time
import hashlib
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import libsql_client
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    BotCommand,
)
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

# --- کش ضد اسپم برای جلوگیری از هنگ با /start مکرر ---
LAST_START_TIME = {}


def hash_user_id(user_id: int) -> str:
    salt = BOT_TOKEN[:12] if BOT_TOKEN else "soroush_salt"
    return hashlib.sha256(f"{salt}:{user_id}".encode("utf-8")).hexdigest()[:24]


def sanitize_text(text: str, max_len: int = 600) -> str:
    cleaned = re.sub(r"[*_`\[\]]", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len]


def build_two_column_keyboard(items, prefix):
    keyboard = []
    row = []
    for item in items:
        row.append(InlineKeyboardButton(item, callback_data=f"{prefix}{item}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return keyboard


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
            user_telegram_id TEXT NOT NULL,
            issue_category TEXT NOT NULL,
            sessions_count TEXT NOT NULL,
            experience_text TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (therapist_id) REFERENCES therapists(id)
        );
    """)
    client.execute("""
        CREATE TABLE IF NOT EXISTS banned_users (
            user_hash TEXT PRIMARY KEY,
            banned_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

DISTRICTS = [
    "📍 احمدآباد",
    "📍 سجاد",
    "📍 وکیل‌آباد",
    "📍 هاشمیه",
    "📍 کوهسنگی",
    "📍 قاسم‌آباد و الهیه",
    "📍 معلم و دانشجو",
    "📍 امامت و آزادشهر",
    "📍 راهنمایی و فلسطین",
    "📍 ملک‌آباد و آبکوه",
    "🗺 سایر مناطق مشهد",
]

APPROACHES = [
    "🌿 انسان‌گرا / وجودی",
    "🧩 شناختی-رفتاری (CBT)",
    "🛋 روانکاوی / پویشی",
    "🧱 طرح‌واره‌درمانی",
    "❤️ هیجان‌مدار (EFT)",
    "🌊 اکت (ACT / پذیرش و تعهد)",
    "👨‍👩‍👧‍👦 خانواده‌درمانی",
    "🧸 کودک و بازی‌درمانی",
    "❓ نمی‌دانم / مطمئن نیستم",
]

ISSUES = [
    "🌧 اضطراب و افسردگی",
    "💑 زوج و خانواده",
    "🧭 توسعه فردی و معناجویی",
    "🎈 کودک و نوجوان",
    "✨ سایر موضوعات",
]

SESSIONS = [
    "🌱 ۱ تا ۳ جلسه",
    "🌿 ۴ تا ۱۰ جلسه",
    "🌳 بیش از ۱۰ جلسه",
]


def persistent_reply_keyboard(is_admin: bool = False):
    """کیبورد ثابت پایین صفحه برای دسترسی فوری بدون نیاز به تایپ"""
    rows = [
        ["🔍 جستجوی درمانگر", "✍️ ثبت تجربه"],
        ["ℹ️ راهنمای سروش"],
    ]
    if is_admin:
        rows.append(["📊 پنل مدیریت و گزارش‌ها"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def main_menu_keyboard(is_admin: bool = False):
    buttons = [
        [InlineKeyboardButton("🔍 جستجوی درمانگر در مشهد", callback_data="browse_start")],
        [InlineKeyboardButton("✍️ معرفی درمانگر و ثبت تجربه", callback_data="submit_start")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton("📊 پنل مدیریت و گزارش‌ها", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = time.time()

    # جلوگیری از هنگ یا اسپم با زدن پشت‌سرهم /start (حداقل فاصله ۱.۵ ثانیه)
    if update.message and (now - LAST_START_TIME.get(user_id, 0) < 1.5):
        return ConversationHandler.END
    LAST_START_TIME[user_id] = now

    context.user_data.clear()
    is_admin = (user_id == ADMIN_ID)

    text = (
        "سلام! به **سروش** خوش آمدید 🌱✨\n\n"
        "«سروش» فضایی امن و کاملاً ناشناس برای به اشتراک گذاشتن تجربه‌های واقعی و مثبت مراجعان در مشهد است، "
        "تا انتخاب درمانگر برای شما شفاف‌تر و آگاهانه‌تر باشد 💚\n\n"
        "👇 از دکمه‌های زیر استفاده کنید:"
    )
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=persistent_reply_keyboard(is_admin),
            parse_mode="Markdown",
        )
        await update.message.reply_text(
            "📌 **منوی سریع:**",
            reply_markup=main_menu_keyboard(is_admin),
            parse_mode="Markdown",
        )
    else:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=main_menu_keyboard(is_admin), parse_mode="Markdown"
            )
        except Exception:
            pass
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = (update.effective_user.id == ADMIN_ID)
    text = (
        "ℹ️ **راهنمای استفاده از سروش**\n\n"
        "1️⃣ **جستجوی درمانگر:** می‌توانید بر اساس موضوع مراجعه (اضطراب، زوج، کودک و...) درمانگران معرفی‌شده در مشهد را ببینید و تجربه مراجعان را بخوانید.\n"
        "2️⃣ **ثبت تجربه:** اگر تجربه مثبت و مفیدی با یک روانشناس یا مشاور داشته‌اید، نام ایشان و تجربه خود را ثبت کنید.\n"
        "3️⃣ **امنیت و ناشناس بودن:** هویت شما کاملاً رمزنگاری و ناشناس می‌ماند و هیچ اطلاعات شخصی منتشر نمی‌شود 🔐"
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard(is_admin), parse_mode="Markdown")
    return ConversationHandler.END


# --- شروع فرآیند ثبت تجربه (هم از دکمه شیشه‌ای و هم از کیبورد پایین) ---
async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = (user_id == ADMIN_ID)
    user_hash = hash_user_id(user_id)

    if update.callback_query:
        await update.callback_query.answer()

    client = get_db()
    ban_res = client.execute("SELECT 1 FROM banned_users WHERE user_hash = ?", [user_hash])
    if ban_res.rows:
        client.close()
        msg = "⛔ دسترسی شما به بخش ثبت تجربه محدود شده است."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=main_menu_keyboard(is_admin))
        else:
            await update.message.reply_text(msg, reply_markup=main_menu_keyboard(is_admin))
        return ConversationHandler.END

    rate_res = client.execute(
        "SELECT COUNT(*) FROM experiences WHERE user_telegram_id = ? AND created_at >= datetime('now', '-1 day')",
        [user_hash],
    )
    client.close()

    if rate_res.rows and rate_res.rows[0][0] >= 2:
        msg = (
            "⏳ هر کاربر می‌تواند حداکثر **۲ تجربه در شبانه‌روز** ثبت کند.\n"
            "لطفاً فردا دوباره تلاش کنید 🌷"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=main_menu_keyboard(is_admin), parse_mode="Markdown")
        else:
            await update.message.reply_text(msg, reply_markup=main_menu_keyboard(is_admin), parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data.clear()
    prompt_text = (
        "👤 نام یا نام خانوادگی درمانگر را بنویسید (بدون پیشوند دکتر):\n"
        "✍️ مثال: علی رضایی\n\n"
        "💡 برای انصراف در هر مرحله می‌توانید /start را بزنید."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(prompt_text)
    else:
        await update.message.reply_text(prompt_text)
    return SEARCH_THERAPIST


async def search_therapist_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # اگر کاربر وسط کار دکمه‌های منوی پایین را زد، محترمانه از مکالمه خارج شو
    if text in ["🔍 جستجوی درمانگر", "ℹ️ راهنمای سروش", "📊 پنل مدیریت و گزارش‌ها"]:
        if text == "🔍 جستجوی درمانگر":
            await browse_start(update, context)
        elif text == "ℹ️ راهنمای سروش":
            await help_command(update, context)
        else:
            await admin_panel(update, context)
        return ConversationHandler.END

    raw_name = text.replace("دکتر ", "").replace("دکتر", "")
    name_query = sanitize_text(raw_name, max_len=50)

    if len(name_query) < 3:
        await update.message.reply_text("⚠️ نام وارد شده بسیار کوتاه است. لطفاً نام و نام خانوادگی را کامل‌تر بنویسید:")
        return SEARCH_THERAPIST

    context.user_data["temp_name"] = name_query

    client = get_db()
    res = client.execute(
        "SELECT id, full_name, clinic_name, district FROM therapists WHERE full_name LIKE ? LIMIT 10",
        [f"%{name_query}%"],
    )
    client.close()

    keyboard = []
    for row in res.rows:
        t_id, f_name, clinic, dist = row
        label = f"🩺 {f_name} ({clinic or 'مطب شخصی'} | {dist or 'مشهد'})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"select_t_{t_id}")])

    keyboard.append([InlineKeyboardButton(f"➕ ثبت درمانگر جدید: «{name_query}»", callback_data="new_therapist")])
    keyboard.append([InlineKeyboardButton("🔙 انصراف و بازگشت", callback_data="cancel_flow")])

    await update.message.reply_text(
        "🔎 اگر درمانگر مورد نظر شما در لیست زیر هست انتخاب کنید، در غیر این صورت دکمه ثبت جدید را بزنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_OR_NEW


async def select_or_new_therapist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    is_admin = (query.from_user.id == ADMIN_ID)

    if data == "cancel_flow":
        return await start(update, context)

    if data.startswith("select_t_"):
        t_id = int(data.split("_")[2])
        user_hash = hash_user_id(query.from_user.id)

        client = get_db()
        dup_res = client.execute(
            "SELECT 1 FROM experiences WHERE therapist_id = ? AND user_telegram_id = ? AND status != 'rejected'",
            [t_id, user_hash],
        )
        client.close()

        if dup_res.rows:
            await query.edit_message_text(
                "⚠️ شما قبلاً برای این درمانگر یک تجربه ثبت کرده‌اید 🌿",
                reply_markup=main_menu_keyboard(is_admin),
            )
            return ConversationHandler.END

        context.user_data["therapist_id"] = t_id
        context.user_data["is_new"] = False
        return await ask_issue(query)

    if data == "new_therapist":
        context.user_data["is_new"] = True
        await query.edit_message_text(
            "🏥 نام کلینیک یا مرکز مشاوره‌ای که ایشان فعالیت دارند را بنویسید:\n"
            "💡 (اگر مطب خصوصی دارند، بنویسید: مطب شخصی)"
        )
        return NEW_CLINIC


async def get_new_clinic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clinic = sanitize_text(update.message.text, max_len=60)
    if len(clinic) < 2:
        await update.message.reply_text("⚠️ لطفاً نام کلینیک یا عبارت «مطب شخصی» را وارد کنید:")
        return NEW_CLINIC

    context.user_data["clinic_name"] = clinic
    keyboard = build_two_column_keyboard(DISTRICTS, "dist_")
    await update.message.reply_text(
        "🗺 محدوده کلینیک یا مطب در مشهد کجاست؟",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return NEW_DISTRICT


async def get_new_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["district"] = query.data.replace("dist_", "")
    keyboard = [[InlineKeyboardButton(a, callback_data=f"app_{a}")] for a in APPROACHES]
    await query.edit_message_text(
        "🧠 رویکرد درمانی ایشان چیست؟\n"
        "💡 (اگر مطمئن نیستید گزینه آخر را انتخاب کنید)",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return NEW_APPROACH


async def get_new_approach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["approach"] = query.data.replace("app_", "")
    return await ask_issue(query)


async def ask_issue(query):
    keyboard = [[InlineKeyboardButton(i, callback_data=f"issue_{i}")] for i in ISSUES]
    await query.edit_message_text(
        "📌 بیشتر برای چه موضوعی به ایشان مراجعه کردید؟",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_ISSUE


async def get_issue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["issue"] = query.data.replace("issue_", "")
    keyboard = [[InlineKeyboardButton(s, callback_data=f"sess_{s}")] for s in SESSIONS]
    await query.edit_message_text(
        "🗓 تا الان حدوداً چند جلسه با ایشان داشته‌اید؟",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_SESSIONS


async def get_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["sessions"] = query.data.replace("sess_", "")
    await query.edit_message_text(
        "✍️ در چند خط تجربه‌ی خودتان را بنویسید (بین ۲۰ تا ۶۰۰ کاراکتر):\n"
        "مثلاً چه ویژگی، فضا یا روشی در جلسات ایشان برای شما مفید و راهگشا بود؟ 🌱\n\n"
        "⚠️ لطفاً از نوشتن شماره تماس، آدرس دقیق یا اطلاعات خصوصی خودداری کنید."
    )
    return WRITE_EXPERIENCE


async def save_experience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    exp_text = sanitize_text(update.message.text, max_
