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
# ۳. اتصال ناهمزمان (Async) به دیتابیس Turso و همگام‌سازی کامل جداول
# ---------------------------------------------------------
def get_db():
    db_url = TURSO_DATABASE_URL.replace("libsql://", "https://")
    return libsql_client.create_client(
        url=db_url,
        auth_token=TURSO_AUTH_TOKEN,
    )

async def init_db():
    async with get_db() as client:
        # بررسی وجود ستون‌های ناسازگار NOT NULL در جدول قدیمی experiences
        try:
            pragma_rs = await client.execute("PRAGMA table_info(experiences)")
            if pragma_rs.rows:
                expected_cols = {
                    "id", "therapist_name", "clinic_name", "clinic_address",
                    "clinic_phone", "neighborhood", "therapy_domain",
                    "approach", "experience_text", "status", "created_at"
                }
                # اگر ستون قدیمی اجباری (notnull == 1) خارج از لیست جدید وجود داشت، جدول را مهاجرت می‌دهیم
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
    kb = await build_clinics_keyboard(include_add_new=True)
    await update.message.reply_text(
        "🏥 کلینیک را از لیست زیر انتخاب کنید (اگر در لیست نیست، گزینه «➕ ثبت کلینیک جدید» را بزنید):",
        reply_markup=kb,
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

    context.user_data["clinic_name"] = text
    existing_address = None
    existing_phone = None
    try:
        async with get_db() as client:
            rs = await client.execute("SELECT address, phone FROM clinics WHERE name = ?", [text])
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
        f"🗺 آدرس کلینیک **{text}** را بنویسید (مثال: بلوار سجاد، بزرگمهر جنوبی ۲):",
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
        f"☎️ شماره تلفن پذیرش یا نوبت‌دهی **{clinic_name}** را بنویسید (مثال: 05137654321):",
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
        "🧠 **رویکرد درمانی** درمانگر چه بوده است؟ از گزینه‌های زیر انتخاب کنید:",
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
        async with get_db() as client:
            # ذخیره یا به‌روزرسانی امن کلینیک بدون وابستگی به ON CONFLICT
            if is_new_clinic and clinic_name not in ["مطب شخصی / نامشخص", "نامشخص"]:
                check_rs = await client.execute(
                    "SELECT id, address, phone FROM clinics WHERE name = ?", [clinic_name]
                )
                if check_rs.rows:
                    c_id, old_addr, old_phone = check_rs.rows[0]
                    final_addr = clinic_address if clinic_address != "ثبت نشده" else (old_addr or "ثبت نشده")
                    final_phone = clinic_phone if clinic_phone != "ثبت نشده" else (old_phone or "ثبت نشده")
                    await client.execute(
                        "UPDATE clinics SET address = ?, phone = ?, status = 'pending' WHERE id = ?",
                        [final_addr, final_phone, c_id],
                    )
                else:
                    await client.execute(
                        "INSERT INTO clinics (name, address, phone, status) VALUES (?, ?, ?, 'pending')",
                        [clinic_name, clinic_address, clinic_phone],
                    )

            result = await client.execute(
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
            exp_id = result.last_insert_rowid or 1

        await update.message.reply_text(
            "✅ سپاس از همراهی شما! تجربه ارزشمندتان به صورت ناشناس ثبت شد و پس از تایید، در سامانه نمایش داده خواهد شد.",
            reply_markup=get_menu_keyboard(update.effective_user.id),
        )

        if ADMIN_ID:
            new_clinic_tag = "🆕 (کلینیک/اطلاعات جدید)" if is_new_clinic else ""
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
            f"⚠️ خطایی در ذخیره‌سازی رخ داد ({str(e)[:80]}). لطفاً دوباره /start را بزنید.",
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
        kb = await build_clinics_keyboard(include_add_new=False)
        await update.message.reply_text(
            "🏥 کلینیک مورد نظر را از دکمه‌های زیر انتخاب کنید:",
            reply_markup=kb,
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
        async with get_db() as client:
            if not query_str:
                rs = await client.execute(
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
                rs = await client.execute(
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
                rs = await client.execute(
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
                "────────────────────\n"
            )

        await update.message.reply_text(
            response,
            parse_mode="Markdown",
            reply_markup=get_menu_keyboard(update.effective_user.id),
        )
    except Exception as e:
        logger.error(f"Search error: {e}")
        await update.message.reply_text(
            "⚠️ خطایی در جستجو رخ داد. لطفاً دوباره تلاش کنید.",
            reply_markup=get_menu_keyboard(update.effective_user.id),
        )

    return ConversationHandler.END

# ---------------------------------------------------------
# ۸. پنل مدیریت و تایید هم‌زمان تجربه، آدرس و تلفن کلینیک
# ---------------------------------------------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    async with get_db() as client:
        rs = await client.execute(
            """
            SELECT id, therapist_name, clinic_name, clinic_address, clinic_phone,
                   neighborhood, therapy_domain, approach, experience_text
            FROM experiences WHERE status = 'pending'
            """
        )
        pending = rs.rows

    if not pending:
        await update.message.reply_text(
            "🎉 هیچ تجربه‌ای در صف انتظار نیست! همه چیز بررسی شده است.",
            reply_markup=ADMIN_KEYBOARD,
        )
        return

    for item in pending:
        msg = (
            f"⏳ **در انتظار تایید (کد #{item[0]})**\n\n"
            f"👤 درمانگر: {item[1]}\n"
            f"🏥 کلینیک: {item[2]}\n"
            f"🗺 آدرس: {item[3]} | ☎️ تلفن: {item[4]}\n"
            f"📍 محله: {item[5]}\n"
            f"🎯 حوزه: {item[6]} | 🧠 رویکرد: {item[7]}\n"
            f"📝 متن: {item[8]}"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ تایید و انتشار", callback_data=f"approve_{item[0]}"),
                    InlineKeyboardButton("❌ رد کردن", callback_data=f"reject_{item[0]}"),
                ]
            ]
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

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
                f"{query.message.text}\n\n✅ **تایید و منتشر شد (کلینیک، آدرس و تلفن نیز در سیستم ثبت شدند).**",
                parse_mode="Markdown",
            )
        elif action == "reject":
            await client.execute("UPDATE experiences SET status = 'rejected' WHERE id = ?", [exp_id])
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ **رد شد.**", parse_mode="Markdown"
            )

# ---------------------------------------------------------
# ۹. اجرای هم‌زمان وب‌سرور و Webhook تلگرام
# ---------------------------------------------------------
async def health_check(request):
    return web.Response(text="Soroush Bot is running!", status=200)

async def run_bot_and_server() -> None:
    await init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^✍️ ثبت تجربه$"), start_submit),
            MessageHandler(filters.Regex("^🔍 جستجوی درمانگر$"), start_search),
        ],
        states={
            THERAPIST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_therapist_name)],
            CLINIC_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_clinic)],
            NEW_CLINIC_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_clinic_name)],
            NEW_CLINIC_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_clinic_address)],
            NEW_CLINIC_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_clinic_phone)],
            CONFIRM_EXISTING_CLINIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_existing_clinic)],
            NEIGHBORHOOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_neighborhood)],
            THERAPY_DOMAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_therapy_domain)],
            APPROACH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_approach)],
            EXPERIENCE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_experience)],
            SEARCH_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_search_type)],
            SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, perform_search)],
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(f"^{BACK_BTN}$"), start),
        ],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.Regex("^ℹ️ راهنمای سروش$"), help_command))
    application.add_handler(MessageHandler(filters.Regex("^📊 پنل مدیریت و گزارش‌ها$"), admin_panel))
    application.add_handler(CallbackQueryHandler(handle_admin_callback))
    application.add_handler(conv_handler)

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

    await application.initialize()
    await application.start()
    await application.bot.set_webhook(url=webhook_url)

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_post(webhook_path, telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info("Soroush Bot & Health Check Server started successfully.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(run_bot_and_server())
