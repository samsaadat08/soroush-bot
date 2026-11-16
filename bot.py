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
    exp_text = sanitize_text(update.message.text, max_len=600)
    is_admin = (update.effective_user.id == ADMIN_ID)

    if len(exp_text) < 20:
        await update.message.reply_text(
            "⚠️ متن تجربه بسیار کوتاه است (حداقل ۲۰ کاراکتر). لطفاً کمی بیشتر توضیح دهید:"
        )
        return WRITE_EXPERIENCE

    user_hash = hash_user_id(update.effective_user.id)
    data = context.user_data

    client = get_db()
    if data.get("is_new"):
        res = client.execute(
            "INSERT INTO therapists (full_name, clinic_name, district, approach) VALUES (?, ?, ?, ?) RETURNING id",
            [data["temp_name"], data["clinic_name"], data["district"], data["approach"]],
        )
        t_id = res.rows[0][0]
        t_info = f"{data['temp_name']} ({data['clinic_name']} | {data['district']}) — رویکرد: {data['approach']}"
    else:
        t_id = data["therapist_id"]
        res = client.execute("SELECT full_name, clinic_name, district, approach FROM therapists WHERE id = ?", [t_id])
        row = res.rows[0]
        t_info = f"{row[0]} ({row[1]} | {row[2]}) — رویکرد: {row[3]}"

    exp_res = client.execute(
        """INSERT INTO experiences (therapist_id, user_telegram_id, issue_category, sessions_count, experience_text, status)
           VALUES (?, ?, ?, ?, ?, 'pending') RETURNING id""",
        [t_id, user_hash, data["issue"], data["sessions"], exp_text],
    )
    exp_id = exp_res.rows[0][0]
    client.close()

    await update.message.reply_text(
        "✅ تجربه‌ی ارزشمند شما با موفقیت ثبت شد و پس از بررسی ادمین منتشر می‌شود 💚🌱",
        reply_markup=main_menu_keyboard(is_admin),
    )

    if ADMIN_ID:
        admin_msg = (
            f"📩 **تجربه جدید در انتظار تایید (کد #{exp_id})**\n\n"
            f"🩺 **درمانگر:** {t_info}\n"
            f"📌 **موضوع:** {data['issue']}\n"
            f"🗓 **تعداد جلسات:** {data['sessions']}\n"
            f"🔐 **شناسه ناشناس کاربر:** `{user_hash}`\n\n"
            f"💬 **متن تجربه:**\n«{exp_text}»"
        )
        admin_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ تایید و انتشار", callback_data=f"adm_approve_{exp_id}"),
                InlineKeyboardButton("❌ رد کردن", callback_data=f"adm_reject_{exp_id}"),
            ],
            [
                InlineKeyboardButton("🚫 رد و مسدود کردن کاربر", callback_data=f"adm_ban_{exp_id}"),
            ]
        ])
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, reply_markup=admin_kb, parse_mode="Markdown")

    return ConversationHandler.END

# --- پنل مدیریت و گزارش‌گیری ادمین با چند کلیک ---
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    if update.callback_query:
        await update.callback_query.answer()

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 آمار کلی سیستم", callback_data="rep_summary"),
            InlineKeyboardButton("⏳ در انتظار تایید", callback_data="rep_pending"),
        ],
        [
            InlineKeyboardButton("🏆 برترین درمانگران", callback_data="rep_top"),
            InlineKeyboardButton("🗺 گزارش مناطق و موضوعات", callback_data="rep_stats"),
        ],
        [InlineKeyboardButton("🏠 بازگشت به منوی اصلی", callback_data="back_main")],
    ])
    text = "📊 **پنل مدیریت و گزارش‌های سروش**\n\n👇 گزارش مورد نظر را انتخاب کنید:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")

async def handle_admin_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ دسترسی غیرمجاز", show_alert=True)
        return

    await query.answer()
    rep_type = query.data
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به پنل مدیریت", callback_data="admin_panel")]])

    client = get_db()

    if rep_type == "rep_summary":
        t_count = client.execute("SELECT COUNT(*) FROM therapists").rows[0][0]
        app_count = client.execute("SELECT COUNT(*) FROM experiences WHERE status = 'approved'").rows[0][0]
        pend_count = client.execute("SELECT COUNT(*) FROM experiences WHERE status = 'pending'").rows[0][0]
        rej_count = client.execute("SELECT COUNT(*) FROM experiences WHERE status = 'rejected'").rows[0][0]
        ban_count = client.execute("SELECT COUNT(*) FROM banned_users").rows[0][0]
        users_count = client.execute("SELECT COUNT(DISTINCT user_telegram_id) FROM experiences").rows[0][0]
        client.close()

        msg = (
            "📈 **گزارش جامع وضعیت سروش**\n"
            "━━━━━━━━━━━━━━\n"
            f"🩺 **تعداد کل درمانگران ثبت‌شده:** {t_count} نفر\n"
            f"👥 **تعداد مراجعان مشارکت‌کننده:** {users_count} نفر\n\n"
            f"✅ **تجربه‌های منتشرشده:** {app_count} مورد\n"
            f"⏳ **در انتظار تایید شما:** {pend_count} مورد\n"
            f"❌ **تجربه‌های ردشده:** {rej_count} مورد\n"
            f"🚫 **کاربران مسدودشده:** {ban_count} کاربر"
        )
        await query.edit_message_text(msg, reply_markup=back_kb, parse_mode="Markdown")

    elif rep_type == "rep_pending":
        res = client.execute("""
            SELECT e.id, t.full_name, e.issue_category, e.experience_text
            FROM experiences e
            JOIN therapists t ON e.therapist_id = t.id
            WHERE e.status = 'pending'
            ORDER BY e.id ASC LIMIT 1
        """)
        client.close()

        if not res.rows:
            await query.edit_message_text("🎉 هیچ تجربه‌ای در صف انتظار نیست! همه‌چیز بررسی شده است.", reply_markup=back_kb)
            return

        exp_id, f_name, issue, exp_text = res.rows[0]
        msg = (
            f"⏳ **تجربه در انتظار بررسی (کد #{exp_id})**\n\n"
            f"🩺 **درمانگر:** {f_name}\n"
            f"📌 **موضوع:** {issue}\n\n"
            f"💬 **متن:**\n«{exp_text}»"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ تایید و انتشار", callback_data=f"adm_approve_{exp_id}"),
                InlineKeyboardButton("❌ رد کردن", callback_data=f"adm_reject_{exp_id}"),
            ],
            [InlineKeyboardButton("🔙 بازگشت به پنل مدیریت", callback_data="admin_panel")],
        ])
        await query.edit_message_text(msg, reply_markup=kb, parse_mode="Markdown")

    elif rep_type == "rep_top":
        res = client.execute("""
            SELECT t.full_name, t.district, COUNT(e.id) as cnt
            FROM therapists t
            JOIN experiences e ON t.id = e.therapist_id
            WHERE e.status = 'approved'
            GROUP BY t.id
            ORDER BY cnt DESC LIMIT 10
        """)
        client.close()

        if not res.rows:
            await query.edit_message_text("هنوز تجربه تاییدشده‌ای ثبت نشده است.", reply_markup=back_kb)
            return

        msg = "🏆 **درمانگران با بیشترین تجربه تاییدشده در مشهد:**\n━━━━━━━━━━━━━━\n"
        for idx, row in enumerate(res.rows, 1):
            msg += f"**{idx}. {row[0]}** ({row[1]}) — {row[2]} تجربه\n"
        await query.edit_message_text(msg, reply_markup=back_kb, parse_mode="Markdown")

    elif rep_type == "rep_stats":
        dist_res = client.execute("""
            SELECT t.district, COUNT(e.id) as cnt
            FROM therapists t
            JOIN experiences e ON t.id = e.therapist_id
            WHERE e.status = 'approved'
            GROUP BY t.district ORDER BY cnt DESC
        """)
        issue_res = client.execute("""
            SELECT issue_category, COUNT(id) as cnt
            FROM experiences
            WHERE status = 'approved'
            GROUP BY issue_category ORDER BY cnt DESC
        """)
        client.close()

        msg = "🗺 **تفکیک تجربه‌های تاییدشده بر اساس منطقه:**\n"
        for r in dist_res.rows:
            msg += f"• {r[0]}: {r[1]} مورد\n"
        msg += "\n📌 **تفکیک بر اساس موضوع مراجعه:**\n"
        for r in issue_res.rows:
            msg += f"• {r[0]}: {r[1]} مورد\n"
        if not dist_res.rows and not issue_res.rows:
            msg = "هنوز داده‌ای برای نمایش وجود ندارد."

        await query.edit_message_text(msg, reply_markup=back_kb, parse_mode="Markdown")

async def admin_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ شما دسترسی ادمین ندارید.", show_alert=True)
        return

    await query.answer()
    parts = query.data.split("_")
    action, exp_id = parts[1], int(parts[2])

    client = get_db()
    if action == "ban":
        res = client.execute("SELECT user_telegram_id FROM experiences WHERE id = ?", [exp_id])
        if res.rows:
            u_hash = res.rows[0][0]
            client.execute("INSERT OR IGNORE INTO banned_users (user_hash) VALUES (?)", [u_hash])
        client.execute("UPDATE experiences SET status = 'rejected' WHERE id = ?", [exp_id])
        status_text = "🚫 رد شد و کاربر برای همیشه مسدود گردید."
    else:
        new_status = "approved" if action == "approve" else "rejected"
        client.execute("UPDATE experiences SET status = ? WHERE id = ?", [new_status, exp_id])
        status_text = "✅ تایید و منتشر شد." if action == "approve" else "❌ رد شد."

    client.close()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📊 رفتن به پنل مدیریت", callback_data="admin_panel")]])
    await query.edit_message_text(
        f"{query.message.text}\n\n━━━━━━━━━━━━━━\n📋 نتیجه بررسی: {status_text}",
        reply_markup=kb,
    )

async def browse_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = (update.effective_user.id == ADMIN_ID)
    if update.callback_query:
        await update.callback_query.answer()

    keyboard = [[InlineKeyboardButton(i, callback_data=f"filter_issue_{i}")] for i in ISSUES]
    keyboard.append([InlineKeyboardButton("📋 نمایش همه درمانگران تاییدشده", callback_data="filter_all")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_main")])

    text = "🔍 برای چه موضوعی دنبال درمانگر در مشهد هستید؟"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_therapists_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    is_admin = (query.from_user.id == ADMIN_ID)

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
            "🌱 هنوز تجربه تاییدشده‌ای در این دسته‌بندی ثبت نشده است.\n"
            "شما می‌توانید اولین نفری باشید که تجربه مثبت خود را ثبت می‌کند!",
            reply_markup=main_menu_keyboard(is_admin),
        )
        return

    keyboard = []
    for row in res.rows:
        t_id, f_name, clinic, dist, app, count = row
        btn_text = f"🩺 {f_name} | {dist} ({count} تجربه 💬)"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"view_t_{t_id}")])

    keyboard.append([InlineKeyboardButton("🔙 بازگشت به دسته‌بندی‌ها", callback_data="browse_start")])
    await query.edit_message_text(
        "✨ درمانگران معرفی‌شده بر اساس تعداد تجربه‌های ثبت‌شده:\n"
        "👇 برای دیدن جزئیات و نظرات روی هر نام کلیک کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def view_therapist_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    t_id = int(query.data.replace("view_t_", ""))
    is_admin = (query.from_user.id == ADMIN_ID)

    client = get_db()
    t_res = client.execute("SELECT full_name, clinic_name, district, approach FROM therapists WHERE id = ?", [t_id])
    e_res = client.execute(
        "SELECT issue_category, sessions_count, experience_text FROM experiences WHERE therapist_id = ? AND status = 'approved' ORDER BY id DESC LIMIT 5",
        [t_id],
    )
    client.close()

    if not t_res.rows:
        await query.edit_message_text("⚠️ اطلاعات یافت نشد.", reply_markup=main_menu_keyboard(is_admin))
        return

    f_name, clinic, dist, app = t_res.rows[0]
    msg = (
        f"🩺 **نام درمانگر:** {f_name}\n"
        f"🏥 **مرکز / کلینیک:** {clinic} ({dist})\n"
        f"🧠 **رویکرد:** {app}\n"
        f"📊 **تعداد تجربه‌های ثبت‌شده:** {len(e_res.rows)} مورد\n"
        f"━━━━━━━━━━━━━━\n"
        f"💬 **تجربه‌ی مراجعان:**\n\n"
    )

    for idx, exp in enumerate(e_res.rows, 1):
        issue, sess, text = exp
        msg += f"**{idx}. موضوع: {issue}** ({sess})\n«{text}»\n\n"

    keyboard = [
        [InlineKeyboardButton("🔙 بازگشت به لیست درمانگران", callback_data="browse_start")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main")],
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    # ثبت دستورات ساده در منوی آبی تلگرام
    await app.bot.set_my_commands([
        BotCommand("start", "🏠 منوی اصلی و شروع"),
        BotCommand("help", "ℹ️ راهنمای استفاده از سروش"),
    ])

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(submit_start, pattern="^submit_start$"),
            MessageHandler(filters.Regex("^✍️ ثبت تجربه$"), submit_start),
        ],
        states={
            SEARCH_THERAPIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_therapist_name)],
            SELECT_OR_NEW: [CallbackQueryHandler(select_or_new_therapist)],
            NEW_CLINIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_clinic)],
            NEW_DISTRICT: [CallbackQueryHandler(get_new_district, pattern="^dist_")],
            NEW_APPROACH: [CallbackQueryHandler(get_new_approach, pattern="^app_")],
