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
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL", "https://soroush-bot-w2q5.onrender.com"
)
PORT = int(os.environ.get("PORT", "8080"))

# ---------------------------------------------------------
# ۳. اتصال و همگام‌سازی جداول دیتابیس Turso
# ---------------------------------------------------------
def get_db():
    db_url = TURSO_DATABASE_URL.replace("libsql://", "https://")
    return libsql_client.create_client_sync(
        url=db_url,
        auth_token=TURSO_AUTH_TOKEN,
    )

def init_db():
    with get_db() as client:
        # جدول تجربه‌ها (همراه با آدرس و شماره تلفن کلینیک، حوزه درمان و رویکرد)
        client.execute("""
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
                client.execute(f"ALTER TABLE experiences ADD COLUMN {col} TEXT")
            except Exception:
                pass

        # جدول هوشمند کلینیک‌ها، آدرس‌ها و شماره تلفن‌ها
        client.execute("""
            CREATE TABLE IF NOT EXISTS clinics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                address TEXT,
                phone TEXT,
                status TEXT DEFAULT 'approved'
            )
        """)
        for col in ["address", "phone", "status"]:
            try:
                client.execute(f"ALTER TABLE clinics ADD COLUMN {col} TEXT")
            except Exception:
                pass

        # افزودن کلینیک‌های پیش‌فرض در صورت خالی بودن
        default_clinics = [
            ("کلینیک هیمان", "مشهد - بلوار هاشمیه", "ثبت نشده"),
            ("کلینیک پناه", "مشهد - محدوده بلوار سجاد / احمدآباد", "ثبت نشده"),
            ("پلی‌کلینیک دانشگاه فردوسی", "مشهد - پردیس دانشگاه فردوسی", "ثبت نشده"),
        ]
        for c_name, c_addr, c_phone in default_clinics:
            try:
                client.execute(
                    "INSERT OR IGNORE INTO clinics (name, address, phone, status) VALUES (?, ?, ?, 'approved')",
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
) = range(12)

BACK_BTN = "🔙 بازگشت به منوی اصلی"

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

def build_clinics_keyboard(include_add_new: bool = True) -> ReplyKeyboardMarkup:
    buttons = []
    try:
        with get_db() as client:
            rs = client.execute("SELECT name FROM clinics WHERE status = 'approved' ORDER BY id ASC")
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
        "اطلاعات کلینیک، آدرس و شماره تماس پس از تایید به دکمه‌های ثابت ربات اضافه می‌شود.\n"
        "• **🔍 جستجوی درمانگر:** بر اساس حوزه درمان (فردی، زوج، کودک...)، رویکرد درمانی (انسان‌گرا، وجودی، طرحواره، EFT...)، "
        "کلینیک، محله یا نام درمانگر جستجو کنید و به اطلاعات تماس کلینیک دسترسی داشته باشید.\n\n"
        "📞 پشتیبانی: @Reza_saadatpour"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

# ---------------------------------------------------------
# ۶. فرآیند گزینه‌ای ثبت تجربه جدید + سیستم هوشمند کلینیک، آدرس و تلفن
# ---------------------------------------------------------
async def start_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👤 نام و نام خانوادگی درمانگر را بنویسید (بدون پیشوند دکتر):\n"
        "✍️ مثال: علی رضایی",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return THERAPIST_NAME

async def get_therapist_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["therapist_name"] = text
    await update.message.reply_text(
        "🏥 کلینیک را از لیست زیر انتخاب کنید (اگر در لیست نیست، گزینه «➕ ثبت کلینیک جدید» را بزنید):",
        reply_markup=build_clinics_keyboard(include_add_new=True),
    )
    return CLINIC_SELECT

async def select_clinic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    if text == "➕ ثبت کلینیک جدید":
        context.user_data["is_new_clinic"] = True
        await update.message.reply_text(
            "🏥 نام کلینیک یا مرکز مشاوره جدید را بنویسید:",
            reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
        )
        return NEW_CLINIC_NAME

    if text == "مطب شخصی / نامشخص":
        context.user_data["clinic_name"] = "مطب شخصی / نامشخص"
        context.user_data["clinic_address"] = "ثبت نشده"
        context.user_data["clinic_phone"] = "ثبت نشده"
        await update.message.reply_text(
            "📍 محله یا محدوده کلینیک/مطب در مشهد را انتخاب کنید:",
            reply_markup=NEIGHBORHOOD_KEYBOARD,
        )
        return NEIGHBORHOOD

    # جستجوی کلینیک در دیتابیس برای بررسی وجود آدرس و تلفن قبلی
    context.user_data["clinic_name"] = text
    existing_address = None
    existing_phone = None
    try:
        with get_db() as client:
            rs = client.execute("SELECT address, phone FROM clinics WHERE name = ?", [text])
            if rs.rows:
                existing_address = rs.rows[0][0]
                existing_phone = rs.rows[0][1]
    except Exception as e:
        logger.error(f"Error fetching clinic info: {e}")

    has_addr = existing_address and existing_address != "ثبت نشده"
    has_phone = existing_phone and existing_phone != "ثبت نشده"

    if has_addr or has_phone:
        context.user_data["clinic_address"] = existing_address or "ثبت نشده"
        context.user_data["clinic_phone"] = existing_phone or "ثبت نشده"
        addr_display = existing_address if has_addr else "هنوز ثبت نشده"
        phone_display = existing_phone if has_phone else "هنوز ثبت نشده"

        await update.message.reply_text(
            f"🏥 اطلاعات ثبت‌شده برای **{text}**:\n"
            f"🗺 **آدرس:** {addr_display}\n"
            f"☎️ **شماره تماس:** {phone_display}\n\n"
            "آیا این اطلاعات را تایید می‌کنید یا می‌خواهید آدرس و شماره تماس را تکمیل/اصلاح کنید؟",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(
                [
                    ["✅ بله، تایید اطلاعات کلینیک"],
                    ["✏️ تکمیل / اصلاح آدرس و تلفن"],
                    [BACK_BTN],
                ],
                resize_keyboard=True,
            ),
        )
        return CONFIRM_EXISTING_CLINIC
    else:
        context.user_data["is_new_clinic"] = True
        await update.message.reply_text(
            f"🗺 آدرس یا محدوده دقیق **{text}** را بنویسید (تا برای مراجعان بعدی هم ذخیره شود):",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(
                [["⏭ رد کردن آدرس"], [BACK_BTN]],
                resize_keyboard=True,
            ),
        )
        return NEW_CLINIC_ADDRESS

async def get_new_clinic_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["clinic_name"] = text
    await update.message.reply_text(
        f"🗺 آدرس کلینیک **{text}** را بنویسید (مثال: بلوار سجاد، بزرگمهر جنوبی ۲، پلاک ۱۲):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [["⏭ رد کردن آدرس"], [BACK_BTN]],
            resize_keyboard=True,
        ),
    )
    return NEW_CLINIC_ADDRESS

async def get_new_clinic_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["clinic_address"] = "ثبت نشده" if text == "⏭ رد کردن آدرس" else text
    clinic_name = context.user_data.get("clinic_name", "کلینیک")
    await update.message.reply_text(
        f"☎️ شماره تلفن پذیرش یا نوبت‌دهی **{clinic_name}** را بنویسید (مثال: 05137654321 یا 0915...):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [["⏭ رد کردن شماره تلفن"], [BACK_BTN]],
            resize_keyboard=True,
        ),
    )
    return NEW_CLINIC_PHONE

async def get_new_clinic_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["clinic_phone"] = "ثبت نشده" if text == "⏭ رد کردن شماره تلفن" else text
    await update.message.reply_text(
        "📍 محله یا محدوده کلینیک در مشهد را از گزینه‌های زیر انتخاب کنید:",
        reply_markup=NEIGHBORHOOD_KEYBOARD,
    )
    return NEIGHBORHOOD

async def confirm_existing_clinic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    if text == "✏️ تکمیل / اصلاح آدرس و تلفن":
        context.user_data["is_new_clinic"] = True
        await update.message.reply_text(
            "🗺 آدرس جدید یا تکمیلی کلینیک را بنویسید:",
            reply_markup=ReplyKeyboardMarkup(
                [["⏭ رد کردن آدرس"], [BACK_BTN]],
                resize_keyboard=True,
            ),
        )
        return NEW_CLINIC_ADDRESS

    await update.message.reply_text(
        "📍 محله یا محدوده کلینیک در مشهد را انتخاب کنید:",
        reply_markup=NEIGHBORHOOD_KEYBOARD,
    )
    return NEIGHBORHOOD

async def get_neighborhood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["neighborhood"] = text
    await update.message.reply_text(
        "🎯 **حوزه درمان** شما در جلسات چه بوده است؟ از گزینه‌های زیر انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=DOMAIN_KEYBOARD,
    )
    return THERAPY_DOMAIN

async def get_therapy_domain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["therapy_domain"] = text
    await update.message.reply_text(
        "🧠 **رویکرد درمانی** درمانگر چه بوده است؟ از گزینه‌های زیر انتخاب کنید (اگر نمی‌دانید گزینه «سایر رویکردها / نامشخص» را بزنید):",
        parse_mode="Markdown",
        reply_markup=APPROACH_KEYBOARD,
    )
    return APPROACH

async def get_approach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["approach"] = text
    await update.message.reply_text(
        "📝 تجربه مثبت و سازنده خود را از روند درمان با این درمانگر بنویسید (هویت شما کاملاً ناشناس می‌ماند):",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return EXPERIENCE_TEXT

async def save_experience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    exp_text = update.message.text.strip()
    if exp_text == BACK_BTN:
        return await start(update, context)

    data = context.user_data
    clinic_name = data.get("clinic_name", "نامشخص")
    clinic_address = data.get("clinic_address", "ثبت نشده")
    clinic_phone = data.get("clinic_phone", "ثبت نشده")
    is_new_clinic = data.get("is_new_clinic", False)

    try:
        with get_db() as client:
            if is_new_clinic and clinic_name not in ["مطب شخصی / نامشخص", "نامشخص"]:
                client.execute(
                    """
                    INSERT INTO clinics (name, address, phone, status)
                    VALUES (?, ?, ?, 'pending')
                    ON CONFLICT(name) DO UPDATE SET
                        address = CASE WHEN excluded.address != 'ثبت نشده' THEN excluded.address ELSE clinics.address END,
                        phone = CASE WHEN excluded.phone != 'ثبت نشده' THEN excluded.phone ELSE clinics.phone END,
                        status = 'pending'
                    """,
                    [clinic_name, clinic_address, clinic_phone],
                )

            result = client.execute(
                """
                INSERT INTO experiences (
                    therapist_name, clinic_name, clinic_address, clinic_phone,
                    neighborhood, therapy_domain, approach, experience_text, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                [
                    data.get("therapist_name", "نامشخص"),
                    clinic_name,
                    clinic_address,
                    clinic_phone,
                    data.get("neighborhood", "نامشخص"),
                    data.get("therapy_domain", "نامشخص"),
                    data.get("approach", "نامشخص"),
                    exp_text,
                ],
            )
            exp_id = result.last_insert_rowid

        await update.message.reply_text(
            "✅ سپاس از همراهی شما! تجربه ارزشمندتان به صورت ناشناس ثبت شد و پس از تایید، در سامانه نمایش داده خواهد شد.",
            reply_markup=get_menu_keyboard(update.effective_user.id),
        )

        if ADMIN_ID:
            new_clinic_tag = "🆕 (کلینیک/اطلاعات جدید برای افزودن به دکمه‌ها)" if is_new_clinic else ""
            admin_msg = (
                f"🆕 **تجربه جدید ثبت شد (کد #{exp_id})**\n\n"
                f"👤 **درمانگر:** {data.get('therapist_name')}\n"
                f"🏥 **کلینیک:** {clinic_name} {new_clinic_tag}\n"
                f"🗺 **آدرس کلینیک:** {clinic_address}\n"
                f"☎️ **شماره تماس:** {clinic_phone}\n"
                f"📍 **محله:** {data.get('neighborhood')}\n"
                f"🎯 **حوزه درمان:** {data.get('therapy_domain')}\n"
                f"🧠 **رویکرد:** {data.get('approach')}\n"
                f"📝 **متن تجربه:**\n{exp_text}"
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

    except Exception as e:
        logger.error(f"Database error in save_experience: {e}")
        await update.message.reply_text(
            "⚠️ خطایی در ذخیره‌سازی رخ داد. لطفاً دوباره /start را بزنید و تلاش کنید.",
            reply_markup=get_menu_keyboard(update.effective_user.id),
        )

    context.user_data.clear()
    return ConversationHandler.END

# ---------------------------------------------------------
# ۷. فرآیند گزینه‌ای جستجوی درمانگر
# ---------------------------------------------------------
async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🔍 روش جستجوی خود را از گزینه‌های زیر انتخاب کنید:",
        reply_markup=SEARCH_TYPE_KEYBOARD,
    )
    return SEARCH_TYPE

async def select_search_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    if choice == BACK_BTN:
        return await start(update, context)

    if choice == "📋 آخرین تجربه‌های ثبت‌شده":
        return await show_search_results(update, context, column=None, query_str=None)

    context.user_data["search_mode"] = choice

    if choice == "👤 جستجو با نام درمانگر":
        await update.message.reply_text(
            "👤 نام یا نام خانوادگی درمانگر مورد نظر را بنویسید:",
            reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
        )
    elif choice == "🏥 جستجو با کلینیک":
        await update.message.reply_text(
            "🏥 کلینیک مورد نظر را از دکمه‌های زیر انتخاب کنید:",
            reply_markup=build_clinics_keyboard(include_add_new=False),
        )
    elif choice == "🎯 جستجو با حوزه درمان":
        await update.message.reply_text(
            "🎯 حوزه درمان مورد نظر را انتخاب کنید:",
            reply_markup=DOMAIN_KEYBOARD,
        )
    elif choice == "🧠 جستجو با رویکرد":
        await update.message.reply_text(
            "🧠 رویکرد درمانی مورد نظر را انتخاب کنید:",
            reply_markup=APPROACH_KEYBOARD,
        )
    elif choice == "📍 جستجو با محله":
        await update.message.reply_text(
            "📍 محله مورد نظر را انتخاب کنید:",
            reply_markup=NEIGHBORHOOD_KEYBOARD,
        )
    else:
        return await show_search_results(update, context, column="all", query_str=choice)

    return SEARCH_QUERY

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    mode = context.user_data.get("search_mode", "")
    col_map = {
        "👤 جستجو با نام درمانگر": "therapist_name",
        "🏥 جستجو با کلینیک": "clinic_name",
        "🎯 جستجو با حوزه درمان": "therapy_domain",
        "🧠 جستجو با رویکرد": "approach",
        "📍 جستجو با محله": "neighborhood",
    }
    column = col_map.get(mode, "all")
    return await show_search_results(update, context, column=column, query_str=text)

async def show_search_results(
    update: Update, context: ContextTypes.DEFAULT_TYPE, column: str | None, query_str: str | None
) -> int:
    try:
        with get_db() as client:
            if not query_str:
                rs = client.execute(
                    """
                    SELECT therapist_name, clinic_name, clinic_address, clinic_phone,
                           neighborhood, therapy_domain, approach, experience_text
                    FROM experiences
                    WHERE status = 'approved'
                    ORDER BY id DESC LIMIT 10
                    """
                )
            elif column and column != "all":
                q = f"%{query_str}%"
                rs = client.execute(
                    f"""
                    SELECT therapist_name, clinic_name, clinic_address, clinic_phone,
                           neighborhood, therapy_domain, approach, experience_text
                    FROM experiences
                    WHERE status = 'approved' AND {column} LIKE ?
                    ORDER BY id DESC LIMIT 10
                    """,
                    [q],
                )
            else:
                q = f"%{query_str}%"
                rs = client.execute(
                    """
                    SELECT therapist_name, clinic_name, clinic_address, clinic_phone,
                           neighborhood, therapy_domain, approach, experience_text
                    FROM experiences
                    WHERE status = 'approved' AND (
                        therapist_name LIKE ? OR
                        clinic_name LIKE ? OR
                        neighborhood LIKE ? OR
                        therapy_domain LIKE ? OR
                        approach LIKE ?
                    )
                    ORDER BY id DESC LIMIT 10
                    """,
                    [q, q, q, q, q],
                )
            rows = rs.rows

        if not rows:
            await update.message.reply_text(
                "😔 هنوز تجربه‌ای مطابق با انتخاب شما ثبت نشده است.\n"
                "می‌توانید گزینه دیگری را امتحان کنید یا تجربه خود را ثبت نمایید.",
                reply_markup=get_menu_keyboard(update.effective_user.id),
            )
            return ConversationHandler.END

        response = f"🔎 **نتایج یافت‌شده ({len(rows)} مورد):**\n\n"
        for r in rows:
            addr_line = f"\n🗺 **آدرس کلینیک:** {r[2]}" if r[2] and r[2] != "ثبت نشده" else ""
            phone_line = f"\n☎️ **شماره تماس:** {r[3]}" if r[3] and r[3] != "ثبت نشده" else ""
            response += (
                f"👤 **درمانگر:** {r[0]}\n"
                f"🏥 **کلینیک:** {r[1]} (📍 {r[4]}){addr_line}{phone_line}\n"
                f"🎯 **حوزه درمان:** {r[5]} | 🧠 **رویکرد:** {r[6]}\n"
                f"💬 **تجربه مراجع:** {r[7]}\n"
                "────────────────────\
